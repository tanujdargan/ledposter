// Overlays: short animations (game hook events) and on-wall text drawn on top
// of whatever mode is running, which keeps running underneath. They are drawn
// into a layer with alpha and blended onto the mode's frame (after the mode's
// own gamma, the layer's colours go through the same LUT), before brightness
// and the limiter, so the limiter sees the composited frame.
#pragma once
#include <array>
#include <cmath>
#include <cstdint>
#include <ctime>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "clockfont.h"
#include "effects.h"
#include "text.h"
#include "wall.h"

// RGB plus alpha per pixel. Colours are before gamma, like a mode's output.
struct Layer {
  int w, h;
  std::vector<uint8_t> rgb, a;
  Layer(int w, int h) : w(w), h(h), rgb(size_t(w) * h * 3), a(size_t(w) * h) {}
  void clear() {
    std::fill(rgb.begin(), rgb.end(), 0);
    std::fill(a.begin(), a.end(), 0);
  }
  // Paints c over what is there with alpha k (0..1).
  void put(int x, int y, fx::RGB c, float k) {
    if (x < 0 || y < 0 || x >= w || y >= h || k <= 0) return;
    const size_t i = size_t(y) * w + x;
    const int ai = int(std::min(k, 1.0f) * 255);
    uint8_t *p = &rgb[i * 3];
    // Straight-alpha "over" onto the layer itself.
    const int ao = a[i], an = ai + ao * (255 - ai) / 255;
    if (an == 0) return;
    auto mix = [&](uint8_t dst, uint8_t src) {
      return uint8_t((src * ai + dst * ao * (255 - ai) / 255) / an);
    };
    p[0] = mix(p[0], c.r), p[1] = mix(p[1], c.g), p[2] = mix(p[2], c.b);
    a[i] = uint8_t(an);
  }
  void rect(int x0, int y0, int x1, int y1, fx::RGB c, float k) {
    for (int y = std::max(0, y0); y < std::min(h, y1); y++)
      for (int x = std::max(0, x0); x < std::min(w, x1); x++) put(x, y, c, k);
  }
  Plot plot(fx::RGB c, float k) {
    return [this, c, k](int x, int y) { put(x, y, c, k); };
  }
};

// Blends the layer onto f (f is already gamma corrected): lut maps the layer's colours.
inline void composite(Frame &f, const Layer &l, const std::array<uint8_t, 256> &lut) {
  const size_t n = size_t(f.w) * f.h;
  for (size_t i = 0; i < n; i++) {
    const int a = l.a[i];
    if (!a) continue;
    uint8_t *p = &f.rgb[i * 3];
    const uint8_t *c = &l.rgb[i * 3];
    for (int k = 0; k < 3; k++) p[k] = uint8_t((p[k] * (255 - a) + lut[c[k]] * a + 127) / 255);
  }
}

// One overlay: draw(layer, age_ms) for age in [0, ms).
struct Overlay {
  std::string name;
  int64_t ms;
  std::function<void(Layer &, int64_t age)> draw;
};

// Fade in over in_ms, out over the last out_ms.
inline float envelope(int64_t age, int64_t total, int64_t in_ms, int64_t out_ms) {
  float k = 1;
  if (age < in_ms) k = float(age) / in_ms;
  if (age > total - out_ms) k = std::min(k, float(total - age) / out_ms);
  return std::clamp(k, 0.0f, 1.0f);
}

// A dark band across the canvas with the text fitted in it.
inline void text_band(Layer &l, const FontSet &fonts, const std::string &text, fx::RGB color,
                      float k, int cy, bool big) {
  const int margin = std::max(2, l.w / 32);
  std::vector<std::pair<std::string, int>> cands;
  if (big) cands = {{"10x20", 2}, {"9x18B", 2}, {"10x20", 1}, {"9x18B", 1}};
  for (auto c : std::vector<std::pair<std::string, int>>{
           {"7x13B", 1}, {"6x10", 1}, {"5x8", 1}, {"tom-thumb", 1}})
    cands.push_back(c);
  const TextFit t = fit_text(fonts, text, l.w - 2 * margin, l.h - 2 * margin, cands);
  if (!t.font) return;
  const int pad = std::max(2, t.line_h / 4);
  const int top = std::clamp(cy - t.h / 2, margin, std::max(margin, l.h - margin - t.h));
  l.rect(0, top - pad, l.w, top + t.h + pad, {0, 0, 0}, 0.75f * k);
  draw_fit(t, l.w / 2, top, l.w, l.plot(color, k));
}

