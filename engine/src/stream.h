// External input: DDP (UDP 4048) and E1.31/sACN (UDP 5568) receivers.
// Pixels are RGB, row-major from the top-left of the logical canvas. The
// packet parsers and the frame assembler are pure (tests/test_engine.cpp);
// StreamReceiver runs the sockets on its own thread and hands complete
// frames to the frame loop through StreamOut (a mutex-guarded copy).
#pragma once
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <functional>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

// ---- packet parsing (pure) ----

inline uint32_t be16(const uint8_t *p) { return uint32_t(p[0]) << 8 | p[1]; }
inline uint32_t be32(const uint8_t *p) { return be16(p) << 16 | be16(p + 2); }

// DDP (http://www.3waylabs.com/ddp/): 10-byte header (14 with timecode), then data.
struct DdpPacket {
  bool push;
  uint32_t offset;  // in bytes into the RGB buffer
  const uint8_t *data;
  size_t len;
};

// nullopt for anything that isn't pixel data we can use: wrong version,
// queries/replies, other devices (id 0/1 only), non-RGB8 data types, short packets.
inline std::optional<DdpPacket> parse_ddp(const uint8_t *p, size_t n) {
  if (n < 10) return std::nullopt;
  const uint8_t flags = p[0], type = p[2], id = p[3];
  if ((flags >> 6) != 1) return std::nullopt;   // version 1
  if (flags & 0x06) return std::nullopt;        // query or reply
  if (id > 1) return std::nullopt;              // 1 = default output; WLED/LedFx use it
  // RGB 8-bit is 0x0B; 0x01 (LedFx) and 0x00 (undefined) are sent as RGB in practice.
  if (type != 0x0B && type != 0x01 && type != 0x00) return std::nullopt;
  const size_t hdr = flags & 0x10 ? 14 : 10;    // timecode adds 4 bytes
  if (n < hdr) return std::nullopt;
  const size_t len = be16(p + 8);
  if (n < hdr + len) return std::nullopt;
  return DdpPacket{bool(flags & 0x01), be32(p + 4), p + hdr, len};
}

// E1.31 data packet: root layer, framing layer, DMP layer, then up to 512 channels.
struct E131Packet {
  uint16_t universe;
  uint8_t sequence;
  const uint8_t *data;  // DMX channels 1..n (after the start code)
  size_t len;
};

inline std::optional<E131Packet> parse_e131(const uint8_t *p, size_t n) {
  static const uint8_t kAcnId[12] = {'A', 'S', 'C', '-', 'E', '1', '.', '1', '7', 0, 0, 0};
  if (n < 126 || n > 638) return std::nullopt;
  auto pdu = [&](size_t at) { return (be16(p + at) >> 12) == 0x7 && (be16(p + at) & 0xfff) == n - at; };
  if (be16(p) != 0x0010 || be16(p + 2) != 0 || memcmp(p + 4, kAcnId, 12)) return std::nullopt;
  if (!pdu(16) || be32(p + 18) != 0x00000004) return std::nullopt;  // root: E1.31 data
  if (!pdu(38) || be32(p + 40) != 0x00000002) return std::nullopt;  // framing: data packet
  const uint8_t options = p[112];
  if (options & 0xC0) return std::nullopt;  // preview data, or stream terminated
  const uint16_t universe = be16(p + 113);
  if (universe < 1 || universe > 63999) return std::nullopt;
  if (!pdu(115) || p[117] != 0x02 || p[118] != 0xA1 || be16(p + 119) != 0 || be16(p + 121) != 1)
    return std::nullopt;  // DMP: set property, address/data type, first address, increment
  const size_t count = be16(p + 123);  // start code + channels
  if (count < 1 || count > 513 || 125 + count != n) return std::nullopt;
  if (p[125] != 0) return std::nullopt;  // only start code 0 (DMX levels)
  return E131Packet{universe, p[111], p + 126, count - 1};
}

// 170 RGB pixels (510 channels) per universe; the last 2 channels are unused.
constexpr int kPixelsPerUniverse = 170;
inline int universes_for(int pixels) {
  return (pixels + kPixelsPerUniverse - 1) / kPixelsPerUniverse;
}

