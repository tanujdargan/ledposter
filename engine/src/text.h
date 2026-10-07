// Text on the wall: BDF fonts through rpi-rgb-led-matrix's own parser
// (lib/bdf-font.cc, include/graphics.h; the Pi build gets it from
// librgbmatrix.a, make sim and make test compile that one file), drawn onto a
// Frame or overlay layer through a tiny Canvas adapter. Also the text scroller,
// clock and idle modes.
#pragma once
#include <graphics.h>

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "effects.h"
#include "wall.h"

// The fonts the engine offers, from fonts.dir (vendor/rpi-rgb-led-matrix/fonts).
// Loaded once at start: the matrix library drops root right after it starts,
// and the fonts dir may not be readable afterwards.
inline const std::vector<std::string> kFontNames = {"tom-thumb", "5x8",   "6x10",
                                                    "7x13B",     "9x18B", "10x20"};

struct FontSet {
  std::map<std::string, std::unique_ptr<rgb_matrix::Font>> fonts;

  // Returns the names that failed to load.
  std::vector<std::string> load(const std::string &dir) {
    std::vector<std::string> missing;
    for (const auto &n : kFontNames) {
      auto f = std::make_unique<rgb_matrix::Font>();
      if (f->LoadFont((dir + "/" + n + ".bdf").c_str()) && f->height() > 0)
        fonts[n] = std::move(f);
      else
        missing.push_back(n);
    }
    return missing;
  }
  const rgb_matrix::Font *get(const std::string &n) const {
    auto it = fonts.find(n);
    return it == fonts.end() ? nullptr : it->second.get();
  }
  std::vector<std::string> names() const {
    std::vector<std::string> out;
    for (const auto &n : kFontNames)
      if (get(n)) out.push_back(n);
    return out;
  }
};

// UTF-8 to code points; malformed bytes become U+FFFD (never reads past the end).
inline std::vector<uint32_t> utf8_decode(const std::string &s) {
  std::vector<uint32_t> out;
  for (size_t i = 0; i < s.size();) {
    const uint8_t c = s[i];
    int n = c < 0x80 ? 0 : (c & 0xe0) == 0xc0 ? 1 : (c & 0xf0) == 0xe0 ? 2 : (c & 0xf8) == 0xf0 ? 3 : -1;
    uint32_t cp = n == 0 ? c : n == 1 ? c & 0x1f : n == 2 ? c & 0x0f : c & 0x07;
    bool ok = n >= 0 && i + n < s.size() + (n == 0);
    for (int k = 1; ok && k <= n; k++) {
      const uint8_t d = s[i + k];
      ok = (d & 0xc0) == 0x80;
      cp = cp << 6 | (d & 0x3f);
    }
    if (!ok) {
      out.push_back(0xfffd);
      i++;
      continue;
    }
    out.push_back(cp);
    i += n + 1;
  }
  return out;
}

// Where glyph pixels go: plot(x, y) for each lit canvas pixel.
using Plot = std::function<void(int x, int y)>;

// rgb_matrix::Canvas over a Plot: a glyph pixel (x, y) becomes a scale x scale
// block at (ox + x*scale, oy + y*scale). Reports a huge size so DrawGlyph never
// clips; callers cull whole glyphs themselves.
struct GlyphCanvas : rgb_matrix::Canvas {
  const Plot &plot;
  int ox = 0, oy = 0, scale = 1;
  explicit GlyphCanvas(const Plot &p) : plot(p) {}
  int width() const override { return 1 << 20; }
  int height() const override { return 1 << 20; }
  void SetPixel(int x, int y, uint8_t, uint8_t, uint8_t) override {
    for (int dy = 0; dy < scale; dy++)
      for (int dx = 0; dx < scale; dx++) plot(ox + x * scale + dx, oy + y * scale + dy);
  }
  void Clear() override {}
  void Fill(uint8_t, uint8_t, uint8_t) override {}
};

inline int glyph_width(const rgb_matrix::Font &f, uint32_t cp) {
  int w = f.CharacterWidth(cp);
  if (w < 0) w = f.CharacterWidth(0xfffd);  // DrawGlyph draws this one instead
  return std::max(0, w);
}

