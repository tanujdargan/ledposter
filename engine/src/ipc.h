// Engine <-> web app sockets (docs/protocol.md). Socket I/O runs on its own
// threads. The frame loop only runs queued jobs (Mailbox) and hands over
// preview frames (PreviewServer::publish), so a client can never stall it.
#pragma once
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using json = nlohmann::json;

// ---- request parsing (pure, unit tested) ----

// Complete newline-terminated lines from buf; the partial tail stays in buf.
inline std::vector<std::string> take_lines(std::string &buf) {
  std::vector<std::string> lines;
  size_t start = 0, nl;
  while ((nl = buf.find('\n', start)) != std::string::npos) {
    std::string line = buf.substr(start, nl - start);
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (!line.empty()) lines.push_back(std::move(line));
    start = nl + 1;
  }
  buf.erase(0, start);
  return lines;
}

// The request envelope: an object with a string "cmd". Returns the cmd.
inline std::string request_cmd(const json &req) {
  if (!req.is_object()) throw std::runtime_error("request must be a JSON object");
  if (!req.contains("cmd") || !req["cmd"].is_string())
    throw std::runtime_error("missing string field 'cmd'");
  return req["cmd"];
}

// Argument readers; they throw messages meant for the client.
inline const json &arg(const json &req, const char *key) {
  if (!req.contains(key)) throw std::runtime_error(std::string("missing field '") + key + "'");
  return req[key];
}
inline std::string arg_str(const json &req, const char *key) {
  const json &v = arg(req, key);
  if (!v.is_string() || v.get<std::string>().empty())
    throw std::runtime_error(std::string("'") + key + "' must be a non-empty string");
  return v;
}
inline bool arg_bool(const json &req, const char *key) {
  const json &v = arg(req, key);
  if (!v.is_boolean()) throw std::runtime_error(std::string("'") + key + "' must be true or false");
  return v;
}
inline int arg_pct(const json &req, const char *key) {
  const json &v = arg(req, key);
  if (!v.is_number() || !(v.get<double>() >= 0 && v.get<double>() <= 100))
    throw std::runtime_error(std::string("'") + key + "' must be a number from 0 to 100");
  return std::lround(v.get<double>());
}
// "RRGGBB", "#RRGGBB" or a 0xRRGGBB integer.
inline uint32_t parse_color(const json &v) {
  if (v.is_number_integer() && v.get<int64_t>() >= 0 && v.get<int64_t>() <= 0xffffff)
    return v.get<uint32_t>();
  if (v.is_string()) {
    std::string s = v;
    if (!s.empty() && s[0] == '#') s.erase(0, 1);
    if (s.size() == 6 && s.find_first_not_of("0123456789abcdefABCDEF") == std::string::npos)
      return std::stoul(s, nullptr, 16);
  }
  throw std::runtime_error("color must be \"RRGGBB\" hex");
}

// ---- sockets ----

