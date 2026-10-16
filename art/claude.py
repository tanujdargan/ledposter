"""Claude-themed pieces (see docs/ART.md). Fan art for the owner's wall: the warm terracotta
spark, the Claude Code pixel mascot (drawn here from scratch) and a little terminal session.

Flat palettes, so every piece is a lossless GIF at 25 fps. Each frame is a pure function of
the loop phase p in [0, 1); every motion runs whole cycles per loop, and the story pieces
end exactly where they begin."""
import numpy as np

from lib import H, TAU, W, C, _FONT, canvas, disc, piece, put, rect, rng


def phases(n):
    return (i / n for i in range(n))


def cyc(p, k, off=0.0):
    return (k * p + off) % 1.0


def ease(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def seg(t, t0, t1):
    """0..1 progress of t through [t0, t1]."""
    return min(max((t - t0) / (t1 - t0), 0.0), 1.0)


# -- palette ----------------------------------------------------------------------------------

TERRA = C("#d97757")   # the spark
CORAL = C("#ec9470")   # lit rays
PEACH = C("#f6c0a0")   # highlights (small)
RUST = C("#b05a3c")
EMBER = C("#82402a")   # glow band (main channel 130)
DEEP = C("#5c2a1c")    # outer glow band (main channel 92)
CREAM = C("#f0eee6")   # small accents only
GREY = C("#a8a396")
DGREY = C("#6a665e")
GREEN = C("#82c47c")
RED = C("#e07070")
BLACK = C("#000000")


# -- text: the lib 3x5 font plus the punctuation a terminal needs ------------------------------

_EXTRA = {">": "42124", "<": "12421", "/": "11244", ",": "00064", "'": "22000", "?": "61202",
          "_": "00007", "(": "24442", ")": "42224", "=": "07070", "+": "02720", "*": "05250",
          "|": "22222", "@": "07770"}  # '@' is the round bullet
FONT = {**_FONT, **_EXTRA}


def tx(f, s, x, y, col, scale=1):
    for k, ch in enumerate(s.upper()):
        for j, r in enumerate(FONT.get(ch, FONT[" "])):
            bits = int(r)
            for i in range(3):
                if bits & (4 >> i):
                    rect(f, x + (k * 4 + i) * scale, y + j * scale,
                         x + (k * 4 + i + 1) * scale, y + (j + 1) * scale, col)


def tw(s, scale=1):
    return (len(s) * 4 - 1) * scale


# Claude Code's spinner: . -> four spokes -> asterisk -> star -> flower, and back again.
SPIN = [["00000", "00000", "00100", "00000", "00000"],
        ["00100", "00100", "11011", "00100", "00100"],
        ["10101", "01110", "11011", "01110", "10101"],
        ["00100", "10101", "01110", "10101", "00100"],
        ["01010", "11011", "00100", "11011", "01010"],
        ["10101", "01110", "11111", "01110", "10101"]]
SPIN_SEQ = [0, 1, 2, 3, 4, 5, 4, 3, 2, 1]


def glyph(f, rows, x, y, col, scale=1):
    for j, r in enumerate(rows):
        for i, ch in enumerate(r):
            if ch == "1":
                rect(f, x + i * scale, y + j * scale, x + (i + 1) * scale, y + (j + 1) * scale, col)


# -- the spark ----------------------------------------------------------------------------------

# Twelve rounded rays of uneven length, a little off-even in angle, like a hand-cut starburst.
SPARK_LEN = np.array([1.0, 0.78, 0.93, 0.72, 0.98, 0.8, 0.9, 0.74, 1.0, 0.82, 0.88, 0.76])
SPARK_ANG = TAU * (np.arange(12) / 12) + np.array([0, .04, -.03, .05, -.02, .03, 0, -.04, .02, -.03, .04, -.02]) \
    - TAU / 4


def spark_mask(cx, cy, R, rot=0.0, lens=None, angs=None, wfrac=0.09, core=0.16):
    """Boolean canvas mask of a spark: capsule rays that widen toward their rounded tips."""
    lens = SPARK_LEN if lens is None else np.asarray(lens)
    angs = SPARK_ANG if angs is None else np.asarray(angs)
    full = np.zeros((H, W), bool)
    pad = R * (wfrac + 1.05) + 2
    x0, x1 = max(int(cx - pad), 0), min(int(cx + pad) + 2, W)
    y0, y1 = max(int(cy - pad), 0), min(int(cy + pad) + 2, H)
    if x0 >= x1 or y0 >= y1 or R <= 0:
        return full
    gy, gx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    gx += 0.5 - cx
    gy += 0.5 - cy
    m = gx * gx + gy * gy <= (core * R) ** 2
    w0 = max(R * wfrac, 0.55)
    for a, L in zip(angs, lens):
        if L <= 0:
            continue
        ca, sa = np.cos(a + rot), np.sin(a + rot)
        u = gx * ca + gy * sa
        v = -gx * sa + gy * ca
        Lr = L * R
        t = np.clip(u, 0, Lr)
        w = w0 * (0.35 + 0.65 * t / Lr)
        m |= (u - t) ** 2 + v * v <= w * w
    full[y0:y1, x0:x1] = m
    return full


def spark(f, cx, cy, R, col, **kw):
    m = spark_mask(cx, cy, R, **kw)
    f[m] = col
    return m


def glowing_spark(f, cx, cy, R, rot=0.0, lens=None, angs=None, glow=1.0, cols=(DEEP, EMBER, TERRA, CORAL)):
    """Spark with a two-band halo that follows the rays and a lit inner core."""
    deep, ember, body, hi = cols
    if glow > 0:
        spark(f, cx, cy, R * (1 + 0.1 * glow), deep, rot=rot, lens=lens, angs=angs, wfrac=0.09 + 0.08 * glow,
              core=0.24)
        spark(f, cx, cy, R * (1 + 0.045 * glow), ember, rot=rot, lens=lens, angs=angs, wfrac=0.09 + 0.04 * glow,
              core=0.2)
    spark(f, cx, cy, R, body, rot=rot, lens=lens, angs=angs)
    if hi is not None:
        spark(f, cx, cy, R * 0.42, hi, rot=rot, lens=lens, angs=angs, wfrac=0.1, core=0.2)


def mini_spark(f, x, y, size, col):
    """Tiny sparks that stay crisp: 1 = dot, 2 = plus, 3 = eight-ray, 4 = eight-ray with tips."""
    x, y = int(round(x)), int(round(y))
    if size <= 1:
        put(f, x, y, col)
        return
    pts = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]
    if size >= 3:
        pts += [(2, 0), (-2, 0), (0, 2), (0, -2), (1, 1), (-1, -1), (1, -1), (-1, 1)]
    if size >= 4:
        pts += [(3, 0), (-3, 0), (0, 3), (0, -3), (2, 2), (-2, -2), (2, -2), (-2, 2)]
    for dx, dy in pts:
        put(f, x + dx, y + dy, col)


