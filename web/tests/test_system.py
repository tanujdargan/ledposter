"""Milestone 8: firewall rule generation, SSH key parsing, release tag ordering, the factory reset
plan, last-mode restore, the updater state machine (with a fake runner: success, failed health
check -> rollback, failed build, rollback, a power cut in every status: a build is dropped, a
switch is finished; the build's memory guard and limits), atomic media cache writes, the
install-time config, and /api/system through the app."""
import asyncio
import json
import os
import stat
import subprocess
import sys
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import PASSWORD, wait_status
from ledposter import config, system, updater
from ledposter.app import create_app

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDQ6Z8ZQ7u9w3mT2Hn5QxqV0lR9ZkD5z1y8o7PeXYgAb me@laptop"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7 other"


# -- pure helpers ---------------------------------------------------------------------

def test_firewall_rules_follow_the_spec():
    base = system.firewall_rules()
    assert base[:3] == ["default deny incoming", "default allow outgoing", "logging off"]
    assert "allow 80/tcp" in base and "allow 5353/udp" in base
    assert not any(p in r for r in base for p in ("4048", "5568", "22/tcp"))  # DDP/E1.31 and SSH off by default
    # the hotspot's DHCP and DNS, on the Wi-Fi interface only (MQTT is outbound: default allow)
    assert "allow in on wlan0 to any port 67 proto udp" in base
    assert "allow in on wlan0 to any port 53 proto udp" in base and "allow in on wlan0 to any port 53 proto tcp" in base
    full = system.firewall_rules(http_port=8080, ssh=True, ddp=True, hotspot_iface="wlp1s0")
    assert "allow 8080/tcp" in full and "allow 22/tcp" in full and "allow 4048/udp" in full and "allow 5568/udp" in full
    assert "allow in on wlp1s0 to any port 67 proto udp" in full and "allow 80/tcp" not in full
    only_ddp = system.firewall_rules(ddp=True, e131=False)
    assert "allow 4048/udp" in only_ddp and "allow 5568/udp" not in only_ddp
    assert "allow 5568/udp" in system.firewall_rules(ddp=False, e131=True)
    assert all(system.RULE_RE.match(r) for r in base + full)  # what ledposter-sys accepts
    for bad in ("allow 22/tcp; rm -rf /", "allow from any", "delete allow 80/tcp", "allow in on wlan0 to any port 53 proto icmp"):
        assert not system.RULE_RE.match(bad), bad


def test_authorized_keys_parsing():
    text = f"# laptop\n{ED}\n\n  {RSA}  \n"
    assert system.parse_authorized_keys(text) == [ED, RSA]
    assert system.parse_authorized_keys("") == [] and system.parse_authorized_keys(None) == []
    sk = "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIGsHL yubikey"
    assert system.parse_authorized_keys(sk) == [sk]
    with pytest.raises(ValueError, match="Line 2"):
        system.parse_authorized_keys(f"{ED}\nssh-ed25519 not base64!! x")
    with pytest.raises(ValueError, match="Line 1"):
        system.parse_authorized_keys("command=\"rm -rf\" " + ED)  # options are refused: keys only


def test_release_tag_ordering():
    tags = ["m8", "m9", "m10", "v1.0.0", "v0.9", "m7", "junk", "m8-rc1"]
    assert system.newer_tags("m8", tags) == ["m9", "m10", "v0.9", "v1.0.0"]
    assert system.newer_tags("m8-3-gabc1234", tags) == ["m9", "m10", "v0.9", "v1.0.0"]  # a dev build of m8
    assert system.newer_tags("v1.0.0", tags) == []
    assert system.newer_tags(None, ["m1"]) == ["m1"]
    assert system.tag_key("v1.10") > system.tag_key("v1.9") > system.tag_key("m99")


def test_factory_reset_plan_keeps_card_password_and_releases(tmp_path):
    for n in ("config.yaml", "secret", "setup-password", "net-rollback", "firewall.rules"):
        (tmp_path / n).write_text("x")
    for d in ("media", "logs", "app", "ssh", "state", "lost+found"):
        (tmp_path / d).mkdir()
    plan = system.factory_reset_plan(str(tmp_path))
    assert plan["keep"] == ["app", "lost+found", "setup-password", "state"]
    assert plan["delete"] == ["config.yaml", "firewall.rules", "logs", "media", "net-rollback", "secret", "ssh"]
    assert system.factory_reset_plan(str(tmp_path / "nope")) == {"delete": [], "keep": []}
    assert system.maintenance_conf({"enabled": False, "day": 2, "hour": 23}) == "ENABLED=0\nDAY=2\nHOUR=23\n"


