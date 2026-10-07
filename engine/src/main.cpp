// ledengine: owns the LED matrix and runs the frame loop.
// Milestone 1: config-driven matrix init, logical canvas -> chain mapping,
// test patterns, and the power limiter (pulled forward so a full-white test
// on six panels can't exceed the supply).
#include <led-matrix.h>
#include <yaml-cpp/yaml.h>

#include <chrono>
#include <csignal>
#include <cstdio>
#include <optional>
#include <string>
#include <thread>

#include "patterns.h"

using rgb_matrix::FrameCanvas;
using rgb_matrix::RGBMatrix;

static volatile std::sig_atomic_t g_stop = 0;

static int usage() {
  fprintf(stderr,
          "usage: ledengine [--config /data/config.yaml] [--pattern P] [--color RRGGBB]\n"
          "  P: panel-id (default), solid, cycle, rows, cols\n");
  return 2;
}

int main(int argc, char **argv) {
  std::string config_path = "/data/config.yaml", pattern = "panel-id";
  uint32_t color = 0xffffff;
  if ((argc - 1) % 2) return usage();
  for (int i = 1; i < argc; i += 2) {
    std::string k = argv[i], v = argv[i + 1];
    if (k == "--config") config_path = v;
    else if (k == "--pattern") pattern = v;
    else if (k == "--color") color = std::stoul(v, nullptr, 16);
    else return usage();
  }

  YAML::Node cfg, m;
  std::optional<Layout> L;
  try {
    cfg = YAML::LoadFile(config_path);
    m = cfg["matrix"];
    std::vector<Panel> chain;
    for (const auto &p : m["panels"])
      chain.push_back({p["col"].as<int>(), p["row"].as<int>(), p["rotate"].as<int>(0)});
    L.emplace(m["panel_size"].as<int>(64), chain);
  } catch (const std::exception &e) {
    fprintf(stderr, "config %s: %s\n", config_path.c_str(), e.what());
    return 1;
  }
  const Layout &layout = *L;
  const double ceiling = cfg["power"]["ceiling_amps"].as<double>(13.0);
  const double amps_full = cfg["power"]["amps_full_white_per_panel"].as<double>(6.0);

  Frame frame(layout.width, layout.height);
  if (!draw_pattern(pattern, frame, layout, 0, color)) return usage();

  const std::string mapping = m["hardware_mapping"].as<std::string>("adafruit-hat-pwm");
  RGBMatrix::Options o;
  o.hardware_mapping = mapping.c_str();
  o.rows = o.cols = layout.size;
  o.chain_length = layout.panels.size();
  o.parallel = 1;
  o.pwm_bits = m["pwm_bits"].as<int>(9);
  o.brightness = m["brightness"].as<int>(100);
  o.show_refresh_rate = m["show_refresh_rate"].as<bool>(false);
  rgb_matrix::RuntimeOptions rt;
  rt.gpio_slowdown = m["gpio_slowdown"].as<int>(2);

  RGBMatrix *matrix = RGBMatrix::CreateFromOptions(o, rt);
  if (!matrix) return 1;  // library already printed why
  FrameCanvas *off = matrix->CreateFrameCanvas();
  fprintf(stderr, "canvas %dx%d, %zu panels, pattern %s\n", layout.width, layout.height,
          layout.panels.size(), pattern.c_str());

  signal(SIGINT, [](int) { g_stop = 1; });
  signal(SIGTERM, [](int) { g_stop = 1; });
  auto next = std::chrono::steady_clock::now();
  for (long t = 0; !g_stop; t++) {
    draw_pattern(pattern, frame, layout, t, color);
    const double amps = estimate_amps(frame, layout.size, amps_full, o.brightness);
    const double k = limiter_scale(amps, ceiling);
    for (int i = 0; i < layout.width * layout.height; i++) {
      const Point p = layout.lut[i];
      if (p.x < 0) continue;
      const uint8_t *c = &frame.rgb[size_t(i) * 3];
      off->SetPixel(p.x, p.y, c[0] * k, c[1] * k, c[2] * k);
    }
    off = matrix->SwapOnVSync(off);
    if (t % 30 == 0)
      fprintf(stderr, "est %.1fA%s\n", amps * k, k < 1 ? " (limited)" : "");
    next += std::chrono::microseconds(33333);
    std::this_thread::sleep_until(next);
  }
  delete matrix;  // clears the panels
  return 0;
}
