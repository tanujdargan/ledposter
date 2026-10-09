#!/usr/bin/env python3
"""Software update with rollback (docs/spec.md "Reliability"). Stdlib only: ledposter-update.service
runs it with the system python3 as root, from /data/app/current.

    python3 updater.py run                      what the unit runs: finish a pending switch, or serve the request file
    python3 updater.py request update TAG       write the request (the web app does this through system.py)
    python3 updater.py request rollback
    python3 updater.py status | tags

Layout (docs/ARCHITECTURE.md). $LEDPOSTER_DATA/app is root-owned; the web app (user `ledwall`)
can read and run the current release but not change it:
  releases/<tag>/     a git checkout of that tag with the built engine and its venv; `.ready` inside
                      marks a finished build (a dir without it is garbage and gets rebuilt)
  current, previous   symlinks to releases/<tag>; `current` is what the services run
  update.json         progress for the UI, rewritten at every step
  pending.json        {"new", "old", "previous", "action"} from just before a switch until the health
                      check passed, so a power cut mid-switch is finished (or rolled back) at boot
  build/              home and pip cache of the `ledwall-build` user that compiles and pip-installs
  origin, deploy_key, known_hosts, token   how to reach the private repo (README: Software updates)
$LEDPOSTER_DATA/update-request.json is the one input the web app writes: {"action": "update",
"target": "m9"} or {"action": "rollback"}. It is read with O_NOFOLLOW and must be a regular file
owned by the service user; the target must match TAG_RE and be a tag the repository actually has.

Who runs what: git clone runs as root (the repo is reached with the deploy key, the tag is
checked against `git ls-remote` first); make, venv, pip and the tests run as the unprivileged
`ledwall-build` user in the release dir, which root then chowns to itself and marks `.ready`.
So root never executes anything the `ledwall` user could have written (docs/DEVIATIONS.md).

Power-cut safety: every file above is written tmp + fsync + rename; symlinks are replaced by
renaming a temp symlink; a half-built release is never `current`; `run` at boot resumes whatever
was in flight. Failure in fetch/build/test never touches `current`.
"""
import atexit
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

HEALTH_TIMEOUT_S = 90
SERVICE_USER = "ledwall"
BUILD_USER = "ledwall-build"
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REQUEST_FILE = "update-request.json"
MAX_INPUT_BYTES = 65536
STEPS = {"update": ["fetching", "building", "testing", "switching", "restarting", "checking"],
         "rollback": ["switching", "restarting", "checking"]}
RESUMABLE = ("fetching", "building", "testing")
SERVICES = ["ledposter-engine.service", "ledposter-web.service"]


class StepError(Exception):
    pass


