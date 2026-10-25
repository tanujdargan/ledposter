// Media cache playback (docs/ARCHITECTURE.md). frames.rgb is mmapped and
// frames are copied out; nothing is decoded here. meta.json parsing lives in
// main.cpp so this stays testable with no dependencies.
#pragma once
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <memory>
#include <numeric>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "wall.h"

struct Clip {
  int w = 0, h = 0;
  std::vector<int64_t> ends_ms;          // frame i shows until ends_ms[i]
  std::shared_ptr<const uint8_t> data;   // mmapped frames.rgb
  int64_t total_ms() const { return ends_ms.back(); }
  size_t frame_bytes() const { return size_t(w) * h * 3; }
  size_t frame_at(int64_t ms) const {  // loops
    return std::upper_bound(ends_ms.begin(), ends_ms.end(), ms % total_ms()) - ends_ms.begin();
  }
  const uint8_t *frame(size_t i) const { return data.get() + i * frame_bytes(); }
};

// Checks a cache entry against the canvas and maps its frames. Throws a
// message meant for the user.
inline Clip open_clip(const std::string &dir, int w, int h, const std::vector<int> &durations_ms,
                      int canvas_w, int canvas_h) {
  auto fail = [&](const std::string &why) { return std::runtime_error(dir + ": " + why); };
  if (w != canvas_w || h != canvas_h)
    throw fail("media is " + std::to_string(w) + "x" + std::to_string(h) + " but the canvas is " +
               std::to_string(canvas_w) + "x" + std::to_string(canvas_h) +
               "; transcode it again for this layout");
  if (durations_ms.empty()) throw fail("no frames in meta.json");
  Clip c;
  c.w = w, c.h = h;
  int64_t t = 0;
  for (int d : durations_ms) {
    if (d <= 0) throw fail("frame durations must be positive");
    c.ends_ms.push_back(t += d);
  }
  const std::string path = dir + "/frames.rgb";
  const int fd = open(path.c_str(), O_RDONLY);
  if (fd < 0) throw fail("frames.rgb: " + std::string(strerror(errno)));
  struct stat st {};
  const size_t want = durations_ms.size() * c.frame_bytes();
  if (fstat(fd, &st) || size_t(st.st_size) != want) {
    close(fd);
    throw fail("frames.rgb is " + std::to_string(st.st_size) + " bytes, expected " +
               std::to_string(want) + " (" + std::to_string(durations_ms.size()) +
               " frames); the cache is incomplete or corrupt, transcode it again");
  }
  void *p = mmap(nullptr, want, PROT_READ, MAP_SHARED, fd, 0);
  close(fd);
  if (p == MAP_FAILED) throw fail("mmap frames.rgb: " + std::string(strerror(errno)));
  c.data.reset(static_cast<const uint8_t *>(p),
               [want](const uint8_t *q) { munmap(const_cast<uint8_t *>(q), want); });
  return c;
}

// Reads a clip into the page cache from a side thread, so its first pass does not fault frames
// in from the SD card on the frame loop (that ran 36-58fps instead of 60; MADV_WILLNEED alone did
// too). The card reads ~4x faster than 60fps plays. Done per playlist item just before its turn,
// not at load: a 60fps library is bigger than RAM, and reading it all would push the playing
// clip out.
inline void prefetch(const Clip &c) {
  const size_t n = c.ends_ms.size() * c.frame_bytes();
  std::thread([d = c.data, n] {
    volatile uint8_t sink = 0;
    for (size_t i = 0; i < n; i += 4096) sink = sink + d.get()[i];
  }).detach();
}

// Default playlist item duration: one full play; stills show for 10s.
inline int64_t default_item_ms(const Clip &c) {
  return c.ends_ms.size() == 1 ? 10000 : c.total_ms();
}

// Which item shows when. item_ms[i] is how long item i shows (a clip shorter
// than that loops within the item).
struct Playlist {
  std::vector<int64_t> item_ms;
  bool loop, shuffle;
  std::mt19937 rng;
  std::vector<int> order;
  size_t pos = 0;
  int64_t start = 0;  // when the current item started
  bool done = false;  // not looping and past the end: hold the last frame

  Playlist(std::vector<int64_t> ms, bool loop, bool shuffle,
           unsigned seed = std::random_device{}())
      : item_ms(std::move(ms)), loop(loop), shuffle(shuffle), rng(seed), order(item_ms.size()) {
    std::iota(order.begin(), order.end(), 0);
    if (shuffle) std::shuffle(order.begin(), order.end(), rng);
  }
  int current() const { return order[pos]; }
  // The item after this one (a guess at a reshuffle: the new pass's order is not drawn yet).
  int upcoming() const { return pos + 1 < order.size() ? order[pos + 1] : order[0]; }
  int64_t end() const { return start + item_ms[current()]; }

  // Jumps to the next item at t; false on the last item of the pass (the button then moves on
  // to button.cycle), unless wrap and the playlist loops: then the next pass starts.
  bool skip(int64_t t, bool wrap) {
    seek(t);
    if (done) return false;
    if (pos + 1 >= order.size()) {
      if (!wrap || !loop) return false;
      start = t - item_ms[current()];  // the item ends now: seek starts the next pass
      seek(t);
      return true;
    }
    pos++, start = t;
    return true;
  }

  // Advances to t (non-decreasing) and returns ms into the current item.
  int64_t seek(int64_t t) {
    while (!done && t >= end()) {
      if (pos + 1 == order.size() && !loop) {
        done = true;
        break;
      }
      start = end();
      if (++pos < order.size()) continue;
      pos = 0;
      if (!shuffle) continue;
      const int last = order.back();  // new pass: reshuffle, but never repeat back to back
      std::shuffle(order.begin(), order.end(), rng);
      if (order.size() > 1 && order[0] == last) std::swap(order[0], order.back());
    }
    return done ? item_ms[current()] - 1 : t - start;
  }
};

// Media player mode: clips[i] is playlist item i.
inline Mode media_mode(const std::string &name, std::vector<Clip> clips, Playlist pl) {
  auto p = std::make_shared<Playlist>(std::move(pl));
  // The item playing and the one prefetched as next, so each item is read in once, ahead.
  auto seen = std::make_shared<std::pair<int, int>>(-1, -1);
  Mode m{name, false, [clips = std::move(clips), p, seen](Frame &f, int64_t t) {
           Playlist &pl = *p;
           const int64_t into = pl.seek(t);
           if (seen->first != pl.current()) {
             if (seen->second != pl.current()) prefetch(clips[pl.current()]);  // first, or a reshuffle
             if (pl.upcoming() != pl.current()) prefetch(clips[pl.upcoming()]);
             *seen = {pl.current(), pl.upcoming()};
           }
           const Clip &c = clips[pl.current()];
           const int64_t local = into % c.total_ms();
           const size_t i = c.frame_at(local);
           std::memcpy(f.rgb.data(), c.frame(i), c.frame_bytes());
           if (pl.done) return t + 1000;
           return std::min(pl.end(), t - local + c.ends_ms[i]);
         }};
  m.skip = [p](int64_t t, bool wrap) { return p->skip(t, wrap); };
  return m;
}
