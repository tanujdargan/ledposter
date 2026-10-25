// Output to a Colorlight 5A-75B receiving card over raw Ethernet: the card refreshes the panels
// itself (FPGA, 1920Hz+), the Pi only sends frames. Protocol as in FPP's ColorLight-5a-75.cpp,
// checked on a 5A-75B V8.2 with firmware 13.39 (Normal) on 2026-10-24:
//  - every packet: dst 11:22:33:44:55:66, src 22:22:33:44:55:66, the packet type in byte 12
//  - per frame: brightness (0x0A), one row packet (0x55) per row, then "show frame" (0x01) twice
//    (firmware v13+ wants it twice; v9 wanted it once)
//  - pixels in BGR order (red went out blue in RGB order)
//  - the card blanks the whole screen now and then when frames come unevenly or at 30Hz, and holds
//    the last frame steady when they stop; a fixed 60Hz stream held solid (120 too, 250 shimmered).
//    So a thread sends the newest frame on a fixed grid, whatever the frame loop is doing.
//    Filmed later (light on a sheet, IMG_0019): resending a still at 60Hz made the whole wall pop
//    ~2% brighter a few times a second; at 120Hz, or sent once and held, it was steady. Hence
//    --colorlight-hz and --colorlight-resend (stop resending an unchanged frame after N sends).
// The card's own settings (module, scan, cabinet 128x192, which of its ports carry which rows)
// are set once with LEDVision and saved on the card; this only sends pixels.
#pragma once
#include <linux/if_packet.h>
#include <net/if.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "wall.h"

struct Colorlight {
  int hz, resend, syncs;  // syncs: "show frame" packets per frame; send rate; stop resending an unchanged frame after this many sends (0: never)
  int fd = -1, w, h, panels_h;  // card frame; panels_h: rows the panels show, the rest is padding
  std::vector<Point> lut;  // logical -> card pixel
  std::mutex mu;
  std::vector<uint8_t> next, cur;  // card frames, BGR; next is handed over under mu
  bool fresh = false;
  std::atomic<bool> stop{false};
  std::thread th;

