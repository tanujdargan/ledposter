// BH1750 ambient light sensor and the lux -> auto brightness curve. The curve
// and smoothing are pure (tests/test_engine.cpp); the sensor is polled on its
// own thread and the frame loop only reads its atomics.
#pragma once
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <string>
#include <thread>

#if defined(__linux__) && !defined(LEDPOSTER_SIM)
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#define LEDPOSTER_I2C 1
#endif

// brightness.auto_curve in the config.
struct AutoCurve {
  double lux_low = 2, min_pct = 10, lux_high = 400, max_pct = 100;
};

// lux_low and darker gives min_pct, lux_high and brighter max_pct, and in
// between it is interpolated on log(lux), which is roughly how eyes see it.
inline double auto_pct(const AutoCurve &c, double lux) {
  if (std::isnan(lux)) return NAN;
  if (lux <= c.lux_low) return c.min_pct;
  if (lux >= c.lux_high) return c.max_pct;
  return c.min_pct + (c.max_pct - c.min_pct) * std::log(lux / c.lux_low) /
                         std::log(c.lux_high / c.lux_low);
}

// One exponential smoothing step from prev toward target, dt_s after the last
// one, with time constant tau_s (0 = no smoothing). NaN prev starts at target.
inline double smooth(double prev, double target, double dt_s, double tau_s) {
  if (tau_s <= 0 || std::isnan(prev)) return target;
  return prev + (target - prev) * (1 - std::exp(-dt_s / tau_s));
}

// Continuous high-res mode reading (big endian) to lux, per the datasheet.
inline double bh1750_lux(uint8_t hi, uint8_t lo) { return ((hi << 8) | lo) / 1.2; }

// Polls the sensor every poll_ms. The reading is smoothed in log(lux) (so the
// curve output moves smoothly) with time constant tau_s. A missing sensor or a
// read error is logged once; lux and smoothed go NaN, so auto brightness falls
// back to the user setting, and it retries every 5s. Sim builds have no I2C:
// set_fake() stands in for the sensor (NaN = missing).
struct LightSensor {
  std::atomic<double> lux{NAN}, smoothed{NAN};  // latest reading; smoothed, for the curve
  std::atomic<double> tau_s;                    // reload_config can change it
  std::atomic<bool> ok{false};

  LightSensor(int bus, int addr, int poll_ms, double tau)
      : tau_s(tau), bus(bus), addr(addr), poll_ms(poll_ms), th([this] { run(); }) {}
  ~LightSensor() {
    {
      std::lock_guard<std::mutex> l(mu);
      stop = true;
    }
    cv.notify_all();
    th.join();
#ifdef LEDPOSTER_I2C
    if (fd >= 0) close(fd);
#endif
  }

  const char *state() const {
#ifdef LEDPOSTER_I2C
    return ok ? "ok" : "missing";
#else
    return ok ? "fake" : "missing";
#endif
  }

  void set_fake(double v) {
    fake = v;
    std::lock_guard<std::mutex> l(mu);
    poked = true;
    cv.notify_all();
  }

  // Sensor thread only from here on.
  const int bus, addr, poll_ms;
  std::atomic<double> fake{NAN};
  std::mutex mu;
  std::condition_variable cv;
  bool stop = false, poked = false;
  int fd = -1;
  std::thread th;  // last: starts after everything above is set up

  // Sleeps up to ms; false when stopping.
  bool pause(int ms) {
    std::unique_lock<std::mutex> l(mu);
    cv.wait_for(l, std::chrono::milliseconds(ms), [&] { return stop || poked; });
    poked = false;
    return !stop;
  }

  bool read(double &v, std::string &why) {
#ifdef LEDPOSTER_I2C
    if (fd < 0) {
      const std::string dev = "/dev/i2c-" + std::to_string(bus);
      fd = open(dev.c_str(), O_RDWR | O_CLOEXEC);
      if (fd < 0) {
        why = dev + ": " + strerror(errno) + (errno == ENOENT ? " (enable I2C in raspi-config)" : "");
        return false;
      }
      const uint8_t power_on = 0x01, continuous_hires = 0x10;
      if (ioctl(fd, I2C_SLAVE, addr) < 0 || write(fd, &power_on, 1) != 1 ||
          write(fd, &continuous_hires, 1) != 1) {
        char a[8];
        snprintf(a, sizeof a, "0x%02x", addr);
        why = "no answer at " + std::string(a) + " on " + dev + ": " + strerror(errno);
        return close(fd), fd = -1, false;
      }
      if (!pause(200)) return false;  // first high-res measurement takes up to 180ms
    }
    uint8_t b[2];
    if (::read(fd, b, 2) != 2) {
      why = std::string("read failed: ") + strerror(errno);
      return close(fd), fd = -1, false;
    }
    v = bh1750_lux(b[0], b[1]);
    return true;
#else
    v = fake;
    why = "no I2C in the simulator (use set_fake_lux)";
    return !std::isnan(v);
#endif
  }

  void run() {
    bool logged = false;
    double log_lux = NAN;
    auto prev = std::chrono::steady_clock::now();
    do {
      double v;
      std::string why;
      const bool good = read(v, why);
      const auto now = std::chrono::steady_clock::now();
      const double dt = std::chrono::duration<double>(now - prev).count();
      prev = now;
      if (good) {
        if (!ok) fprintf(stderr, "light sensor: ok, %.0f lux\n", v);
        log_lux = smooth(ok ? log_lux : NAN, std::log(std::max(v, 0.01)), dt, tau_s);
        lux = v, smoothed = std::exp(log_lux), ok = true, logged = false;
      } else {
        if (!logged)
          fprintf(stderr, "light sensor: %s; auto brightness follows the user setting\n",
                  why.c_str());
        lux = NAN, smoothed = NAN, ok = false, logged = true;
      }
    } while (pause(ok ? poll_ms : 5000));
  }
};
