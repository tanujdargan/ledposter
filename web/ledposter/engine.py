"""All engine I/O in one place (docs/ARCHITECTURE.md, docs/protocol.md).

Control: newline-delimited JSON over the Unix socket `engine.socket`.
Preview: one upstream connection to `engine.preview_socket` (opened only while a browser
is watching), each message a JSON status line then w*h*3 raw RGB bytes, fanned out to
subscribers through size-1 queues so slow clients drop frames instead of lagging.
Both reconnect with backoff; the app keeps working while the engine is offline.
"""
import asyncio
import json
import logging

log = logging.getLogger("ledposter.engine")


class EngineError(Exception):
    """The engine answered ok:false; the message is the engine's."""


def mode_args(args):
    """Shape a /api/mode request into set_mode's fields (docs/protocol.md). The only
    place that knows how the engine spells colours."""
    out = {k: v for k, v in args.items() if v is not None}
    if isinstance(out.get("color"), str):
        out["color"] = out["color"].lstrip("#").lower()
    return out


class EngineOffline(EngineError):
    pass


class Engine:
    def __init__(self, socket_path, preview_path, poll_s=2.0):
        self.socket_path, self.preview_path, self.poll_s = socket_path, preview_path, poll_s
        self.online = False
        self.status = None  # last `status` reply, kept while offline so the UI can show it greyed
        self.preview_status = None
        self._reader = self._writer = None
        self._lock = asyncio.Lock()
        self._id = 0
        self._subs = set()
        self._want_preview = asyncio.Event()
        self._tasks = []

    async def start(self):
        self._tasks = [asyncio.create_task(self._poll()), asyncio.create_task(self._preview_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._drop()

    # -- control -----------------------------------------------------------------

    async def _connect(self):
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.socket_path), timeout=2)

    async def _drop(self):
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    async def call(self, cmd, **args):
        """Send one command and return the reply dict. Raises EngineOffline / EngineError."""
        async with self._lock:
            try:
                if not self._writer:
                    await self._connect()
                self._id += 1
                req = {"id": self._id, "cmd": cmd, **args}
                self._writer.write(json.dumps(req).encode() + b"\n")
                await self._writer.drain()
                line = await asyncio.wait_for(self._reader.readline(), timeout=5)
                if not line:
                    raise ConnectionError("engine closed the connection")
                resp = json.loads(line)
            except (OSError, asyncio.TimeoutError, ValueError) as e:
                await self._drop()
                if self.online:
                    log.warning("engine offline: %s", e)
                self.online = False
                raise EngineOffline("engine offline") from e
            self.online = True
            if not resp.get("ok"):
                raise EngineError(resp.get("error") or "engine error")
            return resp

    async def show_text(self, text, seconds=10):
        """M5 `show_text`: a text overlay on the wall for `seconds`. Best effort: False when
        the engine is offline or does not know the command (pre-M5 engines)."""
        try:
            await self.call("show_text", text=text, seconds=seconds)
            return True
        except EngineError as e:
            log.info("show_text not shown: %s", e)
            return False

    @staticmethod
    def setup_request(status):
        """True while the engine reports a 10s button hold (`wifi_setup_requested`, docs/protocol.md
        Button); it stays set until `ack_wifi_setup`. `wifi_setup_count`/`_at` say which one."""
        return bool((status or {}).get("wifi_setup_requested"))

    async def ack_wifi_setup(self):
        """Clears `wifi_setup_requested`; False if the engine is offline (the flag then stays
        and the request is seen again on the next poll)."""
        try:
            await self.call("ack_wifi_setup")
            return True
        except EngineError as e:
            log.info("ack_wifi_setup failed: %s", e)
            return False

    async def _poll(self):
        delay = 0.5
        while True:
            try:
                st = await self.call("status")
                st.pop("id", None), st.pop("ok", None)
                self.status = st
                delay = self.poll_s
            except EngineOffline:
                delay = min(delay * 2, 10)
            except EngineError as e:
                log.warning("status: %s", e)
                delay = self.poll_s
            await asyncio.sleep(delay)

    # -- preview -----------------------------------------------------------------

    def subscribe(self):
        q = asyncio.Queue(maxsize=1)
        self._subs.add(q)
        self._want_preview.set()
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)
        if not self._subs:
            self._want_preview.clear()

    def _fanout(self, item):
        for q in self._subs:
            if q.full():
                q.get_nowait()
            q.put_nowait(item)

    async def _preview_loop(self):
        delay = 0.5
        while True:
            await self._want_preview.wait()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.preview_path), timeout=2)
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5)
                continue
            delay = 0.5
            try:
                while self._subs:
                    line = await reader.readline()
                    if not line:
                        break
                    st = json.loads(line)
                    frame = await reader.readexactly(int(st["w"]) * int(st["h"]) * 3)
                    self.preview_status = st
                    self._fanout((st, frame))
            except (OSError, asyncio.IncompleteReadError, ValueError, KeyError) as e:
                log.warning("preview stream ended: %s", e)
            except Exception:  # anything else must not end the preview task for good
                log.exception("preview stream failed")
            finally:
                writer.close()
                self.preview_status = None
                self._fanout((None, None))  # tells clients the stream dropped