  // As root: the raw socket needs CAP_NET_RAW, the caller drops root after this.
  // card_w/card_h: the cabinet size set in LEDVision. The whole of it is sent, black past the
  // panels: left unsent, its leftovers showed as one glitchy column on a 256x256 cabinet.
  // x_offset: added to every card column. After a reflash on 2026-10-25 the card showed column x+1
  // at x (each panel's last column showed its neighbour's or black); 1 put it back.
  Colorlight(const std::string &ifname, int hz, int resend, int syncs, const Layout &L, int cols, int card_w,
             int card_h, int x_offset = 0)
      : hz(hz), resend(resend), syncs(syncs), lut(card_lut(L, cols)) {
    if (x_offset < 0 || x_offset > 64) throw std::runtime_error("matrix.card_x_offset must be 0..64");
    for (Point &p : lut)
      if (p.x >= 0) p.x = int16_t(p.x + x_offset);
    if (syncs < 1 || syncs > 2) throw std::runtime_error("--colorlight-syncs must be 1 or 2");
    if (hz < 1 || hz > 240) throw std::runtime_error("--colorlight-hz must be 1..240");
    if (resend < 0) throw std::runtime_error("--colorlight-resend must be >= 0");
    w = 0, h = 0;
    for (Point p : lut) w = std::max(w, p.x + 1), h = std::max(h, p.y + 1);
    panels_h = h;
    w = std::max(w, card_w), h = std::max(h, card_h);
    if (w > 497) throw std::runtime_error("colorlight: rows over 497 pixels need split packets");
    const unsigned idx = if_nametoindex(ifname.c_str());
    if (!idx) throw std::runtime_error("--colorlight " + ifname + ": no such interface");
    carrier_path = "/sys/class/net/" + ifname + "/carrier";
    fd = socket(AF_PACKET, SOCK_RAW, 0);
    if (fd < 0) throw std::runtime_error(std::string("colorlight socket: ") + strerror(errno));
    sockaddr_ll a{};
    a.sll_family = AF_PACKET;
    a.sll_ifindex = int(idx);
    if (bind(fd, reinterpret_cast<sockaddr *>(&a), sizeof a))
      throw std::runtime_error("colorlight bind " + ifname + ": " + strerror(errno));
    // Bring the link up ourselves: NetworkManager only does it ~40s into boot on a Pi 3B,
    // and until then every send fails with "Network is down" and the wall stays dark.
    ifreq r{};
    strncpy(r.ifr_name, ifname.c_str(), IFNAMSIZ - 1);
    if (ioctl(fd, SIOCGIFFLAGS, &r) == 0 && !(r.ifr_flags & IFF_UP)) {
      r.ifr_flags |= IFF_UP;
      if (ioctl(fd, SIOCSIFFLAGS, &r)) fprintf(stderr, "colorlight: %s up: %s\n", ifname.c_str(), strerror(errno));
    }
    const int buf = 4 << 20;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buf, sizeof buf);
    next.assign(size_t(w) * h * 3, 0), cur = next;
    fprintf(stderr, "colorlight: %s, card frame %dx%d at %dHz, resend %d\n", ifname.c_str(), w, h, hz, resend);
  }
  ~Colorlight() {
    stop = true;
    if (th.joinable()) th.join();
    std::fill(cur.begin(), cur.end(), 0);  // leave the wall dark, like the matrix library does
    for (int i = 0; i < 3; i++) send_frame(), std::this_thread::sleep_for(std::chrono::milliseconds(20));
    if (fd >= 0) close(fd);
  }

  void start() {
    th = std::thread([this] {
      using clock = std::chrono::steady_clock;
      const auto t0 = clock::now();
      int sends = 0;  // of the current frame
      for (int64_t k = 1; !stop; k++) {
        {
          std::lock_guard<std::mutex> l(mu);
          if (fresh) std::swap(cur, next), fresh = false, sends = 0;
        }
        // A held frame the card missed (link not up yet after a replug, card power-cycled) would
        // never come: while there is no carrier, count nothing as sent.
        const bool up = carrier();
        if (!up) sends = 0;
        if (up && (!resend || sends < resend)) send_frame(), sends++;
        std::this_thread::sleep_until(t0 + std::chrono::microseconds(k * 1000000 / hz));
      }
    });
  }

  // pm: output brightness after the limiter in tenths of a percent (smooth fades at low levels),
  // scaled into the pixels.
  void show(const Frame &f, int pm) {
    std::lock_guard<std::mutex> l(mu);
    for (size_t i = 0; i < lut.size(); i++) {
      const Point p = lut[i];
      if (p.x < 0) continue;
      const uint8_t *c = &f.rgb[i * 3];
      uint8_t *d = &next[(size_t(p.y) * w + p.x) * 3];
      d[0] = c[2] * pm / 1000, d[1] = c[1] * pm / 1000, d[2] = c[0] * pm / 1000;
    }
    if (next != cur) fresh = true;  // a still redrawn is not a new frame (cur only changes under mu)
  }

 private:
  std::vector<uint8_t> pkt;
  int send_errors = 0;

  std::string carrier_path;
  bool carrier() const {
    FILE *f = fopen(carrier_path.c_str(), "r");
    if (!f) return false;
    const int c = fgetc(f);
    fclose(f);
    return c == '1';
  }

  void put(uint8_t type, const uint8_t *data, size_t n, size_t size) {
    static const uint8_t mac[12] = {0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x22, 0x22, 0x33, 0x44, 0x55, 0x66};
    pkt.assign(size, 0);
    memcpy(pkt.data(), mac, 12);
    pkt[12] = type;
    if (n) memcpy(&pkt[13], data, n);
    if (::send(fd, pkt.data(), pkt.size(), 0) < 0 && ++send_errors % 600 == 1)  // ~once per 10s
      fprintf(stderr, "colorlight send: %s (%d so far)\n", strerror(errno), send_errors);
  }
  void send_frame() {
    const uint8_t bright[4] = {0xFF, 0xFF, 0xFF, 0xFF};
    put(0x0A, bright, 4, 77);
    std::vector<uint8_t> row(8 + size_t(w) * 3);
    // Rows paced over 60% of the frame period, 8 at a time: sent in one burst (200KB for a 256x256
    // cabinet) the card lost some, which then showed an older frame's rows for a frame (filmed at
    // 240fps: the picture jumped back 2 frames every few). LEDVision paces its stream too.
    const auto t0 = std::chrono::steady_clock::now();
    const auto span = std::chrono::microseconds(600000 / hz);
    // Padding rows around the panels' rows: rows sent just after the last frame's "show frame"
    // landed in the wrong buffer (the first port, J5, jittered back a frame), and the last rows
    // before this frame's one were not all in when it swapped (2026-10-25: J7's panel-boundary
    // columns showed stale pixels in motion, and the fault followed the port when the ribbons
    // were swapped). So half the padding goes first and half last.
    const int pre = (h - panels_h) / 2;
    for (int i = 0; i < h; i++) {
      const int y = i < pre ? panels_h + i : i < pre + panels_h ? i - pre : i;
      if (i && i % 8 == 0) std::this_thread::sleep_until(t0 + span * i / h);
      const uint8_t hdr[8] = {uint8_t(y >> 8), uint8_t(y), 0, 0, uint8_t(w >> 8), uint8_t(w), 0x08, 0x88};
      memcpy(row.data(), hdr, 8);
      memcpy(&row[8], &cur[size_t(y) * w * 3], size_t(w) * 3);
      put(0x55, row.data(), row.size(), 13 + row.size());
    }
    uint8_t sync[99] = {};
    sync[0] = 0x07, sync[22] = 0xFF, sync[23] = 0x05, sync[25] = sync[26] = sync[27] = 0xFF;
    // FPP sends it twice for firmware v13+ ("possibly unnecessary"). Twice at 120Hz, the card now
    // and then showed a stale buffer for ~4ms (filmed at 240fps), so --colorlight-syncs.
    for (int i = 0; i < syncs; i++) put(0x01, sync, sizeof sync, 112);
  }
};
