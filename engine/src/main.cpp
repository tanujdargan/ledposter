// ledengine: owns the LED matrix and runs the frame loop.
// Plays cached media and playlists (media.h), the test patterns, generative
// effects (effects.h), clock, idle and text (text.h, catalog.h), with overlays
// (overlay.h) on top. Every frame goes through the brightness chain:
// min(user, BH1750 auto), then the limiter. The control and preview sockets
// (ipc.h, docs/protocol.md) let the web app drive it and watch the output; the
// push button (button.h) cycles modes, toggles power and asks for Wi-Fi setup.
// DDP/E1.31 input (stream.h) takes over the wall while frames arrive.
// Built with -DLEDPOSTER_SIM (make sim) it runs without the matrix library or I2C.
#include <grp.h>
#include <pwd.h>
#include <unistd.h>
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
#include "loop.h"
#include "media.h"
#include "overlay.h"
#include "patterns.h"
#include "privs.h"
#include "stream.h"

#ifndef LEDPOSTER_FONTS_DIR  // the Makefile sets it to vendor/rpi-rgb-led-matrix/fonts
#define LEDPOSTER_FONTS_DIR "/opt/ledposter/vendor/rpi-rgb-led-matrix/fonts"
#endif

// --colorlight IFACE [--colorlight-hz N] [--colorlight-resend N]
struct CardOpts {
  std::string ifname;
  int hz = 60, resend = 0;
};

#ifndef LEDPOSTER_SIM
#include <led-matrix.h>

#include <memory>

#include "colorlight.h"

// Drops root for the Colorlight path, where no matrix library does it. user/group were checked
// by check_drop_target before anything was opened.
static void drop_root(const std::string &user, const std::string &group) {
  if (geteuid() != 0) return;
  const passwd *pw = getpwnam(user.c_str());
  const struct group *gr = getgrnam(group.c_str());
  if (!pw || !gr || initgroups(user.c_str(), gr->gr_gid) || setgid(gr->gr_gid) || setuid(pw->pw_uid) ||
      geteuid() == 0 || setuid(0) == 0) {
    fprintf(stderr, "ledengine: could not drop root to %s:%s\n", user.c_str(), group.c_str());
    exit(1);
  }
}

// The only code that touches the panels: the bonnet through the matrix library, or a
// Colorlight receiving card (--colorlight IFACE).
struct Display {
  rgb_matrix::RGBMatrix *matrix = nullptr;
  rgb_matrix::FrameCanvas *off = nullptr;
  const Layout &L;
  rgb_matrix::FrameCanvas *canvas[2] = {nullptr, nullptr};  // the two that alternate
  Shadow shadow[2];
  std::unique_ptr<Colorlight> card;