// The game hook events (spec: Game hook). Durations 1.5 to 3s.
inline std::optional<Overlay> event_overlay(const std::string &ev, const FontSet &fonts, int w,
                                            int h, uint32_t seed) {
  const float m = std::min(w, h);
  if (ev == "kill") {  // hit marker pops at the centre, then "KILL"
    return Overlay{ev, 1500, [&fonts, m](Layer &l, int64_t age) {
                     const float k = envelope(age, 1500, 60, 350);
                     const float cx = l.w / 2.0f, cy = l.h / 2.0f;
                     const float pop = 1 + 0.4f * std::exp(-age / 120.0f);
                     const float r0 = m * 0.07f * pop, r1 = m * 0.17f * pop;
                     for (int q = 0; q < 4; q++) {
                       const float dx = (q & 1) ? 1 : -1, dy = (q & 2) ? 1 : -1;
                       for (float r = r0; r <= r1; r += 0.5f)
                         for (int th = 0; th < std::max(1, int(m / 40)); th++)
                           l.put(int(cx + dx * r * 0.7071f) + th, int(cy + dy * r * 0.7071f), {255, 255, 255}, k);
                     }
                     if (age > 180) text_band(l, fonts, "KILL", {255, 40, 30}, k, int(cy + m * 0.32f), true);
                   }};
  }
  if (ev == "killstreak") {  // gold shockwaves and a blinking "KILLSTREAK"
    return Overlay{ev, 2500, [&fonts](Layer &l, int64_t age) {
                     const float k = envelope(age, 2500, 80, 400);
                     const float cx = l.w / 2.0f, cy = l.h / 2.0f;
                     for (int ring = 0; ring < 3; ring++) {
                       const float a = (age - ring * 250) / 900.0f;
                       if (a < 0 || a > 1) continue;
                       const float r = a * std::sqrt(float(l.w * l.w + l.h * l.h)) / 2;
                       const int n = std::max(16, int(r * 7));
                       for (int i = 0; i < n; i++) {
                         const float th = i * float(2 * M_PI) / n;
                         for (float d = -1; d <= 1; d += 1)
                           l.put(int(cx + (r + d) * std::cos(th)), int(cy + (r + d) * std::sin(th)),
                                 {255, 190, 0}, (1 - a) * k * (d == 0 ? 1 : 0.5f));
                       }
                     }
                     const bool on = age > 1500 || (age / 180) % 2 == 0;
                     if (on) text_band(l, fonts, "KILLSTREAK", {255, 200, 20}, k, int(cy), true);
                   }};
  }
  if (ev == "death") {  // the edges bleed red, "DOWN"
    return Overlay{ev, 2000, [&fonts](Layer &l, int64_t age) {
                     const float k = envelope(age, 2000, 150, 500);
                     const float cx = l.w / 2.0f, cy = l.h / 2.0f;
                     const float pulse = 0.75f + 0.25f * std::cos(age / 160.0f);
                     for (int y = 0; y < l.h; y++)
                       for (int x = 0; x < l.w; x++) {
                         const float dx = (x - cx) / cx, dy = (y - cy) / cy;
                         const float d = std::sqrt(dx * dx + dy * dy) / 1.4142f;  // 0 centre, 1 corner
                         const float v = std::clamp((d - 0.25f) / 0.6f, 0.0f, 1.0f);
                         l.put(x, y, {200, 0, 0}, (0.25f + 0.75f * v) * k * pulse);
                       }
                     text_band(l, fonts, "DOWN", {255, 255, 255}, k, int(cy), true);
                   }};
  }
  if (ev == "win") {  // confetti bursts from the bottom corners, "VICTORY"
    return Overlay{ev, 3000, [&fonts, seed](Layer &l, int64_t age) {
                     const float k = envelope(age, 3000, 50, 500);
                     static const fx::RGB cols[] = {{255, 60, 60}, {255, 210, 0}, {60, 220, 255},
                                                    {80, 255, 80}, {255, 80, 255}, {255, 255, 255}};
                     const float s = age / 1000.0f, g = l.h * 0.55f;
                     const int n = 40 + l.w * l.h / 300;
                     for (int i = 0; i < n; i++) {
                       const bool left = i & 1;
                       const float a = float(M_PI) * (left ? 0.25f + 0.2f * fx::hashf(seed, i, 1)
                                                           : 0.55f + 0.2f * fx::hashf(seed, i, 1));
                       const float v = l.h * (0.9f + 0.6f * fx::hashf(seed, i, 2));
                       const float x = (left ? 0 : l.w - 1) + std::cos(a) * v * s * 0.6f;
                       const float y = l.h - std::sin(a) * v * s + g * s * s;
                       const fx::RGB c = cols[fx::hash3(seed, i, 3) % 6];
                       const bool tw = (int(age / 70) + i) % 3;  // twinkle
                       l.put(int(x), int(y), c, k * (tw ? 1 : 0.5f));
                       l.put(int(x) + 1, int(y), c, k * (tw ? 1 : 0.5f));
                     }
                     if (age > 300) text_band(l, fonts, "VICTORY", {255, 210, 40}, k, l.h / 3, true);
                   }};
  }
  return std::nullopt;
}

