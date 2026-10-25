// Runs anywhere (no Pi needed): make test
// Optional arg: path to write the panel-id pattern as a PPM for eyeballing.
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <set>

#include "bh1750.h"
#include "ipc.h"
#include "loop.h"
#include "media.h"
#include "patterns.h"
#include "privs.h"
#include "test_m5.h"
#include "stream.h"

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
  assert(p.seek(1000) == 0 && p.current() == 1 && p.upcoming() == 2);
  assert(p.seek(3200) == 200 && p.current() == 2);
  assert(p.upcoming() == 0);  // wraps: what to prefetch while the last item plays
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

  // effective = auto when there is a reading, else user.
  assert(effective_pct(80, NAN) == 80 && effective_pct(80, 30.4) == 30);
  assert(effective_pct(5, 90) == 90);

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


// DDP packet: header (10 bytes, 14 with timecode) + data.
static std::vector<uint8_t> ddp(uint8_t flags, uint32_t offset, std::vector<uint8_t> data,
                                uint8_t type = 0x0B, uint8_t id = 1) {
  std::vector<uint8_t> p = {flags, 0, type, id, uint8_t(offset >> 24), uint8_t(offset >> 16),
                            uint8_t(offset >> 8), uint8_t(offset), uint8_t(data.size() >> 8),
                            uint8_t(data.size())};
  if (flags & 0x10) p.insert(p.end(), 4, 0);
  p.insert(p.end(), data.begin(), data.end());
  return p;
}

// A well-formed E1.31 data packet for universe u.
static std::vector<uint8_t> e131(uint16_t u, uint8_t seq, const std::vector<uint8_t> &dmx,
                                 uint8_t options = 0) {
  const size_t n = 126 + dmx.size();
  std::vector<uint8_t> p(n, 0);
  auto put16 = [&](size_t at, uint32_t v) { p[at] = v >> 8, p[at + 1] = v; };
  auto put32 = [&](size_t at, uint32_t v) { put16(at, v >> 16), put16(at + 2, v); };
  put16(0, 0x0010);
  memcpy(&p[4], "ASC-E1.17\0\0\0", 12);
  put16(16, 0x7000 | (n - 16)), put32(18, 4);
  put16(38, 0x7000 | (n - 38)), put32(40, 2);
  memcpy(&p[44], "test", 4);
  p[108] = 100, p[111] = seq, p[112] = options, put16(113, u);
  put16(115, 0x7000 | (n - 115)), p[117] = 0x02, p[118] = 0xA1, put16(121, 1);
  put16(123, dmx.size() + 1);
  memcpy(&p[126], dmx.data(), dmx.size());
  return p;
}


