"""ledposter.net against the fake runner: nmcli parsing, command construction, static IP
validation and the apply/confirm/rollback sequence. Nothing here touches the real network."""
import pytest

from fake_net import FakeNet
from ledposter import net


def test_terse_parsing_handles_escaped_colons():
    assert net.split_terse("wlan0:wifi:connected:Home\\:Net") == ["wlan0", "wifi", "connected", "Home:Net"]
    assert net.split_terse("a\\\\b:c") == ["a\\b", "c"]
    rows = net.parse_rows("wlan0:wifi:connected:Digiana\nlo:loopback:connected (externally):lo\n", ["DEVICE", "TYPE", "STATE", "CONNECTION"])
    assert rows[0] == {"DEVICE": "wlan0", "TYPE": "wifi", "STATE": "connected", "CONNECTION": "Digiana"}
    show = net.parse_show("GENERAL.HWADDR:B8\\:27\\:EB\\:12\\:34\\:56\nIP4.ADDRESS[1]:192.168.1.23/24\nIP4.DNS[1]:1.1.1.1\nIP4.DNS[2]:8.8.8.8\n")
    assert show["GENERAL.HWADDR"] == ["B8:27:EB:12:34:56"]
    assert show["IP4.ADDRESS"] == ["192.168.1.23/24"] and show["IP4.DNS"] == ["1.1.1.1", "8.8.8.8"]


def test_status_and_scan(tmp_path):
    f = FakeNet()
    n = net.Net(f.run, str(tmp_path))
    s = n.status()
    assert s["device"] == "wlan0" and s["ssid"] == "Digiana" and s["signal"] == 82 and s["mac"] == "B8:27:EB:12:34:56"
    assert s["address"] == "192.168.1.23" and s["prefix"] == 24 and s["gateway"] == "192.168.1.1" and s["dns"] == ["192.168.1.1", "1.1.1.1"]
    assert s["method"] == "dhcp" and s["hostname"] == "ledposter" and s["hotspot"] is False
    assert n.connected() and not n.hotspot_active()
    assert ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"] in f.calls
    nets = n.scan()
    assert [x["ssid"] for x in nets] == ["Digiana", "Cafe Free", "Corp", "Neighbour 5G"]  # deduped, strongest first
    assert nets[0]["signal"] == 82 and nets[0]["secured"] and not nets[1]["secured"] and nets[2]["enterprise"]
    assert f.calls[-1][:3] == ["nmcli", "-t", "-f"] and "--rescan" in f.calls[-1] and "yes" in f.calls[-1]
    # offline: no address, no ssid
    f2 = FakeNet(connected=False)
    s2 = net.Net(f2.run, str(tmp_path)).status()
    assert s2["address"] is None and s2["ssid"] is None and s2["state"] == "disconnected"


def test_join_commands_and_errors(tmp_path):
    f = FakeNet(connected=False)
    n = net.Net(f.run, str(tmp_path))
    with pytest.raises(net.NetError, match="Wrong password"):
        n.join("Digiana", "nope-nope-nope")
    assert "Digiana" not in f.profiles  # the half-made profile was deleted
    with pytest.raises(net.NetError, match="Network not found"):
        n.join("Nope", "whatever123")
    with pytest.raises(net.NetError, match="enterprise"):
        n.join("Corp", "whatever123")
    with pytest.raises(net.NetError, match="8 to 63"):
        n.join("Digiana", "short")
    s = n.join("Digiana", "hunter2hunter2")
    assert s["ssid"] == "Digiana" and s["address"] == "192.168.1.23"
    connect = [c for c in f.calls if c[:5] == ["nmcli", "-w", "45", "dev", "wifi"]][-1]
    assert connect == ["nmcli", "-w", "45", "dev", "wifi", "connect", "Digiana", "ifname", "wlan0", "password", "hunter2hunter2"]
    # stable DHCP client id + hostname were set on the new profile (spec: same lease after reboots)
    assert f.profiles["Digiana"]["ipv4.dhcp-client-id"] == "mac" and f.profiles["Digiana"]["ipv4.dhcp-send-hostname"] == "yes"
    assert n.ensure_stable_dhcp("Digiana") is False  # already set: no modify
    # known network: modify the saved profile's password and bring it up, no new profile
    before = len(f.profiles)
    n.join("Digiana", "hunter2hunter2")
    assert len(f.profiles) == before and ["nmcli", "-w", "45", "connection", "up", "Digiana", "ifname", "wlan0"] in f.calls
    # open network and hidden flag
    n.join("Cafe Free")
    assert ["nmcli", "-w", "45", "dev", "wifi", "connect", "Cafe Free", "ifname", "wlan0"] in f.calls
    f.networks.append(("Secret", 50, "WPA2", "hiddenpass1"))
    n.join("Secret", "hiddenpass1", hidden=True)
    assert any(c[-2:] == ["hidden", "yes"] for c in f.calls)


