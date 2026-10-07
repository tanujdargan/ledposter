"""Fake engine speaking docs/protocol.md on Unix sockets, for tests and local dev.

    python tests/fake_engine.py SOCKDIR [W H]   # serves SOCKDIR/engine.sock and SOCKDIR/frames.sock

The preview stream is a moving colour pattern at 15fps so the UI has something to show.
"""
import asyncio
import json
import math
import os
import sys
import threading


SPEED = {"name": "speed", "type": "number", "min": 0.1, "max": 5, "default": 1.0, "help": "animation speed, 1 = normal"}
# A trimmed copy of the real engine's list_modes answer (docs/protocol.md).
LIST_MODES = {
    "modes": [
        {"mode": "pattern", "label": "Test pattern", "help": "panel test patterns", "settings": [
            {"name": "pattern", "type": "choice", "choices": ["panel-id", "solid", "cycle", "rows", "cols"],
             "default": "panel-id"},
            {"name": "color", "type": "color", "default": "ffffff", "help": "for solid, rows and cols"}]},
        {"mode": "media", "label": "Media", "help": "", "settings": [
            {"name": "media", "type": "string", "max": 200, "default": ""}]},
        {"mode": "playlist", "label": "Playlist", "help": "", "settings": [
            {"name": "playlist", "type": "choice", "choices": [], "default": ""}]},
        {"mode": "effect", "effect": "plasma", "label": "Plasma", "help": "flowing sine plasma", "settings": [
            SPEED, {"name": "colors", "type": "colors", "min": 1, "max": 6, "default": ["ff0060", "4000ff"]},
            {"name": "scale", "type": "number", "min": 0.25, "max": 4, "default": 1.0}]},
        {"mode": "effect", "effect": "fire", "label": "Fire", "help": "heat-diffusion fire", "settings": [
            SPEED, {"name": "colors", "type": "colors", "min": 1, "max": 6, "default": ["b00000", "ffb000"]},
            {"name": "height", "type": "number", "min": 0.2, "max": 1, "default": 0.55}]},
        {"mode": "clock", "label": "Clock", "help": "local time", "settings": [
            {"name": "hour24", "type": "bool", "default": True},
            {"name": "color", "type": "color", "default": "ffffff"}]},
        {"mode": "text", "label": "Text scroller", "help": "", "settings": [
            {"name": "text", "type": "string", "max": 500, "default": "Hello!"}, SPEED,
            {"name": "font", "type": "choice", "choices": ["6x10", "10x20"], "default": "10x20"},
            {"name": "scale", "type": "int", "min": 1, "max": 4, "default": 1},
            {"name": "future", "type": "gradient", "default": None}]},  # unknown types are skipped
        {"mode": "idle", "label": "Idle screen", "help": "", "settings": []},
    ],
    "events": ["kill", "killstreak", "death", "win"],
    "fonts": ["6x10", "10x20"],
}
OVERLAY_MS = {"kill": 1500, "killstreak": 2500, "death": 2000, "win": 3000}


