"""FastAPI app: auth, REST API (OpenAPI at /docs), preview WebSocket relay, uploads,
config writes and the schedule task. Everything the UI does goes through /api/...
"""
import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import time
from types import SimpleNamespace
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from python_multipart import MultipartParser

from . import __version__, auth, config, mqtt, net, schedule, sysapi, system, wifisetup
from .engine import Engine, EngineError, EngineOffline, mode_args
from .media import Library
from .transcode import MAX_BYTES, SUPPORTED

log = logging.getLogger("ledposter")
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
# Connectivity probes phones and laptops send when they join a network. Answered with a
# redirect to the setup page while the hotspot is up, so the OS opens its captive portal.
CAPTIVE_PROBES = ["/hotspot-detect.html", "/generate_204", "/gen_204", "/connecttest.txt", "/ncsi.txt", "/redirect",
                  "/success.txt", "/canonical.html", "/library/test/success.html", "/check_network_status.txt"]
PATTERNS = [("panel-id", "Panel ID"), ("solid", "Solid colour"), ("cycle", "Colour cycle"),
            ("rows", "Row sweep"), ("cols", "Column sweep")]
EVENTS = ["kill", "killstreak", "death", "win"]  # game hook overlays (spec: Game hook)
EVENT_LIMIT, EVENT_WINDOW_S = 10, 5  # per caller: plenty for a game, stops a runaway script
# Friendlier labels for engine setting names (list_modes, docs/protocol.md); others are capitalised.
SETTING_LABELS = {"hour24": "24-hour clock", "seconds": "Show seconds", "date": "Show date",
                  "clock": "Show the time", "level": "Brightness", "count": "Ripples",
                  "background": "Background", "color": "Colour", "colors": "Colours", "text": "Text",
                  "scale": "Size", "density": "Density", "strands": "Strands", "rings": "Rings",
                  "blips": "Blips", "height": "Flame height", "speed": "Speed", "font": "Font"}
DESCRIPTION = """REST API for the LED wall. Every action in the web app goes through these endpoints.

Authentication: a session cookie (`POST /api/auth/login`) or an API token created in
Settings, sent as `Authorization: Bearer lp_...`.

Live preview: WebSocket `/api/preview` (same auth; tokens may be passed as `?token=`).
Each frame arrives as a text message with the engine status JSON (`mode`, `fps`,
`amps`, `limited`, `w`, `h`, ...) followed by one binary message of `w*h*3` RGB bytes,
at most 15 per second. While the engine is offline the text message has `"online": false`.
"""


# -- models --------------------------------------------------------------------------

class AuthState(BaseModel):
    first_run: bool = Field(description="No admin password yet: POST /api/auth/setup is open")
    authenticated: bool


