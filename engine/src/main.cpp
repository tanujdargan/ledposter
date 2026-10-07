// ledengine: owns the LED matrix and runs the frame loop.
// Plays cached media and playlists (media.h), the test patterns, generative
// effects (effects.h), clock, idle and text (text.h, catalog.h), with overlays
// (overlay.h) on top. Every frame goes through the brightness chain:
// min(user, BH1750 auto), then the limiter. The control and preview sockets
// (ipc.h, docs/protocol.md) let the web app drive it and watch the output; the
// push button (button.h) cycles modes, toggles power and asks for Wi-Fi setup.
// Built with -DLEDPOSTER_SIM (make sim) it runs without the matrix library or I2C.
#include <yaml-cpp/yaml.h>

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <mutex>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <thread>

#include "bh1750.h"
#include "button.h"
#include "catalog.h"
#include "ipc.h"
#include "media.h"
#include "overlay.h"
#include "patterns.h"

#ifndef LEDPOSTER_FONTS_DIR  // the Makefile sets it to vendor/rpi-rgb-led-matrix/fonts
#define LEDPOSTER_FONTS_DIR "/opt/ledposter/vendor/rpi-rgb-led-matrix/fonts"
#endif

#ifndef LEDPOSTER_SIM
#include <led-matrix.h>

// The only code that touches the panels.
struct Display {
  rgb_matrix::RGBMatrix *matrix;
  rgb_matrix::FrameCanvas *off;
  const Layout &L;

  Display(const YAML::Node &m, const Layout &L, const std::string &) : L(L) {
    const std::string mapping = m["hardware_mapping"].as<std::string>("adafruit-hat-pwm");
    rgb_matrix::RGBMatrix::Options o;
    o.hardware_mapping = mapping.c_str();
    o.rows = o.cols = L.size;
    o.chain_length = L.panels.size();
    o.parallel = 1;
    o.pwm_bits = m["pwm_bits"].as<int>(9);
    o.brightness = 100;
    o.show_refresh_rate = m["show_refresh_rate"].as<bool>(false);
    rgb_matrix::RuntimeOptions rt;
    rt.gpio_slowdown = m["gpio_slowdown"].as<int>(2);
    matrix = rgb_matrix::RGBMatrix::CreateFromOptions(o, rt);
    if (!matrix) exit(1);  // library already printed why
    matrix->set_luminance_correct(false);  // we own gamma (docs/ARCHITECTURE.md)
    off = matrix->CreateFrameCanvas();
  }
  ~Display() { delete matrix; }  // clears the panels

  // pct: output brightness after the limiter. The library scales each value
  // by it as the pixel is set, so it only affects this frame.
  void show(const Frame &f, int pct) {
    if (pct <= 0) {
      off->Fill(0, 0, 0);  // power off, or brightness 0 (the library's minimum is 1%)
    } else {
      off->SetBrightness(pct);
      for (int i = 0; i < L.width * L.height; i++) {
        const Point p = L.lut[i];
        if (p.x < 0) continue;
        const uint8_t *c = &f.rgb[size_t(i) * 3];
        off->SetPixel(p.x, p.y, c[0], c[1], c[2]);
      }
    }
    off = matrix->SwapOnVSync(off);
  }
};
#else
// make sim: no matrix. Keeps the output frame (after brightness) and, with
// --dump, writes it as a PPM about once a second and on exit.
struct Display {
  Frame out;
  std::string dump;
  std::chrono::steady_clock::time_point last{};

  Display(const YAML::Node &, const Layout &L, const std::string &dump)
      : out(L.width, L.height), dump(dump) {}
  ~Display() { write(); }

  void show(const Frame &f, int pct) {
    scale_frame(f, pct, out);
    const auto now = std::chrono::steady_clock::now();
    if (now - last >= std::chrono::seconds(1)) write(), last = now;
  }
  void write() {
    if (dump.empty()) return;
    const std::string tmp = dump + ".tmp";
    FILE *fp = fopen(tmp.c_str(), "wb");
    if (!fp) return perror(tmp.c_str());
    fprintf(fp, "P6 %d %d 255\n", out.w, out.h);
    fwrite(out.rgb.data(), 1, out.rgb.size(), fp);
    fclose(fp);
    rename(tmp.c_str(), dump.c_str());
  }
};
#endif

