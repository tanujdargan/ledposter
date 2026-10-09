"""Pixel scenes: hand-built 128x192 pixel art in flat palette colours, so they encode as
lossless GIFs and stay crisp with filter=pixel. Each frame is a pure function of the loop
phase p in [0, 1): every motion runs a whole number of cycles per loop, so loops are seamless."""
import numpy as np

from lib import (H, TAU, W, XX, YY, C, Noise, bands, canvas, disc, mix, piece, put, rect, rng,
                 sprite, text, text_width)


def phases(n):
    return (i / n for i in range(n))


def cyc(p, k, off=0.0):
    """0..1 position of something that runs k whole cycles per loop."""
    return (k * p + off) % 1.0


def wave(p, k, off=0.0):
    return np.sin(TAU * (k * p + off))


def starfield(name, count, y_max, levels=(120, 170, 235), y_min=0):
    r = rng(name)
    xs, ys = r.integers(0, W, count), r.integers(y_min, y_max, count)
    return xs, ys, r.choice(levels, count), r.integers(1, 4, count), r.random(count)


def draw_stars(f, stars, p, tint=(1, 1, 1)):
    """Stars only where the sky is still black; each twinkles a whole number of times per loop."""
    xs, ys, lv, tw, ph = stars
    b = np.where(np.sin(TAU * (tw * p + ph)) > 0.55, lv, np.maximum(lv - 55, 80))
    m = f[ys, xs].max(-1) == 0
    put(f, xs[m], ys[m], np.round(b[m, None] * np.array(tint, np.float32)))


def moon(f, cx, cy, r, col, shade, crescent=None):
    disc(f, cx, cy, r, col)
    for dx, dy, rr in ((-3, -2, 2), (3, 3, 2.5), (-1, 5, 1.5), (4, -4, 1.5)):
        if rr < r / 3:
            disc(f, cx + dx * r / 9, cy + dy * r / 9, rr * r / 9, shade)
    if crescent:
        disc(f, cx + crescent[0], cy + crescent[1], r, C("#000000"))