# -- Clawd ----------------------------------------------------------------------------------------
# A 16 x 8 grid: a 12-wide body (columns 2..13, rows 0..5), eyes as two dark holes, a two-pixel
# arm on each side and four stubby legs. Arms, legs and eyes are drawn per pose.

ARMS = {"mid": [(0, 3), (1, 3)],
        "down": [(1, 3), (1, 4), (0, 4)],
        "up": [(1, 3), (1, 2), (0, 2), (0, 1), (0, 0)],
        "out": [(1, 3), (0, 3), (0, 2), (-1, 2)],
        "none": []}


def clawd(f, x, y, s, arms=("mid", "mid"), step=0, eyes="open", look=0, col=TERRA, legs=True):
    """Clawd with its grid origin at (x, y), each grid cell s x s pixels.
    step: 0 standing, 1 / 2 the two walking strides. eyes: open, shut, up."""
    def cell(cx, cy, c=col):
        rect(f, x + cx * s, y + cy * s, x + (cx + 1) * s, y + (cy + 1) * s, c)
    rect(f, x + 2 * s, y, x + 14 * s, y + 6 * s, col)
    for (dx, dy) in ARMS[arms[0]]:
        cell(dx, dy)
    for (dx, dy) in ARMS[arms[1]]:
        cell(15 - dx, dy)
    if legs:
        for k, lx in enumerate((3, 5, 10, 12)):
            lift = (step == 1 and k in (0, 2)) or (step == 2 and k in (1, 3))
            cell(lx, 6)
            if not lift:
                cell(lx, 7)
    ey = {"open": (1, 2), "shut": (2,), "up": (0, 1)}[eyes]
    for ex in (4, 11):
        for r in ey:
            cell(ex + look, r, BLACK)


