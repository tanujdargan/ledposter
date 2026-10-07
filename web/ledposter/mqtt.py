"""MQTT client with Home Assistant discovery (spec: "Home Assistant and MQTT").

Entities, all on one HA device: a `light` (JSON schema: on/off, brightness, effect =
modes and playlists), sensors for current draw and ambient light, and a `select` for
the playlist. Commands from HA call the same functions as the REST API, so the
quiet-hours cap and the media/playlist checks still apply.

paho runs its network loop (and reconnects) on its own thread; its callbacks hand
work to the app's asyncio loop. Discovery, availability and state are retained.
State goes out when it changes (checked every second against the engine status
poller's copy), right after a command, and every HEARTBEAT_S.
"""
import asyncio
import json
import logging
import time
import uuid

log = logging.getLogger("ledposter.mqtt")
HEARTBEAT_S = 60


def paho_client(client_id):
    import paho.mqtt.client as mqtt
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


def device_id(cfg):
    """Stable across restarts: from the config, else the network card's MAC."""
    return cfg.get("device_id") or f"ledposter_{uuid.getnode():012x}"


def effects(mode_defs, playlists):
    """HA effect name -> set_mode args. The one place modes become effects: mode_defs is the
    app's picker list (built from the engine's list_modes). Every mode that needs no choice
    from the user (so not the media item and playlist pickers), at the engine's default
    settings, plus one effect per playlist."""
    out = {}
    for m in mode_defs:
        settings = m.get("settings") or []
        if any(s["type"] in ("media", "select") and s.get("default") is None for s in settings):
            continue
        out[m["name"]] = dict(m["apply"])
    for p in playlists:
        out[f"Playlist: {p}"] = {"mode": "playlist", "playlist": p}
    return out


def current_effect(effect_map, status):
    """The effect matching the engine's mode and mode_name, or None."""
    if not status:
        return None
    for name, args in effect_map.items():
        kind = args["mode"]
        if status.get("mode") == kind and (kind not in args or status.get("mode_name") == args[kind]):
            return name
    return None


class Topics:
    def __init__(self, cfg, dev_id):
        base = f"{cfg.get('base_topic') or 'ledposter'}/{dev_id}"
        self.prefix = cfg.get("discovery_prefix") or "homeassistant"
        self.availability = f"{base}/availability"
        self.light_state, self.light_set = f"{base}/light/state", f"{base}/light/set"
        self.sensors = f"{base}/sensors"
        self.playlist_state, self.playlist_set = f"{base}/playlist/state", f"{base}/playlist/set"


def discovery(t, dev_id, cfg, version, effect_names, playlists):
    """HA discovery topic -> config payload."""
    common = {"availability_topic": t.availability,
              "device": {"identifiers": [dev_id], "name": cfg.get("device_name") or "LED Poster",
                         "model": "128x192 LED wall", "manufacturer": "ledposter", "sw_version": version}}
    p = f"{t.prefix}/{{}}/{dev_id}/{{}}/config"
    return {
        p.format("light", "light"): {
            "name": None, "unique_id": f"{dev_id}_light", "schema": "json",
            "command_topic": t.light_set, "state_topic": t.light_state,
            "brightness": True, "brightness_scale": 255, "supported_color_modes": ["brightness"],
            "effect": True, "effect_list": effect_names, **common},
        p.format("sensor", "current"): {
            "name": "Current draw", "unique_id": f"{dev_id}_current", "state_topic": t.sensors,
            "value_template": "{{ value_json.amps }}", "unit_of_measurement": "A",
            "device_class": "current", "state_class": "measurement", **common},
        p.format("sensor", "illuminance"): {
            "name": "Ambient light", "unique_id": f"{dev_id}_illuminance", "state_topic": t.sensors,
            "value_template": "{{ value_json.lux }}", "unit_of_measurement": "lx",
            "device_class": "illuminance", "state_class": "measurement", **common},
        p.format("select", "playlist"): {
            "name": "Playlist", "unique_id": f"{dev_id}_playlist", "icon": "mdi:playlist-play",
            "command_topic": t.playlist_set, "state_topic": t.playlist_state, "options": playlists, **common},
    }


def states(t, snap, effect_map):
    """State topic -> payload, from snapshot {"engine": status or None, "brightness": user %}.
    "None" is HA's word for unknown (no reading, not playing a playlist)."""
    st = snap["engine"] or {}
    light = {"state": "ON" if st.get("power") else "OFF", "brightness": round(snap["brightness"] * 255 / 100),
             "color_mode": "brightness", "effect": current_effect(effect_map, st)}
    sensors = {"amps": round(st["amps"], 1) if st.get("amps") is not None else None,
               "lux": round(st["lux"]) if st.get("lux") is not None else None}
    return {t.light_state: json.dumps(light), t.sensors: json.dumps(sensors),
            t.playlist_state: st["mode_name"] if st.get("mode") == "playlist" else "None"}


