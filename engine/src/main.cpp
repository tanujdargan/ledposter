// ledengine: owns the LED matrix and runs the frame loop.
// Milestone 2: plays cached media and playlists (media.h) as well as the
// milestone 1 test patterns. The power limiter runs on every frame.
// Built with -DLEDPOSTER_SIM (make sim) it runs without the matrix library.
#include <yaml-cpp/yaml.h>

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <thread>

#include "media.h"
#include "patterns.h"

#ifndef LEDPOSTER_SIM
#include <led-matrix.h>

// The only code that touches the panels.
struct Display {
  rgb_matrix::RGBMatrix *matrix;
  rgb_matrix::FrameCanvas *off;
  const Layout &L;

  Display(const YAML::Node &m, const Layout &L, int brightness, const std::string &) : L(L) {
    const std::string mapping = m["hardware_mapping"].as<std::string>("adafruit-hat-pwm");
    rgb_matrix::RGBMatrix::Options o;
    o.hardware_mapping = mapping.c_str();
    o.rows = o.cols = L.size;
    o.chain_length = L.panels.size();
    o.parallel = 1;
    o.pwm_bits = m["pwm_bits"].as<int>(9);
    o.brightness = brightness;
    o.show_refresh_rate = m["show_refresh_rate"].as<bool>(false);
    rgb_matrix::RuntimeOptions rt;
    rt.gpio_slowdown = m["gpio_slowdown"].as<int>(2);
    matrix = rgb_matrix::RGBMatrix::CreateFromOptions(o, rt);
    if (!matrix) exit(1);  // library already printed why
    matrix->set_luminance_correct(false);  // we own gamma (docs/ARCHITECTURE.md)
    off = matrix->CreateFrameCanvas();
  }
  ~Display() { delete matrix; }  // clears the panels

  void show(const Frame &f, double k) {
    for (int i = 0; i < L.width * L.height; i++) {
      const Point p = L.lut[i];
      if (p.x < 0) continue;
      const uint8_t *c = &f.rgb[size_t(i) * 3];
      off->SetPixel(p.x, p.y, c[0] * k, c[1] * k, c[2] * k);
    }
    off = matrix->SwapOnVSync(off);
  }
};
#else
// make sim: no matrix. Keeps the output frame (after the limiter) and, with
// --dump, writes it as a PPM about once a second and on exit.
struct Display {
  Frame out;
  std::string dump;
  std::chrono::steady_clock::time_point last{};

  Display(const YAML::Node &, const Layout &L, int, const std::string &dump)
      : out(L.width, L.height), dump(dump) {}
  ~Display() { write(); }

  void show(const Frame &f, double k) {
    for (size_t i = 0; i < f.rgb.size(); i++) out.rgb[i] = f.rgb[i] * k;
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
          "                 [--pattern P [--color RRGGBB] | --play MEDIA_ID | --playlist NAME]\n"
          "  P: panel-id (default), solid, cycle, rows, cols\n"
          "  media is read from $LEDPOSTER_DATA/media (LEDPOSTER_DATA defaults to /data)\n"
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

int main(int argc, char **argv) {
  const char *env = getenv("LEDPOSTER_DATA");
  const std::string data = env && *env ? env : "/data", media_dir = data + "/media";
  std::string config_path = data + "/config.yaml", pattern = "panel-id", play, playlist, dump;
  uint32_t color = 0xffffff;
  if ((argc - 1) % 2) return usage();
  for (int i = 1; i < argc; i += 2) {
    std::string k = argv[i], v = argv[i + 1];
    if (k == "--config") config_path = v;
    else if (k == "--pattern") pattern = v;
    else if (k == "--color") color = std::stoul(v, nullptr, 16);
    else if (k == "--play") play = v;
    else if (k == "--playlist") playlist = v;
#ifdef LEDPOSTER_SIM
    else if (k == "--dump") dump = v;
#endif
    else return usage();
  }
  if (!play.empty() && !playlist.empty()) return usage();

  YAML::Node cfg, m;
  std::optional<Layout> L;
  Mode mode;
  try {
    cfg = YAML::LoadFile(config_path);
    m = cfg["matrix"];
    std::vector<Panel> chain;
    for (const auto &p : m["panels"])
      chain.push_back({p["col"].as<int>(), p["row"].as<int>(), p["rotate"].as<int>(0)});
    L.emplace(m["panel_size"].as<int>(64), chain);
    if (!play.empty()) {
      mode = player(play, {{play, 0}}, true, false, media_dir, *L);
    } else if (!playlist.empty()) {
      const YAML::Node p = cfg["playlists"][playlist];
      if (!p) throw std::runtime_error("no playlist '" + playlist + "' under playlists:");
      std::vector<std::pair<std::string, int64_t>> items;
      for (const auto &it : p["items"]) {
        const double s = it["duration_s"].as<double>(0);
        if (it["duration_s"] && s <= 0) throw std::runtime_error("duration_s must be > 0");
        items.push_back({it["media"].as<std::string>(), int64_t(s * 1000)});
      }
      mode = player(playlist, items, p["loop"].as<bool>(true), p["shuffle"].as<bool>(false),
                    media_dir, *L);
    } else {
      Frame probe(L->width, L->height);
      if (!draw_pattern(pattern, probe, *L, 0, color)) return usage();
      mode = pattern_mode(pattern, *L, color);
    }
  } catch (const YAML::Exception &e) {
    fprintf(stderr, "config %s: %s\n", config_path.c_str(), e.what());
    return 1;
  } catch (const std::exception &e) {
    fprintf(stderr, "ledengine: %s\n", e.what());
    return 1;
  }
  const Layout &layout = *L;
  const double ceiling = cfg["power"]["ceiling_amps"].as<double>(13.0);
  const double amps_full = cfg["power"]["amps_full_white_per_panel"].as<double>(6.0);
  const int brightness = m["brightness"].as<int>(100);
  const auto lut = gamma_lut(cfg["display"]["gamma"].as<double>(2.2));

  Display display(m, layout, brightness, dump);
  fprintf(stderr, "canvas %dx%d, %zu panels, mode %s\n", layout.width, layout.height,
          layout.panels.size(), mode.name.c_str());

  signal(SIGINT, [](int) { g_stop = 1; });
  signal(SIGTERM, [](int) { g_stop = 1; });
  Frame frame(layout.width, layout.height);
  const auto t0 = std::chrono::steady_clock::now();
  int64_t last_log = -1000;
  while (!g_stop) {
    using namespace std::chrono;
    const int64_t t = duration_cast<milliseconds>(steady_clock::now() - t0).count();
    const int64_t next = mode.render(frame, t);
    if (mode.gamma)
      for (auto &v : frame.rgb) v = lut[v];
    const double amps = estimate_amps(frame, layout.size, amps_full, brightness);
    const double k = limiter_scale(amps, ceiling);
    display.show(frame, k);
    if (t - last_log >= 1000) {
      fprintf(stderr, "est %.1fA%s\n", amps * k, k < 1 ? " (limited)" : "");
      last_log = t;
    }
    // Wake at the next frame change, and at least every 100ms.
    std::this_thread::sleep_until(t0 + milliseconds(std::clamp(next, t + 1, t + 100)));
  }
  return 0;
}