def test_hotspot_is_wpa2_shared_and_toggles(tmp_path):
    f = FakeNet()
    n = net.Net(f.run, str(tmp_path))
    n.hotspot_up("cardpassword")
    add = next(c for c in f.calls if c[:3] == ["nmcli", "connection", "add"])
    kv = dict(zip(add[3::2], add[4::2]))
    assert kv["con-name"] == "LEDWall-Setup" and kv["ssid"] == "LEDWall-Setup" and kv["802-11-wireless.mode"] == "ap"
    assert kv["ipv4.method"] == "shared" and kv["ipv4.addresses"] == "10.42.0.1/24" and kv["connection.autoconnect"] == "no"
    assert kv["802-11-wireless-security.key-mgmt"] == "wpa-psk" and kv["802-11-wireless-security.proto"] == "rsn"
    assert kv["802-11-wireless-security.psk"] == "cardpassword"
    assert n.hotspot_active() and not n.connected()
    s = n.status()
    assert s["hotspot"] and s["address"] == "10.42.0.1"
    assert n.scan() == []  # one radio: nothing while the AP is up
    n.hotspot_down()
    assert not n.hotspot_active()
    n.hotspot_up("newpassword")  # second time: modify, not add
    assert f.profiles["LEDWall-Setup"]["802-11-wireless-security.psk"] == "newpassword"
    assert sum(1 for c in f.calls if c[:3] == ["nmcli", "connection", "add"]) == 1


def test_hostname(tmp_path):
    f = FakeNet()
    n = net.Net(f.run, str(tmp_path))
    for bad in ("", "-abc", "abc-", "a b", "a.b", "x" * 64, "ünïcode"):
        with pytest.raises(ValueError):
            net.valid_hostname(bad)
    assert net.valid_hostname(" LED-Wall2 ") == "led-wall2"
    assert n.set_hostname("Wall-Two") == "wall-two"
    assert ["hostnamectl", "set-hostname", "wall-two"] in f.calls
    assert f.hostname == "wall-two" and f.hosts_synced == "wall-two"  # /etc/hosts via ledposter-hosts.service
    assert ("start", "ledposter-hosts.service") in f.units


