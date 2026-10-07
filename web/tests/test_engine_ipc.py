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
    def __init__(self, *args, user=100, extra="", config=None):
        self.dir = Path(tempfile.mkdtemp(prefix="lpipc", dir="/tmp"))  # short: socket paths max ~104 chars
        self.ctl_path, self.prev_path = self.dir / "engine.sock", self.dir / "frames.sock"
        self.write_config(user=user, extra=extra)
        if config is not None:  # {dir} is replaced with the engine's temp dir
            (self.dir / "config.yaml").write_text(config.replace("{dir}", str(self.dir)))
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
        "id": 1, "ok": True, "mode": "pattern", "mode_name": "solid", "settings": {}}
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
        ("set_mode", {"mode": "dance"}, "mode must be pattern, media, playlist, effect, clock, text or idle"),
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


# ---- milestone 5: generative modes, clock, text, overlays, button ----

EFFECTS = ["spider-sense", "web-shot", "uav-radar", "plasma", "fire", "starfield"]

BUTTON = """\
button:
  gpio: 25
  debounce_ms: 20
  long_ms: 400
  very_long_ms: 1000
  cycle:
    - {mode: effect, effect: plasma, settings: {speed: 2, colors: ["ff0000", "0000ff"]}}
    - {mode: clock, hour24: false}
    - {mode: playlist, playlist: three}
    - {mode: playlist, playlist: missing}
"""

THREE = """\
  three:
    items:
      - {media: red, duration_s: 60}
      - {media: green, duration_s: 60}
      - {media: blue, duration_s: 60}
"""


def frame_now(e):
    with preview_conn(e.prev_path) as s:
        return read_preview(s.makefile("rb"))


def test_list_modes(engine):
    e = engine()
    r = e.ctl.ok("list_modes")
    assert r["events"] == ["kill", "killstreak", "death", "win"]
    assert "10x20" in r["fonts"] and "tom-thumb" in r["fonts"]
    modes = {(m["mode"], m.get("effect")): m for m in r["modes"]}
    assert set(modes) == {("pattern", None), ("media", None), ("playlist", None), ("clock", None),
                          ("text", None), ("idle", None)} | {("effect", x) for x in EFFECTS}
    for m in r["modes"]:
        assert m["label"] and isinstance(m["settings"], list)
        for s in m["settings"]:
            assert s["type"] in {"number", "int", "bool", "color", "colors", "string", "choice"}
            assert "default" in s
            if s["type"] in ("number", "int", "colors"):
                assert s["min"] <= s["max"]
            if s["type"] == "choice":
                assert s["default"] in s["choices"]
    for x in EFFECTS:
        st = {s["name"]: s for s in modes[("effect", x)]["settings"]}
        assert st["speed"] == {"name": "speed", "type": "number", "min": 0.1, "max": 5, "default": 1.0,
                               "help": "animation speed, 1 = normal"}
        assert st["colors"]["type"] == "colors" and len(st["colors"]["default"]) >= 1
    text = {s["name"]: s for s in modes[("text", None)]["settings"]}
    assert set(text) == {"text", "color", "background", "speed", "font", "scale"}
    assert text["font"]["choices"] == r["fonts"]
    assert {s["name"] for s in modes[("clock", None)]["settings"]} == {"hour24", "seconds", "date", "color"}
    assert modes[("playlist", None)]["settings"][0]["choices"] == ["two"]
    assert modes[("pattern", None)]["settings"][0]["choices"] == ["panel-id", "solid", "cycle", "rows", "cols"]


