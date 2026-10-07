// The engine-drawn modes with their settings schemas: effects, clock, text
// and idle. list_modes reports these schemas; set_mode resolves a request's
// settings against them and builds the Mode.
#pragma once
#include <random>
#include <string>
#include <vector>

#include "effects.h"
#include "settings.h"
#include "text.h"

struct ModeSpec {
  std::string mode, name, label, help;  // name: the effect id for effects, else == mode
  Schema settings;
  std::function<Mode(const json &s, uint32_t seed)> make;
};

inline Setting speed_setting() {
  return {"speed", "number", 0.1, 5, 1.0, {}, "animation speed, 1 = normal"};
}
inline Setting colors_setting(std::vector<std::string> def, int max = 6) {
  return {"colors", "colors", 1, double(max), def, {}, ""};
}

// Everything except pattern/media/playlist, for a w x h canvas.
inline std::vector<ModeSpec> mode_specs(int w, int h, const FontSet &fonts) {
  std::vector<ModeSpec> v;
  auto num = [](const json &s, const char *k) { return s[k].get<double>(); };
  auto in = [](const json &s, const char *k) { return int(s[k].get<int64_t>()); };
  auto cols = [](const json &s) { return colors_of(s["colors"]); };

  v.push_back({"effect", "spider-sense", "Spider-sense ripple",
               "shockwave rings from random points; colors alternate ring by ring",
               {speed_setting(), colors_setting({"ff1010", "1040ff"}),
                {"count", "int", 1, 8, 3, {}, "ripples on screen at once"}},
               [=](const json &s, uint32_t seed) {
                 return fx::ripple(w, h, num(s, "speed"), in(s, "count"), cols(s), seed);
               }});
  v.push_back({"effect", "web-shot", "Web-shot trace",
               "a line shoots to an anchor and spins a web; colors: web, glow",
               {speed_setting(), colors_setting({"ffffff", "8fc8ff"}, 2),
                {"strands", "int", 5, 16, 10, {}, "radial strands"},
                {"rings", "int", 2, 12, 7, {}, "rings spun between the strands"}},
               [=](const json &s, uint32_t seed) {
                 return fx::webshot(w, h, num(s, "speed"), in(s, "strands"), in(s, "rings"), cols(s), seed);
               }});
  v.push_back({"effect", "uav-radar", "UAV radar sweep",
               "rotating sweep with a fading trail and blips; colors: sweep, blips",
               {speed_setting(), colors_setting({"00ff50", "ff2020"}, 2),
                {"blips", "int", 0, 16, 6, {}, "contacts on the scope"}},
               [=](const json &s, uint32_t seed) {
                 return fx::radar(w, h, num(s, "speed"), in(s, "blips"), cols(s), seed);
               }});
  v.push_back({"effect", "plasma", "Plasma", "flowing sine plasma; colors form a looping palette",
               {speed_setting(), colors_setting({"ff0060", "4000ff", "00c0ff", "ff00c0"}),
                {"scale", "number", 0.25, 4, 1.0, {}, "pattern size, higher = smaller waves"}},
               [=](const json &s, uint32_t) {
                 return fx::plasma(w, h, num(s, "speed"), num(s, "scale"), cols(s));
               }});
  v.push_back({"effect", "fire", "Fire", "classic heat-diffusion fire; colors run from cool to hot (black first)",
               {speed_setting(), colors_setting({"b00000", "ff5000", "ffb000", "ffff90"}),
                {"height", "number", 0.2, 1, 0.55, {}, "flame height, fraction of the canvas"}},
               [=](const json &s, uint32_t seed) {
                 return fx::fire(w, h, num(s, "speed"), num(s, "height"), cols(s), seed);
               }});
  v.push_back({"effect", "starfield", "Starfield", "flying through stars; each star takes one of the colors",
               {speed_setting(), colors_setting({"ffffff", "a0c0ff", "ffe0a0"}),
                {"density", "number", 0.2, 3, 1.0, {}, "stars, 1 = one per 100 pixels"}},
               [=](const json &s, uint32_t seed) {
                 return fx::starfield(w, h, num(s, "speed"), num(s, "density"), cols(s), seed);
               }});

  v.push_back({"clock", "clock", "Clock", "local time (the Pi's time zone)",
               {{"hour24", "bool", 0, 0, true, {}, "24-hour time"},
                {"seconds", "bool", 0, 0, false, {}, "show seconds"},
                {"date", "bool", 0, 0, false, {}, "show the date"},
                {"color", "color", 0, 0, "ffffff", {}, ""}},
               [&fonts](const json &s, uint32_t) {
                 ClockStyle c;
                 c.hour24 = s["hour24"], c.seconds = s["seconds"], c.date = s["date"];
                 c.color = parse_color(s["color"]);
                 return clock_mode(fonts, c);
               }});

  std::vector<std::string> fnames = fonts.names();
  const std::string fdef = fonts.get("10x20") ? "10x20" : fnames.empty() ? "" : fnames.back();
  v.push_back({"text", "text", "Text scroller", "scrolls text right to left, centred vertically",
               {{"text", "string", 0, 500, "Hello!", {}, ""},
                {"color", "color", 0, 0, "ffffff", {}, ""},
                {"background", "color", 0, 0, "000000", {}, ""},
                speed_setting(),
                {"font", "choice", 0, 0, fdef, fnames, "BDF font from fonts.dir"},
                {"scale", "int", 1, 4, w >= 128 ? 2 : 1, {}, "pixel size of the font"}},
               [&fonts, in, num](const json &s, uint32_t) {
                 const rgb_matrix::Font *f = fonts.get(s["font"]);
                 if (!f) throw std::runtime_error("no fonts loaded (check fonts.dir in the config)");
                 return text_mode(*f, s["text"], in(s, "scale"), parse_color(s["color"]),
                                  parse_color(s["background"]), num(s, "speed"));
               }});

  v.push_back({"idle", "idle", "Idle screen", "dim, slow blue plasma with a dim clock",
               {{"clock", "bool", 0, 0, true, {}, "show the time"},
                {"hour24", "bool", 0, 0, true, {}, "24-hour time"},
                {"level", "number", 0.05, 1, 0.35, {}, "how bright, 1 = full"}},
               [&fonts, w, h](const json &s, uint32_t) {
                 return idle_mode(fonts, w, h, s["clock"], s["level"], s["hour24"]);
               }});
  return v;
}

