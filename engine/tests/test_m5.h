// Milestone 5 tests: settings schemas, effects, text, clock, overlays, button.
// Included by test_engine.cpp. make test builds with ASan/UBSan, so any
// out-of-range write while rendering fails the run.
#pragma once
#include <cassert>
#include <cstdio>
#include <set>

#include "button.h"
#include "catalog.h"
#include "overlay.h"
#include "settings.h"
#include "text.h"

template <class F> static bool throws_msg(F f, const char *part) {
  try {
    f();
  } catch (const std::runtime_error &e) {
    if (std::string(e.what()).find(part) != std::string::npos) return true;
    fprintf(stderr, "wrong error: %s (wanted %s)\n", e.what(), part);
    return false;
  }
  return false;
}

static const std::vector<std::string> kSix = {"ffffff", "ff0000", "00ff00", "0000ff", "ffff00", "00ffff"};

static json settings_of(const Built &b) { return b.settings; }

static bool lit(const Frame &f) {
  for (uint8_t v : f.rgb)
    if (v) return true;
  return false;
}

static void write_ppm(const Frame &f, const std::string &path) {
  FILE *out = fopen(path.c_str(), "wb");
  if (!out) return;
  fprintf(out, "P6 %d %d 255\n", f.w, f.h);
  fwrite(f.rgb.data(), 1, f.rgb.size(), out);
  fclose(out);
}

static void test_settings() {
  const Schema sc = {{"speed", "number", 0.1, 5, 1.0},
                     {"count", "int", 1, 8, 3},
                     {"on", "bool", 0, 0, true},
                     {"color", "color", 0, 0, "ffffff"},
                     {"colors", "colors", 1, 3, json::array({"ff0000"})},
                     {"text", "string", 0, 5, "hi"},
                     {"font", "choice", 0, 0, "a", {"a", "b"}}};
  json r = resolve_settings(sc, json::object());
  assert(r["speed"] == 1.0 && r["count"] == 3 && r["colors"] == json::array({"ff0000"}));
  r = resolve_settings(sc, {{"settings", {{"speed", 2.5}, {"colors", {"#00FF00", 0x0000ff}}}},
                            {"count", 8}, {"text", "abc"}, {"mode", "x"}});
  assert(r["speed"] == 2.5 && r["count"] == 8 && r["text"] == "abc");
  assert(r["colors"] == json::array({"00ff00", "0000ff"}));  // normalized
  assert(resolve_settings(sc, {{"colors", "abcdef"}})["colors"] == json::array({"abcdef"}));
  assert(throws_msg([&] { resolve_settings(sc, {{"settings", {{"nope", 1}}}}); }, "unknown setting 'nope'"));
  assert(throws_msg([&] { resolve_settings(sc, {{"speed", 5.1}}); }, "'speed' must be a number from 0.1 to 5"));
  assert(throws_msg([&] { resolve_settings(sc, {{"speed", "1"}}); }, "'speed'"));
  assert(throws_msg([&] { resolve_settings(sc, {{"count", 2.5}}); }, "whole number from 1 to 8"));
  assert(throws_msg([&] { resolve_settings(sc, {{"on", 1}}); }, "true or false"));
  assert(throws_msg([&] { resolve_settings(sc, {{"color", "red"}}); }, "'color' must be a color"));
  assert(throws_msg([&] { resolve_settings(sc, {{"colors", json::array()}}); }, "list of 1 to 3"));
  assert(throws_msg([&] { resolve_settings(sc, {{"colors", {"ff0000", "ff0000", "ff0000", "ff0000"}}}); },
                    "list of 1 to 3"));
  assert(throws_msg([&] { resolve_settings(sc, {{"colors", {"ff0000", "zz"}}}); }, "list of 1 to 3"));
  assert(throws_msg([&] { resolve_settings(sc, {{"text", "toolong"}}); }, "at most 5 bytes"));
  assert(throws_msg([&] { resolve_settings(sc, {{"font", "c"}}); }, "one of a, b"));
  assert(throws_msg([&] { resolve_settings(sc, {{"settings", 3}}); }, "'settings' must be an object"));
  const json j = sc[0].to_json();
  assert(j["name"] == "speed" && j["type"] == "number" && j["min"] == 0.1 && j["max"] == 5 && j["default"] == 1.0);
  assert(sc[6].to_json()["choices"] == json::array({"a", "b"}));
}