// Width in canvas pixels.
inline int text_width(const rgb_matrix::Font &f, const std::vector<uint32_t> &cps, int scale) {
  int w = 0;
  for (uint32_t cp : cps) w += glyph_width(f, cp);
  return w * scale;
}

// Draws cps with the top of the font box at y; only glyphs touching
// [0, clip_w) are drawn. Returns the width drawn, in canvas pixels.
inline int draw_text(const rgb_matrix::Font &f, const std::vector<uint32_t> &cps, int x, int y,
                     int scale, int clip_w, const Plot &plot) {
  GlyphCanvas c(plot);
  c.scale = scale, c.oy = y;
  const rgb_matrix::Color white(255, 255, 255);
  int pen = x;
  for (uint32_t cp : cps) {
    const int gw = glyph_width(f, cp) * scale;
    if (pen + gw > 0 && pen < clip_w) {
      c.ox = pen;
      f.DrawGlyph(&c, 0, f.baseline(), white, nullptr, cp);
    }
    pen += gw;
  }
  return pen - x;
}

inline Plot frame_plot(Frame &f, fx::RGB c) {
  return [&f, c](int x, int y) { f.set(x, y, c.r, c.g, c.b); };
}

// ---- fitting text into a box ----

struct TextFit {
  const rgb_matrix::Font *font = nullptr;
  int scale = 1;
  std::vector<std::vector<uint32_t>> lines;
  int line_h = 0, w = 0, h = 0;  // canvas pixels
};

// Word-wraps cps to max_w with font at scale. Words wider than max_w are
// broken between characters (and *broke is set).
inline std::vector<std::vector<uint32_t>> wrap(const rgb_matrix::Font &f,
                                               const std::vector<uint32_t> &cps, int scale,
                                               int max_w, bool *broke = nullptr) {
  std::vector<std::vector<uint32_t>> lines(1);
  std::vector<uint32_t> word;
  auto width = [&](const std::vector<uint32_t> &v) { return text_width(f, v, scale); };
  auto flush_word = [&] {
    if (word.empty()) return;
    auto &cur = lines.back();
    std::vector<uint32_t> joined = cur;
    if (!joined.empty()) joined.push_back(' ');
    joined.insert(joined.end(), word.begin(), word.end());
    if (width(joined) <= max_w) {
      cur = joined;
    } else {
      if (!cur.empty()) lines.push_back({});
      for (uint32_t cp : word) {  // break a word that alone is too wide
        auto &l = lines.back();
        l.push_back(cp);
        if (width(l) > max_w && l.size() > 1) {
          l.pop_back(), lines.push_back({cp});
          if (broke) *broke = true;
        }
      }
    }
    word.clear();
  };
  for (uint32_t cp : cps) {
    if (cp == '\n') flush_word(), lines.push_back({});
    else if (cp == ' ') flush_word();
    else word.push_back(cp);
  }
  flush_word();
  return lines;
}

// The first (font, scale) from candidates, largest first, that fits
// max_w x max_h: on one line if one_line, else word-wrapped without breaking
// a word. Falls back to the last candidate (wrapped anyhow), cut to max_h.
inline TextFit fit_text(const FontSet &fonts, const std::string &text, int max_w, int max_h,
                        const std::vector<std::pair<std::string, int>> &candidates,
                        bool one_line = false) {
  const auto cps = utf8_decode(text);
  TextFit best;
  for (const auto &[name, scale] : candidates) {
    const rgb_matrix::Font *f = fonts.get(name);
    if (!f) continue;
    bool broke = false;
    TextFit t{f, scale, one_line ? std::vector<std::vector<uint32_t>>{cps} : wrap(*f, cps, scale, max_w, &broke),
              f->height() * scale};
    for (const auto &l : t.lines) t.w = std::max(t.w, text_width(*f, l, scale));
    t.h = t.line_h * int(t.lines.size());
    best = std::move(t);
    if (best.w <= max_w && best.h <= max_h && !broke) return best;
  }
  if (best.font && best.w > max_w) best.lines = wrap(*best.font, cps, best.scale, max_w);
  if (best.font) {
    best.w = 0;
    for (const auto &l : best.lines) best.w = std::max(best.w, text_width(*best.font, l, best.scale));
    best.h = best.line_h * int(best.lines.size());
  }
  if (best.font && best.h > max_h) {
    best.lines.resize(std::max(1, max_h / best.line_h));
    best.h = best.line_h * int(best.lines.size());
  }
  return best;
}