// Root-time settings (privs.h): what the engine accepts for its sockets and
// the account it drops to, and what listen_unix will and won't touch.
static void test_privs() {
  assert(socket_dir("/run/ledposter/engine.sock", "/run/ledposter/frames.sock") == "/run/ledposter");
  assert(socket_dir("/tmp/a/b/c.sock", "/tmp/a/b/d.sock") == "/tmp/a/b");
  assert(throws([] { socket_dir("run/ledposter/engine.sock", "/run/ledposter/frames.sock"); }));  // relative
  assert(throws([] { socket_dir("/run/ledposter/../engine.sock", "/run/ledposter/frames.sock"); }));
  assert(throws([] { socket_dir("/run/ledposter/./engine.sock", "/run/ledposter/frames.sock"); }));
  assert(throws([] { socket_dir("/run//ledposter/engine.sock", "/run/ledposter/frames.sock"); }));
  assert(throws([] { socket_dir("/run/ledposter/engine.sock/", "/run/ledposter/frames.sock"); }));
  assert(throws([] { socket_dir("/engine.sock", "/frames.sock"); }));  // directly under /
  assert(throws([] { socket_dir("/run/ledposter/engine.sock", "/run/other/frames.sock"); }));  // two dirs
  assert(throws([] { socket_dir("/run/ledposter/engine.sock", "/run/ledposter/engine.sock"); }));  // same name
  assert(throws([] { socket_dir("", "/run/ledposter/frames.sock"); }));

  check_drop_target("ledwall", 999, "ledwall", 999);
  assert(throws([] { check_drop_target("root", 0, "ledwall", 999); }));
  assert(throws([] { check_drop_target("toor", 0, "ledwall", 999); }));  // uid 0 under another name
  assert(throws([] { check_drop_target("ledwall", 999, "root", 0); }));
  assert(throws([] { check_drop_target("ledwall", 999, "wheel", 0); }));
  assert(throws([] { check_drop_target("", 999, "ledwall", 999); }));
  assert(throws([] { check_drop_target("ledwall", 999, "", 999); }));

  // The socket directory: made 0770 for the group if missing, else it must be a
  // real directory of the owner's that others can't write to.
  char tmpl[] = "/tmp/ledposter-privs-XXXXXX";
  const std::string base = mkdtemp(tmpl);
  const uid_t me = geteuid();
  const gid_t gid = getegid();
  struct stat st;
  int fd = open_socket_dir(base + "/new", me, gid);
  assert(fd >= 0 && fstat(fd, &st) == 0 && S_ISDIR(st.st_mode) && (st.st_mode & 07777) == 0770);
  assert(st.st_gid == gid && st.st_uid == me);
  close(fd);
  fd = open_socket_dir(base + "/new", me, gid);  // and again, now it exists
  assert(fd >= 0);
  close(fd);
  assert(throws([&] { open_socket_dir(base + "/new", me + 1, gid); }));  // someone else's
  assert(chmod((base + "/new").c_str(), 0777) == 0);
  assert(throws([&] { open_socket_dir(base + "/new", me, gid); }));  // world-writable
  assert(chmod((base + "/new").c_str(), 0770) == 0);
  assert(symlink((base + "/new").c_str(), (base + "/link").c_str()) == 0);
  assert(throws([&] { open_socket_dir(base + "/link", me, gid); }));  // a symlink, even to a good dir
  FILE *fp = fopen((base + "/file").c_str(), "w");
  fclose(fp);
  assert(throws([&] { open_socket_dir(base + "/file", me, gid); }));  // not a directory
  assert(throws([&] { open_socket_dir(base + "/file/sub", me, gid); }));  // can't be made

  // listen_unix: never unlinks what isn't a socket; a stale socket is replaced; mode 660.
  assert(throws([&] { listen_unix(base + "/file", false); }));
  assert(lstat((base + "/file").c_str(), &st) == 0 && S_ISREG(st.st_mode));  // still there
  assert(symlink((base + "/file").c_str(), (base + "/new/sym.sock").c_str()) == 0);
  assert(throws([&] { listen_unix(base + "/new/sym.sock", false); }));
  assert(lstat((base + "/new/sym.sock").c_str(), &st) == 0 && S_ISLNK(st.st_mode));
  const std::string sock = base + "/new/engine.sock";
  int lfd = listen_unix(sock, false);
  assert(lfd >= 0 && lstat(sock.c_str(), &st) == 0 && S_ISSOCK(st.st_mode) && (st.st_mode & 0777) == 0660);
  assert(throws([&] { listen_unix(sock, false); }));  // another engine is listening
  close(lfd);  // crashed without unlinking: the next engine takes the name over
  lfd = listen_unix(sock, false);
  assert(lfd >= 0 && lstat(sock.c_str(), &st) == 0 && S_ISSOCK(st.st_mode));
  close(lfd);
  assert(throws([&] { listen_unix(base + "/missing/dir/x.sock", false); }));  // no dir made as root
  lfd = listen_unix(base + "/made/dir/x.sock", true);  // dev: the parent is created
  assert(lfd >= 0);
  close(lfd);
}

