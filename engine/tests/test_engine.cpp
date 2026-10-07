// Runs anywhere (no Pi needed): make test
// Optional arg: path to write the panel-id pattern as a PPM for eyeballing.
#include <cassert>
#include <cstdio>
#include <set>

#include "patterns.h"

static const std::vector<Panel> kSnake = {{0, 0}, {1, 0}, {1, 1}, {0, 1}, {0, 2}, {1, 2}};

static Point at(const Layout &L, int x, int y) { return L.lut[size_t(y) * L.width + x]; }
static bool eq(Point a, int x, int y) { return a.x == x && a.y == y; }

int main(int argc, char **argv) {
  // Spec snake: chain index 0 is top-left and sits at the right end of the strip.
  Layout L(64, kSnake);
  assert(L.width == 128 && L.height == 192);
  assert(eq(at(L, 0, 0), 320, 0));     // panel 1, top-left
  assert(eq(at(L, 64, 0), 256, 0));    // panel 2, top-right
  assert(eq(at(L, 64, 64), 192, 0));   // panel 3, middle-right
  assert(eq(at(L, 0, 64), 128, 0));    // panel 4, middle-left
  assert(eq(at(L, 0, 128), 64, 0));    // panel 5, bottom-left
  assert(eq(at(L, 127, 191), 63, 63)); // panel 6, bottom-right corner
  assert(eq(at(L, 10, 70), 128 + 10, 6));

  // Bijection: every logical pixel hits a distinct in-range physical pixel.
  std::set<std::pair<int, int>> seen;
  for (Point p : L.lut) {
    assert(p.x >= 0 && p.x < 384 && p.y >= 0 && p.y < 64);
    seen.insert({p.x, p.y});
  }
  assert(seen.size() == 128u * 192u);

  // Rotated panels: corners land where an upside-down / quarter-turned panel puts them.
  Layout R(64, {{0, 0, 180}});
  assert(eq(at(R, 0, 0), 63, 63) && eq(at(R, 63, 0), 0, 63));
  Layout Q(64, {{0, 0, 90}});
  assert(eq(at(Q, 0, 0), 0, 63) && eq(at(Q, 63, 0), 0, 0));
  Layout T(64, {{0, 0, 270}});
  assert(eq(at(T, 0, 0), 63, 0) && eq(at(T, 0, 63), 0, 0));

  // Single-panel bring-up: identity.
  Layout S(64, {{0, 0}});
  assert(S.width == 64 && S.height == 64 && eq(at(S, 5, 9), 5, 9));

  // Bad configs are rejected.
  bool threw = false;
  try { Layout(64, {{0, 0}, {0, 0}}); } catch (const std::runtime_error &) { threw = true; }
  assert(threw);
  threw = false;
  try { Layout(64, {{0, 0, 45}}); } catch (const std::runtime_error &) { threw = true; }
  assert(threw);

  // Limiter: full white on 6 panels at 6A each = 36A, scaled under a 13A ceiling.
  Frame f(128, 192);
  f.fill(255, 255, 255);
  double amps = estimate_amps(f, 64, 6.0, 100);
  assert(amps > 35.99 && amps < 36.01);
  double k = limiter_scale(amps, 13.0);
  for (auto &v : f.rgb) v = uint8_t(v * k);  // same truncation as the frame loop
  assert(estimate_amps(f, 64, 6.0, 100) <= 13.0);
  assert(limiter_scale(5.0, 13.0) == 1.0);
  assert(estimate_amps(f, 64, 6.0, 50) <= 6.5);  // hardware brightness counts too

  // Patterns render without touching out-of-range pixels, unknown names fail.
  for (const char *p : {"panel-id", "solid", "cycle", "rows", "cols"})
    assert(draw_pattern(p, f, L, 1234, 0xff8000));
  assert(!draw_pattern("nope", f, L, 0, 0));

  if (argc > 1) {
    draw_pattern("panel-id", f, L, 0, 0);
    FILE *out = fopen(argv[1], "wb");
    fprintf(out, "P6 %d %d 255\n", f.w, f.h);
    fwrite(f.rgb.data(), 1, f.rgb.size(), out);
    fclose(out);
  }
  puts("all engine tests passed");
}
