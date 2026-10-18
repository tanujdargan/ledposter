"""Shared helpers for the art pack: canvas, drawing, loopable noise, a pixel font,
the piece registry and the encoder.

Conventions (see docs/ART.md):
- Frames are float32 arrays (H, W, 3) in sRGB 0..255; the encoder clips and rounds.
- Every animation is a pure function of the loop phase p in [0, 1), or simulates something
  that provably returns to its start, so the last frame flows into the first.
- Colours on the wall: black is "LEDs off". The transcoder applies gamma 2.2 and the panels
  run 7 PWM bits, so any channel under ~40 is effectively off and 40..70 only gets a couple
  of brightness steps (banding, shimmer). Visible colours keep their main channel >= ~70.
"""
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

W, H = 128, 192
YY, XX = np.mgrid[0:H, 0:W].astype(np.float32)
TAU = 2 * np.pi


# -- registry -------------------------------------------------------------------------------

@dataclass
class Piece:
    slug: str
    title: str
    category: str          # "scene" (pixel scenes) or "ambient" (generative)
    seconds: float         # loop length; 0 = still
    fps: int
    render: Callable       # render(n_frames) -> iterable of frames
    blurb: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def frames(self):
        return max(1, round(self.seconds * self.fps))


PIECES: list[Piece] = []


def piece(slug, title, category, seconds=0, fps=60, blurb=""):
    """Register a generator. 60fps: fewer frames flickered visibly more on the wall (2026-10-17). It
    is not a GIF rate (whole centiseconds), so pieces are lossless RGB H.264."""
    def deco(fn):
        PIECES.append(Piece(slug, title, category, seconds, fps, fn, blurb))
        return fn
    return deco


def rng(name):
    """Deterministic generator per piece/purpose (stable across Python runs, unlike hash())."""
    return np.random.default_rng(int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "little"))


# -- colour ---------------------------------------------------------------------------------

def C(hexstr):
    h = hexstr.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def mix(a, b, t):
    t = np.asarray(t, np.float32)[..., None] if np.ndim(t) else t
    return a + (b - a) * t


def ramp(stops, v):
    """Piecewise-linear palette: stops [(pos, colour)], v array in 0..1 -> (..., 3)."""
    v = np.clip(np.asarray(v, np.float32), 0, 1)
    pos = np.array([s[0] for s in stops], np.float32)
    cols = np.array([s[1] for s in stops], np.float32)
    return np.stack([np.interp(v, pos, cols[:, c]) for c in range(3)], -1)


def bands(stops, v):
    """Like ramp() but stepped: each stop's colour holds until the next position (pixel-art look)."""
    v = np.asarray(v, np.float32)
    pos = np.array([s[0] for s in stops], np.float32)
    cols = np.array([s[1] for s in stops], np.float32)
    return cols[np.clip(np.searchsorted(pos, v, side="right") - 1, 0, len(stops) - 1)]


def hsv(h, s, v):
    """Vectorised HSV -> sRGB 0..255; h in turns (0..1)."""
    h, s, v = (np.asarray(a, np.float32) for a in (h, s, v))
    h6 = (h % 1.0) * 6
    k = lambda n: (n + h6) % 6
    f = lambda n: v - v * s * np.clip(np.minimum(k(n), 4 - k(n)), 0, 1)
    return np.stack([f(5), f(3), f(1)], -1) * 255


def quantize(img, palette):
    """Snap every pixel to the nearest palette colour (keeps scenes GIF-exact and crisp)."""
    pal = np.asarray(palette, np.float32)
    d = ((img[..., None, :] - pal) ** 2).sum(-1)
    return pal[d.argmin(-1)]


