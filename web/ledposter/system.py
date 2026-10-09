"""Milestone 8: what the web app asks the OS for. Root-needing actions (firewall, SSH, update,
factory reset, service restarts, reboot) go through fixed systemd units the `ledwall` user may
start via polkit (system/polkit/50-ledposter-system.rules); each unit reads its inputs from a
file at the top of $LEDPOSTER_DATA written here (firewall.rules, ssh/, maintenance.conf,
maintenance-request, factory-reset, update-request.json, net-rollback), so the app never needs
root or sudo (M6 pattern, docs/DEVIATIONS.md). The root side (system/bin/ledposter-sys,
updater.py) treats every one of them as untrusted data: read without following symlinks,
matched line by line against a strict pattern, never sourced or passed to a shell. The pure
parts (rule generation, key parsing, tag ordering, the factory reset plan, last-mode restore)
are plain functions so they are unit tested without a Pi.

    python -m ledposter.system init-config PATH [--ssh disabled|key-only] [--keys FILE]
        used by install.sh and the factory reset: write a fresh config.yaml for the Pi
        (port 80; the engine's user and group come from its unit) and the files the units read from it
"""
import argparse
import logging
import os
import re
import subprocess
import time

import yaml

from . import config, updater
from .net import NetError

log = logging.getLogger("ledposter.system")

UNITS = {"firewall": "ledposter-firewall.service", "ssh": "ledposter-ssh.service",
         "update": "ledposter-update.service", "factory_reset": "ledposter-factory-reset.service",
         "maintenance": "ledposter-maintenance.service",
         "engine": "ledposter-engine.service", "web": "ledposter-web.service"}
# What system/bin/ledposter-sys accepts from $LEDPOSTER_DATA/firewall.rules (kept in step).
RULE_RE = re.compile(r"^(default (deny|allow) (incoming|outgoing)|logging off|allow \d{1,5}/(tcp|udp)|"
                     r"allow in on [a-z0-9.-]{1,15} to any port \d{1,5} proto (tcp|udp))$")
KEY_RE = re.compile(r"^(sk-)?(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521))(@openssh\.com)?"
                    r" AAAA[A-Za-z0-9+/]+={0,3}( [^\s].*)?$")  # every key blob starts with its type: AAAA
KEEP_ON_RESET = {"app", "setup-password", "state", "lost+found"}
TAG_RE = updater.TAG_RE
# Mirrors the parser in ledposter-sys maintenance: three integer lines, nothing else.
MAINT_RE = re.compile(r"^(ENABLED|DAY|HOUR)=[0-9]+$")


# -- firewall (pure) -------------------------------------------------------------------

def firewall_rules(*, http_port=80, ssh=False, ddp=False, e131=None, hotspot_iface="wlan0"):
    """ufw rules as `ufw <rule>` arguments, applied in order by `ledposter-sys firewall` after a
    reset. Spec: only HTTP, mDNS, MQTT outbound (default allow outgoing) and DDP/E1.31 when
    enabled; SSH only when on. The hotspot's DHCP (67) and DNS (53) are allowed on the Wi-Fi
    interface at all times: NetworkManager's dnsmasq only listens while the hotspot is up, and
    without them the captive portal never opens (docs/DEVIATIONS.md). `e131` defaults to `ddp`
    (one switch in M6-era configs; M7 has `e131.enabled` of its own)."""
    rules = ["default deny incoming", "default allow outgoing", "logging off",
             f"allow {int(http_port)}/tcp", "allow 5353/udp"]
    if ddp:
        rules.append("allow 4048/udp")
    if ddp if e131 is None else e131:
        rules.append("allow 5568/udp")
    if ssh:
        rules.append("allow 22/tcp")
    rules += [f"allow in on {hotspot_iface} to any port 67 proto udp",
              f"allow in on {hotspot_iface} to any port 53 proto udp",
              f"allow in on {hotspot_iface} to any port 53 proto tcp"]
    assert all(RULE_RE.match(r) for r in rules), rules
    return rules


# -- SSH (pure) ------------------------------------------------------------------------

def parse_authorized_keys(text):
    """The keys from a textarea, one per line, comments and blanks dropped. ValueError names
    the first bad line (the root-side script only accepts what this accepts)."""
    keys = []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not KEY_RE.match(line):
            raise ValueError(f"Line {n} is not an SSH public key (expected e.g. 'ssh-ed25519 AAAA... name')")
        keys.append(line)
    return keys


# -- releases (pure) -------------------------------------------------------------------