inline const ModeSpec *find_spec(const std::vector<ModeSpec> &specs, const std::string &mode,
                                 const std::string &name) {
  for (const auto &s : specs)
    if (s.mode == mode && s.name == name) return &s;
  return nullptr;
}

inline json spec_json(const ModeSpec &s) {
  json j = {{"mode", s.mode}, {"label", s.label}, {"help", s.help}, {"settings", json::array()}};
  if (s.mode == "effect") j["effect"] = s.name;
  for (const auto &x : s.settings) j["settings"].push_back(x.to_json());
  return j;
}

struct Built {
  Mode mode;
  std::string kind, name;
  json settings;
};

// set_mode for effect/clock/text/idle. Throws for a bad request.
inline Built build_mode(const std::vector<ModeSpec> &specs, const json &req, uint32_t seed) {
  const std::string kind = req.at("mode");
  std::string name = kind;
  if (kind == "effect") {
    name = arg_str(req, "effect");
    if (!find_spec(specs, kind, name)) {
      std::string all;
      for (const auto &s : specs)
        if (s.mode == "effect") all += (all.empty() ? "" : ", ") + s.name;
      throw std::runtime_error("unknown effect '" + name + "' (" + all + ")");
    }
  }
  const ModeSpec *s = find_spec(specs, kind, name);
  if (!s) throw std::runtime_error("unknown mode '" + kind + "'");
  const json settings = resolve_settings(s->settings, req);
  Mode m = s->make(settings, seed);
  m.name = name;
  return {std::move(m), kind, name, settings};
}

inline uint32_t random_seed() { return std::random_device{}(); }
