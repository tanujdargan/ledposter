"""A fake NetworkManager behind ledposter.net's runner interface: answers the exact nmcli,
hostnamectl, systemctl, hostname and ping command lines net.py builds, with terse-mode
escaping, so tests and `LEDPOSTER_FAKE_NET=1` dev never touch this machine's network.

Scenario: one Wi-Fi device wlan0 (MAC B8:27:EB:12:34:56), a few visible networks (two APs
share the SSID "Digiana", "Cafe Free" is open, "Corp" is 802.1X), a saved profile for
"Digiana" that is connected unless `connected=False`. Passwords are in NETWORKS.
"""
import shlex

MAC = "B8:27:EB:12:34:56"
NETWORKS = [  # ssid, signal, security, password
    ("Digiana", 82, "WPA2", "hunter2hunter2"),
    ("Digiana", 61, "WPA2", "hunter2hunter2"),
    ("Cafe Free", 70, "", None),
    ("Corp", 55, "WPA2 802.1X", None),
    ("Neighbour 5G", 34, "WPA2 WPA3", "whoknows1"),
]
LEASES = {"Digiana": ("192.168.1.23/24", "192.168.1.1", ["192.168.1.1", "1.1.1.1"]),
          "Cafe Free": ("172.16.4.9/22", "172.16.4.1", ["172.16.4.1"])}


def esc(v):
    return str(v).replace("\\", "\\\\").replace(":", "\\:")


