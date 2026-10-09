#!/usr/bin/env python3
"""Contact sheet of the art pack as the wall will show it.

  python3 art/preview.py [--dir art/out] [--sheet /tmp/ledposter-art/contact.png] [--only SLUG ...]

Every item in DIR/manifest.json goes through the project's own transcoder
(web/ledposter/transcode.py) with the settings tools/artpack.py uploads it with, then
frames are simulated as the panels light them (gamma-encoded values on 7 PWM bits, the
real wall's config) and converted back for a normal screen. Each tile shows the first
frame and a mid-loop frame, labelled. --zoom writes per-item 3x enlargements too.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build import ensure_numpy  # noqa: E402

ensure_numpy()  # re-runs under web/.venv when python3 lacks numpy

import numpy as np  # noqa: E402
sys.path.insert(0, os.path.join(HERE, "..", "web"))

from ledposter.transcode import transcode  # noqa: E402
from lib import H, W, text, text_width  # noqa: E402

PWM_BITS = 7


def wall_view(frame_gamma):
    """Transcoded (gamma-applied) values -> what the eye sees, as sRGB for a monitor."""
    q = (frame_gamma.astype(np.int32) >> (8 - PWM_BITS)) / (2 ** PWM_BITS - 1)
    return (255 * q ** (1 / 2.2)).round().astype(np.uint8)


def read_frames(tx_dir):
    with open(os.path.join(tx_dir, "meta.json")) as fh:
        meta = json.load(fh)
    raw = np.fromfile(os.path.join(tx_dir, "frames.rgb"), np.uint8)
    return raw.reshape(-1, meta["h"], meta["w"], 3), meta


def save_png(arr, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", f"{arr.shape[1]}x{arr.shape[0]}", "-i", "pipe:0", "-frames:v", "1", path],
                   input=np.ascontiguousarray(arr).tobytes(), check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE, "out"))
    ap.add_argument("--sheet", default="/tmp/ledposter-art/contact.png")
    ap.add_argument("--work", default="/tmp/ledposter-art/tx")
    ap.add_argument("--only", nargs="+")
    ap.add_argument("--category", nargs="+", help="painting, scene, ambient, personal")
    ap.add_argument("--dither", choices=["item", "on", "off"], default="item",
                    help="transcoder dither: the item's upload setting, or force it")
    ap.add_argument("--zoom", action="store_true", help="also write 3x tiles to WORK/zoom/")
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--strip", type=int, default=0, metavar="K",
                    help="also write K evenly spaced frames plus the last->first seam to WORK/strip/")
    a = ap.parse_args()
    with open(os.path.join(a.dir, "manifest.json")) as fh:
        items = json.load(fh)["items"]
    if a.only:
        items = [i for i in items if i["slug"] in a.only]
    if a.category:
        items = [i for i in items if i["category"] in a.category]
    tiles = []
    for it in items:
        up = it["upload"]
        tx = os.path.join(a.work, it["slug"])
        meta = transcode(os.path.join(a.dir, it["file"]), tx, filter=up["filter"], scale=up["scale"],
                         crop=up.get("crop"), position=tuple(up.get("position", (0.5, 0.5))),
                         dither=up.get("dither", False) if a.dither == "item" else a.dither == "on")
        frames, meta = read_frames(tx)
        dur = sum(meta["durations_ms"]) / 1000
        picks = [0, len(frames) // 2] if len(frames) > 1 else [0]
        views = [wall_view(frames[k]) for k in picks]
        power = float(frames[:: max(1, len(frames) // 30)].mean() / 255)  # LED drive, full white = 1
        print(f"{it['slug']:<22} {len(frames):>4} frames {dur:>6.2f}s {meta['scale']}/{meta['filter']:<6} "
              f"power {power:.3f}")
        gap = 4
        tile = np.zeros((H + 14, 2 * W + gap, 3), np.uint8)
        for j, v in enumerate(views):
            x0 = j * (W + gap) if len(views) > 1 else (W + gap) // 2
            tile[:H, x0:x0 + W] = v
        lab = np.zeros((14, tile.shape[1], 3), np.float32)
        label = it["slug"].upper()[:64]
        text(lab, label, 2, 2, np.array([255, 255, 255], np.float32))
        if len(frames) > 1:
            info = f"{dur:.1f}S"
            text(lab, info, tile.shape[1] - text_width(info) - 2, 8, np.array([140, 200, 255], np.float32))
        tile[H:] = lab.astype(np.uint8)
        tiles.append(tile)
        if a.strip and len(frames) > 1:
            idx = sorted(set([int(j * len(frames) / a.strip) for j in range(a.strip)] + [len(frames) - 1]))
            row = np.concatenate([np.pad(wall_view(frames[k]), ((0, 0), (0, 2), (0, 0))) for k in idx[1:] + [0]], 1)
            save_png(np.repeat(np.repeat(row, 2, 0), 2, 1), os.path.join(a.work, "strip", it["slug"] + ".png"))
        if a.zoom:
            save_png(np.repeat(np.repeat(tile, 3, 0), 3, 1), os.path.join(a.work, "zoom", it["slug"] + ".png"))
    cols = a.cols
    th, tw = tiles[0].shape[:2]
    rows = (len(tiles) + cols - 1) // cols
    pad = 6
    sheet = np.full((rows * (th + pad) + pad, cols * (tw + pad) + pad, 3), 40, np.uint8)
    for k, t in enumerate(tiles):
        r, c = divmod(k, cols)
        sheet[pad + r * (th + pad):pad + r * (th + pad) + th, pad + c * (tw + pad):pad + c * (tw + pad) + tw] = t
    save_png(sheet, a.sheet)
    print(f"contact sheet: {a.sheet} ({sheet.shape[1]}x{sheet.shape[0]}, {len(tiles)} items)")


if __name__ == "__main__":
    main()