def test_set_mode_new_modes(engine):
    e = engine()
    for x in EFFECTS:
        r = e.ctl.ok("set_mode", mode="effect", effect=x, settings={"speed": 2.5})
        assert (r["mode"], r["mode_name"], r["settings"]["speed"]) == ("effect", x, 2.5)
        st = e.ctl.wait(lambda s: s["mode_name"] == x)
        assert st["mode"] == "effect" and st["mode_settings"]["speed"] == 2.5
        assert st["mode_settings"]["colors"]
        _, data = frame_now(e)
        assert any(data), x  # something on screen
    r = e.ctl.ok("set_mode", mode="effect", effect="plasma", colors=["#FF0000", "00ff00"], scale=2)
    assert r["settings"] == {"speed": 1.0, "colors": ["ff0000", "00ff00"], "scale": 2.0}

    r = e.ctl.ok("set_mode", mode="clock", settings={"hour24": False, "date": True, "color": "ff8000"})
    assert (r["mode"], r["mode_name"]) == ("clock", "clock")
    st = e.ctl.wait(lambda s: s["mode"] == "clock")
    assert st["mode_settings"] == {"hour24": False, "seconds": False, "date": True, "color": "ff8000"}
    _, data = frame_now(e)
    assert any(data[0::3]) and not any(data[2::3])  # orange digits: red, no blue

    r = e.ctl.ok("set_mode", mode="text", text="Hello wall", color="00ff00", background="000040", speed=3)
    assert r["settings"]["text"] == "Hello wall" and r["settings"]["speed"] == 3
    st = e.ctl.wait(lambda s: s["mode"] == "text")
    assert st["mode_settings"]["background"] == "000040"
    _, data = frame_now(e)
    assert data[0:3] == bytes([0, 0, 12])  # corner: the background, 0x40 through gamma 2.2

    assert e.ctl.ok("set_mode", mode="idle")["mode"] == "idle"
    st = e.ctl.wait(lambda s: s["mode"] == "idle")
    assert st["mode_settings"]["clock"] is True

    for args, msg in [
        ({"mode": "effect", "effect": "lava"}, "unknown effect 'lava' (spider-sense, web-shot"),
        ({"mode": "effect"}, "missing field 'effect'"),
        ({"mode": "effect", "effect": "fire", "settings": {"speed": 6}}, "'speed' must be a number from 0.1 to 5"),
        ({"mode": "effect", "effect": "fire", "settings": {"sped": 1}}, "unknown setting 'sped'"),
        ({"mode": "effect", "effect": "fire", "colors": []}, "'colors' must be a list of 1 to 6"),
        ({"mode": "clock", "hour24": "yes"}, "'hour24' must be true or false"),
        ({"mode": "text", "text": "x" * 501}, "'text' must be a string of at most 500 bytes"),
        ({"mode": "text", "font": "comic"}, "'font' must be one of"),
    ]:
        r = e.ctl("set_mode", **args)
        assert r["ok"] is False and msg in r["error"], (args, r)
    assert e.ctl.ok("status")["mode"] == "idle"  # nothing applied


def test_overlay_and_show_text_expire(engine):
    e = engine("--pattern", "solid", "--color", "000000")
    assert e.ctl.ok("overlay", event="kill")["ms"] == 1500
    t0 = time.monotonic()
    st = e.ctl.wait(lambda s: s["overlays"] == ["kill"])
    assert st["mode"] == "pattern" and st["mode_name"] == "solid"  # the mode keeps running
    time.sleep(0.3)
    _, data = frame_now(e)
    assert any(data)  # drawn on top of black
    e.ctl.wait(lambda s: s["overlays"] == [], timeout=3)
    assert 1.4 < time.monotonic() - t0 < 2.2
    _, data = frame_now(e)
    assert not any(data)  # gone

    for ev, ms in [("killstreak", 2500), ("death", 2000), ("win", 3000)]:
        assert e.ctl.ok("overlay", event=ev)["ms"] == ms
        e.ctl.wait(lambda s: s["overlays"] == [ev])
    r = e.ctl("overlay", event="headshot")
    assert r["ok"] is False and "unknown event 'headshot' (kill, killstreak, death, win)" in r["error"]

    assert e.ctl.ok("show_text", text="10.0.0.42", seconds=0.6)["ms"] == 600
    e.ctl.wait(lambda s: "text" in s["overlays"])
    e.ctl.wait(lambda s: "text" not in s["overlays"], timeout=2)
    e.ctl.ok("show_text", text="Wi-Fi setup", seconds=60, color="00ff00")
    e.ctl.wait(lambda s: "text" in s["overlays"])
    e.ctl.ok("show_text", text="")  # clears
    e.ctl.wait(lambda s: "text" not in s["overlays"])
    for args, msg in [({"text": 3}, "'text' must be a string"),
                      ({"text": "x", "seconds": 0}, "'seconds' must be a number from 0.5"),
                      ({}, "missing field 'text'")]:
        r = e.ctl("show_text", **args)
        assert r["ok"] is False and msg in r["error"], (args, r)


def test_limiter_holds_with_overlays(engine):
    e = engine("--pattern", "solid", "--color", "ffffff")
    e.ctl.ok("overlay", event="win")
    e.ctl.ok("show_text", text="still under the ceiling", seconds=5)
    e.ctl.wait(lambda s: s["overlays"] == ["win", "text"])
    for _ in range(10):
        st = e.ctl.ok("status")
        assert st["limited"] and st["amps"] <= CEILING
        pst, data = frame_now(e)
        assert pst["overlays"] and pst["amps"] <= CEILING and amps_of(data) <= CEILING
        time.sleep(0.1)