class FakeEngine:
    """Control + preview servers in their own thread/loop. `calls` records every request.
    list_modes=False makes it answer like a pre-M5 engine (unknown command)."""

    def __init__(self, sockdir, w=8, h=12):
        self.control_path = os.path.join(sockdir, "engine.sock")
        self.preview_path = os.path.join(sockdir, "frames.sock")
        self.w, self.h = w, h
        self.calls = []
        self.list_modes = True
        self.texts = []  # show_text calls (M5 command used by the networking code)
        self.state = {"mode": "pattern", "mode_name": "panel-id", "fps": 15.0, "amps": 1.2, "amps_unlimited": 1.2,
                      "limited": False, "ceiling_amps": 13.0,
                      "brightness": {"user": 100, "auto": None, "auto_enabled": True, "effective": 100, "output": 100},
                      "lux": None, "sensor": "missing", "power": True, "w": w, "h": h, "uptime_s": 1,
                      "wifi_setup_requested": False, "wifi_setup_count": 0, "wifi_setup_at": None,
                      "button_double_count": 0,
                      "stream": {"active": False, "source": None, "fps": 0, "last_packet_ms_ago": None,
                                 "ddp": False, "e131": False}}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = threading.Event()
        self.servers = []

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.serve())

    async def serve(self):
        self.servers = [await asyncio.start_unix_server(self._control, self.control_path),
                        await asyncio.start_unix_server(self._preview, self.preview_path)]
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            for s in self.servers:
                s.close()
                await s.wait_closed()

    def handle(self, req):
        cmd = req.get("cmd")
        self.calls.append(req)
        if cmd == "status":
            return {"ok": True, **self.state}
        if cmd == "set_brightness":
            v = req.get("value")
            if not isinstance(v, int) or not 0 <= v <= 100:
                return {"ok": False, "error": "value must be 0-100"}
            self.state["brightness"]["user"] = v
            self.state["brightness"]["effective"] = self.state["brightness"]["output"] = v
            return {"ok": True}
        if cmd == "set_auto_brightness":
            self.state["brightness"]["auto_enabled"] = bool(req.get("enabled"))
            return {"ok": True}
        if cmd == "power":
            self.state["power"] = bool(req.get("on"))
            return {"ok": True}
        if cmd == "set_mode":
            mode = req.get("mode")
            if mode not in ("pattern", "media", "playlist", "effect", "clock", "text", "idle"):
                return {"ok": False, "error": f"unknown mode {mode!r}"}
            name = str(req.get(mode)) if mode in ("pattern", "media", "playlist", "effect") else mode
            if mode == "effect" and name not in ("plasma", "fire"):
                return {"ok": False, "error": f"unknown effect '{name}' (plasma, fire)"}
            self.state["mode"], self.state["mode_name"] = mode, name
            return {"ok": True, "mode": mode, "mode_name": name, "settings": {}}
        if cmd == "list_modes" and self.list_modes:
            return {"ok": True, **LIST_MODES}
        if cmd == "overlay":
            if req.get("event") not in OVERLAY_MS:
                return {"ok": False, "error": f"unknown event '{req.get('event')}'"}
            return {"ok": True, "ms": OVERLAY_MS[req["event"]]}
        if cmd == "reload_config":
            return {"ok": True, "restart_needed": []}
        if cmd == "show_text":  # M5 overlay; recorded for the networking tests
            if not isinstance(req.get("text"), str) or not isinstance(req.get("seconds"), (int, float)):
                return {"ok": False, "error": "show_text needs text and seconds"}
            self.texts.append((req["text"], req["seconds"]))
            return {"ok": True}
        if cmd == "ack_wifi_setup":
            self.state["wifi_setup_requested"] = False
            return {"ok": True}
        if cmd == "set_stream":
            for k in ("ddp", "e131"):
                if k in req:
                    self.state["stream"][k] = bool(req[k])
            return {"ok": True, "ddp": self.state["stream"]["ddp"], "e131": self.state["stream"]["e131"]}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def hold_button(self):
        """What a 10s press does to the status (docs/protocol.md, Button)."""
        self.state["wifi_setup_requested"] = True
        self.state["wifi_setup_count"] += 1
        self.state["wifi_setup_at"] = 1_700_000_000 + self.state["wifi_setup_count"]

    async def _control(self, reader, writer):
        try:
            while line := await reader.readline():
                req = json.loads(line)
                resp = {"id": req.get("id"), **self.handle(req)}
                writer.write(json.dumps(resp).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    def frame(self, i):
        """Moving diagonal colour bands, dimmed by the effective brightness and power."""
        w, h, out = self.w, self.h, bytearray(self.w * self.h * 3)
        k = self.state["brightness"]["effective"] / 100 if self.state["power"] else 0
        for y in range(h):
            for x in range(w):
                t = (x + y + i * 2) / 24
                r, g, b = (math.sin(t) + 1) / 2, (math.sin(t + 2.1) + 1) / 2, (math.sin(t + 4.2) + 1) / 2
                o = (y * w + x) * 3
                out[o], out[o + 1], out[o + 2] = int(r ** 2.2 * 255 * k), int(g ** 2.2 * 255 * k), int(b ** 2.2 * 255 * k)
        amps = 0.3 + 2.5 * k
        self.state["amps"] = round(amps, 2)
        self.state["limited"] = amps > self.state["ceiling_amps"]
        return bytes(out)

    async def _preview(self, reader, writer):
        i = 0
        try:
            while True:
                frame = self.frame(i) if self.w > 16 else bytes([i % 256, 0, 255 - i % 256]) * (self.w * self.h)
                st = dict(self.state)
                writer.write(json.dumps(st).encode() + b"\n" + frame)
                await writer.drain()
                i += 1
                await asyncio.sleep(1 / 15)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    def start(self):
        self.thread.start()
        assert self.started.wait(5)
        return self

    def stop(self):
        for t in asyncio.all_tasks(self.loop):
            self.loop.call_soon_threadsafe(t.cancel)
        self.thread.join(5)
        self.loop.close()
        for p in (self.control_path, self.preview_path):
            if os.path.exists(p):
                os.unlink(p)


if __name__ == "__main__":
    d = sys.argv[1]
    os.makedirs(d, exist_ok=True)
    for name in ("engine.sock", "frames.sock"):
        if os.path.exists(os.path.join(d, name)):
            os.unlink(os.path.join(d, name))
    w, h = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (128, 192)
    e = FakeEngine(d, w, h)
    print(f"fake engine: {e.control_path} {e.preview_path} ({w}x{h})", flush=True)
    asyncio.run(e.serve())