def starfield(name, count, y0=0, y1=H, levels=(DGREY, GREY)):
    r = rng(name)
    return [(int(r.integers(0, W)), int(r.integers(y0, y1)), levels[int(r.integers(0, len(levels)))],
             int(r.integers(1, 4)), float(r.random())) for _ in range(count)]


def draw_stars(f, stars, p):
    for x, y, col, k, ph in stars:
        if cyc(p, k, ph) < 0.8 and f[y, x].max() == 0:
            put(f, x, y, col)


def dissolve_order(name):
    return rng(name).random((H, W))


# ---------------------------------------------------------------------------------------------

@piece("claude-spark", "Claude Spark", "claude", seconds=10, fps=25,
       blurb="The warm Claude spark, slowly swaying and breathing inside its glow.")
def claude_spark(n):
    stars = starfield("claude-spark/stars", 46)
    twinkles = [(x, y, k, ph) for x, y, _, k, ph in starfield("claude-spark/twinkle", 10)]
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p)
        breath = 0.5 - 0.5 * np.cos(TAU * p * 2)
        rot = 0.1 * np.sin(TAU * p)
        lens = SPARK_LEN * (1 + 0.05 * np.sin(TAU * (2 * p + np.arange(12) / 12)))
        disc(f, 64, 82, 54 + 4 * breath, BLACK)
        glowing_spark(f, 64, 82, 44 + 2.5 * breath, rot=rot, lens=lens, glow=0.4 + 0.6 * breath)
        for x, y, k, ph in twinkles:
            u = cyc(p, k, ph)
            if (abs(x - 64) > 50 or abs(y - 82) > 50) and u < 0.3:
                mini_spark(f, x, y, 3 if u < 0.15 else 2, TERRA if u < 0.15 else EMBER)
        tx(f, "CLAUDE", (W - tw("CLAUDE", 3)) // 2, 158, TERRA, 3)
        yield f


@piece("claude-thinking", "Claude Thinking", "claude", seconds=8, fps=25,
       blurb="The spark thinking: its rays light up in turn like a loading spinner.")
def claude_thinking(n):
    stars = starfield("claude-thinking/stars", 30)
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p)
        head = (p * 4 * 12) % 12          # four laps per loop
        lens = np.empty(12)
        cols = []
        for k in range(12):
            behind = (head - k) % 12      # 0 = the ray just lit
            lit = max(0.0, 1 - behind / 5)
            lens[k] = SPARK_LEN[k] * (0.72 + 0.28 * lit)
            cols.append(PEACH if behind < 1 else CORAL if behind < 2.5 else TERRA if behind < 4.5 else RUST)
        for k in range(12):
            one = np.zeros(12)
            one[k] = lens[k]
            behind = (head - k) % 12
            if behind < 3:
                spark(f, 64, 80, 40 * 1.08, EMBER, lens=one, wfrac=0.15, core=0)
        for k in range(12):
            one = np.zeros(12)
            one[k] = lens[k]
            spark(f, 64, 80, 40, cols[k], lens=one, core=0)
        disc(f, 64, 80, 6, TERRA)
        dots = int(cyc(p, 4) * 4)
        word = "THINKING" + "." * dots
        tx(f, word, (W - tw("THINKING...", 2)) // 2, 146, TERRA, 2)
        yield f


@piece("clawd-walk", "Clawd Takes a Walk", "claude", seconds=8, fps=25,
       blurb="Clawd, the Claude Code mascot, strolls across the wall under a spark sun.")
def clawd_walk(n):
    stars = starfield("clawd-walk/stars", 40, 0, 120)
    r = rng("clawd-walk/ground")
    pebbles = [(int(r.integers(0, W)), int(r.integers(168, 190))) for _ in range(26)]
    s = 5
    span = W + 16 * s
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p)
        spark(f, 100, 36, 15, EMBER, rot=TAU / 12 * p, lens=np.full(12, 0.9), wfrac=0.2)
        spark(f, 100, 36, 13, TERRA, rot=TAU / 12 * p, lens=np.full(12, 0.9))
        rect(f, 0, 165, W, 166, RUST)
        for k, (x, y) in enumerate(pebbles):  # the ground scrolls one full width per loop
            xx = (x - cyc(p, 1) * W) % W
            put(f, xx, y, DGREY if k % 3 else EMBER)
        x = -16 * s + span * p
        stride = int(cyc(p, 16) * 4)  # 16 strides per loop
        step = (1, 0, 2, 0)[stride]
        bob = 1 if step else 0
        clawd(f, int(round(x)), 125 - bob, s, step=step, arms=("down", "down") if step else ("mid", "mid"),
              eyes="shut" if 0.52 < cyc(p, 1) < 0.55 else "open", look=1)
        # its shadow / a few dust specks behind the feet
        if step:
            put(f, int(x) + 2 * s - 2 - (stride % 2) * 3, 164, DGREY)
        yield f


