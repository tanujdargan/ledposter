"""MQTT / Home Assistant bridge (ledposter/mqtt.py) with a fake paho client: no broker needed.
The last test runs against a real mosquitto when one is installed."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import PASSWORD
from ledposter import mqtt
from ledposter.app import create_app

DEV = "ledposter_test"
BASE = f"ledposter/{DEV}"
CFG = {"enabled": True, "host": "broker.local", "port": 1883, "username": "ha", "password": "pw",
       "tls": False, "device_id": DEV}


class RC:
    """Stands in for paho's ReasonCode."""
    def __init__(self, text="Success", failure=False):
        self.text, self.is_failure = text, failure

    def __str__(self):
        return self.text


class FakeClient:
    """The slice of paho.mqtt.client.Client the bridge uses; records what it was asked to do."""
    instances = []

    def __init__(self, client_id):
        self.client_id = client_id
        self.published, self.subscriptions = [], []
        self.auth = self.will = self.target = None
        self.tls = self.running = self.disconnected = False
        self.on_connect = self.on_disconnect = self.on_connect_fail = self.on_message = None
        FakeClient.instances.append(self)

    def username_pw_set(self, username, password=None):
        self.auth = (username, password)

    def tls_set(self):
        self.tls = True

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.target = (host, port)

    def loop_start(self):
        self.running = True

    def loop_stop(self):
        self.running = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topics):
        self.subscriptions.append(topics)

    def publish(self, topic, payload, qos=0, retain=False):
        assert retain and qos == 1
        self.published.append((topic, payload))

    def retained(self):
        return dict(self.published)

    # what paho's network thread would do
    def connect_ok(self):
        self.on_connect(self, None, {}, RC(), None)

    def message(self, topic, payload):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload.encode()))


MODES = [{"id": "pattern:panel-id", "name": "Panel ID", "apply": {"mode": "pattern", "pattern": "panel-id"}, "settings": []},
         {"id": "pattern:solid", "name": "Solid colour", "apply": {"mode": "pattern", "pattern": "solid"},
          "settings": [{"key": "color", "type": "color", "default": "#ffffff"}]},
         {"id": "media", "name": "Media item", "apply": {"mode": "media"}, "settings": [{"key": "media", "type": "media"}]},
         {"id": "playlist", "name": "Playlist", "apply": {"mode": "playlist"},
          "settings": [{"key": "playlist", "type": "select", "options": ["demo"]}]}]


class FakeActions:
    def __init__(self):
        self.calls = []
        self.engine = {"mode": "pattern", "mode_name": "panel-id", "power": True, "amps": 1.234, "lux": 52.6}
        self.online, self.user = True, 40
        self.fail = None

    async def power(self, on):
        self._call("power", on)

    async def brightness(self, value):
        self._call("brightness", value)

    async def mode(self, args):
        self._call("mode", args)

    def _call(self, *c):
        if self.fail:
            raise self.fail
        self.calls.append(c)

    def mode_defs(self):
        return MODES

    def playlists(self):
        return ["demo"]

    async def snapshot(self, fresh=False):
        return {"online": self.online, "engine": dict(self.engine), "brightness": self.user}


def run(coro):
    return asyncio.run(coro)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


async def started(actions=None, cfg=CFG):
    FakeClient.instances.clear()
    b = mqtt.Bridge(actions or FakeActions(), "1.2.3", FakeClient)
    await b.apply(cfg)
    return b, FakeClient.instances[-1]


DEVICE = {"identifiers": [DEV], "name": "LED Poster", "model": "128x192 LED wall", "manufacturer": "ledposter",
          "sw_version": "1.2.3"}
GOLDEN = {
    f"homeassistant/light/{DEV}/light/config": {
        "name": None, "unique_id": f"{DEV}_light", "schema": "json",
        "command_topic": f"{BASE}/light/set", "state_topic": f"{BASE}/light/state",
        "brightness": True, "brightness_scale": 255, "supported_color_modes": ["brightness"],
        "effect": True, "effect_list": ["Panel ID", "Solid colour", "Playlist: demo"],
        "availability_topic": f"{BASE}/availability", "device": DEVICE},
    f"homeassistant/sensor/{DEV}/current/config": {
        "name": "Current draw", "unique_id": f"{DEV}_current", "state_topic": f"{BASE}/sensors",
        "value_template": "{{ value_json.amps }}", "unit_of_measurement": "A", "device_class": "current",
        "state_class": "measurement", "availability_topic": f"{BASE}/availability", "device": DEVICE},
    f"homeassistant/sensor/{DEV}/illuminance/config": {
        "name": "Ambient light", "unique_id": f"{DEV}_illuminance", "state_topic": f"{BASE}/sensors",
        "value_template": "{{ value_json.lux }}", "unit_of_measurement": "lx", "device_class": "illuminance",
        "state_class": "measurement", "availability_topic": f"{BASE}/availability", "device": DEVICE},
    f"homeassistant/select/{DEV}/playlist/config": {
        "name": "Playlist", "unique_id": f"{DEV}_playlist", "icon": "mdi:playlist-play",
        "command_topic": f"{BASE}/playlist/set", "state_topic": f"{BASE}/playlist/state", "options": ["demo"],
        "availability_topic": f"{BASE}/availability", "device": DEVICE},
}