static volatile std::sig_atomic_t g_stop = 0;

static int usage() {
  fprintf(stderr,
          "usage: ledengine [--config $LEDPOSTER_DATA/config.yaml]\n"
          "                 [--pattern P [--color RRGGBB] | --play MEDIA_ID | --playlist NAME |\n"
          "                  --effect E | --mode clock|idle | --text TEXT]\n"
          "  P: panel-id (default), solid, cycle, rows, cols\n"
          "  E: spider-sense, web-shot, uav-radar, plasma, fire, starfield (default settings)\n"
          "  media is read from $LEDPOSTER_DATA/media (LEDPOSTER_DATA defaults to /data)\n"
          "  the flags set the starting mode; the control socket can change it\n"
#ifdef LEDPOSTER_SIM
          "  sim only: --dump out.ppm writes the output frame about once a second\n"
#endif
  );
  return 2;
}

static Clip load_clip(const std::string &media_dir, const std::string &id, const Layout &L) {
  if (id.empty() || id[0] == '.' || id.find('/') != std::string::npos)
    throw std::runtime_error("bad media id '" + id + "'");
  const std::string dir = media_dir + "/" + id;
  std::ifstream in(dir + "/meta.json");
  if (!in) throw std::runtime_error(dir + ": no meta.json (not transcoded yet?)");
  try {
    const auto j = nlohmann::json::parse(in);
    return open_clip(dir, j.at("w"), j.at("h"), j.at("durations_ms").get<std::vector<int>>(),
                     L.width, L.height);
  } catch (const nlohmann::json::exception &e) {
    throw std::runtime_error(dir + "/meta.json: " + e.what());
  }
}

// items: media id and duration in ms (0 = default_item_ms).
static Mode player(const std::string &name,
                   const std::vector<std::pair<std::string, int64_t>> &items, bool loop,
                   bool shuffle, const std::string &media_dir, const Layout &L) {
  if (items.empty()) throw std::runtime_error("playlist " + name + " has no items");
  std::vector<Clip> clips;
  std::vector<int64_t> ms;
  for (const auto &[id, d] : items) {
    clips.push_back(load_clip(media_dir, id, L));
    ms.push_back(d > 0 ? d : default_item_ms(clips.back()));
  }
  return media_mode(name, std::move(clips), Playlist(std::move(ms), loop, shuffle));
}

static Mode playlist_mode(const YAML::Node &cfg, const std::string &name,
                          const std::string &media_dir, const Layout &L) {
  const YAML::Node p = cfg["playlists"][name];
  if (!p) throw std::runtime_error("no playlist '" + name + "' under playlists:");
  std::vector<std::pair<std::string, int64_t>> items;
  for (const auto &it : p["items"]) {
    const double s = it["duration_s"].as<double>(0);
    if (it["duration_s"] && s <= 0) throw std::runtime_error("duration_s must be > 0");
    items.push_back({it["media"].as<std::string>(), int64_t(s * 1000)});
  }
  return player(name, items, p["loop"].as<bool>(true), p["shuffle"].as<bool>(false), media_dir,
                L);
}

struct ModeInfo {
  std::string kind, name;  // pattern/media/playlist/effect/clock/text/idle, and its name
  json settings = json::object();  // resolved settings (effect/clock/text/idle)
};

static const char *kPatterns[] = {"panel-id", "solid", "cycle", "rows", "cols"};