def test_last_mode_restore_once_per_engine_start():
    sent, fail = [], [False]
    last = {"mode": "effect", "effect": "plasma"}
    ready = {"abc"}

    async def send(cmd, **args):
        sent.append((cmd, args))
        return not fail[0]
    lm = system.LastMode(lambda: last, send, lambda mid: mid in ready)
    run = lambda status, now: asyncio.run(lm.tick(status, now))
    assert run(None, 1000) is False and run({"mode": "effect", "uptime_s": 5}, 1000) is False
    assert run({"mode": "boot", "uptime_s": 3}, 1000) is True and sent == [("set_mode", last)]
    assert run({"mode": "boot", "uptime_s": 5}, 1002) is False and len(sent) == 1  # same engine start: not again
    assert run({"mode": "boot", "uptime_s": 2}, 1500) is True and len(sent) == 2   # the engine restarted
    fail[0] = True
    assert run({"mode": "boot", "uptime_s": 1}, 2000) is False and len(sent) == 3  # tried once, failed...
    assert run({"mode": "boot", "uptime_s": 3}, 2002) is False and len(sent) == 3  # ...not hammered
    fail[0] = False
    last.clear()
    last.update({"mode": "media", "media": "zzz"})
    assert run({"mode": "boot", "uptime_s": 1}, 3000) is False and len(sent) == 3  # item gone: stay on boot
    last["media"] = "abc"
    assert run({"mode": "boot", "uptime_s": 1}, 4000) is True and sent[-1] == ("set_mode", {"mode": "media", "media": "abc"})
    for bad in ({}, {"mode": "boot"}, None):
        last.clear()
        if bad:
            last.update(bad)
        assert run({"mode": "boot", "uptime_s": 1}, 5000 + len(sent)) is False or bad


# -- the updater state machine ----------------------------------------------------------

class FakeWorld:
    """Stands in for git/make/pip/systemctl and the health check. Each command creates what the
    real one would; `fail` makes a step fail, `healthy` scripts the health check per release."""

    def __init__(self, data):
        self.data, self.app = data, os.path.join(data, "app")
        self.calls, self.fail, self.healthy = [], set(), {}
        self.users = {}  # command name -> the user it was run as (None: root/ourselves)
        self.envs = {}  # command name -> its environment
        self.tags = ["m7", "m8", "m9", "m10"]  # what the repository has
        self.t = [1_000_000.0]

    def run(self, cmd, cwd=None, env=None, timeout=None, user=None):
        self.calls.append(cmd)
        what = cmd[0]
        self.users[os.path.basename(what) if what != "git" else " ".join(cmd[:2])] = user
        self.envs[os.path.basename(what) if what != "git" else " ".join(cmd[:2])] = env
        if what == "git" and cmd[1] == "ls-remote":
            if "ls-remote" in self.fail:
                return 128, "fatal: Could not read from remote repository."
            return 0, "".join(f"{i:040x}\trefs/tags/{t}\n" for i, t in enumerate(self.tags))
        if what == "git" and cmd[1] == "clone":
            if "clone" in self.fail:
                return 128, "fatal: Could not read from remote repository."
            rel = cmd[-1]
            os.makedirs(os.path.join(rel, "engine"))
            os.makedirs(os.path.join(rel, "web"))
            open(os.path.join(rel, "web", "requirements.txt"), "w").close()
            return 0, ""
        if what == "git":
            return 0, ""
        if what == "make" and "test" in cmd:
            return (1, "Assertion failed: limiter") if "test" in self.fail else (0, "all engine tests passed")
        if what == "make":
            if "build" in self.fail:
                return 2, "g++: error: boom"
            open(os.path.join(cmd[2], "ledengine"), "w").close()
            return 0, ""
        if what == "python3" and cmd[1:3] == ["-m", "venv"]:
            os.makedirs(os.path.join(cmd[-1], "bin"), exist_ok=True)
            open(os.path.join(cmd[-1], "bin", "python"), "w").close()
            open(os.path.join(cmd[-1], "bin", "pip"), "w").close()
            return 0, ""
        if what.endswith("/pip"):
            return (1, "No matching distribution") if "pip" in self.fail else (0, "")
        if what.endswith("/python"):
            return (1, "ModuleNotFoundError") if "import" in self.fail else (0, "")
        if what == "systemctl":
            return (1, "Access denied") if "systemctl" in self.fail else (0, "")
        raise AssertionError(f"unexpected command {cmd}")

    def health(self, tag):
        ok = self.healthy.get(tag, True)
        return (True, "") if ok else (False, "engine not answering")

    def sleep(self, s):
        self.t[0] += s

    def clock(self):
        return self.t[0]


MEMINFO = """MemTotal:         933164 kB
MemFree:           61224 kB
MemAvailable:     {avail} kB
Buffers:           20480 kB
Cached:           212992 kB
SwapCached:            0 kB
SwapTotal:        {swap} kB
SwapFree:         {swap} kB
"""


def make_updater(tmp_path, releases=("m7", "m8"), current="m8", previous="m7", avail_kb=600 * 1024, swap_kb=0,
                 compiler=True):
    data = str(tmp_path)
    w = FakeWorld(data)
    for r in releases:
        os.makedirs(os.path.join(w.app, "releases", r, "engine"))
        open(os.path.join(w.app, "releases", r, ".ready"), "w").close()
    (tmp_path / "app" / "origin").write_text("git@github.com:tanujdargan/ledposter.git\n")
    u = updater.Updater(data, run=w.run, health=w.health, sleep=w.sleep, clock=w.clock, out=open(os.devnull, "w"),
                        meminfo=lambda: MEMINFO.format(avail=avail_kb, swap=swap_kb),
                        which=lambda c: f"/usr/bin/{c}" if compiler else None)
    if current:
        u.set_link("current", current)
    if previous:
        u.set_link("previous", previous)
    return u, w


