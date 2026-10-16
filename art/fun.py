"""Fun original pieces (see docs/ART.md): flat-palette pixel loops, lossless GIF at 25 fps.
Every frame is a pure function of the loop phase p in [0, 1); motions run whole cycles per
loop, scripted sequences end exactly where they start. All sprites and designs are original."""
import numpy as np

from lib import (H, TAU, W, XX, YY, C, _FONT, canvas, disc, mix, piece, put, rect, rng, sprite,
                 text, text_width)


def phases(n):
    return (i / n for i in range(n))


def cyc(p, k, off=0.0):
    return (k * p + off) % 1.0


def tri(u):
    u = u % 1.0
    return 1 - abs(1 - 2 * u)


def ease(u):
    u = float(np.clip(u, 0, 1))
    return u * u * (3 - 2 * u)


def ellipse(f, cx, cy, rx, ry, col, where=None):
    m = ((XX + 0.5 - cx) / rx) ** 2 + ((YY + 0.5 - cy) / ry) ** 2 <= 1
    if where is not None:
        m &= where
    f[m] = col
    return m


def ring(f, cx, cy, rx, ry, col, width=1.0):
    d = np.sqrt(((XX + 0.5 - cx) / rx) ** 2 + ((YY + 0.5 - cy) / ry) ** 2)
    m = np.abs(d - 1) * min(rx, ry) < width * 0.5 + 0.25
    f[m] = col
    return m


def poly(f, pts, col):
    """Fill a polygon (even-odd rule) given as [(x, y), ...]."""
    pts = np.asarray(pts, np.float32)
    x0, y0 = np.floor(pts.min(0)).astype(int)
    x1, y1 = np.ceil(pts.max(0)).astype(int) + 1
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
    if x0 >= x1 or y0 >= y1:
        return
    ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32) + 0.5
    inside = np.zeros(xs.shape, bool)
    for i in range(len(pts)):
        (ax, ay), (bx, by) = pts[i], pts[(i + 1) % len(pts)]
        if ay == by:
            continue
        cond = (ay > ys) != (by > ys)
        xint = ax + (ys - ay) * (bx - ax) / (by - ay)
        inside ^= cond & (xs < xint)
    f[y0:y1, x0:x1][inside] = col


def line(f, x0, y0, x1, y1, col, w=1):
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for t in np.linspace(0, 1, n + 1):
        x = int(round(x0 + (x1 - x0) * t - (w - 1) / 2))
        y = int(round(y0 + (y1 - y0) * t - (w - 1) / 2))
        rect(f, x, y, x + w, y + w, col)


def stars(name, count, y0, y1, levels=(110, 160, 220)):
    r = rng(name)
    return (r.integers(0, W, count), r.integers(y0, y1, count), r.choice(levels, count),
            r.integers(1, 4, count), r.random(count))


def draw_stars(f, st, p):
    xs, ys, lv, tw, ph = st
    b = np.where(np.sin(TAU * (tw * p + ph)) > 0.5, lv, np.maximum(lv - 50, 80))
    m = f[ys, xs].max(-1) == 0
    put(f, xs[m], ys[m], np.repeat(b[m, None], 3, 1).astype(np.float32))


NEON = [C(c) for c in ("#ff3cc8", "#3cdcff", "#ffdc3c", "#64ff64", "#ff6e3c", "#a064ff", "#ff4646")]


# ---------------------------------------------------------------------------------------------

@piece("corner-hit", "Corner Hit", "fun", seconds=12,
       blurb="A bouncing LED logo that changes colour on every wall hit, and it does hit the corner.")