class FakeNet:
    def __init__(self, connected=True, hostname="ledposter", networks=None):
        self.calls = []
        self.networks = list(networks or NETWORKS)
        self.hostname = hostname
        self.hosts_synced = hostname
        self.profiles = {}
        self.active = None  # profile name on wlan0
        self.units = []  # (verb, unit) from systemctl
        self.timer_active = False
        self.in_use = {"192.168.1.1", "192.168.1.50"}
        self.errors = {}  # substring of the command line -> (rc, stderr) to inject a failure
        if connected:
            self.add_profile("Digiana", ssid="Digiana", psk="hunter2hunter2")
            self.active = "Digiana"

    def add_profile(self, name, ssid=None, psk=None, mode="infrastructure", method="auto", **kw):
        p = {"connection.type": "802-11-wireless", "802-11-wireless.ssid": ssid or name, "802-11-wireless.mode": mode,
             "ipv4.method": method, "ipv4.addresses": "", "ipv4.gateway": "", "ipv4.dns": "", "ipv4.ignore-auto-dns": "no",
             "ipv4.dhcp-client-id": "", "ipv4.dhcp-send-hostname": "yes", "connection.autoconnect": "yes",
             "802-11-wireless-security.key-mgmt": "wpa-psk" if psk else "", "802-11-wireless-security.psk": psk or ""}
        p.update(kw)
        self.profiles[name] = p
        return p

    # -- what the device looks like right now --

    def _ip(self):
        """(address/prefix, gateway, dns list) of wlan0 or None."""
        if not self.active:
            return None
        p = self.profiles[self.active]
        if p["802-11-wireless.mode"] == "ap":
            return (p["ipv4.addresses"] or "10.42.0.1/24", "", [])
        if p["ipv4.method"] == "manual":
            return (p["ipv4.addresses"], p["ipv4.gateway"], p["ipv4.dns"].split())
        return LEASES.get(p["802-11-wireless.ssid"], ("192.168.1.77/24", "192.168.1.1", ["192.168.1.1"]))

    def _devices(self):
        state = "connected" if self.active else "disconnected"
        return [{"DEVICE": "wlan0", "TYPE": "wifi", "STATE": state, "CONNECTION": self.active or ""},
                {"DEVICE": "eth0", "TYPE": "ethernet", "STATE": "unavailable", "CONNECTION": ""},
                {"DEVICE": "lo", "TYPE": "loopback", "STATE": "connected (externally)", "CONNECTION": "lo"}]

    # -- runner entry point --

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        line = shlex.join(argv)
        for frag, (rc, err) in self.errors.items():
            if frag in line:
                return rc, "", err
        try:
            return getattr(self, "_" + argv[0].replace("-", "_"))(argv[1:])
        except AttributeError:
            return 127, "", f"{argv[0]}: command not found"

    def _hostname(self, a):
        return 0, self.hostname + "\n", ""

    def _hostnamectl(self, a):
        assert a[0] == "set-hostname"
        self.hostname = a[1]
        return 0, "", ""

    def _systemctl(self, a):
        verb, unit = a[0], a[1]
        self.units.append((verb, unit))
        if unit == "ledposter-net-rollback.timer":
            self.timer_active = verb in ("start", "restart")
        elif unit == "ledposter-hosts.service":
            self.hosts_synced = self.hostname
        else:
            return 5, "", f"Failed to {verb} {unit}: Unit {unit} not found."
        return 0, "", ""

    def _ping(self, a):
        return (0, "1 packets transmitted, 1 received", "") if a[-1] in self.in_use else (1, "", "")

    def _nmcli(self, a):
        fields, wait = None, None
        a = list(a)
        while a and a[0].startswith("-"):
            opt = a.pop(0)
            if opt == "-f":
                fields = a.pop(0).split(",")
            elif opt == "-w":
                wait = a.pop(0)
        obj, verb, rest = a[0], a[1], a[2:]
        if obj in ("device", "dev"):
            if verb == "status":
                return 0, "".join(":".join(esc(d[f]) for f in fields) + "\n" for d in self._devices()), ""
            if verb == "show":
                return self._device_show(rest[0], fields)
            if verb == "wifi" and rest[0] == "list":
                return self._wifi_list(fields, rest)
            if verb == "wifi" and rest[0] == "connect":
                return self._wifi_connect(rest[1:])
            if verb == "reapply":
                return (0, "Connection successfully reapplied to device 'wlan0'.\n", "") if self.active else (10, "", "Error: not connected")
        if obj in ("connection", "con"):
            if verb == "show" and not rest:
                out = "".join(":".join(esc({"NAME": n, "TYPE": p["connection.type"]}[f]) for f in fields) + "\n" for n, p in self.profiles.items())
                return 0, out, ""
            if verb == "show":
                p = self.profiles.get(rest[0])
                if not p:
                    return 10, "", f"Error: {rest[0]} - no such connection profile."
                return 0, "".join(f"{f}:{esc(p.get(f, ''))}\n" for f in fields), ""
            if verb == "modify":
                return self._modify(rest)
            if verb == "add":
                return self._add(rest)
            if verb == "delete":
                if rest[0] not in self.profiles:
                    return 10, "", f"Error: unknown connection '{rest[0]}'."
                if self.active == rest[0]:
                    self.active = None
                del self.profiles[rest[0]]
                return 0, f"Connection '{rest[0]}' successfully deleted.\n", ""
            if verb == "up":
                return self._up(rest[0])
            if verb == "down":
                if self.active == rest[0]:
                    self.active = None
                    return 0, f"Connection '{rest[0]}' successfully deactivated\n", ""
                return 10, "", f"Error: '{rest[0]}' is not an active connection."
        return 2, "", f"Error: unsupported fake nmcli command: {shlex.join(a)}"

    def _device_show(self, dev, fields):
        if dev != "wlan0":
            return 10, "", f"Error: Device '{dev}' not found."
        ip = self._ip()
        vals = {"GENERAL.HWADDR": [MAC], "GENERAL.STATE": ["100 (connected)" if self.active else "30 (disconnected)"],
                "GENERAL.CONNECTION": [self.active or ""], "IP4.ADDRESS": [ip[0]] if ip else [],
                "IP4.GATEWAY": [ip[1]] if ip else [""], "IP4.DNS": ip[2] if ip else []}
        out = ""
        for f in fields:
            v = vals.get(f, [""])
            for i, x in enumerate(v):
                key = f"{f}[{i + 1}]" if f in ("IP4.ADDRESS", "IP4.DNS") else f
                out += f"{key}:{esc(x)}\n"
        return 0, out, ""

    def _wifi_list(self, fields, rest):
        if self.active and self.profiles[self.active]["802-11-wireless.mode"] == "ap":
            return 0, "", ""  # one radio: no scanning while the AP is up
        cur = self.profiles[self.active]["802-11-wireless.ssid"] if self.active else None
        out = ""
        for ssid, sig, sec, _ in sorted(self.networks, key=lambda n: -n[1]):
            row = {"SSID": ssid, "SIGNAL": sig, "SECURITY": sec, "IN-USE": "*" if ssid == cur else " ", "BSSID": MAC}
            out += ":".join(esc(row[f]) for f in fields) + "\n"
            cur = None  # IN-USE marks one row only
        return 0, out, ""

    def _wifi_connect(self, a):
        ssid, opts = a[0], dict(zip(a[1::2], a[2::2]))
        name = ssid if ssid not in self.profiles else f"{ssid} 1"
        self.add_profile(name, ssid=ssid, psk=opts.get("password"))  # NM saves the profile before it knows
        return self._up(name)

    def _up(self, name):
        p = self.profiles.get(name)
        if not p:
            return 10, "", f"Error: unknown connection '{name}'."
        if p["802-11-wireless.mode"] == "ap":
            self.active = name
            return 0, "Connection successfully activated\n", ""
        ssid = p["802-11-wireless.ssid"]
        known = [n for n in self.networks if n[0] == ssid]
        if not known:
            return 10, "", f"Error: No network with SSID '{ssid}' found."
        if "802.1X" in known[0][2]:
            return 4, "", "Error: Connection activation failed: (7) Secrets were required, but not provided (802.1X)."
        if known[0][3] and p["802-11-wireless-security.psk"] != known[0][3]:
            return 4, "", "Error: Connection activation failed: (7) Secrets were required, but not provided."
        self.active = name
        return 0, f"Device 'wlan0' successfully activated with '{name}'.\n", ""

    def _modify(self, a):
        p = self.profiles.get(a[0])
        if not p:
            return 10, "", f"Error: unknown connection '{a[0]}'."
        p.update(dict(zip(a[1::2], a[2::2])))
        return 0, "", ""

    def _add(self, a):
        kv = dict(zip(a[0::2], a[1::2]))
        name = kv.pop("con-name")
        kv.pop("type"), kv.pop("ifname", None)
        ssid = kv.pop("ssid")
        self.add_profile(name, ssid=ssid, **kv)
        return 0, f"Connection '{name}' successfully added.\n", ""


def fake_runner_from_env(value):
    """`LEDPOSTER_FAKE_NET=1` connected, `offline` for the boot-without-network path."""
    return FakeNet(connected=value != "offline").run
