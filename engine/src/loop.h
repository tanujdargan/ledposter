// Frame loop decisions, kept pure so they are unit tested (tests/test_engine.cpp).
#pragma once
#include <cstdint>
#include <string>

// Whether the frame loop draws on this wake. Every draw is a SetPixel pass and a
// vsync swap, memory traffic that stalls the refresh thread on a Pi 3B, so wakes
// for status polls, other commands and the 100ms tick only draw if the picture
// would change.
struct Redraw {
  bool dirty = true;     // set by jobs that change the picture (mode, settings, gamma, skip, stream end)
  int64_t due = 0;       // engine ms the mode next needs to draw
  int want = -1;         // brightness the last frame went out at (0: power off)
  bool overlay = false;  // the last frame had an overlay on it

  // streaming: the stream is shown instead of the mode; fresh: a new stream frame
  // arrived. overlay: an overlay is active at t. want: brightness asked for now.
  bool needed(int64_t t, bool streaming, bool fresh, bool overlay_on, int want_now) const {
    return dirty || want_now != want || overlay_on || overlay || (streaming ? fresh : t >= due);
  }
  // next_due: what the mode's render returned (unchanged while streaming).
  void drawn(int64_t next_due, bool overlay_on, int want_now) {
    dirty = false, due = next_due, overlay = overlay_on, want = want_now;
  }
};

// reload_config: rebuild (and restart) the running playlist only if its own
// definition changed, so saving unrelated settings keeps its place. defs are the
// playlist's config as text, before and after ("" if missing: rebuilding reports it).
inline bool rebuild_playlist(const std::string &running_kind, const std::string &old_def,
                             const std::string &new_def) {
  return running_kind == "playlist" && old_def != new_def;
}