static void test_text(const FontSet &fonts) {
  assert(utf8_decode("a\xc3\xa9\xe2\x82\xac\xf0\x9f\x98\x80") == (std::vector<uint32_t>{'a', 0xe9, 0x20ac, 0x1f600}));
  assert(utf8_decode("a\xff" "b") == (std::vector<uint32_t>{'a', 0xfffd, 'b'}));
  assert(utf8_decode("a\xe2\x82") == (std::vector<uint32_t>{'a', 0xfffd, 0xfffd}));  // cut short: no over-read

  const rgb_matrix::Font &f6 = *fonts.get("6x10");
  assert(f6.height() == 10 && text_width(f6, utf8_decode("Hello"), 1) == 30);
  assert(text_width(f6, utf8_decode("Hello"), 2) == 60);
  assert(text_width(*fonts.get("10x20"), utf8_decode("12:34"), 2) == 100);

  // Scroll: enters at the right edge, leaves at the left, then wraps.
  assert(scroll_x(0, 30, 60, 128) == 128);
  assert(scroll_x(1000, 30, 60, 128) == 98);
  assert(scroll_x(6233, 30, 60, 128) == 128 - 186);  // 187px moved: one short of the wrap
  assert(scroll_x(6267, 30, 60, 128) == 128);        // 188 = 60 + 128: back to the start
  assert(scroll_x(7267, 30, 60, 128) == 98);

  Mode tm = text_mode(f6, "Hi", 2, 0xff0000, 0x000010, 1.0);
  Frame fr(64, 64);
  tm.render(fr, 0);  // text just off the right edge: only background
  for (size_t i = 0; i < fr.rgb.size(); i += 3) assert(fr.rgb[i] == 0 && fr.rgb[i + 2] == 0x10);
  const int64_t nx = tm.render(fr, 1000);  // 30px in: lit, vertically centred
  assert(nx > 1000 && nx <= 1034);
  int top = 64, bot = -1;
  for (int y = 0; y < 64; y++)
    for (int x = 0; x < 64; x++)
      if (fr.rgb[(y * 64 + x) * 3] == 0xff) top = std::min(top, y), bot = std::max(bot, y);
  assert(bot >= 0 && top >= 64 / 2 - 10 && bot < 64 / 2 + 10);
  assert(text_mode(f6, "Hi", 1, 0xffffff, 0, 0.1).render(fr, 0) >= 333);  // slow: redraw per pixel step

  // Wrapping: words stay whole when a smaller font allows it.
  const auto ip = fit_text(fonts, "192.168.100.200", 60, 60, {{"7x13B", 1}, {"6x10", 1}, {"5x8", 1}, {"tom-thumb", 1}});
  assert(ip.lines.size() == 1 && ip.w <= 60 && ip.font == fonts.get("tom-thumb"));
  const auto msg = fit_text(fonts, "Wi-Fi setup: joining home network", 124, 188, {{"7x13B", 1}, {"6x10", 1}});
  assert(msg.lines.size() >= 2 && msg.w <= 124 && msg.font == fonts.get("7x13B"));
  const auto big = fit_text(fonts, "12:34", 124, 192, {{"10x20", 3}, {"10x20", 2}}, true);
  assert(big.scale == 2 && big.lines.size() == 1);
  const auto toolong = fit_text(fonts, std::string(400, 'W'), 64, 64, {{"6x10", 1}});
  assert(toolong.h <= 64 && toolong.w <= 64);  // cut to the box

  // Clock text and layout.
  std::tm t{};
  t.tm_hour = 0, t.tm_min = 5, t.tm_sec = 9, t.tm_wday = 2, t.tm_mday = 7, t.tm_mon = 9;
  ClockStyle cs;
  assert(clock_time(t, cs) == "00:05");
  cs.seconds = true;
  assert(clock_time(t, cs) == "00:05:09");
  cs.hour24 = false, cs.seconds = false;
  assert(clock_time(t, cs) == "12:05");
  t.tm_hour = 13;
  assert(clock_time(t, cs) == "1:05");
  for (auto [w, h] : {std::pair{128, 192}, {64, 64}})
    for (bool date : {false, true})
      for (bool secs : {false, true}) {
        Frame c(w, h);
        ClockStyle s;
        s.date = date, s.seconds = secs, s.hour24 = !date;
        draw_clock(c, t, s, fonts);
        assert(lit(c));
        for (int x : {0, w - 1})  // margins stay dark
          for (int y = 0; y < h; y++) assert(c.rgb[(y * w + x) * 3] == 0);
      }
}