// set_mode arguments (also what the CLI flags and button.cycle turn into) to a
// ready Mode. Does file I/O for media, so it runs on the control or button
// thread, not the frame loop.
static std::pair<Mode, ModeInfo> make_mode(json a, const YAML::Node &cfg,
                                           const std::string &media_dir, const Layout &L,
                                           const std::vector<ModeSpec> &specs) {
  const std::string kind = arg_str(a, "mode");
  if ((kind == "pattern" || kind == "media" || kind == "playlist") && a.contains("settings")) {
    if (!a["settings"].is_object()) throw std::runtime_error("'settings' must be an object");
    for (auto &[k, v] : a["settings"].items()) a[k] = v;  // same as top-level fields
  }
  if (kind == "effect" || kind == "clock" || kind == "text" || kind == "idle") {
    Built b = build_mode(specs, a, random_seed());
    return {std::move(b.mode), {b.kind, b.name, b.settings}};
  }
  if (kind == "pattern") {
    const std::string p = arg_str(a, "pattern");
    const uint32_t color = a.contains("color") ? parse_color(a["color"]) : 0xffffff;
    Frame probe(L.width, L.height);
    if (!draw_pattern(p, probe, L, 0, color))
      throw std::runtime_error("unknown pattern '" + p + "' (panel-id, solid, cycle, rows, cols)");
    return {pattern_mode(p, L, color), {kind, p}};
  }
  if (kind == "media") {
    const std::string id = arg_str(a, "media");
    return {player(id, {{id, 0}}, true, false, media_dir, L), {kind, id}};
  }
  if (kind == "playlist") {
    const std::string name = arg_str(a, "playlist");
    return {playlist_mode(cfg, name, media_dir, L), {kind, name}};
  }
  throw std::runtime_error("mode must be pattern, media, playlist, effect, clock, text or idle");
}

// list_modes: every mode with its settings schema.
static json list_modes(const std::vector<ModeSpec> &specs, const YAML::Node &cfg,
                       const FontSet &fonts) {
  std::vector<std::string> pats(std::begin(kPatterns), std::end(kPatterns)), lists;
  if (cfg["playlists"] && cfg["playlists"].IsMap())
    for (const auto &p : cfg["playlists"]) lists.push_back(p.first.as<std::string>());
  json modes = json::array({
      {{"mode", "pattern"}, {"label", "Test pattern"}, {"help", "panel test patterns"},
       {"settings", {Setting{"pattern", "choice", 0, 0, "panel-id", pats}.to_json(),
                     Setting{"color", "color", 0, 0, "ffffff", {}, "for solid, rows and cols"}.to_json()}}},
      {{"mode", "media"}, {"label", "Media"}, {"help", "one item from the media library, looped"},
       {"settings", {Setting{"media", "string", 0, 200, "", {}, "media id"}.to_json()}}},
      {{"mode", "playlist"}, {"label", "Playlist"}, {"help", "a playlist from the config"},
       {"settings", {Setting{"playlist", "choice", 0, 0, lists.empty() ? "" : lists[0], lists}.to_json()}}},
  });
  for (const auto &s : specs) modes.push_back(spec_json(s));
  return {{"modes", modes}, {"events", kEvents}, {"fonts", fonts.names()}};
}

// YAML (button.cycle entries) to JSON: quoted scalars stay strings, plain ones
// become bools or numbers when they look like one.
static json yaml_json(const YAML::Node &n) {
  if (n.IsMap()) {
    json j = json::object();
    for (const auto &kv : n) j[kv.first.as<std::string>()] = yaml_json(kv.second);
    return j;
  }
  if (n.IsSequence()) {
    json j = json::array();
    for (const auto &v : n) j.push_back(yaml_json(v));
    return j;
  }
  if (!n.IsScalar()) return nullptr;
  const std::string v = n.Scalar();
  if (n.Tag() == "!") return v;
  if (v == "true") return true;
  if (v == "false") return false;
  char *end = nullptr;
  const long long i = strtoll(v.c_str(), &end, 10);
  if (!v.empty() && *end == 0) return i;
  const double d = strtod(v.c_str(), &end);
  if (!v.empty() && *end == 0) return d;
  return v;
}

// Everything reload_config can change without a restart.
struct Settings {
  double ceiling, amps_full;
  std::array<uint8_t, 256> lut;
  int user;  // percent
  bool auto_on;
  AutoCurve curve;
  double smoothing_s;
  int preview_fps;
};

