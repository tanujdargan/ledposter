"""Milestone 5 web parts: the mode picker built from the engine's list_modes, POST /api/event
(game hook) with token auth and rate limiting, and tools/game_hook.py."""
import importlib.util
import io
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PASSWORD
from ledposter.app import EVENT_LIMIT, create_app

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "tools" / "game_hook.py"


def by_id(modes):
    return {m["id"]: m for m in modes}


def settings(mode):
    return {s["key"]: s for s in mode["settings"]}


# ---- /api/modes ----

def test_modes_come_from_list_modes(authed, engine):
    modes = authed.get("/api/modes").json()
    assert any(c["cmd"] == "list_modes" for c in engine.calls)
    ids = [m["id"] for m in modes]
    assert ids == ["media", "playlist", "effect:plasma", "effect:fire", "clock", "text", "idle",
                   "pattern:panel-id", "pattern:solid", "pattern:cycle", "pattern:rows", "pattern:cols"]
    m = by_id(modes)
    assert m["effect:plasma"]["group"] == "Effects" and m["effect:plasma"]["apply"] == {"mode": "effect", "effect": "plasma"}
    assert m["effect:plasma"]["name"] == "Plasma" and m["effect:plasma"]["help"] == "flowing sine plasma"
    p = settings(m["effect:plasma"])
    assert p["speed"] == {"key": "speed", "label": "Speed", "type": "slider", "options": None, "default": 1.0,
                          "min": 0.1, "max": 5, "step": 0.1, "help": "animation speed, 1 = normal"}
    assert p["colors"]["type"] == "colors" and p["colors"]["default"] == ["#ff0060", "#4000ff"]
    assert (p["colors"]["min"], p["colors"]["max"]) == (1, 6)
    assert settings(m["effect:fire"])["height"]["step"] == 0.05  # short range: finer steps
    c = settings(m["clock"])
    assert c["hour24"]["type"] == "toggle" and c["hour24"]["label"] == "24-hour clock"
    assert c["color"] == {"key": "color", "label": "Colour", "type": "color", "options": None, "default": "#ffffff",
                          "min": None, "max": None, "step": None, "help": None}
    t = settings(m["text"])
    assert t["text"]["type"] == "text" and t["text"]["max"] == 500 and t["text"]["default"] == "Hello!"
    assert t["font"]["type"] == "select" and t["font"]["options"] == ["6x10", "10x20"]
    assert t["scale"]["step"] == 1
    assert "future" not in t  # a setting type the app doesn't know is left at the engine default
    assert m["idle"]["group"] == "Clock and text" and m["idle"]["settings"] == []
    assert [s["key"] for s in m["pattern:solid"]["settings"]] == ["color"]
    assert m["pattern:rows"]["settings"] and not m["pattern:panel-id"]["settings"]
    assert m["pattern:solid"]["name"] == "Solid colour"


def test_modes_fall_back_without_list_modes(authed, engine):
    engine.list_modes = False  # an engine from before M5
    ids = [m["id"] for m in authed.get("/api/modes").json()]
    assert ids == ["media", "playlist", "pattern:panel-id", "pattern:solid", "pattern:cycle", "pattern:rows",
                   "pattern:cols"]
    engine.list_modes = True
    assert "effect:fire" in by_id(authed.get("/api/modes").json())
    engine.list_modes = False  # the last good answer is kept (e.g. engine restarting)
    assert "effect:fire" in by_id(authed.get("/api/modes").json())


def test_set_effect_and_text_modes(authed, engine):
    r = authed.post("/api/mode", json={"mode": "effect", "effect": "plasma", "speed": 2.5,
                                       "colors": ["#ff0000", "#00ff00"], "scale": 1.5})
    assert r.status_code == 200
    call = engine.calls[-1]
    assert call["cmd"] == "set_mode" and call["effect"] == "plasma" and call["colors"] == ["#ff0000", "#00ff00"]
    assert call["speed"] == 2.5 and engine.state["mode_name"] == "plasma"
    assert authed.post("/api/mode", json={"mode": "text", "text": "hi", "color": "#00FF00"}).status_code == 200
    assert engine.calls[-1]["color"] == "00ff00" and engine.calls[-1]["text"] == "hi"
    assert authed.post("/api/mode", json={"mode": "clock", "hour24": False}).status_code == 200
    assert engine.state["mode"] == "clock"
    r = authed.post("/api/mode", json={"mode": "effect", "effect": "lava"})
    assert r.status_code == 502 and "unknown effect 'lava'" in r.json()["detail"]  # the engine's words