def pine(f, x, base, h, col, snow=None):
    """Stepped pixel pine tree, apex at base - h."""
    for j in range(h):
        y = base - h + j
        half = int((j % max(h // 4, 3)) * 0.6 + j * 0.28) + 1
        rect(f, x - half, y, x + half + 1, y + 1, col)
        if snow is not None and j % max(h // 4, 3) == 0 and j > 0:
            rect(f, x - half, y, x - half + max(half, 1), y + 1, snow)
    rect(f, x - 1, base, x + 2, base + 3, col)


# ---------------------------------------------------------------------------------------------

@piece("night-city", "Night City", "scene", seconds=8,
       blurb="Skyline at night: flickering windows, blinking aerials and traffic below.")
def night_city(n):
    r = rng("night-city")
    stars = starfield("night-city/stars", 55, 90)
    street = 168
    glow = [(84, "#2a0c50"), (100, "#3c1466"), (114, "#5a1a78"), (126, "#8c2280"), (136, "#c23c78"),
            (146, "#f06e5a"), (156, "#ff9650")]
    far = np.full(W, H, int)
    x = 0
    while x < W:
        w = int(r.integers(5, 12))
        far[x:x + w] = int(r.integers(100, 140))
        x += w
    near, x = [], int(r.integers(-6, 0))
    while x < W:
        w = int(r.integers(13, 22))
        top = int(r.integers(30, 52)) if r.random() < 0.3 else int(r.integers(96, 140))
        near.append((x, x + w, top))
        x += w + int(r.integers(4, 12))
    warm = [C("#ffc65a"), C("#ff9c3c"), C("#ffe08c")]
    cool = [C("#8cd2ff"), C("#ff78c8")]
    windows = []
    for x0, x1, top in near:
        for wy in range(top + 4, street - 3, 4):
            for wx in range(x0 + 2, x1 - 2, 3):
                if r.random() < 0.38:
                    col = warm[r.integers(0, 3)] if r.random() < 0.8 else cool[r.integers(0, 2)]
                    gap = (r.random(), 0.15 + 0.4 * r.random()) if r.random() < 0.12 else None
                    windows.append((wx, wy, col, gap))
    aerials = [(x0 + (x1 - x0) // 2, top, r.random()) for x0, x1, top in near if top < 60]
    far_win = [(x, int(r.integers(far[x] + 3, street - 2)), r.random()) for x in range(1, W, 2)
               if r.random() < 0.35]
    cars = [C("#c8283c"), C("#2864c8"), C("#a0a0b4"), C("#e6b428"), C("#3ca06e")]
    lanes = [(181, 2, +1, [0.0, 0.45]), (189, 3, -1, [0.2, 0.72])]

    for p in phases(n):
        f = canvas()
        for y, col in glow:
            rect(f, 0, y, W, street, C(col))
        draw_stars(f, stars, p)
        moon(f, 100, 24, 9, C("#f5e6a0"), C("#d2c382"), crescent=(-4, -3))
        for x in range(W):
            rect(f, x, far[x], x + 1, street, C("#232a6e"))
        for x, y, ph in far_win:
            if cyc(p, 1, ph) < 0.8:
                put(f, x, y, C("#6e8cdc"))
        for x0, x1, top in near:
            rect(f, x0, top, x1, street, C("#000000"))
            rect(f, x0, top, x1, top + 1, C("#46467a"))
            rect(f, x0, top, x0 + 1, street, C("#1e1e3c"))
        for wx, wy, col, gap in windows:
            if gap and cyc(p, 1, gap[0]) < gap[1]:
                continue
            rect(f, wx, wy, wx + 1, wy + 2, col)
        for ax, top, ph in aerials:
            rect(f, ax, top - 8, ax + 1, top, C("#46467a"))
            if cyc(p, 8, ph) < 0.3:
                rect(f, ax - 1, top - 10, ax + 2, top - 9, C("#ff2828"))
        rect(f, 0, street, W, street + 2, C("#5a5a7a"))
        for lx in range(12, W, 32):
            rect(f, lx, street - 12, lx + 1, street, C("#5a5a7a"))
            rect(f, lx - 1, street - 13, lx + 3, street - 12, C("#5a5a7a"))
            rect(f, lx + 1, street - 12, lx + 3, street - 11, C("#ffd278"))
            rect(f, lx - 2, street, lx + 6, street + 1, C("#c8965a"))
        for dx in range(0, W, 10):
            rect(f, dx, 185, dx + 5, 186, C("#c8aa46"))
        for y, k, d, offs in lanes:
            for j, off in enumerate(offs):
                pos = cyc(p, k, off) * 176 - 24
                x = int(pos if d > 0 else W - pos)
                body = cars[(j * 2 + y) % len(cars)]
                rect(f, x - 7, y - 3, x + 7, y + 1, body)
                rect(f, x - 4, y - 6, x + 4, y - 3, body * 0.8)
                rect(f, x - 3, y - 5, x - 0, y - 3, C("#a0c8f0"))
                rect(f, x + 1, y - 5, x + 3, y - 3, C("#a0c8f0"))
                rect(f, x - 6, y + 1, x - 3, y + 2, C("#000000"))
                rect(f, x + 3, y + 1, x + 6, y + 2, C("#000000"))
                front, back = (x + 6, x - 7) if d > 0 else (x - 7, x + 6)
                put(f, front, y - 2, C("#fff5c8"))
                put(f, back, y - 2, C("#ff1e1e"))
                for b in range(1, 9):
                    put(f, front + d * b, [y - 2, y - 1], C("#c8b464") if b < 5 else C("#8c7841"))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("synthwave", "Synthwave Sunset", "scene", seconds=4,
       blurb="Striped sun over neon mountains and an endless scrolling grid.")
def synthwave(n):
    hz, cx = 118, 64
    stars = starfield("synthwave/stars", 60, 80)
    sky = [(56, "#200838"), (72, "#2e0a4a"), (86, "#40105e"), (98, "#5a1474"),
           (108, "#7a1a88"), (114, "#a0289a")]
    sun = [C(c) for c in ("#fff064", "#ffd246", "#ffaa3c", "#ff8246", "#ff5a5a", "#ff3c78", "#f0288c")]
    r = rng("synthwave/mtn")
    xs = np.arange(W)
    ridge = sum(a * np.abs(np.sin(TAU * (xs / per + r.random()))) for a, per in ((10, 70), (5, 26), (2, 9)))
    ridge = hz - 4 - (np.abs(xs - cx) / 64) ** 1.4 * 30 - ridge * 0.7
    ridge = np.round(np.minimum(ridge, hz - 1)).astype(int)
    sy, sr = 76, 30
    in_sun = (XX - cx + 0.5) ** 2 + (YY - sy + 0.5) ** 2 <= sr * sr
    band = np.clip(((YY - (sy - sr)) / (2 * sr) * len(sun)).astype(int), 0, len(sun) - 1)
    rel = YY - sy
    for p in phases(n):
        f = canvas()
        for y, col in sky:
            rect(f, 0, y, W, hz, C(col))
        draw_stars(f, stars, p)
        sp = 7  # stripes drift down one spacing per loop and thicken toward the bottom
        cut = (rel > -4) & (((rel - p * sp) % sp) < np.clip(1 + (rel + 4) / 8, 0, 4))
        sel = in_sun & ~cut
        f[sel] = np.array(sun, np.float32)[band[sel]]
        for x in xs:
            rect(f, x, ridge[x], x + 1, hz, C("#2a0c50"))
            rect(f, x, ridge[x], x + 1, ridge[x] + 1, C("#ff46dc"))
        # floor: horizontal lines come toward the viewer by one spacing per loop
        for k in range(0, 40):
            y = hz + (H - hz) / (1 + (k + p) * 0.45)
            if y - hz < 8:  # further lines would merge; a haze band stands in for them
                break
            t = min(1.0, (y - hz) / 50)
            rect(f, 0, int(y), W, int(y) + 1, mix(C("#821e96"), C("#ff32d2"), t))
        for k in range(-10, 11):
            for y in range(hz + 2, H):
                x = int(round(cx + k * 18 * (y - hz) / (H - hz) * 1.6))
                if 0 <= x < W:
                    f[y, x] = mix(C("#821e96"), C("#ff32d2"), min(1.0, (y - hz) / 50))
        rect(f, 0, hz + 1, W, hz + 4, C("#5a147a"))
        rect(f, 0, hz + 4, W, hz + 7, C("#3c0f5a"))
        rect(f, 0, hz, W, hz + 1, C("#ffa0f0"))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("rain-window", "Rain on the Window", "scene", seconds=6,
       blurb="Night city bokeh behind a rain-streaked pane; drips slide and leave trails.")
def rain_window(n):
    r = rng("rain-window")
    cols = [C(c) for c in ("#ffb43c", "#ff8c28", "#ff5050", "#ff64b4", "#50c8ff", "#78ffd2", "#ffe678")]
    bok = [(r.uniform(-6, W + 6), r.uniform(10, H - 10), rad, cols[r.integers(len(cols))],
            r.uniform(0.55, 0.95) * (0.6 if rad > 11 else 1.0), r.integers(1, 3), r.random())
           for rad in sorted(r.uniform(4, 15, 24), reverse=True)]
    drops = [(r.uniform(0, W), r.uniform(0, H), r.choice([1.0, 1.5, 1.5, 2.2, 3.0])) for _ in range(90)]
    drips = [(r.uniform(6, W - 6), r.random(), int(r.integers(1, 3)), r.random()) for _ in range(6)]
    for p in phases(n):
        bg = np.zeros((H, W, 3), np.float32)
        for x, y, rad, col, a, k, ph in bok:
            a = a * (0.8 + 0.2 * wave(p, k, ph))
            d = np.sqrt((XX - x) ** 2 + (YY - y) ** 2)
            cov = np.clip(rad - d + 0.5, 0, 1) * (0.7 + 0.3 * np.clip((d / rad) ** 4, 0, 1))
            bg = bg * (1 - 0.35 * cov[..., None]) + cov[..., None] * col * a
        f = np.clip(bg, 0, 255)
        all_drops = list(drops)
        for x0, off, k, ph in drips:
            c = cyc(p, k, off)
            s = c + 0.06 * np.sin(TAU * 3 * c) / (TAU * 3) * 6  # stick-slip slide
            y = -8 + s * (H + 16)
            x = x0 + 2 * np.sin(TAU * (2 * c + ph))
            all_drops.append((x, y, 2.6))
            for j in range(1, 14):  # trail beads left behind, fading with age
                age = j / 14
                yy = y - j * 7
                if yy > 0:
                    all_drops.append((x0 + 2 * np.sin(TAU * (2 * (c - age * 0.1) + ph)), yy, 1.2 * (1 - age) + 0.4))
        for x, y, rad in all_drops:
            x0i, y0i = int(x - rad - 1), int(y - rad - 1)
            for yy in range(max(y0i, 0), min(int(y + rad + 2), H)):
                for xx in range(max(x0i, 0), min(int(x + rad + 2), W)):
                    dx, dy = xx + 0.5 - x, yy + 0.5 - y
                    if dx * dx + dy * dy <= rad * rad:
                        # a drop is a tiny upside-down lens: show the background from further away
                        sx = int(np.clip(x - dx * 4, 0, W - 1))
                        sy = int(np.clip(y - dy * 4, 0, H - 1))
                        f[yy, xx] = np.clip(bg[sy, sx] * 1.5, 0, 255)
            if rad >= 1.5:
                put(f, x - rad * 0.4, y - rad * 0.5, C("#b4d2f0"))
                put(f, x + rad * 0.3, y + rad * 0.6, C("#000000"))
        yield f


# ---------------------------------------------------------------------------------------------

FISH = {
    "clown": (["..oo.....", ".owwoo.o.", "owkwwooow", "owwwwoooo", ".owwoo.o.", "..oo....."],
              {"o": C("#ff7814"), "w": C("#ffffff"), "k": C("#000000")}),
    "tang": (["...bbbb....", "..bbbbbb.y.", ".bkbbbbbbyy", "bbbbbbbbbyy", ".bbbbbbb.y.", "..bbbbb...."],
             {"b": C("#1e64ff"), "k": C("#000000"), "y": C("#ffdc28")}),
    "angel": (["....y....", "...yy....", "..yyyy...", ".yksyyy.y", "yyysyyyyy", ".yysyyy.y",
               "..yyyy...", "...yy....", "....y...."],
              {"y": C("#ffe650"), "k": C("#000000"), "s": C("#3c3c8c")}),
    "puff": ([".pppp..", "ppkpppp", "ppppppp", "pppppp.", ".pppp.."],
             {"p": C("#b464ff"), "k": C("#000000")}),
}


@piece("aquarium", "Aquarium", "scene", seconds=10,
       blurb="Reef tank: fish crossing, swaying weed, rising bubbles and light shafts.")
def aquarium(n):
    r = rng("aquarium")
    sand_top = 172
    water = [(0, "#1464b4"), (30, "#105aa5"), (60, "#0c4b96"), (95, "#0a3c82"), (130, "#08306e")]
    weeds = [(int(r.integers(4, W - 4)), int(r.integers(30, 70)), r.random(),
              C("#28b45a") if r.random() < 0.6 else C("#78d23c")) for _ in range(9)]
    speck = [(int(r.integers(0, W)), int(r.integers(sand_top + 2, H))) for _ in range(90)]
    rocks = [(22, 176, 12, "#6e6e8c"), (100, 178, 15, "#5a5a78"), (64, 182, 8, "#82829b")]
    school = [(i * 6 % 17, (i * 5) % 9) for i in range(8)]
    fish = [("clown", 1, 58, 0.0, +1), ("tang", 1, 96, 0.45, -1), ("angel", 1, 24, 0.7, +1),
            ("puff", 2, 132, 0.2, -1), ("clown", 1, 116, 0.6, -1)]
    bubbles = [(r.random(), int(r.integers(1, 3)), r.random()) for _ in range(16)]
    for p in phases(n):
        f = canvas()
        for y, col in water:
            rect(f, 0, y, W, sand_top, C(col))
        for k, x0 in enumerate((20, 58, 96)):  # light shafts, slowly swaying
            for y in range(0, sand_top, 1):
                x = x0 + y * 0.35 + 3 * wave(p, 1, k * 0.3)
                wdt = 5 + y // 40
                m = (XX[y] >= x) & (XX[y] < x + wdt)
                f[y, m] = np.minimum(f[y, m] + C("#14283c") * (1 - y / sand_top), 255)
        rect(f, 0, 0, W, 2, C("#3c8cdc"))
        for x in range(0, W, 6):
            rect(f, x + int(3 * cyc(p, 2)) % 6, 1, x + int(3 * cyc(p, 2)) % 6 + 3, 2, C("#8cc8ff"))
        rect(f, 0, sand_top, W, H, C("#c8a064"))
        for x in range(W):
            rect(f, x, sand_top - 1 + int(1.5 * np.sin(x * 0.2)), x + 1, sand_top, C("#c8a064"))
        for x, y in speck:
            put(f, x, y, C("#966e3c"))
        for cx, cy, rad, col in rocks:
            m = ((XX - cx) / rad) ** 2 + ((YY - cy) / (rad * 0.7)) ** 2 <= 1
            f[m & (YY < cy + 6)] = C(col)
            m2 = ((XX - cx + 2) / rad) ** 2 + ((YY - cy + 2) / (rad * 0.7)) ** 2 <= 0.35
            f[m2] = C(col) * 1.25
        for x0, h, ph, col in weeds:
            for j in range(h):
                y = sand_top - j
                sway = (j / h) ** 1.3 * 5 * wave(p, 1, ph + j * 0.012)
                x = int(round(x0 + sway))
                rect(f, x, y, x + 2, y + 1, col)
                if j % 7 == 3:
                    rect(f, x + (2 if j % 14 == 3 else -2), y, x + (4 if j % 14 == 3 else 0), y + 1, col)
        for ph, k, sx in bubbles:  # bubbles from the bubbler stone, wobbling
            c = cyc(p, k, ph)
            y = sand_top - 6 - c * (sand_top + 4)
            x = 112 + 3 * np.sin(TAU * (3 * c + sx)) + sx * 4
            rad = 1 if c < 0.5 else 2
            if rad == 1:
                put(f, x, y, C("#b4e6ff"))
            else:
                put(f, [x - 1, x + 1, x, x], [y, y, y - 1, y + 1], C("#b4e6ff"))
                put(f, x - 1, y - 1, C("#ffffff"))
        rect(f, 108, sand_top - 4, 120, sand_top + 1, C("#5a5a78"))
        for kind, k, y, off, d in fish:
            rows, pal = FISH[kind]
            span = W + 48
            pos = cyc(p, k, off) * span - 24
            x = pos if d > 0 else W - pos
            yy = y + 3 * wave(p, 2 * k, off)
            frame = rows if cyc(p, 30, off) < 0.5 else [row[:-1] + "." for row in rows]
            sprite(f, frame, pal, int(x) - len(rows[0]), int(yy), flip=d < 0, scale=2)
        sc = cyc(p, 1, 0.6) * (W + 40) - 20  # neon tetra school drifting left
        for i, (dx, dy) in enumerate(school):
            x = int(W - sc + dx + 2 * wave(p, 4, i * 0.13))
            y = int(80 + dy + 2 * wave(p, 3, i * 0.21))
            put(f, [x, x + 1], [y, y], C("#3cdcff"))
            put(f, x + 2, y, C("#ff3c3c"))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("campfire", "Campfire", "scene", seconds=4,
       blurb="Crackling fire under the stars; sparks rise and the ground glows.")
def campfire(n):
    r = rng("campfire")
    stars = starfield("campfire/stars", 80, 120)
    tongues = Noise("campfire/flame", (4, 8, 12))    # (t, y, x): wraps in every axis
    detail = Noise("campfire/detail", (8, 16, 24))
    pal = np.array([C(c) for c in ("#000000", "#a01400", "#e63c00", "#ff7800", "#ffb428", "#fff08c")])
    cx, base = 64, 158
    sparks = [(r.uniform(-10, 10), r.random(), int(r.integers(1, 3)), r.random()) for _ in range(30)]
    trees = [(10, 160, 64), (26, 164, 44), (106, 162, 54), (121, 158, 72)]
    yrel = np.clip((base - YY) / 88.0, 0, None)
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p)
        flick = 0.88 + 0.12 * tongues(p * 4, 0.5, 6.5)
        for tx, tb, th in trees:  # dark pines, the side facing the fire catching its light
            pine(f, tx, tb, th, C("#1e3c28"))
            side = 1 if tx < cx else -1
            for j in range(th):
                half = int((j % max(th // 4, 3)) * 0.6 + j * 0.28) + 1
                put(f, tx + side * half, tb - th + j, C("#b4501e") if flick > 0.93 else C("#8c3c1e"))
        # ground lit in rings that breathe with the flame
        d = np.sqrt((XX - cx) ** 2 + ((YY - base - 6) / 0.32) ** 2)
        ground = YY >= 156
        lvl = np.floor(np.clip(1 - d / (70 * flick), 0, 1) * 4)
        gcol = np.array([C("#000000"), C("#501e0a"), C("#78320f"), C("#a04614"), C("#c85a1e")])[lvl.astype(int)]
        f[ground] = gcol[ground]
        for k in range(10):  # stone ring, back half first
            a = np.pi + np.pi * k / 9
            disc(f, cx + 30 * np.cos(a), base + 6 + 7 * np.sin(a), 3.4, C("#5a5050"))
            put(f, cx + 30 * np.cos(a), base + 4 + 7 * np.sin(a), C("#8c786e"))
        # flames: tongues of noise scrolling up by whole lattice periods per loop
        nzv = 0.7 * tongues(p * 4, YY / 11 + p * 8, XX / 7) + 0.3 * detail(p * 8, YY / 6 + p * 16, XX / 4)
        width = 25 * np.clip(1 - yrel, 0, 1) ** 0.7 + 1
        sway = 3 * np.sin(yrel * 5 + TAU * p) * yrel
        shape = 1 - np.abs(XX + 0.5 - cx - sway) / width
        heat = shape * (1.2 - yrel * 0.55) - nzv * (0.35 + 0.9 * yrel)
        lvl = np.clip((heat * 7).astype(int), 0, 5)
        m = (lvl > 0) & (YY < base + 4)
        f[m] = pal[lvl[m]]
        for (x0, y0, x1, y1) in ((34, 170, 94, 156), (94, 170, 34, 156)):  # crossed logs
            for t in np.linspace(0, 1, 90):
                x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                rect(f, x - 1, y - 3, x + 2, y + 3, C("#643719"))
                put(f, x, y - 3, C("#a0643c"))
                put(f, x, y + 2, C("#3c1e0f"))
            for ex, ey in ((x0, y0), (x1, y1)):
                disc(f, ex, ey, 3.2, C("#c8965a"))
                disc(f, ex, ey, 1.5, C("#ff8c28"))
        for k in range(10):  # front half of the stones
            a = np.pi * k / 9
            disc(f, cx + 30 * np.cos(a), base + 6 + 7 * np.sin(a), 3.4, C("#5a5050"))
            put(f, cx + 30 * np.cos(a) - 1, base + 4 + 7 * np.sin(a), C("#c8825a"))
        for sx, off, k, ph in sparks:
            c = cyc(p, k, off)
            y = base - 30 - c * 130
            x = cx + sx + 8 * np.sin(TAU * (2 * c + ph)) * c
            col = pal[5] if c < 0.2 else pal[4] if c < 0.45 else pal[3] if c < 0.7 else pal[2]
            if c < 0.9:
                put(f, x, y, col)
                if c < 0.3:
                    put(f, x, y + 1, pal[3])
        yield f


# ---------------------------------------------------------------------------------------------

@piece("snowy-cabin", "Snowy Cabin", "scene", seconds=8,
       blurb="Log cabin on a winter night: warm windows, chimney smoke, falling snow.")
def snowy_cabin(n):
    r = rng("snowy-cabin")
    stars = starfield("snowy-cabin/stars", 60, 110)
    flakes = [(r.uniform(0, W), r.random(), int(r.integers(1, 3)), r.random(), r.random() < 0.18)
              for _ in range(140)]
    xs = np.arange(W)
    back = np.round(118 - 14 * np.sin(xs / 128 * np.pi * 1.3 + 0.4)).astype(int)
    front = np.round(150 - 6 * np.sin(xs / 128 * np.pi * 2.2 + 1.0)).astype(int)
    trees = [(10, 0, 26), (20, 0, 34), (112, 0, 30), (122, 0, 22), (34, 1, 18)]
    smoke = [(r.random(), r.uniform(-1, 1)) for _ in range(6)]
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p)
        moon(f, 30, 30, 11, C("#f0f0dc"), C("#c8c8b4"))
        for x in xs:
            rect(f, x, back[x], x + 1, H, C("#5a6eaa"))
            rect(f, x, back[x], x + 1, back[x] + 1, C("#8ca0d2"))
        for tx, layer, th in trees:
            base = back[tx] + 4 if layer == 0 else front[tx]
            pine(f, tx, base, th, C("#14503c") if layer == 0 else C("#1e6446"), snow=C("#b4c8f0"))
        for x in xs:
            rect(f, x, front[x], x + 1, H, C("#9bb4e6"))
            rect(f, x, front[x] + 18, x + 1, H, C("#82a0d7"))
        # cabin
        cx, base = 70, 150
        rect(f, cx - 22, base - 22, cx + 22, base, C("#784628"))
        for y in range(base - 22, base, 3):
            rect(f, cx - 22, y, cx + 22, y + 1, C("#5a321e"))
        for k in range(14):
            rect(f, cx - 26 + k, base - 23 - k, cx + 26 - k, base - 22 - k, C("#dce6ff") if k < 12 else C("#b4c3eb"))
        rect(f, cx - 26, base - 24, cx + 26, base - 22, C("#eef3ff"))
        rect(f, cx + 10, base - 44, cx + 16, base - 30, C("#8c4632"))
        rect(f, cx + 9, base - 46, cx + 17, base - 44, C("#dce6ff"))
        for wx in (cx - 16, cx + 6):
            rect(f, wx, base - 17, wx + 10, base - 8, C("#ffbe50"))
            rect(f, wx + 4, base - 17, wx + 6, base - 8, C("#784628"))
            rect(f, wx, base - 13, wx + 10, base - 12, C("#784628"))
            rect(f, wx - 2, base + 1, wx + 12, base + 4, C("#d2b48c"))
            rect(f, wx, base + 4, wx + 10, base + 7, C("#bea08c"))
        rect(f, cx - 4, base - 15, cx + 2, base, C("#46281a"))
        put(f, cx + 1, base - 7, C("#ffbe50"))
        for x in range(cx - 3, cx + 2):
            put(f, x, [base + 2, base + 6, base + 10, base + 14], C("#7891c8"))
        for k, (off, drift) in enumerate(smoke):  # puffs rise, grow and fade out
            c = cyc(p, 1, off)
            y = base - 50 - c * 70
            x = cx + 13 + c * 22 + 4 * np.sin(TAU * (c * 2 + drift))
            rad = 1.5 + c * 3.5
            col = C("#a0a0b4") if c < 0.3 else C("#78788c") if c < 0.6 else C("#55556e")
            if c < 0.8:
                disc(f, x, y, rad, col)
        for x0, off, k, ph, big in flakes:
            c = cyc(p, k, off)
            y = c * (H + 4) - 2
            x = (x0 + 4 * np.sin(TAU * (2 * c + ph))) % W
            if big:
                rect(f, x, y, x + 2, y + 2, C("#f0f5ff"))
            else:
                put(f, x, y, C("#c8d7f5"))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("fireflies", "Fireflies", "scene", seconds=8,
       blurb="Fireflies drifting and blinking over a meadow at the edge of a moonlit forest.")
def fireflies(n):
    r = rng("fireflies")
    xs = np.arange(W)
    tooth = lambda x, per: 1 - np.abs((x / per) % 1.0 - 0.5) * 2   # pointed pine tops
    hf, hm = r.uniform(0.5, 1.0, W // 9 + 2), r.uniform(0.6, 1.0, W // 13 + 2)
    ridge_far = np.round(104 - 18 * tooth(xs, 9) * hf[xs // 9] - 4 * np.sin(xs / 23 + 1)).astype(int)
    ridge_mid = np.round(128 - 26 * tooth(xs + 3, 13) * hm[(xs + 3) // 13] - 4 * np.sin(xs / 17)).astype(int)
    flies = [(r.uniform(4, W - 4), r.uniform(84, 182), r.uniform(4, 14), r.uniform(3, 8),
              int(r.integers(1, 3)), int(r.integers(1, 3)), r.random(), r.random(),
              int(r.integers(2, 5)), r.random()) for _ in range(42)]
    grass = [(int(x), int(r.integers(6, 30)), int(r.integers(-2, 3))) for x in r.choice(W, 150)]
    sky = bands([(0, C("#a0c8be")), (8, C("#78aaaa")), (13, C("#558c96")), (22, C("#3c6e82")),
                 (34, C("#2a5064")), (50, C("#1c3a50")), (72, C("#000000"))],
                np.sqrt((XX - 92) ** 2 + ((YY - 40) * 1.2) ** 2))
    halo = (C("#ffff96"), C("#d2f050"), C("#8cb428"), C("#507814"))
    for p in phases(n):
        f = sky.copy()
        disc(f, 92, 40, 7, C("#f0faeb"))
        for x in xs:  # forest edge: a far blue-green ridge, a darker near one, black meadow
            rect(f, x, ridge_far[x], x + 1, H, C("#1e4b55"))
            rect(f, x, ridge_mid[x], x + 1, H, C("#0f2d32"))
            rect(f, x, ridge_mid[x], x + 1, ridge_mid[x] + 1, C("#28505a"))
        rect(f, 0, 134, W, H, C("#000000"))
        for x0, w in ((6, 8), (118, 10)):  # two big trunks framing the scene
            rect(f, x0 - w // 2, 0, x0 + w // 2, 140, C("#000000"))
        for x, h, lean in grass:  # moonlit grass tips
            for j in range(h):
                put(f, x + (lean * j) // 8, 190 - j, C("#2d6e3c") if j > h - 3 else C("#143c28"))
        lit = []
        for x0, y0, ax, ay, m, k, ph, ps, blinks, bo in flies:
            x = x0 + ax * np.sin(TAU * (m * p + ph))
            y = y0 + ay * np.sin(TAU * (k * p + ps))
            b = cyc(p, blinks, bo)  # quick rise, slow fade, dark the rest of the time
            level = 4 if b < 0.1 else 3 if b < 0.2 else 2 if b < 0.3 else 1 if b < 0.4 else 0
            lit.append((x, y, level))
        for x, y, level in lit:
            if level >= 3:
                for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                    put(f, x + dx, y + dy, halo[3])
            if level >= 2:
                put(f, [x - 1, x + 1, x, x], [y, y, y - 1, y + 1], halo[2] if level < 4 else halo[1])
        for x, y, level in lit:
            if level:
                put(f, x, y, halo[4 - level] if level > 1 else halo[3])
        yield f


# ---------------------------------------------------------------------------------------------

SHIP = ["....w....", "....w....", "...cwc...", "...cwc...", "..cccccc.", ".ccbcbcc.", "cc.ccc.cc",
        "c..rrr..c", "...r.r..."]
ALIENS = [  # original designs: an orb drone, a manta and a jelly; two animation frames each
    (["...mmm...", ".mmmmmmm.", "mm.yyy.mm", "mmmykymmm", ".mmmmmmm.", ".m.m.m.m.", "m.......m"],
     ["...mmm...", ".mmmmmmm.", "mm.yyy.mm", "mmmykymmm", ".mmmmmmm.", "..m.m.m..", ".m.....m."]),
    (["g.......g", "gg..g..gg", "ggggggggg", ".gwgggwg.", "..ggggg..", "...g.g...", "..g...g.."],
     [".........", "g...g...g", "ggggggggg", "ggwgggwgg", "..ggggg..", "...g.g...", "...g.g..."]),
    (["..ccccc..", ".ccccccc.", "cc.ccc.cc", "ccccccccc", ".c.c.c.c.", ".c.c.c.c.", "c.c...c.c"],
     ["..ccccc..", ".ccccccc.", "cc.ccc.cc", "ccccccccc", ".c.c.c.c.", "c.c.c.c.c", ".c.....c."]),
]
APAL = {"m": C("#ff3cc8"), "y": C("#ffe650"), "g": C("#64ff50"), "c": C("#3cc8ff"), "w": C("#ffffff"),
        "b": C("#1e64ff"), "r": C("#ff5a1e"), "k": C("#000000")}


@piece("arcade", "Arcade Attract", "scene", seconds=6,
       blurb="Space-shooter attract mode with original sprites: a fighter picks off divers.")
def arcade(n):
    r = rng("arcade")
    layers = [(r.integers(0, W, 30), r.integers(0, H, 30), k, col)
              for k, col in ((1, "#505078"), (2, "#8c8cb4"), (4, "#dcdcff"))]
    L = 6.0
    py = 166
    slots = [(row, col) for row in range(3) for col in range(4)]
    # (slot index, leave time, hit time, hit x)
    dives = [(1, 0.3, 1.5, 34), (10, 2.3, 3.5, 94), (6, 4.1, 5.3, 60)]
    vb = 160.0  # bullet speed px/s

    def form_xy(t, s):
        row, col = slots[s]
        return 64 - 39 + col * 26 + 10 * np.sin(TAU * t / 3), 38 + row * 22

    def diver_xy(t, d):
        s, t0, t1, xh = d
        u = (t - t0) / (t1 - t0)
        x0, y0 = form_xy(t0, s)
        yh = 112
        x = x0 + (xh - x0) * u + 26 * np.sin(np.pi * u * 2) * (1 if xh > x0 else -1)
        y = y0 + (yh - y0) * u * u * (3 - 2 * u) + 34 * np.sin(np.pi * u) ** 2
        return x, y

    shots = sorted(((t1 - (py - 10 - 112) / vb) % L, xh, t1) for s, t0, t1, xh in dives)

    def player_x(t):
        for k, (ts, xh, _) in enumerate(shots):
            nts, nxh, _ = shots[(k + 1) % len(shots)]
            nts = nts if nts > ts else nts + L
            tt = t if t >= ts else t + L
            if ts <= tt < nts:
                u = np.clip((tt - ts - 0.15) / (nts - ts - 0.4), 0, 1)
                return xh + (nxh - xh) * u * u * (3 - 2 * u)
        return shots[0][1]

    for p in phases(n):
        t = p * L
        f = canvas()
        for xs, ys, k, col in layers:
            put(f, xs, (ys + cyc(p, k) * H).astype(int) % H, C(col))
        text(f, "1UP", 4, 3, C("#ff3c3c"))
        text(f, "004650", 4, 10, C("#ffffff"))
        text(f, "HI-SCORE", W - text_width("HI-SCORE") - 4, 3, C("#ff3c3c"))
        text(f, "052800", W - text_width("052800") - 4, 10, C("#ffffff"))
        flap = cyc(p, 12) < 0.5
        away = {}
        for d in dives:
            s, t0, t1, xh = d
            if t0 <= t < t1:
                away[s] = diver_xy(t, d)
            elif t1 <= t < t1 + 1.6:
                away[s] = None
        for s in range(len(slots)):
            row = slots[s][0]
            spr = ALIENS[row][flap]
            if s in away:
                if away[s] is None:
                    continue
                x, y = away[s]
                spr = ALIENS[row][0]
            else:
                x, y = form_xy(t, s)
                back = [d for d in dives if d[0] == s and d[2] + 1.6 <= t < d[2] + 2.0]
                if back and int((t - back[0][2]) * 20) % 2:
                    continue  # warp-in blink
            sprite(f, spr, APAL, int(x) - 9, int(y) - 7, scale=2)
        for s, t0, t1, xh in dives:  # explosions
            u = (t - t1) % L
            if u < 0.45:
                k = u / 0.45
                rad = 3 + k * 14
                col = C("#ffffff") if k < 0.2 else C("#ffe650") if k < 0.5 else C("#ff7828") if k < 0.8 else C("#b42814")
                for a in range(14):
                    ang = TAU * a / 14 + (a % 2) * 0.2
                    rr = rad * (1 if a % 2 else 0.7)
                    rect(f, xh + rr * np.cos(ang), 112 + rr * np.sin(ang),
                         xh + rr * np.cos(ang) + 2, 112 + rr * np.sin(ang) + 2, col)
                if k < 0.35:
                    disc(f, xh, 112, 5 * (1 - k), C("#ffffff"))
        for ts, xh, t1 in shots:  # bullets
            u = (t - ts) % L
            if u < (t1 - ts) % L:
                y = py - 10 - u * vb
                rect(f, xh - 1, y, xh + 1, y + 5, C("#ffe650"))
        x = player_x(t)
        sprite(f, SHIP, APAL, int(round(x)) - 9, py - 8, scale=2)
        if cyc(p, 12) < 0.5:
            rect(f, int(round(x)) - 3, py + 10, int(round(x)) + 3, py + 12, C("#ffe650"))
        for k in range(2):
            sprite(f, SHIP[2:], APAL, 4 + k * 11, 184)
        if cyc(p, 3) < 0.6:
            msg = "PRESS START"
            text(f, msg, (W - text_width(msg)) // 2, 140, C("#ffe650"))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("sakura", "Sakura Night", "scene", seconds=8,
       blurb="Cherry blossom branch across a full moon, petals drifting down.")
def sakura(n):
    r = rng("sakura")
    stars = starfield("sakura/stars", 50, 140)
    pinks = [C("#ff78aa"), C("#ffaacd"), C("#e6468c"), C("#ffd2e6")]
    # branch as a list of (x, y, thickness) polylines
    branches = [[(-4, 30, 6), (20, 44, 5), (44, 52, 4), (70, 70, 3), (96, 78, 2), (118, 92, 1)],
                [(20, 44, 3), (30, 26, 2), (40, 12, 1)],
                [(44, 52, 3), (56, 86, 2), (60, 108, 1)],
                [(70, 70, 2), (82, 52, 1), (92, 40, 1)],
                [(96, 78, 2), (104, 100, 1)]]
    clusters = []
    for br in branches:
        for (x0, y0, t0), (x1, y1, t1) in zip(br, br[1:]):
            for s in np.linspace(0.2, 1, 3):
                if r.random() < 0.85:
                    clusters.append((x0 + (x1 - x0) * s + r.uniform(-4, 4), y0 + (y1 - y0) * s + r.uniform(-5, 3),
                                     int(r.integers(4, 8))))
    petals = [(r.uniform(-10, W + 10), r.random(), int(r.integers(1, 3)), r.random(), pinks[r.integers(0, 4)])
              for _ in range(42)]
    lake = 150
    floating = [(int(r.integers(0, W)), int(r.integers(lake + 3, H)), pinks[r.integers(0, 4)]) for _ in range(45)]
    xs = np.arange(W)
    hill = np.round(lake - 6 - 9 * np.abs(np.sin(xs / 30 + 0.5)) - 3 * np.sin(xs / 9)).astype(int)
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p, tint=(1, 0.9, 1))
        moon(f, 74, 78, 28, C("#ffd7e1"), C("#f0bed0"))
        for x in xs:
            rect(f, x, hill[x], x + 1, lake, C("#3c1e50"))
            rect(f, x, hill[x], x + 1, hill[x] + 1, C("#64326e"))
        rect(f, 0, lake, W, H, C("#000000"))
        for y in range(lake + 2, H, 3):  # the moon's reflection, broken into drifting ripples
            w = 22 - (y - lake) * 0.12
            shift = 3 * np.sin(TAU * (p + y * 0.07))
            rect(f, 74 - w + shift, y, 74 + w + shift, y + 1, C("#a0647d") if y % 2 else C("#c88ca5"))
        rect(f, 0, lake, W, lake + 1, C("#64326e"))
        for x, y, col in floating:
            put(f, x + int(2 * np.sin(TAU * (p + x * 0.05))), y, col)
        sway = 1.2 * wave(p, 1)
        for br in branches:
            for (x0, y0, t0), (x1, y1, t1) in zip(br, br[1:]):
                for s in np.linspace(0, 1, 40):
                    x, y, t = x0 + (x1 - x0) * s, y0 + (y1 - y0) * s + sway * (x0 + (x1 - x0) * s) / W, t0 + (t1 - t0) * s
                    disc(f, x, y, t / 2 + 0.6, C("#4b1e37"))
                    put(f, x, y - t / 2, C("#783250"))
        for k, (x, y, rad) in enumerate(clusters):
            y = y + sway * x / W
            rr = rng(f"sakura/cl{k}")
            for _ in range(rad * 6):
                a, d = rr.uniform(0, TAU), rad * np.sqrt(rr.random())
                fx, fy = round(x + d * np.cos(a)), round(y + d * np.sin(a) * 0.8)
                col = pinks[rr.integers(0, 3)]
                put(f, [fx, fx - 1, fx + 1, fx, fx], [fy, fy, fy, fy - 1, fy + 1], col)
                put(f, fx, fy, pinks[3] if rr.random() < 0.5 else C("#ff5a8c"))
        for x0, off, k, ph, col in petals:
            c = cyc(p, k, off)
            y = c * (lake + 6) - 5
            x = x0 + 10 * np.sin(TAU * (2 * c + ph)) - c * 18
            flip = cyc(p, 6 * k, ph) < 0.5
            put(f, [x, x + 1] if flip else [x, x], [y, y] if flip else [y, y + 1], col)
        yield f


# ---------------------------------------------------------------------------------------------

@piece("moon-ocean", "Moon over the Ocean", "scene", seconds=8,
       blurb="Full moon over rolling waves, its glitter path shimmering on the water.")
def moon_ocean(n):
    stars = starfield("moon-ocean/stars", 70, 90)
    hz, mx, my = 100, 64, 44
    crest = Noise("moon-ocean/crest", (8, 96, 32))     # (t, row, x): wraps, so scrolling loops
    glit = Noise("moon-ocean/glitter", (8, 96, 48))
    d = np.sqrt((XX - mx) ** 2 + (YY - my) ** 2)
    halo = bands([(0, C("#000000")), (16, C("#46466e")), (20, C("#2d2d55")), (25, C("#000000"))], d)
    depth = np.clip((YY - hz) / (H - hz), 0, 1)
    # rows get taller toward the viewer, crests longer: a simple perspective
    row = (np.sqrt(depth) * 40).astype(np.float32)
    xscale = 0.5 / (0.12 + depth)
    half = 3 + depth * 26
    path = (np.abs(XX + 0.5 - mx) < half) & (YY > hz)
    edge = 1 - np.abs(XX + 0.5 - mx) / half
    for p in phases(n):
        f = halo.copy()
        draw_stars(f, stars, p)
        moon(f, mx, my, 16, C("#fff5d2"), C("#e6dcb4"))
        sea = YY > hz
        c = crest(p * 8, row * 2.4, XX * xscale / 4)
        c2 = crest(p * 8 + 3, row * 2.4 + 40, XX * xscale / 2.5 + 7)
        v = 0.6 * c + 0.4 * c2 + 0.25 * np.sin(TAU * (row * 0.5 - 2 * p))
        col = np.where((v > 0.84)[..., None], C("#5a96e6"),
                       np.where((v > 0.68)[..., None], C("#2864b4"),
                                np.where((v > 0.5)[..., None], C("#14468c"), C("#0a2a64"))))
        f[sea] = col[sea]
        g = glit(p * 8, row * 2.4, XX * xscale / 3)
        spark = path & (g > 0.76 - 0.18 * edge)
        glint = path & ~spark & (g > 0.66 - 0.2 * edge)
        f[glint] = C("#a08c5a")
        f[spark] = C("#fff0b4")
        rect(f, 0, hz, W, hz + 1, C("#3c64a0"))
        rect(f, mx - 4, hz, mx + 4, hz + 1, C("#fff0b4"))
        yield f


# ---------------------------------------------------------------------------------------------

@piece("dusk-peaks", "Dusk Peaks", "scene", seconds=0,
       blurb="Still: layered mountain ridges under a banded dusk sky.")
def dusk_peaks(n):
    r = rng("dusk-peaks")
    f = canvas()
    sky = [(0, "#000000"), (34, "#140a32"), (52, "#231046"), (66, "#37145a"), (80, "#55186e"),
           (92, "#7a1e78"), (102, "#a02878"), (110, "#c83c6e"), (117, "#e6645a"), (123, "#ff8c46")]
    for y, col in sky:
        rect(f, 0, y, W, H, C(col))
    stars = starfield("dusk-peaks/stars", 40, 40)
    draw_stars(f, stars, 0)
    disc(f, 88, 128, 14, C("#ffd264"))
    rect(f, 74, 118, 102, 119, C("#ff8c46"))
    rect(f, 74, 123, 102, 124, C("#ff8c46"))
    layers = [(112, 22, "#a03c82", "#c8649b"), (126, 20, "#6e2878", "#96408c"), (142, 18, "#46195f", "#6e2878"),
              (160, 14, "#231446", None)]
    xs = np.arange(W)
    for k, (base, amp, col, rim) in enumerate(layers):
        ridge = base - amp * np.abs(np.sin(xs / (22 + k * 7) + r.random() * 6)) \
            - amp * 0.4 * np.abs(np.sin(xs / 7.5 + r.random() * 6))
        ridge = np.round(ridge).astype(int)
        for x in xs:
            rect(f, x, ridge[x], x + 1, H, C(col))
            if rim:
                put(f, x, ridge[x], C(rim))
        if k == 3:
            for tx in range(2, W, 7):
                pine(f, tx + int(r.integers(-2, 3)), ridge[min(tx, W - 1)] + 2, int(r.integers(8, 16)), C("#000000"))
    rect(f, 0, 178, W, H, C("#000000"))
    for bx, by in ((40, 70), (48, 66), (55, 72)):  # a few birds
        put(f, [bx - 2, bx - 1, bx, bx + 1, bx + 2], [by - 1, by, by + 1, by, by - 1], C("#000000"))
    yield f
