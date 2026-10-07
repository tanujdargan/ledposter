"""Everything that touches the OS network stack (docs/spec.md "Networking and integrations"):
nmcli, hostnamectl, systemctl and ping, all through one injectable runner so tests and
Mac dev never run them. NetworkManager is Bookworm's default; Avahi follows the hostname.

Static IP rollback: before a static address is applied, the connection name is written to
$LEDPOSTER_DATA/net-rollback and the systemd timer `ledposter-net-rollback.timer`
(system/systemd, 120s) is restarted. The timer runs `system/bin/ledposter-net rollback` as
root, which puts that connection back on DHCP unless the file was removed by a confirm.
Nothing about it depends on this process staying alive.
"""
import ipaddress
import logging
import os
import re
import subprocess
import time

log = logging.getLogger("ledposter.net")

ROLLBACK_TIMER = "ledposter-net-rollback.timer"
HOSTS_SERVICE = "ledposter-hosts.service"
ROLLBACK_SECONDS = 120
HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")  # one RFC 1123 label
JOIN_ERRORS = [  # nmcli stderr fragment -> sentence for the user, first match wins
    ("802.1X", "This network needs an enterprise (802.1X) login, which is not supported"),
    ("Secrets were required", "Wrong password"),
    ("No network with SSID", "Network not found"),
    ("could not be found", "Network not found"),
    ("IP configuration could not be reserved", "Joined, but the router gave no IP address (DHCP timed out)"),
    ("Timeout", "Timed out while joining"),
]


class NetError(Exception):
    """A command failed; the message is meant for a person."""