// Every engine-drawn mode, default and extreme settings, both canvas sizes:
// renders without out-of-range access (ASan), lights something, asks for a
// later frame, and the seed fully decides the output.
static void test_modes(const FontSet &fonts) {
  const char *dir = getenv("LEDPOSTER_RENDER_DIR");  // optional: dump frames for eyeballing
  for (auto [w, h] : {std::pair{128, 192}, {64, 64}}) {
    const auto specs = mode_specs(w, h, fonts);
    std::set<std::string> names;
    for (const auto &s : specs) names.insert(s.name);
    assert(names == (std::set<std::string>{"spider-sense", "web-shot", "uav-radar", "plasma", "fire",
                                           "starfield", "clock", "text", "idle"}));
    for (const auto &s : specs) {
      json j = spec_json(s);
      assert(j["mode"] == s.mode && j["settings"].is_array());
      if (s.mode == "effect") assert(j["effect"] == s.name && j["settings"][0]["name"] == "speed");
      std::vector<json> variants = {json::object()};
      json hi = json::object(), lo = json::object();
      for (const auto &x : s.settings) {
        if (x.type == "number" || x.type == "int") hi[x.name] = x.max, lo[x.name] = x.min;
        if (x.type == "colors") hi[x.name] = json(std::vector<std::string>(
                                         kSix.begin(), kSix.begin() + int(x.max))), lo[x.name] = {"606060"};
      }
      variants.push_back({{"settings", hi}});
      variants.push_back({{"settings", lo}});
      for (const json &v : variants) {
        json req = v;
        req["mode"] = s.mode;
        if (s.mode == "effect") req["effect"] = s.name;
        Built a = build_mode(specs, req, 42), b = build_mode(specs, req, 42), c = build_mode(specs, req, 43);
        assert(a.kind == s.mode && a.name == s.name && a.mode.gamma);
        Frame fa(w, h), fb(w, h), fc(w, h);
        bool any = false, differs = false, moves = false;
        std::vector<uint8_t> first;
        for (int64_t t = 0; t <= 6500; t += 33) {
          const int64_t na = a.mode.render(fa, t);
          b.mode.render(fb, t);
          c.mode.render(fc, t);
          assert(na > t && na <= t + 1000);
          assert(fa.rgb.size() == size_t(w) * h * 3);
          if (s.mode != "clock" && s.mode != "idle") assert(fa.rgb == fb.rgb);  // same seed, same frames
          any |= lit(fa), differs |= fa.rgb != fc.rgb;
          if (first.empty()) first = fa.rgb;
          moves |= fa.rgb != first;
          if (dir && v.empty() && t % 1023 == 0)
            write_ppm(fa, std::string(dir) + "/" + s.name + "-" + std::to_string(w) + "-" +
                              std::to_string(t) + ".ppm");
        }
        for (int64_t t : {int64_t(3600) * 1000, int64_t(86400) * 1000 * 40})  // long uptimes
          assert(a.mode.render(fa, t) > t);
        assert(any);
        // Everything moves, except a one-colour plasma (its palette is flat).
        if (s.mode == "effect" && !(s.name == "plasma" && a.settings["colors"].size() == 1)) assert(moves);
        const bool seeded = s.name != "plasma" && s.mode == "effect";
        // A radar without blips has nothing random left in it.
        if (seeded && !(s.name == "uav-radar" && settings_of(a)["blips"] == 0)) assert(differs);
      }
    }
    // Plasma only uses its palette: with these stops, green never passes 0xc0.
    Built p = build_mode(specs, {{"mode", "effect"}, {"effect", "plasma"},
                                 {"colors", {"ff0060", "4000ff", "00c0ff", "ff00c0"}}}, 1);
    Frame fp(w, h);
    for (int64_t t = 0; t < 20000; t += 33) {
      p.mode.render(fp, t);
      for (size_t i = 1; i < fp.rgb.size(); i += 3) assert(fp.rgb[i] <= 0xc0);
    }
    assert(throws_msg([&] { build_mode(specs, {{"mode", "effect"}, {"effect", "nope"}}, 1); },
                      "unknown effect 'nope' (spider-sense, web-shot"));
    assert(throws_msg([&] { build_mode(specs, {{"mode", "effect"}, {"effect", "fire"}, {"settings", {{"speed", 9}}}}, 1); },
                      "'speed' must be a number from 0.1 to 5"));
    assert(throws_msg([&] { build_mode(specs, {{"mode", "text"}, {"settings", {{"font", "comic"}}}}, 1); },
                      "'font' must be one of"));
  }
}