def smoothstep(e0, e1, x):
    t = np.clip((np.asarray(x, np.float32) - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


# -- canvas and drawing ---------------------------------------------------------------------

def canvas(col=None):
    f = np.zeros((H, W, 3), np.float32)
    if col is not None:
        f[:] = col
    return f


def rect(f, x0, y0, x1, y1, col):
    """Fill [x0, x1) x [y0, y1), clipped."""
    x0, y0, x1, y1 = (int(round(v)) for v in (x0, y0, x1, y1))
    f[max(y0, 0):max(min(y1, H), 0), max(x0, 0):max(min(x1, W), 0)] = col


def put(f, x, y, col):
    """Set pixels at (arrays of) x, y; out-of-canvas points are dropped."""
    x, y = np.broadcast_arrays(np.round(np.atleast_1d(x)).astype(int), np.round(np.atleast_1d(y)).astype(int))
    ok = (x >= 0) & (x < W) & (y >= 0) & (y < H)
    col = np.asarray(col, np.float32)
    f[y[ok], x[ok]] = col if col.ndim == 1 else col[ok]


def disc(f, cx, cy, r, col, mask_only=False):
    m = (XX - cx + 0.5) ** 2 + (YY - cy + 0.5) ** 2 <= r * r
    if mask_only:
        return m
    f[m] = col
    return m


def soft_disc(cx, cy, r, edge=1.0):
    """Anti-aliased disc coverage 0..1 (for the smooth, non-pixel pieces)."""
    d = np.sqrt((XX - cx + 0.5) ** 2 + (YY - cy + 0.5) ** 2)
    return np.clip((r - d) / edge + 0.5, 0, 1)


def glow(cx, cy, r, falloff=2.0):
    """Radial glow 1 at the centre to 0 at radius r."""
    d = np.sqrt((XX - cx) ** 2 + (YY - cy) ** 2) / r
    return np.clip(1 - d, 0, 1) ** falloff


def over(f, col, alpha):
    """Alpha-composite a colour over the frame with a per-pixel alpha map."""
    a = np.asarray(alpha, np.float32)[..., None]
    f *= 1 - a
    f += a * np.asarray(col, np.float32)
    return f


def sprite(f, rows, pal, x, y, flip=False, scale=1):
    """Draw a text sprite: one char per pixel, '.' transparent, other chars index `pal`."""
    for j, row in enumerate(rows):
        if flip:
            row = row[::-1]
        for i, ch in enumerate(row):
            if ch != ".":
                rect(f, x + i * scale, y + j * scale, x + (i + 1) * scale, y + (j + 1) * scale, pal[ch])


# -- loopable value noise -------------------------------------------------------------------

class Noise:
    """Value noise on a lattice that wraps in every axis, so sampling it along a loop phase
    (t = p * lattice_t) or scrolling by whole lattice periods is seamless. Quintic fade."""

    def __init__(self, name, shape):
        self.g = rng(name).random(shape).astype(np.float32)
        self.shape = shape

    def __call__(self, *coords):
        coords = [np.asarray(c, np.float32) for c in coords]
        coords = np.broadcast_arrays(*coords)
        i0 = [np.floor(c).astype(int) for c in coords]
        fr = [c - i for c, i in zip(coords, i0)]
        fr = [t * t * t * (t * (t * 6 - 15) + 10) for t in fr]
        out = 0
        nd = len(coords)
        for corner in range(1 << nd):
            idx, wgt = [], 1
            for d in range(nd):
                b = (corner >> d) & 1
                idx.append((i0[d] + b) % self.shape[d])
                wgt = wgt * (fr[d] if b else 1 - fr[d])
            out = out + self.g[tuple(idx)] * wgt
        return out


def fbm(noises, *coords, gain=0.5):
    """Sum octaves: noises[k] is sampled at coords * 2**k (each lattice must be 2**k larger)."""
    tot, amp, norm = 0, 1.0, 0
    for k, n in enumerate(noises):
        # offset each octave so their lattice points (where value noise momentarily stops) don't line up
        tot = tot + amp * n(*[c * (2 ** k) + 0.37 * k for c in coords])
        norm += amp
        amp *= gain
    return tot / norm


def octaves(name, shape, n):
    return [Noise(f"{name}/{k}", tuple(s * 2 ** k for s in shape)) for k in range(n)]


# -- pixel font (3x5) -----------------------------------------------------------------------

_FONT = {
    "A": "25755", "B": "65656", "C": "34443", "D": "65556", "E": "74647", "F": "74644", "G": "34553",
    "H": "55755", "I": "72227", "J": "11152", "K": "55655", "L": "44447", "M": "57755", "N": "57775",
    "O": "25552", "P": "65644", "Q": "25563", "R": "65655", "S": "34216", "T": "72222", "U": "55557",
    "V": "55552", "W": "55775", "X": "55255", "Y": "55222", "Z": "71247", "0": "75557", "1": "26227",
    "2": "61247", "3": "61216", "4": "55711", "5": "74616", "6": "34757", "7": "71222", "8": "75757",
    "9": "75716", "-": "00700", ":": "02020", ".": "00002", " ": "00000", "!": "22202",
}


def text(f, s, x, y, col, scale=1):
    """Draw text in the 3x5 font (4px advance per char at scale 1)."""
    for k, ch in enumerate(s.upper()):
        rows = _FONT.get(ch, _FONT[" "])
        for j, r in enumerate(rows):
            bits = int(r)
            for i in range(3):
                if bits & (4 >> i):
                    rect(f, x + (k * 4 + i) * scale, y + j * scale,
                         x + (k * 4 + i + 1) * scale, y + (j + 1) * scale, col)


def text_width(s, scale=1):
    return (len(s) * 4 - 1) * scale


# -- encoding -------------------------------------------------------------------------------

GIF_FPS = (20, 25, 50)  # GIF delays are whole centiseconds


def to_u8(frames):
    return np.stack([np.clip(np.round(f), 0, 255).astype(np.uint8) for f in frames])


def colour_count(arr):
    flat = arr.reshape(-1, 3).astype(np.uint32)
    return len(np.unique((flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]))


def encode(arr, base, fps):
    """Write PNG (still), lossless GIF (<=256 colours at a GIF-able fps) or lossless RGB H.264.
    Returns the path. Verifies the round trip so a lossy encode can never slip through."""
    n = len(arr)
    cleanup = []
    raw = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "pipe:0"]
    if n == 1:
        path = base + ".png"
        cmd = raw + ["-frames:v", "1", path]
    elif fps in GIF_FPS and colour_count(arr) <= 256:
        # Exact palette (the scene's own colours) so paletteuse is a lossless lookup.
        path = base + ".gif"
        pal = np.unique(arr.reshape(-1, 3), axis=0)
        pal = np.concatenate([pal, np.repeat(pal[-1:], 256 - len(pal), 0)]).reshape(16, 16, 3)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "16x16",
                        "-i", "pipe:0", "-frames:v", "1", tmp], input=pal.tobytes(), check=True)
        cleanup.append(tmp)
        cmd = raw + ["-i", tmp, "-lavfi", "[0:v][1:v]paletteuse=dither=none", "-loop", "0", path]
    else:
        path = base + ".mp4"
        cmd = raw + ["-c:v", "libx264rgb", "-qp", "0", "-preset", "veryslow", "-pix_fmt", "rgb24",
                     "-movflags", "+faststart", path]
    for ext in (".png", ".gif", ".mp4"):  # a piece that changed format leaves no stale twin
        if os.path.exists(base + ext) and base + ext != path:
            os.remove(base + ext)
    try:
        subprocess.run(cmd, input=arr.tobytes(), check=True)
    finally:
        for tmp in cleanup:
            os.remove(tmp)
    back = decode(path)
    if back.shape != arr.shape or np.abs(back.astype(int) - arr).max() > 0:
        diff = "shape " + str(back.shape) if back.shape != arr.shape else \
            f"max diff {np.abs(back.astype(int) - arr).max()}"
        raise RuntimeError(f"{path}: encode is not lossless ({diff})")
    return path


