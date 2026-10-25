"""Owns $LEDPOSTER_DATA/config.yaml: read with defaults, atomic write (docs/ARCHITECTURE.md).

The web app is the only writer. Every write goes through save(): tmp file, fsync,
rename, fsync dir, mode 640 (the file holds the admin password hash and token hashes;
the group is the `ledwall` group the engine drops to, so reload_config can read it).
"""
import copy
import logging
import os
import tempfile

import yaml

log = logging.getLogger("ledposter.config")
# docs/spec.md: 100W PD supply, about 15A at 5V for the panels. A ceiling above it would let the
# limiter pass frames the supply cannot deliver; the API refuses it and load() clamps it (the
# engine clamps and logs it too).
MAX_CEILING_AMPS = 15.0

DEFAULTS = {
    "version": 1,
    "matrix": {
        "hardware_mapping": "adafruit-hat-pwm",
        "panel_size": 64,
        "gpio_slowdown": 2,
        "pwm_bits": 6,
        "pwm_dither_bits": 2,
        "pwm_lsb_nanoseconds": 100,
        "limit_refresh_rate_hz": 130,
        "show_refresh_rate": False,
        "panels": [{"col": 0, "row": 0}, {"col": 1, "row": 0}, {"col": 1, "row": 1},
                   {"col": 0, "row": 1}, {"col": 0, "row": 2}, {"col": 1, "row": 2}],
    },
    "power": {"ceiling_amps": 13.0, "amps_full_white_per_panel": 6.0},
    "display": {"gamma": 2.2},
    # brightness.user is the slider; the engine reads both at start and on reload_config
    "brightness": {"user": 100, "auto": True},
    "engine": {"socket": "/run/ledposter/engine.sock", "preview_socket": "/run/ledposter/frames.sock"},
    "web": {"port": 8080, "admin_password": None, "tokens": []},
    "playlists": {},
    "schedule": {
        "enabled": False, "on": "07:00", "off": "23:00", "days": [0, 1, 2, 3, 4, 5, 6],
        "quiet": {"enabled": False, "start": "22:00", "end": "07:00", "max_brightness": 20},
    },
    # Setup hotspot and on-wall IP display (M6). wifi_device "" = first Wi-Fi device.
    "network": {"hotspot_ssid": "LEDWall-Setup", "hotspot_ip": "10.42.0.1", "setup_timeout_s": 60,
                "show_ip_s": 10, "wifi_device": ""},
    # M7: MQTT/Home Assistant (web app) and DDP/E1.31 input (engine); both inputs off by default
    "mqtt": {"enabled": False, "host": "", "port": 1883, "username": "", "password": "", "tls": False},
    "ddp": {"enabled": False},
    "e131": {"enabled": False, "start_universe": 1, "multicast": False},
    "stream": {"apply_gamma": True},
    # M8: SSH mode applied by ledposter-ssh.service (disabled, or key-only with these keys),
    # the weekly unattended-upgrades window (day: 0 = Monday, hour: local), and the last
    # set_mode the web app sent, restored when the engine comes up on its boot screen.
    "ssh": {"mode": "disabled", "authorized_keys": ""},
    "maintenance": {"enabled": True, "day": 6, "hour": 4},
    "last_mode": {},
    # The time (and date) the engine draws on top of whatever plays; applied by reload_config.
    "clock_overlay": {"enabled": False, "position": "bottom-center", "hour24": True, "date": True,
                      "time_size": "m", "date_size": "s"},
}


def data_dir():
    return os.environ.get("LEDPOSTER_DATA") or "/data"


def path():
    return os.path.join(data_dir(), "config.yaml")


class EmptyConfig(ValueError):
    pass


def load(p=None, allow_empty=True):
    """Config dict with every DEFAULTS section present (missing keys filled in, nothing removed).
    allow_empty=False: an existing file with no document raises EmptyConfig (read mid-write)."""
    p = p or path()
    cfg = {}
    if os.path.exists(p):
        with open(p) as f:
            cfg = yaml.safe_load(f)
        if cfg is None or cfg == {}:
            if not allow_empty:
                raise EmptyConfig(f"{p}: empty")
            cfg = {}
        if not isinstance(cfg, dict):
            raise ValueError(f"{p}: top level must be a mapping")
    for key, default in DEFAULTS.items():
        if isinstance(default, dict):
            if not isinstance(cfg.get(key), dict):  # missing, or a bare `web:` (null)
                cfg[key] = {}
            if key != "playlists":
                for k, v in default.items():
                    cfg[key].setdefault(k, copy.deepcopy(v))
        else:
            cfg.setdefault(key, default)
    if not isinstance(cfg["web"]["tokens"], list):  # auth.find_token iterates it
        cfg["web"]["tokens"] = []
    cfg["version"] = cfg.get("version") or DEFAULTS["version"]
    try:
        ceiling = float(cfg["power"].get("ceiling_amps"))
    except (TypeError, ValueError):
        ceiling = DEFAULTS["power"]["ceiling_amps"]
    if not ceiling > 0:
        ceiling = DEFAULTS["power"]["ceiling_amps"]
    if ceiling > MAX_CEILING_AMPS:
        log.warning("power.ceiling_amps %s is above the %sA supply budget; using %sA", ceiling, MAX_CEILING_AMPS, MAX_CEILING_AMPS)
        ceiling = MAX_CEILING_AMPS
    cfg["power"]["ceiling_amps"] = ceiling
    return cfg


def save(cfg, p=None):
    p = p or path()
    d = os.path.dirname(os.path.abspath(p))
    os.makedirs(d, exist_ok=True)
    out = dict(cfg)
    out["version"] = cfg.get("version") or DEFAULTS["version"]
    out = {"version": out.pop("version"), **out}  # version stays the first key
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(fd, 0o640)
            yaml.safe_dump(out, f, sort_keys=False, default_flow_style=None)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def canvas_size(cfg):
    """Logical canvas (w, h) from the panel layout, as the engine computes it."""
    m = cfg["matrix"]
    size = int(m.get("panel_size", 64))
    panels = m.get("panels") or [{"col": 0, "row": 0}]
    cols = max(int(p.get("col", 0)) for p in panels) + 1
    rows = max(int(p.get("row", 0)) for p in panels) + 1
    return size * cols, size * rows