def run_subprocess(argv, timeout=30):
    """The real runner: (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise NetError(f"{argv[0]} is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise NetError(f"{argv[0]} timed out") from e
    return p.returncode, p.stdout, p.stderr


# -- nmcli terse output --------------------------------------------------------------

def split_terse(line):
    """Fields of one `nmcli -t` line. Values escape ':' as '\\:' and '\\' as '\\\\'."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def parse_rows(text, fields):
    """`nmcli -t -f F1,F2 ...` list output as dicts keyed by the field names."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        vals = split_terse(line)
        vals += [""] * (len(fields) - len(vals))
        rows.append(dict(zip(fields, vals)))
    return rows


def parse_show(text):
    """`nmcli -t ... show X` key:value output. Repeated keys (IP4.DNS[1], [2]) become lists
    under the bare key; every value is also a list, so callers index [0] for scalars."""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = split_terse(line)[0], ":".join(split_terse(line)[1:])
        key = re.sub(r"\[\d+\]$", "", key)
        out.setdefault(key, []).append(value)
    return out


def _join_error(stderr):
    for frag, msg in JOIN_ERRORS:
        if frag in stderr:
            return msg
    msg = stderr.strip().splitlines()[-1] if stderr.strip() else "NetworkManager refused the connection"
    return re.sub(r"^Error: Connection activation failed: (\(\d+\) )?", "", msg).rstrip(".")


# -- validation (pure) ---------------------------------------------------------------

def valid_hostname(name):
    name = (name or "").strip().lower()
    if not HOSTNAME_RE.match(name):
        raise ValueError("Hostname: 1 to 63 letters, digits or hyphens, not starting or ending with a hyphen")
    return name


def validate_static(address, prefix, gateway, dns, current):
    """Check a static IPv4 config against the subnet the wall is on now (`current` has
    address/prefix/gateway from Net.status()). Returns normalised values or raises
    ValueError with the reason. Addresses outside the current network are refused because
    nothing could reach the wall there."""
    try:
        addr = ipaddress.IPv4Address(str(address).strip())
    except ValueError:
        raise ValueError("Address is not a valid IPv4 address")
    try:
        prefix = int(prefix)
    except (TypeError, ValueError):
        raise ValueError("Prefix must be a number")
    if not current or not current.get("address"):
        raise ValueError("The wall has no IPv4 address right now; connect it first")
    net = ipaddress.ip_network(f"{current['address']}/{current['prefix']}", strict=False)
    if prefix != net.prefixlen:
        raise ValueError(f"Prefix must be /{net.prefixlen}, the current network's")
    if addr not in net:
        raise ValueError(f"Address must be inside the current network {net.with_prefixlen}")
    if addr == net.network_address or addr == net.broadcast_address:
        raise ValueError("That is the network or broadcast address, not a usable one")
    if addr.is_multicast or addr.is_loopback:
        raise ValueError("Address is not a usable host address")
    gateway = str(gateway or current.get("gateway") or "").strip()
    try:
        gw = ipaddress.IPv4Address(gateway)
    except ValueError:
        raise ValueError("Gateway is not a valid IPv4 address")
    if gw not in net or gw == net.network_address or gw == net.broadcast_address:
        raise ValueError(f"Gateway must be inside {net.with_prefixlen}")
    if gw == addr:
        raise ValueError("Gateway cannot be the wall's own address")
    servers = []
    for d in dns or []:
        d = str(d).strip()
        if not d:
            continue
        try:
            servers.append(str(ipaddress.IPv4Address(d)))
        except ValueError:
            raise ValueError(f"DNS server {d!r} is not a valid IPv4 address")
    if len(servers) > 3:
        raise ValueError("At most 3 DNS servers")
    if not servers:
        servers = [str(gw)]
    return {"address": str(addr), "prefix": prefix, "gateway": str(gw), "dns": servers}


# -- the OS --------------------------------------------------------------------------

class Net:
    def __init__(self, runner=run_subprocess, data_dir="/data", hotspot_ssid="LEDWall-Setup",
                 hotspot_ip="10.42.0.1", device=None):
        self.runner = runner
        self.data_dir = data_dir
        self.hotspot_ssid, self.hotspot_ip = hotspot_ssid, hotspot_ip
        self._device = device or None

    def _run(self, *argv, timeout=30, check=True):
        rc, out, err = self.runner(list(argv), timeout=timeout)
        if rc != 0 and check:
            raise NetError((err or out).strip() or f"{argv[0]} failed ({rc})")
        return out

    def _rows(self, fields, *args, timeout=30):
        return parse_rows(self._run("nmcli", "-t", "-f", ",".join(fields), *args, timeout=timeout), fields)

    def _show(self, *args, fields, timeout=30):
        return parse_show(self._run("nmcli", "-t", "-f", ",".join(fields), *args, timeout=timeout))

    # -- devices and status --

    def devices(self):
        return self._rows(["DEVICE", "TYPE", "STATE", "CONNECTION"], "device", "status")

    def wifi_device(self):
        if not self._device:
            for d in self.devices():
                if d["TYPE"] == "wifi":
                    self._device = d["DEVICE"]
                    break
            else:
                raise NetError("No Wi-Fi device found")
        return self._device

    def connected(self):
        """True when Wi-Fi or Ethernet is up with something other than our hotspot."""
        return any(d["TYPE"] in ("wifi", "ethernet") and d["STATE"].startswith("connected")
                   and d["CONNECTION"] != self.hotspot_ssid for d in self.devices())

    def hotspot_active(self):
        return any(d["CONNECTION"] == self.hotspot_ssid and d["STATE"].startswith("connected") for d in self.devices())

    def hostname(self):
        return self._run("hostname").strip()

    def status(self):
        """The wall's addressing as the UI shows it. Prefers the Wi-Fi device; falls back to
        a connected Ethernet device so a cabled wall still reports its address."""
        devs = self.devices()
        dev = next((d for d in devs if d["TYPE"] == "wifi"), None)
        eth = next((d for d in devs if d["TYPE"] == "ethernet" and d["STATE"].startswith("connected")), None)
        if dev and not dev["STATE"].startswith("connected") and eth:
            dev = eth
        dev = dev or eth
        out = {"device": None, "type": None, "connection": None, "state": "none", "mac": None, "address": None,
               "prefix": None, "gateway": None, "dns": [], "method": None, "ssid": None, "signal": None,
               "hostname": self.hostname(), "hotspot": False}
        if not dev:
            return out
        show = self._show("device", "show", dev["DEVICE"],
                          fields=["GENERAL.HWADDR", "GENERAL.STATE", "GENERAL.CONNECTION", "IP4.ADDRESS", "IP4.GATEWAY", "IP4.DNS"])
        out.update(device=dev["DEVICE"], type=dev["TYPE"], state=dev["STATE"].split(" ")[0],
                   mac=(show.get("GENERAL.HWADDR") or [None])[0], connection=(show.get("GENERAL.CONNECTION") or [""])[0] or None,
                   gateway=(show.get("IP4.GATEWAY") or [None])[0] or None, dns=[d for d in show.get("IP4.DNS", []) if d])
        if show.get("IP4.ADDRESS"):
            a, _, p = show["IP4.ADDRESS"][0].partition("/")
            out["address"], out["prefix"] = a, int(p or 24)
        if out["connection"]:
            con = self._show("connection", "show", out["connection"], fields=["ipv4.method", "802-11-wireless.ssid", "802-11-wireless.mode"])
            out["method"] = {"auto": "dhcp", "manual": "static"}.get((con.get("ipv4.method") or [""])[0], (con.get("ipv4.method") or [None])[0])
            out["ssid"] = (con.get("802-11-wireless.ssid") or [None])[0] or None
            out["hotspot"] = (con.get("802-11-wireless.mode") or [""])[0] == "ap" or out["connection"] == self.hotspot_ssid
        if dev["TYPE"] == "wifi" and out["ssid"] and not out["hotspot"]:
            for n in self._rows(["IN-USE", "SSID", "SIGNAL"], "dev", "wifi", "list", "--rescan", "no"):
                if n["IN-USE"].strip() == "*":
                    out["signal"] = int(n["SIGNAL"] or 0)
                    break
        return out

    # -- Wi-Fi --

    def scan(self):
        """Nearby networks, strongest first, one row per SSID, hidden ones dropped.
        Does not work while the hotspot is up (one radio), so WifiSetup caches it."""
        dev = self.wifi_device()
        best = {}
        for n in self._rows(["SSID", "SIGNAL", "SECURITY"], "dev", "wifi", "list", "ifname", dev, "--rescan", "yes", timeout=40):
            ssid = n["SSID"]
            if not ssid or ssid == self.hotspot_ssid:
                continue
            sig = int(n["SIGNAL"] or 0)
            if ssid in best and best[ssid]["signal"] >= sig:
                continue
            sec = n["SECURITY"].strip()
            best[ssid] = {"ssid": ssid, "signal": sig, "security": sec, "secured": sec not in ("", "--"),
                          "enterprise": "802.1X" in sec}
        return sorted(best.values(), key=lambda x: -x["signal"])

    def known_wifi(self):
        """{ssid: profile name} for saved Wi-Fi profiles (not the hotspot)."""
        out = {}
        for c in self._rows(["NAME", "TYPE"], "connection", "show"):
            if c["TYPE"] != "802-11-wireless" or c["NAME"] == self.hotspot_ssid:
                continue
            show = self._show("connection", "show", c["NAME"], fields=["802-11-wireless.ssid", "802-11-wireless.mode"])
            if (show.get("802-11-wireless.mode") or [""])[0] == "ap":
                continue
            ssid = (show.get("802-11-wireless.ssid") or [""])[0]
            if ssid:
                out[ssid] = c["NAME"]
        return out

    def ensure_stable_dhcp(self, connection):
        """Stable client id + hostname in DHCP, so routers hand back the same lease (spec)."""
        cur = self._show("connection", "show", connection, fields=["ipv4.dhcp-client-id", "ipv4.dhcp-send-hostname"])
        if (cur.get("ipv4.dhcp-client-id") or [""])[0] == "mac" and (cur.get("ipv4.dhcp-send-hostname") or [""])[0] == "yes":
            return False
        self._run("nmcli", "connection", "modify", connection, "ipv4.dhcp-client-id", "mac", "ipv4.dhcp-send-hostname", "yes")
        return True

    def join(self, ssid, password=None, hidden=False):
        """Join a Wi-Fi network and return status(). Raises NetError with a plain reason.
        A failed first attempt leaves no half-made profile behind."""
        if password is not None and password != "" and not 8 <= len(password) <= 63:
            raise NetError("Wi-Fi passwords are 8 to 63 characters")
        dev = self.wifi_device()
        existing = self.known_wifi().get(ssid)
        if existing:
            if password:
                self._run("nmcli", "connection", "modify", existing, "802-11-wireless-security.key-mgmt", "wpa-psk",
                          "802-11-wireless-security.psk", password)
            cmd = ["nmcli", "-w", "45", "connection", "up", existing, "ifname", dev]
        else:
            cmd = ["nmcli", "-w", "45", "dev", "wifi", "connect", ssid, "ifname", dev]
            if password:
                cmd += ["password", password]
            if hidden:
                cmd += ["hidden", "yes"]
        rc, out, err = self.runner(cmd, timeout=60)
        if rc != 0:
            if not existing:
                for name in self.known_wifi().get(ssid, ssid), ssid:
                    self._run("nmcli", "connection", "delete", name, check=False)
            raise NetError(_join_error(err or out))
        name = existing or self.known_wifi().get(ssid) or ssid
        try:
            self.ensure_stable_dhcp(name)
        except NetError as e:
            log.warning("stable DHCP options not set on %s: %s", name, e)
        return self.status()

    # -- hotspot --

    def hotspot_up(self, password):
        """WPA2 AP on the Wi-Fi device in NM shared mode (dnsmasq + NAT). The captive DNS
        comes from system/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf."""
        dev = self.wifi_device()
        exists = any(c["NAME"] == self.hotspot_ssid for c in self._rows(["NAME", "TYPE"], "connection", "show"))
        props = ["802-11-wireless.mode", "ap", "802-11-wireless.band", "bg", "ipv4.method", "shared",
                 "ipv4.addresses", f"{self.hotspot_ip}/24", "ipv6.method", "disabled", "connection.autoconnect", "no",
                 "802-11-wireless-security.key-mgmt", "wpa-psk", "802-11-wireless-security.proto", "rsn",
                 "802-11-wireless-security.pairwise", "ccmp", "802-11-wireless-security.group", "ccmp",
                 "802-11-wireless-security.psk", password]
        if exists:
            self._run("nmcli", "connection", "modify", self.hotspot_ssid, *props)
        else:
            self._run("nmcli", "connection", "add", "type", "wifi", "ifname", dev, "con-name", self.hotspot_ssid,
                      "ssid", self.hotspot_ssid, *props)
        self._run("nmcli", "-w", "30", "connection", "up", self.hotspot_ssid, timeout=45)

    def hotspot_down(self):
        self._run("nmcli", "connection", "down", self.hotspot_ssid, check=False)

    # -- hostname --

    def set_hostname(self, name):
        """hostnamectl (polkit lets the service user do it); /etc/hosts is rewritten as root by
        ledposter-hosts.service (system/systemd), which also nudges Avahi."""
        name = valid_hostname(name)
        self._run("hostnamectl", "set-hostname", name)
        rc, _, err = self.runner(["systemctl", "start", HOSTS_SERVICE], timeout=30)
        if rc != 0:
            log.warning("/etc/hosts not updated (%s): %s", HOSTS_SERVICE, err.strip())
        return name

    # -- static IP with rollback --

    def address_in_use(self, address):
        """Does something answer at that address already? ping is on every Pi; arping is not."""
        rc, _, _ = self.runner(["ping", "-c", "1", "-W", "1", address], timeout=5)
        return rc == 0

    def _rollback_path(self):
        return os.path.join(self.data_dir, "net-rollback")

    def rollback_pending(self):
        try:
            with open(self._rollback_path()) as f:
                con, deadline, address = (f.read().split("\n") + ["", "", ""])[:3]
        except OSError:
            return None
        deadline = float(deadline or 0)
        return {"connection": con, "address": address, "deadline": deadline,
                "seconds_left": max(0, int(deadline - time.time()))}

    def arm_rollback(self, connection, address, seconds=ROLLBACK_SECONDS):
        """Write the rollback note, then (re)start the systemd timer. Refuses to leave the
        note behind if the timer could not be armed, so set_static is never run unprotected."""
        p = self._rollback_path()
        os.makedirs(self.data_dir, exist_ok=True)
        with open(p, "w") as f:
            f.write(f"{connection}\n{int(time.time() + seconds)}\n{address}\n")
        rc, _, err = self.runner(["systemctl", "restart", ROLLBACK_TIMER], timeout=30)
        if rc != 0:
            os.unlink(p)
            raise NetError(f"Could not arm the DHCP rollback timer, so nothing was changed: {err.strip() or rc}")

    def cancel_rollback(self):
        try:
            os.unlink(self._rollback_path())
        except OSError:
            pass
        self.runner(["systemctl", "stop", ROLLBACK_TIMER], timeout=30)

    def _reapply(self, connection):
        """Apply changed IP settings without dropping the radio when NM allows it."""
        dev = next((d["DEVICE"] for d in self.devices() if d["CONNECTION"] == connection), None)
        rc = 1
        if dev:
            rc, _, err = self.runner(["nmcli", "device", "reapply", dev], timeout=30)
        if rc != 0:
            self._run("nmcli", "-w", "30", "connection", "up", connection, timeout=45)

    def set_static(self, connection, address, prefix, gateway, dns):
        self._run("nmcli", "connection", "modify", connection, "ipv4.method", "manual",
                  "ipv4.addresses", f"{address}/{prefix}", "ipv4.gateway", gateway, "ipv4.dns", " ".join(dns),
                  "ipv4.ignore-auto-dns", "yes")
        self._reapply(connection)

    def set_dhcp(self, connection):
        """Mirrors `ledposter-net rollback` (system/bin); keep the two in step."""
        self._run("nmcli", "connection", "modify", connection, "ipv4.method", "auto", "ipv4.addresses", "",
                  "ipv4.gateway", "", "ipv4.dns", "", "ipv4.ignore-auto-dns", "no")
        self._reapply(connection)
