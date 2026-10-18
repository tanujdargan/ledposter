"""Personal media: prepares your own images, GIFs and clips for the wall at native 128x192.

Reads art/personal/sources.json (git-ignored, like everything under art/personal/), downloads
each source once into art/personal/cache/, and writes OUT/personal/<slug>.mp4|gif plus manifest
entries that tools/artpack.py uploads (filter=pixel, so the wall shows exactly these pixels)
into the playlist named in the sources file. Nothing here is committed except this code.

sources.json:
  {"playlist": "My Playlist",
   "items": [{"slug": "...", "title": "...", "url": "https://...",
              "mode": "clip" | "still" | "pixel",
              "frame": [cx, cy, h],            # 2:3 crop: centre (fractions) and height (fraction)
              "stack": [[x, y, w, h], ...],    # clip: instead of frame, panels (fractions) stacked top to bottom
              "trim": [start_s, end_s],        # clip: cut to a clean loop
              "key": ["#rrggbb", ...], "key_tol": 40,   # flat background colours -> black (LEDs off)
              "key_to": ["#rrggbb", ...],      # optional: replace each key colour with this instead
              "grade": {"black": 0.06, "gamma": 1.0, "contrast": 1.1, "saturation": 1.2,
                        "sharpen": 0.6, "gain": 1.0},
              "alive": {"seconds": 8, "zoom": 0.06, "focus": [x, y], "glow": 1.0,
                        "ripple": false, "web": [x0, y0, x1, y1]},  # still: loop effects (output px)
              "grid": 8, "scale": 2,           # pixel: source pixels per art pixel, LEDs per art pixel
              "dither": false,                 # transcoder dither on upload
              "playlist": "..."}]}             # optional: this item's playlist instead of the top one
"""
import json
import os
import subprocess
import urllib.request

import numpy as np

from lib import TAU, W, H, XX, YY, encode, smoothstep, to_u8, wall_stats

HERE = os.path.dirname(os.path.abspath(__file__))
PERSONAL = os.path.join(HERE, "personal")
UA = "Mozilla/5.0 (compatible; ledposter-artpack/1.0; personal LED wall)"
SS = 3  # supersampling for stills: work at 3x and box-average down (area filter)