// Draws a fit centred on (cx, top..), each line centred.
inline void draw_fit(const TextFit &t, int cx, int top, int clip_w, const Plot &plot) {
  if (!t.font) return;
  for (size_t i = 0; i < t.lines.size(); i++) {
    const int lw = text_width(*t.font, t.lines[i], t.scale);
    draw_text(*t.font, t.lines[i], cx - lw / 2, top + int(i) * t.line_h, t.scale, clip_w, plot);
  }
}

// ---- text scroller ----

// Scroll position: x of the text's left edge at t. The text enters at the
// right edge and scrolls left until it is gone, then starts again.
// px_per_s: canvas pixels per second.
inline int scroll_x(int64_t t_ms, double px_per_s, int text_w, int canvas_w) {
  const int64_t period = int64_t(text_w) + canvas_w;
  const int64_t moved = int64_t(t_ms * px_per_s / 1000.0);
  return canvas_w - int(moved % std::max<int64_t>(1, period));
}

// speed 1 = 30 canvas pixels per second.
inline Mode text_mode(const rgb_matrix::Font &font, const std::string &text, int scale,
                      uint32_t color, uint32_t background, double speed) {
  const auto cps = utf8_decode(text.empty() ? " " : text);
  const int tw = text_width(font, cps, scale);
  const double px_s = 30 * speed;
  const fx::RGB fg = fx::rgb(color), bg = fx::rgb(background);
  return {"text", true, [=, &font](Frame &f, int64_t t) -> int64_t {
            f.fill(bg.r, bg.g, bg.b);
            const int y = (f.h - font.height() * scale) / 2;
            draw_text(font, cps, scroll_x(t, px_s, tw, f.w), y, scale, f.w, frame_plot(f, fg));
            // Redraw when the text has moved a pixel, at most every frame.
            const double step = 1000.0 / px_s;
            return std::max(fx::next_frame(t), int64_t((std::floor(t / step) + 1) * step));
          }};
}

// ---- clock ----

struct ClockStyle {
  bool hour24 = true, date = false, seconds = false;
  uint32_t color = 0xffffff;
  double level = 1.0;  // dims the colour (idle screen)
};

inline std::string clock_time(const std::tm &tm, const ClockStyle &s) {
  char b[16];
  int hh = tm.tm_hour;
  if (!s.hour24) hh = hh % 12 == 0 ? 12 : hh % 12;
  if (s.seconds) snprintf(b, sizeof b, s.hour24 ? "%02d:%02d:%02d" : "%d:%02d:%02d", hh, tm.tm_min, tm.tm_sec);
  else snprintf(b, sizeof b, s.hour24 ? "%02d:%02d" : "%d:%02d", hh, tm.tm_min);
  return b;
}

// Time centred, with AM/PM (12h) and the date ("TUE 7 OCT") underneath in a
// smaller font. Picks the biggest font/scale that fits the canvas width.
inline void draw_clock(Frame &f, const std::tm &tm, const ClockStyle &s, const FontSet &fonts,
                       bool clear = true) {
  if (clear) f.fill(0, 0, 0);
  const fx::RGB c = fx::rgb(s.color);
  const fx::RGB col{uint8_t(c.r * s.level), uint8_t(c.g * s.level), uint8_t(c.b * s.level)};
  const int margin = std::max(2, f.w / 32);
  const TextFit big = fit_text(fonts, clock_time(tm, s), f.w - 2 * margin, f.h,
                               {{"10x20", 3}, {"10x20", 2}, {"9x18B", 2}, {"10x20", 1},
                                {"9x18B", 1}, {"7x13B", 1}, {"6x10", 1}, {"5x8", 1}, {"tom-thumb", 1}},
                               true);
  if (!big.font) return;
  static const char *days[] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
  static const char *months[] = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                 "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};
  std::string sub;
  if (!s.hour24) sub = tm.tm_hour < 12 ? "AM" : "PM";
  if (s.date) {
    char d[32];
    snprintf(d, sizeof d, "%s %d %s", days[tm.tm_wday % 7], tm.tm_mday, months[tm.tm_mon % 12]);
    sub += (sub.empty() ? "" : "  ") + std::string(d);
  }
  const int sub_scale = big.scale >= 2 ? 2 : 1;
  TextFit small;
  if (!sub.empty())
    small = fit_text(fonts, sub, f.w - 2 * margin, f.h,
                     {{"6x10", sub_scale}, {"6x10", 1}, {"5x8", 1}, {"tom-thumb", 1}});
  const int gap = small.font ? std::max(2, big.line_h / 5) : 0;
  const int total = big.h + gap + (small.font ? small.h : 0);
  const int top = (f.h - total) / 2;
  draw_fit(big, f.w / 2, top, f.w, frame_plot(f, col));
  if (small.font) draw_fit(small, f.w / 2, top + big.h + gap, f.w, frame_plot(f, col));
}