// Binds a Unix stream socket at path, mode 660 (set through the umask while
// binding: nothing is ever chmod'ed or chowned by path, see docs/DEVIATIONS.md).
// Replaces a stale socket file (refuses if something still answers on it);
// anything at the path that is not a socket is left alone and refused.
// make_dir: create the parent (dev); as root the engine has verified the
// directory first (privs.h). gid: the new socket's group, taken from the
// effective gid while binding (root only; -1 keeps the process's own).
inline int listen_unix(const std::string &path, bool make_dir = true, gid_t gid = gid_t(-1)) {
  auto fail = [&](const std::string &what) {
    return std::runtime_error(path + ": " + what + ": " + strerror(errno));
  };
  sockaddr_un a{};
  a.sun_family = AF_UNIX;
  if (path.empty() || path.size() >= sizeof a.sun_path)
    throw std::runtime_error(path + ": socket path empty or too long");
  memcpy(a.sun_path, path.c_str(), path.size() + 1);
  std::error_code ec;
  const auto dir = std::filesystem::path(path).parent_path();
  if (make_dir && !dir.empty()) std::filesystem::create_directories(dir, ec);
  struct stat st;
  if (lstat(path.c_str(), &st) == 0) {
    if (!S_ISSOCK(st.st_mode)) throw std::runtime_error(path + ": exists and is not a socket; refusing to replace it");
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) throw fail("socket");
    if (connect(fd, (sockaddr *)&a, sizeof a) == 0) {
      close(fd);
      throw std::runtime_error(path + ": another engine is already listening here");
    }
    close(fd);
    if (unlink(path.c_str()) && errno != ENOENT) throw fail("unlink");
  } else if (errno != ENOENT) {
    throw fail("lstat");
  }
  const int fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0) throw fail("socket");
  const gid_t old_gid = getegid();
  if (gid != gid_t(-1) && setegid(gid)) {
    const auto e = fail("setegid");
    close(fd);
    throw e;
  }
  const mode_t old_umask = umask(0117);
  const int bound = bind(fd, (sockaddr *)&a, sizeof a);
  umask(old_umask);
  if (gid != gid_t(-1) && setegid(old_gid)) {
    perror("setegid");
    exit(1);  // never carry on with the wrong gid
  }
  if (bound || listen(fd, 8)) {
    const auto e = fail("bind");
    close(fd);
    throw e;
  }
  fcntl(fd, F_SETFL, O_NONBLOCK);
  fcntl(fd, F_SETFD, FD_CLOEXEC);
  return fd;
}

// Wakes a socket thread out of poll().
struct Waker {
  int r = -1, w = -1;
  Waker() {
    int p[2];
    if (pipe(p)) throw std::runtime_error(std::string("pipe: ") + strerror(errno));
    r = p[0], w = p[1];
    for (int fd : p) fcntl(fd, F_SETFL, O_NONBLOCK), fcntl(fd, F_SETFD, FD_CLOEXEC);
  }
  ~Waker() { close(r), close(w); }
  void poke() { (void)!write(w, "x", 1); }  // a full pipe already means "wake up"
  void drain() {
    char b[64];
    while (::read(r, b, sizeof b) > 0) {}
  }
};

inline int accept_client(int lfd) {
  const int fd = accept(lfd, nullptr, nullptr);
  if (fd >= 0) fcntl(fd, F_SETFL, O_NONBLOCK), fcntl(fd, F_SETFD, FD_CLOEXEC);
  return fd;
}

// Runs on the frame loop; returns the response fields. Throw to answer ok:false.
using Job = std::function<json()>;

// Control thread -> frame loop jobs, and their replies back.
struct Mailbox {
  struct Item {
    uint64_t client;
    json id;
    Job job;
  };
  std::mutex mu;
  std::condition_variable cv;
  std::deque<Item> jobs;
  std::deque<std::pair<uint64_t, std::string>> replies;
  std::function<void()> on_reply;  // set before any job is pushed

  void push(Item it) {
    {
      std::lock_guard<std::mutex> l(mu);
      jobs.push_back(std::move(it));
    }
    cv.notify_one();
  }
  // Any thread: wake the frame loop early (e.g. a stream frame arrived).
  void wake() {
    {
      std::lock_guard<std::mutex> l(mu);
      woken = true;
    }
    cv.notify_one();
  }
  bool woken = false;
  // Frame loop: sleeps until deadline, or until a job arrives or wake().
  template <class TP> void wait_until(TP deadline) {
    std::unique_lock<std::mutex> l(mu);
    cv.wait_until(l, deadline, [&] { return !jobs.empty() || woken; });
    woken = false;
  }
  // Frame loop: runs the queued jobs and queues their replies.
  void run() {
    std::deque<Item> todo;
    {
      std::lock_guard<std::mutex> l(mu);
      todo.swap(jobs);
    }
    if (todo.empty()) return;
    std::deque<std::pair<uint64_t, std::string>> out;
    for (auto &it : todo) {
      json r;
      try {
        r = it.job();
        r["ok"] = true;
      } catch (const std::exception &e) {
        r = {{"ok", false}, {"error", e.what()}};
      }
      r["id"] = it.id;
      // replace: invalid UTF-8 (a name, an error quoting input) must not throw here
      out.push_back({it.client, r.dump(-1, ' ', false, json::error_handler_t::replace) + "\n"});
    }
    {
      std::lock_guard<std::mutex> l(mu);
      for (auto &o : out) replies.push_back(std::move(o));
    }
    if (on_reply) on_reply();
  }
};