static Settings load_settings(const YAML::Node &cfg) {
  auto check = [](bool ok, const char *what) {
    if (!ok) throw std::runtime_error(what);
  };
  Settings s;
  s.ceiling = cfg["power"]["ceiling_amps"].as<double>(13.0);
  s.amps_full = cfg["power"]["amps_full_white_per_panel"].as<double>(6.0);
  check(s.ceiling > 0 && s.amps_full > 0, "power: ceiling_amps and amps_full_white_per_panel must be > 0");
  s.lut = gamma_lut(cfg["display"]["gamma"].as<double>(2.2));
  const YAML::Node b = cfg["brightness"], c = b["auto_curve"];
  s.user = b["user"].as<int>(cfg["matrix"]["brightness"].as<int>(100));  // M1/M2 configs
  check(s.user >= 0 && s.user <= 100, "brightness.user must be 0 to 100");
  s.auto_on = b["auto"].as<bool>(true);
  s.curve = {c["lux_low"].as<double>(2), c["min_pct"].as<double>(10),
             c["lux_high"].as<double>(400), c["max_pct"].as<double>(100)};
  check(s.curve.lux_low > 0 && s.curve.lux_high > s.curve.lux_low,
        "brightness.auto_curve: need 0 < lux_low < lux_high");
  check(0 <= s.curve.min_pct && s.curve.min_pct <= s.curve.max_pct && s.curve.max_pct <= 100,
        "brightness.auto_curve: need 0 <= min_pct <= max_pct <= 100");
  s.smoothing_s = c["smoothing_s"].as<double>(3);
  s.preview_fps = cfg["engine"]["preview_fps"].as<int>(15);
  check(s.preview_fps >= 1 && s.preview_fps <= 30, "engine.preview_fps must be 1 to 30");
  return s;
}

// Frame loop state. Only the frame loop touches it (jobs run there too).
struct State {
  Settings s;
  Mode mode;
  ModeInfo info;
  int64_t mode_t0 = -1;  // -1: (re)start the mode at the next frame
  bool power = true;
  double auto_pct = NAN, fps = 0;
  int effective = 0;
  Power out;
  std::optional<Overlays> ov;
  bool wifi_requested = false;  // very long button press; cleared by ack_wifi_setup
  int wifi_count = 0;
  double wifi_at = NAN;  // unix time of the last request
};

// A config section as text, to spot changes that need a restart. The live
// settings in those sections (engine.preview_fps, button.cycle,
// button.skip_playlist_items) are left out.
static std::string yaml_at(const YAML::Node &cfg, const char *key) {
  const YAML::Node n = cfg[key];
  if (!n) return "";
  YAML::Node c = YAML::Clone(n);
  if (c.IsMap())
    for (const char *live : {"preview_fps", "cycle", "skip_playlist_items"}) c.remove(live);
  return YAML::Dump(c);
}

static PressClassifier load_button(YAML::Node b) {  // non-const: missing keys read as defaults
  PressClassifier c;
  c.debounce_ms = b["debounce_ms"].as<int>(30);
  c.long_ms = b["long_ms"].as<int>(2000);
  c.very_long_ms = b["very_long_ms"].as<int>(10000);
  if (c.debounce_ms < 1 || c.debounce_ms > 500 || c.long_ms <= c.debounce_ms ||
      c.very_long_ms <= c.long_ms)
    throw std::runtime_error(
        "button: need debounce_ms from 1 to 500, and debounce_ms < long_ms < very_long_ms");
  return c;
}

