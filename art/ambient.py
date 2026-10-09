"""Generative / ambient pieces: smooth colour fields, so they are encoded as lossless RGB
H.264 (more than 256 colours). Loops are seamless by construction: noise lattices wrap in
time, every orbit/oscillation runs whole cycles per loop, and Life fades through black."""
import numpy as np

from lib import (H, TAU, W, XX, YY, C, Noise, canvas, fbm, glow, hsv, octaves, piece, put, ramp, rng,
                 soft_disc, smoothstep)


def phases(n):
    return (i / n for i in range(n))


def splat(f, x, y, col, a=1.0):
    """Additive anti-aliased point (bilinear), for stars and streaks."""
    x0, y0 = int(np.floor(x)), int(np.floor(y))
    fx, fy = x - x0, y - y0
    for dx, dy, w in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)), (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
        xx, yy = x0 + dx, y0 + dy
        if 0 <= xx < W and 0 <= yy < H:
            f[yy, xx] += col * w * a


def stars(name, count, y_max=H, lo=90, hi=220):
    r = rng(name)
    return r.uniform(0, W, count), r.uniform(0, y_max, count), r.uniform(lo, hi, count), r.integers(1, 4, count), r.random(count)


def draw_stars(f, st, p, mask=None):
    xs, ys, b, k, ph = st
    tw = b * (0.75 + 0.25 * np.sin(TAU * (k * p + ph)))
    xi, yi = xs.astype(int), ys.astype(int)
    ok = np.ones(len(xs), bool) if mask is None else mask[yi, xi]
    f[yi[ok], xi[ok]] = np.maximum(f[yi[ok], xi[ok]], tw[ok, None])


# ---------------------------------------------------------------------------------------------

@piece("lava-lamp", "Lava Lamp", "ambient", seconds=10, fps=30,
       blurb="Molten blobs rising, merging and sinking in hot magenta and orange.")
def lava_lamp(n):
    r = rng("lava-lamp")
    blobs = [(r.uniform(28, 100), r.uniform(10, 26), r.integers(1, 3), r.random(),
              r.uniform(60, 80), r.integers(1, 2, endpoint=True), r.random(), r.uniform(11, 19)) for _ in range(8)]
    for p in phases(n):
        field = np.zeros((H, W), np.float32)
        for cx, ax, kx, phx, ay, ky, phy, rad in blobs:
            x = cx + ax * np.sin(TAU * (kx * p + phx))
            y = 96 + ay * np.sin(TAU * (ky * p + phy))
            field += rad * rad / ((XX - x) ** 2 + (YY - y) ** 2 + 30)
        # warmer near the bottom heat source
        heat = 0.35 + 0.65 * (YY / H)
        inside = smoothstep(0.95, 1.1, field)
        core = smoothstep(1.4, 3.5, field)
        rim = smoothstep(0.55, 0.95, field) * (1 - inside)
        body = ramp([(0, C("#c8146e")), (0.5, C("#ff3c50")), (1, C("#ff8c1e"))], heat)
        hot = ramp([(0, C("#ff6450")), (1, C("#ffd25a"))], heat)
        f = body * inside[..., None]
        f = f * (1 - core[..., None]) + hot * (core * inside)[..., None]
        f += C("#6e0a6e") * rim[..., None] * 0.9
        base = np.clip((YY - 150) / 42, 0, 1) ** 2  # glow from the lamp base
        f += C("#ff3c14") * (base * 0.55 * (1 - inside))[..., None]
        yield f


@piece("aurora", "Aurora", "ambient", seconds=10, fps=30,
       blurb="Northern lights rippling over snowy peaks, mirrored in a still lake.")
def aurora(n):
    curt = octaves("aurora/curtain", (4, 6), 3)      # (t, x) lattices
    rays = Noise("aurora/rays", (8, 64))
    st = stars("aurora/stars", 110, 140, 80, 200)
    lake = 158
    xs = np.arange(W, dtype=np.float32)
    rr = rng("aurora/mtn")
    ridge = 150 - 26 * np.abs(np.sin(xs / 29 + 0.7)) - 10 * np.abs(np.sin(xs / 11 + rr.random() * 6)) \
        - 3 * np.abs(np.sin(xs / 4.3))
    sky_mask = YY < ridge[None, :]
    snow = (~sky_mask) & (YY < ridge[None, :] + 2 + 5 * (ridge[None, :] < 124)) & (YY < lake)
    for p in phases(n):
        f = canvas()
        draw_stars(f, st, p, mask=sky_mask)
        for k, (yb, amp, decay) in enumerate([(104, 22, 30), (74, 16, 22)]):
            base = yb + amp * (fbm(curt, p * 4 + k * 1.7, xs / W * 6 + k * 2.3) - 0.5) * 2
            ray = 0.4 + 0.6 * rays(p * 8 + k * 3, xs / 2.0 + k * 20) ** 1.5
            up = base[None, :] - YY
            inten = np.where(up >= 0, np.exp(-up / decay), np.exp(up / 5)) * ray[None, :]
            inten *= 0.75 + 0.25 * np.sin(TAU * (p + k * 0.4) + xs[None, :] / 20)
            col = ramp([(0, C("#28ff8c")), (0.35, C("#14e6aa")), (0.7, C("#4664ff")), (1, C("#b43cff"))],
                       np.clip(up / 50, 0, 1))
            f += col * (inten * (1.0 - 0.25 * k))[..., None]
        f = np.where(sky_mask[..., None], f, 0)
        f[snow] = C("#64788c")
        for y in range(lake, H):  # mirrored into the lake, darker, broken by slow ripples
            src = 2 * lake - y - 2
            shift = int(round(1.5 * np.sin(y * 0.8 + TAU * 2 * p)))
            row = np.roll(f[src], shift, axis=0) * 0.5
            f[y] = row * (0.55 if (y + int(4 * p * 4)) % 4 == 0 else 1.0)
        yield np.clip(f, 0, 255)


@piece("ink", "Flowing Ink", "ambient", seconds=10, fps=30,
       blurb="Domain-warped noise like marbled ink: indigo, magenta, gold and teal.")
def ink(n):
    a = octaves("ink/a", (4, 6, 4), 3)
    b = octaves("ink/b", (4, 6, 4), 3)
    c = octaves("ink/c", (4, 6, 4), 3)
    ys, xs = YY / H * 3, XX / W * 2
    pal = [(0.00, C("#000000")), (0.18, C("#000000")), (0.30, C("#28148c")), (0.40, C("#8c1eb4")),
           (0.48, C("#ff3c96")), (0.54, C("#ffb43c")), (0.58, C("#000000")), (0.70, C("#000000")),
           (0.78, C("#00a0b4")), (0.86, C("#50f0d2")), (0.92, C("#000000")), (1.0, C("#000000"))]
    for p in phases(n):
        t = p * 4
        q1 = fbm(a, t, ys, xs)
        q2 = fbm(b, t + 1.33, ys + 1.7, xs + 3.1)
        v = fbm(c, t + 2.66, ys + 1.6 * q1, xs + 1.6 * q2)
        v = (v - 0.25) / 0.5
        yield ramp(pal, (v * 1.6 + 0.2 * q1) % 1.0)


@piece("life", "Life", "ambient", seconds=10, fps=25,
       blurb="Conway's Game of Life from a random soup, with colour trails; fades out and re-seeds.")
def life(n):
    gw, gh = 64, 96
    g = rng("life/soup").random((gh, gw)) < 0.32
    age = np.zeros((gh, gw), np.float32)     # generations alive
    trail = np.zeros((gh, gw), np.float32)   # 1 at death, decays
    hue_at = np.zeros((gh, gw), np.float32)
    fade_from = int(n * 0.86)
    for i in range(n):
        alive_col = hsv(0.13 - np.clip(age / 40, 0, 0.6) * 0.5, 0.65 + 0.35 * np.clip(age / 10, 0, 1), 1.0)
        trail_col = hsv(hue_at, 1.0, 1.0) * (trail ** 1.3)[..., None] * 0.85
        img = np.where(g[..., None], alive_col, trail_col)
        # fade in from black and back out, so the loop restarts invisibly
        env = min(smoothstep(0, 1, i / 12), 1 - smoothstep(0, 1, (i - fade_from) / (n - 1 - fade_from)))
        img = img * env
        yield np.repeat(np.repeat(img, 2, 0), 2, 1)
        nb = sum(np.roll(np.roll(g, dy, 0), dx, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)
        new = (nb == 3) | (g & (nb == 2))
        died = g & ~new
        trail = np.where(died, 1.0, trail * 0.82)
        hue_at = np.where(died, 0.62 + 0.3 * np.clip(age / 30, 0, 1), hue_at)
        age = np.where(new, np.where(g, age + 1, 0), 0)
        g = new


@piece("breathing", "Breathing Light", "ambient", seconds=10, fps=30,
       blurb="Three soft colour clouds drifting and breathing slowly; calm and dark at the edges.")
def breathing(n):
    orbs = [((0.95, "#ff1e78"), 40, 58, 18, 22, 0.0), ((0.55, "#1e78ff"), 86, 110, 22, 26, 0.33),
            ((0.08, "#ff8c1e"), 58, 150, 20, 18, 0.66)]
    for p in phases(n):
        f = canvas()
        breath = 0.5 - 0.5 * np.cos(TAU * p)
        for (h, col), cx, cy, ax, ay, ph in orbs:
            x = cx + ax * np.sin(TAU * (p + ph))
            y = cy + ay * np.sin(TAU * (p + ph) * 1 + 1.3)
            rad = 62 + 14 * breath
            f += C(col) * glow(x, y, rad, 1.6)[..., None] * (0.55 + 0.45 * breath)
        yield np.clip(f, 0, 255)


@piece("starfield", "Warp Starfield", "ambient", seconds=5, fps=30,
       blurb="Hyperspace: stars streak outward from the centre.")
def starfield(n):
    r = rng("warp")
    N = 200
    ang = r.uniform(0, TAU, N)
    spread = r.uniform(0.3, 1.0, N)
    k = r.integers(1, 3, N)
    off = r.random(N)
    tint = np.array([C("#c8dcff"), C("#ffffff"), C("#a0c8ff"), C("#ffd2a0"), C("#d2b4ff")])[r.integers(0, 5, N)]
    cx, cy = 64, 96
    for p in phases(n):
        f = canvas()
        f += C("#140a32") * glow(cx, cy, 40, 2.0)[..., None]
        for i in range(N):
            c = (k[i] * p + off[i]) % 1.0
            z = 1.0 - c * 0.97            # z: 1 (far) -> 0.03 (near)
            z0 = min(1.0, z + 0.09 * k[i] * z)
            for s in np.linspace(0, 1, 10):  # streak from previous depth to now
                zz = z0 + (z - z0) * s
                rad = spread[i] * 14 / zz
                x, y = cx + rad * np.cos(ang[i]), cy + rad * np.sin(ang[i]) * 1.25
                b = np.clip((1 - zz) * 1.3, 0, 1) ** 2.2 * (0.2 + 0.8 * s)
                splat(f, x, y, tint[i], b * 0.9)
        yield np.clip(f, 0, 255)


@piece("orbits", "Orrery", "ambient", seconds=10, fps=30,
       blurb="Planets on tilted orbits around a glowing sun; a moon and a ringed giant.")
def orbits(n):
    cx, cy = 64, 96
    st = stars("orbits/stars", 90, H, 70, 170)
    tilt = 0.22
    planets = [  # orbit radius, x squash, revolutions per loop, size, colour, phase
        (26, 0.42, 5, 2.4, "#c8aa8c", 0.1), (44, 0.42, 3, 3.6, "#ff8c3c", 0.6),
        (64, 0.42, 2, 4.2, "#3c8cff", 0.3), (88, 0.42, 1, 6.0, "#e6c878", 0.85)]

    def pos(rad, sq, k, ph, p):
        a = TAU * (k * p + ph)
        x, y = rad * sq * np.cos(a), rad * np.sin(a)
        return cx + x * np.cos(tilt) - y * np.sin(tilt), cy + x * np.sin(tilt) + y * np.cos(tilt), np.cos(a)

    def planet(f, x, y, size, col):
        cov = soft_disc(x, y, size, 0.8)
        lx, ly = cx - x, cy - y
        ln = np.hypot(lx, ly) + 1e-6
        nx, ny = (XX + 0.5 - x) / size, (YY + 0.5 - y) / size
        shade = np.clip(0.25 + 0.95 * (nx * lx + ny * ly) / ln, 0.08, 1.0)
        a = cov[..., None]
        f[:] = f * (1 - a) + C(col) * shade[..., None] * a

    for p in phases(n):
        f = canvas()
        draw_stars(f, st, p)
        for rad, sq, k, size, col, ph in planets:  # dotted orbit paths
            for j in range(int(rad * 2.2)):
                x, y, _ = pos(rad, sq, 0, j / int(rad * 2.2), 0)
                put(f, x, y, C("#3c3c6e"))
        bodies = []
        for rad, sq, k, size, col, ph in planets:
            x, y, depth = pos(rad, sq, k, ph, p)
            bodies.append((depth, x, y, size, col, rad))
        bodies.sort()
        sun_drawn = False
        for depth, x, y, size, col, rad in bodies + [(9, 0, 0, 0, None, 0)]:
            if depth > 0 and not sun_drawn:
                f += C("#ff6414") * glow(cx, cy, 30, 2.2)[..., None] * 0.9
                f += C("#ffb432") * glow(cx, cy, 15, 1.5)[..., None]
                a = soft_disc(cx, cy, 8.5)[..., None]
                f[:] = f * (1 - a) + C("#fff0a0") * a
                sun_drawn = True
            if col is None:
                continue
            if rad == 88:  # ringed giant: back half of the ring, planet, front half
                ring = lambda front: ((((XX - x) / 11) ** 2 + ((YY - y) / 3.2) ** 2 <= 1)
                                      & (((XX - x) / 8.5) ** 2 + ((YY - y) / 2.2) ** 2 > 1)
                                      & ((YY < y) ^ front))
                f[ring(False)] = C("#b49664")
                planet(f, x, y, size, col)
                f[ring(True)] = C("#dcbe8c")
            else:
                planet(f, x, y, size, col)
            if rad == 64:  # a moon around the blue planet
                a = TAU * 8 * p
                planet(f, x + 9 * np.cos(a), y + 4 * np.sin(a), 1.3, "#dcdcdc")
        yield np.clip(f, 0, 255)


@piece("bounce", "Bounce", "ambient", seconds=10, fps=30,
       blurb="A faceted gem bouncing off the edges, new colour every hit; it does hit the corner.")
def bounce(n):
    size = 17
    cols = [C("#ff3cc8"), C("#3cdcff"), C("#ffdc3c"), C("#64ff64")]

    def tri(u):
        u = u % 1.0
        return 1 - abs(1 - 2 * u)

    def state(p):
        x = size + tri(3 * p) * (W - 2 * size)
        y = size + tri(2 * p) * (H - 2 * size)
        ex = np.floor(6 * p + 1e-9)          # wall hits so far this loop
        ey = np.floor(4 * p + 1e-9)
        both = np.floor(2 * p + 1e-9)        # p = 0 and 1/2 hit a corner: count once
        return x, y, int(ex + ey - both) % len(cols)

    def gem(f, x, y, col, a=1.0):
        ax_, ay_ = np.abs(XX + 0.5 - x), np.abs(YY + 0.5 - y)
        d = np.maximum(np.maximum(ax_, ay_), (ax_ + ay_) * 0.75)  # octagon
        body = np.clip(size - d, 0, 1)
        up = (YY + 0.5 - y) < 0
        left = (XX + 0.5 - x) < 0
        shade = np.where(up, np.where(left, 1.0, 0.8), np.where(left, 0.55, 0.38))
        inner = np.clip(size * 0.45 - d, 0, 1)
        c = col * shade[..., None]
        c = c * (1 - inner[..., None]) + np.minimum(col * 1.15 + 40, 255) * inner[..., None]
        f[:] = f * (1 - (body * a)[..., None]) + c * (body * a)[..., None]

    for p in phases(n):
        f = canvas()
        for j in range(6, 0, -1):  # trail
            tx, ty, ci = state((p - j * 0.005) % 1.0)
            gem(f, tx, ty, cols[ci] * 0.5, 0.35 * (1 - j / 7))
        x, y, ci = state(p)
        f += cols[ci] * glow(x, y, size * 2.6, 2.0)[..., None] * 0.35
        gem(f, x, y, cols[ci])
        u = (2 * p) % 1.0  # corner hits at p = 0 and 1/2: a burst of sparks
        for burst in (u, u - 1):
            if -0.06 < burst < 0.1:
                k = (burst + 0.06) / 0.16
                for a in range(10):
                    ang = TAU * a / 10
                    bx = x + np.cos(ang) * (4 + 26 * k)
                    by = y + np.sin(ang) * (4 + 26 * k)
                    splat(f, bx, by, C("#ffffff"), 1 - k)
        yield np.clip(f, 0, 255)


@piece("kaleidoscope", "Kaleidoscope", "ambient", seconds=8, fps=30,
       blurb="Eightfold mirrored noise, slowly turning and blooming outward.")
def kaleidoscope(n):
    tex = octaves("kaleido", (4, 8, 8), 3)
    cx, cy = 64, 96
    r = np.hypot(XX + 0.5 - cx, YY + 0.5 - cy)
    th = np.arctan2(YY + 0.5 - cy, XX + 0.5 - cx)
    seg = TAU / 8
    for p in phases(n):
        a = np.abs(((th + seg * p) % seg) - seg / 2)
        u, v = r * np.cos(a) / 40, r * np.sin(a) / 40
        val = fbm(tex, p * 4, u, v + 0.5)
        hue = (val * 2.2 - r / 140 - p) % 1.0
        level = (val * 6 - r / 30 + p * 2) % 1.0
        bright = smoothstep(0.3, 0.45, level) * (1 - smoothstep(0.62, 0.8, level))
        f = hsv(hue, 0.9, bright)
        yield f
