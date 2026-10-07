// The push button (spec: Controls on the frame). A pure press classifier
// (unit tested) fed by a 5ms poll of one GPIO line through the Linux GPIO
// character device (v1 uAPI, the one libgpiod 1.6 on Bookworm uses; no
// library needed). Sim builds have no GPIO: sim_button holds a fake line down.
#pragma once
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <string>
#include <thread>

#if defined(__linux__) && !defined(LEDPOSTER_SIM)
#include <fcntl.h>
#include <linux/gpio.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#define LEDPOSTER_GPIO 1
#endif

enum class Press { None, Short, Long, VeryLong };

inline const char *press_name(Press p) {
  return p == Press::Short ? "short" : p == Press::Long ? "long" : p == Press::VeryLong ? "very long" : "none";
}

// Feed it the raw level (true = pressed) with a non-decreasing time in ms.
// Debounce: a level counts once it held for debounce_ms; durations are
// measured between the raw edges, so debouncing doesn't shorten them.
//   released after < long_ms        -> Short (on release)
//   released after long_ms..very    -> Long (on release)
//   still held at very_long_ms      -> VeryLong (right then; the release is ignored)
struct PressClassifier {
  int debounce_ms = 30, long_ms = 2000, very_long_ms = 10000;
  bool stable = false, raw = false, very_fired = false;
  int64_t raw_since = 0, press_start = 0;

  Press feed(bool pressed, int64_t t) {
    if (pressed != raw) raw = pressed, raw_since = t;
    if (raw != stable && t - raw_since >= debounce_ms) {
      stable = raw;
      if (stable) {
        press_start = raw_since, very_fired = false;
      } else if (!very_fired) {
        return raw_since - press_start >= long_ms ? Press::Long : Press::Short;
      }
    }
    if (stable && raw && !very_fired && t - press_start >= very_long_ms) {
      very_fired = true;
      return Press::VeryLong;
    }
    return Press::None;
  }
};

// Polls the line every 5ms on its own thread and calls on_press. Open the
// line (constructor) before the matrix starts: the matrix library drops root
// right after, and /dev/gpiochip* needs it (or the gpio group).
struct Button {
  PressClassifier cls;
  std::string state = "off";  // off (no gpio configured), ok, missing, sim
  int gpio = -1;
  std::function<void(Press)> on_press;
  std::atomic<bool> stop{false};
  std::atomic<int64_t> fake_until{0};  // sim: pressed until this steady-clock ms
  int fd = -1;
  std::thread th;

  static int64_t now_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
  }

  Button(const std::string &chip, int gpio, PressClassifier c) : cls(c), gpio(gpio) {
#ifdef LEDPOSTER_GPIO
    if (gpio < 0) return;
    const int cfd = open(chip.c_str(), O_RDONLY | O_CLOEXEC);
    if (cfd < 0) {
      fprintf(stderr, "button: %s: %s; button off\n", chip.c_str(), strerror(errno));
      state = "missing";
      return;
    }
    gpiohandle_request req{};
    req.lineoffsets[0] = gpio;
    req.lines = 1;
    req.flags = GPIOHANDLE_REQUEST_INPUT;
#ifdef GPIOHANDLE_REQUEST_BIAS_PULL_UP
    req.flags |= GPIOHANDLE_REQUEST_BIAS_PULL_UP;  // active low: button to GND
#endif
    snprintf(req.consumer_label, sizeof req.consumer_label, "ledposter-button");
    if (ioctl(cfd, GPIO_GET_LINEHANDLE_IOCTL, &req) < 0) {
      fprintf(stderr, "button: GPIO%d on %s: %s; button off\n", gpio, chip.c_str(), strerror(errno));
      state = "missing";
    } else {
      fd = req.fd;
      state = "ok";
      fprintf(stderr, "button: GPIO%d on %s\n", gpio, chip.c_str());
    }
    close(cfd);
#else
    (void)chip;
    state = gpio >= 0 ? "sim" : "off";
#endif
  }
  ~Button() {
    halt();
#ifdef LEDPOSTER_GPIO
    if (fd >= 0) close(fd);
#endif
  }

  bool pressed() {
#ifdef LEDPOSTER_GPIO
    gpiohandle_data d{};
    if (ioctl(fd, GPIOHANDLE_GET_LINE_VALUES_IOCTL, &d) < 0) return false;
    return d.values[0] == 0;  // active low
#else
    return now_ms() < fake_until;
#endif
  }

  void simulate(int ms) { fake_until = now_ms() + ms; }

  void halt() {
    stop = true;
    if (th.joinable()) th.join();
  }

  void start(std::function<void(Press)> cb) {
    if (state != "ok" && state != "sim") return;
    on_press = std::move(cb);
    th = std::thread([this] {
      while (!stop) {
        const Press p = cls.feed(pressed(), now_ms());
        if (p != Press::None) on_press(p);
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
      }
    });
  }
};