def states_logged(tmp_path):
    return [line.split(" ", 2)[2].split(":")[0] for line in (tmp_path / "logs" / "update.log").read_text().splitlines()
            if len(line.split(" ", 2)) > 2 and line.split(" ", 2)[2].split(":")[0] in
            ("fetching", "building", "testing", "switching", "restarting", "checking", "done", "failed", "rolled_back")]


def test_update_success_switches_and_prunes(tmp_path):
    u, w = make_updater(tmp_path)
    assert u.run_once() == "idle"
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "done"
    assert states_logged(tmp_path) == ["fetching", "building", "testing", "switching", "restarting", "checking", "done"]
    st = u.status()
    assert st["state"] == "done" and st["target"] == "m9" and st["from_release"] == "m8" and st["error"] is None
    assert u.link("current") == "m9" and u.link("previous") == "m8"
    assert os.path.exists(tmp_path / "app" / "releases" / "m9" / ".ready")
    assert not os.path.exists(tmp_path / "app" / "pending.json") and not os.path.exists(tmp_path / "update-request.json")
    assert sorted(os.listdir(tmp_path / "app" / "releases")) == ["m8", "m9"]  # m7 pruned
    assert [c for c in w.calls if c[0] == "systemctl"] == [["systemctl", "restart", "ledposter-engine.service", "ledposter-web.service"]]
    assert ["git", "clone", "--quiet", "--depth", "1", "--branch", "m9", "git@github.com:tanujdargan/ledposter.git",
            str(tmp_path / "app" / "releases" / "m9")] in w.calls
    assert w.calls.index(["git", "ls-remote", "--tags", "--refs", "git@github.com:tanujdargan/ledposter.git"]) < \
        w.calls.index(["git", "clone", "--quiet", "--depth", "1", "--branch", "m9", "git@github.com:tanujdargan/ledposter.git",
                       str(tmp_path / "app" / "releases" / "m9")])  # the tag was checked before anything was fetched
    # git runs as root (deploy key); make, venv, pip and the tests as the build user, never as root
    assert w.users["git ls-remote"] is None and w.users["git clone"] is None
    assert w.users["make"] == w.users["python3"] == w.users["pip"] == w.users["python"] == updater.BUILD_USER
    assert u.run_once() == "idle"  # nothing left to do
    # the same tag again: already built, already current
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "done" and "Already running" in u.status()["message"]


def test_failed_health_check_rolls_back(tmp_path):
    u, w = make_updater(tmp_path)
    w.healthy["m9"] = False
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "rolled_back"
    st = u.status()
    assert st["state"] == "rolled_back" and "m9 failed its health check" in st["message"] and "back on m8" in st["message"]
    assert u.link("current") == "m8" and u.link("previous") == "m7"  # previous untouched by the failed attempt
    assert w.t[0] - 1_000_000 >= updater.HEALTH_TIMEOUT_S  # waited the full window before giving up
    assert sum(1 for c in w.calls if c[0] == "systemctl") == 2  # switched forward, then back
    assert not os.path.exists(tmp_path / "app" / "pending.json")
    assert os.path.exists(tmp_path / "app" / "releases" / "m9" / ".ready")  # kept: a retry needs no rebuild
    assert u.run_once() == "idle"


@pytest.mark.parametrize("step", ["clone", "build", "pip", "test", "import"])
def test_failed_build_never_switches(tmp_path, step):
    u, w = make_updater(tmp_path)
    w.fail.add(step)
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "failed"
    st = u.status()
    assert st["state"] == "failed" and "failed" in st["error"] and "still running m8" in st["message"]
    assert u.link("current") == "m8" and u.link("previous") == "m7"
    assert not os.path.exists(tmp_path / "app" / "releases" / "m9")  # half-built dir removed
    assert not any(c[0] == "systemctl" for c in w.calls)
    assert not os.path.exists(tmp_path / "update-request.json")


def test_rollback_and_forward_again(tmp_path):
    u, w = make_updater(tmp_path)
    u.write_request({"action": "rollback"})
    assert u.run_once() == "done"
    assert u.link("current") == "m7" and u.link("previous") == "m8"  # the way forward again
    assert states_logged(tmp_path) == ["switching", "restarting", "checking", "done"]
    assert u.status()["total"] == 3
    u.write_request({"action": "rollback"})
    assert u.run_once() == "done" and u.link("current") == "m8" and u.link("previous") == "m7"
    # nothing to roll back to
    u2, _ = make_updater(tmp_path / "solo", releases=("m8",), previous=None)
    u2.write_request({"action": "rollback"})
    assert u2.run_once() == "failed" and "No previous release" in u2.status()["message"]
    # a broken rollback target is rolled back from too
    w.healthy["m7"] = False
    u.write_request({"action": "rollback"})
    assert u.run_once() == "rolled_back" and u.link("current") == "m8"