def test_sim_button(engine):
    e = engine(extra=BUTTON)
    e.write_config(user=100, extra=BUTTON)  # the playlist below goes under playlists:
    cfg = (e.dir / "config.yaml").read_text()
    (e.dir / "config.yaml").write_text(cfg.replace("playlists:\n", "playlists:\n" + THREE))
    assert e.ctl.ok("reload_config")["restart_needed"] == []
    for media, c in [("red", (200, 0, 0)), ("green", (0, 200, 0)), ("blue", (0, 0, 200))]:
        e.make_media(media, [c])
    assert e.ctl.ok("status")["button"] == "sim"

    def press(ms):
        e.ctl.ok("sim_button", ms=ms)

    press(60)  # short: first cycle entry
    st = e.ctl.wait(lambda s: s["mode"] == "effect")
    assert st["mode_name"] == "plasma" and st["mode_settings"]["colors"] == ["ff0000", "0000ff"]
    press(60)
    st = e.ctl.wait(lambda s: s["mode"] == "clock")
    assert st["mode_settings"]["hour24"] is False

    press(60)  # the playlist: short presses step through its items
    e.ctl.wait(lambda s: s["mode"] == "playlist" and s["mode_name"] == "three")
    colours = []
    for i in range(3):
        if i:
            press(60)
            time.sleep(0.3)
        _, data = frame_now(e)
        colours.append(tuple(data[:3]))
    assert [c.index(max(c)) for c in colours] == [0, 1, 2]  # red, green, blue
    press(60)  # past the last item; the next entry is broken (no such playlist): wraps to plasma
    st = e.ctl.wait(lambda s: s["mode"] == "effect")
    assert "button.cycle[3]: no playlist 'missing'" in e.logtext()

    press(600)  # long: display off, the mode keeps running
    st = e.ctl.wait(lambda s: not s["power"])
    assert st["mode"] == "effect" and st["amps"] == 0
    press(600)
    e.ctl.wait(lambda s: s["power"])

    assert e.ctl.ok("status")["wifi_setup_requested"] is False
    press(1300)  # very long: asks for Wi-Fi setup (networking is milestone 6)
    st = e.ctl.wait(lambda s: s["wifi_setup_requested"])
    assert st["wifi_setup_count"] == 1 and abs(st["wifi_setup_at"] - time.time()) < 5
    assert "text" in st["overlays"] and st["power"]  # "Wi-Fi setup" on the wall
    time.sleep(0.5)  # the release after a very long press does nothing
    st = e.ctl.ok("status")
    assert st["power"] and st["mode"] == "effect"
    e.ctl.ok("ack_wifi_setup")
    st = e.ctl.wait(lambda s: not s["wifi_setup_requested"])
    assert st["wifi_setup_count"] == 1

    r = e.ctl("sim_button", ms=0)
    assert r["ok"] is False and "'ms' must be a number from 1 to 60000" in r["error"]


def test_sim_button_off_without_gpio(engine):
    e = engine()
    assert e.ctl.ok("status")["button"] == "off"
    r = e.ctl("sim_button", ms=100)
    assert r["ok"] is False and "the button is off" in r["error"]


def test_start_flags(engine):
    assert engine("--effect", "fire").ctl.ok("status")["mode_name"] == "fire"
    assert engine("--mode", "clock").ctl.ok("status")["mode"] == "clock"
    st = engine("--text", "hi there").ctl.ok("status")
    assert st["mode"] == "text" and st["mode_settings"]["text"] == "hi there"


def test_minimal_config_starts(engine):
    """The web app writes config.yaml from its own defaults: no auto_curve, sensors or display
    sections. The engine fills in defaults instead of refusing to start."""
    e = engine("--effect", "plasma", config="""version: 1
matrix: {panel_size: 64, panels: [{col: 0, row: 0}, {col: 1, row: 0}, {col: 1, row: 1}, {col: 0, row: 1}, {col: 0, row: 2}, {col: 1, row: 2}]}
brightness: {user: 100, auto: true}
engine: {socket: {dir}/engine.sock, preview_socket: {dir}/frames.sock}
playlists: {}
""")
    st = e.ctl.ok("status")
    assert st["mode_name"] == "plasma" and st["ceiling_amps"] == 13.0 and st["button"] == "off"
    assert e.ctl.ok("reload_config")["restart_needed"] == []