def tag_key(tag):
    """Orders release tags: m1 < m8 < m10 < v0.9 < v1.0.0; anything else sorts first."""
    m = re.fullmatch(r"(m|v)(\d+(?:\.\d+)*)", tag or "")
    if not m:
        return (0, [], tag or "")
    return (1 if m.group(1) == "m" else 2, [int(x) for x in m.group(2).split(".")], tag)


def newer_tags(current, tags):
    """Tags newer than the running one (`current` may be `m8-3-gabc1234`: compare its tag part)."""
    base = re.match(r"^[^-]+", current or "")
    cur = tag_key(base.group(0) if base else current)
    return sorted((t for t in set(tags) if TAG_RE.match(t) and tag_key(t) > cur), key=tag_key)


# -- factory reset (pure) --------------------------------------------------------------

def factory_reset_plan(data_dir):
    """What `ledposter-sys factory-reset` erases under the data dir: everything except the
    installed releases (`app`), the hotspot password on the card, the persisted system state
    dir (its Wi-Fi profiles and hostname are reset by the script itself) and lost+found."""
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        names = []
    return {"delete": [n for n in names if n not in KEEP_ON_RESET],
            "keep": [n for n in names if n in KEEP_ON_RESET]}


def maintenance_conf(m):
    """What ledposter-maintenance.timer's hourly check reads. ledposter-sys parses it line by
    line (`^(ENABLED|DAY|HOUR)=[0-9]+$`, range-checked); it is never sourced."""
    text = f"ENABLED={1 if m.get('enabled', True) else 0}\nDAY={int(m.get('day', 6))}\nHOUR={int(m.get('hour', 4))}\n"
    assert all(MAINT_RE.match(line) for line in text.splitlines()), text
    return text


# -- last mode restore -----------------------------------------------------------------

class LastMode:
    """The engine starts on its boot screen; once per engine start the web app sends the last
    set_mode it made (config `last_mode`). `send(cmd, **args)` returns False when it failed."""

    def __init__(self, get_last, send, media_ready=lambda mid: True):
        self.get_last, self.send, self.media_ready = get_last, send, media_ready
        self._engine_start = None

    def restorable(self):
        args = dict(self.get_last() or {})
        mode = args.get("mode")
        if not mode or mode == "boot":
            return None
        if mode == "media" and not self.media_ready(args.get("media")):
            return None
        return args

    async def tick(self, status, now=None):
        """True when a set_mode was sent."""
        if not status or status.get("mode") != "boot":
            return False
        start = (now if now is not None else time.time()) - float(status.get("uptime_s") or 0)
        if self._engine_start is not None and abs(start - self._engine_start) < 60:
            return False  # already tried for this engine start (a failed mode must not loop)
        self._engine_start = start
        args = self.restorable()
        if not args:
            return False
        return bool(await self.send("set_mode", **args))


# -- the OS ------------------------------------------------------------------------

class SystemError_(Exception):
    """A unit could not be started; the message is meant for a person."""


