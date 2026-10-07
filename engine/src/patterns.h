// Milestone 1 test patterns, drawn on the logical canvas.
#pragma once
#include <cstdint>
#include <string>

#include "wall.h"

// 3x5 digits, one row per byte, bit 2 = left column.
static const uint8_t kDigits[10][5] = {
    {7, 5, 5, 5, 7}, {2, 6, 2, 2, 7}, {7, 1, 7, 4, 7}, {7, 1, 7, 1, 7}, {5, 5, 7, 1, 1},
    {7, 4, 7, 1, 7}, {7, 4, 7, 5, 7}, {7, 1, 1, 1, 1}, {7, 5, 7, 5, 7}, {7, 5, 7, 1, 7},
};

inline void draw_text(Frame &f, const std::string &digits, int cx, int top, int scale) {
  int x0 = cx - (int(digits.size()) * 4 - 1) * scale / 2;
  for (char c : digits) {
    for (int r = 0; r < 5; r++)
      for (int b = 0; b < 3; b++)
        if (kDigits[c - '0'][r] >> (2 - b) & 1)
          for (int dy = 0; dy < scale; dy++)
            for (int dx = 0; dx < scale; dx++)
              f.set(x0 + b * scale + dx, top + r * scale + dy, 255, 255, 255);
    x0 += 4 * scale;
  }
}

// Each panel: colored border, up-arrow, and its chain number (1 = plugged into
// the Pi). Correct wiring + config shows every number upright in its spot.
inline void draw_panel_id(Frame &f, const Layout &L) {
  static const uint8_t hues[6][3] = {{255, 0, 0},   {0, 255, 0},   {0, 0, 255},
                                     {255, 255, 0}, {0, 255, 255}, {255, 0, 255}};
  f.fill(0, 0, 0);
  const int s = L.size;
  for (size_t i = 0; i < L.panels.size(); i++) {
    const int ox = L.panels[i].col * s, oy = L.panels[i].row * s, cx = ox + s / 2;
    const uint8_t *c = hues[i % 6];
    for (int k = 0; k < s; k++) {
      f.set(ox + k, oy, c[0], c[1], c[2]), f.set(ox + k, oy + s - 1, c[0], c[1], c[2]);
      f.set(ox, oy + k, c[0], c[1], c[2]), f.set(ox + s - 1, oy + k, c[0], c[1], c[2]);
    }
    const int a = oy + s * 3 / 64;  // arrow tip
    for (int dy = 0; dy < s * 10 / 64; dy++)
      for (int x = cx - 1 - dy; x <= cx + dy; x++) f.set(x, a + dy, 255, 255, 255);
    for (int y = a + s * 10 / 64; y < a + s * 15 / 64; y++)
      for (int x = cx - 3; x < cx + 3; x++) f.set(x, y, 255, 255, 255);
    std::string n = std::to_string(i + 1);
    draw_text(f, n, cx, oy + s * 21 / 64, (n.size() == 1 ? 8 : 4) * s / 64);
  }
}

// Draws pattern `name` for frame number t (30fps). Returns false if unknown.
inline bool draw_pattern(const std::string &name, Frame &f, const Layout &L, long t,
                         uint32_t color) {
  if (name == "panel-id") {
    draw_panel_id(f, L);
  } else if (name == "solid") {
    f.fill(color >> 16, color >> 8, color);
  } else if (name == "cycle") {  // red, green, blue, white; 2s each
    static const uint32_t seq[] = {0xff0000, 0x00ff00, 0x0000ff, 0xffffff};
    uint32_t c = seq[t / 60 % 4];
    f.fill(c >> 16, c >> 8, c);
  } else if (name == "rows") {  // one lit row walks down: catches dead address lines
    f.fill(0, 0, 0);
    for (int x = 0; x < f.w; x++) f.set(x, t % f.h, color >> 16, color >> 8, color);
  } else if (name == "cols") {  // one lit column walks right: catches shift-register faults
    f.fill(0, 0, 0);
    for (int y = 0; y < f.h; y++) f.set(t % f.w, y, color >> 16, color >> 8, color);
  } else {
    return false;
  }
  return true;
}

inline Mode pattern_mode(const std::string &name, const Layout &L, uint32_t color) {
  return {name, true, [name, &L, color](Frame &f, int64_t t) -> int64_t {
            draw_pattern(name, f, L, t / 33, color);
            return (t / 33 + 1) * 33;
          }};
}