@piece("clawd-wave", "Clawd Says Hi", "claude", seconds=6, fps=25,
       blurb="A big Clawd waves hello from a speech bubble, blinking now and then.")
def clawd_wave(n):
    stars = starfield("clawd-wave/stars", 36)
    s = 6
    ox, oy = (W - 16 * s) // 2, 104
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p)
        t = p * 6
        waving = (t % 3) < 2.0            # two bursts of waving per loop
        arm = ("up" if cyc(p, 12) < 0.5 else "out") if waving else "mid"
        bounce = 2 if waving and cyc(p, 6) < 0.5 else 0
        eyes = "shut" if (2.3 < t < 2.42) or (5.2 < t < 5.32) else "open"
        # speech bubble
        bx0, by0, bx1, by1 = 22, 22, 106, 66
        rect(f, bx0 + 2, by0, bx1 - 2, by0 + 2, TERRA)
        rect(f, bx0 + 2, by1 - 2, bx1 - 2, by1, TERRA)
        rect(f, bx0, by0 + 2, bx0 + 2, by1 - 2, TERRA)
        rect(f, bx1 - 2, by0 + 2, bx1, by1 - 2, TERRA)
        for k in range(9):  # the tail, pointing down at Clawd
            rect(f, 54 + k, by1 - 2 + k, 64, by1 - 1 + k, TERRA)
            if 0 < k < 7:
                rect(f, 56 + k, by1 - 2 + k, 62, by1 - 1 + k, BLACK)
        tx(f, "HI!", (W - tw("HI!", 5)) // 2 - 6, 32, CREAM, 5)
        sp = cyc(p, 3)
        spark(f, 92, 44, 7 + 2 * np.sin(TAU * sp), CORAL, rot=TAU / 12 * cyc(p, 3), lens=np.full(12, 0.9),
              wfrac=0.16)
        clawd(f, ox, oy - bounce, s, arms=("mid", arm), eyes=eyes)
        rect(f, 14, 156, W - 14, 158, RUST)
        yield f


CODE = ["DEF SPARK(R):", "  RAYS = 12", "  FOR K IN RAYS:", "    GLOW(K, R)", "  RETURN LIGHT",
        "", "CLAWD.WAVE()", "PRINT('HI!')"]
KEYWORDS = ("DEF", "FOR", "RETURN", "PRINT")


def code_line(f, s, x, y, upto=None):
    s = s if upto is None else s[:upto]
    body = s.lstrip()
    ind = len(s) - len(body)
    word = body.split(" ")[0].split("(")[0]
    if word in KEYWORDS:
        tx(f, " " * ind + word, x, y, TERRA)
        tx(f, " " * (ind + len(word)) + body[len(word):], x, y, GREY)
    elif body.startswith("CLAWD"):
        tx(f, s, x, y, CORAL)
    else:
        tx(f, s, x, y, GREY)


@piece("clawd-terminal", "Clawd at the Terminal", "claude", seconds=8, fps=25,
       blurb="Clawd hammers away at a keyboard while code scrolls up a little terminal.")
def clawd_terminal(n):
    r = rng("clawd-terminal/keys")
    keys = [(int(r.integers(0, 10)), int(r.integers(0, 2))) for _ in range(64)]
    V = 8
    for p in phases(n):
        f = canvas()
        slot = int(p * 8)                  # one line typed per second
        u = p * 8 - slot
        # monitor
        x0, y0, x1, y1 = 12, 14, 116, 96
        rect(f, x0, y0, x1, y0 + 1, DGREY)
        rect(f, x0, y1 - 1, x1, y1, DGREY)
        rect(f, x0, y0, x0 + 1, y1, DGREY)
        rect(f, x1 - 1, y0, x1, y1, DGREY)
        for k, c in enumerate((RED, TERRA, GREEN)):
            rect(f, x0 + 4 + k * 4, y0 + 3, x0 + 6 + k * 4, y0 + 5, c)
        rect(f, 58, y1, 70, y1 + 5, DGREY)
        rect(f, 48, y1 + 5, 80, y1 + 6, DGREY)
        for j in range(V):
            idx = slot - (V - 1) + j
            line = CODE[idx % len(CODE)]
            ly = y0 + 10 + j * 7
            if j < V - 1:
                code_line(f, line, x0 + 5, ly)
            else:
                shown = int(min(u / 0.7, 1) * len(line) + 0.001)
                code_line(f, line, x0 + 5, ly, shown)
                if cyc(p, 16) < 0.5 or u < 0.7:
                    rect(f, x0 + 5 + shown * 4, ly, x0 + 8 + shown * 4, ly + 5, CORAL)
        # Clawd behind the keyboard, looking up at the screen
        s = 4
        cx, cy = 32, 118
        typing = u < 0.72
        beat = int(cyc(p, 48) * 2)
        arms = (("down", "mid") if beat else ("mid", "down")) if typing else ("mid", "mid")
        blink = 0.40 < cyc(p, 1) < 0.43
        clawd(f, cx, cy - (1 if typing and beat else 0), s, arms=arms, eyes="shut" if blink else "open",
              legs=False)
        # keyboard
        kx0, ky0 = 22, 142
        rect(f, kx0, ky0, kx0 + 84, ky0 + 12, DGREY)
        hot = keys[int(cyc(p, 64) * 64)] if typing else None
        for row in range(2):
            for col in range(10):
                c = CORAL if hot == (col, row) else GREY
                rect(f, kx0 + 3 + col * 8, ky0 + 2 + row * 5, kx0 + 8 + col * 8, ky0 + 5 + row * 5, c)
        rect(f, 6, 156, W - 6, 157, RUST)
        yield f


SESSION = [  # (y, x, text, colour, bullet colour)
    (48, 4, "@ I'LL ADD A WARM SPARK", GREY, CREAM),
    (56, 4, "  THAT BREATHES SLOWLY.", GREY, None),
    (68, 4, "@ UPDATE(ART/CLAUDE.PY)", CREAM, GREEN),
    (76, 4, "  + GLOW = BANDS(R=46)", GREEN, None),
    (84, 4, "  + LOOP = SEAMLESS", GREEN, None),
    (92, 4, "  - LEDS = ALL WHITE", RED, None),
    (104, 4, "@ DONE. THE WALL GLOWS!", GREY, CREAM),
]


@piece("claude-code", "Claude Code Session", "claude", seconds=12, fps=25,
       blurb="A tiny Claude Code session: a prompt, the spinner pondering, an edit, and a glowing spark.")
def claude_code(n):
    prompt = "MAKE MY WALL GLOW"
    starts = []
    t = 5.6
    for y, x, s, col, b in SESSION:   # stream the answer at 40 characters a second
        starts.append(t)
        t += len(s) / 40 + 0.15
    done = t
    for p in phases(n):
        f = canvas()
        tt = p * 12
        # welcome box
        rect(f, 1, 3, 127, 4, TERRA)
        rect(f, 1, 27, 127, 28, TERRA)
        rect(f, 1, 3, 2, 28, TERRA)
        rect(f, 126, 3, 127, 28, TERRA)
        glyph(f, SPIN[4], 6, 8, TERRA)
        tx(f, "WELCOME TO CLAUDE CODE!", 14, 8, CREAM)
        tx(f, "/HELP FOR HELP", 14, 18, DGREY)
        cursor_on = cyc(p, 24) < 0.5
        live = tt < 11.3
        typed = int(seg(tt, 1.0, 2.6) * len(prompt) + 1e-6) if live else 0
        tx(f, ">", 4, 36, GREY)
        tx(f, prompt[:typed], 12, 36, CREAM)
        if (tt < 1.0 or not live) and cursor_on:
            rect(f, 12, 36, 15, 41, GREY)
        elif 1.0 <= tt < 2.8:
            rect(f, 12 + typed * 4, 36, 15 + typed * 4, 41, GREY)
        if live and tt >= 2.8:
            last_y = 40
            for (y, x, s, col, b), t0 in zip(SESSION, starts):
                k = int(seg(tt, t0, t0 + len(s) / 40) * len(s) + 1e-6)
                if k <= 0:
                    continue
                last_y = y
                txt = s[:k]
                if txt.startswith("@"):
                    tx(f, "@", x, y, b)
                    tx(f, " " + txt[1:], x, y, col)
                else:
                    tx(f, txt, x, y, col)
            if tt < done:  # the spinner rides one line below the stream
                g = SPIN_SEQ[int(tt / 0.1) % len(SPIN_SEQ)]
                sy = last_y + 12
                glyph(f, SPIN[g], 4, sy, TERRA)
                word = "PONDERING" if tt < 5.6 else "SPARKING"
                tx(f, word + "." * (int(tt * 3) % 4), 12, sy, TERRA)
            grow = ease(seg(tt, done + 0.2, done + 1.4))
            if grow > 0:
                glowing_spark(f, 64, 150, 24 * grow, rot=0.4 * (tt - done), glow=grow)
        yield f


@piece("spark-field", "Field of Sparks", "claude", seconds=10, fps=25,
       blurb="A night sky of tiny Claude sparks twinkling and drifting past in layers.")
def spark_field(n):
    r = rng("spark-field")
    cols = [TERRA, TERRA, CORAL, RUST, PEACH]
    layers = []
    for speed, count, sizes in ((0, 70, (1,)), (1, 26, (2, 3)), (2, 9, (4,))):
        layers.append([(r.uniform(0, W), r.uniform(0, H), int(r.choice(sizes)), cols[int(r.integers(0, len(cols)))],
                        int(r.integers(1, 4)), r.random(), speed) for _ in range(count)])
    big = [(r.uniform(16, W - 16), r.uniform(0, H), r.random(), int(r.integers(0, 2)) * 2 - 1) for _ in range(4)]
    for p in phases(n):
        f = canvas()
        for layer in layers:
            for x, y, size, col, k, ph, speed in layer:
                yy = (y + cyc(p, speed) * H) % H if speed else y
                u = cyc(p, k, ph)
                if speed == 0:
                    if u < 0.75:
                        put(f, x, yy, col if u < 0.25 else (RUST if col is not RUST else EMBER))
                else:
                    sz = size if u < 0.6 else max(size - 1, 1)
                    mini_spark(f, x, yy, sz, col if u < 0.6 else RUST)
        for x, y, ph, d in big:   # a few proper sparks, turning one ray-step per loop
            yy = (y + cyc(p, 2) * (H + 40)) % (H + 40) - 20
            R = 9 + 1.5 * np.sin(TAU * (2 * p + ph))
            lens = np.full(8, 0.7)
            lens[::2] = 1.0
            angs = TAU * np.arange(8) / 8
            spark(f, x, yy, R, TERRA, rot=d * TAU / 4 * p, lens=lens, angs=angs, wfrac=0.15)
            disc(f, x, yy, 1.5, CORAL)
        yield f


@piece("hello-claude", "Hello, I'm Claude", "claude", seconds=10, fps=25,
       blurb="The spark blooms, a greeting types itself out, then it all sparkles away.")
def hello_claude(n):
    order = dissolve_order("hello-claude/dissolve")
    l1, l2 = "HELLO, I'M", "CLAUDE"
    for p in phases(n):
        f = canvas()
        t = p * 10
        g = ease(seg(t, 0.5, 1.7))
        if g > 0:
            rot = 0.9 * (1 - g) + 0.05 * np.sin(TAU * p * 2)
            glowing_spark(f, 64, 56, 34 * g, rot=rot, glow=0.5 + 0.5 * np.sin(TAU * p * 2) ** 2)
        k1 = int(seg(t, 2.0, 3.4) * len(l1) + 1e-6)
        k2 = int(seg(t, 3.7, 4.7) * len(l2) + 1e-6)
        x1 = (W - tw(l1, 2)) // 2
        x2 = (W - tw(l2, 4)) // 2
        tx(f, l1[:k1], x1, 110, CREAM, 2)
        tx(f, l2[:k2], x2, 132, TERRA, 4)
        blink = cyc(p, 20) < 0.5
        if 1.8 < t < 3.55 and (blink or t < 3.4):
            rect(f, x1 + k1 * 8, 110, x1 + k1 * 8 + 6, 120, CORAL)
        elif 3.55 <= t < 8.3 and (blink or t < 4.7):
            rect(f, x2 + k2 * 16, 132, x2 + k2 * 16 + 12, 152, CORAL)
        out = seg(t, 8.3, 9.4)
        if out > 0:
            gone = order < out
            f[gone] = 0
            edge = (order >= out) & (order < out + 0.06) & (f.max(-1) > 0)
            f[edge] = PEACH
        yield f


# Constellation nodes on the 16 x 8 Clawd grid (cell corners), and the path that joins them.
CONST = {"tl": (2, 0), "tr": (14, 0), "rs": (14, 3), "ra": (16, 3), "rb": (16, 4), "rh": (14, 4),
         "br": (14, 6), "bl": (2, 6), "lh": (2, 4), "lb": (0, 4), "la": (0, 3), "ls": (2, 3),
         "f1": (3.5, 8), "f2": (5.5, 8), "f3": (10.5, 8), "f4": (12.5, 8),
         "k1": (3.5, 6), "k2": (5.5, 6), "k3": (10.5, 6), "k4": (12.5, 6)}
PATH = [("tl", "tr"), ("tr", "rs"), ("rs", "ra"), ("ra", "rb"), ("rb", "rh"), ("rh", "br"),
        ("br", "k4"), ("k4", "f4"), ("k3", "f3"), ("k3", "k2"), ("k2", "f2"), ("k1", "f1"),
        ("k1", "bl"), ("bl", "lh"), ("lh", "lb"), ("lb", "la"), ("la", "ls"), ("ls", "tl")]
STAR_NODES = ["tl", "tr", "ra", "rb", "br", "f1", "f2", "f3", "f4", "bl", "lb", "la"]


@piece("clawd-constellation", "Clawd Constellation", "claude", seconds=10, fps=25,
       blurb="Stars join up into Clawd, it lights up and blinks, then fades back into the sky.")
def clawd_constellation(n):
    stars = starfield("clawd-constellation/stars", 70)
    s, ox, oy = 7, 8, 70
    pt = {k: (ox + gx * s, oy + gy * s) for k, (gx, gy) in CONST.items()}
    lens = [np.hypot(pt[b][0] - pt[a][0], pt[b][1] - pt[a][1]) for a, b in PATH]
    total = sum(lens)
    r = rng("clawd-constellation/nodes")
    appear = {k: r.uniform(0.3, 1.1) for k in STAR_NODES}
    for p in phases(n):
        f = canvas()
        t = p * 10
        draw_stars(f, stars, p)
        fill = seg(t, 5.0, 5.6) if t < 8.0 else 1 - seg(t, 8.0, 8.4)
        if fill > 0:   # the body lights up as a dim terracotta silhouette
            col = DEEP if fill < 0.5 or 7.3 < t < 8.2 else EMBER
            clawd(f, ox, oy, s, col=col, eyes="open")
            for ex in (4, 11):
                rect(f, ox + ex * s, oy + s, ox + (ex + 1) * s, oy + 3 * s, BLACK)
        drawn = seg(t, 1.3, 4.8) * total if t < 8.6 else (1 - seg(t, 8.6, 9.3)) * total
        acc = 0
        for (a, b), L in zip(PATH, lens):
            if drawn <= acc:
                break
            u = min((drawn - acc) / L, 1)
            m = max(int(L * u), 1)
            xs = np.linspace(pt[a][0], pt[a][0] + (pt[b][0] - pt[a][0]) * u, m + 1)
            ys = np.linspace(pt[a][1], pt[a][1] + (pt[b][1] - pt[a][1]) * u, m + 1)
            put(f, xs, ys, RUST)
            acc += L
        for k in STAR_NODES:
            if appear[k] < t < 9.4 + appear[k] * 0.4:
                x, y = pt[k]
                tw_ = cyc(p, 5, appear[k]) < 0.5
                mini_spark(f, x, y, 3 if tw_ else 2, CORAL if tw_ else TERRA)
        if 5.3 < t < 8.3:   # the eyes are the two brightest stars
            for ex in (4.5, 11.5):
                ok = not (6.6 < t < 6.75)
                if ok:
                    mini_spark(f, ox + ex * s, oy + 1.5 * s, 2, PEACH)
        yield f


@piece("spark-bloom", "Spark Garden", "claude", seconds=10, fps=25,
       blurb="Claude sparks grow on stems and bloom like flowers, open, turn, and fold away.")
def spark_bloom(n):
    r = rng("spark-bloom")
    heads = [TERRA, CORAL, RUST, TERRA, CORAL, TERRA, RUST]
    xs = [12, 30, 48, 66, 84, 102, 118]
    flowers = []
    for k, x in enumerate(xs):
        lens = r.uniform(0.7, 1.0, 12)
        flowers.append((x + r.uniform(-3, 3), r.uniform(70, 150), r.uniform(9, 15), (k * 0.37) % 1.0,
                        heads[k], lens, int(r.integers(0, 2)) * 2 - 1, r.random()))
    stem_c, leaf_c = C("#5f9a52"), C("#4c8044")
    stars = starfield("spark-bloom/stars", 30, 0, 90)
    for p in phases(n):
        f = canvas()
        draw_stars(f, stars, p)
        for x, top, R, off, col, lens, d, ph in flowers:
            u = (p + off) % 1.0
            grow = ease(seg(u, 0.0, 0.15)) * (1 - ease(seg(u, 0.85, 1.0)))
            bloom = ease(seg(u, 0.14, 0.3)) * (1 - ease(seg(u, 0.72, 0.84)))
            if grow <= 0:
                continue
            hy = H - (H - top) * grow
            sway = 2.5 * np.sin(TAU * (2 * p + ph)) * grow
            ys = np.arange(int(hy), H)
            k = (H - ys) / max(H - hy, 1)
            sx = x + sway * k * k
            put(f, sx, ys, stem_c)
            ly = int(H - (H - hy) * 0.45)
            lk = (H - ly) / max(H - hy, 1)
            lx = x + sway * lk * lk
            for j in range(1, 6):
                put(f, lx + d * j, ly - j // 2, leaf_c)
            hx = x + sway
            if bloom > 0:
                rot = d * 0.8 * u + 0.4 * (1 - bloom)
                spark(f, hx, hy, R * bloom * 1.15, EMBER, rot=rot, lens=lens, wfrac=0.18)
                spark(f, hx, hy, R * bloom, col, rot=rot, lens=lens)
                disc(f, hx, hy, max(1.5, 2.5 * bloom), PEACH)
            else:
                disc(f, hx, hy, 2, RUST)
        yield f
