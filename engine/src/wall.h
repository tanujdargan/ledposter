// Pure wall geometry, power math and the mode interface: no hardware
// dependency, so it is unit tested on any machine (see tests/test_engine.cpp).
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

struct Frame {
  int w, h;
  std::vector<uint8_t> rgb;  // RGB24, row-major
  Frame(int w, int h) : w(w), h(h), rgb(size_t(w) * h * 3) {}
  void set(int x, int y, uint8_t r, uint8_t g, uint8_t b) {
    if (x < 0 || y < 0 || x >= w || y >= h) return;
    uint8_t *p = &rgb[(size_t(y) * w + x) * 3];
    p[0] = r, p[1] = g, p[2] = b;
  }
  void fill(uint8_t r, uint8_t g, uint8_t b) {
    for (int y = 0; y < h; y++)
      for (int x = 0; x < w; x++) set(x, y, r, g, b);
  }
};

// Where a panel sits on the logical grid. rotate is how far the panel is
// physically turned clockwise from upright (0, 90, 180, 270).
struct Panel {
  int col = 0, row = 0, rotate = 0;
};

struct Point {
  int x, y;
};

// Maps the logical canvas (panels on a col/row grid) to the coordinates
// rpi-rgb-led-matrix exposes for a chain: one strip chain_length*size wide,
// with chain index 0 (the panel plugged into the Pi) at the RIGHT end
// (lib/pixel-mapper.cc: "first panel is top-right corner").
struct Layout {
  int size;
  std::vector<Panel> panels;  // chain order: [0] is plugged into the Pi
  int width = 0, height = 0;  // logical canvas
  std::vector<Point> lut;     // logical y*width+x -> physical; {-1,-1} = no panel

  Layout(int size, std::vector<Panel> chain) : size(size), panels(std::move(chain)) {
    if (panels.empty()) throw std::runtime_error("matrix.panels is empty");
    for (auto &p : panels) {
      if (p.col < 0 || p.row < 0) throw std::runtime_error("panel col/row must be >= 0");
      if (p.rotate % 90 || p.rotate < 0 || p.rotate > 270)
        throw std::runtime_error("panel rotate must be 0, 90, 180 or 270");
      width = std::max(width, (p.col + 1) * size);
      height = std::max(height, (p.row + 1) * size);
    }
    lut.assign(size_t(width) * height, {-1, -1});
    const int n = panels.size(), s = size;
    for (int i = 0; i < n; i++) {
      const Panel &p = panels[i];
      for (int ly = 0; ly < s; ly++)
        for (int lx = 0; lx < s; lx++) {
          int nx = lx, ny = ly;  // panel-native coords that appear at local (lx, ly)
          if (p.rotate == 90) nx = ly, ny = s - 1 - lx;
          if (p.rotate == 180) nx = s - 1 - lx, ny = s - 1 - ly;
          if (p.rotate == 270) nx = s - 1 - ly, ny = lx;
          Point &dst = lut[size_t(p.row * s + ly) * width + p.col * s + lx];
          if (dst.x >= 0)
            throw std::runtime_error("two panels share col " + std::to_string(p.col) +
                                     " row " + std::to_string(p.row));
          dst = {(n - 1 - i) * s + nx, ny};
        }
    }
  }
};

// Estimated panel current in amps, linear in the RGB sum. The engine turns off
// the library's luminance correction and applies gamma itself (media at
// transcode time, everything else via gamma_lut), so pixel values are linear
// PWM duty and this tracks real current rather than overestimating it.
// amps_full_white_per_panel is the calibration knob (~6A = 30W at 5V).
inline double estimate_amps(const Frame &f, int panel_size, double amps_full_white_per_panel,
                            int brightness_pct) {
  uint64_t sum = 0;
  for (uint8_t v : f.rgb) sum += v;
  return sum / (255.0 * 3 * panel_size * panel_size) * amps_full_white_per_panel *
         brightness_pct / 100.0;
}

// Factor (<= 1) to scale a frame's pixel values by so it stays under the ceiling.
inline double limiter_scale(double amps, double ceiling_amps) {
  return amps > ceiling_amps ? ceiling_amps / amps : 1.0;
}

// The brightness chain (docs/ARCHITECTURE.md): effective = min(user, auto),
// then the limiter. auto_pct is NaN when auto is off or the sensor has no
// reading; the user setting then applies on its own.
inline int effective_pct(int user, double auto_pct) {
  return std::isnan(auto_pct) ? user : std::min(user, int(std::lround(auto_pct)));
}

// How one frame goes out. The output brightness is a whole percent because
// that is what the matrix takes (FrameCanvas::SetBrightness; with luminance
// correction off the panel shows each value as v * pct / 100, see scale_frame).
struct Power {
  int want = 0, pct = 0;        // effective brightness asked for, and what the limiter allows
  double amps = 0, amps_unlimited = 0;
  bool limited() const { return pct < want; }
};

// The limiter: the highest pct <= want whose estimate stays under the ceiling.
inline Power limit_power(const Frame &f, int panel_size, double amps_full_white_per_panel,
                         int want, double ceiling_amps) {
  const double full = estimate_amps(f, panel_size, amps_full_white_per_panel, 100);
  Power p{want, want, full * want / 100, full * want / 100};
  if (p.amps > ceiling_amps) {
    p.pct = int(ceiling_amps / full * 100);
    while (p.pct > 0 && full * p.pct / 100 > ceiling_amps) p.pct--;  // float rounding guard
    p.amps = full * p.pct / 100;
  }
  return p;
}

// The frame as the panels show it at pct (same integer math as the library):
// the preview stream sends this, and tests check the ceiling against it.
inline void scale_frame(const Frame &f, int pct, Frame &out) {
  for (size_t i = 0; i < f.rgb.size(); i++) out.rgb[i] = f.rgb[i] * pct / 100;
}

// display.gamma as a LUT, for output the engine generates (patterns, effects,
// text). Cached media is gamma corrected at transcode time and skips it.
inline std::array<uint8_t, 256> gamma_lut(double gamma) {
  std::array<uint8_t, 256> lut;
  for (int i = 0; i < 256; i++) lut[i] = std::lround(std::pow(i / 255.0, gamma) * 255);
  return lut;
}

// A mode draws the whole canvas for t_ms (time since the mode started) and
// returns the t_ms at which it next needs to draw. gamma: output still needs
// the engine's gamma LUT (false for pre-corrected media).
struct Mode {
  std::string name;
  bool gamma = true;
  std::function<int64_t(Frame &, int64_t t_ms)> render;
};