def fetch(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return False
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(dest + ".part", "wb") as fh:
        fh.write(data)
    os.replace(dest + ".part", dest)
    return True


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,avg_frame_rate,r_frame_rate", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    num, den = (int(v) for v in (s["avg_frame_rate"] if s["avg_frame_rate"] != "0/0" else s["r_frame_rate"]).split("/"))
    return s["width"], s["height"], (num / den if den else 0)


def crop_px(sw, sh, frame):
    cx, cy, fh = frame
    h = min(sh * fh, sw * 1.5)
    w = h / 1.5
    x = min(max(cx * sw - w / 2, 0), sw - w)
    y = min(max(cy * sh - h / 2, 0), sh - h)
    return int(round(x)), int(round(y)), int(w), int(h)


def decode(path, vf, w, h, extra=()):
    out = subprocess.run(["ffmpeg", "-v", "error", *extra, "-i", path, "-vf", vf + ",format=rgb24",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(-1, h, w, 3).astype(np.float32)


def hexc(c):
    return np.array([int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def key_out(img, colours, tol, to=None):
    """Replace flat background colours with black (or the matching `to` colour), soft-edged."""
    for k, c in enumerate(colours):
        d = np.sqrt(((img - hexc(c)) ** 2).sum(-1))
        a = smoothstep(tol * 0.6, tol, d)[..., None]
        img = img * a + (1 - a) * (hexc(to[k]) if to else 0)
    return img


def box_down(img, k):
    h, w = img.shape[-3] // k, img.shape[-2] // k
    return img[..., :h * k, :w * k, :].reshape(*img.shape[:-3], h, k, w, k, 3).mean(axis=(-4, -2))


def blur3(img):
    p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
    k = (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:] + 2 * p[1:-1, :-2] + 4 * p[1:-1, 1:-1] + 2 * p[1:-1, 2:]
         + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:])
    return k / 16


def grade(img, g):
    """Crush near-blacks to true black (LEDs off), lift contrast and saturation, mild unsharp mask."""
    v = img / 255
    b = g.get("black", 0.05)
    v = np.clip((v - b) / (1 - b), 0, 1)
    v = v ** g.get("gamma", 1.0)  # > 1 pushes bright backgrounds down without touching blacks
    c = g.get("contrast", 1.0)
    v = np.clip((v - 0.5) * c + 0.5, 0, 1) * (v > 0)
    lum = (v * np.array([0.299, 0.587, 0.114], np.float32)).sum(-1, keepdims=True)
    v = np.clip(lum + (v - lum) * g.get("saturation", 1.0), 0, 1)
    v = v * g.get("gain", 1.0)
    out = v * 255
    s = g.get("sharpen", 0.0)
    if s:
        out = out + s * (out - blur3(out))
    return np.clip(out, 0, 255)


def render_clip(src, it):
    sw, sh, fps = probe(src)
    fps = min(60, round(fps) or 10)
    trim = []
    if it.get("trim"):
        trim = ["-ss", str(it["trim"][0]), "-to", str(it["trim"][1])]
    pre = f"fps={fps}"
    if it.get("stack"):
        panels = []
        ph = H // len(it["stack"])
        for x, y, w, h in it["stack"]:
            cx, cy, cw, ch = int(x * sw), int(y * sh), int(w * sw), int(h * sh)
            panels.append(decode(src, f"{pre},crop={cw}:{ch}:{cx}:{cy},scale={W * 2}:{ph * 2}:flags=area",
                                 W * 2, ph * 2, trim))
        n = min(len(p) for p in panels)
        frames = np.concatenate([p[:n] for p in panels], axis=1)
        for k in range(1, len(panels)):  # thin black gutter between panels, like a comic strip
            frames[:, k * ph * 2 - 2:k * ph * 2 + 2] = 0
    else:
        x, y, w, h = crop_px(sw, sh, it.get("frame", [0.5, 0.5, 1.0]))
        frames = decode(src, f"{pre},crop={w}:{h}:{x}:{y},scale={W * 2}:{H * 2}:flags=area", W * 2, H * 2, trim)
    out = []
    for f in frames:
        if it.get("key"):
            f = key_out(f, it["key"], it.get("key_tol", 40), it.get("key_to"))
        out.append(grade(box_down(f, 2), it.get("grade", {})))
    return out, fps


def render_pixel(src, it):
    """Pixel art: sample each art pixel's centre (nearest), key the background, then place it on the
    canvas at a whole-number scale (`scale`, default 1) so every art pixel stays a crisp block."""
    sw, sh, fps = probe(src)
    fps = min(60, round(fps) or 10)
    g, k = it["grid"], it.get("scale", 1)
    nw, nh = sw // g, sh // g
    frames = decode(src, f"fps={fps},scale={nw}:{nh}:flags=neighbor", nw, nh)
    out = []
    bw, bh = nw * k, nh * k
    ox, oy = (W - bw) // 2, (H - bh) // 2 + it.get("yoff", 0)
    for f in frames:
        if it.get("key"):
            d = np.stack([np.sqrt(((f - hexc(c)) ** 2).sum(-1)) for c in it["key"]])
            hit = d.min(0) < it.get("key_tol", 40)
            to = np.stack([hexc(c) for c in it.get("key_to", ["#000000"] * len(it["key"]))])
            f = np.where(hit[..., None], to[d.argmin(0)], f)
        f = np.repeat(np.repeat(f, k, 0), k, 1)
        c = np.zeros((H, W, 3), np.float32)
        y0, y1 = max(oy, 0), min(oy + bh, H)
        x0, x1 = max(ox, 0), min(ox + bw, W)
        c[y0:y1, x0:x1] = f[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
        out.append(grade(c, it.get("grade", {})))
    return out, fps


def render_still(src, it):
    """A short seamless loop from a still: slow Ken Burns breathing on the subject, a pulsing red/blue
    rim light, a spider-sense ripple from the focus point and an optional web-shot streak."""
    sw, sh, _ = probe(src)
    al = it.get("alive", {})
    zoom = al.get("zoom", 0.06)
    x, y, w, h = crop_px(sw, sh, it.get("frame", [0.5, 0.5, 1.0]))
    hw, hh = W * SS, H * SS
    hi = decode(src, f"crop={w}:{h}:{x}:{y},scale={hw}:{hh}:flags=area", hw, hh)[0]
    if it.get("key"):
        hi = key_out(hi, it["key"], it.get("key_tol", 40), it.get("key_to"))
    fx, fy = al.get("focus", [W / 2, H / 2])
    seconds, fps = al.get("seconds", 8), 60
    n = seconds * fps
    # supersampled output grid, in output pixel units
    sy, sx = np.mgrid[0:hh, 0:hw].astype(np.float32) / SS + 0.5 / SS
    out = []
    for i in range(n):
        p = i / n
        z = 1 + zoom * (0.5 - 0.5 * np.cos(TAU * p))           # in and back out once per loop
        dx = 1.5 * np.sin(TAU * p)                               # a little drift
        u = (fx + (sx - fx) / z + dx) * SS - 0.5
        v = (fy + (sy - fy) / z) * SS - 0.5
        u0, v0 = np.clip(np.floor(u).astype(int), 0, hw - 2), np.clip(np.floor(v).astype(int), 0, hh - 2)
        fu, fv = np.clip(u - u0, 0, 1)[..., None], np.clip(v - v0, 0, 1)[..., None]
        s = (hi[v0, u0] * (1 - fu) * (1 - fv) + hi[v0, u0 + 1] * fu * (1 - fv)
             + hi[v0 + 1, u0] * (1 - fu) * fv + hi[v0 + 1, u0 + 1] * fu * fv)
        f = grade(box_down(s, SS), it.get("grade", {}))
        g = al.get("glow", 1.0)
        if g:  # rim light: red from the left, blue from the right, breathing in opposite phase
            pulse = 0.5 + 0.5 * np.sin(TAU * p)
            left = np.clip(1 - XX / 24, 0, 1) ** 2.5
            right = np.clip(1 - (W - 1 - XX) / 24, 0, 1) ** 2.5
            f += g * (np.array([200, 10, 30], np.float32) * (left * (0.35 + 0.65 * pulse))[..., None]
                      + np.array([20, 60, 220], np.float32) * (right * (1 - 0.65 * pulse))[..., None]) * 0.4
        if al.get("ripple", False):  # spider-sense: three rings from the head, once per loop (opt-in)
            for k in range(3):
                t = (p - 0.55 - k * 0.05) / 0.3
                if 0 <= t <= 1:
                    rad = 6 + t * 70
                    d = np.abs(np.hypot(XX + 0.5 - fx, YY + 0.5 - fy) - rad)
                    a = np.clip(1 - d / 1.4, 0, 1) * (1 - t) ** 1.5 * 0.9
                    f = f * (1 - a[..., None]) + np.array([255, 235, 235], np.float32) * a[..., None]
        if al.get("web"):  # a web-shot streak that zips across and fades
            x0, y0, x1, y1 = al["web"]
            t = (p - 0.15) / 0.18
            if 0 <= t <= 1:
                head = min(1.0, t * 2.5)
                tail = max(0.0, t * 2.5 - 1.2)
                L = np.hypot(x1 - x0, y1 - y0)
                along = ((XX + 0.5 - x0) * (x1 - x0) + (YY + 0.5 - y0) * (y1 - y0)) / L ** 2
                across = np.abs((XX + 0.5 - x0) * (y1 - y0) - (YY + 0.5 - y0) * (x1 - x0)) / L
                a = (np.clip(1 - across / 0.9, 0, 1) * ((along >= tail) & (along <= head))
                     * (1 - smoothstep(0.6, 1.0, t)))
                f = f * (1 - a[..., None]) + np.array([240, 245, 255], np.float32) * a[..., None]
        out.append(np.clip(f, 0, 255))
    return out, fps


def build(out_dir, items_out):
    path = os.path.join(PERSONAL, "sources.json")
    if not os.path.exists(path):
        print(f"no {os.path.relpath(path)}: nothing personal to build")
        return
    with open(path) as fh:
        cfg = json.load(fh)
    cache = os.path.join(PERSONAL, "cache")
    os.makedirs(cache, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "personal"), exist_ok=True)
    for it in cfg["items"]:
        ext = os.path.splitext(it["url"].split("?")[0])[1].lower() or ".bin"
        src = os.path.join(cache, it["slug"] + ext)
        try:
            got = fetch(it["url"], src)
        except Exception as e:
            print(f"FAILED {it['slug']}: {e}")
            continue
        mode = it.get("mode", "clip")
        frames, fps = {"clip": render_clip, "still": render_still, "pixel": render_pixel}[mode](src, it)
        arr = to_u8(frames)
        dest = encode(arr, os.path.join(out_dir, "personal", it["slug"]), fps)
        rel = os.path.relpath(dest, out_dir)
        for k in [k for k, v in items_out.items() if v.get("slug") == it["slug"]]:
            del items_out[k]
        st = wall_stats(arr)
        items_out[rel] = {"file": rel, "slug": it["slug"], "name": it["title"], "category": "personal",
                          "playlist": it.get("playlist") or cfg.get("playlist", "Personal"), "seconds": round(len(arr) / fps, 2),
                          "frames": len(arr), "fps": fps, "about": it["url"],
                          "upload": {"filter": "pixel", "scale": "fit", "dither": bool(it.get("dither"))},
                          "stats": {k: round(v, 3) for k, v in st.items()}}
        print(f"{'downloaded' if got else 'cached    '} {it['slug']:<22} {mode:<5} {len(arr):>4} frames "
              f"@{fps}fps -> {rel} ({os.path.getsize(dest) // 1024}K, power {st['power']:.3f})", flush=True)
