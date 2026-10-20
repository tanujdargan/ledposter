"""Pieces from ASCII Magic (https://www.ascii-magic.com/ascii, MIT licence, (c) 2026 ASCII Magic).

Each piece there is a JS function from time to a frame of coloured text. ascii_magic.mjs turns a
frame into wall pixels (needs node >= 18 on PATH); the repo is cloned at a pinned commit into
art/.cache. PICKS were chosen by eye from contact sheets of all 634 pieces: text-heavy pieces
(UIs, charts, lettering) turn to noise at 128 px wide and are left out.

mode "reflow" gives the piece a portrait grid so it redraws to fill the wall (works for scenes that
lay out from meta.cols/rows); "fit" keeps its own grid, centred with black above and below.
Pieces are not loops, so the last second crossfades into the first.
"""
import json
import os
import subprocess

import numpy as np

from lib import H, PIECES, W, piece

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, ".cache", "ascii-magic-art")
URL, COMMIT = "https://github.com/kailashsr38/ascii-magic-art", "de88962156057cbef583bd265bf55e23d6c5e30d"
SECONDS, FADE = 12, 1.0

FIT = {"aurora-curtains", "christmas-tree", "clockwork-gears", "coffee-steam", "sleeping-cat", "sleepy-moon",
       "scarf-penguin", "snowy-cabin"}  # reflowed, these are mostly white snow: full power, no picture
PICKS = """
apollonian-gasket arctic-icebergs asteroid-belt aurora-curtains autumn-forest black-hole bubble-tea-pearls
campfire-under-stars campfire candle-flame cherry-blossom-river christmas-tree clifford-attractor clockwork-gears
cloud-drift coffee-steam cool-sun-shades cuckoo-clock cyberpunk-alley dancing-cactus debug-duck-bob desert-dunes
desk-fan double-helix earth-rotating fern-fractal ferris-wheel fireflies fireworks floating-islands flow-field
fluid-smoke friendly-ghost-float ganges-dawn golden-gate-fog goldfish-tank growing-tree halloween-pumpkin heart-3d
hourglass hummingbird-hibiscus jellyfish juggling-octopus kaleidoscope lava-cracks lava-lamp lavender-fields
leaping-dolphins lighthouse-storm liquid-chrome mandelbrot-zoom mars-colony matrix-rain melting-ice-cream-cone
metaballs moonlit-sea mountain-sunrise nebula-drift neon-city-rain neon-jukebox northern-lights ocean-waves octopus
orbiting-planets owl-blink paris-rooftops pendulum-wall-clock pixel-fire planet-astronaut-wave plasma rainbow-arc
rainbow-warp-road reaction-diffusion retro-synth-sunset rice-terraces ringed-planet scarf-penguin sleeping-cat
sleepy-moon snow-fox-trot snow-globe snowy-cabin solar-flare spider-web spiral-galaxy starburst starfield-warp
sunrise-horizon swimming-whale synthwave-grid taj-mahal-sunset tethered-astronaut torus-knot tropical-island tunnel
valentine-hearts vaporwave-sun volcano-eruption voronoi-cells water-caustics waterfall
""".split()


def ensure_repo():
    if not os.path.isdir(os.path.join(REPO, ".git")):
        subprocess.run(["git", "clone", "-q", URL, REPO], check=True)
    subprocess.run(["git", "-C", REPO, "checkout", "-q", COMMIT], check=True)


def make(slug, n, fps):
    mjs = os.path.join(HERE, "ascii_magic.mjs")
    extra = round(FADE * fps)
    raw = subprocess.run(["node", mjs, REPO, "render", slug, str(n + extra), str(fps),
                          "fit" if slug in FIT else "reflow"], capture_output=True, check=True).stdout
    f = np.frombuffer(raw, np.uint8).reshape(n + extra, H, W, 3).astype(np.float32)
    # frame n-1 -> 0 must be as small a step as any other: the first `extra` frames fade from the
    # frames that follow the loop end (n..) into the start, so the wrap continues the motion
    w = (np.arange(extra, dtype=np.float32) / extra)[:, None, None, None]
    f[:extra] = f[n:] * (1 - w) + f[:extra] * w
    return list(f[:n])


def register():
    ensure_repo()
    meta = json.loads(subprocess.run(["node", os.path.join(HERE, "ascii_magic.mjs"), REPO, "meta", *PICKS],
                                     capture_output=True, check=True, text=True).stdout)
    taken = {p.title for p in PIECES}  # tools/artpack.py matches wall items by name
    for slug in PICKS:
        name = meta[slug]["name"] + (" (ASCII)" if meta[slug]["name"] in taken else "")
        piece("ascii-" + slug, name, "ascii", seconds=SECONDS,
              blurb=meta[slug]["note"] + " (ASCII Magic, MIT)")(lambda n, s=slug: make(s, n, 60))


register()