def write_atomic(path, text, mode=0o644):
    """tmp + fsync + rename + fsync dir, so a power cut leaves the old file or the new one."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    fsync_dir(d)


def fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class InputError(Exception):
    """A file the web app wrote is not what root may read (symlink, not a regular file, wrong owner, too big)."""


def input_owners():
    """Who may have written a request file: the service user, root, and whoever runs this (tests)."""
    uids = {0, os.getuid()}
    try:
        uids.add(pwd.getpwnam(SERVICE_USER).pw_uid)
    except KeyError:
        pass
    return uids


def read_input(path, limit=MAX_INPUT_BYTES):
    """Reads a file the unprivileged web app wrote, as root, without following anything it could
    have planted: O_NOFOLLOW, then the open file must be a regular file owned by the service user
    (or root), at most `limit` bytes. FileNotFoundError when it is absent, InputError when refused.
    system/bin/ledposter-sys has the same check for its inputs."""
    fd = None
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError as e:
            if e.errno == 2:  # ENOENT
                raise FileNotFoundError(path)
            raise InputError(f"{path}: {e.strerror} (a symlink?)")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise InputError(f"{path}: not a regular file")
        if st.st_uid not in input_owners():
            raise InputError(f"{path}: owned by uid {st.st_uid}, not the service user")
        if st.st_size > limit:
            raise InputError(f"{path}: {st.st_size} bytes is too big")
        data = os.read(fd, limit + 1)
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > limit or b"\0" in data:
        raise InputError(f"{path}: too big or not text")
    return data.decode("utf-8", "replace")


def chown_tree(path, user):
    """chown -R without following symlinks; a no-op when not root (dev boxes, tests)."""
    if os.geteuid() != 0:
        return
    pw = pwd.getpwnam(user)
    os.lchown(path, pw.pw_uid, pw.pw_gid)
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            os.lchown(os.path.join(root, n), pw.pw_uid, pw.pw_gid)


def config_port(data_dir):
    """web.port from config.yaml without PyYAML (the file is written by yaml.safe_dump)."""
    try:
        with open(os.path.join(data_dir, "config.yaml")) as f:
            m = re.search(r"^web:\n(?:[ \t]+.*\n)*?[ \t]+port:[ \t]*(\d+)", f.read(), re.M)
            return int(m.group(1)) if m else 80
    except OSError:
        return 80


# -- the repo ------------------------------------------------------------------------

def origin_url(app_dir):
    try:
        with open(os.path.join(app_dir, "origin")) as f:
            url = f.read().strip()
        if url:
            return url
    except OSError:
        pass
    r = subprocess.run(["git", "-C", os.path.join(app_dir, "current"), "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "git@github.com:tanujdargan/ledposter.git"


def private_key_copy(key):
    """The deploy key is root:ledwall 0640 so the web app can check for updates. ssh refuses
    a group-readable key when it runs as the key's owner (root, the updater), so root gets a
    0600 copy in a private temp dir, removed at exit."""
    st = os.stat(key)
    if st.st_uid != os.geteuid() or not st.st_mode & 0o077:
        return key
    d = tempfile.mkdtemp(prefix="ledposter-key-")  # 0700
    atexit.register(shutil.rmtree, d, True)
    copy = os.path.join(d, "deploy_key")
    fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(key, "rb") as src:
        out.write(src.read())
    return copy


def git_env(app_dir):
    """Deploy key (recommended) via GIT_SSH_COMMAND; never prompts."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", HOME=app_dir)
    key = os.path.join(app_dir, "deploy_key")
    if os.path.exists(key):
        key = private_key_copy(key)
        # known_hosts is seeded by install.sh (ssh-keyscan github.com) and root-owned; the web app's
        # `git ls-remote` can read it but not add to it (ssh warns and carries on).
        env["GIT_SSH_COMMAND"] = (f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes "
                                  f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={app_dir}/known_hosts")
    return env


def fetch_url(app_dir):
    """The clone URL, with a fine-grained token folded in when that is what is configured."""
    url = origin_url(app_dir)
    try:
        with open(os.path.join(app_dir, "token")) as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if token and not os.path.exists(os.path.join(app_dir, "deploy_key")):
        m = re.match(r"^(?:git@github\.com:|https://github\.com/)(.+?)(?:\.git)?$", url)
        if m:
            return f"https://x-access-token:{token}@github.com/{m.group(1)}.git"
    return url


def git_run(argv, env=None, timeout=30):
    """(rc, stdout, stderr); the injectable runner for list_tags."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out"
    return r.returncode, r.stdout, r.stderr


def list_tags(app_dir, run=git_run):
    rc, out, err = run(["git", "ls-remote", "--tags", "--refs", fetch_url(app_dir)], env=git_env(app_dir), timeout=30)
    if rc != 0:
        raise StepError(((err or out).strip().splitlines() or ["git ls-remote failed"])[-1])
    return [line.split("refs/tags/", 1)[1] for line in out.splitlines() if "refs/tags/" in line]


# -- the state machine -----------------------------------------------------------------

class Updater:
    def __init__(self, data_dir, run=None, health=None, sleep=time.sleep, clock=time.time, out=None):
        self.data = data_dir
        self.app = os.path.join(data_dir, "app")
        self.releases = os.path.join(self.app, "releases")
        self.build_home = os.path.join(self.app, "build")
        self.log_path = os.path.join(data_dir, "logs", "update.log")
        self.request_path = os.path.join(data_dir, REQUEST_FILE)
        self._run = run or self._run_subprocess
        self._health = health or self._health_http
        self.sleep, self.clock, self.out = sleep, clock, out or sys.stderr

    # -- files --

    def _p(self, name):
        return os.path.join(self.app, name)

    def status(self):
        return read_json(self._p("update.json")) or {"state": "idle"}

    def set_status(self, **kw):
        st = self.status()
        st.update(kw, updated=int(self.clock()))
        if st.get("state") in STEPS.get(st.get("action") or "", []):
            steps = STEPS[st["action"]]
            st["step"], st["total"] = steps.index(st["state"]) + 1, len(steps)
        write_atomic(self._p("update.json"), json.dumps(st))
        self.log(f"{st['state']}: {st.get('message', '')}")
        return st

    def request(self):
        """The web app's request, or None when there is none. InputError if the file is not one
        root may read (the caller reports it and removes the file)."""
        try:
            text = read_input(self.request_path)
        except FileNotFoundError:
            return None
        try:
            req = json.loads(text)
        except ValueError:
            raise InputError(f"{self.request_path}: not JSON")
        if not isinstance(req, dict):
            raise InputError(f"{self.request_path}: not a JSON object")
        return req

    def write_request(self, req):
        write_atomic(self.request_path, json.dumps(req))

    def pending(self):
        return read_json(self._p("pending.json"))

    def _clear(self, name):
        p = self.request_path if name == REQUEST_FILE else self._p(name)
        try:
            os.unlink(p)
        except OSError:
            pass
        fsync_dir(os.path.dirname(p))

    def link(self, name):
        try:
            return os.path.basename(os.readlink(self._p(name)))
        except OSError:
            return None

    def set_link(self, name, target):
        tmp = self._p(f".{name}.tmp")
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(os.path.join("releases", target), tmp)
        os.replace(tmp, self._p(name))
        fsync_dir(self.app)

    def ready(self, tag):
        return bool(tag) and os.path.exists(os.path.join(self.releases, tag, ".ready"))

    def log(self, line):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(f"{stamp} {line.rstrip()}\n")
        except OSError:
            pass
        print(line, file=self.out, flush=True)

    # -- commands --

    def _run_subprocess(self, cmd, cwd=None, env=None, timeout=1800, user=None):
        """Returns (rc, output). Output is appended to the log. `user` runs the command as that
        account (supplementary groups dropped) when we are root; otherwise as ourselves."""
        as_user = {}
        if user and os.geteuid() == 0:
            try:
                pw = pwd.getpwnam(user)
            except KeyError:
                return 1, f"user {user} does not exist (run system/install.sh)"
            as_user = {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}
        try:
            r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, **as_user)
            out = (r.stdout or "") + (r.stderr or "")
            rc = r.returncode
        except FileNotFoundError as e:
            rc, out = 127, str(e)
        except subprocess.TimeoutExpired:
            rc, out = 124, f"timed out after {timeout}s"
        self.log(f"$ {'(' + user + ') ' if user else ''}{' '.join(cmd)}\n{out.rstrip()}")
        return rc, out

    def cmd(self, cmd, cwd=None, env=None, what=None, timeout=1800, user=None):
        rc, out = self._run(cmd, cwd=cwd, env=env, timeout=timeout, **({"user": user} if user else {}))
        if rc != 0:
            tail = " | ".join(line.strip() for line in out.strip().splitlines()[-3:]) or f"exit {rc}"
            raise StepError(f"{what or cmd[0]} failed: {tail}")
        return out

    def _health_http(self, tag):
        """(healthy, why). The web app's /api/health says whether the engine answers and which release it runs."""
        url = f"http://127.0.0.1:{config_port(self.data)}/api/health"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                h = json.loads(r.read())
        except Exception as e:  # connection refused, 5xx, bad JSON: all "not yet"
            return False, f"web app not answering ({e.__class__.__name__})"
        if h.get("release") != tag:
            return False, f"web app still reports release {h.get('release')}"
        if not h.get("engine"):
            return False, "engine not answering"
        return True, ""

    def wait_healthy(self, tag, timeout=HEALTH_TIMEOUT_S):
        t0, why = self.clock(), "no answer"
        while self.clock() - t0 < timeout:
            ok, why = self._health(tag)
            if ok:
                return True, ""
            self.sleep(2)
        return False, why

    def restart_services(self):
        self.cmd(["systemctl", "restart", *SERVICES], what="systemctl restart", timeout=120)

    # -- steps --

    def remote_tags(self):
        """The repository's tags; the only names an update request may ask for."""
        out = self.cmd(["git", "ls-remote", "--tags", "--refs", fetch_url(self.app)], env=git_env(self.app),
                       what="git ls-remote", timeout=60)
        return {line.split("refs/tags/", 1)[1].strip() for line in out.splitlines() if "refs/tags/" in line}

    def fetch(self, tag, rel):
        """As root: the remote is ours (deploy key), the tag was checked against remote_tags()."""
        env = git_env(self.app)
        os.makedirs(self.releases, exist_ok=True)
        self.cmd(["git", "clone", "--quiet", "--depth", "1", "--branch", tag, fetch_url(self.app), rel],
                 env=env, what="git clone", timeout=600)
        if fetch_url(self.app) != origin_url(self.app):  # a token: keep it out of the checkout
            self.cmd(["git", "-C", rel, "remote", "set-url", "origin", origin_url(self.app)], env=env)
        rc, _ = self._run(["git", "-C", rel, "submodule", "update", "--init", "--depth", "1"], env=env, timeout=600)
        if rc != 0:  # the server did not allow a shallow fetch of the pinned commit
            self.cmd(["git", "-C", rel, "submodule", "update", "--init"], env=env, what="git submodule", timeout=900)

    def build_env(self):
        """HOME and the pip cache of the build user, under the root-owned app dir."""
        if os.geteuid() == 0:
            try:
                pw = pwd.getpwnam(BUILD_USER)
            except KeyError:
                raise StepError(f"build user {BUILD_USER} does not exist (run system/install.sh)")
            os.makedirs(self.build_home, mode=0o700, exist_ok=True)
            os.chown(self.build_home, pw.pw_uid, pw.pw_gid)
        return {"HOME": self.build_home, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "PIP_CACHE_DIR": os.path.join(self.build_home, "pip-cache"), "LANG": "C.UTF-8"}

    def build(self, rel):
        """make, venv and pip as the build user: pip runs setup code from PyPI, make runs the
        checkout's Makefiles; neither as root. The checkout is handed over first."""
        env = self.build_env()
        chown_tree(rel, BUILD_USER)
        self.cmd(["make", "-C", os.path.join(rel, "engine"), "-j2"], env=env, what="engine build", timeout=3600, user=BUILD_USER)
        venv = os.path.join(rel, "web", ".venv")
        self.cmd(["python3", "-m", "venv", "--system-site-packages", venv], env=env, what="venv", user=BUILD_USER)
        self.cmd([os.path.join(venv, "bin", "pip"), "install", "--quiet", "--retries", "5", "--timeout", "60",
                  "-r", os.path.join(rel, "web", "requirements.txt")], env=env, what="pip install", timeout=1800, user=BUILD_USER)

    def test(self, rel):
        env = self.build_env()
        self.cmd(["make", "-C", os.path.join(rel, "engine"), "test"], env=env, what="engine tests", timeout=1800, user=BUILD_USER)
        self.cmd([os.path.join(rel, "web", ".venv", "bin", "python"), "-c", "import ledposter.app"],
                 cwd=os.path.join(rel, "web"), env=env, what="web app import check", user=BUILD_USER)

    # -- actions --

    def run_once(self):
        """What the unit does: one pass, returns the final state name."""
        pend = self.pending()
        if pend:
            self.log(f"resuming: {pend['new']} switched but not yet checked")
            return self.finish(pend)
        try:
            req = self.request()
        except InputError as e:
            self._clear(REQUEST_FILE)
            self.set_status(state="failed", error="bad request", message=f"Refused the update request: {e}")
            return "failed"
        if not req:
            return "idle"
        if req.get("action") == "rollback":
            return self.rollback()
        if req.get("action") == "update" and isinstance(req.get("target"), str) and TAG_RE.match(req["target"]):
            return self.update(req["target"])
        self._clear(REQUEST_FILE)
        self.set_status(state="failed", error="bad request", message=f"Bad update request: {req}")
        return "failed"

    def update(self, tag):
        st = self.status()
        if st.get("state") in RESUMABLE and st.get("target") == tag:
            self.log(f"resuming interrupted update to {tag}")
        rel = os.path.join(self.releases, tag)
        self.set_status(state="fetching", action="update", target=tag, from_release=self.link("current"),
                        started=int(self.clock()), error=None, message=f"Fetching {tag}")
        try:
            if not self.ready(tag):
                shutil.rmtree(rel, ignore_errors=True)
                if tag not in self.remote_tags():
                    raise StepError(f"{tag} is not a release tag on the repository")
                self.fetch(tag, rel)
                self.set_status(state="building", message="Building the engine and installing Python packages (minutes on a Pi 3B)")
                self.build(rel)
                self.set_status(state="testing", message="Running the engine tests and an import check")
                self.test(rel)
                chown_tree(rel, "root")  # from here on only root can change the release
                write_atomic(os.path.join(rel, ".ready"), f"{int(self.clock())}\n")
            elif tag == self.link("current"):
                self._clear(REQUEST_FILE)
                self.set_status(state="done", message=f"Already running {tag}")
                return "done"
        except StepError as e:
            shutil.rmtree(rel, ignore_errors=True)
            self._clear(REQUEST_FILE)
            self.set_status(state="failed", error=str(e), message=f"Update to {tag} failed; still running {self.link('current')}")
            return "failed"
        return self.switch_to(tag, "update")

    def rollback(self):
        prev = self.link("previous")
        if not self.ready(prev):
            self._clear(REQUEST_FILE)
            self.set_status(state="failed", action="rollback", error="no previous release",
                            message="No previous release to roll back to")
            return "failed"
        return self.switch_to(prev, "rollback", target=prev, from_release=self.link("current"),
                              started=int(self.clock()), error=None)

    def switch_to(self, tag, action, **fields):
        old = self.link("current")
        pend = {"new": tag, "old": old, "previous": self.link("previous"), "action": action}
        self.set_status(state="switching", action=action, message=f"Switching to {tag}", **fields)
        write_atomic(self._p("pending.json"), json.dumps(pend))  # before the switch: resumable
        self.set_link("current", tag)
        self.set_status(state="restarting", message="Restarting the engine and the web app")
        try:
            self.restart_services()
        except StepError as e:
            self.log(str(e))  # the health check decides
        return self.finish(pend)

    def finish(self, pend):
        new, old = pend["new"], pend.get("old")
        self.set_status(state="checking", action=pend.get("action", "update"), target=new,
                        message="Waiting for the engine and the web app to answer")
        ok, why = self.wait_healthy(new)
        if ok:
            if old and old != new:
                self.set_link("previous", old)  # for an update: where to roll back to; for a rollback: the way forward
            self._clear("pending.json")
            self._clear(REQUEST_FILE)
            self.set_status(state="done", error=None, message=f"Running {new}")
            self.prune()
            return "done"
        self.log(f"{new} is not healthy: {why}")
        if old and old != new and self.ready(old):
            self.set_link("current", old)
            if pend.get("previous") and pend["previous"] != old:
                self.set_link("previous", pend["previous"])  # untouched by the failed attempt
            self._clear("pending.json")
            try:
                self.restart_services()
            except StepError as e:
                self.log(str(e))
            back, _ = self.wait_healthy(old)
            self._clear(REQUEST_FILE)
            self.set_status(state="rolled_back", error=why, target=new,
                            message=f"{new} failed its health check ({why}); back on {old}"
                                    + ("" if back else ", which is not answering either"))
            return "rolled_back"
        self._clear("pending.json")
        self._clear(REQUEST_FILE)
        self.set_status(state="failed", error=why, message=f"{new} is not healthy ({why}) and there is nothing to roll back to")
        return "failed"

    def prune(self):
        """Keep current and previous; everything else under releases/ is deleted."""
        keep = {self.link("current"), self.link("previous")}
        try:
            names = os.listdir(self.releases)
        except OSError:
            return
        for n in names:
            if n not in keep:
                shutil.rmtree(os.path.join(self.releases, n), ignore_errors=True)
                self.log(f"pruned release {n}")


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    data = os.environ.get("LEDPOSTER_DATA") or "/data"
    u = Updater(data)
    if a[:1] == ["run"]:
        print(u.run_once())
    elif a[:1] == ["status"]:
        print(json.dumps(u.status(), indent=1))
    elif a[:2] == ["request", "rollback"]:
        u.write_request({"action": "rollback"})
    elif a[:2] == ["request", "update"] and len(a) == 3:
        u.write_request({"action": "update", "target": a[2]})
    elif a[:1] == ["tags"]:
        print("\n".join(list_tags(u.app)))
    else:
        sys.exit(__doc__.split("\n\n")[1])


if __name__ == "__main__":
    main()
