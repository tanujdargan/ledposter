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


class FakeEngine:
    """Control + preview servers in their own thread/loop. `calls` records every request."""

    def __init__(self, sockdir, w=8, h=12):
        self.control_path = os.path.join(sockdir, "engine.sock")
        self.preview_path = os.path.join(sockdir, "frames.sock")
        self.w, self.h = w, h
        self.calls = []
        self.state = {"mode": "pattern", "mode_name": "panel-id", "fps": 15.0, "amps": 1.2, "amps_unlimited": 1.2,
                      "limited": False, "ceiling_amps": 13.0,
                      "brightness": {"user": 100, "auto": None, "auto_enabled": True, "effective": 100, "output": 100},
                      "lux": None, "sensor": "missing", "power": True, "w": w, "h": h, "uptime_s": 1}
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
            if mode not in ("pattern", "media", "playlist"):
                return {"ok": False, "error": f"unknown mode {mode!r}"}
            self.state["mode"], self.state["mode_name"] = mode, str(req.get(mode))
            return {"ok": True, "mode": mode, "mode_name": self.state["mode_name"]}
        if cmd == "reload_config":
            return {"ok": True, "restart_needed": []}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

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