def test_effects_map_modes_and_playlists():
    emap = mqtt.effects(MODES, ["demo", "night"])
    assert emap == {"Panel ID": {"mode": "pattern", "pattern": "panel-id"},
                    "Solid colour": {"mode": "pattern", "pattern": "solid"},
                    "Playlist: demo": {"mode": "playlist", "playlist": "demo"},
                    "Playlist: night": {"mode": "playlist", "playlist": "night"}}
    assert mqtt.current_effect(emap, {"mode": "pattern", "mode_name": "solid"}) == "Solid colour"
    assert mqtt.current_effect(emap, {"mode": "playlist", "mode_name": "night"}) == "Playlist: night"
    assert mqtt.current_effect(emap, {"mode": "media", "mode_name": "x"}) is None
    assert mqtt.current_effect(emap, {"mode": "stream", "mode_name": "ddp"}) is None
    assert mqtt.current_effect(emap, None) is None


def test_device_id_is_stable():
    assert mqtt.device_id({"device_id": "x"}) == "x"
    assert mqtt.device_id({}) == mqtt.device_id({}) and mqtt.device_id({}).startswith("ledposter_")


def test_discovery_and_state_on_connect():
    async def go():
        b, c = await started()
        assert (c.client_id, c.target, c.auth, c.tls, c.running) == (DEV, ("broker.local", 1883), ("ha", "pw"), False, True)
        assert c.will == (f"{BASE}/availability", "offline", 1, True)
        assert b.status == "connecting" and c.published == []
        c.connect_ok()
        await settle()
        assert b.status == "connected"
        assert c.subscriptions == [[(f"{BASE}/light/set", 1), (f"{BASE}/playlist/set", 1)]]
        got = c.retained()
        assert {t: json.loads(got[t]) for t in GOLDEN} == GOLDEN
        assert got[f"{BASE}/availability"] == "online"
        assert json.loads(got[f"{BASE}/light/state"]) == {"state": "ON", "brightness": 102, "color_mode": "brightness",
                                                          "effect": "Panel ID"}
        assert json.loads(got[f"{BASE}/sensors"]) == {"amps": 1.2, "lux": 53}
        assert got[f"{BASE}/playlist/state"] == "None"
        await b.stop()
    run(go())


def test_commands_go_through_actions():
    async def go():
        a = FakeActions()
        b, c = await started(a)
        c.connect_ok()
        await settle()
        c.message(f"{BASE}/light/set", json.dumps({"state": "ON", "brightness": 128, "effect": "Solid colour"}))
        await settle()
        assert a.calls == [("mode", {"mode": "pattern", "pattern": "solid"}),
                           ("brightness", 50), ("power", True)]
        a.calls.clear()
        await b.handle(f"{BASE}/light/set", b'{"state": "OFF"}')
        await b.handle(f"{BASE}/light/set", b'{"brightness": 255}')
        await b.handle(f"{BASE}/light/set", b'{"effect": "Playlist: demo"}')
        await b.handle(f"{BASE}/playlist/set", b"demo")
        assert a.calls == [("power", False), ("brightness", 100), ("mode", {"mode": "playlist", "playlist": "demo"}),
                           ("mode", {"mode": "playlist", "playlist": "demo"})]
        a.calls.clear()
        for bad in (b"not json", b"[1]", b'{"effect": "Nope"}', b'{"brightness": "x"}'):
            await b.handle(f"{BASE}/light/set", bad)
        await b.handle("somewhere/else", b"{}")
        assert a.calls == []

        # A refused command (e.g. engine offline) is logged, and the real state is republished.
        a.fail = RuntimeError("engine offline")
        a.engine["power"] = False
        n = len(c.published)
        await b.handle(f"{BASE}/light/set", b'{"state": "ON"}')
        assert json.loads(c.retained()[f"{BASE}/light/state"])["state"] == "OFF" and len(c.published) == n + 1
        await b.stop()
    run(go())