class Password(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class PasswordChange(BaseModel):
    current: str
    new: str = Field(min_length=8, max_length=256)


class Power(BaseModel):
    on: bool


class Brightness(BaseModel):
    value: int = Field(ge=0, le=100)


class AutoBrightness(BaseModel):
    enabled: bool


class ModeRequest(BaseModel):
    """`mode` plus that mode's fields (`pattern`, `color`, `media`, `playlist`, ...).
    Extra fields are passed to the engine unchanged, so new modes need no API change."""
    model_config = ConfigDict(extra="allow")
    mode: str


class ModeSetting(BaseModel):
    key: str
    label: str
    type: Literal["select", "color", "colors", "media", "slider", "text", "toggle"] = Field(
        description="colors: a list of '#rrggbb' with min..max entries; text: up to max characters")
    options: Optional[list[str]] = None
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    help: Optional[str] = None


class ModeDef(BaseModel):
    id: str
    name: str
    group: str
    apply: dict[str, Any] = Field(description="Base fields of the set_mode request")
    settings: list[ModeSetting] = []
    help: Optional[str] = None


class EventRequest(BaseModel):
    event: str = Field(description="kill, killstreak, death or win", examples=["kill"])


class EventResult(BaseModel):
    ok: bool = True
    event: str
    ms: int = Field(description="How long the overlay shows")


class CanvasSize(BaseModel):
    w: int
    h: int


class Status(BaseModel):
    online: bool
    engine: Optional[dict[str, Any]] = Field(None, description="Last engine status (docs/protocol.md)")
    brightness: int = Field(description="The user's brightness setting, before the quiet-hours cap")
    quiet_cap: Optional[int] = Field(None, description="Active quiet-hours brightness cap, if any")
    canvas: CanvasSize
    mqtt: str = Field(description="MQTT connection: disabled, connecting, connected or \"error: ...\"")
    version: str
    timezone: str
    time: str


class MediaSettings(BaseModel):
    scale: Literal["fit", "fill", "stretch"] = "fit"
    filter: Optional[Literal["smooth", "pixel"]] = Field(None, description="None = auto: pixel for native-size sources")
    crop: Optional[list[int]] = Field(None, description="x, y, w, h in source pixels", min_length=4, max_length=4)
    position: list[float] = Field([0.5, 0.5], description="Anchor for fit/fill, 0..1", min_length=2, max_length=2)
    speed: float = Field(1.0, ge=0.1, le=8)
    dither: bool = False

    @field_validator("position")
    @classmethod
    def _pos(cls, v):
        if not all(0 <= x <= 1 for x in v):
            raise ValueError("position must be within 0..1")
        return v

    @field_validator("crop")
    @classmethod
    def _crop(cls, v):
        if v is not None and (v[0] < 0 or v[1] < 0 or v[2] <= 0 or v[3] <= 0):
            raise ValueError("crop needs x,y >= 0 and w,h > 0")
        return v


class MediaInfo(BaseModel):
    w: int
    h: int
    frames: int
    duration_ms: int
    filter: str
    source_w: int
    source_h: int
    format: str


class MediaItem(BaseModel):
    id: str
    name: str
    ext: str
    size: int
    created: float
    status: Literal["uploading", "queued", "transcoding", "ready", "failed"]
    error: Optional[str] = Field(None, description="Failed transcodes: the reason, verbatim")
    kind: Literal["image", "gif", "video", "pixel"]
    settings: MediaSettings
    info: Optional[MediaInfo] = None


class MediaPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    settings: Optional[MediaSettings] = Field(None, description="Changing settings re-transcodes the item")


class PlaylistItem(BaseModel):
    media: str
    duration_s: Optional[float] = Field(None, gt=0, description="Default: one full play (10 s for stills)")


class Playlist(BaseModel):
    items: list[PlaylistItem] = []
    loop: bool = True
    shuffle: bool = False


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = Field("22:00", pattern=TIME_RE)
    end: str = Field("07:00", pattern=TIME_RE)
    max_brightness: int = Field(20, ge=0, le=100)


class Schedule(BaseModel):
    enabled: bool = False
    on: str = Field("07:00", pattern=TIME_RE)
    off: str = Field("23:00", pattern=TIME_RE)
    days: list[int] = Field([0, 1, 2, 3, 4, 5, 6], description="Weekdays the on time applies, 0 = Monday")
    quiet: QuietHours = QuietHours()

    @field_validator("days")
    @classmethod
    def _days(cls, v):
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days are 0..6")
        return sorted(set(v))


class Panel(BaseModel):
    col: int = Field(ge=0, le=7)
    row: int = Field(ge=0, le=7)
    rotate: Literal[0, 90, 180, 270] = 0


class MatrixSettings(BaseModel):
    hardware_mapping: Literal["adafruit-hat-pwm", "adafruit-hat"]
    panel_size: int = Field(ge=4, le=64)
    gpio_slowdown: int = Field(ge=0, le=10)
    pwm_bits: int = Field(ge=1, le=11)
    panels: list[Panel] = Field(min_length=1, max_length=16, description="Chain order; first is on the bonnet")


class PowerSettings(BaseModel):
    ceiling_amps: float = Field(gt=0, le=config.MAX_CEILING_AMPS, description="The supply's panel budget is 15A (docs/spec.md)")
    amps_full_white_per_panel: float = Field(gt=0, le=20)


class Mqtt(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = Field(1883, ge=1, le=65535)
    username: str = ""
    password: str = ""
    tls: bool = Field(False, description="TLS with the system CA store (usually port 8883)")


class Ddp(BaseModel):
    enabled: bool = False


class E131(BaseModel):
    enabled: bool = False
    start_universe: int = Field(1, ge=1, le=63999, description="Universe of the top-left pixel; 170 pixels per universe")
    multicast: bool = Field(False, description="Also join the multicast groups (unicast needs nothing)")


class StreamSettings(BaseModel):
    apply_gamma: bool = Field(True, description="Treat incoming colours as sRGB and apply the wall's gamma")


class Settings(BaseModel):
    matrix: MatrixSettings
    power: PowerSettings
    mqtt: Mqtt = Field(description="MQTT broker for Home Assistant discovery")
    ddp: Ddp = Field(description="DDP input on UDP 4048 (WLED, LedFx, xLights)")
    e131: E131 = Field(E131(), description="E1.31/sACN input on UDP 5568")
    stream: StreamSettings = StreamSettings()


class SettingsSaved(Settings):
    restart_required: bool = Field(description="Matrix options only apply when the engine restarts")
    stream_error: Optional[str] = Field(None, description="The engine couldn't open a DDP/E1.31 port")


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class TokenInfo(BaseModel):
    id: str
    name: str
    created: float


class TokenCreated(TokenInfo):
    token: str = Field(description="Shown once; store it now")


class Ok(BaseModel):
    ok: bool = True


# -- mode picker from the engine's list_modes ----------------------------------------

def _label(name):
    return SETTING_LABELS.get(name, name.replace("_", " ").capitalize())


def _hex(c):
    return f"#{c}" if isinstance(c, str) and not c.startswith("#") else c


def setting_def(s):
    """One engine setting schema (docs/protocol.md, list_modes) as a picker control."""
    t, name = s["type"], s["name"]
    out = {"key": name, "label": _label(name), "help": s.get("help") or None, "default": s.get("default")}
    if t in ("number", "int"):
        span = s["max"] - s["min"]
        out.update(type="slider", min=s["min"], max=s["max"],
                   step=1 if t == "int" else 0.05 if span <= 2 else 0.1)
    elif t == "bool":
        out.update(type="toggle")
    elif t == "color":
        out.update(type="color", default=_hex(s.get("default")))
    elif t == "colors":
        out.update(type="colors", min=s["min"], max=s["max"], default=[_hex(c) for c in s.get("default") or []])
    elif t == "string":
        out.update(type="text", max=s.get("max"))
    elif t == "choice":
        out.update(type="select", options=list(s.get("choices") or []))
    else:
        return None  # a type this app doesn't know yet: leave it at the engine's default
    return ModeSetting(**out)


def mode_defs(engine_modes, playlists):
    """The picker's modes: from the engine's list_modes when known, else the M4 basics."""
    media = [ModeDef(id="media", name="Media item", group="Media", apply={"mode": "media"},
                     settings=[ModeSetting(key="media", label="Item", type="media")]),
             ModeDef(id="playlist", name="Playlist", group="Media", apply={"mode": "playlist"},
                     settings=[ModeSetting(key="playlist", label="Playlist", type="select", options=playlists)])]
    names = dict(PATTERNS)
    patterns = [ModeDef(id=f"pattern:{p}", name=n, group="Test patterns", apply={"mode": "pattern", "pattern": p},
                        settings=[ModeSetting(key="color", label="Colour", type="color", default="#ffffff")]
                        if p == "solid" else []) for p, n in PATTERNS]
    if not engine_modes:
        return media + patterns
    out, effects, other = list(media), [], []
    for m in engine_modes:
        kind = m.get("mode")
        settings = [d for d in (setting_def(s) for s in m.get("settings", [])) if d]
        if kind == "effect":
            effects.append(ModeDef(id=f"effect:{m['effect']}", name=m.get("label") or m["effect"], group="Effects",
                                   apply={"mode": "effect", "effect": m["effect"]}, settings=settings,
                                   help=m.get("help")))
        elif kind in ("clock", "text", "idle"):
            other.append(ModeDef(id=kind, name=m.get("label") or kind, group="Clock and text", apply={"mode": kind},
                                 settings=settings, help=m.get("help")))
        elif kind == "pattern":
            choices = next((s.get("choices") for s in m.get("settings", []) if s["name"] == "pattern"), None)
            color = next((d for d in settings if d.key == "color"), None)
            if choices:
                patterns = [ModeDef(id=f"pattern:{p}", name=names.get(p, p), group="Test patterns",
                                    apply={"mode": "pattern", "pattern": p},
                                    settings=[color] if color and p in ("solid", "rows", "cols") else [])
                            for p in choices]
    return out + effects + other + patterns
class Rollback(BaseModel):
    address: str
    seconds_left: int = Field(description="Time left to confirm before the wall goes back to DHCP")


class NetworkStatus(BaseModel):
    device: Optional[str] = None
    type: Optional[Literal["wifi", "ethernet"]] = None
    connection: Optional[str] = Field(None, description="NetworkManager profile name")
    state: str
    mac: Optional[str] = Field(None, description="For a DHCP reservation in the router")
    address: Optional[str] = None
    prefix: Optional[int] = None
    gateway: Optional[str] = None
    dns: list[str] = []
    method: Optional[str] = Field(None, description="dhcp or static")
    ssid: Optional[str] = None
    signal: Optional[int] = Field(None, description="0 to 100")
    hostname: str
    hotspot: bool = Field(description="The setup hotspot is what is up, not a network")
    rollback: Optional[Rollback] = Field(None, description="A static IP is waiting to be confirmed")
    setup_mode: bool = Field(description="Setup hotspot is up")


class WifiNetwork(BaseModel):
    ssid: str
    signal: int
    security: str
    secured: bool
    enterprise: bool = Field(description="802.1X: cannot be joined with a password")


class WifiList(BaseModel):
    networks: list[WifiNetwork]
    scanned_at: Optional[float] = None
    current: Optional[str] = Field(None, description="SSID the wall is on now")
    setup_mode: bool


class WifiJoin(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    password: Optional[str] = Field(None, max_length=63, description="Omit or empty for an open network")
    hidden: bool = False


class SetupState(BaseModel):
    mode: Literal["off", "starting", "hotspot", "scanning", "joining", "joined", "failed"]
    hotspot_up: bool
    step: int = Field(description="0 hotspot, 1 joining, 2 getting IP, 3 done")
    target: Optional[str] = Field(None, description="SSID being joined")
    reason: Optional[str] = Field(None, description="Why the last join failed")
    ip: Optional[str] = None
    hostname: Optional[str] = None
    trigger: Optional[str] = Field(None, description="Why setup mode started (boot, button, Settings)")
    ssid: str = Field(description="Hotspot SSID")
    hotspot_ip: str
    first_run: bool
    authenticated: bool


class StaticIp(BaseModel):
    address: str
    prefix: int = Field(ge=8, le=30)
    gateway: Optional[str] = Field(None, description="Default: the current gateway")
    dns: list[str] = Field([], max_length=3, description="Default: the gateway")


class StaticApplied(BaseModel):
    address: str
    url: str = Field(description="Open this within the deadline and confirm, or the wall reverts to DHCP")
    seconds: int


class Hostname(BaseModel):
    hostname: str = Field(min_length=1, max_length=63)


class HostnameSet(BaseModel):
    hostname: str
    url: str


# -- app -----------------------------------------------------------------------------

def create_app(data_dir=None, net_runner=None, mqtt_client_factory=None, git_runner=None):
    """`net_runner` replaces the subprocess runner for nmcli & co (tests, LEDPOSTER_FAKE_NET);
    `mqtt_client_factory` the paho client (tests); `git_runner` the one for `git ls-remote`
    (checking for updates)."""
    data_dir = data_dir or config.data_dir()
    app = FastAPI(title="ledposter", version=__version__, description=DESCRIPTION,
                  openapi_tags=[{"name": "auth"}, {"name": "control"}, {"name": "media"},
                                {"name": "playlists"}, {"name": "schedule"}, {"name": "settings"}, {"name": "network"},
                                {"name": "system"}])
    st = app.state
    st.data_dir = data_dir
    st.cfg_path = os.path.join(data_dir, "config.yaml")
    st.cfg = config.load(st.cfg_path)
    st.sessions = auth.Sessions(auth.load_secret(data_dir))
    st.limiter = auth.RateLimiter()
    st.engine = Engine(st.cfg["engine"]["socket"], st.cfg["engine"]["preview_socket"])
    st.library = Library(data_dir, lambda: config.canvas_size(st.cfg), lambda: float(st.cfg["display"]["gamma"]))
    st.save_task = None
    st.engine_modes = None  # last list_modes reply; the picker falls back to it while the engine is offline
    st.event_limiter = auth.RateLimiter(limit=EVENT_LIMIT, window=EVENT_WINDOW_S)
    ncfg = st.cfg["network"]
    st.net = net.Net(net_runner or net.run_subprocess, data_dir, hotspot_ssid=ncfg["hotspot_ssid"],
                     hotspot_ip=ncfg["hotspot_ip"], device=ncfg.get("wifi_device") or None)
    st.setup = wifisetup.WifiSetup(st.net, st.engine.show_text, data_dir, ssid=ncfg["hotspot_ssid"],
                                   timeout_s=float(ncfg["setup_timeout_s"]), show_ip_s=int(ncfg["show_ip_s"]))
    st.net_task = None  # pending static/DHCP apply, delayed so the browser gets its response first
    # M8: root-side units (firewall, SSH, update, factory reset) and the last-mode restore
    st.system = system.System(net_runner or net.run_subprocess, data_dir, hotspot_iface=st.net.wifi_device,
                              git=git_runner)

    def cfg_mtime():
        try:
            return os.stat(st.cfg_path).st_mtime_ns
        except OSError:
            return None
    st.cfg_mtime = cfg_mtime()

    def save():
        config.save(st.cfg, st.cfg_path)
        st.cfg_mtime = cfg_mtime()
    st.save = save

    @app.middleware("http")
    async def reload_edited_config(request, call_next):
        # A hand edit of config.yaml (or the installer) must win over the copy loaded at
        # startup; otherwise the next save from the app silently writes the old values back.
        m = cfg_mtime()
        if m is not None and m != st.cfg_mtime:
            st.cfg, st.cfg_mtime = config.load(st.cfg_path), m
            log.info("config.yaml changed on disk; reloaded")
        return await call_next(request)

    def save_soon(delay=1.5):
        """Coalesces slider drags into one write."""
        async def later():
            await asyncio.sleep(delay)
            save()
        if st.save_task and not st.save_task.done():
            st.save_task.cancel()
        st.save_task = asyncio.create_task(later())

    async def engine_send(cmd, **args):
        """Best-effort command for config-driven changes; False if the engine is offline."""
        try:
            await st.engine.call(cmd, **args)
            return True
        except EngineError as e:
            log.info("%s not applied: %s", cmd, e)
            return False

    st.scheduler = schedule.Scheduler(lambda: st.cfg["schedule"], lambda: st.cfg["brightness"]["user"],
                                      engine_send)
    st.lastmode = system.LastMode(lambda: st.cfg.get("last_mode"), engine_send,
                                  lambda mid: (st.library.get(mid) or {}).get("status") == "ready")

    async def lastmode_loop():
        """The engine boots on its boot screen; send it the last mode once it is up."""
        while True:
            try:
                await st.lastmode.tick(st.engine.status if st.engine.online else None)
            except Exception:
                log.exception("last mode restore")
            await asyncio.sleep(2)

    async def reload():
        """reload_config after a config write. The engine re-reads brightness.user from the
        file, so the quiet-hours clamp is sent again. Returns the engine's restart_needed
        list, or None when it was unreachable."""
        try:
            r = await st.engine.call("reload_config")
        except EngineError as e:
            log.info("reload_config not applied: %s", e)
            return None
        if st.scheduler.cap is not None:
            await engine_send("set_brightness", value=schedule.clamp(st.cfg["brightness"]["user"], st.scheduler.cap))
        return r.get("restart_needed") or []

    async def schedule_loop():
        while True:
            try:
                await st.scheduler.tick()
            except Exception:
                log.exception("schedule tick")
            await asyncio.sleep(15)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await st.engine.start()
        await st.library.start()
        try:  # firewall/SSH inputs follow the config; apply them if they changed while we were down
            await st.system_sync()
        except Exception as e:
            log.warning("system files not synced: %s", e)
        tasks = [asyncio.create_task(schedule_loop()), asyncio.create_task(st.setup.loop(st.engine)),
                 asyncio.create_task(lastmode_loop())]
        await st.mqtt.apply(st.cfg["mqtt"])
        yield
        await st.mqtt.stop()
        for task in tasks:
            task.cancel()
        if st.save_task and not st.save_task.done():
            st.save_task.cancel()
            save()
        await st.library.stop()
        await st.engine.stop()

    app.router.lifespan_context = lifespan

    # -- auth helpers ----------------------------------------------------------------

    def first_run():
        return not st.cfg["web"].get("admin_password")

    def principal(auth_header, cookie, token_param=None):
        """"session", the token's name, or None."""
        if first_run():
            return None
        bearer = None
        if auth_header and auth_header.lower().startswith("bearer "):
            bearer = auth_header[7:].strip()
        bearer = bearer or token_param
        if bearer:
            t = auth.find_token(st.cfg["web"]["tokens"], bearer)
            return f"token:{t['name']}" if t else None
        if cookie and st.sessions.verify(cookie, st.cfg["web"]["admin_password"]):
            return "session"
        return None

    def require_auth(request: Request):
        who = principal(request.headers.get("authorization"), request.cookies.get("session"))
        if not who:
            raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
        return who

    def setup_auth(request: Request):
        """Setup endpoints: normal auth, or open while the hotspot is up on a wall that has no
        admin password yet (only someone with the card's hotspot password can reach it)."""
        who = principal(request.headers.get("authorization"), request.cookies.get("session"))
        if who:
            return who
        if st.setup.state["hotspot_up"] and first_run():
            return "setup"
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})

    def set_session(response):
        response.set_cookie("session", st.sessions.issue(st.cfg["web"]["admin_password"]),
                            max_age=auth.SESSION_TTL, httponly=True, samesite="strict", path="/")

    async def engine_call(cmd, **args):
        try:
            return await st.engine.call(cmd, **args)
        except EngineOffline:
            raise HTTPException(503, "Engine offline")
        except EngineError as e:
            raise HTTPException(502, str(e))

    def media_item(item):
        return MediaItem(**item, kind=Library.kind(item))

    # -- shell -----------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        if not principal(None, request.cookies.get("session")):
            return RedirectResponse("/login", 302)
        return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if principal(None, request.cookies.get("session")):
            return RedirectResponse("/", 302)
        return FileResponse(os.path.join(STATIC, "login.html"), headers={"Cache-Control": "no-store"})

    @app.get("/setup", include_in_schema=False)
    def setup_page():
        """Captive portal / change-network wizard. The page is public like /login; the data
        behind it is gated by setup_auth."""
        return FileResponse(os.path.join(STATIC, "setup.html"), headers={"Cache-Control": "no-store"})

    def server_url(request, host):
        port = (request.scope.get("server") or ("", 80))[1] or 80
        return f"http://{host}" + ("" if int(port) == 80 else f":{port}")

    def captive_probe(request: Request):
        if not st.setup.state["hotspot_up"]:
            raise HTTPException(404)
        return RedirectResponse(server_url(request, st.net.hotspot_ip) + "/setup", 302)

    for p in CAPTIVE_PROBES:
        app.get(p, include_in_schema=False)(captive_probe)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(os.path.join(STATIC, "manifest.webmanifest"), media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    # -- auth ------------------------------------------------------------------------

    @app.get("/api/auth/state", response_model=AuthState, tags=["auth"])
    def auth_state(request: Request):
        return AuthState(first_run=first_run(),
                         authenticated=principal(request.headers.get("authorization"),
                                                 request.cookies.get("session")) is not None)

    @app.post("/api/auth/setup", response_model=Ok, tags=["auth"],
              summary="First run only: set the admin password")
    def auth_setup(body: Password, response: Response):
        if not first_run():
            raise HTTPException(409, "Admin password is already set")
        st.cfg["web"]["admin_password"] = auth.hash_password(body.password)
        save()
        set_session(response)
        return Ok()

    @app.post("/api/auth/login", response_model=Ok, tags=["auth"])
    def login(body: Password, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        if st.limiter.blocked(ip):
            raise HTTPException(429, "Too many attempts, try again in a minute")
        if first_run():
            raise HTTPException(409, "No admin password yet; set one first")
        if not auth.verify_password(body.password, st.cfg["web"]["admin_password"]):
            st.limiter.fail(ip)
            raise HTTPException(401, "Wrong password")
        st.limiter.reset(ip)
        set_session(response)
        return Ok()

    @app.post("/api/auth/logout", response_model=Ok, tags=["auth"])
    def logout(response: Response):
        response.delete_cookie("session", path="/")
        return Ok()

    @app.post("/api/auth/password", response_model=Ok, tags=["auth"], summary="Change the admin password")
    def change_password(body: PasswordChange, response: Response, who=Depends(require_auth)):
        if not auth.verify_password(body.current, st.cfg["web"]["admin_password"]):
            raise HTTPException(403, "Current password is wrong")
        st.cfg["web"]["admin_password"] = auth.hash_password(body.new)
        save()
        set_session(response)  # other sessions are now invalid; keep this one
        return Ok()

    @app.get("/api/tokens", response_model=list[TokenInfo], tags=["auth"])
    def list_tokens(who=Depends(require_auth)):
        return [TokenInfo(**t) for t in st.cfg["web"]["tokens"]]

    @app.post("/api/tokens", response_model=TokenCreated, status_code=201, tags=["auth"])
    def create_token(body: TokenCreate, who=Depends(require_auth)):
        plain, h = auth.new_token()
        rec = {"id": h[:12], "name": body.name, "hash": h, "created": time.time()}
        st.cfg["web"]["tokens"].append(rec)
        save()
        return TokenCreated(**rec, token=plain)

    @app.delete("/api/tokens/{token_id}", response_model=Ok, tags=["auth"], summary="Revoke a token")
    def revoke_token(token_id: str, who=Depends(require_auth)):
        before = len(st.cfg["web"]["tokens"])
        st.cfg["web"]["tokens"] = [t for t in st.cfg["web"]["tokens"] if t["id"] != token_id]
        if len(st.cfg["web"]["tokens"]) == before:
            raise HTTPException(404, "No such token")
        save()
        return Ok()

    # -- status and control ----------------------------------------------------------
    # The act_* functions are shared by the REST API and MQTT (mqtt.py).

    async def refresh_modes():
        try:
            st.engine_modes = (await st.engine.call("list_modes"))["modes"]
        except EngineError as e:  # offline, or an engine without list_modes
            log.info("list_modes: %s", e)

    async def snapshot(fresh=False):
        """Engine status for /api/status and MQTT; fresh: ask the engine now (the poller's copy
        can be 2s old), and get list_modes if it is still unknown."""
        if fresh and st.engine.online:
            try:
                st.engine.status = {k: v for k, v in (await st.engine.call("status")).items() if k not in ("id", "ok")}
            except EngineError:
                pass
        if fresh and st.engine.online and st.engine_modes is None:
            await refresh_modes()
        return {"online": st.engine.online, "engine": st.engine.status, "brightness": int(st.cfg["brightness"]["user"])}

    async def act_power(on):
        await engine_call("power", on=on)

    async def act_brightness(value):
        st.cfg["brightness"]["user"] = value
        save_soon()
        await engine_call("set_brightness", value=schedule.clamp(value, st.scheduler.cap))

    async def act_mode(args):
        if args["mode"] == "media" and args.get("media") not in st.library.items:
            raise HTTPException(404, "No such media item")
        if args["mode"] == "media" and st.library.items[args["media"]]["status"] != "ready":
            raise HTTPException(409, "That item is not transcoded yet")
        if args["mode"] == "playlist" and args.get("playlist") not in st.cfg["playlists"]:
            raise HTTPException(404, "No such playlist")
        await engine_call("set_mode", **mode_args(args))
        st.cfg["last_mode"] = mode_args(args)  # restored after a reboot (the engine boots on its boot screen)
        save()

    st.mqtt = mqtt.Bridge(SimpleNamespace(
        power=act_power, brightness=act_brightness, mode=act_mode, snapshot=snapshot,
        mode_defs=lambda: [m.model_dump() for m in mode_defs(st.engine_modes, sorted(st.cfg["playlists"]))],
        playlists=lambda: sorted(st.cfg["playlists"])),
        __version__, mqtt_client_factory or mqtt.paho_client)

    @app.get("/api/status", response_model=Status, tags=["control"])
    async def status(who=Depends(require_auth)):
        snap = await snapshot(fresh=True)
        now = dt.datetime.now().astimezone()
        w, h = config.canvas_size(st.cfg)
        return Status(online=snap["online"], engine=snap["engine"], brightness=snap["brightness"],
                      quiet_cap=st.scheduler.cap, canvas=CanvasSize(w=w, h=h), mqtt=st.mqtt.status,
                      version=__version__, timezone=now.tzname() or "", time=now.strftime("%H:%M"))

    @app.post("/api/power", response_model=Ok, tags=["control"])
    async def power(body: Power, who=Depends(require_auth)):
        await act_power(body.on)
        return Ok()

    @app.post("/api/brightness", response_model=Ok, tags=["control"],
              summary="Set brightness (clamped by the quiet-hours cap while it is active)")
    async def brightness(body: Brightness, who=Depends(require_auth)):
        await act_brightness(body.value)
        return Ok()

    @app.post("/api/auto_brightness", response_model=Ok, tags=["control"])
    async def auto_brightness(body: AutoBrightness, who=Depends(require_auth)):
        await engine_call("set_auto_brightness", enabled=body.enabled)
        st.cfg["brightness"]["auto"] = body.enabled  # so it survives an engine restart or reload
        save()
        return Ok()

    @app.get("/api/modes", response_model=list[ModeDef], tags=["control"],
             summary="Modes the picker offers, with their per-mode settings",
             description="Built from the engine's `list_modes` (docs/protocol.md): media, effects, clock, "
                         "text, idle and test patterns, each with its settings schema. While the engine is "
                         "offline the last answer is used; before any answer, only media and test patterns.")
    async def modes(who=Depends(require_auth)):
        await refresh_modes()
        return mode_defs(st.engine_modes, sorted(st.cfg["playlists"]))

    @app.post("/api/event", response_model=EventResult, tags=["control"],
              summary="Game hook: play an overlay animation on top of the current mode",
              description="Named events (`kill`, `killstreak`, `death`, `win`) play a 1.5-3s overlay; "
                          "the current mode keeps running underneath. Meant for an API token "
                          "(`Authorization: Bearer lp_...`, see tools/game_hook.py). Unknown events are a "
                          f"400. At most {EVENT_LIMIT} events per {EVENT_WINDOW_S}s per caller, then 429.")
    async def event(body: EventRequest, who=Depends(require_auth)):
        if body.event not in EVENTS:
            raise HTTPException(400, f"Unknown event '{body.event}'; use one of {', '.join(EVENTS)}")
        if st.event_limiter.blocked(who):
            raise HTTPException(429, "Too many events, slow down", headers={"Retry-After": str(EVENT_WINDOW_S)})
        st.event_limiter.fail(who)  # counts this event in the window
        r = await engine_call("overlay", event=body.event)
        return EventResult(event=body.event, ms=int(r.get("ms") or 0))

    @app.post("/api/mode", response_model=Ok, tags=["control"])
    async def set_mode(body: ModeRequest, who=Depends(require_auth)):
        await act_mode(body.model_dump())
        return Ok()

    # -- media -----------------------------------------------------------------------

    @app.get("/api/media", response_model=list[MediaItem], tags=["media"])
    def list_media(who=Depends(require_auth)):
        return [media_item(i) for i in st.library.list()]

    def get_item(media_id):
        item = st.library.get(media_id)
        if not item:
            raise HTTPException(404, "No such media item")
        return item

    @app.post("/api/media", response_model=MediaItem, status_code=201, tags=["media"],
              summary="Upload a file (multipart field `file`, optional settings fields)",
              description=f"Streams to disk and rejects files over 200MB. Other fields: `scale`, `filter`, "
                          f"`crop` (x,y,w,h), `position` (x,y), `speed`, `dither`, `name`. {SUPPORTED}",
              openapi_extra={"requestBody": {"content": {"multipart/form-data": {"schema": {
                  "type": "object", "required": ["file"],
                  "properties": {"file": {"type": "string", "format": "binary"},
                                 "scale": {"type": "string"}, "filter": {"type": "string"},
                                 "crop": {"type": "string"}, "position": {"type": "string"},
                                 "speed": {"type": "number"}, "dither": {"type": "boolean"},
                                 "name": {"type": "string"}}}}}}})
    async def upload_media(request: Request, who=Depends(require_auth)):
        m = re.search(r'boundary="?([^";]+)"?', request.headers.get("content-type", ""))
        if not m:
            raise HTTPException(400, "Expected multipart/form-data")
        length = request.headers.get("content-length")
        if length and int(length) > MAX_BYTES + 64 * 1024:
            raise HTTPException(413, f"File is over the 200MB limit. {SUPPORTED}")
        up = {"headers": {}, "hname": b"", "hval": b"", "name": None, "filename": None,
              "value": b"", "fields": {}, "file": None, "item": None, "bytes": 0}

        class TooBig(Exception):
            pass

        def on_header_field(data, s, e):
            up["hname"] += data[s:e]

        def on_header_value(data, s, e):
            up["hval"] += data[s:e]

        def on_header_end():
            up["headers"][up["hname"].decode().lower()] = up["hval"].decode(errors="replace")
            up["hname"], up["hval"] = b"", b""

        def on_headers_finished():
            cd = up["headers"].get("content-disposition", "")
            name = re.search(r'name="([^"]*)"', cd)
            fn = re.search(r'filename="([^"]*)"', cd)
            up["name"] = name.group(1) if name else None
            if up["name"] == "file" and fn:
                if up["item"]:
                    raise HTTPException(400, "Only one file per upload")
                up["item"], path = st.library.new(fn.group(1))
                up["file"] = open(path, "wb")

        def on_part_data(data, s, e):
            if up["file"]:
                up["bytes"] += e - s
                if up["bytes"] > MAX_BYTES:
                    raise TooBig()
                up["file"].write(data[s:e])
            else:
                up["value"] += data[s:e]

        def on_part_end():
            if up["file"]:
                up["file"].flush()
                os.fsync(up["file"].fileno())  # the source must survive a power cut before its transcode
                up["file"].close()
                up["file"] = None
            elif up["name"]:
                up["fields"][up["name"]] = up["value"].decode(errors="replace")
            up["value"], up["headers"] = b"", {}

        parser = MultipartParser(m.group(1).encode(), {
            "on_header_field": on_header_field, "on_header_value": on_header_value,
            "on_header_end": on_header_end, "on_headers_finished": on_headers_finished,
            "on_part_begin": lambda: None, "on_part_data": on_part_data, "on_part_end": on_part_end,
            "on_end": lambda: None})
        try:
            async for chunk in request.stream():
                parser.write(chunk)
            parser.finalize()
            if not up["item"]:
                raise HTTPException(400, "Missing file field")
            f = up["fields"]
            settings = MediaSettings(
                scale=f.get("scale") or "fit", filter=f.get("filter") or None,
                crop=[int(v) for v in f["crop"].split(",")] if f.get("crop") else None,
                position=[float(v) for v in f["position"].split(",")] if f.get("position") else [0.5, 0.5],
                speed=float(f.get("speed") or 1), dither=f.get("dither", "").lower() in ("1", "true", "on"))
        except TooBig:
            st.library.discard(up["item"]["id"])
            raise HTTPException(413, f"File is over the 200MB limit. {SUPPORTED}")
        except (ValueError, Exception) as e:
            if up["file"]:
                up["file"].close()
            if up["item"]:
                st.library.discard(up["item"]["id"])
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(422, f"Bad upload: {e}")
        if f.get("name"):
            up["item"]["name"] = f["name"][:120]
        return media_item(st.library.enqueue(up["item"]["id"], settings.model_dump()))

    @app.get("/api/media/{media_id}", response_model=MediaItem, tags=["media"])
    def get_media(media_id: str, who=Depends(require_auth)):
        return media_item(get_item(media_id))

    @app.patch("/api/media/{media_id}", response_model=MediaItem, tags=["media"],
               summary="Rename and/or change settings (settings changes re-transcode)")
    def patch_media(media_id: str, body: MediaPatch, who=Depends(require_auth)):
        item = get_item(media_id)
        if body.name:
            st.library.rename(media_id, body.name)
        if body.settings is not None:
            if item["status"] in ("uploading", "transcoding"):
                raise HTTPException(409, f"Item is {item['status']}; try again when it is done")
            st.library.enqueue(media_id, body.settings.model_dump())
        return media_item(item)

    @app.post("/api/media/{media_id}/retry", response_model=MediaItem, tags=["media"])
    def retry_media(media_id: str, who=Depends(require_auth)):
        item = get_item(media_id)
        if item["status"] in ("queued", "transcoding"):
            return media_item(item)
        return media_item(st.library.enqueue(media_id))

    @app.delete("/api/media/{media_id}", response_model=Ok, tags=["media"])
    def delete_media(media_id: str, who=Depends(require_auth)):
        get_item(media_id)
        for pl in st.cfg["playlists"].values():
            pl["items"] = [i for i in pl.get("items", []) if i.get("media") != media_id]
        save()
        st.library.delete(media_id)
        return Ok()

    @app.get("/api/media/{media_id}/thumb.png", tags=["media"], response_class=Response,
             summary="Thumbnail at wall resolution")
    def media_thumb(media_id: str, who=Depends(require_auth)):
        get_item(media_id)
        p = st.library.thumb_path(media_id)
        if not os.path.exists(p):
            raise HTTPException(404, "Not transcoded yet")
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/api/media/{media_id}/source", tags=["media"], response_class=Response,
             summary="The original upload (for client-side preview of scaling and crop)")
    def media_source(media_id: str, who=Depends(require_auth)):
        item = get_item(media_id)
        types = {".gif": "image/gif", ".png": "image/png", ".apng": "image/apng", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm",
                 ".mov": "video/quicktime"}
        return FileResponse(st.library.source_path(media_id), media_type=types.get(item["ext"], "application/octet-stream"))

    @app.get("/api/media/{media_id}/frames", tags=["media"], response_class=Response,
             summary="Evenly spaced cached frames as raw RGB (for the preview renderer)",
             description="Body: `X-Frames` frames of `X-Width`*`X-Height`*3 bytes, gamma-corrected as "
                         "stored. `X-Indices` lists the frame numbers.")
    def media_frames(media_id: str, count: int = Query(12, ge=1, le=60), who=Depends(require_auth)):
        item = get_item(media_id)
        if item["status"] != "ready":
            raise HTTPException(409, "Not transcoded yet")
        data, idx = st.library.frames(media_id, count)
        return Response(data, media_type="application/octet-stream", headers={
            "X-Width": str(item["info"]["w"]), "X-Height": str(item["info"]["h"]),
            "X-Frames": str(len(idx)), "X-Indices": ",".join(map(str, idx)), "Cache-Control": "no-cache"})

    # -- playlists -------------------------------------------------------------------

    def playlist_model(p):
        return Playlist(items=[PlaylistItem(media=i["media"], duration_s=i.get("duration_s")) for i in p.get("items", [])],
                        loop=p.get("loop", True), shuffle=p.get("shuffle", False))

    @app.get("/api/playlists", response_model=dict[str, Playlist], tags=["playlists"])
    def list_playlists(who=Depends(require_auth)):
        return {n: playlist_model(p) for n, p in st.cfg["playlists"].items()}

    @app.put("/api/playlists/{name}", response_model=Playlist, tags=["playlists"], summary="Create or replace")
    async def put_playlist(name: str, body: Playlist, who=Depends(require_auth)):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}", name):
            raise HTTPException(422, "Name: letters, digits, space, _ or -, up to 40 characters")
        missing = [i.media for i in body.items if i.media not in st.library.items]
        if missing:
            raise HTTPException(404, f"No such media: {', '.join(missing)}")
        st.cfg["playlists"][name] = {
            "loop": body.loop, "shuffle": body.shuffle,
            "items": [{"media": i.media, **({"duration_s": i.duration_s} if i.duration_s else {})} for i in body.items]}
        save()
        await reload()
        return body

    @app.delete("/api/playlists/{name}", response_model=Ok, tags=["playlists"])
    async def delete_playlist(name: str, who=Depends(require_auth)):
        if name not in st.cfg["playlists"]:
            raise HTTPException(404, "No such playlist")
        del st.cfg["playlists"][name]
        save()
        await reload()
        return Ok()

    # -- schedule --------------------------------------------------------------------

    @app.get("/api/schedule", response_model=Schedule, tags=["schedule"])
    def get_schedule(who=Depends(require_auth)):
        return Schedule(**st.cfg["schedule"])

    @app.put("/api/schedule", response_model=Schedule, tags=["schedule"])
    async def put_schedule(body: Schedule, who=Depends(require_auth)):
        st.cfg["schedule"] = body.model_dump()
        save()
        await st.scheduler.tick()
        return body

    # -- settings --------------------------------------------------------------------

    def settings_model():
        c = st.cfg
        return Settings(matrix=MatrixSettings(**{k: c["matrix"][k] for k in MatrixSettings.model_fields}),
                        power=PowerSettings(**c["power"]), mqtt=Mqtt(**c["mqtt"]), ddp=Ddp(**c["ddp"]),
                        e131=E131(**c["e131"]), stream=StreamSettings(**c["stream"]))

    @app.get("/api/settings", response_model=Settings, tags=["settings"])
    def get_settings(who=Depends(require_auth)):
        return settings_model()

    @app.put("/api/settings", response_model=SettingsSaved, tags=["settings"],
             summary="Write matrix, power, MQTT, DDP and E1.31 settings to config")
    async def put_settings(body: Settings, who=Depends(require_auth)):
        before = settings_model()
        st.cfg["matrix"].update(body.matrix.model_dump())
        st.cfg["power"] = body.power.model_dump()
        for k in ("mqtt", "ddp", "e131", "stream"):  # update: keeps keys the UI doesn't show (ports, ids)
            st.cfg[k].update(getattr(body, k).model_dump())
        save()
        stream_error = None
        if (before.ddp.enabled, before.e131.enabled) != (body.ddp.enabled, body.e131.enabled):
            try:
                await st.engine.call("set_stream", ddp=body.ddp.enabled, e131=body.e131.enabled)
            except EngineOffline:
                pass  # the engine reads the config when it starts
            except EngineError as e:
                stream_error = str(e)
        needed = await reload()
        await st.mqtt.apply(st.cfg["mqtt"])
        await st.system_sync()  # DDP/E1.31 on/off changes the firewall
        restart = bool(needed) or before.matrix != body.matrix
        return SettingsSaved(**body.model_dump(), restart_required=restart, stream_error=stream_error)

    # -- network ---------------------------------------------------------------------

    async def net_call(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except net.NetError as e:
            raise HTTPException(503, str(e))

    def apply_later(coro_fn, delay=1.5):
        """Run a network change after the response has left, so the browser is not cut off
        mid-request when the address changes."""
        async def later():
            await asyncio.sleep(delay)
            try:
                await coro_fn()
            except Exception:
                log.exception("network change")
        st.net_task = asyncio.create_task(later())

    sysapi.install(app, st, require_auth, apply_later)  # /api/system/*, /api/health (M8)

    def network_model(s):
        rb = st.net.rollback_pending()
        return NetworkStatus(**s, rollback=Rollback(address=rb["address"], seconds_left=rb["seconds_left"]) if rb else None,
                             setup_mode=st.setup.state["hotspot_up"])

    @app.get("/api/network", response_model=NetworkStatus, tags=["network"],
             summary="Address, Wi-Fi, hostname and any pending static IP confirmation")
    async def network(who=Depends(require_auth)):
        return network_model(await net_call(st.net.status))

    @app.put("/api/network/hostname", response_model=HostnameSet, tags=["network"],
             summary="Set the hostname (one RFC 1123 label); ledposter.local follows it")
    async def set_hostname(body: Hostname, request: Request, who=Depends(require_auth)):
        try:
            name = net.valid_hostname(body.hostname)
        except ValueError as e:
            raise HTTPException(422, str(e))
        await net_call(st.net.set_hostname, name)
        return HostnameSet(hostname=name, url=server_url(request, f"{name}.local") + "/")

    @app.post("/api/network/static", response_model=StaticApplied, tags=["network"],
              summary="Static IP, validated against the current subnet, with a 2-minute DHCP rollback",
              description="Arms a systemd timer that puts the connection back on DHCP, then applies the address "
                          "1.5s after this response. Open `url` on the new address and POST "
                          "`/api/network/static/confirm` before `seconds` run out, or the rollback happens.")
    async def set_static(body: StaticIp, request: Request, who=Depends(require_auth)):
        cur = await net_call(st.net.status)
        try:
            v = net.validate_static(body.address, body.prefix, body.gateway, body.dns, cur)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not cur.get("connection") or cur.get("hotspot"):
            raise HTTPException(409, "The wall is not on a network right now")
        if v["address"] != cur["address"] and await net_call(st.net.address_in_use, v["address"]):
            raise HTTPException(409, f"{v['address']} already answers on this network; pick another address")
        con = cur["connection"]
        await net_call(st.net.arm_rollback, con, v["address"])

        async def apply():
            try:
                await asyncio.to_thread(st.net.set_static, con, v["address"], v["prefix"], v["gateway"], v["dns"])
            except net.NetError as e:
                log.error("static IP not applied, rollback cancelled: %s", e)
                await asyncio.to_thread(st.net.cancel_rollback)
        apply_later(apply)
        return StaticApplied(address=v["address"], url=server_url(request, v["address"]) + "/#/settings/confirm",
                             seconds=net.ROLLBACK_SECONDS)

    @app.post("/api/network/static/confirm", response_model=NetworkStatus, tags=["network"],
              summary="Keep the new static IP (cancels the rollback timer)")
    async def confirm_static(who=Depends(require_auth)):
        if not st.net.rollback_pending():
            raise HTTPException(409, "No static IP change is waiting for confirmation")
        if st.net_task and not st.net_task.done():
            await st.net_task  # a confirm racing the delayed apply must not leave the change unprotected
        await net_call(st.net.cancel_rollback)
        return network_model(await net_call(st.net.status))

    @app.post("/api/network/dhcp", response_model=HostnameSet, tags=["network"], summary="Back to DHCP now")
    async def set_dhcp(request: Request, who=Depends(require_auth)):
        cur = await net_call(st.net.status)
        if not cur.get("connection") or cur.get("hotspot"):
            raise HTTPException(409, "The wall is not on a network right now")
        con, host = cur["connection"], cur["hostname"]
        if st.net.rollback_pending():
            await net_call(st.net.cancel_rollback)

        async def apply():
            await asyncio.to_thread(st.net.set_dhcp, con)
        apply_later(apply)
        return HostnameSet(hostname=host, url=server_url(request, f"{host}.local") + "/")

    @app.post("/api/network/show_ip", response_model=Ok, tags=["network"], summary="Show hostname and IP on the wall for 10s")
    async def show_ip(who=Depends(require_auth)):
        await st.setup.show_ip()
        return Ok()

    def setup_model(request):
        return SetupState(**st.setup.public(first_run()),
                          authenticated=principal(request.headers.get("authorization"), request.cookies.get("session")) is not None)

    @app.get("/api/network/setup", response_model=SetupState, tags=["network"],
             summary="Setup hotspot / join progress (open without login only on a first-run wall in setup mode)")
    def setup_state(request: Request, who=Depends(setup_auth)):
        return setup_model(request)

    @app.post("/api/network/setup/start", response_model=SetupState, tags=["network"],
              summary="Raise the LEDWall-Setup hotspot (the wall leaves its current network)")
    async def setup_start(request: Request, who=Depends(require_auth)):
        await st.setup.start("Started from Settings")
        if st.setup.state["mode"] == "off":
            raise HTTPException(503, st.setup.state.get("reason") or "Hotspot failed")
        return setup_model(request)

    @app.post("/api/network/setup/stop", response_model=SetupState, tags=["network"], summary="Drop the setup hotspot")
    async def setup_stop(request: Request, who=Depends(require_auth)):
        await st.setup.stop()
        return setup_model(request)

    def wifi_model():
        s = st.setup.state
        return WifiList(networks=[WifiNetwork(**n) for n in s["networks"]], scanned_at=s["scanned_at"],
                        current=None, setup_mode=s["hotspot_up"])

    @app.get("/api/network/wifi", response_model=WifiList, tags=["network"],
             summary="Nearby networks (cached from the last scan while the hotspot is up)")
    async def wifi_list(who=Depends(setup_auth)):
        if not st.setup.state["networks"] and not st.setup.state["hotspot_up"]:
            await st.setup.rescan()
        m = wifi_model()
        if not st.setup.state["hotspot_up"]:
            try:
                m.current = (await asyncio.to_thread(st.net.status)).get("ssid")
            except net.NetError:
                pass
        return m

    @app.post("/api/network/wifi/scan", response_model=WifiList, tags=["network"],
              summary="Rescan. In setup mode the hotspot drops for about 15s and comes back")
    async def wifi_scan(who=Depends(setup_auth)):
        await st.setup.rescan()
        if st.setup.state["mode"] == "off" and st.setup.state.get("reason"):
            raise HTTPException(503, st.setup.state["reason"])
        return wifi_model()

    @app.post("/api/network/wifi/join", response_model=SetupState, tags=["network"],
              summary="Join a network. Returns at once; poll /api/network/setup for progress",
              description="In setup mode the hotspot switches off while joining and comes back if the join fails, "
                          "so the client loses this server for a while either way. The wall shows each step.")
    async def wifi_join(body: WifiJoin, request: Request, who=Depends(setup_auth)):
        if body.password and not 8 <= len(body.password) <= 63:
            raise HTTPException(422, "Wi-Fi passwords are 8 to 63 characters")
        try:
            st.setup.join(body.ssid, body.password or None, body.hidden)
        except net.NetError as e:
            raise HTTPException(409, str(e))
        return setup_model(request)

    # -- preview ---------------------------------------------------------------------

    @app.websocket("/api/preview")
    async def preview(ws: WebSocket, token: Optional[str] = None):
        await ws.accept()  # accept first so the client sees the 4401 close code, not a failed handshake
        if not principal(ws.headers.get("authorization"), ws.cookies.get("session"), token):
            await ws.close(code=4401)
            return
        q = st.engine.subscribe()

        async def pump():
            while True:
                try:
                    status, frame = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    status, frame = None, None
                if frame is None:  # no stream right now: say so about once a second
                    await ws.send_text(json.dumps({"online": st.engine.online, "streaming": False,
                                                   **(st.engine.status or {})}))
                    continue
                await ws.send_text(json.dumps({"online": True, "streaming": True, **status}))
                await ws.send_bytes(frame)

        sender = asyncio.create_task(pump())
        try:
            while True:
                await ws.receive()  # only to notice the client leaving
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sender.cancel()
            st.engine.unsubscribe(q)

    return app
