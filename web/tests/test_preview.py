import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect


def test_preview_requires_auth(authed):
    with authed.websocket_connect("/api/preview", headers={"cookie": ""}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401


def test_preview_relays_frames(authed, engine):
    with authed.websocket_connect("/api/preview") as ws:
        frames = 0
        last = None
        for _ in range(20):
            msg = ws.receive()
            if "text" in msg:
                st = json.loads(msg["text"])
                if st.get("streaming"):
                    assert st["w"] == 8 and st["h"] == 12 and st["mode"] == "pattern" and st["mode_name"] == "panel-id" and "fps" in st
                    frame = ws.receive_bytes()
                    assert len(frame) == 8 * 12 * 3
                    assert frame[0] != last  # a new frame each time, never a repeat
                    last = frame[0]
                    frames += 1
        assert frames >= 10

    # fake engine's token param auth works for scripts too
    tok = authed.post("/api/tokens", json={"name": "script"}).json()["token"]
    with authed.websocket_connect(f"/api/preview?token={tok}", headers={"cookie": ""}) as ws:
        assert "online" in json.loads(ws.receive_text())


def test_engine_offline_and_reconnect(authed, engine):
    time.sleep(0.3)
    assert authed.get("/api/status").json()["online"] is True
    engine.stop()
    assert authed.post("/api/brightness", json={"value": 5}).status_code == 503
    st = authed.get("/api/status").json()
    assert st["online"] is False and st["engine"]["w"] == 8  # last known status kept for the UI
    assert authed.get("/api/media").status_code == 200  # library keeps working
    with authed.websocket_connect("/api/preview") as ws:
        assert json.loads(ws.receive_text())["online"] is False
    engine.__init__(engine.control_path.rsplit("/", 1)[0])  # same paths, fresh server
    engine.start()
    assert authed.post("/api/brightness", json={"value": 5}).status_code == 200  # reconnects on demand
    assert engine.state["brightness"]["user"] == 5
    with authed.websocket_connect("/api/preview") as ws:
        for _ in range(60):
            msg = ws.receive()
            if "text" in msg and json.loads(msg["text"]).get("streaming"):
                assert len(ws.receive_bytes()) == 8 * 12 * 3
                break
        else:
            raise AssertionError("preview did not resume")