  // user, group: what the library drops root to once the GPIO is set up.
  Display(const YAML::Node &m, const Layout &L, const std::string &, const std::string &user,
          const std::string &group, const CardOpts &colorlight)
      : L(L) {
    if (!colorlight.ifname.empty()) {
      card = std::make_unique<Colorlight>(colorlight.ifname, colorlight.hz, colorlight.resend, L, m["card_columns"].as<int>(2));
      drop_root(user, group);
      card->start();
      return;
    }
    const std::string mapping = m["hardware_mapping"].as<std::string>("adafruit-hat-pwm");
    rgb_matrix::RGBMatrix::Options o;
    o.hardware_mapping = mapping.c_str();
    o.rows = o.cols = L.size;
    o.chain_length = L.panels.size();
    o.parallel = 1;
    o.pwm_bits = m["pwm_bits"].as<int>(9);
    // Refresh-rate knobs (measured on a Pi 3B, 6x 64x64 chain: bits 8 + dither 2 = ~153Hz).
    o.pwm_dither_bits = m["pwm_dither_bits"].as<int>(0);
    o.pwm_lsb_nanoseconds = m["pwm_lsb_nanoseconds"].as<int>(130);
    // Caps refresh at a steady rate below the dips our frame loop causes on a Pi 3
    // (measured 103-155Hz unlimited): an uneven refresh rate reads as flicker.
    o.limit_refresh_rate_hz = m["limit_refresh_rate_hz"].as<int>(0);
    // Sleep out the rest of each limit_refresh_rate_hz period instead of spinning: a refresh
    // thread busy at RT 99 runs into the kernel's RT throttling (the library sets
    // sched_rt_runtime_us to 990000), a ~10ms stall about once a second.
    o.disable_busy_waiting = m["disable_busy_waiting"].as<bool>(true);
    o.brightness = 100;
    o.show_refresh_rate = m["show_refresh_rate"].as<bool>(false);
    rgb_matrix::RuntimeOptions rt;
    rt.gpio_slowdown = m["gpio_slowdown"].as<int>(2);
    rt.drop_priv_user = user.c_str();
    rt.drop_priv_group = group.c_str();
    // The library only sets uid and gid; without this the engine would keep
    // root's supplementary groups instead of the user's (i2c for the BH1750).
    const bool was_root = geteuid() == 0;
    const struct group *gr = was_root ? getgrnam(group.c_str()) : nullptr;
    if (gr && initgroups(user.c_str(), gr->gr_gid)) perror("initgroups");
    matrix = rgb_matrix::RGBMatrix::CreateFromOptions(o, rt);
    if (!matrix) exit(1);  // library already printed why
    if (was_root && geteuid() == 0) {  // the library logs a failed drop and carries on; we don't
      fprintf(stderr, "ledengine: still root after starting the matrix (drop to %s:%s failed)\n",
              user.c_str(), group.c_str());
      exit(1);
    }
    matrix->set_luminance_correct(false);  // we own gamma (docs/ARCHITECTURE.md)
    off = matrix->CreateFrameCanvas();
  }
  ~Display() { delete matrix; }  // clears the panels