class System:
    def __init__(self, runner, data_dir, hotspot_iface=None, git=None):
        self.runner, self.data = runner, data_dir
        self.app = os.path.join(data_dir, "app")
        self._iface = hotspot_iface or (lambda: "wlan0")
        self.git = git or updater.git_run  # (argv, env, timeout) -> (rc, out, err); tests inject one

    def hotspot_iface(self):
        try:
            return self._iface() or "wlan0"
        except NetError:
            return "wlan0"

    # -- files the root-side units read --

    def derived_files(self, cfg):
        ssh_on = cfg["ssh"].get("mode") == "key-only"
        rules = firewall_rules(http_port=cfg["web"]["port"], ssh=ssh_on, ddp=bool(cfg["ddp"].get("enabled")),
                               e131=bool((cfg.get("e131") or {}).get("enabled")), hotspot_iface=self.hotspot_iface())
        keys = parse_authorized_keys(cfg["ssh"].get("authorized_keys") or "")
        return {"firewall.rules": "\n".join(rules) + "\n",
                "ssh/mode": ("key-only" if ssh_on else "disabled") + "\n",
                "ssh/authorized_keys": "".join(k + "\n" for k in keys),
                "maintenance.conf": maintenance_conf(cfg["maintenance"])}

    def sync_files(self, cfg):
        """Writes the derived files that changed; returns their names."""
        changed = []
        for name, text in self.derived_files(cfg).items():
            p = os.path.join(self.data, name)
            try:
                with open(p) as f:
                    if f.read() == text:
                        continue
            except OSError:
                pass
            updater.write_atomic(p, text)
            changed.append(name)
        return changed

    # -- units --

    def _systemctl(self, *args, timeout=60):
        rc, out, err = self.runner(["systemctl", *args], timeout=timeout)
        if rc != 0:
            raise SystemError_(f"systemctl {' '.join(args)} failed: {(err or out).strip() or rc}")

    def start(self, what):
        self._systemctl("start", "--no-block", UNITS[what])

    def restart(self, what):
        self._systemctl("restart", "--no-block", UNITS[what])

    def reboot(self):
        self._systemctl("reboot")

    def unit_active(self, what):
        rc, out, _ = self.runner(["systemctl", "is-active", UNITS[what]], timeout=10)
        return rc == 0 or out.strip() in ("active", "activating")

    # -- update --

    def update_status(self):
        return updater.Updater(self.data).status()

    def releases(self):
        u = updater.Updater(self.data)
        return {"current": u.link("current"), "previous": u.link("previous") if u.ready(u.link("previous")) else None}

    def check_updates(self, current):
        try:
            tags = updater.list_tags(self.app, self.git)
        except (updater.StepError, subprocess.SubprocessError, OSError) as e:
            raise SystemError_(f"Could not reach the repository: {e}")
        return {"all": sorted(tags, key=tag_key), "newer": newer_tags(current, tags)}

    def request_update(self, target):
        if not TAG_RE.match(target or ""):
            raise ValueError("Bad release tag")
        updater.Updater(self.data).write_request({"action": "update", "target": target})
        self.start("update")

    def request_rollback(self):
        if not self.releases()["previous"]:
            raise ValueError("No previous release to roll back to")
        updater.Updater(self.data).write_request({"action": "rollback"})
        self.start("update")

    # -- the rest --

    def request_factory_reset(self):
        updater.write_atomic(os.path.join(self.data, "factory-reset"), f"{int(time.time())}\n")
        self.start("factory_reset")

    def request_maintenance(self):
        """`ledposter-sys maintenance timer` begins the window when this file exists (state/ is
        root-owned, so the request lives next to the other unit inputs at the top of the data dir)."""
        updater.write_atomic(os.path.join(self.data, "maintenance-request"), f"{int(time.time())}\n")
        self.start("maintenance")

    def maintenance_pending(self):
        return os.path.exists(os.path.join(self.data, "state", "maintenance-pending"))

    @staticmethod
    def overlay_active():
        try:
            with open("/proc/cmdline") as f:
                return bool(re.search(r"overlayroot=tmpfs|boot=overlay", f.read()))
        except OSError:
            return False


# -- install-time config -------------------------------------------------------------

def init_config(path, ssh_mode=None, keys_text="", port=80):
    """The Pi's config: a fresh one from config/config.example.yaml (no example playlist, port
    80) when none exists, else the existing file; `ssh_mode` (with keys) replaces the ssh
    section when given. Writes the derived unit input files next to it. The engine's socket
    paths and drop user are not written here: as root the engine takes them from the unit's
    flags, never from this file (it is the web app's)."""
    if os.path.exists(path):
        cfg = config.load(path)
    else:
        example = os.path.join(os.path.dirname(__file__), "..", "..", "config", "config.example.yaml")
        with open(example) as f:
            cfg = yaml.safe_load(f)
        cfg["playlists"] = {}
        for k in ("user", "group"):  # dev-only keys of the example; the installed engine ignores them
            cfg["engine"].pop(k, None)
        cfg["web"]["port"] = port
        config.save(cfg, path)
        cfg = config.load(path)  # every section present, like the app would leave it
    if ssh_mode:
        keys = parse_authorized_keys(keys_text)
        if ssh_mode == "key-only" and not keys:
            raise ValueError("key-only SSH needs at least one authorized key")
        cfg["ssh"] = {"mode": ssh_mode, "authorized_keys": "".join(k + "\n" for k in keys)}
    config.save(cfg, path)
    System(lambda argv, timeout=0: (1, "", ""), os.path.dirname(os.path.abspath(path))).sync_files(cfg)
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m ledposter.system", description=__doc__.split("\n\n")[1])
    sub = p.add_subparsers(dest="cmd", required=True)
    ic = sub.add_parser("init-config")
    ic.add_argument("path")
    ic.add_argument("--ssh", choices=["disabled", "key-only"], help="default: keep (or the default, disabled)")
    ic.add_argument("--keys", help="authorized_keys file for key-only mode")
    a = p.parse_args(argv)
    keys = ""
    if a.keys:
        with open(a.keys) as f:
            keys = f.read()
    cfg = init_config(a.path, a.ssh, keys)
    print(f"wrote {a.path}: ssh {cfg['ssh']['mode']}, {len(parse_authorized_keys(keys))} key(s)")


if __name__ == "__main__":
    main()