def test_state_published_on_change_only_then_heartbeat():
    async def go():
        a = FakeActions()
        b, c = await started(a)
        c.connect_ok()
        await settle()
        n = len(c.published)
        await b.publish_all()
        assert len(c.published) == n  # nothing changed
        a.engine.update(mode="playlist", mode_name="demo")
        await b.publish_all()
        new = c.published[n:]
        assert [t for t, _ in new] == [f"{BASE}/light/state", f"{BASE}/playlist/state"]
        assert json.loads(new[0][1])["effect"] == "Playlist: demo" and new[1][1] == "demo"
        a.online = False  # engine down: HA shows the entities unavailable, state is left alone
        await b.publish_all()
        assert c.published[-1] == (f"{BASE}/availability", "offline")
        a.online = True
        n = len(c.published)
        await b.publish_all(force=True)  # heartbeat: everything again
        assert len(c.published) - n == len(GOLDEN) + 4
        await b.stop()
    run(go())


def test_reconnect_resubscribes_and_republishes():
    async def go():
        b, c = await started()
        c.on_connect(c, None, {}, RC("Not authorized", failure=True), None)
        assert b.status == "error: Not authorized" and c.published == []
        c.on_connect_fail(c, None)
        assert b.status.startswith("error: can't reach broker.local:1883")
        c.connect_ok()
        await settle()
        n = len(c.published)
        c.on_disconnect(c, None, {}, RC("Unspecified error"), None)
        assert b.status.startswith("error: connection lost")
        await b.publish_all()
        assert len(c.published) == n  # nothing sent while disconnected
        c.connect_ok()  # paho reconnected by itself
        await settle()
        assert b.status == "connected" and len(c.subscriptions) == 2
        assert len(c.published) - n == len(GOLDEN) + 4
        await b.stop()
        assert c.published[-1] == (f"{BASE}/availability", "offline") and c.disconnected and not c.running
        assert b.status == "disabled"
    run(go())


def test_apply_restarts_only_on_change():
    async def go():
        b, c = await started()
        await b.apply(dict(CFG))
        assert FakeClient.instances == [c]
        await b.apply({**CFG, "tls": True, "port": 8883})
        c2 = FakeClient.instances[-1]
        assert c2 is not c and c.disconnected and c2.tls and c2.target == ("broker.local", 8883)
        await b.apply({**CFG, "enabled": False})
        assert b.status == "disabled" and not c2.running
        await b.apply({**CFG, "host": ""})
        assert b.status == "error: no broker host set"
        await b.stop()
    run(go())


def test_paho_client_builds():
    c = mqtt.paho_client("ledposter_x")
    assert hasattr(c, "connect_async") and hasattr(c, "will_set")


# -- through the web app: HA commands use the REST code paths -----------------------

@pytest.fixture
def ha(data, engine, fakenet):
    cfg = yaml.safe_load((data / "config.yaml").read_text())
    cfg["mqtt"] = dict(CFG)
    cfg["playlists"] = {"demo": {"items": []}}
    cfg["schedule"] = {"quiet": {"enabled": True, "start": "00:00", "end": "23:59", "max_brightness": 25}}
    (data / "config.yaml").write_text(yaml.safe_dump(cfg))
    FakeClient.instances.clear()
    with TestClient(create_app(str(data), net_runner=fakenet.run, mqtt_client_factory=FakeClient)) as c:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
        yield c, FakeClient.instances[-1]


def wait_for(pred, timeout=3):
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end
        time.sleep(0.02)