  // pct: output brightness after the limiter. The library scales each value
  // by it as the pixel is set, so it only affects this frame.
  void show(const Frame &f, int pct) {
    if (card) return card->show(f, pct);
    int k = off == canvas[0] ? 0 : off == canvas[1] ? 1 : !canvas[0] ? 0 : 1;
    canvas[k] = off;
    if (pct <= 0) {
      off->Fill(0, 0, 0);  // power off, or brightness 0 (the library's minimum is 1%)
      shadow[k].pct = -1;  // rewrite all once the picture comes back
    } else {
      off->SetBrightness(pct);
      push_changed(f, pct, shadow[k], [&](size_t i, const uint8_t *c) {
        const Point p = L.lut[i];
        if (p.x >= 0) off->SetPixel(p.x, p.y, c[0], c[1], c[2]);
      });
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

  Display(const YAML::Node &, const Layout &L, const std::string &dump, const std::string &,
          const std::string &, const CardOpts &)
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
          "                 [--socket PATH] [--preview-socket PATH] [--user NAME] [--group NAME]\n"
          "                 [--colorlight IFACE [--colorlight-hz N] [--colorlight-resend N]]\n"
          "                 [--pattern P [--color RRGGBB] | --play MEDIA_ID | --playlist NAME |\n"
          "                  --effect E | --mode clock|idle | --text TEXT]\n"
          "  P: panel-id, solid, cycle, rows, cols\n"
          "  E: spider-sense, web-shot, uav-radar, plasma, fire, starfield (default settings)\n"
          "  media is read from $LEDPOSTER_DATA/media (LEDPOSTER_DATA defaults to /data)\n"
          "  the mode flags set the starting mode (default: the boot screen until the web app\n"
          "  sets one); the control socket can change it\n"
          "  --colorlight IFACE: drive a Colorlight receiving card on that Ethernet interface\n"
          "  instead of the bonnet (matrix.panels[i] is the card's slot i, matrix.card_columns\n"
          "  slots per row of the card's frame); --colorlight-hz: send rate (default 60),\n"
          "  --colorlight-resend: stop resending an unchanged frame after N sends (default 0 =\n"
          "  never stop; the card holds the last frame)\n"
          "  --socket, --preview-socket, --user, --group: where the sockets go and whom the\n"
          "  engine drops root to (defaults /run/ledposter/{engine,frames}.sock, daemon:daemon).\n"
          "  As root the config's engine.socket/preview_socket/user/group are ignored (the\n"
          "  config belongs to the web app's user); they apply when not running as root.\n"
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

// Config readers take YAML::Node by value: non-const lookups of a missing
// section read as defaults instead of throwing (the web app writes configs
// without the sections it doesn't manage).
static Mode playlist_mode(YAML::Node cfg, const std::string &name,
                          const std::string &media_dir, const Layout &L) {
  YAML::Node p = cfg["playlists"][name];
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
                                           const std::vector<ModeSpec> &specs,
                                           const FontSet &fonts) {
  const std::string kind = arg_str(a, "mode");
  if (kind == "boot") return {boot_mode(fonts, L.width, L.height), {kind, kind}};
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
  throw std::runtime_error("mode must be pattern, media, playlist, effect, clock, text, idle or boot");
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

// 100W PD supply, about 15A at 5V for the panels (docs/spec.md). Never a higher ceiling.
static constexpr double kMaxCeilingAmps = 15.0;

// Everything reload_config can change without a restart.
struct Settings {
  double ceiling, amps_full;
  std::array<uint8_t, 256> lut;
  int user;  // percent
  bool auto_on;
  AutoCurve curve;
  double smoothing_s;
  int preview_fps;
  int max_fps;  // redraws per second at most (art and effects); overlays and streams are not capped.
                // Default 60: at 15 and 30 the panels flickered visibly more (by eye, 2026-10-17)
  StreamConfig stream;
};

static Settings load_settings(YAML::Node cfg) {
  auto check = [](bool ok, const char *what) {
    if (!ok) throw std::runtime_error(what);
  };
  Settings s;
  s.ceiling = cfg["power"]["ceiling_amps"].as<double>(13.0);
  s.amps_full = cfg["power"]["amps_full_white_per_panel"].as<double>(6.0);
  check(s.ceiling > 0 && s.amps_full > 0, "power: ceiling_amps and amps_full_white_per_panel must be > 0");
  if (s.ceiling > kMaxCeilingAmps) {  // the supply's panel budget (docs/spec.md); the web app refuses more too
    fprintf(stderr, "power.ceiling_amps %.1f is above the %.0fA supply budget; clamped to %.0fA\n", s.ceiling,
            kMaxCeilingAmps, kMaxCeilingAmps);
    s.ceiling = kMaxCeilingAmps;
  }
  s.lut = gamma_lut(cfg["display"]["gamma"].as<double>(2.2));
  YAML::Node b = cfg["brightness"], c = b["auto_curve"];
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
  s.preview_fps = cfg["engine"]["preview_fps"].as<int>(5);
  check(s.preview_fps >= 1 && s.preview_fps <= 30, "engine.preview_fps must be 1 to 30");
  // On a Pi 3B every frame written disturbs the panel refresh (shared memory bus): fewer
  // frames, less visible flicker, choppier motion. 30 = the art's own rate.
  s.max_fps = cfg["engine"]["max_fps"].as<int>(60);
  check(s.max_fps >= 1 && s.max_fps <= 120, "engine.max_fps must be 1 to 120");
  YAML::Node d = cfg["ddp"], e = cfg["e131"], sm = cfg["stream"];
  StreamConfig &t = s.stream;
  t.ddp = d["enabled"].as<bool>(false), t.ddp_port = d["port"].as<int>(4048);
  t.e131 = e["enabled"].as<bool>(false), t.e131_port = e["port"].as<int>(5568);
  t.start_universe = e["start_universe"].as<int>(1), t.multicast = e["multicast"].as<bool>(false);
  t.apply_gamma = sm["apply_gamma"].as<bool>(true), t.timeout_s = sm["timeout_s"].as<double>(5);
  check(t.ddp_port > 0 && t.ddp_port < 65536 && t.e131_port > 0 && t.e131_port < 65536,
        "ddp.port and e131.port must be 1 to 65535");
  check(t.start_universe >= 1 && t.start_universe <= 63999, "e131.start_universe must be 1 to 63999");
  check(t.timeout_s > 0, "stream.timeout_s must be > 0");
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
  int double_count = 0;  // double presses: the web app shows the IP
  // External input: while streaming, the mode above is paused (not rendered)
  // and resumes where it was when the stream stops. dismissed: a short press
  // ended the takeover; the stream is ignored until it stops for timeout_s.
  bool streaming = false, stream_dismissed = false;
  int64_t paused_at = 0, stream_last_ms = -1;
  uint64_t stream_seen = 0, stream_fps_base = 0;
  double stream_fps = 0;
  std::string stream_src;
  Redraw draw;
};

// A config section as text, to spot changes that need a restart. The live
// settings in those sections (engine.preview_fps, engine.max_fps, button.cycle,
// button.skip_playlist_items) are left out.
static std::string yaml_at(const YAML::Node &cfg, const char *key) {
  const YAML::Node n = cfg[key];
  if (!n) return "";
  YAML::Node c = YAML::Clone(n);
  if (c.IsMap())
    for (const char *live : {"preview_fps", "max_fps", "cycle", "skip_playlist_items"}) c.remove(live);
  return YAML::Dump(c);
}

// Binds a socket now, while still root (the matrix library drops root when it
// starts). As root (gid set) the socket is root:gid 660 in the directory
// open_socket_dir verified, so the dropped-to group can connect and the engine
// can still remove it after the drop; nothing is chowned or chmoded by path.
// -1 if it can't: the wall keeps running without it (e.g. no write access to
// /run), it just can't be controlled or previewed.
static int open_socket(const char *what, const std::string &path, gid_t gid) {
  try {
    return listen_unix(path, gid == gid_t(-1), gid);
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: %s socket off: %s\n", what, e.what());
    return -1;
  }
}

// The root-time settings (privs.h). flags: from the command line. As root the
// config is ignored for these (it is the web app's file): flags, else defaults.
// Not root: flags, else config, else defaults, so dev and sim keep working
// from the config alone.
static PrivSettings priv_settings(const PrivSettings &flags, const PrivSettings &defaults,
                                  YAML::Node eng) {
  PrivSettings s = defaults;
  struct Key {
    const char *name;
    std::string PrivSettings::*field;
  };
  for (const Key &k : {Key{"socket", &PrivSettings::socket},
                       Key{"preview_socket", &PrivSettings::preview_socket},
                       Key{"user", &PrivSettings::user}, Key{"group", &PrivSettings::group}}) {
    const std::string &flag = flags.*k.field;
    const std::string in_cfg = eng[k.name] ? eng[k.name].as<std::string>("") : "";  // "" if not a scalar
    if (!flag.empty()) s.*k.field = flag;
    else if (geteuid() != 0 && !in_cfg.empty()) s.*k.field = in_cfg;
    if (geteuid() == 0 && !in_cfg.empty() && in_cfg != s.*k.field)
      fprintf(stderr, "ledengine: engine.%s in the config is ignored when running as root (using %s; set it with --%s)\n",
              k.name, (s.*k.field).c_str(), k.name == std::string("preview_socket") ? "preview-socket" : k.name);
  }
  return s;
}

static PressClassifier load_button(YAML::Node b) {  // non-const: missing keys read as defaults
  PressClassifier c;
  c.debounce_ms = b["debounce_ms"].as<int>(30);
  c.long_ms = b["long_ms"].as<int>(2000);
  c.very_long_ms = b["very_long_ms"].as<int>(10000);
  c.double_ms = b["double_ms"].as<int>(350);
  if (c.debounce_ms < 1 || c.debounce_ms > 500 || c.long_ms <= c.debounce_ms ||
      c.very_long_ms <= c.long_ms)
    throw std::runtime_error(
        "button: need debounce_ms from 1 to 500, and debounce_ms < long_ms < very_long_ms");
  if (c.double_ms < 0 || c.double_ms > 1000)
    throw std::runtime_error("button.double_ms must be 0 (off) to 1000");
  return c;
}

int main(int argc, char **argv) {
  const char *env = getenv("LEDPOSTER_DATA");
  const std::string data = env && *env ? env : "/data", media_dir = data + "/media";
  std::string config_path = data + "/config.yaml", pattern, play, playlist, dump;
  std::string effect, kind, text;
  CardOpts colorlight;
  uint32_t color = 0xffffff;
  PrivSettings flags{"", "", "", ""};  // only what was given; privs.h has the defaults
  if ((argc - 1) % 2) return usage();
  for (int i = 1; i < argc; i += 2) {
    std::string k = argv[i], v = argv[i + 1];
    if (k == "--config") config_path = v;
    else if (k == "--socket") flags.socket = v;
    else if (k == "--preview-socket") flags.preview_socket = v;
    else if (k == "--user") flags.user = v;
    else if (k == "--group") flags.group = v;
    else if (k == "--colorlight") colorlight.ifname = v;
    else if (k == "--colorlight-hz") colorlight.hz = std::stoi(v);
    else if (k == "--colorlight-resend") colorlight.resend = std::stoi(v);
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
  const int picked = !play.empty() + !playlist.empty() + !effect.empty() + !kind.empty() +
                     !text.empty() + !pattern.empty();
  if (picked > 1) return usage();
  // No flag: the boot screen, until the web app restores the last mode (M8).
  const json start = !play.empty()       ? json{{"mode", "media"}, {"media", play}}
                     : !playlist.empty() ? json{{"mode", "playlist"}, {"playlist", playlist}}
                     : !effect.empty()   ? json{{"mode", "effect"}, {"effect", effect}}
                     : !text.empty()     ? json{{"mode", "text"}, {"text", text}}
                     : !kind.empty()     ? json{{"mode", kind}}
                     : !pattern.empty()  ? json{{"mode", "pattern"}, {"pattern", pattern},
                                                {"color", color & 0xffffff}}
                                         : json{{"mode", "boot"}};
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
      std::tie(st.mode, st.info) = make_mode(start, cfg, media_dir, *L, specs, fonts);
    } catch (const std::runtime_error &e) {
      if (!pattern.empty()) return usage();  // unknown pattern
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
  YAML::Node bh = cfg["sensors"]["bh1750"];
  // Socket paths and the account the matrix library drops root to (ledposter
  // on an installed wall, from the unit's flags). As root nothing here comes
  // from the config, and the paths and account are checked before use.
  const PrivSettings ps = priv_settings(flags, PrivSettings{}, cfg["engine"]);
  const std::string &sock = ps.socket, &psock = ps.preview_socket, &user = ps.user, &group = ps.group;
  gid_t sock_gid = gid_t(-1);  // root: the sockets' group; the dir they go in is verified
  int dir_fd = -1;
  if (geteuid() == 0) {
    try {
      const passwd *pw = getpwnam(user.c_str());
      const struct group *gr = getgrnam(group.c_str());
      if (!pw) throw std::runtime_error("--user " + user + ": no such user");
      if (!gr) throw std::runtime_error("--group " + group + ": no such group");
      check_drop_target(user, pw->pw_uid, group, gr->gr_gid);
      sock_gid = gr->gr_gid;
      dir_fd = open_socket_dir(socket_dir(sock, psock), 0, sock_gid);
    } catch (const std::exception &e) {
      fprintf(stderr, "ledengine: %s\n", e.what());
      return 1;
    }
  }
  // Sockets, the I2C bus and the button line are opened before the matrix
  // starts (it drops root). The button is polled from start() below.
  const int sock_fd = open_socket("control", sock, sock_gid);
  const int psock_fd = open_socket("preview", psock, sock_gid);
  if (dir_fd >= 0) close(dir_fd);
  YAML::Node bcfg = cfg["button"];  // non-const: a missing section reads as defaults
  Button button(bcfg["chip"].as<std::string>("/dev/gpiochip0"), bcfg["gpio"].as<int>(-1), press);

  signal(SIGINT, [](int) { g_stop = 1; });
  signal(SIGTERM, [](int) { g_stop = 1; });
  signal(SIGPIPE, SIG_IGN);  // a client hanging up must not kill the engine

  LightSensor sensor(bh["enabled"].as<bool>(false), bh["i2c_bus"].as<int>(1), bh["address"].as<int>(0x23),
                     bh["poll_ms"].as<int>(500), st.s.smoothing_s);
  std::unique_ptr<Display> dp;
  try {
    dp = std::make_unique<Display>(m, layout, dump, user, group, colorlight);
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: %s\n", e.what());
    return 1;
  }
  Display &display = *dp;
  fprintf(stderr, "canvas %dx%d, %zu panels, mode %s\n", layout.width, layout.height,
          layout.panels.size(), st.info.name.c_str());
  const auto t0 = std::chrono::steady_clock::now();
  StreamOut sout;
  StreamReceiver receiver(layout.width, layout.height, sout);

  auto status = [&]() -> json {
    auto r = [](double v, double k) { return std::isnan(v) ? json() : json(std::round(v * k) / k); };
    const double lux = sensor.lux;
    const int64_t ago = st.stream_last_ms < 0 ? -1 : steady_ms() - st.stream_last_ms;
    return {{"mode", st.streaming ? "stream" : st.info.kind},
            {"mode_name", st.streaming ? st.stream_src : st.info.name},
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
            {"button_double_count", st.double_count},
            {"stream",
             {{"active", st.streaming},
              {"source", st.stream_src.empty() ? json() : json(st.stream_src)},
              {"fps", r(st.stream_fps, 10)},
              {"last_packet_ms_ago", ago < 0 ? json() : json(ago)},
              {"ddp", receiver.ddp_on.load()},
              {"e131", receiver.e131_on.load()}}},
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
  StreamConfig scfg = st.s.stream;  // the receivers as asked for (ctl_mu)
  std::atomic<int> cycle_pos{-1};  // last button.cycle entry the button picked
  // Frame loop: back to the paused mode, where it was.
  auto end_stream = [&](int64_t t, bool dismissed) {
    st.streaming = false, st.stream_dismissed = dismissed, st.draw.dirty = true;
    if (st.mode_t0 >= 0) st.mode_t0 += t - st.paused_at;
  };
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
      const json n = req.value("count", json(1)), gap = req.value("gap_ms", json(100));
      if (!n.is_number_integer() || n.get<int>() < 1 || n.get<int>() > 5)
        throw std::runtime_error("'count' must be 1 to 5");
      if (!gap.is_number() || !(gap.get<double>() >= 1 && gap.get<double>() <= 5000))
        throw std::runtime_error("'gap_ms' must be a number from 1 to 5000");
      button.simulate(int(v.get<double>()), n.get<int>(), int(gap.get<double>()));
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
      auto [mode, info] = make_mode(req, cfg, media_dir, layout, specs, fonts);
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
      // The running playlist, rebuilt from the new config if its definition changed.
      std::optional<Mode> mode;
      auto def = [&](YAML::Node c) {  // as text, "" if missing
        const YAML::Node p = c["playlists"][running.name];
        return p ? YAML::Dump(p) : std::string();
      };
      if (rebuild_playlist(running.kind, def(cfg), def(next)))
        mode = playlist_mode(next, running.name, media_dir, layout);
      json restart = json::array();
      for (const char *k : {"matrix", "sensors", "engine", "button", "fonts"})
        if (yaml_at(next, k) != yaml_at(boot, k)) restart.push_back(k);
      cfg = next;
      std::string stream_err;  // a port that can't be opened doesn't stop the rest applying
      try {
        receiver.configure(scfg = s.stream);
      } catch (const std::exception &e) {
        stream_err = e.what();
      }
      return [&, s, mode, restart, stream_err]() mutable {
        st.s = s, sensor.tau_s = s.smoothing_s, st.draw.dirty = true;  // gamma may have changed
        if (mode) st.mode = std::move(*mode), st.mode_t0 = -1;
        if (!stream_err.empty())
          throw std::runtime_error("config applied, but stream input failed: " + stream_err);
        return json{{"restart_needed", restart}};
      };
    }
    if (cmd == "set_stream") {
      if (!req.contains("ddp") && !req.contains("e131"))
        throw std::runtime_error("set_stream needs 'ddp' and/or 'e131'");
      StreamConfig c = scfg;
      if (req.contains("ddp")) c.ddp = arg_bool(req, "ddp");
      if (req.contains("e131")) c.e131 = arg_bool(req, "e131");
      receiver.configure(scfg = c);
      return [&] { return json{{"ddp", receiver.ddp_on.load()}, {"e131", receiver.e131_on.load()}}; };
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
    } else if (p == Press::Double) {
      job = [&] { return st.double_count++, ok(); };  // the web app shows the IP
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
          next = make_mode(yaml_json(cyc[idx]), ctl_cfg, media_dir, layout, specs, fonts);
        } catch (const std::exception &e) {
          fprintf(stderr, "button.cycle[%d]: %s\n", idx, e.what());
        }
      }
      job = [&, next, idx, skip_items]() mutable {
        if (st.streaming) end_stream(engine_ms(), true);  // then advance the mode underneath
        if (skip_items && st.mode.skip && st.mode_t0 >= 0 && st.mode.skip(engine_ms() - st.mode_t0))
          return st.draw.dirty = true, ok();
        if (next) install(std::move(next->first), next->second), cycle_pos = idx;
        return ok();
      };
    }
    mailbox.push({0, nullptr, std::move(job)});
  };

  // The wall keeps running without its sockets (see open_socket).
  std::unique_ptr<ControlServer> control;
  std::unique_ptr<PreviewServer> preview;
  try {
    if (sock_fd >= 0) control = std::make_unique<ControlServer>(sock, sock_fd, mailbox, handler);
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: control socket off: %s\n", e.what());
  }
  try {
    if (psock_fd >= 0) preview = std::make_unique<PreviewServer>(psock, psock_fd);
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: preview socket off: %s\n", e.what());
  }
  button.start(on_press);

  sout.on_frame = [&] { mailbox.wake(); };
  try {
    receiver.configure(st.s.stream);
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: stream input off: %s\n", e.what());
  }
  if (receiver.ddp_on) fprintf(stderr, "ddp: listening on UDP %d\n", st.s.stream.ddp_port);
  if (receiver.e131_on)
    fprintf(stderr, "e131: listening on UDP %d, universes %d to %d\n", st.s.stream.e131_port,
            st.s.stream.start_universe,
            st.s.stream.start_universe + universes_for(layout.width * layout.height) - 1);

  Frame frame(layout.width, layout.height), out(layout.width, layout.height),
      sframe(layout.width, layout.height);
  int64_t last_log = -1000000, fps_t = 0, last_preview = -1000;
  int frames = 0, log_pct = -1, frame_errors = 0;
  int64_t last_frame_error = 0;
  bool shown = false, log_limited = false;
  uint64_t drawn = 0, previewed = 0, joined = 0;  // frames drawn, the last one previewed, clients seen
  while (!g_stop) {
    using namespace std::chrono;
    mailbox.run();
    const int64_t t = duration_cast<milliseconds>(steady_clock::now() - t0).count();
    int64_t wake = t + 100;
    try {  // a mode or overlay that throws must not take the wall down
      bool fresh;
      {
        std::lock_guard<std::mutex> l(sout.mu);
        st.stream_last_ms = sout.last_packet_ms;
        fresh = sout.frames != st.stream_seen && sout.rgb.size() == sframe.rgb.size();
        if (fresh) sframe.rgb = sout.rgb, st.stream_seen = sout.frames, st.stream_src = sout.source;
        if (fresh && receiver.any() && !st.stream_dismissed && !st.streaming)
          st.streaming = true, st.paused_at = t;
      }
      const bool quiet = steady_ms() - st.stream_last_ms > st.s.stream.timeout_s * 1000;
      if (st.streaming && (!receiver.any() || quiet)) end_stream(t, false);
      if (st.stream_dismissed && (!receiver.any() || quiet)) st.stream_dismissed = false;
      if (!st.streaming && st.mode_t0 < 0) st.draw.dirty = true;  // new or rebuilt mode
      const bool ov = st.ov->active(t);
      st.auto_pct = auto_pct(st.s.curve, sensor.smoothed);
      st.effective = effective_pct(st.s.user, st.s.auto_on ? st.auto_pct : NAN);
      const int want = st.power ? st.effective : 0;
      if (st.draw.needed(t, st.streaming, fresh, ov, want)) {
        int64_t due = st.draw.due;
        if (st.streaming) {
          frame.rgb = sframe.rgb;
          if (st.s.stream.apply_gamma)
            for (auto &v : frame.rgb) v = st.s.lut[v];
        } else {
          if (st.mode_t0 < 0) st.mode_t0 = t;
          // The cap is a fixed grid of max_fps slots from the mode's start, not "period after this
          // frame": a frame held back by the swap then does not push every later one back too
          // (that ran 60 as 54).
          const int64_t m = t - st.mode_t0, f = st.s.max_fps;
          int64_t k = m * f / 1000 + 1;
          while (k * 1000 / f <= m) k++;  // slot starts are rounded down to whole ms
          due = std::max(st.mode_t0 + st.mode.render(frame, m), st.mode_t0 + k * 1000 / f);
          if (st.mode.gamma)
            for (auto &v : frame.rgb) v = st.s.lut[v];
        }
        // Overlays go on top of the mode or stream, before brightness and the limiter.
        if (ov) st.ov->apply(frame, t, st.s.lut);
        st.out = limit_power(frame, layout.size, st.s.amps_full, want, st.s.ceiling);
        display.show(frame, st.out.pct);
        st.draw.drawn(due, ov, want);
        if (!shown) fprintf(stderr, "first frame\n"), shown = true;  // boot timing (docs/ACCEPTANCE.md)
        frames++, drawn++;
      }
      // Still at least every 100ms: the light sensor moves `want` without waking us. Cheap,
      // since a wake only redraws when needed() says so.
      if (!st.streaming) wake = std::min(wake, st.draw.due);
      if (ov) wake = std::min(wake, t + fx::kFrameMs);
      if (t - fps_t >= 1000) {
        st.fps = frames * 1000.0 / (t - fps_t), frames = 0;
        st.stream_fps = (st.stream_seen - st.stream_fps_base) * 1000.0 / (t - fps_t);
        st.stream_fps_base = st.stream_seen, fps_t = t;
      }
      // Only new frames (a still picture is sent once), or the current one for a new client.
      const uint64_t j = preview ? preview->joined.load() : 0;  // read once: a later join gets the next
      if (preview && preview->wanted() && (drawn != previewed || j != joined) &&
          t - last_preview >= 1000 / st.s.preview_fps) {
        scale_frame(frame, st.out.pct, out);
        std::string msg = status().dump(-1, ' ', false, json::error_handler_t::replace) + "\n";
        msg.append(out.rgb.begin(), out.rgb.end());
        preview->publish(std::move(msg));
        last_preview = t, previewed = drawn, joined = j;
      }
      // On a change (at most once a second, the limiter moves pct with the picture), else
      // a heartbeat every minute.
      const bool changed = st.out.pct != log_pct || st.out.limited() != log_limited;
      if ((changed && t - last_log >= 1000) || t - last_log >= 60000) {
        fprintf(stderr, "est %.2fA%s, brightness %d%%\n", st.out.amps,
                st.out.limited() ? " (limited)" : "", st.out.pct);
        last_log = t, log_pct = st.out.pct, log_limited = st.out.limited();
      }
    } catch (const std::exception &e) {
      // A mode that keeps throwing retries every 100ms: log the first and then once a minute,
      // not 10 lines a second into the journal on the SD card.
      if (++frame_errors == 1 || t - last_frame_error >= 60000) {
        fprintf(stderr, "frame: %s (%d errors so far)\n", e.what(), frame_errors);
        last_frame_error = t;
      }
      wake = t + 100;
    }
    // Wake at the next frame change, at least every 100ms, or when a command arrives.
    mailbox.wait_until(t0 + milliseconds(std::clamp(wake, t + 1, t + 100)));
  }
  button.halt();  // its callback uses everything declared after it
  receiver.shutdown();  // its thread wakes the mailbox, which goes first
  return 0;
}