// Double press: two short presses with a gap under double_ms (button.h).
static void test_double_press() {
  auto run = [](PressClassifier c, std::vector<std::pair<int64_t, bool>> edges, int64_t until) {
    std::vector<std::pair<int64_t, Press>> out;  // (time, press) in 5ms polls, like Button
    bool level = false;
    size_t e = 0;
    for (int64_t t = 0; t <= until; t += 5) {
      while (e < edges.size() && edges[e].first <= t) level = edges[e++].second;
      const Press p = c.feed(level, t);
      if (p != Press::None) out.push_back({t, p});
    }
    return out;
  };
  PressClassifier c;
  c.double_ms = 350;
  // A lone short press is reported double_ms after its (debounced) release.
  auto r = run(c, {{100, true}, {200, false}}, 2000);
  assert(r.size() == 1 && r[0].second == Press::Short && r[0].first == 555);
  // Two quick presses: one Double on the second release, no Short.
  r = run(c, {{100, true}, {200, false}, {400, true}, {500, false}}, 2000);
  assert(r.size() == 1 && r[0].second == Press::Double && r[0].first == 530);
  // Gap over the window: two shorts.
  r = run(c, {{100, true}, {200, false}, {600, true}, {700, false}}, 2000);
  assert(r.size() == 2 && r[0].second == Press::Short && r[1].second == Press::Short);
  // Contact bounce in the gap is not a second press.
  r = run(c, {{100, true}, {200, false}, {300, true}, {310, false}}, 2000);
  assert(r.size() == 1 && r[0].second == Press::Short);
  // Second press held long: reported as Long only; very long still fires while held.
  r = run(c, {{100, true}, {200, false}, {400, true}, {3000, false}}, 4000);
  assert(r.size() == 1 && r[0].second == Press::Long);
  r = run(c, {{100, true}, {200, false}, {400, true}, {11000, false}}, 12000);
  assert(r.size() == 1 && r[0].second == Press::VeryLong && r[0].first == 10400);
  // Three presses: a double, then a short on its own.
  r = run(c, {{100, true}, {200, false}, {400, true}, {500, false}, {700, true}, {800, false}}, 2000);
  assert(r.size() == 2 && r[0].second == Press::Double && r[1].second == Press::Short);
  // double_ms 0: shorts at once, never a double.
  c.double_ms = 0;
  r = run(c, {{100, true}, {200, false}, {400, true}, {500, false}}, 2000);
  assert(r.size() == 2 && r[0].second == Press::Short && r[0].first == 230);
  assert(std::string(press_name(Press::Double)) == "double");
}