inline const std::vector<std::string> kEvents = {"kill", "killstreak", "death", "win"};

// show_text: a message in a band in the middle of the wall for ms.
inline Overlay text_overlay(const FontSet &fonts, const std::string &text, uint32_t color, int64_t ms) {
  const fx::RGB c = fx::rgb(color);
  return {"text", ms, [&fonts, text, c, ms](Layer &l, int64_t age) {
            text_band(l, fonts, text, c, envelope(age, ms, 120, 300), l.h / 2, false);
          }};
}

// The active overlays: one event slot and one text slot (text on top). A new
// event or text replaces the one in its slot. Times are engine ms.
struct Overlays {
  struct Active {
    Overlay o;
    int64_t start = -1;  // -1: starts at the next frame
  };
  std::optional<Active> event, text;
  Layer layer;
  explicit Overlays(int w, int h) : layer(w, h) {}

  void set_event(Overlay o) { event = Active{std::move(o)}; }
  void set_text(Overlay o) { text = Active{std::move(o)}; }
  void clear_text() { text.reset(); }

  // Drops expired overlays; true when something is left to draw at t.
  bool active(int64_t t) {
    for (auto *s : {&event, &text}) {
      if (!*s) continue;
      if ((*s)->start < 0) (*s)->start = t;
      if (t - (*s)->start >= (*s)->o.ms) s->reset();
    }
    return event || text;
  }
  std::vector<std::string> names() const {
    std::vector<std::string> n;
    if (event) n.push_back(event->o.name);
    if (text) n.push_back(text->o.name);
    return n;
  }
  // Draws the active overlays onto f (call active(t) first).
  void apply(Frame &f, int64_t t, const std::array<uint8_t, 256> &lut) {
    if (!event && !text) return;
    layer.clear();
    if (event) event->o.draw(layer, t - event->start);
    if (text) text->o.draw(layer, t - text->start);
    composite(f, layer, lut);
  }
};

// ---- clock overlay ----

// The time, and the date under it, on top of whatever plays (config clock_overlay, applied
// live by reload_config). Lock-screen style: Inter Light, soft white, a soft dark halo so it
// reads on bright pictures too. Rendered into a small layer only when the text or the
// settings change (once a minute); each frame just blends that box.
inline const std::vector<std::string> kClockPositions = {
    "top-left", "top-center", "top-right", "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right"};

// Sizes in px (clock_overlay.time_px, date_px): one pre-rasterized font per px in
// kClockTimeMinPx..MaxPx and kClockDateMinPx..MaxPx (clockfont.h). Configs from before the
// sliders have time_size/date_size names instead: the px those fonts had (resolve_clock_sizes).
inline const std::vector<std::pair<std::string, int>> kClockTimeSizes = {{"s", 24}, {"m", 32}, {"l", 39}, {"xl", 46}};
inline const std::vector<std::pair<std::string, int>> kClockDateSizes = {{"s", 9}, {"m", 12}, {"l", 16}};

struct ClockOverlayCfg {
  bool enabled = false, hour24 = true;
  int pos = 7;  // index in kClockPositions
  int time_px = 32, date_px = 9;  // date_px 0: no date
  bool operator==(const ClockOverlayCfg &o) const {
    return enabled == o.enabled && hour24 == o.hour24 && pos == o.pos && time_px == o.time_px &&
           date_px == o.date_px;
  }
};

