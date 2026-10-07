// Per-mode settings schemas (list_modes in docs/protocol.md) and validation of
// the settings a set_mode request brings. Pure, unit tested.
#pragma once
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <string>
#include <vector>

#include "ipc.h"

// One setting. type is one of:
//   number  min..max            int     min..max (whole numbers)
//   bool                        color   "rrggbb"
//   colors  list of "rrggbb", min..max entries
//   string  up to max characters (UTF-8 bytes)
//   choice  one of choices
struct Setting {
  std::string name, type;
  double min = 0, max = 0;
  json def;
  std::vector<std::string> choices = {};
  std::string help = "";

  json to_json() const {
    json j = {{"name", name}, {"type", type}, {"default", def}};
    if (type == "number" || type == "int" || type == "colors" || type == "string") {
      if (type != "string") j["min"] = min;
      j["max"] = max;
    }
    if (type == "choice") j["choices"] = choices;
    if (!help.empty()) j["help"] = help;
    return j;
  }
};
using Schema = std::vector<Setting>;

inline std::string hex6(uint32_t c) {
  char b[8];
  snprintf(b, sizeof b, "%06x", c & 0xffffff);
  return b;
}

inline std::string fmt_num(double v) {
  char b[32];
  snprintf(b, sizeof b, "%g", v);
  return b;
}

// One value checked against its setting, normalized (colors as "rrggbb").
inline json check_setting(const Setting &s, const json &v) {
  auto bad = [&](const std::string &what) {
    return std::runtime_error("'" + s.name + "' must be " + what);
  };
  const std::string range = fmt_num(s.min) + " to " + fmt_num(s.max);
  if (s.type == "number") {
    if (!v.is_number() || !(v.get<double>() >= s.min && v.get<double>() <= s.max))
      throw bad("a number from " + range);
    return v.get<double>();
  }
  if (s.type == "int") {
    if (!v.is_number() || v.get<double>() != std::floor(v.get<double>()) ||
        !(v.get<double>() >= s.min && v.get<double>() <= s.max))
      throw bad("a whole number from " + range);
    return int64_t(v.get<double>());
  }
  if (s.type == "bool") {
    if (!v.is_boolean()) throw bad("true or false");
    return v;
  }
  if (s.type == "color") {
    try {
      return hex6(parse_color(v));
    } catch (const std::runtime_error &) {
      throw bad("a color, \"RRGGBB\" hex");
    }
  }
  if (s.type == "colors") {
    const json list = v.is_string() ? json::array({v}) : v;
    const std::string want = "a list of " + fmt_num(s.min) + " to " + fmt_num(s.max) +
                             " \"RRGGBB\" colors";
    if (!list.is_array() || list.size() < s.min || list.size() > s.max) throw bad(want);
    json out = json::array();
    for (const auto &c : list) {
      try {
        out.push_back(hex6(parse_color(c)));
      } catch (const std::runtime_error &) {
        throw bad(want);
      }
    }
    return out;
  }
  if (s.type == "string") {
    if (!v.is_string() || v.get<std::string>().size() > s.max)
      throw bad("a string of at most " + fmt_num(s.max) + " bytes");
    return v;
  }
  if (s.type == "choice") {
    if (v.is_string())
      for (const auto &c : s.choices)
        if (c == v) return v;
    std::string all;
    for (const auto &c : s.choices) all += (all.empty() ? "" : ", ") + c;
    throw bad("one of " + all);
  }
  throw std::logic_error("bad setting type " + s.type);
}

// The settings for a set_mode request: defaults, overridden by the request's
// "settings" object, overridden by top-level fields named like a setting (so
// {"mode":"text","text":"hi"} works). Unknown keys inside "settings" are an
// error; unknown top-level keys are ignored (they are cmd, id, mode, ...).
inline json resolve_settings(const Schema &schema, const json &req) {
  json out = json::object();
  for (const auto &s : schema) out[s.name] = s.def;
  auto apply = [&](const json &src, bool strict) {
    for (auto it = src.begin(); it != src.end(); ++it) {
      const Setting *s = nullptr;
      for (const auto &c : schema)
        if (c.name == it.key()) s = &c;
      if (!s) {
        if (!strict) continue;
        std::string names;
        for (const auto &c : schema) names += (names.empty() ? "" : ", ") + c.name;
        throw std::runtime_error("unknown setting '" + it.key() + "'" +
                                 (names.empty() ? " (this mode has none)" : " (" + names + ")"));
      }
      out[s->name] = check_setting(*s, it.value());
    }
  };
  if (req.contains("settings")) {
    if (!req["settings"].is_object()) throw std::runtime_error("'settings' must be an object");
    apply(req["settings"], true);
  }
  if (req.is_object()) apply(req, false);
  return out;
}

inline std::vector<uint32_t> colors_of(const json &list) {
  std::vector<uint32_t> out;
  for (const auto &c : list) out.push_back(parse_color(c));
  return out;
}
