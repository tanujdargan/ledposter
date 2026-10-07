"""Transcode an upload into the engine's media cache (docs/ARCHITECTURE.md):
out_dir/frames.rgb (raw RGB24 frames, w*h*3 bytes each), meta.json, thumb.png.

ffmpeg decodes, drops frames above fps_cap and scales; numpy applies gamma and
optional ordered dither. Frames are streamed, never all held in memory.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from fractions import Fraction

import numpy as np

MAX_BYTES = 200 * 1024 * 1024
SUPPORTED = "Supported formats: GIF, APNG, PNG, JPG, WebP, MP4, WebM, MOV (up to 200MB)."
MOV = "mov,mp4,m4a,3gp,3g2,mj2"
# extension -> what ffprobe may detect it as (image2 is how some builds open single images)
FORMATS = {
    ".gif": {"gif"},
    ".png": {"png_pipe", "apng", "image2"},
    ".apng": {"apng"},
    ".jpg": {"jpeg_pipe", "image2"},
    ".jpeg": {"jpeg_pipe", "image2"},
    ".webp": {"webp_pipe", "webp_anim", "image2"},
    ".mp4": {MOV},
    ".mov": {MOV},
    ".webm": {"matroska,webm"},
}
ALPHA = {".gif", ".png", ".apng", ".webp"}
STILL_MS = 10000  # single-frame items: the engine's default still duration
BAYER4 = (np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) + 0.5) / 16


class TranscodeError(Exception):
    """User-facing failure; the message is shown verbatim in the web app."""

    def __init__(self, what):
        super().__init__(f"{what}. {SUPPORTED}")


def _probe(src):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=format_name:stream=width,height,time_base:stream_side_data=rotation"
         ":packet=pts,duration", "-of", "json", src],
        capture_output=True, text=True)
    try:
        info = json.loads(r.stdout)
        fmt, st = info["format"]["format_name"], info["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise TranscodeError("Could not read this file as an image or video"
                             + (f" ({r.stderr.strip().splitlines()[-1]})" if r.stderr.strip() else ""))
    sw, sh = st["width"], st["height"]
    if any(abs(sd.get("rotation", 0)) == 90 for sd in st.get("side_data_list", [])):
        sw, sh = sh, sw  # ffmpeg autorotates, so crop and auto-filter see the rotated frame
    # Source timeline length from packets (demux only, no decode): keeps the last frame's delay.
    pk = [p for p in info.get("packets", []) if "pts" in p]
    tb = Fraction(st.get("time_base", "1/1"))
    span = (max(p["pts"] + p.get("duration", 0) for p in pk) - min(p["pts"] for p in pk)) * tb if pk else 0
    return fmt, sw, sh, span


def _ffmpeg_error(log):
    lines = [l.strip() for l in log.splitlines() if "Parsed_showinfo" not in l and l.strip()]
    errors = [l for l in lines if ("rror" in l or "Invalid" in l) and "Conversion failed" not in l]
    return (errors or lines or ["unknown error"])[0]


def transcode(src_path, out_dir, *, w=128, h=192, scale="fit", filter=None, crop=None,
              position=(0.5, 0.5), gamma=2.2, dither=False, fps_cap=30):
    if scale not in ("fit", "fill", "stretch"):
        raise ValueError(f"scale must be fit, fill or stretch, not {scale!r}")
    if filter not in (None, "smooth", "pixel"):
        raise ValueError(f"filter must be smooth, pixel or None, not {filter!r}")
    px, py = position
    if not (0 <= px <= 1 and 0 <= py <= 1) or gamma <= 0 or fps_cap <= 0:
        raise ValueError("position must be within 0..1, gamma and fps_cap positive")

    ext = os.path.splitext(src_path)[1].lower()
    if ext not in FORMATS:
        raise TranscodeError(f"Unsupported file type '{ext or 'no extension'}'")
    try:
        size = os.path.getsize(src_path)
    except OSError as e:
        raise TranscodeError(f"Cannot read the file: {e.strerror}")
    if size > MAX_BYTES:
        raise TranscodeError(f"File is {size / 2**20:.0f}MB, over the 200MB limit")
    fmt, sw, sh, span = _probe(src_path)
    if fmt not in FORMATS[ext]:
        raise TranscodeError(f"File content ({fmt}) does not match its {ext} extension")

    if filter is None:  # native pixel art: exactly the canvas, or half of it shown 2x
        filter = "pixel" if (sw, sh) in ((w, h), (w // 2, h // 2)) else "smooth"
    flags = "area" if filter == "smooth" else "neighbor"
    # At most one frame per 1/fps_cap s slot; durations come from the kept frames' pts.
    slot = f"floor(t*{fps_cap}+0.01)"
    vf = [f"select='if(isnan(prev_selected_t),1,gte({slot}-floor(prev_selected_t*{fps_cap}+0.01),1))'"]
    if crop:
        cx, cy, cw, ch = (int(v) for v in crop)
        if cx < 0 or cy < 0 or cw <= 0 or ch <= 0 or cx + cw > sw or cy + ch > sh:
            raise ValueError(f"crop {cx},{cy},{cw},{ch} is outside the {sw}x{sh} source")
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
    if ext in ALPHA:  # composite transparency onto black; 16-bit so opaque pixels stay exact
        vf.append("format=rgba64le,premultiply=inplace=1")
    fit = {"fit": ":force_original_aspect_ratio=decrease", "fill": ":force_original_aspect_ratio=increase"}
    vf += [f"scale={w}:{h}{fit.get(scale, '')}:flags={flags}", "format=rgb24"]
    if scale == "fit":
        vf.append(f"pad={w}:{h}:(ow-iw)*{px}:(oh-ih)*{py}:black")
    elif scale == "fill":
        vf.append(f"crop={w}:{h}:(iw-ow)*{px}:(ih-oh)*{py}")
    vf.append("showinfo")

    os.makedirs(out_dir, exist_ok=True)
    frames_path = os.path.join(out_dir, "frames.rgb")
    lut = (np.arange(256) / 255.0) ** gamma * 255.0
    thresh = np.tile(BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w, None] if dither else 0.5
    fb = w * h * 3
    n, thumb = 0, None
    with tempfile.TemporaryFile() as log, open(frames_path + ".part", "wb") as out:
        proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-i", src_path,
             "-map", "0:v:0", "-vf", ",".join(vf), "-fps_mode", "passthrough",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            stdout=subprocess.PIPE, stderr=log)
        while len(buf := proc.stdout.read(fb)) == fb:
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            if thumb is None or (thumb.mean() < 16 and frame.mean() >= 16):
                thumb = frame  # first non-black frame, before gamma so it looks right on screens
            out.write(np.floor(lut[frame] + thresh).clip(0, 255).astype(np.uint8).tobytes())
            n += 1
        proc.stdout.close()
        rc = proc.wait()
        log.seek(0)
        text = log.read().decode(errors="replace")
    try:
        if rc != 0 and ext == ".webp" and fmt == "webp_pipe" and b"ANIM" in open(src_path, "rb").read(64):
            raise TranscodeError("Animated WebP is not supported by this system's ffmpeg; "
                                 "convert it to GIF or MP4")
        if rc != 0:
            raise TranscodeError(f"ffmpeg could not decode this file: {_ffmpeg_error(text)}")
        if n == 0:
            raise TranscodeError("No frames could be decoded from this file")
        tb = re.search(r"Parsed_showinfo.*config in time_base: (\d+)/(\d+)", text)
        pts = [int(p) for p in re.findall(r"Parsed_showinfo\S* @ \S+\] n:\s*\d+ pts:\s*(-?\d+)", text)]
        if not tb or len(pts) != n:
            raise TranscodeError(f"Could not read frame timings ({len(pts)} for {n} frames)")
        tb = Fraction(int(tb[1]), int(tb[2]))
        starts = [round((p - pts[0]) * tb * 1000) for p in pts]
        durations = [b - a for a, b in zip(starts, starts[1:])]
        last = round(span * 1000) - starts[-1]
        durations.append(last if last > 0 else durations[-1] if durations else STILL_MS)
        if n == 1:
            durations = [STILL_MS]
        os.replace(frames_path + ".part", frames_path)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
             "-i", "pipe:0", "-frames:v", "1", os.path.join(out_dir, "thumb.png")],
            input=thumb.tobytes(), check=True, capture_output=True)
    except BaseException:
        for f in (frames_path + ".part", frames_path):
            if os.path.exists(f):
                os.remove(f)
        raise

    meta = {
        "w": w, "h": h, "durations_ms": durations, "fps_cap": fps_cap,
        "scale": scale, "filter": filter, "crop": list(crop) if crop else None,
        "position": [px, py], "gamma": gamma, "dither": dither,
        "source": {"name": os.path.basename(src_path), "format": fmt, "w": sw, "h": sh},
    }
    tmp = os.path.join(out_dir, "meta.json.tmp")
    with open(tmp, "w") as f:  # written last: a cache entry is complete once meta.json exists
        json.dump(meta, f)
    os.replace(tmp, os.path.join(out_dir, "meta.json"))
    return meta


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m ledposter.transcode", description=__doc__.split("\n")[0])
    p.add_argument("src")
    p.add_argument("out_dir")
    p.add_argument("--w", type=int, default=128)
    p.add_argument("--h", type=int, default=192)
    p.add_argument("--scale", choices=["fit", "fill", "stretch"], default="fit")
    p.add_argument("--filter", choices=["smooth", "pixel"], help="default: auto")
    p.add_argument("--crop", help="x,y,w,h in source pixels")
    p.add_argument("--position", default="0.5,0.5", help="anchor x,y in 0..1 for fit/fill")
    p.add_argument("--gamma", type=float, default=2.2)
    p.add_argument("--dither", action="store_true")
    p.add_argument("--fps-cap", type=int, default=30)
    a = p.parse_args(argv)
    try:
        m = transcode(a.src, a.out_dir, w=a.w, h=a.h, scale=a.scale, filter=a.filter,
                      crop=[int(v) for v in a.crop.split(",")] if a.crop else None,
                      position=tuple(float(v) for v in a.position.split(",")),
                      gamma=a.gamma, dither=a.dither, fps_cap=a.fps_cap)
    except (TranscodeError, ValueError) as e:
        sys.exit(f"error: {e}")
    d = m["durations_ms"]
    print(f"{len(d)} frames, {sum(d) / 1000:.2f}s, {m['w']}x{m['h']} {m['scale']}/{m['filter']} -> {a.out_dir}")


if __name__ == "__main__":
    main()