@pytest.mark.parametrize("state", ["fetching", "building", "testing"])
def test_power_cut_during_build_is_never_resumed(tmp_path, state):
    """At boot a fetch/build/test that was cut off is dropped, not resumed: nothing runs, the
    half-built release is deleted, current is untouched and Settings says to start it again."""
    u, w = make_updater(tmp_path)
    rel = tmp_path / "app" / "releases" / "m9"
    rel.mkdir()
    (rel / "half").write_text("partial clone")  # no .ready: garbage
    u.write_request({"action": "update", "target": "m9"})  # still there from the run that was cut off
    u.set_status(state=state, action="update", target="m9", from_release="m8", message="Building")
    assert u.run_once() == "failed"
    assert not w.calls  # no git, no make, no pip, no systemctl
    assert not rel.exists() and not (tmp_path / "update-request.json").exists()
    assert u.link("current") == "m8" and u.link("previous") == "m7"
    assert sorted(os.listdir(tmp_path / "app" / "releases")) == ["m7", "m8"]
    st = u.status()
    assert st["state"] == "failed" and st["error"] == "interrupted"
    assert st["message"] == ("Update to m9 was interrupted (power loss or reboot); still running m8. "
                             "Start it again from Settings.")
    assert u.run_once() == "idle" and not w.calls  # the next boot: nothing
    # starting it again from Settings builds it from scratch
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "done" and u.link("current") == "m9" and (rel / ".ready").exists()


def test_interrupted_never_deletes_a_finished_or_running_release(tmp_path):
    # cut after .ready was written but before the switch: the release is kept, nothing runs
    u, w = make_updater(tmp_path, releases=("m7", "m8", "m9"))
    u.set_status(state="testing", action="update", target="m9", from_release="m8")
    assert u.run_once() == "failed" and not w.calls
    assert (tmp_path / "app" / "releases" / "m9" / ".ready").exists() and u.link("current") == "m8"
    # a status that names the running or the previous release, or a path, deletes nothing
    os.unlink(tmp_path / "app" / "releases" / "m7" / ".ready")
    for target in ("m8", "m7", "../app", None):
        u.set_status(state="building", action="update", target=target)
        assert u.run_once() == "failed" and not w.calls, target
    assert sorted(os.listdir(tmp_path / "app" / "releases")) == ["m7", "m8", "m9"]