static void test_stream() {
  // DDP parsing.
  auto pk = ddp(0x41, 300, {1, 2, 3, 4, 5, 6});
  auto d = parse_ddp(pk.data(), pk.size());
  assert(d && d->push && d->offset == 300 && d->len == 6 && d->data[0] == 1);
  pk = ddp(0x50, 3, {9, 9, 9});  // timecode: 14-byte header
  d = parse_ddp(pk.data(), pk.size());
  assert(d && !d->push && d->offset == 3 && d->data[0] == 9);
  for (uint8_t t : {0x00, 0x01}) assert(parse_ddp(ddp(0x41, 0, {1}, t).data(), 11));  // LedFx-style types
  auto bad = [](const std::vector<uint8_t> &p) { return !parse_ddp(p.data(), p.size()); };
  assert(bad(ddp(0x01, 0, {1, 2, 3})));              // version 0
  assert(bad(ddp(0x81, 0, {1, 2, 3})));              // version 2
  assert(bad(ddp(0x43, 0, {1, 2, 3})));              // query
  assert(bad(ddp(0x45, 0, {1, 2, 3})));              // reply
  assert(bad(ddp(0x41, 0, {1, 2, 3}, 0x1B)));        // RGBW
  assert(bad(ddp(0x41, 0, {1, 2, 3}, 0x0B, 246)));   // JSON control
  pk = ddp(0x41, 0, {1, 2, 3});
  assert(!parse_ddp(pk.data(), 9) && !parse_ddp(pk.data(), pk.size() - 1));  // short
  pk = ddp(0x50, 0, {});
  assert(!parse_ddp(pk.data(), 12));  // timecode header cut off

  // DDP assembly on a 4x2 canvas (24 bytes): per packet until push is seen, then on push.
  Assembler a(4, 2, 1);
  pk = ddp(0x40, 0, {10, 11, 12});
  assert(a.ddp(pk.data(), pk.size()) == Assembler::kFrame && a.frame[0] == 10);
  pk = ddp(0x40, 21, {7, 8, 9, 99, 99});  // runs off the end: clipped
  assert(a.ddp(pk.data(), pk.size()) == Assembler::kFrame && a.frame[21] == 7 && a.frame[23] == 9);
  pk = ddp(0x41, 3, {20, 21, 22});
  assert(a.ddp(pk.data(), pk.size()) == Assembler::kFrame && a.frame[3] == 20 && a.frame[0] == 10);
  pk = ddp(0x40, 6, {30, 31, 32});
  assert(a.ddp(pk.data(), pk.size()) == Assembler::kPartial && a.frame[6] == 0);  // waits for push
  pk = ddp(0x41, 1000, {1, 2, 3});  // offset past the canvas: valid, nothing written
  assert(a.ddp(pk.data(), pk.size()) == Assembler::kFrame && a.frame[6] == 30);
  pk = ddp(0x43, 0, {1});
  assert(a.ddp(pk.data(), pk.size()) == Assembler::kIgnored);

  // E1.31 parsing.
  pk = e131(7, 3, {1, 2, 3});
  auto e = parse_e131(pk.data(), pk.size());
  assert(e && e->universe == 7 && e->sequence == 3 && e->len == 3 && e->data[2] == 3);
  assert(parse_e131(e131(1, 0, std::vector<uint8_t>(512, 1)).data(), 638));
  auto ebad = [](std::vector<uint8_t> p) { return !parse_e131(p.data(), p.size()); };
  assert(!parse_e131(pk.data(), 125) && !parse_e131(pk.data(), pk.size() - 1));  // short / lengths off
  pk = e131(7, 3, {1, 2, 3}), pk[4] = 'X';
  assert(ebad(pk));                                     // not ACN
  pk = e131(7, 3, {1, 2, 3}), pk[21] = 8;
  assert(ebad(pk));                                     // extended (sync/discovery)
  pk = e131(7, 3, {1, 2, 3}), pk[43] = 1;
  assert(ebad(pk));                                     // framing vector
  pk = e131(7, 3, {1, 2, 3}), pk[39] -= 1;
  assert(ebad(pk));                                     // framing length
  pk = e131(7, 3, {1, 2, 3}), pk[118] = 0;
  assert(ebad(pk));                                     // DMP address/data type
  pk = e131(7, 3, {1, 2, 3}), pk[124] += 1;
  assert(ebad(pk));                                     // property count vs length
  pk = e131(7, 3, {1, 2, 3}), pk[125] = 0xDD;
  assert(ebad(pk));                                     // non-zero start code
  assert(ebad(e131(7, 3, {1, 2, 3}, 0x80)));            // preview data
  assert(ebad(e131(7, 3, {1, 2, 3}, 0x40)));            // stream terminated
  assert(ebad(e131(0, 3, {1, 2, 3})));                  // universe 0

  // Sequence numbers: within 20 behind (or equal) is stale; wraps at 255.
  assert(!e131_stale(-1, 5) && e131_stale(5, 5) && e131_stale(5, 0) && !e131_stale(5, 6));
  assert(!e131_stale(255, 0) && e131_stale(0, 255) && !e131_stale(0, 230));

  // Universe -> pixel mapping on the full 128x192 canvas: 145 universes of 170 pixels,
  // the last one has 24576 - 144*170 = 96 pixels.
  assert(universes_for(128 * 192) == 145 && universes_for(170) == 1 && universes_for(171) == 2);
  Assembler w(128, 192, 1);
  std::vector<uint8_t> dmx(510);
  for (int u = 1; u <= 145; u++) {
    for (size_t i = 0; i < dmx.size(); i++) dmx[i] = u;
    pk = e131(u, 0, u == 145 ? std::vector<uint8_t>(288, 145) : dmx);
    assert(w.e131(pk.data(), pk.size()) == (u == 145 ? Assembler::kFrame : Assembler::kPartial));
  }
  assert(w.frame[0] == 1 && w.frame[169 * 3 + 2] == 1 && w.frame[170 * 3] == 2);
  assert(w.frame[(144 * 170) * 3] == 145 && w.frame.back() == 145);
  pk = e131(146, 0, dmx);
  assert(w.e131(pk.data(), pk.size()) == Assembler::kIgnored);  // past the canvas
  pk = e131(2, 0, std::vector<uint8_t>(510, 50));
  assert(w.e131(pk.data(), pk.size()) == Assembler::kIgnored);  // stale: same sequence
  pk = e131(2, 1, std::vector<uint8_t>(512, 50));               // 512 channels: last 2 unused
  assert(w.e131(pk.data(), pk.size()) == Assembler::kPartial && w.frame[170 * 3] == 2);
  assert(w.work[340 * 3] == 3);
  // The sender only covers universe 2: when it repeats, the frame so far is published.
  pk = e131(2, 2, std::vector<uint8_t>(510, 60));
  assert(w.e131(pk.data(), pk.size()) == Assembler::kFrame && w.frame[170 * 3] == 50);
  assert(w.work[170 * 3] == 60);

  // start_universe offsets the mapping.
  Assembler o(4, 2, 10);
  pk = e131(9, 0, {1, 2, 3});
  assert(o.e131(pk.data(), pk.size()) == Assembler::kIgnored);
  pk = e131(10, 0, {1, 2, 3, 4, 5, 6, 7});  // 2 whole pixels + 1 stray channel
  assert(o.e131(pk.data(), pk.size()) == Assembler::kFrame && o.frame[5] == 6 && o.frame[6] == 0);
}

