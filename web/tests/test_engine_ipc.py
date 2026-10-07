"""Engine control and preview sockets (docs/protocol.md), against the simulator build.

Needs engine/build/ledengine-sim (cd engine && make sim); skipped without it.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "engine" / "build" / "ledengine-sim"
pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="engine sim not built (cd engine && make sim)")

W, H = 128, 192
FRAME = W * H * 3
CEILING = 13.0

CONFIG = """\
version: 1
matrix:
  panel_size: 64
  gpio_slowdown: 2
  panels:
    - {{col: 0, row: 0}}
    - {{col: 1, row: 0}}
    - {{col: 1, row: 1}}
    - {{col: 0, row: 1}}
    - {{col: 0, row: 2}}
    - {{col: 1, row: 2}}
power: {{ceiling_amps: 13.0, amps_full_white_per_panel: 6.0}}
brightness:
  user: {user}
  auto: true
  auto_curve: {{lux_low: 2, min_pct: 10, lux_high: 400, max_pct: 100, smoothing_s: 0}}
sensors:
  bh1750: {{i2c_bus: 1, address: 0x23, poll_ms: 50}}
engine:
  socket: {dir}/engine.sock
  preview_socket: {dir}/frames.sock
  preview_fps: 15
display: {{gamma: 2.2}}
playlists:
  two:
    items:
      - {{media: grey, duration_s: 5}}
"""


class Control:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.f = self.sock.makefile("rwb")
        self.n = 0

    def raw(self, line):
        self.f.write(line + b"\n")
        self.f.flush()
        return json.loads(self.f.readline())

    def __call__(self, cmd, **args):
        self.n += 1
        r = self.raw(json.dumps({"id": self.n, "cmd": cmd, **args}).encode())
        assert r["id"] == self.n
        return r

    def ok(self, cmd, **args):
        r = self(cmd, **args)
        assert r["ok"], r
        return r

    def wait(self, pred, timeout=3.0):
        """Status once pred(status) holds; settings apply from the next frame."""
        end = time.monotonic() + timeout
        while True:
            st = self.ok("status")
            if pred(st):
                return st
            assert time.monotonic() < end, st
            time.sleep(0.05)


def preview_conn(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(str(path))
    return s


def read_preview(f):
    status = json.loads(f.readline())
    data = f.read(status["w"] * status["h"] * 3)
    assert len(data) == FRAME
    return status, data


def amps_of(data):
    """The engine's estimate for a frame as output (6A per 64x64 panel at full white)."""
    return sum(data) / (255 * 3 * 64 * 64) * 6.0


class Engine:
    def __init__(self, *args, user=100):
        self.dir = Path(tempfile.mkdtemp(prefix="lpipc", dir="/tmp"))  # short: socket paths max ~104 chars
        self.ctl_path, self.prev_path = self.dir / "engine.sock", self.dir / "frames.sock"
        self.write_config(user=user)
        self.make_media("grey", [(100, 100, 100), (150, 150, 150)])
        self.log = open(self.dir / "engine.log", "wb")
        self.proc = subprocess.Popen([str(ENGINE), *args], stderr=self.log,
                                     env={**os.environ, "LEDPOSTER_DATA": str(self.dir)})
        end = time.monotonic() + 5
        while not (self.ctl_path.exists() and self.prev_path.exists()):
            assert self.proc.poll() is None, self.logtext()
            assert time.monotonic() < end, self.logtext()
            time.sleep(0.02)
        self.ctl = Control(self.ctl_path)
        self.ctl.wait(lambda s: s["fps"] > 0)  # fps is measured over 1s windows
        self.ctl.n = 0

    def write_config(self, extra="", **kw):
        (self.dir / "config.yaml").write_text(CONFIG.format(dir=self.dir, **kw) + extra)

    def make_media(self, media_id, colors):
        d = self.dir / "media" / media_id
        d.mkdir(parents=True)
        (d / "frames.rgb").write_bytes(b"".join(bytes(c) * (W * H) for c in colors))
        (d / "meta.json").write_text(json.dumps({"w": W, "h": H, "durations_ms": [200] * len(colors)}))

    def logtext(self):
        return (self.dir / "engine.log").read_text(errors="replace")

    def stop(self):
        self.ctl.sock.close()
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        rc = self.proc.wait(5)
        self.log.close()
        sockets_left = self.ctl_path.exists() or self.prev_path.exists()
        shutil.rmtree(self.dir, ignore_errors=True)
        assert rc == 0
        assert not sockets_left


@pytest.fixture
def engine():
    started = []

    def start(*args, **kw):
        started.append(Engine(*args, **kw))
        return started[-1]

    yield start
    for e in started:
        e.stop()