// The clock_overlay size keys as found in the config (absent ones empty).
struct ClockSizeKeys {
  std::optional<int> time_px, date_px;
  std::optional<std::string> time_size, date_size;
  std::optional<bool> date;
};
// Sets c's sizes from the keys: px when given, else the old names (a config from before the
// sliders, until the web app next saves it; date: false was no date). Throws on a bad value.
inline void resolve_clock_sizes(ClockOverlayCfg &c, const ClockSizeKeys &k) {
  auto named = [](const std::optional<std::string> &n, const std::vector<std::pair<std::string, int>> &sizes,
                  const char *def, const char *err) {
    for (auto &[name, px] : sizes)
      if (name == n.value_or(def)) return px;
    throw std::runtime_error(err);
  };
  c.time_px = k.time_px ? *k.time_px : named(k.time_size, kClockTimeSizes, "m", "clock_overlay.time_size must be s, m, l or xl");
  c.date_px = k.date_px            ? *k.date_px
              : !k.date.value_or(true) ? 0
                                       : named(k.date_size, kClockDateSizes, "s", "clock_overlay.date_size must be s, m or l");
  if (c.time_px < kClockTimeMinPx || c.time_px > kClockTimeMaxPx)
    throw std::runtime_error("clock_overlay.time_px must be " + std::to_string(kClockTimeMinPx) + " to " +
                             std::to_string(kClockTimeMaxPx));
  if (c.date_px != 0 && (c.date_px < kClockDateMinPx || c.date_px > kClockDateMaxPx))
    throw std::runtime_error("clock_overlay.date_px must be 0 (no date) or " + std::to_string(kClockDateMinPx) +
                             " to " + std::to_string(kClockDateMaxPx));
}

// Ink extent of text in font: left and right of the ink from the pen start, top and bottom
// from the baseline.
struct InkBox {
  int x0 = 1 << 20, x1 = -(1 << 20), y0 = 1 << 20, y1 = -(1 << 20);
};
inline InkBox ink_box(const AaFont &f, const std::string &text) {
  InkBox b;
  int pen = 0;
  for (char ch : text) {
    const AaGlyph *g = f.get(uint8_t(ch));
    if (!g) continue;
    if (g->w) {
      b.x0 = std::min(b.x0, pen + g->x), b.x1 = std::max(b.x1, pen + g->x + g->w);
      b.y0 = std::min(b.y0, g->y), b.y1 = std::max(b.y1, g->y + g->h);
    }
    pen += g->adv;
  }
  return b;
}

// Draws text's coverage into mask (w wide), the ink's top-left at (x, y).
inline void draw_aa(const AaFont &f, const std::string &text, const InkBox &b, int x, int y,
                    std::vector<uint8_t> &mask, int w) {
  int pen = x - b.x0;
  for (char ch : text) {
    const AaGlyph *g = f.get(uint8_t(ch));
    if (!g) continue;
    for (int r = 0; r < g->h; r++)
      for (int c = 0; c < g->w; c++) {
        uint8_t &m = mask[size_t(y - b.y0 + g->y + r) * w + pen + g->x + c];
        m = std::max(m, f.alpha[g->off + r * g->w + c]);
      }
    pen += g->adv;
  }
}

struct ClockOverlay {
  static constexpr int kMargin = 6, kHalo = 3;  // canvas px
  ClockOverlayCfg cfg;
  std::string text;  // what the layer shows ("" = nothing yet)
  std::time_t at = -1;
  int x = 0, y = 0;  // the layer's top-left on the canvas
  Layer layer{0, 0};

  // Re-renders if the time text or the settings changed; true if they did (redraw the frame).
  bool update(const ClockOverlayCfg &c, std::time_t now, int w, int h) {
    if (now == at && c == cfg) return false;
    at = now;
    std::tm tm{};
    localtime_r(&now, &tm);
    ClockStyle cs;
    cs.hour24 = c.hour24;
    const std::string time = clock_time(tm, cs), date = c.date_px ? clock_date(tm) : "";
    if (c == cfg && time + "|" + date == text) return false;
    cfg = c, text = time + "|" + date;
    render(time, date, w, h);
    return true;
  }