def test_app_commands_respect_quiet_cap(ha, engine, data):
    client, fake = ha
    assert client.get("/api/status").json()["mqtt"] == "connecting"
    fake.connect_ok()  # from this (non-loop) thread, as paho would
    wait_for(lambda: f"{BASE}/light/state" in fake.retained())
    assert client.get("/api/status").json()["mqtt"] == "connected"
    effects = json.loads(fake.retained()[f"homeassistant/light/{DEV}/light/config"])["effect_list"]
    # from the engine's list_modes (the picker's list): effects, clock/text/idle, patterns, then playlists
    picker = [m["name"] for m in client.get("/api/modes").json() if m["id"] not in ("media", "playlist")]
    assert effects == picker + ["Playlist: demo"]
    assert "Plasma" in effects and "Row sweep" in effects and "Media item" not in effects

    fake.message(f"{BASE}/light/set", json.dumps({"state": "ON", "brightness": 255}))
    wait_for(lambda: any(c["cmd"] == "power" for c in engine.calls))
    assert [c for c in engine.calls if c["cmd"] == "set_brightness"][-1]["value"] == 25  # quiet hours cap
    wait_for(lambda: json.loads(fake.retained()[f"{BASE}/light/state"])["brightness"] == 255)  # the user's 100%

    fake.message(f"{BASE}/light/set", json.dumps({"effect": "Row sweep"}))
    wait_for(lambda: engine.state["mode_name"] == "rows")
    wait_for(lambda: json.loads(fake.retained()[f"{BASE}/light/state"])["effect"] == "Row sweep")
    fake.message(f"{BASE}/playlist/set", "demo")
    wait_for(lambda: fake.retained()[f"{BASE}/playlist/state"] == "demo")
    n = len(engine.calls)
    fake.message(f"{BASE}/playlist/set", "missing")  # the REST check (404) applies
    time.sleep(0.3)
    assert not any(c["cmd"] == "set_mode" for c in engine.calls[n:])

    # Settings: turning MQTT off disconnects and says so.
    s = client.get("/api/settings").json()
    s["mqtt"]["enabled"] = False
    assert client.put("/api/settings", json=s).status_code == 200
    assert fake.disconnected and client.get("/api/status").json()["mqtt"] == "disabled"
    assert yaml.safe_load((data / "config.yaml").read_text())["mqtt"]["device_id"] == DEV  # kept


def test_settings_send_stream_toggle(authed, engine, data):
    s = authed.get("/api/settings").json()
    assert s["ddp"] == {"enabled": False} and s["e131"] == {"enabled": False, "start_universe": 1, "multicast": False}
    s["ddp"]["enabled"] = True
    s["e131"]["start_universe"] = 5
    r = authed.put("/api/settings", json=s)
    assert r.status_code == 200 and r.json()["stream_error"] is None
    assert {"cmd": "set_stream", "ddp": True, "e131": False}.items() <= next(
        c for c in engine.calls if c["cmd"] == "set_stream").items()
    assert engine.calls[-1]["cmd"] == "reload_config"
    cfg = yaml.safe_load((data / "config.yaml").read_text())
    assert cfg["ddp"] == {"enabled": True} and cfg["e131"]["start_universe"] == 5
    n = len(engine.calls)
    authed.put("/api/settings", json=s)  # toggles unchanged: only the reload
    assert [c["cmd"] for c in engine.calls[n:]] == ["reload_config"]


# -- optional: a real broker ---------------------------------------------------------

MOSQUITTO = shutil.which("mosquitto") or next(
    (p for p in ("/opt/homebrew/opt/mosquitto/sbin/mosquitto", "/usr/local/opt/mosquitto/sbin/mosquitto",
                 "/usr/sbin/mosquitto") if os.path.exists(p)), None)


@pytest.mark.skipif(not MOSQUITTO, reason="mosquitto not installed")
def test_real_broker(tmp_path):
    import paho.mqtt.client as paho
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = tmp_path / "m.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    broker = subprocess.Popen([MOSQUITTO, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        end = time.monotonic() + 5
        while socket.socket().connect_ex(("127.0.0.1", port)):
            assert time.monotonic() < end
            time.sleep(0.05)
        seen = {}
        ha = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="fake-ha")
        ha.on_message = lambda c, u, m: seen.__setitem__(m.topic, m.payload.decode())
        ha.connect("127.0.0.1", port)
        ha.subscribe([("homeassistant/#", 0), ("ledposter/#", 0)])
        ha.loop_start()

        async def go():
            a = FakeActions()
            b = mqtt.Bridge(a, "1.2.3")
            await b.apply({**CFG, "host": "127.0.0.1", "port": port, "username": ""})
            for _ in range(100):
                if f"{BASE}/light/state" in seen:
                    break
                await asyncio.sleep(0.05)
            assert b.status == "connected" and seen[f"{BASE}/availability"] == "online"
            assert json.loads(seen[f"homeassistant/light/{DEV}/light/config"]) == GOLDEN[f"homeassistant/light/{DEV}/light/config"]
            ha.publish(f"{BASE}/light/set", '{"state": "OFF"}', qos=1)
            for _ in range(100):
                if a.calls:
                    break
                await asyncio.sleep(0.05)
            assert a.calls == [("power", False)]
            await b.stop()
            for _ in range(100):
                if seen[f"{BASE}/availability"] == "offline":
                    break
                await asyncio.sleep(0.05)
            assert seen[f"{BASE}/availability"] == "offline"
        run(go())
        ha.loop_stop()
        ha.disconnect()
    finally:
        broker.terminate()
        broker.wait(5)
