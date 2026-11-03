"""The first-time setup state machine with the fake NetworkManager and a fake wall.
Sleeps are instant and nmcli runs inline, so each test is a deterministic sequence."""
import asyncio
import functools
import os
import time
import stat

from fake_net import FakeNet
from ledposter import net, wifisetup
from ledposter.engine import Engine


class Wall:
    def __init__(self):
        self.texts = []

    async def show_text(self, text, seconds):
        self.texts.append((text, seconds))


class FakeEngineStatus:
    """Just what WifiSetup.loop uses from the Engine client (docs/protocol.md Button fields)."""
    def __init__(self, online=True):
        self.online = online
        self.status = {"wifi_setup_requested": False, "wifi_setup_count": 0, "wifi_setup_at": None,
                       "button_double_count": 0}
        self.acks = 0
    setup_request = staticmethod(Engine.setup_request)

    def hold(self):
        self.status.update(wifi_setup_requested=True, wifi_setup_count=self.status["wifi_setup_count"] + 1, wifi_setup_at=1.7e9)

    async def ack_wifi_setup(self):
        self.acks += 1
        self.status["wifi_setup_requested"] = False
        return True


async def inline(fn, *args):
    return fn(*args)


async def no_sleep(_s):
    await asyncio.sleep(0)


def sync(coro_fn):
    """Run an async test under asyncio.run (no pytest-asyncio dependency)."""
    @functools.wraps(coro_fn)  # keeps the signature so pytest still injects fixtures
    def wrapper(*a, **kw):
        return asyncio.run(coro_fn(*a, **kw))
    return wrapper


def make(tmp_path, connected=True, timeout_s=60):
    f = FakeNet(connected=connected)
    n = net.Net(f.run, str(tmp_path))
    wall = Wall()
    s = wifisetup.WifiSetup(n, wall.show_text, str(tmp_path), timeout_s=timeout_s, sleep=no_sleep, to_thread=inline)
    return f, n, wall, s


def test_setup_password_is_created_once_and_private(tmp_path):
    pw = wifisetup.setup_password(str(tmp_path))
    p = tmp_path / "setup-password"
    assert len(pw) == 10 and p.read_text().strip() == pw and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert wifisetup.setup_password(str(tmp_path)) == pw  # reused, not regenerated
    p.write_text("fromthecard\n")
    assert wifisetup.setup_password(str(tmp_path)) == "fromthecard"


@sync
async def test_boot_without_network_raises_hotspot_after_timeout(tmp_path, monkeypatch):
    f, n, wall, s = make(tmp_path, connected=False, timeout_s=10)
    eng = FakeEngineStatus()
    clock = [0.0]
    monkeypatch.setattr(wifisetup.time, "monotonic", lambda: clock[0])
    t0 = 0.0
    assert await s._boot_tick(t0, eng) is False and not s.state["hotspot_up"]  # still waiting
    clock[0] = 11
    assert await s._boot_tick(t0, eng) is True
    assert s.state["mode"] == "hotspot" and s.state["hotspot_up"] and s.state["trigger"] == "No known Wi-Fi network at boot"
    # scanned before the AP came up (one radio), list cached
    scan_i = next(i for i, c in enumerate(f.calls) if "wifi" in c and "list" in c)
    up_i = next(i for i, c in enumerate(f.calls) if c[-2:] == ["up", "LEDWall-Setup"])
    assert scan_i < up_i and [x["ssid"] for x in s.state["networks"]][:2] == ["Digiana", "Cafe Free"]
    assert f.active == "LEDWall-Setup" and f.profiles["LEDWall-Setup"]["802-11-wireless-security.psk"] == s.password
    assert wall.texts[-1][0].startswith("Wi-Fi setup\nJoin LEDWall-Setup") and "10.42.0.1" in wall.texts[-1][0]
    assert "password: on the card" in wall.texts[-1][0] and s.password not in wall.texts[-1][0]


@sync
async def test_boot_with_network_shows_ip_once_engine_is_up(tmp_path):
    f, n, wall, s = make(tmp_path, connected=True)
    eng = FakeEngineStatus(online=False)
    assert await s._boot_tick(0.0, eng) is False and wall.texts == []  # waits for the engine
    eng.online = True
    assert await s._boot_tick(0.0, eng) is True
    assert wall.texts == [("ledposter.local\n192.168.1.23", 10)] and not s.state["hotspot_up"]


@sync
async def test_double_press_shows_ip(tmp_path):
    f, n, wall, s = make(tmp_path, connected=True)
    eng = FakeEngineStatus()
    eng.status["button_double_count"] = 3  # presses before the web app started: baseline only
    await s._double_tick(eng)
    assert wall.texts == []
    eng.status["button_double_count"] = 4
    await s._double_tick(eng)
    await s._double_tick(eng)  # same count: once
    assert wall.texts == [("ledposter.local\n192.168.1.23", 10)]
    eng.status["button_double_count"] = 0  # engine restarted
    await s._double_tick(eng)
    eng.status["button_double_count"] = 1
    await s._double_tick(eng)
    assert len(wall.texts) == 2
    await s.start("test")  # during setup it repeats the hotspot instructions instead
    eng.status["button_double_count"] = 2
    await s._double_tick(eng)
    assert wall.texts[-1][0].startswith("Wi-Fi setup\nJoin LEDWall-Setup")
    eng.status = None  # engine offline
    await s._double_tick(eng)


