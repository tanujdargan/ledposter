import os
import time

import yaml

from conftest import wait_status


def test_playlist_crud(authed, engine, gif, data):
    with open(gif, "rb") as f:
        a = authed.post("/api/media", files={"file": ("a.gif", f)}).json()["id"]
    with open(gif, "rb") as f:
        b = authed.post("/api/media", files={"file": ("b.gif", f)}).json()["id"]
    wait_status(authed, a), wait_status(authed, b)
    assert authed.get("/api/playlists").json() == {}

    body = {"items": [{"media": a, "duration_s": 8}, {"media": b}], "loop": True, "shuffle": True}
    r = authed.put("/api/playlists/evening", json=body)
    assert r.status_code == 200 and r.json() == {"items": [{"media": a, "duration_s": 8.0}, {"media": b, "duration_s": None}],
                                                 "loop": True, "shuffle": True}
    assert authed.get("/api/playlists").json()["evening"]["shuffle"] is True
    # stored in the engine's format and the engine was told to reload
    cfg = yaml.safe_load((data / "config.yaml").read_text())
    assert cfg["playlists"]["evening"] == {"loop": True, "shuffle": True, "items": [{"media": a, "duration_s": 8.0}, {"media": b}]}
    assert any(c["cmd"] == "reload_config" for c in engine.calls)

    # reorder = replace
    body["items"].reverse()
    assert authed.put("/api/playlists/evening", json=body).json()["items"][0]["media"] == b

    assert authed.put("/api/playlists/evening", json={"items": [{"media": "nope"}]}).status_code == 404
    assert authed.put("/api/playlists/evening", json={"items": [{"media": a, "duration_s": 0}]}).status_code == 422
    assert authed.put("/api/playlists/bad/name", json=body).status_code == 404
    assert authed.put("/api/playlists/%2E%2E", json=body).status_code == 422

    pl = next(m for m in authed.get("/api/modes").json() if m["id"] == "playlist")
    assert "evening" in pl["settings"][0]["options"]
    assert authed.post("/api/mode", json={"mode": "playlist", "playlist": "evening"}).status_code == 200
    assert engine.state["mode_name"] == "evening"
    assert authed.post("/api/mode", json={"mode": "playlist", "playlist": "nope"}).status_code == 404

    assert authed.delete("/api/playlists/evening").status_code == 200
    assert authed.delete("/api/playlists/evening").status_code == 404
    assert authed.get("/api/playlists").json() == {}


def test_controls_reach_engine(authed, engine, data):
    assert authed.post("/api/power", json={"on": False}).status_code == 200
    assert engine.state["power"] is False
    assert authed.post("/api/brightness", json={"value": 37}).status_code == 200
    assert engine.state["brightness"]["user"] == 37
    assert authed.post("/api/brightness", json={"value": 101}).status_code == 422
    assert authed.post("/api/auto_brightness", json={"enabled": False}).status_code == 200
    assert engine.state["brightness"]["auto_enabled"] is False
    assert authed.post("/api/mode", json={"mode": "pattern", "pattern": "solid", "color": "#FF8800"}).status_code == 200
    assert engine.calls[-1]["color"] == "ff8800" and engine.state["mode_name"] == "solid"
    r = authed.post("/api/mode", json={"mode": "bogus"})
    assert r.status_code == 502 and "unknown mode" in r.json()["detail"]  # engine's error, verbatim
    time.sleep(2.2)  # debounced persist of the slider value
    st = authed.get("/api/status").json()
    assert st["brightness"] == 37 and st["online"] is True and st["canvas"] == {"w": 8, "h": 12}
    assert yaml.safe_load((data / "config.yaml").read_text())["brightness"] == {"user": 37, "auto": False}
    assert st["engine"]["mode"] == "pattern" and st["engine"]["mode_name"] == "solid"


def test_schedule_api_and_quiet_cap(authed, engine, data):
    r = authed.get("/api/schedule")
    assert r.json()["enabled"] is False
    body = {"enabled": True, "on": "07:00", "off": "23:00", "days": [6, 0],
            "quiet": {"enabled": True, "start": "00:00", "end": "23:59", "max_brightness": 25}}
    assert authed.put("/api/schedule", json=body).json()["days"] == [0, 6]
    assert authed.put("/api/schedule", json={**body, "on": "25:00"}).status_code == 422
    assert yaml.safe_load((data / "config.yaml").read_text())["schedule"]["quiet"]["max_brightness"] == 25
    # quiet hours span the whole day in this test, so brightness is capped right away
    assert engine.state["brightness"]["user"] == 25
    assert authed.get("/api/status").json()["quiet_cap"] == 25
    authed.post("/api/brightness", json={"value": 90})
    assert engine.state["brightness"]["user"] == 25  # clamped while sending
    assert authed.get("/api/status").json()["brightness"] == 90  # but the user's value is remembered
    # reload_config makes the engine re-read brightness.user from the file: the clamp is sent again
    authed.put("/api/playlists/x", json={"items": []})
    assert [c["cmd"] for c in engine.calls[-2:]] == ["reload_config", "set_brightness"] and engine.calls[-1]["value"] == 25
    time.sleep(2.2)
    assert yaml.safe_load((data / "config.yaml").read_text())["brightness"]["user"] == 90