inline std::tm local_now() {
  const std::time_t now = std::time(nullptr);
  std::tm tm{};
  localtime_r(&now, &tm);
  return tm;
}

// Wall-clock ms until the next whole second.
inline int64_t ms_to_next_second() {
  timespec ts{};
  clock_gettime(CLOCK_REALTIME, &ts);
  return 1000 - ts.tv_nsec / 1000000;
}

inline Mode clock_mode(const FontSet &fonts, ClockStyle s) {
  return {"clock", true, [&fonts, s](Frame &f, int64_t t) -> int64_t {
            draw_clock(f, local_now(), s, fonts);
            return t + ms_to_next_second();
          }};
}

// Boot screen (milestone 8): what the wall shows from the engine's first frame
// until the web app restores the last mode. "LED POSTER" with a spinner of
// eight dots turning under it, so a wall that is stuck here is visibly alive.
inline Mode boot_mode(const FontSet &fonts, int w, int h) {
  return {"boot", true, [&fonts, w, h](Frame &f, int64_t t) -> int64_t {
            f.fill(0, 0, 0);
            const int margin = std::max(2, w / 32), r = std::max(4, std::min(w, h) / 10);
            const TextFit fit = fit_text(fonts, "LED POSTER", w - 2 * margin, h / 2,
                                         {{"10x20", 2}, {"9x18B", 2}, {"10x20", 1}, {"9x18B", 1},
                                          {"7x13B", 1}, {"6x10", 1}, {"5x8", 1}, {"tom-thumb", 1}});
            const int total = fit.h + r + 2 * r + 2, top = (h - total) / 2;
            draw_fit(fit, w / 2, top, w, frame_plot(f, {255, 176, 32}));
            const int cx = w / 2, cy = top + fit.h + r + r + 1, step = int(t / 100) % 8;
            const int dot = r >= 8 ? 2 : 1;
            for (int i = 0; i < 8; i++) {
              const float a = float(i) * float(M_PI) / 4, k = 0.15f + 0.85f * (((i - step) % 8 + 8) % 8) / 7.0f;
              const int x = int(std::lround(cx + r * std::sin(a))), y = int(std::lround(cy - r * std::cos(a)));
              for (int dy = 0; dy < dot; dy++)
                for (int dx = 0; dx < dot; dx++)
                  f.set(x + dx, y + dy, uint8_t(255 * k), uint8_t(176 * k), uint8_t(32 * k));
            }
            return (t / 100 + 1) * 100;
          }};
}

// Idle screen: a dark, slow plasma in deep blues (level: how bright, 0..1)
// with a dim clock on top. Meant for "on but nobody's watching".
inline Mode idle_mode(const FontSet &fonts, int w, int h, bool show_clock, double level,
                      bool hour24) {
  Mode bg = fx::plasma(w, h, 0.15, 0.6, {0x1830c0, 0x5018d0, 0x0080a0, 0x0818a0}, level);
  ClockStyle cs;
  cs.hour24 = hour24, cs.color = 0xc0c8ff, cs.level = std::min(1.0, level * 1.6);
  return {"idle", true, [&fonts, bg, cs, show_clock](Frame &f, int64_t t) -> int64_t {
            bg.render(f, t);
            if (show_clock) draw_clock(f, local_now(), cs, fonts, false);
            return t + 66;  // slow drift: 15fps is plenty
          }};
}