@sync
async def test_join_success_turns_hotspot_off(tmp_path):
    f, n, wall, s = make(tmp_path, connected=False)
    await s.start("test")
    assert s.state["hotspot_up"]
    task = s.join("Digiana", "hunter2hunter2", delay=0)
    assert s.state["mode"] == "joining" and s.state["step"] == 1  # returned before anything changed
    await task
    assert s.state["mode"] == "joined" and s.state["step"] == 3 and s.state["ip"] == "192.168.1.23"
    assert not s.state["hotspot_up"] and f.active == "Digiana" and not n.hotspot_active()
    texts = [t for t, _ in wall.texts]
    assert texts[-2] == "Joining\nDigiana..." and texts[-1] == "Joined Digiana\n192.168.1.23\nhttp://ledposter.local"
    # hotspot went down before the join command
    down_i = next(i for i, c in enumerate(f.calls) if c[-2:] == ["down", "LEDWall-Setup"])
    join_i = next(i for i, c in enumerate(f.calls) if "connect" in c)
    assert down_i < join_i


@sync
async def test_join_failure_brings_hotspot_back_with_reason(tmp_path):
    f, n, wall, s = make(tmp_path, connected=False)
    await s.start("test")
    await s.join("Digiana", "wrong-password", delay=0)
    assert s.state["mode"] == "failed" and s.state["reason"] == "Wrong password" and s.state["step"] == 1
    assert s.state["hotspot_up"] and f.active == "LEDWall-Setup"
    assert wall.texts[-1][0] == "Wi-Fi failed\nWrong password\nSetup hotspot is back"
    # try again from the failed state works and ends setup
    await s.join("Digiana", "hunter2hunter2", delay=0)
    assert s.state["mode"] == "joined" and f.active == "Digiana"


@sync
async def test_join_from_settings_without_hotspot(tmp_path):
    f, n, wall, s = make(tmp_path, connected=True)
    await s.join("Cafe Free", None, delay=0)
    assert s.state["mode"] == "joined" and f.active == "Cafe Free" and not s.state["hotspot_up"]
    assert not any(c[-2:] == ["down", "LEDWall-Setup"] for c in f.calls)
    await s.join("Nope", "whatever123", delay=0)
    assert s.state["mode"] == "failed" and s.state["reason"] == "Network not found" and not s.state["hotspot_up"]
    assert wall.texts[-1][0] == "Wi-Fi failed\nNetwork not found"


@sync
async def test_button_request_starts_setup_once_per_change(tmp_path):
    f, n, wall, s = make(tmp_path, connected=True)
    eng = FakeEngineStatus()
    eng.status["wifi_setup_count"] = 3  # earlier holds that were already acked: no trigger
    await s._button_tick(eng)
    await s._button_tick(eng)
    assert not s.state["hotspot_up"] and eng.acks == 0
    eng.hold()
    await s._button_tick(eng)
    assert s.state["hotspot_up"] and s.state["trigger"] == "Button held" and f.active == "LEDWall-Setup"
    assert eng.acks == 1 and eng.status["wifi_setup_requested"] is False  # cleared with ack_wifi_setup
    await s._button_tick(eng)  # nothing pending: no restart
    assert sum(1 for c in f.calls if c[-2:] == ["up", "LEDWall-Setup"]) == 1 and eng.acks == 1
    await s.stop()
    assert not s.state["hotspot_up"] and f.active is None
    # a hold while the hotspot cannot come up is acked once and not retried every poll
    f.errors["connection up LEDWall-Setup"] = (4, "Error: device busy")
    eng.hold()
    await s._button_tick(eng)
    await s._button_tick(eng)
    assert not s.state["hotspot_up"] and eng.acks == 2 and s.state["reason"].startswith("Hotspot failed")


@sync
async def test_rescan_drops_and_restores_hotspot(tmp_path):
    f, n, wall, s = make(tmp_path, connected=False)
    await s.start("test")
    f.networks.append(("Newcomer", 90, "WPA2", "x"))
    nets = await s.rescan()
    assert nets[0]["ssid"] == "Newcomer" and s.state["hotspot_up"] and f.active == "LEDWall-Setup"
    seq = [c[-2:] for c in f.calls if c[-1] == "LEDWall-Setup" and c[-2] in ("up", "down")]
    assert seq == [["up", "LEDWall-Setup"], ["down", "LEDWall-Setup"], ["up", "LEDWall-Setup"]]


@sync
async def test_hotspot_failure_is_reported_not_raised(tmp_path):
    f, n, wall, s = make(tmp_path, connected=False)
    f.errors["connection up LEDWall-Setup"] = (4, "Error: Connection activation failed: device busy")
    await s.start("test")
    assert s.state["mode"] == "off" and not s.state["hotspot_up"] and "Hotspot failed" in s.state["reason"]


@sync
async def test_loop_survives_missing_nmcli(tmp_path):
    def broken(argv, timeout=None):
        raise net.NetError("nmcli is not installed")
    s = wifisetup.WifiSetup(net.Net(broken, str(tmp_path)), Wall().show_text, str(tmp_path), sleep=no_sleep, to_thread=inline)
    assert await s._boot_tick(time.monotonic(), FakeEngineStatus()) is False  # NM may still be starting
    assert await s._boot_tick(0.0, FakeEngineStatus()) is True and s.state["mode"] == "off"
    assert os.path.exists(os.path.join(tmp_path, "setup-password"))
