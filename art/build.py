#!/usr/bin/env python3
"""Render the ledposter art pack.

  python3 art/build.py [--out art/out] [--only SLUG ...]   original pieces (numpy + ffmpeg)
  python3 art/build.py --paintings [--out art/out]        download the public-domain paintings
  python3 art/build.py --personal [--out art/out]         your own media from art/personal/sources.json
                                                          (kept out of the repo; see art/personal.py)

Writes OUT/<category>/<slug>.{gif,mp4,png} and OUT/manifest.json, which tools/artpack.py
reads to upload everything with the right settings. Deterministic: the same code gives
byte-identical frames. Needs numpy (web/.venv has it) and ffmpeg on PATH.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

USER_AGENT = "ledposter-artpack/1.0 (personal LED wall; https://github.com/ ledposter art/build.py)"
PAINTINGS_JSON = os.path.join(HERE, "paintings.json")
CACHE = os.path.join(HERE, ".cache")


def load_manifest(out):
    try:
        with open(os.path.join(out, "manifest.json")) as fh:
            return {i["file"]: i for i in json.load(fh)["items"]}
    except (OSError, ValueError, KeyError):
        return {}


def save_manifest(out, items):
    from lib import write_json
    order = {"painting": 0, "scene": 1, "ambient": 2, "personal": 3}
    write_json(os.path.join(out, "manifest.json"), {
        "version": 1, "canvas": [128, 192],
        "items": sorted(items.values(), key=lambda i: (order.get(i["category"], 9), i["file"]))})


def build_pieces(out, only):
    import numpy as np  # noqa: F401  (fail early with a clear message if missing)
    import ambient  # noqa: F401  (registers pieces)
    import scenes  # noqa: F401
    from lib import PIECES, encode, to_u8, wall_stats

    items = load_manifest(out)
    unknown = set(only or ()) - {p.slug for p in PIECES}
    if unknown:
        sys.exit(f"unknown piece(s): {', '.join(sorted(unknown))}; have: {', '.join(p.slug for p in PIECES)}")
    print(f"{'piece':<18} {'file':<28} {'frames':>6} {'loop':>6} {'size':>8} {'cols':>6} "
          f"{'power':>6} {'peak':>5} {'dim':>5} {'white':>5} {'seam':>5}")
    for pc in PIECES:
        if only and pc.slug not in only:
            continue
        t0 = time.time()
        frames = list(pc.render(pc.frames))
        if len(frames) != pc.frames:
            raise RuntimeError(f"{pc.slug}: rendered {len(frames)} frames, expected {pc.frames}")
        arr = to_u8(frames)
        os.makedirs(os.path.join(out, pc.category), exist_ok=True)
        path = encode(arr, os.path.join(out, pc.category, pc.slug), pc.fps)
        rel = os.path.relpath(path, out)
        for k in [k for k, v in items.items() if v.get("slug") == pc.slug]:
            del items[k]
        st = wall_stats(arr)
        items[rel] = {"file": rel, "slug": pc.slug, "name": pc.title, "category": pc.category,
                      "seconds": pc.seconds if len(arr) > 1 else 0, "frames": len(arr), "fps": pc.fps,
                      "about": pc.blurb, "upload": {"filter": "pixel", "scale": "fit"},
                      "stats": {k: round(v, 3) for k, v in st.items()}}
        size = os.path.getsize(path)
        print(f"{pc.slug:<18} {rel:<28} {len(arr):>6} {pc.seconds:>5}s {size / 1024:>7.0f}K "
              f"{st['colours']:>6} {st['power']:>6.3f} {st['peak_power']:>5.2f} {st['dim']:>5.2f} "
              f"{st['white']:>5.2f} {st.get('seam', 0):>5.2f}  ({time.time() - t0:.1f}s)", flush=True)
    save_manifest(out, items)


def fetch(url, dest):
    """Download once into the cache; a polite UA and a pause between real requests."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return False
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as fh:
                ctype = r.headers.get("Content-Type", "")
                if not ctype.startswith("image/"):
                    raise RuntimeError(f"expected an image, got {ctype or 'no content type'}")
                shutil.copyfileobj(r, fh)
            os.replace(dest + ".part", dest)
            time.sleep(1.0)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} for {url}") from None