def decode(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24",
                          "-fps_mode", "passthrough", "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(-1, H, W, 3)


# -- wall checks ----------------------------------------------------------------------------

def wall_stats(arr):
    """Numbers that predict how a piece looks and draws on the LEDs (gamma 2.2, 7 PWM bits).
    power: mean LED drive across all channels, 0..1 (full white = 1).
    dim: share of lit pixels whose brightest channel is < 40 (lands on 0-1 PWM steps: lost or shimmering).
    white: share of pixels with every channel > 200.
    seam: last->first frame change relative to the largest frame-to-frame change inside the loop
    (<= 1: the wrap is no bigger a step than any other frame step)."""
    lin = (arr.astype(np.float32) / 255) ** 2.2
    mx = arr.max(-1)
    lit = mx > 0
    st = {"power": float(lin.mean()),
          "peak_power": float(lin.mean(axis=(1, 2, 3)).max()),
          "dim": float(((mx > 0) & (mx < 40)).sum() / max(lit.sum(), 1)),
          "white": float((arr.min(-1) > 200).mean()),
          "colours": colour_count(arr)}
    if len(arr) > 2:
        d = np.abs(np.diff(arr.astype(np.int16), axis=0)).mean(axis=(1, 2, 3))
        seam = np.abs(arr[0].astype(np.int16) - arr[-1]).mean()
        st["seam"] = float(seam / max(d.max(), 1e-6))
    return st


def write_json(path, obj):
    with open(path + ".tmp", "w") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(path + ".tmp", path)