int main(int argc, char **argv) {
  const char *env = getenv("LEDPOSTER_DATA");
  const std::string data = env && *env ? env : "/data", media_dir = data + "/media";
  std::string config_path = data + "/config.yaml", pattern = "panel-id", play, playlist, dump;
  std::string effect, kind, text;
  uint32_t color = 0xffffff;
  if ((argc - 1) % 2) return usage();
  for (int i = 1; i < argc; i += 2) {
    std::string k = argv[i], v = argv[i + 1];
    if (k == "--config") config_path = v;
    else if (k == "--pattern") pattern = v;
    else if (k == "--color") color = std::stoul(v, nullptr, 16);
    else if (k == "--play") play = v;
    else if (k == "--playlist") playlist = v;
    else if (k == "--effect") effect = v;
    else if (k == "--mode") kind = v;
    else if (k == "--text") text = v;
#ifdef LEDPOSTER_SIM
    else if (k == "--dump") dump = v;
#endif
    else return usage();
  }
  if (!kind.empty() && kind != "clock" && kind != "idle") return usage();
  const int picked = !play.empty() + !playlist.empty() + !effect.empty() + !kind.empty() + !text.empty();
  if (picked > 1) return usage();
  const json start = !play.empty()       ? json{{"mode", "media"}, {"media", play}}
                     : !playlist.empty() ? json{{"mode", "playlist"}, {"playlist", playlist}}
                     : !effect.empty()   ? json{{"mode", "effect"}, {"effect", effect}}
                     : !text.empty()     ? json{{"mode", "text"}, {"text", text}}
                     : !kind.empty()     ? json{{"mode", kind}}
                                         : json{{"mode", "pattern"}, {"pattern", pattern},
                                                {"color", color & 0xffffff}};
  tzset();  // the clock uses the system's local time zone

  YAML::Node cfg, m;
  std::optional<Layout> L;
  State st;
  FontSet fonts;
  std::vector<ModeSpec> specs;
  PressClassifier press;
  try {
    cfg = YAML::LoadFile(config_path);
    m = cfg["matrix"];
    std::vector<Panel> chain;
    for (const auto &p : m["panels"])
      chain.push_back({p["col"].as<int>(), p["row"].as<int>(), p["rotate"].as<int>(0)});
    L.emplace(m["panel_size"].as<int>(64), chain);
    st.s = load_settings(cfg);
    press = load_button(cfg["button"]);
    // Fonts load now: the matrix library drops root when it starts.
    const std::string fdir = cfg["fonts"]["dir"].as<std::string>(LEDPOSTER_FONTS_DIR);
    const auto missing = fonts.load(fdir);
    if (!missing.empty())
      fprintf(stderr, "fonts: %zu of %zu missing in %s (set fonts.dir)%s\n", missing.size(),
              kFontNames.size(), fdir.c_str(), fonts.names().empty() ? "; no text or clock" : "");
    specs = mode_specs(L->width, L->height, fonts);
    st.ov.emplace(L->width, L->height);
    try {
      std::tie(st.mode, st.info) = make_mode(start, cfg, media_dir, *L, specs);
    } catch (const std::runtime_error &e) {
      if (!picked) return usage();  // unknown pattern
      throw;
    }
  } catch (const YAML::Exception &e) {
    fprintf(stderr, "config %s: %s\n", config_path.c_str(), e.what());
    return 1;
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: %s\n", e.what());
    return 1;
  }
  const Layout &layout = *L;
  const YAML::Node bh = cfg["sensors"]["bh1750"], eng = cfg["engine"];
  const std::string sock = eng["socket"].as<std::string>("/run/ledposter/engine.sock");
  const std::string psock = eng["preview_socket"].as<std::string>("/run/ledposter/frames.sock");
  // Opened before the matrix starts (it drops root); polled from start() below.
  YAML::Node bcfg = cfg["button"];  // non-const: a missing section reads as defaults
  Button button(bcfg["chip"].as<std::string>("/dev/gpiochip0"), bcfg["gpio"].as<int>(-1), press);

  signal(SIGINT, [](int) { g_stop = 1; });
  signal(SIGTERM, [](int) { g_stop = 1; });
  signal(SIGPIPE, SIG_IGN);  // a client hanging up must not kill the engine

  Display display(m, layout, dump);
  fprintf(stderr, "canvas %dx%d, %zu panels, mode %s\n", layout.width, layout.height,
          layout.panels.size(), st.info.name.c_str());
  LightSensor sensor(bh["i2c_bus"].as<int>(1), bh["address"].as<int>(0x23),
                     bh["poll_ms"].as<int>(500), st.s.smoothing_s);
  const auto t0 = std::chrono::steady_clock::now();

  auto status = [&]() -> json {
    auto r = [](double v, double k) { return std::isnan(v) ? json() : json(std::round(v * k) / k); };
    const double lux = sensor.lux;
    return {{"mode", st.info.kind},
            {"mode_name", st.info.name},
            {"fps", r(st.fps, 10)},
            {"amps", std::floor(st.out.amps * 100) / 100},  // floor: never shows above the ceiling
            {"amps_unlimited", r(st.out.amps_unlimited, 100)},
            {"limited", st.out.limited()},
            {"ceiling_amps", st.s.ceiling},
            {"brightness",
             {{"user", st.s.user},
              {"auto", r(st.auto_pct, 10)},
              {"auto_enabled", st.s.auto_on},
              {"effective", st.effective},
              {"output", st.out.pct}}},
            {"lux", r(lux, 10)},
            {"sensor", sensor.state()},
            {"power", st.power},
            {"w", layout.width},
            {"h", layout.height},
            {"mode_settings", st.info.settings},
            {"overlays", st.ov->names()},
            {"button", button.state},
            {"wifi_setup_requested", st.wifi_requested},
            {"wifi_setup_count", st.wifi_count},
            {"wifi_setup_at", std::isnan(st.wifi_at) ? json() : json(int64_t(st.wifi_at))},
            {"uptime_s", int64_t(std::chrono::duration<double>(
                                     std::chrono::steady_clock::now() - t0)
                                     .count())}};
  };
  auto ok = [] { return json::object(); };
  auto engine_ms = [&] {
    using namespace std::chrono;
    return int64_t(duration_cast<milliseconds>(steady_clock::now() - t0).count());
  };

  // Shared by the control and button threads (ctl_mu): the config as last
  // loaded. cur (cur_mu, never held during I/O) is the running mode, kept by
  // the jobs that switch it.
  std::mutex ctl_mu, cur_mu;
  YAML::Node ctl_cfg = YAML::Clone(cfg);
  const YAML::Node boot = YAML::Clone(cfg);
  ModeInfo cur = st.info;
  std::atomic<int> cycle_pos{-1};  // last button.cycle entry the button picked
  auto install = [&](Mode mode, ModeInfo info) {
    st.mode = std::move(mode), st.info = info, st.mode_t0 = -1;
    std::lock_guard<std::mutex> l(cur_mu);
    cur = std::move(info);
  };

  // Runs on the control thread (under ctl_mu): validates, does any file I/O,
  // and returns the job that applies the command on the frame loop.
  auto handle = [&](const json &req) -> Job {
    YAML::Node &cfg = ctl_cfg;
    const std::string cmd = request_cmd(req);
    if (cmd == "status") return status;
    if (cmd == "list_modes") {
      json r = list_modes(specs, cfg, fonts);
      return [r] { return r; };
    }
    if (cmd == "overlay") {
      const std::string ev = arg_str(req, "event");
      auto o = event_overlay(ev, fonts, layout.width, layout.height, random_seed());
      if (!o) throw std::runtime_error("unknown event '" + ev + "' (kill, killstreak, death, win)");
      return [&, o = *o] { return st.ov->set_event(o), json{{"ms", o.ms}}; };
    }
    if (cmd == "show_text") {
      const json &tx = arg(req, "text");
      if (!tx.is_string() || tx.get<std::string>().size() > 500)
        throw std::runtime_error("'text' must be a string of at most 500 bytes (empty clears)");
      const json sec = req.value("seconds", json(5));
      if (!sec.is_number() || !(sec.get<double>() >= 0.5 && sec.get<double>() <= 86400))
        throw std::runtime_error("'seconds' must be a number from 0.5 to 86400");
      const uint32_t color = req.contains("color") ? parse_color(req["color"]) : 0xffffff;
      if (tx.get<std::string>().empty()) return [&] { return st.ov->clear_text(), ok(); };
      Overlay o = text_overlay(fonts, tx, color, int64_t(sec.get<double>() * 1000));
      return [&, o] { return st.ov->set_text(o), json{{"ms", o.ms}}; };
    }
    if (cmd == "ack_wifi_setup") return [&] { return st.wifi_requested = false, ok(); };
    if (cmd == "sim_button") {
#ifdef LEDPOSTER_SIM
      const json &v = arg(req, "ms");
      if (!v.is_number() || !(v.get<double>() >= 1 && v.get<double>() <= 60000))
        throw std::runtime_error("'ms' must be a number from 1 to 60000");
      if (button.state != "sim") throw std::runtime_error("the button is off (set button.gpio in the config)");
      button.simulate(int(v.get<double>()));
      return ok;
#else
      throw std::runtime_error("sim_button only works in the simulator build (make sim)");
#endif
    }
    if (cmd == "set_brightness") {
      const int v = arg_pct(req, "value");
      return [&, v] { return st.s.user = v, ok(); };
    }
    if (cmd == "set_auto_brightness") {
      const bool on = arg_bool(req, "enabled");
      return [&, on] { return st.s.auto_on = on, ok(); };
    }
    if (cmd == "power") {
      const bool on = arg_bool(req, "on");
      return [&, on] { return st.power = on, ok(); };
    }
    if (cmd == "set_mode") {
      auto [mode, info] = make_mode(req, cfg, media_dir, layout, specs);
      return [&, mode = std::move(mode), info = info]() mutable {
        install(std::move(mode), info);
        return json{{"mode", info.kind}, {"mode_name", info.name}, {"settings", info.settings}};
      };
    }
    if (cmd == "reload_config") {
      YAML::Node next = YAML::LoadFile(config_path);
      const Settings s = load_settings(next);
      load_button(next["button"]);
      ModeInfo running;
      {
        std::lock_guard<std::mutex> l(cur_mu);
        running = cur;
      }
      std::optional<Mode> mode;  // the running playlist, rebuilt from the new config
      if (running.kind == "playlist") mode = playlist_mode(next, running.name, media_dir, layout);
      json restart = json::array();
      for (const char *k : {"matrix", "sensors", "engine", "button", "fonts"})
        if (yaml_at(next, k) != yaml_at(boot, k)) restart.push_back(k);
      cfg = next;
      return [&, s, mode, restart]() mutable {
        st.s = s, sensor.tau_s = s.smoothing_s;
        if (mode) st.mode = std::move(*mode), st.mode_t0 = -1;
        return json{{"restart_needed", restart}};
      };
    }
    if (cmd == "set_fake_lux") {
#ifdef LEDPOSTER_SIM
      const json &v = arg(req, "lux");
      if (!v.is_null() && !(v.is_number() && v.get<double>() >= 0))
        throw std::runtime_error("'lux' must be a number >= 0, or null for no sensor");
      sensor.set_fake(v.is_null() ? NAN : v.get<double>());
      return ok;
#else
      throw std::runtime_error("set_fake_lux only works in the simulator build (make sim)");
#endif
    }
    throw std::runtime_error("unknown cmd '" + cmd + "'");
  };
  ControlServer::Handler handler = [&](const json &req) {
    std::lock_guard<std::mutex> l(ctl_mu);
    return handle(req);
  };

  // Button presses become jobs too (no client: nobody gets the reply).
  Mailbox mailbox;
  auto on_press = [&](Press p) {
    fprintf(stderr, "button: %s press\n", press_name(p));
    Job job;
    if (p == Press::Long) {
      job = [&] { return st.power = !st.power, ok(); };
    } else if (p == Press::VeryLong) {
      // Networking (milestone 6) watches wifi_setup_requested / wifi_setup_count.
      Overlay o = text_overlay(fonts, "Wi-Fi setup", 0xffffff, 4000);
      job = [&, o] {
        st.wifi_requested = true, st.wifi_count++, st.wifi_at = double(std::time(nullptr));
        st.power = true;
        return st.ov->set_text(o), ok();
      };
    } else {
      // Next playlist item, or the next button.cycle entry. The next entry is
      // built here (file I/O) in case the playlist is on its last item.
      std::lock_guard<std::mutex> l(ctl_mu);
      YAML::Node b = ctl_cfg["button"], cyc = b["cycle"];  // non-const: missing reads as empty
      const bool skip_items = b["skip_playlist_items"].as<bool>(true);
      const int n = cyc && cyc.IsSequence() ? int(cyc.size()) : 0;
      std::optional<std::pair<Mode, ModeInfo>> next;
      int idx = -1;
      for (int k = 1; k <= n && !next; k++) {
        idx = ((cycle_pos + k) % n + n) % n;
        try {
          next = make_mode(yaml_json(cyc[idx]), ctl_cfg, media_dir, layout, specs);
        } catch (const std::exception &e) {
          fprintf(stderr, "button.cycle[%d]: %s\n", idx, e.what());
        }
      }
      job = [&, next, idx, skip_items]() mutable {
        if (skip_items && st.mode.skip && st.mode_t0 >= 0 && st.mode.skip(engine_ms() - st.mode_t0))
          return ok();
        if (next) install(std::move(next->first), next->second), cycle_pos = idx;
        return ok();
      };
    }
    mailbox.push({0, nullptr, std::move(job)});
  };

  // The wall keeps running without its sockets (e.g. no write access to
  // /run); it just can't be controlled or previewed.
  std::unique_ptr<ControlServer> control;
  std::unique_ptr<PreviewServer> preview;
  try {
    control = std::make_unique<ControlServer>(sock, mailbox, handler);
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: control socket off: %s\n", e.what());
  }
  try {
    preview = std::make_unique<PreviewServer>(psock);
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: preview socket off: %s\n", e.what());
  }
  button.start(on_press);

  Frame frame(layout.width, layout.height), out(layout.width, layout.height);
  int64_t last_log = -1000, fps_t = 0, last_preview = -1000;
  int frames = 0;
  while (!g_stop) {
    using namespace std::chrono;
    mailbox.run();
    const int64_t t = duration_cast<milliseconds>(steady_clock::now() - t0).count();
    if (st.mode_t0 < 0) st.mode_t0 = t;
    const int64_t next = st.mode_t0 + st.mode.render(frame, t - st.mode_t0);
    int64_t wake = next;
    if (st.mode.gamma)
      for (auto &v : frame.rgb) v = st.s.lut[v];
    if (st.ov->active(t)) {  // on top of the mode, before brightness and the limiter
      st.ov->apply(frame, t, st.s.lut);
      wake = std::min(wake, t + fx::kFrameMs);
    }
    st.auto_pct = auto_pct(st.s.curve, sensor.smoothed);
    st.effective = effective_pct(st.s.user, st.s.auto_on ? st.auto_pct : NAN);
    st.out = limit_power(frame, layout.size, st.s.amps_full, st.power ? st.effective : 0,
                         st.s.ceiling);
    display.show(frame, st.out.pct);
    frames++;
    if (t - fps_t >= 1000) st.fps = frames * 1000.0 / (t - fps_t), frames = 0, fps_t = t;
    if (preview && preview->wanted() && t - last_preview >= 1000 / st.s.preview_fps) {
      scale_frame(frame, st.out.pct, out);
      std::string msg = status().dump() + "\n";
      msg.append(out.rgb.begin(), out.rgb.end());
      preview->publish(std::move(msg));
      last_preview = t;
    }
    if (t - last_log >= 1000) {
      fprintf(stderr, "est %.2fA%s, brightness %d%%\n", st.out.amps,
              st.out.limited() ? " (limited)" : "", st.out.pct);
      last_log = t;
    }
    // Wake at the next frame change, at least every 100ms, or when a command arrives.
    mailbox.wait_until(t0 + milliseconds(std::clamp(wake, t + 1, t + 100)));
  }
  button.halt();  // its callback uses everything declared after it
  return 0;
}