def probe_size(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "json", path], capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    return s["width"], s["height"]


def portrait_crop(sw, sh, frame):
    """frame = [cx, cy, height] as fractions: centre of a 2:3 crop and its height relative to the
    source height (1 = as tall as possible). Returns x, y, w, h in source pixels, inside the image."""
    cx, cy, fh = frame
    h = min(sh * fh, sw * 1.5)
    w = h / 1.5
    x = min(max(cx * sw - w / 2, 0), sw - w)
    y = min(max(cy * sh - h / 2, 0), sh - h)
    return [int(round(x)), int(round(y)), int(w), int(h)]


def build_paintings(out):
    with open(PAINTINGS_JSON) as fh:
        works = json.load(fh)["paintings"]
    os.makedirs(os.path.join(CACHE, "paintings"), exist_ok=True)
    os.makedirs(os.path.join(out, "painting"), exist_ok=True)
    items = load_manifest(out)
    failed = []
    for wk in works:
        ext = os.path.splitext(wk["image_url"].split("?")[0])[1].lower() or ".jpg"
        ext = ".jpg" if ext == ".jpeg" else ext
        cached = os.path.join(CACHE, "paintings", wk["slug"] + ext)
        try:
            got = fetch(wk["image_url"], cached)
        except Exception as e:
            failed.append(wk["slug"])
            print(f"FAILED {wk['slug']}: {e}", flush=True)
            continue
        dest = os.path.join(out, "painting", wk["slug"] + ext)
        shutil.copyfile(cached, dest)
        sw, sh = probe_size(dest)
        crop = portrait_crop(sw, sh, wk["frame"])
        rel = os.path.relpath(dest, out)
        items[rel] = {"file": rel, "slug": wk["slug"], "name": f"{wk['title']} ({wk['artist']})",
                      "category": "painting", "seconds": 0, "frames": 1,
                      "about": f"{wk['title']}, {wk['artist']}, {wk['year']}. {wk['licence']}; {wk['source_page']}",
                      "upload": {"filter": "smooth", "scale": "fill", "crop": crop, "position": [0.5, 0.5]}}
        print(f"{'downloaded' if got else 'cached    '} {wk['slug']:<22} {sw}x{sh} "
              f"{os.path.getsize(dest) / 1024:>6.0f}K crop {','.join(map(str, crop))}", flush=True)
    save_manifest(out, items)
    if failed:
        sys.exit(f"{len(failed)} download(s) failed: {', '.join(failed)}")


def ensure_numpy():
    """`python3 art/build.py` should just work: fall back to the web app's venv for numpy."""
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        venv = os.path.realpath(os.path.join(HERE, "..", "web", ".venv"))
        if os.path.exists(os.path.join(venv, "bin", "python")) and os.path.realpath(sys.prefix) != venv:
            os.execv(os.path.join(venv, "bin", "python"), [os.path.join(venv, "bin", "python"), *sys.argv])
        sys.exit("numpy not found: pip install numpy, or create web/.venv (README: Development setup)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--only", nargs="+", metavar="SLUG", help="render just these pieces")
    ap.add_argument("--paintings", action="store_true", help="download the paintings instead of rendering")
    ap.add_argument("--personal", action="store_true", help="build art/personal/sources.json instead")
    a = ap.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH (brew install ffmpeg / apt install ffmpeg)")
    os.makedirs(a.out, exist_ok=True)
    ensure_numpy()
    if a.paintings:
        build_paintings(a.out)
    elif a.personal:
        import personal
        items = load_manifest(a.out)
        personal.build(a.out, items)
        save_manifest(a.out, items)
    else:
        build_pieces(a.out, a.only)


if __name__ == "__main__":
    main()