static void test_redraw() {
  Redraw r;
  assert(r.needed(0, false, false, false, 80));  // the first frame
  r.drawn(100, false, 80);
  // Status polls and the 100ms tick before the mode's next frame: nothing to draw.
  assert(!r.needed(10, false, false, false, 80) && !r.needed(99, false, false, false, 80));
  assert(r.needed(100, false, false, false, 80));  // the mode's next frame is due
  assert(r.needed(50, false, false, false, 79));   // brightness (auto, user or limiter input)
  assert(r.needed(50, false, false, false, 0));    // power off
  assert(r.needed(50, false, false, true, 80));    // an overlay is up
  r.drawn(100, true, 80);
  assert(r.needed(60, false, false, false, 80));   // ...and one more frame once it is gone
  r.drawn(100, false, 80);
  assert(!r.needed(60, false, false, false, 80));
  r.dirty = true;  // set_mode, reload_config, a playlist skip, stream end
  assert(r.needed(60, false, false, false, 80));
  r.drawn(100, false, 80);
  // Streaming: only new stream frames draw, whatever the paused mode's due time.
  assert(!r.needed(500, true, false, false, 80) && r.needed(500, true, true, false, 80));

  // reload_config: the running playlist keeps its place unless its definition changed.
  const std::string a = "items:\n  - media: x\n", b = "items:\n  - media: y\n";
  assert(!rebuild_playlist("playlist", a, a));  // unrelated keys changed
  assert(rebuild_playlist("playlist", a, b) && rebuild_playlist("playlist", a, ""));
  assert(!rebuild_playlist("effect", a, b));

  // Replies with invalid UTF-8 (a name from a file) still go out.
  Mailbox mb;
  mb.push({1, 7, [] { return json{{"name", std::string("bad \xff name")}}; }});
  mb.run();
  assert(mb.replies.size() == 1 && mb.replies[0].second.find("\"ok\":true") != std::string::npos);
}

static void test_push_changed() {
  Frame f(4, 2);
  Shadow sh;
  std::vector<size_t> got;
  auto set = [&](size_t i, const uint8_t *) { got.push_back(i); };
  assert(push_changed(f, 80, sh, set) == 8);  // unknown canvas: everything
  got.clear();
  assert(push_changed(f, 80, sh, set) == 0);  // same picture: nothing
  f.set(3, 1, 9, 9, 9);
  assert(push_changed(f, 80, sh, set) == 1 && got == std::vector<size_t>{7});
  assert(push_changed(f, 50, sh, set) == 8);  // brightness is baked into the canvas
  Shadow other;                              // the second canvas has its own contents
  assert(push_changed(f, 50, other, set) == 8 && push_changed(f, 50, other, set) == 0);
  sh.pct = -1;  // after a power-off Fill
  assert(push_changed(f, 50, sh, set) == 8);
}