  void render(const std::string &time, const std::string &date, int w, int h) {
    // resolve_clock_sizes validates the sizes; clamped here anyway, an index past the table is no font.
    const AaFont &tf = *kClockTimeFonts[std::clamp(cfg.time_px, kClockTimeMinPx, kClockTimeMaxPx) - kClockTimeMinPx];
    const AaFont &df = *kClockDateFonts[std::clamp(cfg.date_px, kClockDateMinPx, kClockDateMaxPx) - kClockDateMinPx];
    const InkBox tb = ink_box(tf, time), db = ink_box(df, date);
    const int tw = tb.x1 - tb.x0, th = tb.y1 - tb.y0;
    const int kGap = std::max(3, th / 5);  // time to date, 5px at the default size
    const int dw = date.empty() ? 0 : db.x1 - db.x0, dh = date.empty() ? 0 : db.y1 - db.y0;
    const int bw = std::max(tw, dw), bh = th + (dh ? kGap + dh : 0);
    const int col = cfg.pos % 3, row = cfg.pos / 3;
    // Block position on the canvas, then the layer around it with room for the halo.
    const int bx = col == 0 ? kMargin : col == 1 ? (w - bw) / 2 : w - kMargin - bw;
    const int by = row == 0 ? kMargin : row == 1 ? (h - bh) / 2 : h - kMargin - bh;
    const int lw = bw + 2 * kHalo, lh = bh + 2 * kHalo;
    x = bx - kHalo, y = by - kHalo;
    std::vector<uint8_t> mask(size_t(lw) * lh);
    auto line_x = [&](int lwid) { return kHalo + (col == 0 ? 0 : col == 1 ? (bw - lwid) / 2 : bw - lwid); };
    draw_aa(tf, time, tb, line_x(tw), kHalo, mask, lw);
    if (dh) draw_aa(df, date, db, line_x(dw), kHalo + th + kGap, mask, lw);
    // Halo: the ink grown by a pixel, then blurred (two 3x3 box passes).
    std::vector<uint8_t> halo(mask.size()), tmp(mask.size());
    for (int r = 0; r < lh; r++)
      for (int c = 0; c < lw; c++) {
        uint8_t m = 0;
        for (int dy = -1; dy <= 1; dy++)
          for (int dx = -1; dx <= 1; dx++)
            if (r + dy >= 0 && r + dy < lh && c + dx >= 0 && c + dx < lw)
              m = std::max(m, mask[size_t(r + dy) * lw + c + dx]);
        halo[size_t(r) * lw + c] = m;
      }
    for (int pass = 0; pass < 2; pass++) {
      for (int r = 0; r < lh; r++)
        for (int c = 0; c < lw; c++) {
          int sum = 0;
          for (int dy = -1; dy <= 1; dy++)
            for (int dx = -1; dx <= 1; dx++)
              if (r + dy >= 0 && r + dy < lh && c + dx >= 0 && c + dx < lw)
                sum += halo[size_t(r + dy) * lw + c + dx];
          tmp[size_t(r) * lw + c] = uint8_t(sum / 9);
        }
      halo.swap(tmp);
    }
    layer = Layer(lw, lh);
    for (int r = 0; r < lh; r++)
      for (int c = 0; c < lw; c++) {
        const size_t i = size_t(r) * lw + c;
        layer.put(c, r, {0, 0, 0}, 0.75f * halo[i] / 255);
        layer.put(c, r, {236, 236, 232}, 0.9f * mask[i] / 255);
      }
  }

  // Blends the layer onto f (after the mode's gamma, like composite()).
  void apply(Frame &f, const std::array<uint8_t, 256> &lut) const {
    for (int r = 0; r < layer.h; r++) {
      const int fy = y + r;
      if (fy < 0 || fy >= f.h) continue;
      for (int c = 0; c < layer.w; c++) {
        const int fx = x + c;
        const int a = layer.a[size_t(r) * layer.w + c];
        if (fx < 0 || fx >= f.w || !a) continue;
        uint8_t *p = &f.rgb[(size_t(fy) * f.w + fx) * 3];
        const uint8_t *s = &layer.rgb[(size_t(r) * layer.w + c) * 3];
        for (int k = 0; k < 3; k++) p[k] = uint8_t((p[k] * (255 - a) + lut[s[k]] * a + 127) / 255);
      }
    }
  }
};