# ---- POST /api/event ----

def token(authed, name="game-hook"):
    return {"Authorization": f"Bearer {authed.post('/api/tokens', json={'name': name}).json()['token']}"}


def overlay_calls(engine):
    return [c for c in engine.calls if c["cmd"] == "overlay"]


def test_event_with_token(authed, engine):
    bearer = token(authed)
    cookie = authed.cookies["session"]
    authed.cookies.clear()  # token only from here
    assert authed.post("/api/event", json={"event": "kill"}).status_code == 401
    assert authed.post("/api/event", json={"event": "kill"}, headers={"Authorization": "Bearer lp_nope"}).status_code == 401
    r = authed.post("/api/event", json={"event": "kill"}, headers=bearer)
    assert r.status_code == 200 and r.json() == {"ok": True, "event": "kill", "ms": 1500}
    assert overlay_calls(engine)[-1] == {"id": overlay_calls(engine)[-1]["id"], "cmd": "overlay", "event": "kill"}
    for ev, ms in [("killstreak", 2500), ("death", 2000), ("win", 3000)]:
        assert authed.post("/api/event", json={"event": ev}, headers=bearer).json()["ms"] == ms

    n = len(overlay_calls(engine))
    r = authed.post("/api/event", json={"event": "headshot"}, headers=bearer)
    assert r.status_code == 400 and "Unknown event 'headshot'; use one of kill, killstreak, death, win" in r.json()["detail"]
    assert authed.post("/api/event", json={}, headers=bearer).status_code == 422
    assert len(overlay_calls(engine)) == n  # nothing reached the engine
    authed.cookies.set("session", cookie)
    tid = authed.get("/api/tokens").json()[0]["id"]
    assert authed.delete(f"/api/tokens/{tid}").status_code == 200
    authed.cookies.clear()
    assert authed.post("/api/event", json={"event": "kill"}, headers=bearer).status_code == 401  # revoked
    authed.cookies.set("session", cookie)


def test_event_rate_limit(authed, engine):
    a, b = token(authed, "pc"), token(authed, "other")
    cookie = authed.cookies["session"]
    authed.cookies.clear()
    codes = [authed.post("/api/event", json={"event": "kill"}, headers=a).status_code for _ in range(EVENT_LIMIT + 2)]
    assert codes == [200] * EVENT_LIMIT + [429, 429]
    r = authed.post("/api/event", json={"event": "kill"}, headers=a)
    assert r.headers["retry-after"] == "5" and "slow down" in r.json()["detail"]
    assert authed.post("/api/event", json={"event": "win"}, headers=b).status_code == 200  # per caller
    assert len(overlay_calls(engine)) == EVENT_LIMIT + 1  # refused events never reach the engine
    authed.cookies.set("session", cookie)
    assert authed.post("/api/event", json={"event": "death"}).status_code == 200  # the app's own session too


def test_event_in_openapi(authed):
    spec = authed.get("/openapi.json").json()
    assert "/api/event" in spec["paths"] and "post" in spec["paths"]["/api/event"]


# ---- tools/game_hook.py ----