def test_interrupted_run_at_boot_exits_0(tmp_path):
    """What ledposter-update.service runs at boot: `updater.py run` with an interrupted build
    in update.json exits 0 (the unit does not fail) without running anything."""
    u, _ = make_updater(tmp_path)
    u.set_status(state="building", action="update", target="m9", from_release="m8")
    (tmp_path / "app" / "releases" / "m9").mkdir()
    script = os.path.join(os.path.dirname(updater.__file__), "updater.py")
    r = subprocess.run([sys.executable, script, "run"], env=dict(os.environ, LEDPOSTER_DATA=str(tmp_path), PATH="/nonexistent"),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "failed", r.stderr
    assert not (tmp_path / "app" / "releases" / "m9").exists() and u.status()["error"] == "interrupted"


def test_build_needs_free_memory(tmp_path):
    assert updater.build_memory_mb(MEMINFO.format(avail=300 * 1024, swap=150 * 1024)) == 450
    assert updater.build_memory_mb("MemTotal: 1 kB\n") is None and updater.build_memory_mb(None) is None
    # 399MB of RAM, no swap: refused before make, venv or pip ran; current untouched
    u, w = make_updater(tmp_path, avail_kb=399 * 1024)
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "failed"
    st = u.status()
    assert "not enough free memory" in st["error"] and "399 MB" in st["error"] and "400 MB needed" in st["error"]
    assert not any(c[0] in ("make", "python3") or c[0].endswith("/pip") for c in w.calls)
    assert u.link("current") == "m8" and not (tmp_path / "app" / "releases" / "m9").exists()
    # swap counts: 300MB + 100MB free swap is enough
    u, w = make_updater(tmp_path / "b", avail_kb=300 * 1024, swap_kb=100 * 1024)
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "done"
    assert updater.MIN_BUILD_MEMORY_MB == 400


def test_build_is_light_and_needs_a_compiler(tmp_path):
    u, w = make_updater(tmp_path)
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "done"
    make = [c for c in w.calls if c[0] == "make"]
    assert make and not any(a.startswith("-j") for c in make for a in c)  # MAKEFLAGS decides
    assert w.envs["make"]["MAKEFLAGS"] == "-j1" and "PIP_CACHE_DIR" not in w.envs["pip"]
    assert "--no-cache-dir" in next(c for c in w.calls if c[0].endswith("/pip"))
    # installed with --prebuilt: no compiler, refused before anything is fetched
    u, w = make_updater(tmp_path / "b", compiler=False)
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "failed" and "tools/deploy.sh" in u.status()["error"] and not w.calls
    # installed with --prebuilt on an image that ships a compiler anyway: the marker still refuses
    u, w = make_updater(tmp_path / "c")
    (tmp_path / "c" / "app" / ".prebuilt").write_text("installed from a prebuilt release\n")
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "failed" and "prebuilt" in u.status()["error"] and not w.calls
    unit = open(os.path.join(os.path.dirname(updater.__file__), "..", "..", "system", "systemd", "ledposter-update.service")).read()
    assert "Nice=19" in unit and "IOSchedulingClass=idle" in unit and "CPUWeight=10" in unit


def test_power_cut_after_pending_written_but_before_switch(tmp_path):
    u, w = make_updater(tmp_path, releases=("m7", "m8", "m9"))
    updater.write_atomic(str(tmp_path / "app" / "pending.json"),
                         json.dumps({"new": "m9", "old": "m8", "previous": "m7", "action": "update"}))
    u.write_request({"action": "update", "target": "m9"})
    u.set_status(state="switching", action="update", target="m9", from_release="m8")
    w.healthy["m9"] = False  # current is still m8 at boot, so "m9" is not what runs
    assert u.run_once() == "rolled_back"
    assert u.link("current") == "m8" and u.link("previous") == "m7" and not (tmp_path / "app" / "pending.json").exists()


@pytest.mark.parametrize("state", ["switching", "restarting", "checking"])
def test_power_cut_after_switch_finishes_or_rolls_back(tmp_path, state):
    # healthy: the boot-time run just confirms and promotes
    u, w = make_updater(tmp_path, releases=("m7", "m8", "m9"), current="m9", previous="m7")
    updater.write_atomic(str(tmp_path / "app" / "pending.json"),
                         json.dumps({"new": "m9", "old": "m8", "previous": "m7", "action": "update"}))
    u.write_request({"action": "update", "target": "m9"})
    u.set_status(state=state, action="update", target="m9", from_release="m8")
    assert u.run_once() == "done"
    assert u.link("current") == "m9" and u.link("previous") == "m8"
    assert not any(c[0] == "systemctl" for c in w.calls)  # services already came up from `current`
    # unhealthy: rolled back at boot
    u, w = make_updater(tmp_path / "b", releases=("m7", "m8", "m9"), current="m9", previous="m7")
    updater.write_atomic(str(tmp_path / "b" / "app" / "pending.json"),
                         json.dumps({"new": "m9", "old": "m8", "previous": "m7", "action": "update"}))
    u.set_status(state=state, action="update", target="m9", from_release="m8")
    w.healthy["m9"] = False
    assert u.run_once() == "rolled_back"
    assert u.link("current") == "m8" and u.link("previous") == "m7"
    assert [c for c in w.calls if c[0] == "systemctl"] == [["systemctl", "restart", "ledposter-engine.service", "ledposter-web.service"]]


@pytest.mark.parametrize("state", ["done", "failed", "rolled_back", "idle"])
def test_terminal_states_do_nothing_at_boot(tmp_path, state):
    u, w = make_updater(tmp_path)
    u.set_status(state=state, action="update", target="m9")
    assert u.run_once() == "idle" and u.link("current") == "m8" and not w.calls


def test_bad_request_and_atomic_files(tmp_path):
    u, w = make_updater(tmp_path)
    for target in ("../etc", "m9; rm -rf /", "-C", "", None, 7, "a" * 65):
        u.write_request({"action": "update", "target": target})
        assert u.run_once() == "failed" and u.status()["error"] == "bad request" and not w.calls, target
        assert not os.path.exists(tmp_path / "update-request.json")
    for text in ("[1, 2]", "not json", ""):
        updater.write_atomic(str(tmp_path / "update-request.json"), text)
        assert u.run_once() == "failed" and u.status()["error"] == "bad request" and not w.calls, text
    # a well-formed tag the repository does not have: refused before anything is cloned
    u.write_request({"action": "update", "target": "m99"})
    assert u.run_once() == "failed" and "not a release tag" in u.status()["error"]
    assert [c[:2] for c in w.calls] == [["git", "ls-remote"]] and not os.path.exists(tmp_path / "app" / "releases" / "m99")
    w.calls.clear()
    w.fail.add("ls-remote")
    u.write_request({"action": "update", "target": "m9"})
    assert u.run_once() == "failed" and "git ls-remote failed" in u.status()["error"]
    assert u.link("current") == "m8"
    p = tmp_path / "app" / "update.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o644
    assert [f for f in os.listdir(tmp_path / "app") if f.endswith(".tmp")] == []
    # the symlink switch leaves no temp link and is a relative link into releases/
    u.set_link("current", "m7")
    assert os.readlink(tmp_path / "app" / "current") == "releases/m7"
    assert not os.path.lexists(tmp_path / "app" / ".current.tmp")
    assert updater.config_port(str(tmp_path)) == 80
    (tmp_path / "config.yaml").write_text("version: 1\nweb:\n  admin_password: x\n  port: 8080\n  tokens: []\n")
    assert updater.config_port(str(tmp_path)) == 8080


def test_request_file_must_be_a_plain_file_of_the_service_user(tmp_path):
    """Root reads the request the unprivileged web app wrote: no symlinks, no FIFOs, no
    directories, nothing owned by someone else, nothing huge."""
    u, w = make_updater(tmp_path)
    req = tmp_path / "update-request.json"
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"action": "rollback"}))
    req.symlink_to(target)
    assert u.run_once() == "failed" and "Refused" in u.status()["message"] and "symlink" in u.status()["message"]
    assert not req.exists() and not os.path.lexists(req) and target.exists()  # the link went, its target did not
    assert u.link("current") == "m8" and not w.calls
    with pytest.raises(updater.InputError, match="symlink"):
        req.symlink_to("/etc/passwd")
        updater.read_input(str(req))
    req.unlink()
    os.mkfifo(req)
    with pytest.raises(updater.InputError, match="not a regular file"):
        updater.read_input(str(req))
    assert u.run_once() == "failed" and not w.calls
    req.mkdir()
    with pytest.raises(updater.InputError, match="not a regular file"):
        updater.read_input(str(req))
    req.rmdir()
    req.write_bytes(b"x" * (updater.MAX_INPUT_BYTES + 1))
    with pytest.raises(updater.InputError, match="too big"):
        updater.read_input(str(req))
    req.write_bytes(b'{"action": "rollback"}\0')
    with pytest.raises(updater.InputError, match="not text"):
        updater.read_input(str(req))
    req.unlink()
    with pytest.raises(FileNotFoundError):
        updater.read_input(str(req))
    assert u.run_once() == "idle"
    # someone else's file (a uid that is not root, not us, not the service user)
    u.write_request({"action": "rollback"})
    other = next(uid for uid in range(60000, 60100) if uid not in updater.input_owners())
    real = os.fstat

    def fake_fstat(fd):
        st = real(fd)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, other, st.st_gid, st.st_size,
                               st.st_atime, st.st_mtime, st.st_ctime))
    os.fstat = fake_fstat
    try:
        with pytest.raises(updater.InputError, match="owned by uid"):
            updater.read_input(str(req))
    finally:
        os.fstat = real
    assert updater.TAG_RE.match("m9") and updater.TAG_RE.match("v1.0.0-rc1") and not updater.TAG_RE.match(".hidden")
    assert not updater.TAG_RE.match("a/b") and not updater.TAG_RE.match("m9 x") and not updater.TAG_RE.match("$(id)")