static void test_overlays(const FontSet &fonts) {
  const auto lut = gamma_lut(2.2);
  for (auto [w, h] : {std::pair{128, 192}, {64, 64}}) {
    for (const auto &ev : kEvents) {
      auto o = event_overlay(ev, fonts, w, h, 7);
      assert(o && o->name == ev && o->ms >= 1000 && o->ms <= 3000);
      Layer l(w, h);
      bool any = false;
      for (int64_t age = 0; age < o->ms; age += 33) {
        l.clear();
        o->draw(l, age);
        for (uint8_t a : l.a) any |= a > 0;
      }
      assert(any);
    }
    Overlays ov(w, h);
    assert(!ov.active(0) && ov.names().empty());
    ov.set_event(*event_overlay("kill", fonts, w, h, 1));
    ov.set_text(text_overlay(fonts, "10.0.0.42", 0xffffff, 10000));
    assert(ov.active(1000) && ov.names() == (std::vector<std::string>{"kill", "text"}));
    Frame f(w, h), before(w, h);
    f.fill(0, 0, 255), before = f;
    ov.apply(f, 1500, lut);
    assert(f.rgb != before.rgb);  // drawn on top
    assert(ov.active(2499) && ov.names().size() == 2);   // kill lasts 1500ms
    assert(ov.active(2500) && ov.names() == std::vector<std::string>{"text"});
    assert(ov.active(10999) && !ov.active(11000) && ov.names().empty());
    f = before;
    ov.apply(f, 11000, lut);
    assert(f.rgb == before.rgb);  // gone: the mode shows unchanged
    ov.set_text(text_overlay(fonts, "x", 0xffffff, 500));
    ov.clear_text();
    assert(!ov.active(12000));
    assert(!event_overlay("dance", fonts, w, h, 1));

    // The limiter still holds on a full-white frame with an overlay on top.
    Frame white(w, h), out(w, h);
    white.fill(255, 255, 255);
    ov.set_event(*event_overlay("win", fonts, w, h, 3));
    ov.active(0);
    ov.apply(white, 1000, lut);
    const double ceiling = w * h / 4096.0 * 2;  // a third of full white
    const Power p = limit_power(white, 64, 6.0, 100, ceiling);
    scale_frame(white, p.pct, out);
    assert(p.limited() && estimate_amps(out, 64, 6.0, 100) <= ceiling);
  }
  // Blending: alpha 0 keeps the base, full alpha replaces it (through the LUT).
  Frame f(2, 1);
  f.fill(100, 100, 100);
  Layer l(2, 1);
  l.put(1, 0, {128, 0, 255}, 1.0f);
  composite(f, l, lut);
  assert(f.rgb[0] == 100 && f.rgb[3] == lut[128] && f.rgb[4] == 0 && f.rgb[5] == 255);
  assert(envelope(0, 1000, 100, 200) == 0 && envelope(500, 1000, 100, 200) == 1);
  assert(envelope(900, 1000, 100, 200) == 0.5f);
}