// The control socket: newline-delimited JSON requests. handler runs on this
// thread (it may do file I/O, like opening media) and returns the Job that
// applies the command on the frame loop; it throws for a bad request.
struct ControlServer {
  using Handler = std::function<Job(const json &)>;

  ControlServer(const std::string &path, Mailbox &mb, Handler h)
      : ControlServer(path, listen_unix(path), mb, std::move(h)) {}
  // lfd: listen_unix(path) done earlier (before the matrix drops root).
  ControlServer(const std::string &path, int lfd, Mailbox &mb, Handler h)
      : path(path), lfd(lfd), mb(mb), handler(std::move(h)) {
    mb.on_reply = [this] { wake.poke(); };
    th = std::thread([this] { run(); });
  }
  ~ControlServer() {
    stop = true;
    wake.poke();
    th.join();
    mb.on_reply = nullptr;
    for (auto &c : clients) close(c.second.fd);
    close(lfd);
    unlink(path.c_str());
  }

  struct Client {
    int fd;
    std::string in, out;
  };
  const std::string path;
  const int lfd;
  Mailbox &mb;
  Handler handler;
  Waker wake;
  std::atomic<bool> stop{false};
  std::map<uint64_t, Client> clients;
  uint64_t next_id = 1;
  std::thread th;

  void on_line(uint64_t cid, const std::string &line) {
    json id = nullptr;
    Job job;
    try {
      json req;
      try {
        req = json::parse(line);
      } catch (const json::parse_error &e) {
        throw std::runtime_error(std::string("bad JSON: ") + e.what());
      }
      if (req.is_object() && req.contains("id")) id = req["id"];
      job = handler(req);
    } catch (const std::exception &e) {
      job = [m = std::string(e.what())]() -> json { throw std::runtime_error(m); };
    }
    mb.push({cid, id, std::move(job)});  // errors too, so replies keep request order
  }
  // false: drop the client.
  bool on_readable(uint64_t cid, Client &c) {
    char b[4096];
    const ssize_t n = recv(c.fd, b, sizeof b, 0);
    if (n == 0) return false;
    if (n < 0) return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
    c.in.append(b, n);
    if (c.in.size() > (1 << 20)) return false;  // no newline in 1MB: not our protocol
    for (const auto &line : take_lines(c.in)) on_line(cid, line);
    return true;
  }
  bool flush(Client &c) {
    while (!c.out.empty()) {
      const ssize_t n = send(c.fd, c.out.data(), c.out.size(), 0);
      if (n < 0) return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
      c.out.erase(0, n);
    }
    return true;
  }
  void run() {
    while (!stop) {
      std::vector<pollfd> p = {{lfd, POLLIN, 0}, {wake.r, POLLIN, 0}};
      std::vector<uint64_t> ids;
      for (auto &[id, c] : clients) {
        p.push_back({c.fd, short(POLLIN | (c.out.empty() ? 0 : POLLOUT)), 0});
        ids.push_back(id);
      }
      if (poll(p.data(), p.size(), -1) < 0) continue;  // EINTR
      std::vector<uint64_t> dead;
      if (p[1].revents) {
        wake.drain();
        std::deque<std::pair<uint64_t, std::string>> r;
        {
          std::lock_guard<std::mutex> l(mb.mu);
          r.swap(mb.replies);
        }
        for (auto &[cid, line] : r) {
          auto it = clients.find(cid);
          if (it == clients.end()) continue;  // client left before its reply
          it->second.out += line;
          if (!flush(it->second) || it->second.out.size() > (1 << 20)) dead.push_back(cid);
        }
      }
      for (size_t i = 0; i < ids.size(); i++) {
        auto it = clients.find(ids[i]);
        if (it == clients.end()) continue;
        const short ev = p[i + 2].revents;
        bool alive = true;
        if (ev & (POLLIN | POLLHUP | POLLERR)) alive = on_readable(ids[i], it->second);
        if (alive && (ev & POLLOUT)) alive = flush(it->second);
        if (!alive) dead.push_back(ids[i]);
      }
      for (uint64_t cid : dead) {
        auto it = clients.find(cid);
        if (it == clients.end()) continue;
        close(it->second.fd);
        clients.erase(it);
      }
      if (p[0].revents & POLLIN)
        for (int fd; (fd = accept_client(lfd)) >= 0;) {
          if (clients.size() >= 32) close(fd);
          else clients[next_id++] = {fd, "", ""};
        }
    }
  }
};