def test_fetch_url_prefers_deploy_key_over_token(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "origin").write_text("git@github.com:owner/repo.git\n")
    assert updater.fetch_url(str(app)) == "git@github.com:owner/repo.git" and "GIT_SSH_COMMAND" not in updater.git_env(str(app))
    (app / "token").write_text("github_pat_123\n")
    assert updater.fetch_url(str(app)) == "https://x-access-token:github_pat_123@github.com/owner/repo.git"
    (app / "deploy_key").write_text("key")
    assert updater.fetch_url(str(app)) == "git@github.com:owner/repo.git"
    assert "/deploy_key -o IdentitiesOnly=yes" in updater.git_env(str(app))["GIT_SSH_COMMAND"]
    assert updater.list_tags(str(app), lambda argv, env, timeout: (0, "abc\trefs/tags/m8\nabd\trefs/tags/m9\n", "")) == ["m8", "m9"]
    with pytest.raises(updater.StepError, match="Permission denied"):
        updater.list_tags(str(app), lambda argv, env, timeout: (128, "", "git@github.com: Permission denied (publickey)."))


# -- media cache durability -------------------------------------------------------------

def test_media_cache_is_written_atomically(tmp_path, gif, monkeypatch):
    from ledposter import transcode as tc
    replaced, synced = [], []
    real_replace, real_fsync = os.replace, os.fsync
    monkeypatch.setattr(tc.os, "replace", lambda a, b: (replaced.append(os.path.basename(b)), real_replace(a, b)))
    monkeypatch.setattr(tc.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    out = tmp_path / "cache"
    tc.transcode(str(gif), str(out), w=8, h=12)
    assert replaced == ["frames.rgb", "thumb.png", "meta.json"]  # frames and thumb are durable before meta appears
    assert len(synced) >= 4  # frames, thumb, meta and the directory
    assert sorted(os.listdir(out)) == ["frames.rgb", "meta.json", "thumb.png"]  # no .part/.tmp left behind


def test_item_json_written_atomically(authed, gif, data, monkeypatch):
    from ledposter import transcode as tc
    synced = []
    real = os.fsync
    monkeypatch.setattr(tc.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    with open(gif, "rb") as f:
        mid = authed.post("/api/media", files={"file": ("a.gif", f, "image/gif")}).json()["id"]
    wait_status(authed, mid)
    files = os.listdir(data / "media" / mid)
    assert "item.json" in files and not any(f.endswith((".tmp", ".part")) for f in files)
    assert synced  # item.json and the cache went through fsync


# -- install-time config ---------------------------------------------------------------

def test_init_config_for_the_pi(tmp_path):
    p = tmp_path / "config.yaml"
    cfg = system.init_config(str(p), "key-only", f"# laptop\n{ED}\n")
    assert stat.S_IMODE(p.stat().st_mode) == 0o640
    text = yaml.safe_load(p.read_text())
    assert "user" not in text["engine"] and "group" not in text["engine"] and text["web"]["port"] == 80  # the unit's flags, not the app's file
    assert text["engine"]["socket"] == "/run/ledposter/engine.sock"  # where the web app connects
    assert text["playlists"] == {} and text["ssh"] == {"mode": "key-only", "authorized_keys": ED + "\n"}
    assert text["maintenance"] == {"enabled": True, "day": 6, "hour": 4} and text["version"] == 1
    assert text["matrix"]["hardware_mapping"] == "adafruit-hat-pwm" and cfg["network"]["hotspot_ssid"] == "LEDWall-Setup"
    assert (tmp_path / "ssh" / "mode").read_text() == "key-only\n" and (tmp_path / "ssh" / "authorized_keys").read_text() == ED + "\n"
    rules = (tmp_path / "firewall.rules").read_text().splitlines()
    assert "allow 80/tcp" in rules and "allow 22/tcp" in rules and "allow 4048/udp" not in rules
    assert (tmp_path / "maintenance.conf").read_text() == "ENABLED=1\nDAY=6\nHOUR=4\n"
    with pytest.raises(ValueError, match="at least one"):
        system.init_config(str(tmp_path / "b" / "config.yaml"), "key-only", "")
    system.init_config(str(tmp_path / "c" / "config.yaml"))
    assert (tmp_path / "c" / "ssh" / "mode").read_text() == "disabled\n"


# -- the API --------------------------------------------------------------------------

def test_health_is_open_and_system_info_is_not(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["engine"] is True and "version" in r.json()
    assert client.get("/api/system").status_code == 401
    client.post("/api/auth/setup", json={"password": PASSWORD})
    s = client.get("/api/system").json()
    assert s["hostname"] == "ledposter" and s["update"]["state"] == "idle" and s["ssh"]["mode"] == "disabled"
    assert s["maintenance"] == {"enabled": True, "day": 6, "hour": 4} and s["previous"] is None and s["overlay"] is False
    assert "config.yaml" in s["reset_plan"] and "setup-password" not in s["reset_plan"]


def test_ssh_and_maintenance_settings_write_unit_inputs(authed, data):
    fake = authed.fakenet
    assert authed.put("/api/system/ssh", json={"mode": "key-only", "authorized_keys": ""}).status_code == 422
    r = authed.put("/api/system/ssh", json={"mode": "key-only", "authorized_keys": "garbage"})
    assert r.status_code == 422 and "Line 1" in r.json()["detail"]
    r = authed.put("/api/system/ssh", json={"mode": "key-only", "authorized_keys": f"# me\n{ED}\n"})
    assert r.status_code == 200 and r.json() == {"mode": "key-only", "authorized_keys": ED + "\n"}
    assert (data / "ssh" / "mode").read_text() == "key-only\n" and (data / "ssh" / "authorized_keys").read_text() == ED + "\n"
    assert "allow 22/tcp" in (data / "firewall.rules").read_text()
    assert ("start", "ledposter-ssh.service") in fake.units and ("start", "ledposter-firewall.service") in fake.units
    assert yaml.safe_load((data / "config.yaml").read_text())["ssh"]["mode"] == "key-only"
    n = len(fake.units)
    assert authed.put("/api/system/ssh", json={"mode": "disabled", "authorized_keys": ED}).status_code == 200
    assert (data / "ssh" / "mode").read_text() == "disabled\n" and "allow 22/tcp" not in (data / "firewall.rules").read_text()
    assert len(fake.units) == n + 2  # both changed again
    # DDP on opens its ports through the same path
    s = authed.get("/api/settings").json()
    s["ddp"]["enabled"] = True
    assert authed.put("/api/settings", json=s).status_code == 200
    assert "allow 4048/udp" in (data / "firewall.rules").read_text() and fake.units[-1] == ("start", "ledposter-firewall.service")
    r = authed.put("/api/system/maintenance", json={"enabled": False, "day": 0, "hour": 3})
    assert r.status_code == 200 and (data / "maintenance.conf").read_text() == "ENABLED=0\nDAY=0\nHOUR=3\n"
    assert authed.put("/api/system/maintenance", json={"enabled": True, "day": 7, "hour": 3}).status_code == 422
    assert authed.post("/api/system/maintenance/run").status_code == 200
    assert (data / "maintenance-request").exists() and fake.units[-1] == ("start", "ledposter-maintenance.service")
    assert not (data / "state").exists()  # state/ is root's; the app writes nothing there


def test_update_rollback_restart_and_factory_reset_requests(authed, data, engine):
    fake = authed.fakenet
    r = authed.post("/api/system/update", json={"target": "m9"})
    assert r.status_code == 202 and r.json()["state"] == "fetching" and r.json()["target"] == "m9"
    assert json.load(open(data / "update-request.json")) == {"action": "update", "target": "m9"}
    assert not (data / "app" / "update-request.json").exists()  # app/ is root's
    assert fake.units[-1] == ("start", "ledposter-update.service")
    assert authed.post("/api/system/update", json={"target": "../x"}).status_code == 422
    assert authed.post("/api/system/rollback").status_code == 409  # nothing to roll back to
    updater.write_atomic(str(data / "app" / "update.json"), json.dumps({"state": "building", "action": "update", "target": "m9"}))
    assert authed.post("/api/system/update", json={"target": "m10"}).status_code == 409  # one at a time
    assert authed.get("/api/system/update").json()["state"] == "building"
    # restarts go through systemctl after the response has left
    assert authed.post("/api/system/restart", json={"what": "engine"}).status_code == 200
    assert authed.post("/api/system/restart", json={"what": "nope"}).status_code == 422
    time.sleep(2)
    assert ("restart", "ledposter-engine.service") in fake.units
    r = authed.post("/api/system/factory_reset", json={"hostname": "wrong"})
    assert r.status_code == 422 and "ledposter" in r.json()["detail"]
    assert authed.post("/api/system/factory_reset", json={"hostname": "LEDposter"}).status_code == 200
    time.sleep(2)
    assert (data / "factory-reset").exists() and fake.units[-1] == ("start", "ledposter-factory-reset.service")
    assert any(t.startswith("Factory reset") for t, _ in engine.texts)
    assert authed.post("/api/system/restart", json={"what": "reboot"}).status_code == 200
    time.sleep(2)
    assert ("reboot", None) in fake.units


def test_check_updates_uses_the_repo(data, engine, fakenet):
    calls = []

    def git(argv, env=None, timeout=30):
        calls.append(argv)
        return 0, "x\trefs/tags/m7\ny\trefs/tags/m8\nz\trefs/tags/m9\n", ""
    (data / "app" / "releases" / "m8").mkdir(parents=True)
    os.symlink("releases/m8", data / "app" / "current")
    with TestClient(create_app(str(data), net_runner=fakenet.run, git_runner=git)) as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        r = c.get("/api/system/updates")
        assert r.status_code == 200 and r.json() == {"current": "m8", "newer": ["m9"], "all": ["m7", "m8", "m9"]}
        assert calls[0][:4] == ["git", "ls-remote", "--tags", "--refs"]
        assert c.get("/api/system").json()["release"] == "m8"
    with TestClient(create_app(str(data), net_runner=fakenet.run,
                               git_runner=lambda a, env=None, timeout=30: (128, "", "Permission denied (publickey)"))) as c:
        c.post("/api/auth/login", json={"password": PASSWORD})
        r = c.get("/api/system/updates")
        assert r.status_code == 503 and "Permission denied" in r.json()["detail"]


def test_last_mode_is_persisted_and_restored(authed, data, engine):
    assert authed.post("/api/mode", json={"mode": "effect", "effect": "plasma", "speed": 2}).status_code == 200
    assert yaml.safe_load((data / "config.yaml").read_text())["last_mode"] == {"mode": "effect", "effect": "plasma", "speed": 2}
    engine.state["mode"], engine.state["mode_name"], engine.state["uptime_s"] = "boot", "boot", 1  # the engine rebooted
    t0 = time.time()
    while time.time() - t0 < 8 and engine.state["mode"] == "boot":
        time.sleep(0.1)
    assert engine.state["mode"] == "effect" and engine.state["mode_name"] == "plasma"
    assert {"cmd": "set_mode", "mode": "effect", "effect": "plasma", "speed": 2, "id": engine.calls[-1]["id"]} in engine.calls or \
        any(c.get("cmd") == "set_mode" and c.get("effect") == "plasma" for c in engine.calls)


def test_config_defaults_have_the_m8_sections():
    cfg = config.load("/nonexistent/config.yaml")
    assert cfg["ssh"] == {"mode": "disabled", "authorized_keys": ""} and cfg["last_mode"] == {}
    assert cfg["maintenance"] == {"enabled": True, "day": 6, "hour": 4}


def test_limiter_ceiling_is_capped_at_the_supply_budget(authed, data, tmp_path):
    """docs/spec.md: about 15A for the panels. The API refuses more, config.load clamps a hand-edited
    file (and the engine clamps and logs it too, engine/src/main.cpp)."""
    assert config.MAX_CEILING_AMPS == 15.0
    s = authed.get("/api/settings").json()
    s["power"]["ceiling_amps"] = 15
    assert authed.put("/api/settings", json=s).status_code == 200
    for bad in (15.5, 16, 100, 0, -1):
        s["power"]["ceiling_amps"] = bad
        assert authed.put("/api/settings", json=s).status_code == 422, bad
    assert yaml.safe_load((data / "config.yaml").read_text())["power"]["ceiling_amps"] == 15
    p = tmp_path / "c.yaml"
    for raw, want in (("40", 15.0), ("13", 13.0), ("0", 13.0), ("-2", 13.0), ("abc", 13.0)):
        p.write_text(f"version: 1\npower: {{ceiling_amps: {raw}, amps_full_white_per_panel: 6}}\n")
        assert config.load(str(p))["power"]["ceiling_amps"] == want, raw


def test_deploy_key_copy_for_its_owner(tmp_path):
    """ssh refuses a group-readable key run by its owner (root, the updater): it gets a 0600 copy."""
    key = tmp_path / "deploy_key"
    key.write_bytes(b"KEY")
    key.chmod(0o640)
    copy = updater.private_key_copy(str(key))
    assert copy != str(key) and open(copy, "rb").read() == b"KEY"
    assert os.stat(copy).st_mode & 0o777 == 0o600
    key.chmod(0o600)
    assert updater.private_key_copy(str(key)) == str(key)
