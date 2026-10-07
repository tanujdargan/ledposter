// Runs anywhere (no Pi needed): make test
// Optional arg: path to write the panel-id pattern as a PPM for eyeballing.
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <set>

#include "bh1750.h"
#include "ipc.h"
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

static bool near(double a, double b) { return std::fabs(a - b) < 1e-6; }

static void test_brightness() {
  // Lux -> auto brightness: clamped at both ends, log-interpolated between.
  const AutoCurve c{2, 10, 400, 100};
  assert(auto_pct(c, 0) == 10 && auto_pct(c, 2) == 10 && auto_pct(c, 1) == 10);
  assert(auto_pct(c, 400) == 100 && auto_pct(c, 1e5) == 100);
  assert(near(auto_pct(c, std::sqrt(2.0 * 400)), 55));  // geometric midpoint -> halfway
  assert(auto_pct(c, 20) < auto_pct(c, 40) && auto_pct(c, 40) < auto_pct(c, 200));
  assert(std::isnan(auto_pct(c, NAN)));
  assert(near(bh1750_lux(0x01, 0x2c), 250));  // 300 counts

  // Smoothing: no jump, converges, tau 0 or no history = straight to target.
  assert(smooth(NAN, 7, 0.5, 3) == 7 && smooth(1, 7, 0.5, 0) == 7);
  assert(near(smooth(0, 1, 3, 3), 1 - std::exp(-1.0)));  // one time constant: 63%
  double v = 0;
  for (int i = 0; i < 20; i++) {
    const double nv = smooth(v, 100, 0.5, 3);
    assert(nv > v && nv < 100 && nv - v < 16);
    v = nv;
  }
  assert(v > 95);

  // effective = min(user, auto); no auto reading -> user.
  assert(effective_pct(80, NAN) == 80 && effective_pct(80, 30.4) == 30);
  assert(effective_pct(20, 90) == 20);

  // Ceiling holds for full white on 6 panels at every user/auto combination,
  // measured on the frame as the panels show it.
  Frame white(128, 192), out(128, 192);
  white.fill(255, 255, 255);
  for (int user = 0; user <= 100; user += 5)
    for (double au : {double(NAN), 0.0, 10.0, 35.5, 36.0, 37.0, 50.0, 99.6, 100.0})
      for (double ceiling : {13.0, 5.0, 0.3}) {
        const int want = effective_pct(user, au);
        const Power p = limit_power(white, 64, 6.0, want, ceiling);
        scale_frame(white, p.pct, out);
        assert(p.pct <= want && p.amps <= ceiling);
        assert(estimate_amps(out, 64, 6.0, 100) <= ceiling);
        assert(p.limited() == (want * 0.36 > ceiling));
        if (p.limited()) assert(p.pct == int(ceiling / 36.0 * 100));  // as bright as allowed
        else assert(p.pct == want && near(p.amps, want * 0.36));
      }
  Power p = limit_power(white, 64, 6.0, 100, 13.0);
  assert(p.pct == 36 && near(p.amps_unlimited, 36) && near(p.amps, 12.96));
  scale_frame(white, 36, out);
  assert(out.rgb[0] == 91);  // 255 * 36 / 100, what SetBrightness(36) puts on the panel
  assert(limit_power(white, 64, 6.0, 0, 13.0).amps == 0);  // power off / brightness 0
}

static void test_ipc() {
  std::string buf = "{\"id\":1}\n\n{\"id\":2}\r\n{\"id";
  auto lines = take_lines(buf);
  assert(lines.size() == 2 && lines[1] == "{\"id\":2}" && buf == "{\"id");

  assert(request_cmd(json{{"id", 1}, {"cmd", "status"}}) == "status");
  assert(throws([] { request_cmd(json::array()); }));
  assert(throws([] { request_cmd(json{{"id", 1}}); }));
  assert(throws([] { request_cmd(json{{"cmd", 3}}); }));

  assert(arg_pct(json{{"value", 50}}, "value") == 50 && arg_pct(json{{"value", 49.6}}, "value") == 50);
  for (json bad : {json(101), json(-1), json("50"), json(nullptr)})
    assert(throws([&] { arg_pct(json{{"value", bad}}, "value"); }));
  assert(throws([] { arg_pct(json::object(), "value"); }));
  assert(arg_bool(json{{"on", false}}, "on") == false);
  assert(throws([] { arg_bool(json{{"on", 1}}, "on"); }));
  assert(throws([] { arg_str(json{{"media", ""}}, "media"); }));

  assert(parse_color("ff8000") == 0xff8000 && parse_color("#00FF7f") == 0x00ff7f);
  assert(parse_color(0x123456) == 0x123456);
  for (json bad : {json("fff"), json("gg0000"), json(0x1000000), json(-1), json(true)})
    assert(throws([&] { parse_color(bad); }));

  // Mailbox: jobs run in order, failures become ok:false replies with the id.
  Mailbox mb;
  int pokes = 0;
  mb.on_reply = [&] { pokes++; };
  mb.push({7, 1, [] { return json{{"x", 1}}; }});
  mb.push({7, "a", []() -> json { throw std::runtime_error("nope"); }});
  mb.run();
  assert(pokes == 1 && mb.replies.size() == 2);
  auto r0 = json::parse(mb.replies[0].second), r1 = json::parse(mb.replies[1].second);
  assert(r0["ok"] == true && r0["id"] == 1 && r0["x"] == 1);
  assert(r1["ok"] == false && r1["id"] == "a" && r1["error"] == "nope");
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
  test_brightness();
  test_ipc();

  if (argc > 1) {
    draw_pattern("panel-id", f, L, 0, 0);
    FILE *out = fopen(argv[1], "wb");
    fprintf(out, "P6 %d %d 255\n", f.w, f.h);
    fwrite(f.rgb.data(), 1, f.rgb.size(), out);
    fclose(out);
  }
  puts("all engine tests passed");
}