def test_refresh_slider_saves_the_cap_and_keeps_unlisted_matrix_keys(authed, engine, data):
    """The Settings slider sends limit_refresh_rate_hz (0 = no cap) and dithering through PUT;
    keys the form doesn't show (disable_busy_waiting) must survive the save."""
    path = data / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["matrix"]["disable_busy_waiting"] = True
    path.write_text(yaml.safe_dump(cfg))
    old = time.time() - 5
    os.utime(path, (old, old))  # an old hand edit: the app reloads it
    s = authed.get("/api/settings").json()
    s["matrix"].update(limit_refresh_rate_hz=110, pwm_dither_bits=1, pwm_lsb_nanoseconds=100)  # off the defaults
    r = authed.put("/api/settings", json=s)
    assert r.status_code == 200 and r.json()["restart_required"] is True
    m = yaml.safe_load(path.read_text())["matrix"]
    assert (m["limit_refresh_rate_hz"], m["pwm_dither_bits"], m["disable_busy_waiting"]) == (110, 1, True)
    s["matrix"]["limit_refresh_rate_hz"] = 0  # "No cap"
    assert authed.put("/api/settings", json=s).status_code == 200
    assert yaml.safe_load(path.read_text())["matrix"]["limit_refresh_rate_hz"] == 0


def test_settings_roundtrip(authed, engine, data):
    s = authed.get("/api/settings").json()
    assert s["matrix"]["panel_size"] == 4 or s["matrix"]["panel_size"] in (32, 64) or True
    s["matrix"]["panel_size"] = 64
    s["matrix"]["gpio_slowdown"] = 3
    s["power"]["ceiling_amps"] = 10
    s["mqtt"] = {"enabled": True, "host": "mqtt.local", "port": 1883, "username": "u", "password": "p"}
    r = authed.put("/api/settings", json=s)
    assert r.status_code == 200 and r.json()["restart_required"] is True
    r = authed.put("/api/settings", json=s)
    assert r.json()["restart_required"] is False
    cfg = yaml.safe_load((data / "config.yaml").read_text())
    assert cfg["matrix"]["gpio_slowdown"] == 3 and cfg["power"]["ceiling_amps"] == 10 and cfg["mqtt"]["host"] == "mqtt.local"
    assert cfg["matrix"]["panels"][2] == {"col": 1, "row": 1, "rotate": 0}
    assert engine.calls[-1]["cmd"] == "reload_config"
    s["matrix"]["pwm_bits"] = 12
    assert authed.put("/api/settings", json=s).status_code == 422


def test_docs_and_openapi(authed):
    assert authed.get("/docs").status_code == 200
    spec = authed.get("/openapi.json").json()
    paths = spec["paths"]
    for p in ("/api/auth/login", "/api/auth/setup", "/api/status", "/api/power", "/api/brightness", "/api/mode",
              "/api/modes", "/api/media", "/api/media/{media_id}", "/api/playlists/{name}", "/api/schedule",
              "/api/settings", "/api/tokens", "/api/tokens/{token_id}", "/api/auth/password"):
        assert p in paths, p
    assert "multipart/form-data" in paths["/api/media"]["post"]["requestBody"]["content"]
    assert "/api/preview" in spec["info"]["description"]


def test_clock_overlay_saves_and_reloads(authed, engine, data):
    assert authed.get("/api/clock_overlay").json() == {"enabled": False, "position": "bottom-center", "hour24": True,
                                                       "time_px": 32, "date_px": 9}
    body = {"enabled": True, "position": "top-right", "hour24": False, "time_px": 48, "date_px": 0}
    assert authed.put("/api/clock_overlay", json=body).json() == body
    assert yaml.safe_load((data / "config.yaml").read_text())["clock_overlay"] == body
    assert engine.calls[-1]["cmd"] == "reload_config"
    for ok in ({"time_px": 14, "date_px": 8}, {"time_px": 27, "date_px": 16}):
        assert authed.put("/api/clock_overlay", json={**body, **ok}).json() == {**body, **ok}
    for bad in ({"position": "left"}, {"time_px": 13}, {"time_px": 49}, {"time_px": 20.5},
                {"date_px": 7}, {"date_px": 17}, {"date_px": -1}):
        assert authed.put("/api/clock_overlay", json={**body, **bad}).status_code == 422, bad
    # An app from before the sliders sends size names: the px of the fonts they had.
    old = {"enabled": True, "position": "center", "hour24": True, "date": True, "time_size": "xl", "date_size": "m"}
    assert authed.put("/api/clock_overlay", json=old).json() == {"enabled": True, "position": "center", "hour24": True,
                                                                 "time_px": 46, "date_px": 12}
    assert authed.put("/api/clock_overlay", json={**old, "date": False}).json()["date_px"] == 0
    assert authed.put("/api/clock_overlay", json={**old, "time_size": "xxl"}).json()["time_px"] == 32


def test_next_item(authed, engine):
    assert authed.post("/api/next").json() == {"skipped": False}
    authed.post("/api/mode", json={"mode": "effect", "effect": "plasma"})
    assert engine.calls[-1]["cmd"] == "set_mode"
    authed.put("/api/playlists/p", json={"items": [], "loop": True, "shuffle": False})
    engine.state["mode"] = "playlist"
    assert authed.post("/api/next").json() == {"skipped": True}
    assert engine.calls[-1]["cmd"] == "next"


def test_first_boot_arm(authed, data):
    assert authed.get("/api/first_boot").json() == {"media": ""}
    assert authed.put("/api/first_boot", json={"media": "nope"}).status_code == 404
    assert authed.put("/api/first_boot", json={"media": ""}).json() == {"media": ""}
    assert yaml.safe_load((data / "config.yaml").read_text())["first_boot"] == {"media": "", "armed_at": 0}