def test_full_white_is_limited(engine):
    e = engine("--pattern", "solid", "--color", "000000")
    assert e.ctl.ok("set_mode", mode="pattern", pattern="solid", color="ffffff") == {
        "id": 1, "ok": True, "mode": "pattern", "mode_name": "solid"}
    st = e.ctl.wait(lambda s: s["limited"])
    assert st["amps"] <= CEILING and st["amps"] > CEILING - 0.5
    assert st["amps_unlimited"] == pytest.approx(36.0, abs=0.01)
    assert st["ceiling_amps"] == CEILING
    assert (st["w"], st["h"], st["power"]) == (W, H, True)
    assert st["brightness"] == {"user": 100, "auto": None, "auto_enabled": True, "effective": 100,
                                "output": 36}
    assert (st["lux"], st["sensor"]) == (None, "missing")
    assert st["fps"] > 10 and st["uptime_s"] >= 0

    with preview_conn(e.prev_path) as s:
        pst, data = read_preview(s.makefile("rb"))
    assert pst["limited"] and pst["mode"] == "pattern"
    assert set(data) == {255 * 36 // 100}  # every pixel scaled down by the limiter
    assert amps_of(data) <= CEILING


def test_user_brightness_lowers_amps(engine):
    e = engine("--pattern", "solid", "--color", "808080")  # gamma 2.2: 56/255 of full
    full = e.ctl.wait(lambda s: s["amps"] > 0)
    assert not full["limited"] and full["amps"] == pytest.approx(36 * 56 / 255, abs=0.01)
    e.ctl.ok("set_brightness", value=50)
    half = e.ctl.wait(lambda s: s["brightness"]["output"] == 50)
    assert half["amps"] == pytest.approx(full["amps"] / 2, abs=0.01)
    assert half["brightness"]["user"] == 50

    e.ctl.ok("set_mode", mode="pattern", pattern="solid", color="ffffff")
    e.ctl.ok("set_brightness", value=20)  # 20% of 36A: under the ceiling
    st = e.ctl.wait(lambda s: s["amps_unlimited"] > 7)
    assert not st["limited"] and st["amps"] == pytest.approx(7.2, abs=0.01)


def test_fake_lux_drives_auto_brightness(engine):
    e = engine("--pattern", "solid", "--color", "ffffff", user=80)
    e.ctl.ok("set_fake_lux", lux=1)  # darker than lux_low
    st = e.ctl.wait(lambda s: s["sensor"] == "fake")
    assert st["lux"] == 1 and st["brightness"]["auto"] == 10
    assert st["brightness"]["effective"] == 10 and st["brightness"]["output"] == 10
    assert not st["limited"]

    e.ctl.ok("set_fake_lux", lux=28.3)  # geometric midpoint of 2..400 -> 55%
    st = e.ctl.wait(lambda s: s["lux"] == 28.3)
    assert st["brightness"]["auto"] == pytest.approx(55, abs=0.5)
    assert st["brightness"]["effective"] == 55 and st["limited"]  # 55% white > 13A

    e.ctl.ok("set_fake_lux", lux=100000)
    st = e.ctl.wait(lambda s: s["brightness"]["auto"] == 100)
    assert st["brightness"]["effective"] == 80  # min(user, auto)

    e.ctl.ok("set_fake_lux", lux=1)
    e.ctl.ok("set_auto_brightness", enabled=False)
    st = e.ctl.wait(lambda s: not s["brightness"]["auto_enabled"] and s["lux"] == 1)
    assert st["brightness"]["auto"] == 10 and st["brightness"]["effective"] == 80

    e.ctl.ok("set_auto_brightness", enabled=True)
    e.ctl.ok("set_fake_lux", lux=None)  # sensor gone: falls back to user
    st = e.ctl.wait(lambda s: s["sensor"] == "missing")
    assert st["lux"] is None and st["brightness"]["auto"] is None
    assert st["brightness"]["effective"] == 80


def test_power_off_outputs_black(engine):
    e = engine("--pattern", "solid", "--color", "ffffff")
    e.ctl.ok("power", on=False)
    st = e.ctl.wait(lambda s: not s["power"] and s["amps"] == 0)
    assert st["brightness"]["output"] == 0 and st["fps"] > 0  # still running
    with preview_conn(e.prev_path) as s:
        _, data = read_preview(s.makefile("rb"))
    assert set(data) == {0}
    e.ctl.ok("power", on=True)
    e.ctl.wait(lambda s: s["amps"] > 12)


def test_media_and_playlist_modes(engine):
    e = engine("--pattern", "panel-id", user=50)
    e.ctl.ok("set_mode", mode="media", media="grey")
    st = e.ctl.wait(lambda s: s["mode"] == "media")
    assert st["mode_name"] == "grey"
    with preview_conn(e.prev_path) as s:
        f = s.makefile("rb")
        seen = {read_preview(f)[1][0] for _ in range(8)}  # ~0.5s: both 200ms frames
    assert seen == {50, 75}  # media is pre-gamma'd: only brightness applies (50%)

    assert e.ctl.ok("set_mode", mode="playlist", playlist="two")["mode_name"] == "two"
    e.ctl.wait(lambda s: s["mode"] == "playlist")


def test_errors(engine):
    e = engine()
    bad = e.ctl.raw(b"{not json")
    assert bad["ok"] is False and bad["id"] is None and "bad JSON" in bad["error"]
    assert e.ctl.raw(b'{"id": 9}') == {"id": 9, "ok": False, "error": "missing string field 'cmd'"}
    for cmd, args, msg in [
        ("nope", {}, "unknown cmd 'nope'"),
        ("set_brightness", {"value": 101}, "'value' must be a number from 0 to 100"),
        ("set_brightness", {}, "missing field 'value'"),
        ("power", {"on": "yes"}, "'on' must be true or false"),
        ("set_mode", {"mode": "pattern", "pattern": "nope"}, "unknown pattern 'nope'"),
        ("set_mode", {"mode": "pattern", "pattern": "solid", "color": "red"}, "color must be"),
        ("set_mode", {"mode": "media", "media": "missing"}, "no meta.json"),
        ("set_mode", {"mode": "media", "media": "../etc"}, "bad media id"),
        ("set_mode", {"mode": "playlist", "playlist": "nope"}, "no playlist 'nope'"),
        ("set_mode", {"mode": "dance"}, "mode must be pattern, media or playlist"),
        ("set_fake_lux", {"lux": -1}, "'lux' must be a number"),
    ]:
        r = e.ctl(cmd, **args)
        assert r["ok"] is False and msg in r["error"], (cmd, r)
    assert e.ctl.ok("status")["mode_name"] == "panel-id"  # nothing applied


def test_reload_config(engine):
    e = engine("--playlist", "two")
    e.write_config(user=40)
    assert e.ctl.ok("reload_config")["restart_needed"] == []
    st = e.ctl.wait(lambda s: s["brightness"]["user"] == 40)
    assert st["mode"] == "playlist"

    e.write_config(user=40, extra="  # comment only\n")
    (e.dir / "config.yaml").write_text((e.dir / "config.yaml").read_text().replace(
        "gpio_slowdown: 2", "gpio_slowdown: 3").replace("preview_fps: 15", "preview_fps: 10"))
    assert e.ctl.ok("reload_config")["restart_needed"] == ["matrix"]  # preview_fps is live

    (e.dir / "config.yaml").write_text("matrix: [")
    r = e.ctl("reload_config")
    assert r["ok"] is False and r["error"]
    assert e.ctl.ok("status")["brightness"]["user"] == 40  # kept the last good config


def test_stalled_preview_client_does_not_slow_engine(engine):
    e = engine("--pattern", "cycle")  # redraws every 33ms
    time.sleep(2.2)
    base = e.ctl.ok("status")["fps"]
    assert base > 20
    stalled = []
    for _ in range(3):  # connect, never read
        s = preview_conn(e.prev_path)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stalled.append(s)
    time.sleep(2.2)
    assert e.ctl.ok("status")["fps"] >= base * 0.8

    with preview_conn(e.prev_path) as s:  # a reading client still gets <= 15 frames/s
        f = s.makefile("rb")
        read_preview(f)
        t0, n = time.monotonic(), 0
        while time.monotonic() - t0 < 1.0:
            read_preview(f)
            n += 1
    assert 5 <= n <= 16
    for s in stalled:
        s.close()


CLIENT = """
import socket, sys, time
c = socket.socket(socket.AF_UNIX); c.connect(sys.argv[1])
c.sendall(b'{"id": 1, "cmd": "sta')  # half a request
p = socket.socket(socket.AF_UNIX); p.connect(sys.argv[2])
p.recv(1000)  # part of a frame
print("ready", flush=True)
time.sleep(60)
"""


def test_killed_client_does_not_kill_engine(engine):
    e = engine("--pattern", "cycle")
    client = subprocess.Popen([sys.executable, "-c", CLIENT, str(e.ctl_path), str(e.prev_path)],
                              stdout=subprocess.PIPE)
    assert client.stdout.readline() == b"ready\n"
    client.kill()
    client.wait()
    client.stdout.close()
    time.sleep(0.3)
    assert e.proc.poll() is None
    assert e.ctl.ok("status")["fps"] > 0
    with preview_conn(e.prev_path) as s:
        read_preview(s.makefile("rb"))
    ctl2 = Control(e.ctl_path)  # new clients still welcome
    ctl2.ok("status")
    ctl2.sock.close()