// The clock overlay renders once per text change, inside the canvas, at its corner.
static void test_clock_overlay() {
  setenv("TZ", "UTC", 1), tzset();
  const std::time_t t = 1761390000;  // 2025-10-25 11:00:00 UTC, a Saturday
  ClockOverlay c;
  ClockOverlayCfg cfg;
  cfg.enabled = true, cfg.pos = 0;  // top-left
  assert(c.update(cfg, t, 128, 192) && c.text == "11:00|SAT 25 OCT");
  assert(!c.update(cfg, t, 128, 192) && !c.update(cfg, t + 59, 128, 192));
  assert(c.update(cfg, t + 60, 128, 192) && c.text == "11:01|SAT 25 OCT");
  cfg.hour24 = false, cfg.date_px = 0, cfg.pos = 8;  // bottom-right
  assert(c.update(cfg, t + 60, 128, 192) && c.text == "11:01|");
  assert(c.x >= 0 && c.y >= 0 && c.x + c.layer.w <= 128 && c.y + c.layer.h <= 192);
  assert(c.x + c.layer.w > 120 && c.y + c.layer.h > 184);
  Frame f(128, 192);
  c.apply(f, gamma_lut(2.2));
  int lit = 0;
  for (int y = 0; y < 192; y++)
    for (int x = 0; x < 128; x++) lit += f.rgb[(size_t(y) * 128 + x) * 3] > 100 && (assert(x > 40 && y > 150), true);
  assert(lit > 50);
  // Every time of the day, 24h and 12h, fits inside the margins at every time size (clockfont.py
  // tightens the tracking for it), and the date at every date size.
  for (int px = kClockTimeMinPx; px <= kClockTimeMaxPx; px++) {
    const AaFont &f = *kClockTimeFonts[px - kClockTimeMinPx];
    for (int m = 0; m < 1440; m++) {
      std::tm tm{};
      tm.tm_hour = m / 60, tm.tm_min = m % 60;
      for (bool h24 : {true, false}) {
        ClockStyle cs;
        cs.hour24 = h24;
        const InkBox b = ink_box(f, clock_time(tm, cs));
        assert(b.x1 - b.x0 <= 128 - 2 * ClockOverlay::kMargin);
      }
    }
  }
  for (int px = kClockDateMinPx; px <= kClockDateMaxPx; px++)
    for (int d = 0; d < 7; d++)
      for (int mon = 0; mon < 12; mon++) {
        std::tm tm{};
        tm.tm_wday = d, tm.tm_mon = mon, tm.tm_mday = 28;
        const InkBox b = ink_box(*kClockDateFonts[px - kClockDateMinPx], clock_date(tm));
        assert(b.x1 - b.x0 > 0 && b.x1 - b.x0 <= 128 - 2 * ClockOverlay::kMargin);
      }
  // Rendered: the layer stays on the canvas at every position, and each size is taller than the
  // one before (the date's height counts once it is on).
  const std::time_t day = 1761350400;  // 2025-10-25 00:00 UTC
  for (int dpx : {0, kClockDateMinPx, 12, kClockDateMaxPx}) {
    int last_h = 0;
    for (int tpx = kClockTimeMinPx; tpx <= kClockTimeMaxPx; tpx++) {
      int hh = 0;
      for (std::time_t at : {day, day + 4 * 3600 + 44 * 60, day + 12 * 3600})
        for (bool h24 : {true, false})
          for (int pos = 0; pos < 9; pos++) {
            ClockOverlay o;
            ClockOverlayCfg k;
            k.enabled = true, k.hour24 = h24, k.pos = pos, k.time_px = tpx, k.date_px = dpx;
            assert(o.update(k, at, 128, 192));
            assert(o.x >= 0 && o.y >= 0 && o.x + o.layer.w <= 128 && o.y + o.layer.h <= 192);
            hh = std::max(hh, o.layer.h);
          }
      assert(hh >= last_h);
      last_h = hh;
    }
  }
  {
    int last_h = 0;
    for (int dpx = kClockDateMinPx; dpx <= kClockDateMaxPx; dpx++) {
      ClockOverlay o;
      ClockOverlayCfg k;
      k.enabled = true, k.date_px = dpx;
      assert(o.update(k, day, 128, 192) && o.layer.h >= last_h);
      last_h = o.layer.h;
    }
  }
  // Config sizes: px, or the names of a config from before the sliders (their old fonts).
  auto sizes = [](const ClockSizeKeys &k) {
    ClockOverlayCfg c;
    resolve_clock_sizes(c, k);
    return std::pair{c.time_px, c.date_px};
  };
  auto bad = [](const ClockSizeKeys &k) {
    ClockOverlayCfg c;
    try {
      resolve_clock_sizes(c, k);
    } catch (const std::runtime_error &) {
      return true;
    }
    return false;
  };
  assert(sizes({}) == std::pair(32, 9));
  assert(sizes({28, 0, {}, {}, {}}) == std::pair(28, 0));
  assert(sizes({{}, {}, "xl", "l", true}) == std::pair(46, 16));
  assert(sizes({{}, {}, "s", "m", false}) == std::pair(24, 0));
  assert(sizes({40, 11, "s", "s", false}) == std::pair(40, 11));  // px wins
  assert(bad({kClockTimeMinPx - 1, {}, {}, {}, {}}) && bad({kClockTimeMaxPx + 1, {}, {}, {}, {}}));
  assert(bad({{}, kClockDateMinPx - 1, {}, {}, {}}) && bad({{}, kClockDateMaxPx + 1, {}, {}, {}}));
  assert(bad({{}, {}, "xxl", {}, {}}) && bad({{}, {}, {}, "xl", {}}));
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
  static_assert(sizeof(Point) == 4, "lut entries stay 16-bit");
  std::vector<Panel> wide;  // 600 * 64 physical columns don't fit an int16_t
  for (int i = 0; i < 600; i++) wide.push_back({i, 0});
  assert(throws([&] { Layout(64, wide); }));

  // Rotated panels: corners land where an upside-down / quarter-turned panel puts them.
  Layout R(64, {{0, 0, 180}});
  assert(eq(at(R, 0, 0), 63, 63) && eq(at(R, 63, 0), 0, 63));
  Layout Q(64, {{0, 0, 90}});
  assert(eq(at(Q, 0, 0), 0, 63) && eq(at(Q, 63, 0), 0, 0));
  Layout T(64, {{0, 0, 270}});
  assert(eq(at(T, 0, 0), 63, 0) && eq(at(T, 0, 63), 0, 0));

  // Colorlight card: panels[i] is card slot i (2 per row of the card's 128x192 frame). This is the
  // wall as photographed with numbers on 2026-10-24: slot 4 top-left turned 90, slot 5 top-right
  // upside down, slots 3/2 in the middle, slots 0/1 upright at the bottom.
  Layout C(64, {{0, 2, 0}, {1, 2, 0}, {1, 1, 180}, {0, 1, 90}, {0, 0, 90}, {1, 0, 180}});
  const auto card = card_lut(C, 2);
  auto cat = [&](int x, int y) { return card[size_t(y) * C.width + x]; };
  assert(eq(cat(0, 128), 0, 0) && eq(cat(127, 191), 127, 63));  // bottom row: slots 0 and 1 as is
  assert(eq(cat(0, 0), 0, 191));       // top-left: slot 4 (card rows 128-191), turned 90 clockwise
  assert(eq(cat(63, 0), 0, 128));
  assert(eq(cat(64, 0), 127, 191));    // top-right: slot 5, upside down
  assert(eq(cat(64, 64), 63, 127));    // middle-right: slot 2 (card cols 0-63, rows 64-127), upside down
  std::set<std::pair<int, int>> cseen;  // every card pixel used once
  for (Point p : card) {
    assert(p.x >= 0 && p.x < 128 && p.y >= 0 && p.y < 192);
    cseen.insert({p.x, p.y});
  }
  assert(cseen.size() == 128u * 192u);
  assert(throws([&] { card_lut(C, 0); }));

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
  test_m5();
  test_stream();
  test_double_press();
  test_privs();
  test_redraw();
  test_push_changed();
  test_clock_overlay();

  if (argc > 1) {
    draw_pattern("panel-id", f, L, 0, 0);
    FILE *out = fopen(argv[1], "wb");
    fprintf(out, "P6 %d %d 255\n", f.w, f.h);
    fwrite(f.rgb.data(), 1, f.rgb.size(), out);
    fclose(out);
  }
  puts("all engine tests passed");
}
