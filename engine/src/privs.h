// What the engine decides while it is still root: where its sockets go and whom
// it drops to. /data/config.yaml belongs to the ledposter user, whose compromise
// must not reach root (docs/DEVIATIONS.md), so as root these come from the CLI
// (the root-owned systemd unit) or the built-in defaults, never from the config;
// the config keys only count when the engine is not root (dev, make sim).
// The checks here are pure (unit tested); open_socket_dir does the syscalls.
#pragma once
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

struct PrivSettings {
  std::string socket = "/run/ledposter/engine.sock";
  std::string preview_socket = "/run/ledposter/frames.sock";
  std::string user = "daemon", group = "daemon";  // what the matrix library drops root to
};

// Path split on '/', refusing anything a root process should not walk: relative
// paths and "", "." or ".." components (no normalisation, no symlink games).
inline std::vector<std::string> plain_parts(const std::string &path) {
  if (path.empty() || path[0] != '/') throw std::runtime_error(path + ": socket path must be absolute");
  std::vector<std::string> parts;
  for (size_t i = 1, j; i <= path.size(); i = j + 1) {
    j = path.find('/', i);
    if (j == std::string::npos) j = path.size();
    const std::string p = path.substr(i, j - i);
    if (p.empty() || p == "." || p == "..")
      throw std::runtime_error(path + ": socket path must not contain empty, '.' or '..' parts");
    parts.push_back(p);
  }
  return parts;
}

// As root, both sockets must be plain names in one directory (not "/"), which
// the engine then creates or verifies (open_socket_dir). Returns that directory.
inline std::string socket_dir(const std::string &sock, const std::string &psock) {
  const auto a = plain_parts(sock), b = plain_parts(psock);
  if (a.size() < 2 || b.size() < 2)
    throw std::runtime_error("socket paths must be inside a directory, not directly under /");
  if (a.back() == b.back() || std::vector<std::string>(a.begin(), a.end() - 1) !=
                                   std::vector<std::string>(b.begin(), b.end() - 1))
    throw std::runtime_error(sock + " and " + psock + ": the two sockets must be different names in the same directory");
  return sock.substr(0, sock.rfind('/'));
}

// As root, the account dropped to is never root: not by name, not by id.
inline void check_drop_target(const std::string &user, uid_t uid, const std::string &group, gid_t gid) {
  if (user.empty() || user == "root" || uid == 0)
    throw std::runtime_error("--user " + user + ": the engine must drop to a non-root user");
  if (group.empty() || group == "root" || gid == 0)
    throw std::runtime_error("--group " + group + ": the engine must drop to a non-root group");
}

// Opens the socket directory for the root-time socket setup and returns its fd.
// Missing: made now, mode 0770, owner:gid (the dropped-to group can connect and,
// after the drop, remove the sockets). Present: must be a real directory (not a
// symlink: O_NOFOLLOW), owned by `owner` and not world-writable, like the unit's
// RuntimeDirectory. owner is the caller's uid (root), a parameter so tests can run it.
inline int open_socket_dir(const std::string &dir, uid_t owner, gid_t gid) {
  auto fail = [&](const char *what) {
    return std::runtime_error(dir + ": " + what + ": " + strerror(errno));
  };
  const bool made = mkdir(dir.c_str(), 0770) == 0;
  if (!made && errno != EEXIST) throw fail("mkdir");
  const int fd = open(dir.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) throw errno == ELOOP || errno == ENOTDIR
                  ? std::runtime_error(dir + ": not a directory (a symlink?); refusing to put sockets there")
                  : fail("open");
  struct stat st;
  if (fstat(fd, &st)) {
    close(fd);
    throw fail("fstat");
  }
  if (made) {
    if (fchown(fd, owner, gid) || fchmod(fd, 0770)) {  // on the fd we hold, never by path
      const auto e = fail("chown");
      close(fd);
      throw e;
    }
  } else if (st.st_uid != owner || (st.st_mode & S_IWOTH)) {
    close(fd);
    throw std::runtime_error(dir + ": must be owned by uid " + std::to_string(owner) +
                             " and not world-writable; refusing to put sockets there");
  }
  return fd;
}