def test_static_validation():
    cur = {"address": "192.168.1.23", "prefix": 24, "gateway": "192.168.1.1"}
    ok = net.validate_static("192.168.1.50", 24, None, [], cur)
    assert ok == {"address": "192.168.1.50", "prefix": 24, "gateway": "192.168.1.1", "dns": ["192.168.1.1"]}
    assert net.validate_static(" 192.168.1.51 ", "24", "192.168.1.254", ["1.1.1.1", " 8.8.8.8 "], cur)["dns"] == ["1.1.1.1", "8.8.8.8"]
    bad = [("10.0.0.5", 24, None, [], "inside the current network"),
           ("192.168.1.0", 24, None, [], "network or broadcast"),
           ("192.168.1.255", 24, None, [], "network or broadcast"),
           ("192.168.1.50", 16, None, [], "Prefix must be /24"),
           ("192.168.1.50", 24, "192.168.2.1", [], "Gateway must be inside"),
           ("192.168.1.50", 24, "192.168.1.50", [], "own address"),
           ("192.168.1.50", 24, None, ["dns.example"], "DNS server"),
           ("192.168.1.50", 24, None, ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"], "At most 3"),
           ("not-an-ip", 24, None, [], "not a valid IPv4"),
           ("192.168.1.50", "x", None, [], "Prefix must be a number")]
    for addr, prefix, gw, dns, msg in bad:
        with pytest.raises(ValueError, match=msg):
            net.validate_static(addr, prefix, gw, dns, cur)
    with pytest.raises(ValueError, match="no IPv4 address"):
        net.validate_static("192.168.1.50", 24, None, [], {"address": None})


def test_static_apply_confirm_and_rollback_sequence(tmp_path):
    f = FakeNet()
    n = net.Net(f.run, str(tmp_path))
    assert n.rollback_pending() is None
    assert n.address_in_use("192.168.1.50") and not n.address_in_use("192.168.1.51")
    # 1. arm: note written, timer (re)started, nothing else changed yet
    n.arm_rollback("Digiana", "192.168.1.51")
    rb = n.rollback_pending()
    assert rb["connection"] == "Digiana" and rb["address"] == "192.168.1.51" and 118 <= rb["seconds_left"] <= 120
    assert f.units == [("restart", "ledposter-net-rollback.timer")] and f.timer_active
    assert (tmp_path / "net-rollback").read_text().split("\n")[0] == "Digiana"
    assert f.profiles["Digiana"]["ipv4.method"] == "auto"
    # 2. apply: manual method, address, gateway, dns; reapplied live
    n.set_static("Digiana", "192.168.1.51", 24, "192.168.1.1", ["1.1.1.1"])
    p = f.profiles["Digiana"]
    assert p["ipv4.method"] == "manual" and p["ipv4.addresses"] == "192.168.1.51/24" and p["ipv4.gateway"] == "192.168.1.1" and p["ipv4.dns"] == "1.1.1.1"
    assert ["nmcli", "device", "reapply", "wlan0"] in f.calls
    assert n.status()["address"] == "192.168.1.51" and n.status()["method"] == "static"
    # 3a. confirm: timer stopped, note gone, address stays
    n.cancel_rollback()
    assert n.rollback_pending() is None and not f.timer_active and f.units[-1] == ("stop", "ledposter-net-rollback.timer")
    assert n.status()["address"] == "192.168.1.51"
    # 3b. no confirm: what the timer runs (system/bin/ledposter-net rollback) is set_dhcp's mirror
    n.set_dhcp("Digiana")
    assert f.profiles["Digiana"]["ipv4.method"] == "auto" and f.profiles["Digiana"]["ipv4.addresses"] == ""
    assert n.status()["address"] == "192.168.1.23" and n.status()["method"] == "dhcp"
    # arming must fail closed when the timer cannot be started
    f.errors["systemctl restart ledposter-net-rollback.timer"] = (1, "Failed to restart: Access denied")
    with pytest.raises(net.NetError, match="rollback timer"):
        n.arm_rollback("Digiana", "192.168.1.52")
    assert n.rollback_pending() is None  # no note left behind


def test_reapply_falls_back_to_connection_up(tmp_path):
    f = FakeNet()
    f.errors["nmcli device reapply wlan0"] = (2, "Error: Reapply failed")
    n = net.Net(f.run, str(tmp_path))
    n.set_static("Digiana", "192.168.1.51", 24, "192.168.1.1", ["1.1.1.1"])
    assert ["nmcli", "-w", "30", "connection", "up", "Digiana"] in f.calls


def test_real_runner_reports_missing_binary():
    with pytest.raises(net.NetError, match="not installed"):
        net.run_subprocess(["definitely-not-a-command-ledposter"])