def corner_hit(n):
    lw, lh = 44, 30
    L = 12.0
    r = rng("corner-hit/confetti")
    conf = [(r.uniform(0.05, 1.5), r.uniform(50, 130), r.uniform(0.7, 1.6), int(r.integers(0, 7)))
            for _ in range(36)]

    def state(p):
        x = int(round(tri(5 * p) * (W - lw)))
        y = int(round(tri(3 * p) * (H - lh)))
        hits = int(np.floor(10 * p + 1e-9) + np.floor(6 * p + 1e-9) - np.floor(2 * p + 1e-9))
        return x, y, hits % 7  # 14 hits per loop: a multiple of 7 colours

    for p in phases(n):
        f = canvas()
        x, y, ci = state(p)
        col = NEON[ci]
        tc = (p % 0.5) * L  # seconds since the last corner hit (p = 0 top-left, p = 1/2 bottom-right)
        corner = (0, 0, 1, 1) if p < 0.5 else (W, H, -1, -1)
        if tc < 1.8:  # confetti from the corner
            cx, cy, sx, sy = corner
            for ang, sp, life, k in conf:
                if tc < life:
                    px = cx + sx * np.cos(ang) * sp * tc
                    py = cy + sy * np.sin(ang) * sp * tc * (1 if sy > 0 else 1.6) + 50 * tc * tc
                    rect(f, px, py, px + 2, py + 2, NEON[k])
            if int(tc * 6) % 2 == 0 and tc < 1.6:
                msg = "CORNER!"
                text(f, msg, (W - text_width(msg, 2)) // 2, 90, col, scale=2)
        text(f, "LED", x, y, col, scale=4)
        ellipse(f, x + 22, y + 26, 22, 4.2, col)
        text(f, "WALL", x + 15, y + 24, C("#000000"))
        yield f


# ---------------------------------------------------------------------------------------------

def _glyph(ch):
    g = np.zeros((5, 3), bool)
    for j, rr in enumerate(_FONT[ch]):
        for i in range(3):
            g[j, i] = bool(int(rr) & (4 >> i))
    return g


@piece("code-rain", "Code Rain", "fun", seconds=8,
       blurb="Green glyph rain with a hidden hello that flickers past when the drops cross it.")
def code_rain(n):
    r = rng("code-rain")
    cw, ch = 4, 6
    ncol, nrow = W // cw, H // ch
    Q = 12
    glyphs = r.random((48, 5, 3)) < 0.5
    glyphs[:, 2, 1] |= r.random(48) < 0.5
    gidx = r.integers(0, 48, (Q, nrow, ncol))
    goff = r.random((nrow, ncol))
    secret = {}
    for k, c in enumerate("HELLO WALL"):
        if c != " ":
            secret[(15, 11 + k)] = _glyph(c)
    L = 52
    drops = []
    for c in range(ncol):
        k = int(r.choice([1, 2, 2, 3]))
        off = r.random()
        trail = int(r.integers(8, 20))
        drops.append((c, k, off, trail))
        if r.random() < 0.6:
            drops.append((c, k, off + 0.5, int(r.integers(6, 14))))
    cols = [None, C("#1e8c32"), C("#28b446"), C("#3cdc5a"), C("#78ff8c"), C("#dcffdc")]

    for p in phases(n):
        f = canvas()
        lev = np.zeros((nrow, ncol), int)
        for c, k, off, trail in drops:
            head = int(cyc(p, k, off) * L) - 12
            for j in range(trail):
                row = head - j
                if 0 <= row < nrow:
                    lv = 5 if j == 0 else 4 if j < 3 else 3 if j < 7 else 2 if j < 12 else 1
                    lev[row, c] = max(lev[row, c], lv)
        rows, cs = np.nonzero(lev)
        for row, c in zip(rows, cs):
            g = secret.get((row, c))
            lv = lev[row, c]
            if g is None:
                q = int(np.floor(p * Q + goff[row, c])) % Q
                g = glyphs[gidx[q, row, c]]
            else:
                lv = max(lv, 4)
            f[row * ch:row * ch + 5, c * cw:c * cw + 3][g] = cols[lv]
        yield f


# ---------------------------------------------------------------------------------------------

ZED = ["zzzzz", "...z.", "..z..", ".z...", "zzzzz"]


@piece("cat-nap", "Cat Nap", "fun", seconds=8,
       blurb="An orange cat naps on a cushion under the moonlit window; tail swish, ear twitch, a peek.")
def cat_nap(n):
    st = stars("cat-nap/stars", 26, 18, 74)
    L = 8.0
    ora, stripe, cream = C("#ff9a3c"), C("#c8641e"), C("#ffd29a")
    dark, pink = C("#7a3c14"), C("#ff8cb4")
    frame = C("#46508c")

    for p in phases(n):
        t = p * L
        f = canvas()
        # window
        rect(f, 34, 14, 94, 76, frame)
        rect(f, 37, 17, 91, 73, C("#000000"))
        disc(f, 77, 32, 7, C("#f5e6a0"))
        disc(f, 74, 30, 6, C("#000000"))
        draw_stars(f, st, p)
        rect(f, 63, 17, 65, 73, frame)
        rect(f, 37, 44, 91, 46, frame)
        rect(f, 30, 76, 98, 79, C("#6e78b4"))
        for cx0, d in ((28, 1), (100, -1)):  # curtains
            for k in range(4):
                xx = cx0 + d * k * 2
                rect(f, xx, 10, xx + 2, 84 - (k % 2) * 4, C("#8c3cb4") if k % 2 else C("#b45ae6"))
        rect(f, 24, 8, 104, 11, C("#a07850"))
        # floor and cushion
        rect(f, 0, 168, W, 169, C("#5a3c8c"))
        ellipse(f, 66, 161, 46, 9, C("#8c3cc8"))
        ellipse(f, 66, 157, 43, 6, C("#b45ae6"))
        rect(f, 22, 163, 110, 164, C("#a050dc"))
        # yarn ball
        disc(f, 118, 162, 6, C("#3cb4ff"))
        for k in range(3):
            line(f, 113 + k * 3, 158, 115 + k * 3, 167, C("#1e78c8"))
        line(f, 112, 167, 100, 168, C("#3cb4ff"))
        # body (breathes 4 times per loop)
        b = 1 if np.sin(TAU * 4 * p) > 0 else 0
        cy, ry = 140 - b, 14 + b
        body = ellipse(f, 72, cy, 30, ry, ora)
        sm = body & (((XX * 0.8 + YY * 0.35) % 8) < 2) & (YY < cy + 2)
        f[sm] = stripe
        # tail curling round the front, tip swishes twice per loop
        sw = np.sin(TAU * 2 * p)
        pts = []
        for s in np.linspace(0, 1, 44):
            tx = 101 - 4 * np.sin(np.pi * s * 0.5) - 38 * s
            ty = 146 + 7 * np.sin(np.pi * min(s * 1.6, 1))
            if s > 0.72:
                ty -= ((s - 0.72) / 0.28) ** 1.5 * (7 + 6 * sw)
            pts.append((tx, ty, s))
        for tx, ty, s in pts:
            disc(f, tx, ty, 4.2, stripe)
        for tx, ty, s in pts:
            col = stripe if int(s * 44) % 8 in (0, 1) else ora
            disc(f, tx, ty, 3.0, cream if s > 0.92 else col)
        # head
        hx, hy = 44, 143 - b * 0.0
        twitch = 2 if 3.0 < t < 3.25 or 3.45 < t < 3.6 else 0
        poly(f, [(33, 140), (35, 125 + twitch), (44, 134)], ora)
        poly(f, [(44, 133), (53, 125), (55, 140)], ora)
        poly(f, [(36, 136), (37, 129 + twitch), (41, 134)], pink)
        poly(f, [(47, 133), (52, 129), (52, 136)], pink)
        disc(f, hx, hy, 11.5, ora)
        for sx in (-6, 0, 6):
            rect(f, hx + sx - 1 + (0 if sx else 0), hy - 11, hx + sx, hy - 7, stripe)
        ellipse(f, hx, hy + 5, 6, 3.5, cream)
        rect(f, hx - 1, hy + 2, hx + 1, hy + 4, pink)
        for wy in (4, 6):
            line(f, hx - 8, hy + wy, hx - 15, hy + wy + (wy - 5) * 2, cream)
            line(f, hx + 8, hy + wy, hx + 15, hy + wy + (wy - 5) * 2, cream)
        peek = 5.0 < t < 5.9
        for ex in (hx - 5, hx + 5):
            if peek and ex < hx and 5.15 < t < 5.75:
                rect(f, ex - 1, hy - 2, ex + 2, hy + 1, C("#8cdc3c"))
                rect(f, ex, hy - 2, ex + 1, hy + 1, C("#000000"))
            else:
                put(f, [ex - 2, ex - 1, ex, ex + 1], [hy - 1, hy, hy, hy - 1], dark)
        disc(f, 40, 155, 3.5, cream)
        disc(f, 49, 155, 3.5, cream)
        # Zzz drifting up (paused while she peeks)
        if not peek:
            for k in range(3):
                u = cyc(p, 2, k / 3)
                zx = 56 + u * 30 + 3 * np.sin(TAU * u * 2)
                zy = 124 - u * 40
                sc = 1 if u < 0.4 else 2
                if u < 0.9:
                    zc = C("#a0b4ff") if u < 0.6 else C("#7882dc")
                    sprite(f, ZED, {"z": zc}, int(zx), int(zy), scale=sc)
        yield f


# ---------------------------------------------------------------------------------------------

TILING = ["aabbbbccxi",
          "aaeefccxxi",
          "ggeefffxhi",
          "ggkkkkhhhi"]
TCOL = {"a": "#ffdc3c", "e": "#ffdc3c", "g": "#ffdc3c", "b": "#3cdcff", "k": "#3cdcff", "i": "#3cdcff",
        "c": "#50e650", "x": "#ff4646", "f": "#3c78ff", "h": "#ff9628"}


@piece("block-drop", "Block Drop", "fun", seconds=10.5,
       blurb="Falling-blocks demo: ten pieces stack into four full rows for a perfect clear.")
def block_drop(n):
    L = 10.5
    cs, wx, wy, rows = 8, 8, 24, 20
    cells = {}
    for rr, s in enumerate(TILING):
        for cc, ch in enumerate(s):
            cells.setdefault(ch, []).append((rr + rows - 4, cc))
    r = rng("block-drop")
    placed, order = set(), []
    while len(order) < len(cells):
        cands = []
        for k, cl in sorted(cells.items()):
            if k in order:
                continue
            path = all((rr2, c) not in placed for rr, c in cl for rr2 in range(rr))
            rest = all(rr == rows - 1 or (rr + 1, c) in placed or (rr + 1, c) in cl for rr, c in cl)
            if path and rest:
                cands.append(k)
        k = cands[int(r.integers(0, len(cands)))]
        order.append(k)
        placed.update(cells[k])
    slot, hold, step = 0.85, 0.12, 0.035
    t_clear = slot * len(order)

    def shade(col):
        return C(col), mix(C(col), C("#ffffff"), 0.55), C(col) * 0.7

    def cell(f, row, col, c, size=cs, x0=wx, y0=wy):
        if row < 0:
            return
        base, hi, lo = shade(c)
        x, y = x0 + col * size, y0 + row * size
        rect(f, x, y, x + size - 1, y + size - 1, base)
        rect(f, x, y, x + size - 1, y + 1, hi)
        rect(f, x, y, x + 1, y + size - 1, hi)
        rect(f, x + 1, y + size - 2, x + size - 1, y + size - 1, lo)
        rect(f, x + size - 2, y + 1, x + size - 1, y + size - 1, lo)

    def falling(k, u):
        """Cells of piece k at u seconds after it spawns (it lands exactly on its tiling cells)."""
        cl = cells[k]
        top = min(rr for rr, _ in cl)
        minc, maxc = min(c for _, c in cl), max(c for _, c in cl)
        spawn = 5 - (maxc - minc + 2) // 2
        steps = int(max(0, u - hold) / step)
        d = max(top - steps, 0)
        sh = spawn - minc
        sh = int(np.sign(sh)) * max(abs(sh) - steps, 0)
        return [(rr - d, c + sh) for rr, c in cl]

    title = "BLOCK DROP"
    for p in phases(n):
        t = p * L
        f = canvas()
        tx = (W - text_width(title)) // 2
        for j, chh in enumerate(title):
            text(f, chh, tx + 4 * j, 8, NEON[j % 6])
        frame = C("#5a6eb4")
        rect(f, wx - 2, wy, wx, wy + rows * cs + 2, frame)
        rect(f, wx + 10 * cs, wy, wx + 10 * cs + 2, wy + rows * cs + 2, frame)
        rect(f, wx - 2, wy + rows * cs, wx + 10 * cs + 2, wy + rows * cs + 2, frame)
        cur = min(int(t / slot), len(order))
        for k in order[:cur]:
            for rr, c in cells[k]:
                if t >= t_clear + 0.5:  # cells vanish from the middle outwards
                    gone = int((t - t_clear - 0.5) / 0.08)
                    if abs(c - 4.5) < gone:
                        continue
                if t_clear <= t < t_clear + 0.5 and int((t - t_clear) * 16) % 2 == 0:
                    rect(f, wx + c * cs, wy + rr * cs, wx + c * cs + cs - 1, wy + rr * cs + cs - 1, C("#fff0b4"))
                else:
                    cell(f, rr, c, TCOL[k])
        if cur < len(order):
            k = order[cur]
            for rr, c in falling(k, t - cur * slot):
                cell(f, rr, c, TCOL[k])
        # side panel
        px = 98
        text(f, "NEXT", px, wy, C("#a0aadc"))
        rect(f, px - 2, wy + 8, px + 26, wy + 30, C("#000000"))
        nxt = order[(cur + 1) % len(order)] if cur < len(order) else order[0]
        cl = cells[nxt]
        r0, c0 = min(a for a, _ in cl), min(b for _, b in cl)
        for rr, c in cl:
            cell(f, rr - r0, c - c0, TCOL[nxt], size=5, x0=px + 2, y0=wy + 12)
        text(f, "LINES", px, wy + 40, C("#a0aadc"))
        text(f, "4" if t >= t_clear + 0.5 else "0", px, wy + 48, C("#ffffff"))
        text(f, "LEVEL", px, wy + 62, C("#a0aadc"))
        text(f, "1", px, wy + 70, C("#ffffff"))
        if t >= t_clear + 0.9:
            for j, w in enumerate(("PERFECT", "CLEAR!")):
                text(f, w, wx + (80 - text_width(w, 2)) // 2, wy + 60 + j * 14,
                     NEON[(j + int(t * 8)) % 6], scale=2)
        yield f


# ---------------------------------------------------------------------------------------------

@piece("tall-pong", "Tall Pong", "fun", seconds=10,
       blurb="Paddle-ball attract mode turned portrait: two paddles that never miss.")
def tall_pong(n):
    k, m = 5, 3                      # ball round trips top<->bottom, and wall-to-wall, per loop
    pw, bs = 24, 4
    ytop, ybot = 12, 176             # paddle rows (4 px thick)
    by0, by1 = ytop + 4, ybot - bs
    cols = {0: C("#ff3cc8"), 1: C("#3cdcff")}
    hits = 2 * k
    aim = rng("tall-pong").integers(-7, 8, hits)

    def ball(p):
        return 2 + tri(m * p) * (W - 4 - bs), by0 + tri(k * p) * (by1 - by0)

    def hit_x(j):
        return ball((j % hits) / hits)[0] + bs / 2 + aim[j % hits]

    def paddle_x(p, side):
        # side 0 (top) hits at even j, side 1 (bottom) at odd j
        j = int(np.floor(p * hits + 1e-9))
        prev = j if j % 2 == side else j - 1
        u = (p * hits - prev) / 2
        a, b = hit_x(prev), hit_x(prev + 2)
        x = a + (b - a) * ease((u - 0.25) / 0.55)
        return int(round(np.clip(x - pw / 2, 0, W - pw)))

    for p in phases(n):
        f = canvas()
        for x in range(2, W, 8):
            rect(f, x, 95, x + 4, 97, C("#5a5a8c"))
        rect(f, 0, 0, 1, H, C("#5a5a8c"))
        rect(f, W - 1, 0, W, H, C("#5a5a8c"))
        text(f, "7", 10, 74, C("#ff3cc8"), scale=3)
        text(f, "7", 10, 103, C("#3cdcff"), scale=3)
        if cyc(p, 5) < 0.6:
            msg = "INSERT COIN"
            text(f, msg, (W - text_width(msg)) // 2, 120, C("#ffdc3c"))
        j = int(np.floor(p * hits + 1e-9))
        last = j % 2
        for side, y in ((0, ytop), (1, ybot)):
            rect(f, paddle_x(p, side), y, paddle_x(p, side) + pw, y + 4, cols[side])
        for jj in (j, j + 1):  # spark at each contact
            dt = (p - jj / hits) % 1.0 * 10
            if dt < 0.25:
                hx, hy = ball((jj % hits) / hits)
                side = jj % 2
                sy = ytop - 1 if side == 0 else ybot + 4
                for a in range(-2, 3):
                    rr = 2 + dt * 30
                    put(f, hx + 2 + a * rr * 0.6, sy + (-1 if side == 0 else 1) * rr * (1 - abs(a) * 0.25),
                        C("#ffffff") if dt < 0.12 else cols[side])
        for jt, colv in ((3, cols[last] * 0.4), (2, cols[last] * 0.6), (1, cols[last] * 0.8)):
            tx, ty = ball((p - jt * 0.004) % 1.0)
            rect(f, tx + 1, ty + 1, tx + 3, ty + 3, colv)
        bx, byy = ball(p)
        rect(f, bx, byy, bx + bs, byy + bs, mix(cols[last], C("#ffffff"), 0.5))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("frog-pond", "Frog Pond", "fun", seconds=10,
       blurb="A frog on a lily pad croaks, blinks and snaps up a firefly; another one drifts in.")
def frog_pond(n):
    L = 10.0
    st = stars("frog-pond/stars", 40, 0, 56)
    r = rng("frog-pond")
    pads = [(22, 112, 14, 4.5), (104, 120, 16, 5), (30, 176, 20, 6), (108, 172, 15, 5), (88, 90, 9, 3)]
    ripples = [(float(r.uniform(10, 118)), float(r.uniform(80, 186)), r.random(), int(r.integers(1, 3)))
               for _ in range(6)]
    green, belly, lite = C("#50c83c"), C("#b4e650"), C("#78e650")
    eyew = C("#f0f5dc")
    tc0, tc1 = 0.55, 0.574       # tongue out and back
    gone, back0, back1 = 0.562, 0.75, 0.86

    def fly_path(p):
        return 64 + 34 * np.sin(TAU * 2 * p), 96 + 12 * np.sin(TAU * 4 * p) + 2 * np.sin(TAU * 9 * p)

    def fly_xy(p):
        if gone <= p < back0:
            return None
        if back0 <= p < back1:
            u = ease((p - back0) / (back1 - back0))
            ex, ey = fly_path(back1)
            return 140 + (ex - 140) * u, 60 + (ey - 60) * u
        return fly_path(p)

    mouth = (64, 132)
    for p in phases(n):
        f = canvas()
        draw_stars(f, st, p)
        disc(f, 100, 24, 8, C("#f5e6a0"))
        disc(f, 97, 22, 1.5, C("#dcc882"))
        disc(f, 103, 27, 1.2, C("#dcc882"))
        rect(f, 0, 58, W, 59, C("#2d7832"))
        for x in range(0, W, 5):
            rect(f, x, 56 - (x * 7 % 3), x + 1, 58, C("#2d7832"))
        for j, y in enumerate(range(64, 104, 5)):  # moon glitter on the water
            w = 3 + j + int(2 * np.sin(TAU * (2 * p + j * 0.37)))
            ox = int(2 * np.sin(TAU * (3 * p + j * 0.21)))
            rect(f, 100 - w + ox, y, 100 + w + ox, y + 1, C("#c8b46e") if j < 4 else C("#8c7d4b"))
        for x, y, ph, k in ripples:
            u = cyc(p, k, ph)
            if u < 0.6:
                rad = 2 + u * 22
                ring(f, x, y, rad, rad / 3, C("#3c78c8") if u < 0.35 else C("#2a55a0"))
        for x, y, rx, ry in pads:
            ellipse(f, x, y, rx, ry, C("#3ca03c"))
            poly(f, [(x, y), (x + rx * 0.9, y - ry * 1.4), (x + rx * 1.3, y - ry * 0.3)], C("#000000"))
            rect(f, x - rx * 0.5, y, x + rx * 0.3, y + 1, C("#2d8232"))
        disc(f, 104, 116, 4, C("#ff78b4"))
        disc(f, 104, 115, 2, C("#ffd2e6"))
        # cattails
        for cx0, ph in ((6, 0.0), (14, 0.3), (118, 0.6), (124, 0.1)):
            sw = int(round(1.5 * np.sin(TAU * (p + ph))))
            line(f, cx0, 112, cx0 + sw, 70, C("#3c8c3c"))
            rect(f, cx0 + sw - 1, 72, cx0 + sw + 2, 84, C("#a0642d"))
        # frog's pad
        ellipse(f, 64, 152, 32, 9, C("#3ca03c"))
        poly(f, [(64, 152), (90, 141), (98, 150)], C("#000000"))
        ring(f, 64, 152, 22, 6, C("#2d8232"))
        # frog
        ellipse(f, 46, 147, 8, 5, green)
        ellipse(f, 82, 147, 8, 5, green)
        ellipse(f, 64, 138, 17, 12, green)
        ellipse(f, 64, 143, 10, 7, belly)
        croak = 0
        for c0 in (0.1, 0.16, 0.3, 0.36):
            if c0 <= p < c0 + 0.05:
                croak = np.sin(np.pi * (p - c0) / 0.05)
        if croak > 0.05:
            ellipse(f, 64, 137 + 2 * croak, 4 + 7 * croak, 2 + 5 * croak, C("#e6f0a0"))
        for ex in (55, 73):
            disc(f, ex, 126, 6.5, green)
        happy = 0.58 <= p < 0.66
        blink = any(b0 <= p < b0 + 0.015 for b0 in (0.25, 0.9, 0.93))
        fx = fly_xy(p)
        for ex in (55, 73):
            if happy:
                put(f, [ex - 3, ex - 2, ex - 1, ex, ex + 1, ex + 2, ex + 3],
                    [127, 126, 125, 124, 125, 126, 127], C("#000000"))
                put(f, [ex - 3, ex - 2, ex + 2, ex + 3], [128, 127, 127, 128], C("#000000"))
            elif blink:
                rect(f, ex - 4, 126, ex + 5, 127, C("#000000"))
            else:
                disc(f, ex, 126, 4.5, eyew)
                lx = 0 if fx is None else int(np.clip((fx[0] - ex) / 25, -1.5, 1.5))
                ly = 0 if fx is None else int(np.clip((fx[1] - 126) / 25, -1.5, 0))
                rect(f, ex - 2 + lx, 125 + ly, ex + 2 + lx, 127 + ly, C("#000000"))
        line(f, 50, 130, 54, 134, C("#000000"))
        line(f, 54, 134, 74, 134, C("#000000"))
        line(f, 74, 134, 78, 130, C("#000000"))
        put(f, [61, 67], [130, 130], C("#000000"))
        ellipse(f, 54, 150, 4, 2.5, lite)
        ellipse(f, 74, 150, 4, 2.5, lite)
        # tongue
        if tc0 <= p < tc1:
            tx, ty = fly_path(gone)
            u = 1 - abs((p - gone) / (gone - tc0))
            u = float(np.clip(u, 0, 1))
            ex, ey = mouth[0] + (tx - mouth[0]) * u, mouth[1] + (ty - mouth[1]) * u
            line(f, mouth[0], mouth[1], ex, ey, C("#ff5a8c"), w=2)
            disc(f, ex, ey, 2, C("#ff5a8c"))
            if p >= gone:
                put(f, ex, ey, C("#ffe650"))
        if fx is not None:
            x, y = fx
            x, y = int(round(x)), int(round(y))
            if cyc(p, 5, 0.3) < 0.6:
                disc(f, x + 1, y + 1, 3.2, C("#8c7d23"))
            flap = int(cyc(p, 60) < 0.5)
            rect(f, x - 2, y - 2 - flap, x, y - 1 - flap, C("#a0c8ff"))
            rect(f, x + 2, y - 2 - flap, x + 4, y - 1 - flap, C("#a0c8ff"))
            rect(f, x, y, x + 2, y + 2, C("#ffe650"))
            put(f, x + 1, y - 1, C("#78645a"))
        yield f


# ---------------------------------------------------------------------------------------------

DUCK = ["......yyyy......",
        ".....yyyyyy.....",
        ".....yyyywky....",
        ".....yyyyyyyooo.",
        ".....yyyyyyoooo.",
        "......yyyyyy....",
        "y......yyyy.....",
        "yy....yyyyyy....",
        "yyy.yyyyyyyyyy..",
        "yyyyyyyddyyyyyy.",
        "yyyyyydddyyyyyy.",
        ".yyyyyyddyyyyy..",
        "..yyyyyyyyyyy...",
        "...yyyyyyyyy...."]
DPAL = {"y": C("#ffd228"), "d": C("#e69614"), "k": C("#000000"), "w": C("#ffffff"), "o": C("#ff7814")}


@piece("bath-duck", "Bath Duck", "fun", seconds=8,
       blurb="A rubber duck bobbing in a bubble bath; the tap drips and bubbles float off and pop.")
def bath_duck(n):
    L = 8.0
    r = rng("bath-duck")
    bubbles = [(float(r.uniform(14, 112)), r.random(), int(r.integers(1, 3)), float(r.uniform(2.5, 4.0)))
               for _ in range(9)]
    foam = [(x, float(r.uniform(2.5, 5))) for x in (14, 20, 27, 33, 93, 100, 107, 114)]
    tub_l, tub_r, rim, bottom = 8, 120, 112, 180
    xs = np.arange(W)

    def surface(x, p):
        return 126 + 2.2 * np.sin(TAU * (x / 64 - 2 * p)) + 1.0 * np.sin(TAU * (x / 23 + 3 * p))

    for p in phases(n):
        t = p * L
        f = canvas()
        tile = C("#284678")
        for y in range(4, rim, 16):
            rect(f, 0, y, W, y + 1, tile)
        for y0 in range(4, rim, 16):
            off = 8 if (y0 // 16) % 2 else 0
            for x in range(off, W, 16):
                rect(f, x, y0, x + 1, min(y0 + 16, rim), tile)
        # tap
        rect(f, 96, 54, 124, 60, C("#a0aabe"))
        rect(f, 96, 54, 100, 70, C("#a0aabe"))
        rect(f, 95, 70, 101, 72, C("#c8d2e6"))
        rect(f, 112, 46, 120, 50, C("#c8d2e6"))
        rect(f, 115, 50, 117, 54, C("#a0aabe"))
        rim_c, side_c = C("#dce6f0"), C("#a0b4d2")
        rect(f, tub_l - 2, rim, tub_r + 2, rim + 4, rim_c)
        # water
        sy = surface(xs, p)
        for x in range(tub_l + 3, tub_r - 3):
            s = int(round(sy[x]))
            rect(f, x, s, x + 1, bottom - 3, C("#1e5ab4"))
            rect(f, x, s, x + 1, s + 1, C("#5ab4ff"))
        for x, y in ((24, 150), (60, 162), (96, 148), (40, 170), (84, 172)):
            rect(f, x, y, x + 6, y + 1, C("#3c82d2"))
        # drip every 2 s
        u = cyc(p, 4)
        dx = 98
        if u < 0.3:
            rect(f, dx - 1, 72, dx + 1, 72 + int(u / 0.3 * 3) + 1, C("#78c8ff"))
        elif u < 0.5:
            yy = 74 + ((u - 0.3) / 0.2) ** 2 * (surface(dx, p) - 76)
            rect(f, dx - 1, yy, dx + 1, yy + 3, C("#78c8ff"))
        elif u < 0.85:
            k = (u - 0.5) / 0.35
            ring(f, dx, surface(dx, p) + 1, 2 + 12 * k, 1 + 3 * k, C("#78c8ff") if k < 0.5 else C("#3c8cdc"))
            if k < 0.25:
                put(f, [dx - 3, dx + 3, dx], [surface(dx, p) - 3 - 4 * k * 4, surface(dx, p) - 3 - 4 * k * 4,
                                             surface(dx, p) - 5 - 6 * k * 4], C("#78c8ff"))
        # duck (bobs with the water, gently rocking)
        bx = 48 + 6 * np.sin(TAU * p)
        s = surface(bx + 16, p)
        sprite(f, DUCK, DPAL, int(round(bx)), int(round(s - 22)), scale=2)
        # foam riding the surface, drawn over the duck's waterline
        for x0, rr in foam:
            x = x0 + 3 * np.sin(TAU * (p + x0 / 50))
            yy = surface(x, p)
            disc(f, x, yy, rr, C("#dce6f5"))
            disc(f, x - rr * 0.3, yy - rr * 0.3, max(rr * 0.45, 1), C("#ffffff"))
            disc(f, x + rr * 0.4, yy + rr * 0.3, max(rr * 0.35, 0.8), C("#a0b4d2"))
        # floating bubbles
        for x0, ph, k, rr in bubbles:
            u = cyc(p, k, ph)
            x = x0 + 5 * np.sin(TAU * (u * 2 + ph))
            y = surface(x0, p) - 4 - u * 100
            if u < 0.88:
                ring(f, x, y, rr, rr, C("#a0dcff"))
                put(f, x - rr * 0.4, y - rr * 0.4, C("#ffffff"))
            elif u < 0.95:
                for a in range(6):
                    ang = TAU * a / 6
                    put(f, x + np.cos(ang) * (rr + 2), y + np.sin(ang) * (rr + 2), C("#a0dcff"))
        # tub
        rect(f, tub_l, rim + 4, tub_l + 3, bottom - 4, side_c)
        rect(f, tub_r - 3, rim + 4, tub_r, bottom - 4, side_c)
        rect(f, tub_l + 3, bottom - 3, tub_r - 3, bottom, side_c)
        disc(f, tub_l + 4, bottom - 4, 4, side_c)
        disc(f, tub_r - 4, bottom - 4, 4, side_c)
        rect(f, tub_l + 6, bottom, tub_l + 12, bottom + 6, C("#c8aa46"))
        rect(f, tub_r - 12, bottom, tub_r - 6, bottom + 6, C("#c8aa46"))
        yield f


# ---------------------------------------------------------------------------------------------

DANCER = ["........h.",
          "...hh..h..",
          "..hhhhh...",
          "..hhhh....",
          "...hh.....",
          "..hhhh....",
          ".h.hhh....",
          "h..hhh....",
          "...hhh....",
          "...hhhh...",
          "..hh.hh...",
          "..h...h...",
          ".hh....h..",
          ".h.....hh."]


@piece("disco-floor", "Disco Floor", "fun", seconds=8,
       blurb="Mirror ball, flying light spots and a beat-synced dance floor with a tiny dancer.")
def disco_floor(n):
    beats = 16
    r = rng("disco-floor")
    ys = np.array([100, 107, 115, 125, 138, 154, 173, 192])
    vy = 40.0
    spread = 22 * (YY - vy) / (192 - vy)
    floor = YY >= ys[0]
    row = np.clip(np.searchsorted(ys, YY, side="right") - 1, 0, len(ys) - 2)
    u = (XX + 0.5 - 64) / np.maximum(spread, 1e-3) + 4
    colf = np.floor(u).astype(int)
    gap_x = np.abs(u - np.round(u)) * spread < 0.6
    gap_y = np.isin(YY.astype(int), ys)
    tile_ok = floor & ~gap_x & ~gap_y
    rnd = r.random((beats, 8, 12))
    spots = [(float(r.uniform(20, 62)), r.random(), int(r.integers(0, 6))) for _ in range(28)]
    # mirror ball facets
    bx, by, br = 64, 30, 16
    dx, dy = XX + 0.5 - bx, YY + 0.5 - by
    inball = dx * dx + dy * dy <= br * br
    lon = np.arcsin(np.clip(dx / np.sqrt(np.maximum(br * br - dy * dy, 1e-3)), -1, 1)) / np.pi
    lat = np.arcsin(np.clip(dy / br, -1, 1)) / np.pi
    greys = np.array([C("#6e788c"), C("#a0aabe"), C("#d2dcf0"), C("#8c96aa")])
    ftab = r.integers(0, 4, (12, 40))

    for p in phases(n):
        f = canvas()
        beat = int(p * beats) % beats
        bu = (p * beats) % 1.0
        pat = (beat // 4) % 4
        # floor
        tt = rnd[beat]
        rr_, cc_ = row, colf
        if pat == 0:
            lit = (rr_ + cc_ + beat) % 2 == 0
        elif pat == 1:
            lit = ((cc_ + rr_ - beat) % 4) == 0
        elif pat == 2:
            dist = np.abs(cc_ - 3.5) + np.abs(rr_ - 3)
            lit = (np.floor(dist) % 4) == (beat % 4)
        else:
            lit = tt[np.clip(rr_, 0, 7), np.clip(cc_, 0, 11)] < 0.35
        pal = np.array(NEON[:6])
        cidx = (cc_ + rr_ * 2 + beat * (1 if pat != 1 else 0) + pat) % 6
        m = tile_ok & lit & (bu < 0.85 or pat != 3)
        f[m] = pal[cidx[m]]
        # light spots wheeling round the room
        rot = p
        for rad, ph, ci in spots:
            a = TAU * (rot + ph)
            sx = 64 + rad * 1.6 * np.cos(a)
            sy = 30 + rad * np.sin(a) * 0.9 + 22
            if 0 <= sy < 96:
                rect(f, sx, sy, sx + 2, sy + 2, NEON[ci])
        # ball
        rect(f, 63, 0, 64, by - br, C("#787890"))
        fi = (np.floor((lon + 2 * p) * 20).astype(int)) % 40
        fj = (np.floor((lat + 0.5) * 12).astype(int)) % 12
        fac = greys[ftab[fj, fi]]
        edge = (((lon + 2 * p) * 20) % 1 < 0.2) | (((lat + 0.5) * 12) % 1 < 0.2)
        f[inball] = np.where(edge[inball, None], C("#3c4150") * 0 + C("#505a6e"), fac[inball])
        for k in range(3):
            gx = bx + int(rnd[beat, k, 0] * 20 - 10)
            gy = by + int(rnd[beat, k, 1] * 20 - 10)
            if bu < 0.5 and (gx - bx) ** 2 + (gy - by) ** 2 < 150:
                put(f, [gx, gx - 1, gx + 1, gx, gx], [gy, gy, gy, gy - 1, gy + 1], C("#ffffff"))
        # dancer, a new pose every beat
        flip = beat % 2 == 1
        hop = 1 if bu < 0.25 else 0
        sx, sy = 49, 132 - hop * 3
        for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            sprite(f, DANCER, {"h": C("#000000")}, sx + ox, sy + oy, flip=flip, scale=3)
        sprite(f, DANCER, {"h": C("#ffffff")}, sx, sy, flip=flip, scale=3)
        yield f


# ---------------------------------------------------------------------------------------------

@piece("pizza-spin", "Pizza Spin", "fun", seconds=12,
       blurb="A whole pepperoni pizza turning slowly under a flickering neon PIZZA sign.")
def pizza_spin(n):
    r = rng("pizza-spin")
    cx, cy, R = 64, 82, 40
    dx, dy = XX + 0.5 - cx, YY + 0.5 - cy
    inside = dx * dx + dy * dy <= (R + 0.5) ** 2
    peps = []
    while len(peps) < 9:
        a, d = r.uniform(0, TAU), np.sqrt(r.uniform(0.02, 1)) * (R - 13)
        x, y = d * np.cos(a), d * np.sin(a)
        if all((x - px) ** 2 + (y - py) ** 2 > 12 ** 2 for px, py in peps):
            peps.append((x, y))
    spots = [(r.uniform(-3, 3), r.uniform(-3, 3)) for _ in range(9)]
    basil = [(r.uniform(0, TAU), r.uniform(8, R - 10), r.uniform(0, np.pi)) for _ in range(6)]
    olives = [(r.uniform(0, TAU), r.uniform(6, R - 10)) for _ in range(7)]
    bubbles = [(r.uniform(0, TAU), r.uniform(0, R - 8), r.uniform(1.2, 2.5)) for _ in range(14)]
    st = stars("pizza-spin/stars", 20, 0, 192)
    colours = {"crust": C("#c8783c"), "ridge": C("#e6a050"), "sauce": C("#c83214"), "cheese": C("#f0b43c"),
               "melt": C("#ffd264"), "pep": C("#c8281e"), "pepd": C("#8c1e14"), "basil": C("#3cb43c"),
               "olive": C("#6e3c6e"), "cut": C("#b47828")}

    def shade(ox, oy, rot):
        """Pizza colours at world offset (ox, oy) from the centre, pizza turned by rot."""
        ca, sa = np.cos(rot), np.sin(rot)
        lx, ly = ox * ca + oy * sa, -ox * sa + oy * ca
        rr = np.sqrt(lx * lx + ly * ly)
        th = np.arctan2(ly, lx)
        img = np.zeros((H, W, 3), np.float32)
        img[:] = colours["cheese"]
        for a, d, s_ in bubbles:
            img[(lx - d * np.cos(a)) ** 2 + (ly - d * np.sin(a)) ** 2 <= s_ * s_] = colours["melt"]
        for x, y in peps:
            img[(lx - x) ** 2 + (ly - y) ** 2 <= 5.5 ** 2] = colours["pep"]
            for sx_, sy_ in spots[:3]:
                img[(lx - x - sx_) ** 2 + (ly - y - sy_) ** 2 <= 0.9] = colours["pepd"]
        for a, d in olives:
            q = np.sqrt((lx - d * np.cos(a)) ** 2 + (ly - d * np.sin(a)) ** 2)
            img[(q <= 2.4) & (q >= 1.0)] = colours["olive"]
        for a, d, o in basil:
            px, py = lx - d * np.cos(a), ly - d * np.sin(a)
            u, v = px * np.cos(o) + py * np.sin(o), -px * np.sin(o) + py * np.cos(o)
            img[(u / 3.5) ** 2 + (v / 1.6) ** 2 <= 1] = colours["basil"]
        img[(np.abs(rr * np.sin(th % (TAU / 8))) < 0.55) & (rr < R - 5)] = colours["cut"]
        img[(rr > R - 7) & (rr <= R - 5.5)] = colours["sauce"]
        img[rr > R - 5.5] = colours["crust"]
        img[(rr > R - 4.5) & (rr <= R - 2.5)] = colours["ridge"]
        sl = (th >= -np.pi / 2) & (th < -np.pi / 4) & (rr <= R + 0.5)  # the slice that gets lifted
        return img, rr <= R + 0.5, sl

    L = 12.0
    bis = 5 * np.pi / 8  # world direction of the lifted slice while the pizza pauses
    for p in phases(n):
        t = p * L
        f = canvas()
        draw_stars(f, st, p)
        g = t / 8 if t < 4 else 0.5 if t < 8 else (t - 4) / 8   # one turn, pausing for a slice
        rot = TAU * g
        d = 12 * (ease((t - 4.3) / 1.0) - ease((t - 6.7) / 1.0))
        ring(f, cx, cy, R + 3, R + 3, C("#5a6ea0"), width=1)
        img, inn, sl = shade(dx, dy, rot)
        m = inn & ~sl if d > 0.5 else inn
        f[m] = img[m]
        if d > 0.5:
            ox, oy = d * np.cos(bis), d * np.sin(bis)
            img2, inn2, sl2 = shade(dx - ox, dy - oy, rot)
            f[sl2] = img2[sl2]
            for k, a in enumerate((bis - 0.36, bis + 0.36)):  # cheese strings
                r0 = R * (0.45 + 0.25 * k)
                x0, y0 = cx + r0 * np.cos(a), cy + r0 * np.sin(a)
                line(f, x0, y0, x0 + ox * 0.9, y0 + oy * 0.9, C("#ffd264"))
        # steam
        for k, x0 in enumerate((48, 64, 80)):
            for y in range(cy - R - 8, cy - R - 32, -1):
                h = (cy - R - 8 - y) / 24
                g = np.sin(TAU * (h * 1.2 - 2 * p + k * 0.33))
                if g > -0.2:
                    x = x0 + 3 * h * np.sin(TAU * (h * 1.5 - 2 * p + k * 0.4))
                    put(f, x, y, C("#b4b4c8") if h < 0.5 else C("#82829b"))
        # neon sign
        flick = 0.30 <= p < 0.32 or 0.34 <= p < 0.35 or 0.71 <= p < 0.72
        sxp = (W - text_width("PIZZA", 3)) // 2
        rect(f, sxp - 5, 140, sxp + text_width("PIZZA", 3) + 5, 141, C("#3cdcff"))
        rect(f, sxp - 5, 162, sxp + text_width("PIZZA", 3) + 5, 163, C("#3cdcff"))
        for j, chh in enumerate("PIZZA"):
            if j == 3 and flick:
                continue
            text(f, chh, sxp + j * 12, 146, C("#ff3c78"), scale=3)
        if cyc(p, 6) < 0.7:
            msg = "OPEN LATE"
            text(f, msg, (W - text_width(msg)) // 2, 172, C("#ffdc3c"))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("coffee-cup", "Cozy Coffee", "fun", seconds=8,
       blurb="A red mug of latte with a heart, steam curling up past a snowy window.")
def coffee_cup(n):
    r = rng("coffee-cup")
    snow = [(float(r.uniform(78, 120)), r.random(), int(r.integers(1, 3)), r.random()) for _ in range(22)]
    mug, rimc = C("#d23c50"), C("#ff7882")
    for p in phases(n):
        f = canvas()
        # window
        rect(f, 74, 12, 124, 72, C("#5a3c28"))
        rect(f, 77, 15, 121, 69, C("#000000"))
        for x0, ph, k, w in snow:
            u = cyc(p, k, ph)
            x = x0 + 2 * np.sin(TAU * (u * 2 + w))
            y = 15 + u * 54
            if 77 <= x < 121:
                put(f, x, y, C("#dce6ff") if w < 0.5 else C("#a0aadc"))
        rect(f, 77, 64, 121, 69, C("#c8d2eb"))
        rect(f, 98, 15, 100, 69, C("#5a3c28"))
        rect(f, 77, 41, 121, 43, C("#5a3c28"))
        rect(f, 72, 72, 126, 75, C("#7a5032"))
        # table
        rect(f, 0, 168, W, H, C("#8c5a32"))
        for y in (174, 181, 188):
            rect(f, 0, y, W, y + 1, C("#6e4628"))
        rect(f, 0, 168, W, 169, C("#b4783c"))
        # saucer
        ellipse(f, 58, 164, 40, 6, C("#a0b4d2"))
        ellipse(f, 58, 163, 30, 3.5, C("#788caf"))
        # mug
        cxm = 56
        ring(f, 85, 134, 11, 13, mug, width=5)
        rect(f, 30, 110, 82, 156, mug)
        ellipse(f, cxm, 156, 26, 7, mug)
        rect(f, 33, 116, 35, 152, C("#ff6e7d"))
        # heart on mug
        disc(f, 52, 133, 3.2, C("#ffb4c8"))
        disc(f, 58, 133, 3.2, C("#ffb4c8"))
        poly(f, [(48.8, 134), (61.2, 134), (55, 142)], C("#ffb4c8"))
        # top
        ellipse(f, cxm, 110, 26, 5, rimc)
        ellipse(f, cxm, 110, 23, 3.5, C("#965028"))
        disc(f, 53, 109, 1.8, C("#e6be8c"))
        disc(f, 58, 109, 1.8, C("#e6be8c"))
        poly(f, [(51, 109.5), (60, 109.5), (55.5, 113)], C("#e6be8c"))
        # cookie
        disc(f, 108, 176, 9, C("#d2965a"))
        for ox, oy in ((-3, -3), (3, -1), (-1, 3), (4, 4), (-5, 2)):
            rect(f, 108 + ox, 176 + oy, 110 + ox, 177 + oy, C("#6e3c1e"))
        # steam
        for k, (x0, ph) in enumerate(((46, 0.0), (56, 0.37), (66, 0.71))):
            for y in range(102, 24, -1):
                h = (102 - y) / 78
                gate = np.sin(TAU * (h * 2.2 - 2 * p + ph))
                if gate < -0.25:
                    continue
                amp = 2 + h * 9
                x = x0 + amp * np.sin(TAU * (h * 1.6 - 1 * p + ph))
                col = C("#d2d2e1") if h < 0.3 else C("#a0a0b9") if h < 0.65 else C("#78789b")
                w = 2 if h < 0.5 else 1
                rect(f, x, y, x + w, y + 1, col)
        yield f


# ---------------------------------------------------------------------------------------------

ROCKET = ["....r....",
          "...rrr...",
          "...rrr...",
          "..wwwww..",
          "..wwwww..",
          "..wwbww..",
          "..wbbbw..",
          "..wwbww..",
          "..wwwww..",
          "..wwwww..",
          "..rrrrr..",
          "..wwwww..",
          "..wwwww..",
          "..wwwww..",
          ".rwwwwwr.",
          "rrwwwwwrr",
          "rr.ggg.rr"]
RPAL = {"r": C("#e63232"), "w": C("#dcdce6"), "b": C("#3ca0ff"), "g": C("#8c8ca0")}


@piece("liftoff", "Liftoff", "fun", seconds=10,
       blurb="Countdown, ignition, liftoff; then the empty launcher rolls away and the next rocket rolls in.")
def liftoff(n):
    L = 10.0
    st = stars("liftoff/stars", 50, 0, 150)
    r = rng("liftoff")
    puffs = [(float(r.uniform(2.5, 4.2)), float(r.choice([-1, 1]) * r.uniform(10, 60)),
              float(r.uniform(5, 11)), float(r.uniform(-4, 6))) for _ in range(30)]
    ground = 182
    t_ign, t_lift = 2.4, 2.9

    def assembly(f, ox, arm, with_rocket, rise, t):
        cx = 64 + ox
        # tower
        tx0, tx1 = cx - 27, cx - 18
        for x in (tx0, tx1):
            rect(f, x, 96, x + 1, 170, C("#c84632"))
        for y in range(96, 170, 8):
            line(f, tx0, y, tx1, y + 8, C("#c84632"))
            rect(f, tx0, y, tx1 + 1, y + 1, C("#c84632"))
        rect(f, tx0 - 2, 92, tx1 + 3, 96, C("#c84632"))
        rect(f, cx - 23, 84, cx - 22, 92, C("#a0a0b4"))
        if cyc(t / L, 10) < 0.3:
            put(f, cx - 23, 83, C("#ff2828"))
        if arm:
            rect(f, tx1, 130, cx - 7, 132, C("#a0a0b4"))
            rect(f, cx - 9, 128, cx - 7, 134, C("#a0a0b4"))
        # platform and treads
        rect(f, cx - 26, 170, cx + 26, 176, C("#787896"))
        rect(f, cx - 6, 170, cx + 6, 172, C("#000000"))
        rect(f, cx - 24, 176, cx + 24, ground, C("#50506e"))
        for x in range(-24, 24, 4):
            xx = cx + x + int(ox) % 4
            if cx - 24 <= xx < cx + 23:
                rect(f, xx, 177, xx + 2, ground - 1, C("#8c8ca0"))
        if with_rocket:
            sprite(f, ROCKET, RPAL, int(round(cx)) - 13, 119 - int(round(rise)), scale=3)

    for p in phases(n):
        t = p * L
        f = canvas()
        draw_stars(f, st, p)
        disc(f, 104, 26, 7, C("#f5e6a0"))
        disc(f, 101, 24, 6, C("#000000"))
        rect(f, 0, ground, W, H, C("#46465f"))
        rect(f, 0, ground, W, ground + 1, C("#6e6e8c"))
        rise = 0.0 if t < t_lift else 24 * (t - t_lift) ** 2
        # flame
        if t_ign <= t and rise < 260:
            fy = 170 - rise
            flick = cyc(p, 50) < 0.5
            ln = 8 + (4 if flick else 0) + min(rise * 0.15, 18)
            for w, frac, col in ((8, 1.0, "#ff3c1e"), (6, 0.8, "#ff8c1e"), (4, 0.55, "#ffd23c"), (2, 0.3, "#ffffa0")):
                poly(f, [(64.5 - w, fy), (64.5 + w, fy), (64.5, fy + ln * frac + w)], C(col))
        # smoke
        for ts, vx, rmax, vy in puffs:
            if t < ts:
                continue
            a = t - ts
            x = 64 + vx * (1 - np.exp(-a * 1.2))
            y = 176 - abs(vy) * 0.5 - a * 1.5
            rad = rmax * min(1, a * 1.5) * (1 - ease((t - 6.0) / 1.6))
            if rad >= 1:
                disc(f, x, y, rad, C("#8c8c9b"))
                disc(f, x - rad * 0.2, y - rad * 0.25, rad * 0.75, C("#b4b4c3"))
        # old launcher leaves, the next arrives
        ox_old = -110 * ease((t - 5.6) / 1.8)
        assembly(f, ox_old, t < t_ign, True, rise, t)
        ox_new = 110 * (1 - ease((t - 7.0) / 2.6))
        if t > 6.9:
            assembly(f, ox_new, True, True, 0, t)
        # vent puffs before ignition
        if t < t_ign:
            u = cyc(p, 5)
            if u < 0.5:
                disc(f, 81 + u * 10, 150 - u * 6, 2 + u * 4, C("#a0a0b4"))
        # countdown
        if t < t_ign:
            d = str(3 - int(t / 0.8))
            text(f, d, (W - text_width(d, 6)) // 2, 30, C("#ffd23c"), scale=6)
        elif t < 5.0 and int(t * 5) % 2 == 0:
            msg = "LIFTOFF!"
            text(f, msg, (W - text_width(msg, 2)) // 2, 40, C("#ffd23c"), scale=2)
        yield f


# ---------------------------------------------------------------------------------------------

AXO = [["d.....ppp.....d",
        ".d..ppppppp..d.",
        "ddddpkpppkpdddd",
        "...dpppppppd...",
        "dd.dpppmpppd.dd",
        "....ppppppp....",
        ".....ppppp.....",
        "..ff.ppppp.ff..",
        "...ffpppppff...",
        ".....ppppp.....",
        ".....ppppp.....",
        ".....ppppp.....",
        "...ffpppppff...",
        "..ff.ppppp.ff..",
        "......ppp......",
        "......ppp......",
        ".......p.......",
        ".......p......."],
       ["d.....ppp.....d",
        ".d..ppppppp..d.",
        "ddddpkpppkpdddd",
        "...dpppppppd...",
        "dd.dpppmpppd.dd",
        "....ppppppp....",
        ".....ppppp.....",
        "...ffpppppff...",
        "..ff.ppppp.ff..",
        ".....ppppp.....",
        ".....ppppp.....",
        ".....ppppp.....",
        "..ff.ppppp.ff..",
        "...ffpppppff...",
        "......ppp......",
        ".....ppp.......",
        ".....pp........",
        "....p.........."]]
AXPAL = {"p": C("#ff8cc8"), "d": C("#ff3c8c"), "k": C("#000000"), "m": C("#c8326e"), "f": C("#ffb4dc")}
RAINBOW = [C(c) for c in ("#ff3c3c", "#ff9628", "#ffe63c", "#50e650", "#3c8cff", "#a050ff")]


@piece("rainbow-axolotl", "Rainbow Axolotl", "fun", seconds=4,
       blurb="A pink axolotl paddles up through space on a wiggling rainbow trail.")
def rainbow_axolotl(n):
    r = rng("rainbow-axolotl")
    sparks = [(int(r.integers(4, W - 4)), r.random(), int(r.integers(1, 3)), int(r.integers(0, 4)))
              for _ in range(16)]
    dust = [(int(r.integers(0, W)), r.random(), int(r.choice([2, 3]))) for _ in range(40)]
    for p in phases(n):
        f = canvas()
        for x, ph, k in dust:
            y = int(cyc(p, k, ph) * H)
            rect(f, x, y, x + 1, y + 2 + k, C("#6e78b4") if k == 2 else C("#a0aadc"))
        for x, ph, k, typ in sparks:
            u = cyc(p, k, ph)
            y = int(u * (H + 10)) - 5
            stage = int(cyc(p, 8, ph) * 4)
            col = C("#ffffff") if stage < 2 else C("#ffe63c")
            put(f, x, y, col)
            if stage in (1, 2):
                put(f, [x - 2, x + 2, x, x], [y, y, y - 2, y + 2], col)
            if stage == 1:
                put(f, [x - 1, x + 1, x, x], [y, y, y - 1, y + 1], col)
        bob = int(round(2 * np.sin(TAU * 2 * p)))
        ax, ay = 41, 36 + bob
        sw = 4  # stripe width
        x0 = 64 - 3 * sw
        tail_y = ay + 13 * 3
        scroll = cyc(p, 4) * 16
        for y in range(tail_y, H):
            seg = int(np.floor((y - tail_y + scroll) / 8))
            off = 2 if seg % 2 else 0
            for j, c in enumerate(RAINBOW):
                rect(f, x0 + j * sw + off, y, x0 + j * sw + off + sw, y + 1, c)
        frame_i = 0 if cyc(p, 4) < 0.5 else 1
        sprite(f, AXO[frame_i], AXPAL, ax, ay, scale=3)
        put(f, [ax + 15, ax + 27], [ay + 6, ay + 6], C("#ffffff"))
        rect(f, ax + 12, ay + 12, ax + 15, ay + 14, C("#ff5aa0"))
        rect(f, ax + 30, ay + 12, ax + 33, ay + 14, C("#ff5aa0"))
        yield f