// E1.31 6.7.2: a sequence number within 20 behind the last one is stale (or a duplicate).
inline bool e131_stale(int last, uint8_t seq) {
  if (last < 0) return false;
  const int d = int8_t(uint8_t(seq - last));
  return d <= 0 && d > -20;
}

// Assembles packets into a w*h RGB frame. Each call returns kIgnored (not for
// us or malformed), kPartial (valid, frame not complete yet) or kFrame (frame
// now holds a complete picture to show).
struct Assembler {
  enum Result { kIgnored, kPartial, kFrame };
  int start_universe;
  std::vector<uint8_t> work, frame;  // being filled; last complete
  // DDP: senders that set the push flag get frames on push; others on every packet.
  bool ddp_push_seen = false;
  // E1.31: per-universe last sequence, and which universes this frame has.
  std::vector<int> last_seq;
  std::vector<bool> got;
  int ngot = 0;

  Assembler(int w, int h, int start_universe)
      : start_universe(start_universe), work(size_t(w) * h * 3), frame(work.size()),
        last_seq(universes_for(w * h), -1), got(last_seq.size()) {}

  Result ddp(const uint8_t *p, size_t n) {
    const auto d = parse_ddp(p, n);
    if (!d) return kIgnored;
    if (d->offset < work.size())
      memcpy(work.data() + d->offset, d->data, std::min(d->len, work.size() - d->offset));
    ddp_push_seen |= d->push;
    if (!d->push && ddp_push_seen) return kPartial;
    frame = work;
    return kFrame;
  }

  // A frame is complete when every universe has arrived, or when one arrives
  // again (the sender covers fewer universes than the canvas has).
  Result e131(const uint8_t *p, size_t n) {
    const auto e = parse_e131(p, n);
    if (!e || e->universe < start_universe) return kIgnored;
    const size_t u = e->universe - start_universe;
    if (u >= got.size() || e131_stale(last_seq[u], e->sequence)) return kIgnored;
    last_seq[u] = e->sequence;
    Result r = kPartial;
    if (got[u]) r = kFrame, frame = work, got.assign(got.size(), false), ngot = 0;
    const size_t at = u * kPixelsPerUniverse * 3;
    memcpy(work.data() + at, e->data,
           std::min({e->len - e->len % 3, size_t(kPixelsPerUniverse * 3), work.size() - at}));
    got[u] = true;
    if (++ngot == int(got.size())) r = kFrame, frame = work, got.assign(got.size(), false), ngot = 0;
    return r;
  }
};

// ---- receiver ----

// ddp.*, e131.* and stream.* in the config.
struct StreamConfig {
  bool ddp = false, e131 = false, multicast = false, apply_gamma = true;
  int ddp_port = 4048, e131_port = 5568, start_universe = 1;
  double timeout_s = 5;
  bool same_sockets(const StreamConfig &o) const {
    return ddp == o.ddp && e131 == o.e131 && multicast == o.multicast && ddp_port == o.ddp_port &&
           e131_port == o.e131_port && start_universe == o.start_universe;
  }
};

inline int64_t steady_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

// Receiver thread -> frame loop.
struct StreamOut {
  std::mutex mu;
  std::vector<uint8_t> rgb;
  uint64_t frames = 0;          // complete frames received
  int64_t last_packet_ms = -1;  // steady_ms() of the last valid packet
  std::string source;           // "ddp" or "e131", of the last frame
  std::function<void()> on_frame;  // wakes the frame loop; set before the receiver starts

  void touch() {
    std::lock_guard<std::mutex> l(mu);
    last_packet_ms = steady_ms();
  }
  void publish(const std::vector<uint8_t> &f, const char *src) {
    {
      std::lock_guard<std::mutex> l(mu);
      rgb = f, frames++, source = src, last_packet_ms = steady_ms();
    }
    if (on_frame) on_frame();
  }
};

inline int open_udp(int port, const char *what) {
  auto fail = [&](const char *step) {
    return std::runtime_error(std::string(what) + ": UDP port " + std::to_string(port) + ": " +
                              step + ": " + strerror(errno));
  };
  const int fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0) throw fail("socket");
  const int rcvbuf = 1 << 20;  // an E1.31 frame is 145 packets in a burst
  setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons(port);
  a.sin_addr.s_addr = htonl(INADDR_ANY);
  if (bind(fd, (sockaddr *)&a, sizeof a)) {
    const auto e = fail("bind");
    close(fd);
    throw e;
  }
  fcntl(fd, F_SETFL, O_NONBLOCK);
  fcntl(fd, F_SETFD, FD_CLOEXEC);
  return fd;
}

