"""Media library: $LEDPOSTER_DATA/media/<id>/ holds the upload (source.<ext>), item.json
(name, status, settings) and the transcoder's cache files (frames.rgb, meta.json,
thumb.png). One background worker transcodes items one at a time, never in a request.

Speed is applied here, not in ffmpeg: after a transcode the frame durations in
meta.json are divided by `speed`, which the engine honours with no change on its side.
"""
import asyncio
import json
import logging
import os
import secrets
import shutil
import time

from . import transcode as tc

log = logging.getLogger("ledposter.media")

DEFAULT_SETTINGS = {"scale": "fit", "filter": None, "crop": None, "position": [0.5, 0.5],
                    "speed": 1.0, "dither": False}
VIDEO_EXT = {".mp4", ".webm", ".mov"}
ANIM_EXT = {".gif", ".apng", ".webp"}


class Library:
    def __init__(self, data_dir, canvas, gamma):
        """canvas() -> (w, h) and gamma() -> float read the live config."""
        self.dir = os.path.join(data_dir, "media")
        self.canvas, self.gamma = canvas, gamma
        self.items = {}
        self._queue = asyncio.Queue()
        self._task = None

    # -- persistence --------------------------------------------------------------

    def _path(self, mid, *parts):
        return os.path.join(self.dir, mid, *parts)

    def _write(self, item):
        tc.write_json_atomic(self._path(item["id"], "item.json"), item)

    async def start(self):
        os.makedirs(self.dir, exist_ok=True)
        for mid in sorted(os.listdir(self.dir)):
            try:
                with open(self._path(mid, "item.json")) as f:
                    item = json.load(f)
            except (OSError, ValueError):
                continue  # a bare transcode cache (made by hand) is not a library item
            item["settings"] = {**DEFAULT_SETTINGS, **item.get("settings", {})}
            self.items[mid] = item
            if item["status"] in ("queued", "transcoding"):  # interrupted by a restart
                item["status"] = "queued"
                self._queue.put_nowait(mid)
        self._task = asyncio.create_task(self._worker())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # -- items --------------------------------------------------------------------

    def get(self, mid):
        return self.items.get(mid)

    def list(self):
        return sorted(self.items.values(), key=lambda i: i["created"], reverse=True)

    def new(self, filename):
        """Reserve an id and directory for an upload; returns (item, source path)."""
        ext = os.path.splitext(filename)[1].lower()
        mid = secrets.token_hex(6)
        os.makedirs(self._path(mid))
        item = {"id": mid, "name": os.path.basename(filename) or mid, "ext": ext, "size": 0,
                "created": time.time(), "status": "uploading", "error": None,
                "settings": dict(DEFAULT_SETTINGS), "info": None}
        self.items[mid] = item
        return item, self._path(mid, "source" + ext)

    def discard(self, mid):
        """Remove an item whose upload did not complete."""
        self.items.pop(mid, None)
        shutil.rmtree(self._path(mid), ignore_errors=True)

    def enqueue(self, mid, settings=None):
        item = self.items[mid]
        if settings:
            item["settings"] = {**item["settings"], **settings}
        item["size"] = os.path.getsize(self._path(mid, "source" + item["ext"]))
        item["status"], item["error"] = "queued", None
        self._write(item)
        self._queue.put_nowait(mid)
        return item

    def rename(self, mid, name):
        self.items[mid]["name"] = name
        self._write(self.items[mid])

    def delete(self, mid):
        self.items.pop(mid)
        shutil.rmtree(self._path(mid), ignore_errors=True)

    def source_path(self, mid):
        return self._path(mid, "source" + self.items[mid]["ext"])

    def thumb_path(self, mid):
        return self._path(mid, "thumb.png")

    def frames(self, mid, count):
        """Up to `count` evenly spaced cached frames as one bytes object, plus their indices."""
        item = self.items[mid]
        if item["status"] != "ready":
            return b"", []
        n, fb = item["info"]["frames"], item["info"]["w"] * item["info"]["h"] * 3
        idx = sorted({round(i * (n - 1) / max(count - 1, 1)) for i in range(min(count, n))})
        out = bytearray()
        with open(self._path(mid, "frames.rgb"), "rb") as f:
            for i in idx:
                f.seek(i * fb)
                out += f.read(fb)
        return bytes(out), idx

    @staticmethod
    def kind(item):
        if item["info"] and item["info"].get("filter") == "pixel":
            return "pixel"
        if item["ext"] in VIDEO_EXT:
            return "video"
        if item["ext"] in ANIM_EXT and (not item["info"] or item["info"]["frames"] > 1):
            return "gif"
        return "image"

    # -- worker -------------------------------------------------------------------

    async def _worker(self):
        while True:
            mid = await self._queue.get()
            item = self.items.get(mid)
            if not item or item["status"] != "queued":
                continue
            item["status"] = "transcoding"
            self._write(item)
            try:
                info = await asyncio.to_thread(self._transcode, item)
                item.update(status="ready", error=None, info=info)
            except tc.TranscodeError as e:  # verbatim, except ffmpeg's path to our copy becomes the file's name
                item.update(status="failed", error=str(e).replace(self.source_path(mid), item["name"]))
            except Exception as e:  # a bug, not a bad file: still surface it in the UI
                log.exception("transcode %s", mid)
                item.update(status="failed", error=f"Internal error: {e}")
            if mid in self.items:  # deleted while transcoding?
                self._write(item)

    def _transcode(self, item):
        s = item["settings"]
        w, h = self.canvas()
        out = self._path(item["id"])
        meta = tc.transcode(self.source_path(item["id"]), out, w=w, h=h, scale=s["scale"],
                            filter=s["filter"], crop=s["crop"], position=tuple(s["position"]),
                            gamma=self.gamma(), dither=bool(s["dither"]))
        speed = float(s.get("speed") or 1.0)
        if speed != 1.0 and len(meta["durations_ms"]) > 1:
            meta["durations_ms"] = [max(1, round(d / speed)) for d in meta["durations_ms"]]
        meta["speed"] = speed
        tc.write_json_atomic(os.path.join(out, "meta.json"), meta)
        return {"w": meta["w"], "h": meta["h"], "frames": len(meta["durations_ms"]),
                "duration_ms": sum(meta["durations_ms"]), "filter": meta["filter"],
                "source_w": meta["source"]["w"], "source_h": meta["source"]["h"],
                "format": meta["source"]["format"]}