def load_hook():
    spec = importlib.util.spec_from_file_location("game_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Wall(BaseHTTPRequestHandler):
    """Stands in for the web app's /api/event."""
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Wall.seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"), body))
        if self.headers.get("Authorization") != "Bearer lp_good":
            code, out = 401, {"detail": "Not authenticated"}
        elif body.get("event") not in ("kill", "killstreak", "death", "win"):
            code, out = 400, {"detail": f"Unknown event '{body.get('event')}'; use one of kill, killstreak, death, win"}
        else:
            code, out = 200, {"ok": True, "event": body["event"], "ms": 1500}
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def wall():
    Wall.seen = []
    srv = HTTPServer(("127.0.0.1", 0), Wall)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_game_hook_send(wall, monkeypatch):
    hook = load_hook()
    assert hook.send("kill", wall + "/", "lp_good") == {"ok": True, "event": "kill", "ms": 1500}
    assert Wall.seen[-1] == ("/api/event", "Bearer lp_good", "application/json", {"event": "kill"})
    with pytest.raises(hook.HookError, match="400: Unknown event 'nope'"):
        hook.send("nope", wall, "lp_good")
    with pytest.raises(hook.HookError, match="401: Not authenticated .check the token"):
        hook.send("kill", wall, "lp_bad")
    monkeypatch.delenv("LEDPOSTER_TOKEN", raising=False)
    with pytest.raises(hook.HookError, match="no API token"):
        hook.send("kill", wall, None)
    monkeypatch.setenv("LEDPOSTER_TOKEN", "lp_good")
    monkeypatch.setenv("LEDPOSTER_URL", wall)
    assert hook.send("win")["ok"] is True  # both from the environment
    with pytest.raises(hook.HookError, match="can't reach http://127.0.0.1:9"):
        hook.send("kill", "http://127.0.0.1:9", "lp_good", timeout=1)


def test_game_hook_cli(wall, monkeypatch):
    r = subprocess.run([sys.executable, str(HOOK), "--url", wall, "--token", "lp_good", "death"],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout == "death: ok, 1.5s on the wall\n"
    r = subprocess.run([sys.executable, str(HOOK), "--url", wall, "--token", "lp_good", "dance"],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "invalid choice" in r.stderr  # argparse catches typos
    hook = load_hook()
    monkeypatch.setattr(sys, "stdin", io.StringIO("kill\n\nnope\nwin\n"))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert hook.main(["--stdin", "--url", wall, "--token", "lp_good"]) == 1  # one failed
    assert out.getvalue() == "kill: ok, 1.5s on the wall\nwin: ok, 1.5s on the wall\n"
    assert "nope: 400: Unknown event 'nope'" in err.getvalue()
    assert [s[3]["event"] for s in Wall.seen[-3:]] == ["kill", "nope", "win"]


# ---- end to end with the real engine (make sim) ----

def test_real_engine_picker_and_event():
    from test_engine_ipc import ENGINE, Engine
    if not ENGINE.exists():
        pytest.skip("engine sim not built (cd engine && make sim)")
    e = Engine("--pattern", "solid", "--color", "000000")
    try:
        with TestClient(create_app(str(e.dir))) as c:
            assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
            m = by_id(c.get("/api/modes").json())
            for x in ("spider-sense", "web-shot", "uav-radar", "plasma", "fire", "starfield"):
                sp = settings(m[f"effect:{x}"])["speed"]
                assert (sp["type"], sp["min"], sp["max"], sp["default"]) == ("slider", 0.1, 5, 1.0)
                assert settings(m[f"effect:{x}"])["colors"]["type"] == "colors"
            assert {"clock", "text", "idle", "media", "playlist", "pattern:solid"} <= set(m)
            assert settings(m["text"])["font"]["options"][-1] == "10x20"
            # Every default the picker would send is accepted by the engine.
            for mid in [k for k in m if k.startswith("effect:")] + ["clock", "text", "idle"]:
                body = {**m[mid]["apply"], **{s["key"]: s["default"] for s in m[mid]["settings"]}}
                assert c.post("/api/mode", json=body).status_code == 200, (mid, c.post("/api/mode", json=body).json())
            body = {"mode": "effect", "effect": "fire", "speed": 3, "colors": ["#0000ff", "#ffffff"], "height": 0.9}
            assert c.post("/api/mode", json=body).status_code == 200
            st = e.ctl.wait(lambda s: s["mode_name"] == "fire")
            assert st["mode_settings"] == {"speed": 3.0, "colors": ["0000ff", "ffffff"], "height": 0.9}
            r = c.post("/api/mode", json={"mode": "effect", "effect": "fire", "speed": 9})
            assert r.status_code == 502 and "'speed' must be a number from 0.1 to 5" in r.json()["detail"]
            tok = c.post("/api/tokens", json={"name": "pc"}).json()["token"]
            c.cookies.clear()
            r = c.post("/api/event", json={"event": "kill"}, headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200 and r.json()["ms"] == 1500
            e.ctl.wait(lambda s: s["overlays"] == ["kill"])
            e.ctl.wait(lambda s: s["overlays"] == [], timeout=3)
    finally:
        e.stop()
