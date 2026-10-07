// Generative effects (spec: Modes). Each is a Mode drawing the whole canvas
// at 30fps on any canvas size, gamma = true (the engine's LUT is applied
// after). Per-frame work is O(pixels) with table lookups: distance, angle and
// sine tables are built once when the mode is made. Randomness comes from a
// seed (hash of position/slot/time), so a seed and a time give the same frame;
// only fire keeps state, and it steps in fixed 33ms ticks so the same render
// times give the same frames too.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "wall.h"

namespace fx {

constexpr int64_t kFrameMs = 33;  // ~30fps
inline int64_t next_frame(int64_t t) { return (t / kFrameMs + 1) * kFrameMs; }

// ---- helpers ----

inline uint32_t hash32(uint32_t x) {
  x ^= x >> 16, x *= 0x7feb352du, x ^= x >> 15, x *= 0x846ca68bu, x ^= x >> 16;
  return x;
}
inline uint32_t hash3(uint32_t seed, uint32_t a, uint32_t b) {
  return hash32(seed ^ hash32(a * 0x9e3779b9u ^ hash32(b + 0x85ebca6bu)));
}
// [0, 1)
inline float hashf(uint32_t seed, uint32_t a, uint32_t b) {
  return (hash3(seed, a, b) >> 8) * (1.0f / 16777216.0f);
}

struct RGB {
  uint8_t r = 0, g = 0, b = 0;
};
inline RGB rgb(uint32_t c) { return {uint8_t(c >> 16), uint8_t(c >> 8), uint8_t(c)}; }

inline void clear(Frame &f) { std::fill(f.rgb.begin(), f.rgb.end(), 0); }

// Saturating add of c * k (k in 0..1) at (x, y); off-canvas is ignored.
inline void add(Frame &f, int x, int y, RGB c, float k) {
  if (x < 0 || y < 0 || x >= f.w || y >= f.h || k <= 0) return;
  uint8_t *p = &f.rgb[(size_t(y) * f.w + x) * 3];
  const int ki = int(std::min(k, 1.0f) * 256);
  p[0] = uint8_t(std::min(255, p[0] + ((c.r * ki) >> 8)));
  p[1] = uint8_t(std::min(255, p[1] + ((c.g * ki) >> 8)));
  p[2] = uint8_t(std::min(255, p[2] + ((c.b * ki) >> 8)));
}

// Anti-aliased line (Xiaolin Wu), added with intensity k.
inline void line(Frame &f, float x0, float y0, float x1, float y1, RGB c, float k) {
  const bool steep = std::fabs(y1 - y0) > std::fabs(x1 - x0);
  if (steep) std::swap(x0, y0), std::swap(x1, y1);
  if (x0 > x1) std::swap(x0, x1), std::swap(y0, y1);
  const float dx = x1 - x0, grad = dx < 1e-4f ? 0 : (y1 - y0) / dx;
  auto plot = [&](int a, int b, float v) { steep ? add(f, b, a, c, v * k) : add(f, a, b, c, v * k); };
  // Bounded to the canvas so a line far off-screen costs nothing.
  const float lim = float(steep ? f.h : f.w);
  const int xa = int(std::max(std::floor(x0 + 0.5f), -1.0f));
  const int xb = int(std::min(std::floor(x1 + 0.5f), lim));
  for (int x = xa; x <= xb; x++) {
    const float y = y0 + grad * (x - x0);
    const int yi = int(std::floor(y));
    const float fr = y - yi;
    plot(x, yi, 1 - fr), plot(x, yi + 1, fr);
  }
}

// 256-entry palette through the given stops, evenly spaced. cyclic: the last
// stop blends back into the first (for palettes that scroll).
inline std::array<RGB, 256> palette(const std::vector<uint32_t> &stops, bool cyclic) {
  std::array<RGB, 256> p;
  const int n = stops.size(), segs = cyclic ? n : n - 1;
  for (int i = 0; i < 256; i++) {
    if (n == 1) {
      p[i] = rgb(stops[0]);
      continue;
    }
    const float pos = i / (cyclic ? 256.0f : 255.0f) * segs;
    const int a = std::min(int(pos), segs - 1);
    const float fr = pos - a;
    const RGB c0 = rgb(stops[a]), c1 = rgb(stops[(a + 1) % n]);
    p[i] = {uint8_t(c0.r + (c1.r - c0.r) * fr), uint8_t(c0.g + (c1.g - c0.g) * fr),
            uint8_t(c0.b + (c1.b - c0.b) * fr)};
  }
  return p;
}

// Euclidean length for |dx| < w, |dy| < h, from a table: dist(dx,dy).
struct DistTable {
  int w, h;
  std::vector<float> d;
  DistTable(int w, int h) : w(w), h(h), d(size_t(w) * h) {
    for (int y = 0; y < h; y++)
      for (int x = 0; x < w; x++) d[size_t(y) * w + x] = std::sqrt(float(x * x + y * y));
  }
  float operator()(int dx, int dy) const { return d[size_t(std::abs(dy)) * w + std::abs(dx)]; }
};

// ---- plasma ----

// Four sine fields (columns, rows, diagonal, rings around a drifting centre)
// summed and run through a cyclic palette that slowly scrolls. scale: spatial
// frequency (1 = about one wave per 64 pixels).
inline Mode plasma(int w, int h, double speed, double scale, const std::vector<uint32_t> &colors,
                   double level = 1.0) {
  struct S {
    int w, h;
    double speed, k;
    std::array<RGB, 256> pal;
    DistTable dist;
    std::array<float, 1024> sine;
    std::vector<float> col, row, diag, ring;
  };
  auto s = std::make_shared<S>(S{w, h, speed, scale * 2 * M_PI / 64, palette(colors, true),
                                 DistTable(w, h), {}, std::vector<float>(w),
                                 std::vector<float>(h), std::vector<float>(w + h),
                                 std::vector<float>(1024)});
  for (int i = 0; i < 1024; i++) s->sine[i] = std::sin(i * 2 * M_PI / 1024);
  for (auto &c : s->pal)
    c = {uint8_t(c.r * level), uint8_t(c.g * level), uint8_t(c.b * level)};
  return {"plasma", true, [s](Frame &f, int64_t t) -> int64_t {
            const double tt = t * s->speed / 1000.0;
            auto sn = [&](double a) { return s->sine[int64_t(std::floor(a * 1024 / (2 * M_PI))) & 1023]; };
            for (int x = 0; x < s->w; x++) s->col[x] = sn(x * s->k + tt * 1.1);
            for (int y = 0; y < s->h; y++) s->row[y] = sn(y * s->k * 0.8 - tt * 0.9);
            for (int i = 0; i < s->w + s->h; i++) s->diag[i] = sn(i * s->k * 0.6 + tt * 0.7);
            // Rings: distance in quarter pixels.
            const size_t nring = s->ring.size();
            for (size_t i = 0; i < nring; i++) s->ring[i] = sn(i * 0.25 * s->k * 1.3 - tt * 1.7);
            const int cx = int(s->w / 2 + s->w * 0.35 * std::sin(tt * 0.31));
            const int cy = int(s->h / 2 + s->h * 0.35 * std::cos(tt * 0.23));
            const int shift = int(tt * 24);
            uint8_t *p = f.rgb.data();
            for (int y = 0; y < s->h; y++) {
              const float ry = s->row[y];
              for (int x = 0; x < s->w; x++, p += 3) {
                const int dx = x - cx, dy = y - cy;
                float d = 0;
                if (std::abs(dx) < s->w && std::abs(dy) < s->h) d = s->dist(dx, dy);
                const size_t ri = std::min(nring - 1, size_t(d * 4));
                const float v = s->col[x] + ry + s->diag[x + y] + s->ring[ri];  // -4..4
                const RGB c = s->pal[(int(v * 32) + shift) & 255];
                p[0] = c.r, p[1] = c.g, p[2] = c.b;
              }
            }
            return next_frame(t);
          }};
}

// ---- fire ----

// Classic heat diffusion: the bottom rows are fed random heat, every cell
// takes the average of the three below it and the one two below, minus a
// random cooling, so heat rises and fades. height: how far up the flames
// reach, as a fraction of the canvas. Palette: black, then colors.
inline Mode fire(int w, int h, double speed, double height, const std::vector<uint32_t> &colors,
                 uint32_t seed) {
  struct S {
    int w, h;
    double speed;
    int cool;  // max cooling per row, heat units (0..255*16)
    std::array<RGB, 256> pal;
    std::vector<uint16_t> heat;  // (h + 2) rows
    std::vector<uint16_t> src;   // slowly changing base heat per column
    uint32_t rng;
    int64_t ticks = 0;
    uint32_t next() { return rng ^= rng << 13, rng ^= rng >> 17, rng ^= rng << 5; }
  };
  std::vector<uint32_t> stops = {0x000000};
  stops.insert(stops.end(), colors.begin(), colors.end());
  // Mean cooling cool/2 per row empties 255*16 heat in height*h rows.
  const int cool = std::max(2, int(2 * 255 * 16 / std::max(4.0, height * h)));
  auto s = std::make_shared<S>(S{w, h, speed, cool, palette(stops, false),
                                 std::vector<uint16_t>(size_t(w) * (h + 2)),
                                 std::vector<uint16_t>(w), hash32(seed) | 1});
  auto step = [](S &s) {
    const int w = s.w, h = s.h;
    // Base heat drifts per column, so flames form tongues instead of a wall.
    for (int x = 0; x < w; x++) {
      int v = s.src[x] + int(s.next() % 1201) - 600;
      s.src[x] = uint16_t(std::clamp(v, 1200, 255 * 16));
    }
    for (int x = 1; x < w - 1; x++) s.src[x] = uint16_t((s.src[x - 1] + 2 * s.src[x] + s.src[x + 1]) / 4);
    for (int y = h; y < h + 2; y++)
      for (int x = 0; x < w; x++)
        s.heat[size_t(y) * w + x] = (s.next() & 7) ? s.src[x] : 255 * 16;  // sparks
    for (int y = 0; y < h; y++) {
      uint16_t *row = &s.heat[size_t(y) * w];
      const uint16_t *b1 = row + w, *b2 = row + 2 * w;
      for (int x = 0; x < w; x++) {
        const int xl = x > 0 ? x - 1 : x, xr = x < w - 1 ? x + 1 : x;
        const int sum = b1[xl] + b1[x] + b1[xr] + b2[x];
        const int v = sum / 4 - int(s.next() % (s.cool + 1));
        row[x] = uint16_t(std::max(0, v));
      }
    }
  };
  for (int i = 0; i < h + 20; i++) step(*s);  // already burning at t = 0
  return {"fire", true, [s, step](Frame &f, int64_t t) -> int64_t {
            // Fixed ticks of 33ms of effect time; at most 5 per frame of catch-up.
            const int64_t want = int64_t(t * s->speed / kFrameMs);
            if (want - s->ticks > 5) s->ticks = want - 5;
            for (; s->ticks < want; s->ticks++) step(*s);
            uint8_t *p = f.rgb.data();
            for (size_t i = 0; i < size_t(s->w) * s->h; i++, p += 3) {
              const RGB c = s->pal[s->heat[i] >> 4];
              p[0] = c.r, p[1] = c.g, p[2] = c.b;
            }
            return next_frame(t);
          }};
}

// ---- starfield ----

// Flying through stars: each star has a random direction and depth; depth
// falls with time and wraps, and each wrap picks a new direction. Near stars
// are brighter, bigger and leave a short streak. density: 1 = one star per 100
// pixels.
inline Mode starfield(int w, int h, double speed, double density,
                      const std::vector<uint32_t> &colors, uint32_t seed) {
  const int n = std::max(8, int(w * h / 100.0 * density));
  std::vector<RGB> cols;
  for (uint32_t c : colors) cols.push_back(rgb(c));
  return {"starfield", true, [=](Frame &f, int64_t t) -> int64_t {
            clear(f);
            const double tt = t * speed / 1000.0 * 0.35;  // depth units per second
            const float cx = w / 2.0f, cy = h / 2.0f, fl = std::max(w, h) * 0.5f;
            auto proj = [&](float x, float y, float z, float &sx, float &sy) {
              sx = cx + x / z * fl, sy = cy + y / z * fl;
            };
            for (int i = 0; i < n; i++) {
              const double ph = hashf(seed, i, 0) + tt;
              const uint32_t gen = uint32_t(int64_t(std::floor(ph)));
              const float z = 1.0f - float(ph - std::floor(ph)) * 0.97f;  // 1 -> 0.03
              const float x = hashf(seed, i, gen * 2 + 1) * 2 - 1;
              const float y = hashf(seed, i, gen * 2 + 2) * 2 - 1;
              const RGB c = cols[hash3(seed, i, gen) % cols.size()];
              float sx, sy, tx, ty;
              proj(x, y, z, sx, sy);
              if (sx < -2 || sy < -2 || sx > w + 1 || sy > h + 1) continue;
              const float b = std::min(1.0f, 0.35f + 0.65f * (1 - z) * (1 - z) * 1.4f);
              // Streak back to where the star was a moment ago.
              proj(x, y, std::min(1.0f, z + float(0.05 * std::min(speed, 2.0))), tx, ty);
              line(f, tx, ty, sx, sy, c, b * 0.6f);
              const int ix = int(std::floor(sx)), iy = int(std::floor(sy));
              add(f, ix, iy, c, b);
              if (z < 0.3f) add(f, ix + 1, iy, c, b), add(f, ix, iy + 1, c, b), add(f, ix + 1, iy + 1, c, b);
            }
            return next_frame(t);
          }};
}

// ---- spider-sense ripple ----

// Shockwaves from random points: each ripple is three concentric rings
// alternating through colors, expanding past the far corner and fading. A new
// ripple starts every life/count, at a point picked from the seed.
inline Mode ripple(int w, int h, double speed, int count, const std::vector<uint32_t> &colors,
                   uint32_t seed) {
  struct S {
    int w, h;
    double speed;
    int count;
    std::vector<RGB> cols;
    DistTable dist;
    std::array<float, 64> prof;  // ring profile by |distance from ring| in 1/8 px
    uint32_t seed;
  };
  auto s = std::make_shared<S>(S{w, h, speed, count, {}, DistTable(w, h), {}, seed});
  for (uint32_t c : colors) s->cols.push_back(rgb(c));
  for (int i = 0; i < 64; i++) {
    const float e = i / 8.0f / 3.0f;  // ring half-width ~3px
    s->prof[i] = e >= 1 ? 0 : (1 - e * e) * (1 - e * e);
  }
  return {"spider-sense", true, [s](Frame &f, int64_t t) -> int64_t {
            clear(f);
            const int w = s->w, h = s->h;
            const double tt = t * s->speed;  // effect ms
            const double life = 2800, every = life / s->count;
            const float diag = std::sqrt(float(w * w + h * h));
            const float vel = diag * 1.15f / float(life);  // px per effect ms
            const float gap = std::max(5.0f, std::min(w, h) * 0.075f);
            const int rings = 3;
            const int64_t last = int64_t(std::floor(tt / every));
            for (int64_t k = std::max<int64_t>(0, last - s->count - 1); k <= last; k++) {
              const double age = tt - k * every;
              if (age < 0 || age >= life) continue;
              const float fade = float(std::pow(1 - age / life, 0.7));
              const int cx = int(hashf(s->seed, uint32_t(k), 1) * w * 0.8 + w * 0.1);
              const int cy = int(hashf(s->seed, uint32_t(k), 2) * h * 0.8 + h * 0.1);
              const float r = float(age) * vel;
              // Spawn flash at the centre for the first 200ms.
              if (age < 200) {
                const float a = float(1 - age / 200);
                for (int dy = -3; dy <= 3; dy++)
                  for (int dx = -3; dx <= 3; dx++) {
                    const float d = std::sqrt(float(dx * dx + dy * dy));
                    if (d <= 3) add(f, cx + dx, cy + dy, {255, 255, 255}, a * (1 - d / 3.5f));
                  }
              }
              const float rout = r + 3.5f, rin = r - (rings - 1) * gap - 3.5f;
              const int y0 = std::max(0, int(cy - rout)), y1 = std::min(h - 1, int(cy + rout) + 1);
              for (int y = y0; y <= y1; y++) {
                const int dy = y - cy;
                if (std::abs(dy) > rout) continue;
                const int hx = int(std::sqrt(rout * rout - float(dy * dy))) + 1;
                // Skip the dark middle of the ring set.
                int ix = -1;
                if (rin > 0 && std::abs(dy) < rin) ix = int(std::sqrt(rin * rin - float(dy * dy))) - 1;
                for (int x = std::max(0, cx - hx); x <= std::min(w - 1, cx + hx); x++) {
                  const int dx = x - cx;
                  if (ix > 0 && std::abs(dx) < ix) {
                    x = cx + ix - 1;
                    continue;
                  }
                  const float u = r - s->dist(dx, dy);  // 0 at the leading ring
                  int ring = int(std::lround(u / gap));
                  if (ring < 0 || ring >= rings) continue;
                  const int e = int(std::fabs(u - ring * gap) * 8);
                  if (e >= 64) continue;
                  const float k2 = s->prof[e] * fade * (1.0f - ring * 0.2f);
                  add(f, x, y, s->cols[(uint32_t(k) + ring) % s->cols.size()], k2);
                }
              }
            }
            return next_frame(t);
          }};
}

// ---- UAV radar sweep ----

// A radar scope in the middle of the canvas: range rings and crosshair, a
// sweep line with a fading trail, and blips that light up when the sweep
// passes and fade over one turn. Blips move to new places every 3 turns.
// colors: sweep, blips.
inline Mode radar(int w, int h, double speed, int blips, const std::vector<uint32_t> &colors,
                  uint32_t seed) {
  struct S {
    int w, h, R, cx, cy;
    double speed;
    int blips;
    RGB sweep, blip;
    std::vector<uint16_t> ang;   // per pixel: angle clockwise from up, 0..65535
    std::vector<uint8_t> grid;   // per pixel: static scope markings 0..255, 0 outside
    std::vector<uint8_t> inside; // per pixel: 1 inside the scope
    std::array<uint8_t, 1024> trail;
  };
  const int R = std::min(w, h) / 2 - 1;
  auto s = std::make_shared<S>(S{w, h, R, w / 2, h / 2, speed, blips, rgb(colors[0]),
                                 rgb(colors.size() > 1 ? colors[1] : colors[0]),
                                 std::vector<uint16_t>(size_t(w) * h),
                                 std::vector<uint8_t>(size_t(w) * h),
                                 std::vector<uint8_t>(size_t(w) * h), {}});
  for (int y = 0; y < h; y++)
    for (int x = 0; x < w; x++) {
      const size_t i = size_t(y) * w + x;
      const float dx = x - s->cx + 0.5f, dy = y - s->cy + 0.5f, d = std::sqrt(dx * dx + dy * dy);
      double a = std::atan2(dx, -dy);  // 0 = up, clockwise
      if (a < 0) a += 2 * M_PI;
      s->ang[i] = uint16_t(int(a / (2 * M_PI) * 65536) & 0xffff);
      s->inside[i] = d <= R;
      float g = 0;
      auto ringk = [&](float rr, float strength) { g = std::max(g, strength * std::max(0.0f, 1 - std::fabs(d - rr))); };
      ringk(R - 0.5f, 1.0f);
      ringk(R * 2 / 3.0f, 0.35f);
      ringk(R / 3.0f, 0.35f);
      if (d <= R && (std::fabs(dx) < 0.6f || std::fabs(dy) < 0.6f)) g = std::max(g, 0.25f);
      if (d > R + 1) g = 0;
      s->grid[i] = uint8_t(g * 255);
    }
  for (int i = 0; i < 1024; i++) s->trail[i] = uint8_t(255 * std::exp(-i / 1024.0 * 7));  // ~1/7 turn
  return {"uav-radar", true, [s, seed](Frame &f, int64_t t) -> int64_t {
            const double turns = t * s->speed / 3000.0;  // one turn per 3s
            const uint16_t sw = uint16_t(int64_t(turns * 65536) & 0xffff);
            uint8_t *p = f.rgb.data();
            const RGB c = s->sweep;
            for (size_t i = 0; i < s->ang.size(); i++, p += 3) {
              int v = (s->grid[i] * 120) >> 8;  // markings at ~47% (~19% after gamma)
              if (s->inside[i]) {
                const uint16_t diff = uint16_t(sw - s->ang[i]);  // how far behind the sweep
                v = std::max(v, int(s->trail[diff >> 6]));
                v = std::max(v, 40);  // faint scope background
              }
              p[0] = uint8_t((c.r * v) >> 8), p[1] = uint8_t((c.g * v) >> 8), p[2] = uint8_t((c.b * v) >> 8);
            }
            // Blips: placed per epoch of 3 turns, visible once the sweep passed them.
            const int64_t epoch = int64_t(std::floor(turns / 3));
            const double into = turns - epoch * 3.0;  // turns into this epoch
            for (int b = 0; b < s->blips; b++) {
              const float a = hashf(seed, uint32_t(epoch), b * 2);
              const float rr = (0.18f + 0.72f * hashf(seed, uint32_t(epoch), b * 2 + 1)) * s->R;
              const double since = into - a;  // turns since the sweep first passed it
              if (since < 0) continue;
              const float k = float(1 - (since - std::floor(since)));  // fades over a turn
              const float bx = s->cx + rr * std::sin(a * 2 * M_PI), by = s->cy - rr * std::cos(a * 2 * M_PI);
              const int ix = int(bx), iy = int(by);
              add(f, ix, iy, s->blip, k);
              add(f, ix + 1, iy, s->blip, k), add(f, ix, iy + 1, s->blip, k), add(f, ix + 1, iy + 1, s->blip, k);
              add(f, ix - 1, iy, s->blip, k * 0.4f), add(f, ix + 2, iy, s->blip, k * 0.4f);
              add(f, ix, iy - 1, s->blip, k * 0.4f), add(f, ix + 1, iy + 2, s->blip, k * 0.4f);
              add(f, ix + 1, iy - 1, s->blip, k * 0.4f), add(f, ix, iy + 2, s->blip, k * 0.4f);
              add(f, ix - 1, iy + 1, s->blip, k * 0.4f), add(f, ix + 2, iy + 1, s->blip, k * 0.4f);
            }
            // The sweep line itself, brightest.
            const double sa = turns * 2 * M_PI;
            line(f, float(s->cx), float(s->cy), float(s->cx + s->R * std::sin(sa)),
                 float(s->cy - s->R * std::cos(sa)), c, 0.9f);
            return next_frame(t);
          }};
}

// ---- web-shot trace ----

// One cycle (6s at speed 1): a line shoots from an edge to an anchor point,
// strands burst out from the anchor to the edges, then the spiral of rings is
// spun strand by strand, it holds, and fades. Each cycle picks a new anchor
// and source. colors: web, glow (shot head and impact).
inline Mode webshot(int w, int h, double speed, int strands, int rings,
                    const std::vector<uint32_t> &colors, uint32_t seed) {
  const RGB web = rgb(colors[0]), glow = rgb(colors.size() > 1 ? colors[1] : colors[0]);
  return {"web-shot", true, [=](Frame &f, int64_t t) -> int64_t {
            clear(f);
            const double tt = t * speed;
            const double period = 6000;
            const uint32_t cyc = uint32_t(int64_t(std::floor(tt / period)));
            const float c = float(tt - cyc * period);  // ms into the cycle
            auto ease = [](float x) { x = std::clamp(x, 0.0f, 1.0f); return 1 - (1 - x) * (1 - x); };
            auto ph = [&](float a, float b) { return std::clamp((c - a) / (b - a), 0.0f, 1.0f); };
            const float ax = w * (0.3f + 0.4f * hashf(seed, cyc, 1));
            const float ay = h * (0.3f + 0.4f * hashf(seed, cyc, 2));
            // Source on the bottom edge or a side edge.
            float sx, sy;
            const uint32_t side = hash3(seed, cyc, 3) % 3;
            const float r3 = hashf(seed, cyc, 4);
            if (side == 0) sx = w * (0.2f + 0.6f * r3), sy = float(h - 1);
            else sx = side == 1 ? 0.0f : float(w - 1), sy = h * (0.55f + 0.4f * r3);
            const float fade = 1 - ph(5000, 6000);
            // Shot.
            const float shot = ease(ph(0, 450));
            const float hx = sx + (ax - sx) * shot, hy = sy + (ay - sy) * shot;
            line(f, sx, sy, hx, hy, web, 0.85f * fade);
            if (shot < 1)
              for (int dy = -1; dy <= 1; dy++)
                for (int dx = -1; dx <= 1; dx++)
                  add(f, int(hx) + dx, int(hy) + dy, glow, dx || dy ? 0.5f : 1.0f);
            // Impact burst.
            const float imp = ph(450, 900);
            if (imp > 0 && imp < 1) {
              const float r = 1 + imp * std::min(w, h) * 0.12f;
              for (int i = 0; i < 24; i++) {
                const float a = i * float(2 * M_PI / 24);
                add(f, int(ax + r * std::cos(a)), int(ay + r * std::sin(a)), glow, 1 - imp);
              }
            }
            if (c < 450) return next_frame(t);
            // Strands: rays from the anchor to the canvas edge.
            std::vector<float> ex(strands), ey(strands);
            for (int i = 0; i < strands; i++) {
              const float a = float((i + 0.35 * (hashf(seed, cyc, 10 + i) - 0.5)) * 2 * M_PI / strands);
              const float dx = std::cos(a), dy = std::sin(a);
              float k = 1e9f;
              if (dx > 1e-4f) k = std::min(k, (w - 1 - ax) / dx);
              if (dx < -1e-4f) k = std::min(k, -ax / dx);
              if (dy > 1e-4f) k = std::min(k, (h - 1 - ay) / dy);
              if (dy < -1e-4f) k = std::min(k, -ay / dy);
              ex[i] = dx * k, ey[i] = dy * k;  // relative to the anchor
            }
            const float grow = ease(ph(450, 1500));
            for (int i = 0; i < strands; i++)
              line(f, ax, ay, ax + ex[i] * grow, ay + ey[i] * grow, web, 0.75f * fade);
            // Rings: spun inside out, one strand gap at a time, sagging toward the anchor.
            const float spin = ph(1400, 3800) * rings;
            for (int j = 0; j < rings; j++) {
              const float prog = std::clamp(spin - j, 0.0f, 1.0f) * strands;
              if (prog <= 0) break;
              const float fr = (j + 1.0f) / (rings + 0.6f);
              for (int i = 0; i < strands && i < prog; i++) {
                const int i2 = (i + 1) % strands;
                const float part = std::min(1.0f, prog - i);
                const float x0 = ax + ex[i] * fr, y0 = ay + ey[i] * fr;
                const float x1 = ax + ex[i2] * fr, y1 = ay + ey[i2] * fr;
                const float mx = ax + (ex[i] + ex[i2]) * 0.5f * fr * 0.9f;
                const float my = ay + (ey[i] + ey[i2]) * 0.5f * fr * 0.9f;
                const float k = 0.55f * fade;
                if (part <= 0.5f) {
                  const float q = part * 2;
                  line(f, x0, y0, x0 + (mx - x0) * q, y0 + (my - y0) * q, web, k);
                } else {
                  const float q = (part - 0.5f) * 2;
                  line(f, x0, y0, mx, my, web, k);
                  line(f, mx, my, mx + (x1 - mx) * q, my + (y1 - my) * q, web, k);
                }
              }
            }
            add(f, int(ax), int(ay), glow, fade);
            return next_frame(t);
          }};
}

}  // namespace fx