// The preview socket: each client gets the latest published message (one
// status JSON line, then w*h*3 frame bytes). A client still sending the
// previous message skips the frames published meanwhile; one that stops
// reading just stays stuck on its message without affecting anyone else.
struct PreviewServer {
  explicit PreviewServer(const std::string &path) : PreviewServer(path, listen_unix(path)) {}
  PreviewServer(const std::string &path, int lfd) : path(path), lfd(lfd), th([this] { run(); }) {}
  ~PreviewServer() {
    stop = true;
    wake.poke();
    th.join();
    for (auto &c : clients) close(c.fd);
    close(lfd);
    unlink(path.c_str());
  }

  bool wanted() const { return nclients > 0; }
  // Frame loop: never blocks beyond a short mutex hold.
  void publish(std::string msg) {
    auto m = std::make_shared<const std::string>(std::move(msg));
    {
      std::lock_guard<std::mutex> l(mu);
      latest = std::move(m);
      seq++;
    }
    wake.poke();
  }

  struct Client {
    int fd;
    std::shared_ptr<const std::string> msg;  // being sent
    size_t off = 0;
    uint64_t seq = 0;  // of the last message taken
  };
  const std::string path;
  const int lfd;
  Waker wake;
  std::atomic<bool> stop{false};
  std::atomic<int> nclients{0};
  std::atomic<uint64_t> joined{0};  // clients accepted so far: the frame loop resends a still frame
  std::mutex mu;
  std::shared_ptr<const std::string> latest;
  uint64_t seq = 0;
  std::vector<Client> clients;
  std::thread th;  // last: starts after everything above is set up

  // false: drop the client.
  bool pump(Client &c, const std::shared_ptr<const std::string> &m, uint64_t s) {
    for (;;) {
      if (!c.msg) {
        if (s == c.seq || !m) return true;  // nothing new
        c.msg = m, c.off = 0, c.seq = s;
      }
      const ssize_t n = send(c.fd, c.msg->data() + c.off, c.msg->size() - c.off, 0);
      if (n < 0) return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
      if ((c.off += n) == c.msg->size()) c.msg.reset();
    }
  }
  void run() {
    while (!stop) {
      std::vector<pollfd> p = {{lfd, POLLIN, 0}, {wake.r, POLLIN, 0}};
      for (auto &c : clients) p.push_back({c.fd, short(POLLIN | (c.msg ? POLLOUT : 0)), 0});
      if (poll(p.data(), p.size(), -1) < 0) continue;  // EINTR
      if (p[1].revents) wake.drain();
      std::shared_ptr<const std::string> m;
      uint64_t s;
      {
        std::lock_guard<std::mutex> l(mu);
        m = latest, s = seq;
      }
      std::vector<Client> keep;
      for (size_t i = 0; i < clients.size(); i++) {
        Client &c = clients[i];
        bool alive = true;
        if (p[i + 2].revents & (POLLIN | POLLHUP | POLLERR)) {  // clients don't talk: EOF or junk
          char b[256];
          const ssize_t n = recv(c.fd, b, sizeof b, 0);
          alive = n > 0 || (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR));
        }
        if (alive && (!c.msg || p[i + 2].revents & POLLOUT)) alive = pump(c, m, s);
        if (alive) keep.push_back(std::move(c));
        else close(c.fd);
      }
      clients.swap(keep);
      if (p[0].revents & POLLIN)
        for (int fd; (fd = accept_client(lfd)) >= 0;) {
          if (clients.size() >= 8) close(fd);
          else clients.push_back({fd, nullptr, 0, s}), joined++;  // starts with the next frame
        }
      nclients = clients.size();
    }
  }
};