static void test_button() {
  auto run = [](PressClassifier c, std::vector<std::pair<int64_t, bool>> edges, int64_t until) {
    std::vector<std::pair<int64_t, Press>> out;  // (time, press) in 5ms polls
    bool level = false;
    size_t e = 0;
    for (int64_t t = 0; t <= until; t += 5) {
      while (e < edges.size() && edges[e].first <= t) level = edges[e++].second;
      const Press p = c.feed(level, t);
      if (p != Press::None) out.push_back({t, p});
    }
    return out;
  };
  using P = Press;
  PressClassifier c;  // 30 / 2000 / 10000
  auto r = run(c, {{100, true}, {250, false}}, 1000);
  assert(r.size() == 1 && r[0].second == P::Short && r[0].first == 280);  // after the debounce
  // Contact bounce on press and release: still one short press.
  r = run(c, {{100, true}, {105, false}, {110, true}, {300, false}, {305, true}, {310, false}}, 1000);
  assert(r.size() == 1 && r[0].second == P::Short);
  // A glitch shorter than the debounce is no press.
  assert(run(c, {{100, true}, {120, false}}, 1000).empty());
  // A dropout shorter than the debounce while held doesn't split the press.
  r = run(c, {{0, true}, {1000, false}, {1015, true}, {2600, false}}, 4000);
  assert(r.size() == 1 && r[0].second == P::Long);
  // Thresholds, measured between raw edges.
  r = run(c, {{100, true}, {2095, false}}, 3000);
  assert(r.size() == 1 && r[0].second == P::Short);
  r = run(c, {{100, true}, {2100, false}}, 3000);
  assert(r.size() == 1 && r[0].second == P::Long);
  r = run(c, {{100, true}, {10095, false}}, 11000);
  assert(r.size() == 1 && r[0].second == P::Long);
  // Very long fires while still held, once; the release after it is ignored.
  r = run(c, {{100, true}, {15000, false}}, 16000);
  assert(r.size() == 1 && r[0].second == P::VeryLong && r[0].first == 10100);
  // Presses back to back.
  r = run(c, {{100, true}, {200, false}, {400, true}, {3000, false}}, 4000);
  assert(r.size() == 2 && r[0].second == P::Short && r[1].second == P::Long);
  // Configurable thresholds.
  PressClassifier q{10, 300, 800};
  r = run(q, {{0, true}, {900, false}}, 1000);
  assert(r.size() == 1 && r[0].second == P::VeryLong && r[0].first == 800);
}

// Boot screen (M8): lit on both canvas sizes, the spinner moves, redraws every 100ms.
static void test_boot(const FontSet &fonts) {
  for (auto [w, h] : {std::pair{128, 192}, {64, 64}}) {
    Mode m = boot_mode(fonts, w, h);
    Frame a(w, h), b(w, h);
    assert(m.name == "boot" && m.gamma);
    assert(m.render(a, 0) == 100 && lit(a));
    assert(m.render(b, 250) == 300 && lit(b) && a.rgb != b.rgb);
    for (int x : {0, w - 1})  // margins stay dark
      for (int y = 0; y < h; y++) assert(a.rgb[(y * w + x) * 3] == 0);
    Frame c(w, h);
    m.render(c, 800);  // the spinner has 8 steps: same picture as t = 0
    assert(c.rgb == a.rgb);
  }
}

static void test_playlist_skip() {
  Playlist p({1000, 1000, 1000}, true, false);
  assert(p.seek(500) == 500 && p.skip(500) && p.current() == 1);
  assert(p.seek(600) == 100 && p.end() == 1500);
  assert(p.skip(700) && p.current() == 2);
  assert(!p.skip(800) && p.current() == 2);  // last item: the button moves to the next mode
  assert(p.seek(1700) == 0 && p.current() == 0);  // the item still ends on time and loops
}

static void test_m5() {
  FontSet fonts;
  const auto missing = fonts.load(LEDPOSTER_FONTS_DIR);
  if (!missing.empty()) fprintf(stderr, "fonts missing in %s (git submodule update --init?)\n", LEDPOSTER_FONTS_DIR);
  assert(missing.empty());
  assert(fonts.names() == kFontNames);
  test_settings();
  test_text(fonts);
  test_modes(fonts);
  test_overlays(fonts);
  test_button();
  test_playlist_skip();
  test_boot(fonts);
}
