"""/api/network/* and the captive portal through the app, with the fake engine and fake
NetworkManager: auth rules in setup mode, probe redirects, static IP confirm flow."""
import time

from fastapi.testclient import TestClient

from conftest import PASSWORD

PROBES = ["/hotspot-detect.html", "/generate_204", "/connecttest.txt", "/ncsi.txt"]


def wait_for(pred, timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def start_hotspot(client):
    """Flip the app into setup mode the way the button would (status marker change)."""
    client.app.state.setup.state.update(mode="hotspot", hotspot_up=True)


def test_network_status_and_hostname(authed):
    r = authed.get("/api/network")
    assert r.status_code == 200
    n = r.json()
    assert n["ssid"] == "Digiana" and n["address"] == "192.168.1.23" and n["method"] == "dhcp" and n["mac"] == "B8:27:EB:12:34:56"
    assert n["hostname"] == "ledposter" and n["rollback"] is None and n["setup_mode"] is False and n["signal"] == 82
    assert authed.put("/api/network/hostname", json={"hostname": "bad name"}).status_code == 422
    r = authed.put("/api/network/hostname", json={"hostname": "Wall-Two"})
    assert r.status_code == 200 and r.json() == {"hostname": "wall-two", "url": "http://wall-two.local/"}
    assert authed.get("/api/network").json()["hostname"] == "wall-two"
    assert authed.post("/api/network/show_ip").status_code == 200


def test_double_press_shows_ip_on_the_wall(authed, engine):
    assert wait_for(lambda: authed.app.state.setup._double_seen == 0)  # the loop has a baseline
    n = len(engine.texts)
    engine.state["button_double_count"] = 1  # what a double press does in the engine
    assert wait_for(lambda: ("ledposter.local\n192.168.1.23", 10) in engine.texts[n:], timeout=8)


def test_show_ip_reaches_the_wall(authed, engine):
    authed.post("/api/network/show_ip")
    assert wait_for(lambda: ("ledposter.local\n192.168.1.23", 10) in engine.texts)


def test_network_endpoints_need_auth(authed):
    anon = TestClient(authed.app)
    for path in ("/api/network", "/api/network/setup", "/api/network/wifi"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/api/network/static", json={"address": "192.168.1.50", "prefix": 24}).status_code == 401
    assert anon.post("/api/network/wifi/join", json={"ssid": "Digiana", "password": "hunter2hunter2"}).status_code == 401
    assert anon.post("/api/network/setup/start").status_code == 401
    assert anon.get("/setup").status_code == 200  # the page itself is public, like /login


def test_captive_probes_redirect_only_in_setup_mode(authed):
    anon = TestClient(authed.app)
    for p in PROBES:
        assert anon.get(p).status_code == 404, p
    start_hotspot(authed)
    for p in PROBES:
        r = anon.get(p, follow_redirects=False, headers={"host": "captive.apple.com"})
        assert r.status_code == 302 and r.headers["location"] == "http://10.42.0.1/setup", p
    assert anon.get("/api/network").status_code == 401  # the rest stays locked


def test_setup_endpoints_open_only_on_first_run_in_setup_mode(client):
    # fresh wall, no admin password, hotspot up: the portal works without login
    assert client.get("/api/network/setup").status_code == 401  # not in setup mode yet
    start_hotspot(client)
    r = client.get("/api/network/setup")
    assert r.status_code == 200 and r.json()["first_run"] is True and r.json()["authenticated"] is False and r.json()["ssid"] == "LEDWall-Setup"
    assert client.get("/api/network/wifi").status_code == 200
    assert client.get("/api/network").status_code == 401  # everything else still needs the admin
    # admin password set (the wizard does this first): now the portal needs the session too
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/network/setup").status_code == 200 and client.get("/api/network/setup").json()["authenticated"]
    anon = TestClient(client.app)
    assert anon.get("/api/network/setup").status_code == 401
    assert anon.post("/api/network/wifi/join", json={"ssid": "Digiana", "password": "hunter2hunter2"}).status_code == 401


def test_join_flow_through_api(authed, engine):
    fake = authed.fakenet
    fake.active = None  # pretend we booted without a network
    authed.app.state.setup.state["networks"] = []
    assert authed.post("/api/network/setup/start").status_code == 200
    st = authed.get("/api/network/setup").json()
    assert st["mode"] == "hotspot" and st["hotspot_up"] and fake.active == "LEDWall-Setup"
    wifi = authed.get("/api/network/wifi").json()
    assert wifi["setup_mode"] and [n["ssid"] for n in wifi["networks"]][0] == "Digiana"
    assert authed.post("/api/network/wifi/join", json={"ssid": "Digiana", "password": "short"}).status_code == 422
    r = authed.post("/api/network/wifi/join", json={"ssid": "Digiana", "password": "nope-nope-nope"})
    assert r.status_code == 200 and r.json()["mode"] == "joining"
    assert wait_for(lambda: authed.get("/api/network/setup").json()["mode"] == "failed")
    st = authed.get("/api/network/setup").json()
    assert st["reason"] == "Wrong password" and st["hotspot_up"] and fake.active == "LEDWall-Setup"
    assert wait_for(lambda: any(t.startswith("Wi-Fi failed\nWrong password") for t, _ in engine.texts))
    r = authed.post("/api/network/wifi/join", json={"ssid": "Digiana", "password": "hunter2hunter2"})
    assert r.status_code == 200
    assert wait_for(lambda: authed.get("/api/network/setup").json()["mode"] == "joined")
    st = authed.get("/api/network/setup").json()
    assert st["ip"] == "192.168.1.23" and not st["hotspot_up"] and fake.active == "Digiana"
    assert wait_for(lambda: ("Joined Digiana\n192.168.1.23\nhttp://ledposter.local", 15) in engine.texts)
    assert authed.get("/api/network").json()["setup_mode"] is False
    assert authed.get("/hotspot-detect.html").status_code == 404


def test_button_hold_raises_hotspot_through_the_engine_status(authed, engine):
    fake = authed.fakenet
    engine.hold_button()  # what the 10s press sets in the status (docs/protocol.md)
    assert wait_for(lambda: authed.get("/api/network/setup").json()["hotspot_up"], timeout=8)
    assert fake.active == "LEDWall-Setup" and authed.get("/api/network/setup").json()["trigger"] == "Button held"
    assert wait_for(lambda: any(c.get("cmd") == "ack_wifi_setup" for c in engine.calls))
    assert engine.state["wifi_setup_requested"] is False and engine.state["wifi_setup_count"] == 1
    assert any(t.startswith("Wi-Fi setup\nJoin LEDWall-Setup") for t, _ in engine.texts)


def test_static_ip_confirm_and_rollback_through_api(authed):
    fake = authed.fakenet
    bad = authed.post("/api/network/static", json={"address": "10.0.0.5", "prefix": 24})
    assert bad.status_code == 422 and "current network" in bad.json()["detail"]
    assert authed.post("/api/network/static", json={"address": "192.168.1.50", "prefix": 24}).status_code == 409  # answers ping
    r = authed.post("/api/network/static", json={"address": "192.168.1.51", "prefix": 24, "dns": ["1.1.1.1"]})
    assert r.status_code == 200
    assert r.json() == {"address": "192.168.1.51", "url": "http://192.168.1.51/#/settings/confirm", "seconds": 120}
    assert fake.timer_active and fake.profiles["Digiana"]["ipv4.method"] == "auto"  # armed first, applied after the response
    # confirming before the delayed apply ran must wait for it (never an unprotected change)
    r = authed.post("/api/network/static/confirm")
    assert r.status_code == 200 and r.json()["address"] == "192.168.1.51" and r.json()["rollback"] is None
    assert fake.profiles["Digiana"]["ipv4.method"] == "manual" and not fake.timer_active
    assert authed.post("/api/network/static/confirm").status_code == 409
    # second round: apply completes, status shows the trial, then confirm
    assert authed.post("/api/network/dhcp").status_code == 200
    assert wait_for(lambda: fake.profiles["Digiana"]["ipv4.method"] == "auto")
    r = authed.post("/api/network/static", json={"address": "192.168.1.51", "prefix": 24, "dns": ["1.1.1.1"]})
    assert r.status_code == 200
    assert wait_for(lambda: fake.profiles["Digiana"]["ipv4.method"] == "manual")
    n = authed.get("/api/network").json()
    assert n["address"] == "192.168.1.51" and n["method"] == "static" and n["rollback"]["address"] == "192.168.1.51" and n["rollback"]["seconds_left"] > 100
    r = authed.post("/api/network/static/confirm")
    assert r.status_code == 200 and r.json()["rollback"] is None and not fake.timer_active
    assert authed.post("/api/network/static/confirm").status_code == 409
    # back to DHCP from the app (what the timer would have done)
    r = authed.post("/api/network/dhcp")
    assert r.status_code == 200 and r.json()["url"] == "http://ledposter.local/"
    assert wait_for(lambda: fake.profiles["Digiana"]["ipv4.method"] == "auto")
    assert authed.get("/api/network").json()["address"] == "192.168.1.23"
    # a timer that cannot be armed means nothing is applied
    fake.errors["systemctl restart"] = (1, "Access denied")
    r = authed.post("/api/network/static", json={"address": "192.168.1.52", "prefix": 24})
    assert r.status_code == 503 and "nothing was changed" in r.json()["detail"]
    time.sleep(0.3)
    assert fake.profiles["Digiana"]["ipv4.method"] == "auto"


def test_network_unavailable_is_a_clean_error(data, engine):
    def no_nm(argv, timeout=None):
        return 127, "", "nmcli: command not found"
    from ledposter.app import create_app
    with TestClient(create_app(str(data), net_runner=no_nm)) as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        r = c.get("/api/network")
        assert r.status_code == 503 and "nmcli" in r.json()["detail"]
        assert c.get("/api/status").status_code == 200  # the rest of the app is unaffected
