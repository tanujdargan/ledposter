// Runs anywhere (no Pi needed): make test
// Optional arg: path to write the panel-id pattern as a PPM for eyeballing.
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <set>

#include "media.h"
#include "patterns.h"

static const std::vector<Panel> kSnake = {{0, 0}, {1, 0}, {1, 1}, {0, 1}, {0, 2}, {1, 2}};

static Point at(const Layout &L, int x, int y) { return L.lut[size_t(y) * L.width + x]; }
static bool eq(Point a, int x, int y) { return a.x == x && a.y == y; }

template <class F> static bool throws(F f) {
  try { f(); } catch (const std::runtime_error &) { return true; }
  return false;
}

// Writes n frames of 4x2 where every byte of frame i is i+1.
static std::string write_cache(int n) {
  char tmpl[] = "/tmp/ledposter-test-XXXXXX";
  std::string dir = mkdtemp(tmpl);
  FILE *fp = fopen((dir + "/frames.rgb").c_str(), "wb");
  for (int i = 0; i < n; i++)
    for (int b = 0; b < 4 * 2 * 3; b++) fputc(i + 1, fp);
  fclose(fp);
  return dir;
}

static void test_media() {
  // Cache validation: canvas size, file size, durations.
  std::string dir = write_cache(2);
  assert(throws([&] { open_clip(dir, 4, 2, {100, 50}, 128, 192); }));  // wrong canvas
  assert(throws([&] { open_clip(dir, 4, 2, {100, 50, 10}, 4, 2); }));  // 3 frames promised, 2 stored
  assert(throws([&] { open_clip(dir, 4, 2, {100, 0}, 4, 2); }));
  assert(throws([&] { open_clip(dir + "/nope", 4, 2, {100}, 4, 2); }));
  Clip c = open_clip(dir, 4, 2, {100, 50}, 4, 2);
  assert(c.total_ms() == 150 && c.frame(1)[0] == 2 && c.frame(1)[23] == 2);
  assert(c.frame_at(0) == 0 && c.frame_at(99) == 0 && c.frame_at(100) == 1);
  assert(c.frame_at(149) == 1 && c.frame_at(150) == 0);  // loops
  assert(default_item_ms(c) == 150);
  Clip still = open_clip(write_cache(1), 4, 2, {40}, 4, 2);
  assert(default_item_ms(still) == 10000);

  // Playlist order and per-item durations.
  Playlist p({1000, 2000, 500}, true, false);
  assert(p.seek(0) == 0 && p.current() == 0);
  assert(p.seek(999) == 999 && p.current() == 0);
  assert(p.seek(1000) == 0 && p.current() == 1);
  assert(p.seek(3200) == 200 && p.current() == 2);
  assert(p.seek(3500) == 0 && p.current() == 0);    // looped
  assert(p.seek(10100) == 100 && p.current() == 2);  // 2 passes + 3000ms: skips ahead
  Playlist once({1000, 2000}, false, false);
  assert(once.seek(5000) == 1999 && once.current() == 1 && once.done);  // holds the end

  // Shuffle: every pass plays each item once, never the same item twice in a row.
  Playlist sh({10, 10, 10, 10}, true, true, 42);
  std::vector<int> seq;
  for (int t = 0; t < 400; t += 10) sh.seek(t), seq.push_back(sh.current());
  for (size_t i = 0; i < seq.size(); i += 4)
    assert(std::set<int>(seq.begin() + i, seq.begin() + i + 4).size() == 4);
  for (size_t i = 1; i < seq.size(); i++) assert(seq[i] != seq[i - 1]);
  assert(seq != std::vector<int>(seq.size(), seq[0]));

  // Media mode: copies the frame showing at t, asks to wake at the next change.
  Mode m = media_mode("x", {c, still}, Playlist({300, 1000}, true, false));
  Frame f(4, 2);
  assert(!m.gamma);
  assert(m.render(f, 0) == 100 && f.rgb[0] == 1);
  assert(m.render(f, 120) == 150 && f.rgb[0] == 2);
  assert(m.render(f, 200) == 250 && f.rgb[0] == 1);  // clip loops within the item
  assert(m.render(f, 290) == 300 && f.rgb[0] == 2);  // item end cuts the frame short
  assert(m.render(f, 300) == 340 && f.rgb[0] == 1);   // the still (one 40ms frame, looped)
  assert(m.render(f, 1300) == 1400 && f.rgb[0] == 1);  // back to item 0
}

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

  // Gamma LUT for engine-drawn output.
  auto lut = gamma_lut(2.2);
  assert(lut[0] == 0 && lut[255] == 255 && lut[128] == 56);
  assert(gamma_lut(1.0)[77] == 77);

  test_media();

  if (argc > 1) {
    draw_pattern("panel-id", f, L, 0, 0);
    FILE *out = fopen(argv[1], "wb");
    fprintf(out, "P6 %d %d 255\n", f.w, f.h);
    fwrite(f.rgb.data(), 1, f.rgb.size(), out);
    fclose(out);
  }
  puts("all engine tests passed");
}