// Owns the UDP sockets and their thread. configure() is called from the
// control thread (and once at start); it replaces sockets only when they change.
struct StreamReceiver {
  const int w, h;
  StreamOut &out;
  StreamConfig cfg;
  std::atomic<bool> ddp_on{false}, e131_on{false};
  int ddp_fd = -1, e131_fd = -1, wake_r = -1, wake_w = -1;
  std::atomic<bool> stop{false};
  std::thread th;

  StreamReceiver(int w, int h, StreamOut &out) : w(w), h(h), out(out) {}
  ~StreamReceiver() { shutdown(); }

  bool any() const { return ddp_on || e131_on; }

  // Throws if a port can't be opened; that receiver then stays off.
  void configure(const StreamConfig &c) {
    if (th.joinable() && c.same_sockets(cfg)) return;
    shutdown();
    cfg = c;
    if (!c.ddp && !c.e131) return;
    std::string err;
    try {
      if (c.ddp) ddp_fd = open_udp(c.ddp_port, "ddp");
    } catch (const std::exception &e) {
      err = e.what();
    }
    try {
      if (c.e131) e131_fd = open_udp(c.e131_port, "e131"), join_multicast();
    } catch (const std::exception &e) {
      err += (err.empty() ? "" : "; ") + std::string(e.what());
    }
    ddp_on = ddp_fd >= 0, e131_on = e131_fd >= 0;
    cfg.ddp = ddp_on, cfg.e131 = e131_on;
    if (any()) {
      int p[2];
      if (pipe(p) == 0) {
        wake_r = p[0], wake_w = p[1];
        stop = false;
        th = std::thread([this, a = Assembler(w, h, c.start_universe)]() mutable { run(a); });
      }
    }
    if (!err.empty()) throw std::runtime_error(err);
  }

  void join_multicast() {
    if (!cfg.multicast) return;
    // 239.255.<hi>.<lo> per universe. Linux allows 20 groups per socket by
    // default (net.ipv4.igmp_max_memberships); unicast needs none.
    const int n = universes_for(w * h);
    int joined = 0;
    for (int u = cfg.start_universe; u < cfg.start_universe + n; u++) {
      ip_mreq m{};
      m.imr_multiaddr.s_addr = htonl(0xEFFF0000u | uint32_t(u));
      m.imr_interface.s_addr = htonl(INADDR_ANY);
      joined += setsockopt(e131_fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &m, sizeof m) == 0;
    }
    if (joined < n)
      fprintf(stderr,
              "e131: joined %d of %d multicast groups (raise net.ipv4.igmp_max_memberships, or "
              "send unicast)\n",
              joined, n);
  }

  void shutdown() {
    if (th.joinable()) {
      stop = true;
      (void)!write(wake_w, "x", 1);
      th.join();
    }
    for (int *fd : {&ddp_fd, &e131_fd, &wake_r, &wake_w})
      if (*fd >= 0) close(*fd), *fd = -1;
    ddp_on = e131_on = false;
  }

  void run(Assembler &a) {
    std::vector<uint8_t> buf(65536);
    while (!stop) {
      pollfd p[3] = {{wake_r, POLLIN, 0}, {ddp_fd, POLLIN, 0}, {e131_fd, POLLIN, 0}};
      if (poll(p, 3, -1) < 0) continue;  // EINTR; negative fds are skipped by poll
      for (int i = 1; i < 3; i++) {
        if (!(p[i].revents & POLLIN)) continue;
        for (ssize_t n; (n = recv(p[i].fd, buf.data(), buf.size(), 0)) >= 0;) {
          const bool ddp = i == 1;
          const auto r = ddp ? a.ddp(buf.data(), n) : a.e131(buf.data(), n);
          if (r == Assembler::kFrame) out.publish(a.frame, ddp ? "ddp" : "e131");
          else if (r == Assembler::kPartial) out.touch();
        }
      }
    }
  }
};