class Bridge:
    """`actions` is the app's shared control code: async power(on), brightness(value 0-100),
    mode(args) (raise on failure); mode_defs() and playlists() for the effect list; async
    snapshot(fresh) -> {"online", "engine", "brightness"}."""

    def __init__(self, actions, version, client_factory=paho_client):
        self.a, self.version, self.factory = actions, version, client_factory
        self.cfg = None
        self.client = None
        self.status = "disabled"  # shown in Settings: disabled, connecting, connected, error: ...
        self.loop = None
        self._published = {}
        self._task = None
        self._last_full = 0.0

    # -- lifecycle ----------------------------------------------------------------

    async def apply(self, cfg):
        """(Re)start with the config's `mqtt:` section; no-op when nothing changed."""
        cfg = dict(cfg)
        if cfg == self.cfg:
            return
        await self.stop()
        self.cfg = cfg
        if not cfg.get("enabled"):
            return
        if not cfg.get("host"):
            self.status = "error: no broker host set"
            return
        self.loop = asyncio.get_running_loop()
        self.dev_id = device_id(cfg)
        self.t = Topics(cfg, self.dev_id)
        try:
            c = self.factory(self.dev_id)
            if cfg.get("username"):
                c.username_pw_set(cfg["username"], cfg.get("password") or None)
            if cfg.get("tls"):
                c.tls_set()  # system CA store
            c.will_set(self.t.availability, "offline", qos=1, retain=True)
            c.on_connect, c.on_disconnect, c.on_connect_fail = self._on_connect, self._on_disconnect, self._on_connect_fail
            c.on_message = self._on_message
            c.reconnect_delay_set(1, 60)
            self.client, self.status = c, "connecting"  # before the callbacks can run
            c.connect_async(cfg["host"], int(cfg.get("port") or 1883), keepalive=60)
            c.loop_start()
        except Exception as e:  # bad TLS setup, unresolvable options
            self.client, self.status = None, f"error: {e}"
            log.warning("mqtt: %s", e)
            return
        self._task = asyncio.create_task(self._publisher())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        c, self.client = self.client, None
        if c:
            if self.status == "connected":  # a clean disconnect doesn't fire the LWT
                c.publish(self.t.availability, "offline", qos=1, retain=True)
            c.disconnect()
            await asyncio.to_thread(c.loop_stop)
        self._published.clear()
        self.cfg, self.status = None, "disabled"

    # -- paho callbacks (paho's thread) -------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if client is not self.client:
            return
        if getattr(reason_code, "is_failure", reason_code != 0):
            self.status = f"error: {reason_code}"
            log.warning("mqtt: broker refused the connection: %s", reason_code)
            return
        self.status = "connected"
        log.info("mqtt: connected to %s:%s", self.cfg["host"], self.cfg.get("port"))
        client.subscribe([(self.t.light_set, 1), (self.t.playlist_set, 1)])  # again after every reconnect
        asyncio.run_coroutine_threadsafe(self.publish_all(force=True), self.loop)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if client is self.client:
            self.status = f"error: connection lost ({reason_code}), reconnecting"
            self._published.clear()

    def _on_connect_fail(self, client, userdata):
        if client is self.client:
            self.status = f"error: can't reach {self.cfg['host']}:{self.cfg.get('port') or 1883}, retrying"

    def _on_message(self, client, userdata, msg):
        asyncio.run_coroutine_threadsafe(self.handle(msg.topic, msg.payload), self.loop)

    # -- commands -----------------------------------------------------------------

    def effect_map(self):
        return effects(self.a.mode_defs(), self.a.playlists())

    async def handle(self, topic, payload):
        """One command from HA. Failures are logged; the state is then republished so
        HA shows what really happened."""
        try:
            text = payload.decode() if isinstance(payload, bytes) else str(payload)
            if topic == self.t.light_set:
                cmd = json.loads(text)
                if not isinstance(cmd, dict):
                    raise ValueError("expected a JSON object")
                if "effect" in cmd:
                    args = self.effect_map().get(cmd["effect"])
                    if args is None:
                        raise ValueError(f"unknown effect {cmd['effect']!r}")
                    await self.a.mode(dict(args))
                if "brightness" in cmd:
                    await self.a.brightness(max(0, min(100, round(float(cmd["brightness"]) * 100 / 255))))
                if cmd.get("state") in ("ON", "OFF"):
                    await self.a.power(cmd["state"] == "ON")
            elif topic == self.t.playlist_set:
                await self.a.mode({"mode": "playlist", "playlist": text})
            else:
                return
        except Exception as e:  # HTTPException from the shared actions, EngineError, bad JSON
            log.warning("mqtt: command on %s failed: %s", topic, getattr(e, "detail", e))
        await self.publish_all(fresh=True)

    # -- publishing ---------------------------------------------------------------

    def _send(self, topic, payload, force):
        if force or self._published.get(topic) != payload:
            self.client.publish(topic, payload, qos=1, retain=True)
            self._published[topic] = payload

    async def publish_all(self, force=False, fresh=False):
        if not self.client or self.status != "connected":
            return
        snap = await self.a.snapshot(fresh or force)
        playlists = list(self.a.playlists())
        emap = effects(self.a.mode_defs(), playlists)
        for topic, payload in discovery(self.t, self.dev_id, self.cfg, self.version, list(emap), playlists).items():
            self._send(topic, json.dumps(payload), force)
        self._send(self.t.availability, "online" if snap["online"] else "offline", force)
        if snap["online"]:
            for topic, payload in states(self.t, snap, emap).items():
                self._send(topic, payload, force)
        if force:
            self._last_full = time.monotonic()

    async def _publisher(self):
        while True:
            await asyncio.sleep(1)
            try:
                await self.publish_all(force=time.monotonic() - self._last_full > HEARTBEAT_S)
            except Exception:
                log.exception("mqtt: publish")
