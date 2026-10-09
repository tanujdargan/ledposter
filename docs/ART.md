# Art pack

A set of original pixel scenes and generative loops drawn at the wall's native 128 x 192, plus a gallery of public-domain paintings. The generator, the painting list and the upload tool are in the repo. The rendered files, downloads and personal media are not.

```
art/build.py          render the pieces / download the paintings / build personal media
art/scenes.py         pixel scenes (flat palettes, lossless GIF)
art/ambient.py        generative pieces (lossless RGB H.264)
art/lib.py            shared helpers: canvas, drawing, loopable noise, 3x5 font, encoder, wall checks
art/paintings.json    the paintings: URL, title, artist, year, licence, source page, crop
art/personal.py       optional: your own images/GIFs/clips -> native 128x192 loops
art/preview.py        contact sheet through the real transcoder, simulated as the LEDs show it
tools/artpack.py      upload everything and build the playlists (stdlib only)
```

## Rebuild and upload

```sh
python3 art/build.py                 # ~2 min; renders art/out/{scene,ambient}/ + manifest.json
python3 art/build.py --paintings     # downloads into art/.cache/ once, copies to art/out/painting/
python3 art/build.py --personal      # only if art/personal/sources.json exists (see below)
python3 art/preview.py               # optional: /tmp/ledposter-art/contact.png
LEDPOSTER_TOKEN=lp_... python3 tools/artpack.py --url http://ledposter.local
```

`build.py` needs numpy and ffmpeg. If `python3` has no numpy, it re-runs itself with `web/.venv/bin/python`. Output is deterministic: every piece uses a fixed, named random seed. `--only SLUG ...` renders a subset.

Create the token in the web app under Settings > API tokens. `artpack.py` uploads every manifest item that is not already on the wall (matched by name). Native pieces go up with `filter=pixel`. Paintings go up with `filter=smooth`, `scale=fill` and their crop. It then waits for the transcodes and creates or replaces these playlists:

| Playlist | Contents | Per item |
|---|---|---|
| Gallery | the paintings | 20 s |
| Pixel Scenes | the pixel scenes | 18 s |
| Ambient | the generative pieces, shuffled | 30 s |
| Everything | all of the above, shuffled | as above |

You can run it again safely. It reuses existing items and rewrites the playlists with the same content. It re-uploads an item only if its previous transcode failed. To refresh a piece after changing its code, delete it in the web app's library and run `artpack.py` again.

## Designing for the wall

These rules come from the real panels (gamma 2.2, 7 PWM bits, viewed from about 1 m):

- **Black is LEDs off.** It looks best and costs no power. Every piece is mostly black, with colour where it matters.
- **Dark tones vanish.** After gamma, a channel under about 40 lands on PWM step 0 or 1. Between 40 and 70 it gets only a few steps, so it bands and can shimmer. Visible colours keep their main channel at 70 or above. Skies are drawn as deliberate bands, not smooth dark gradients.
- **Avoid big bright fields.** White draws the most current. The scenes stay under about 0.18 mean LED drive (1.0 is a full white wall).
- **Seamless loops.** Every frame is a pure function of the loop phase. Each motion runs a whole number of cycles per loop, and the noise lattices wrap in time. Life fades out and back in through black. `build.py` reports `seam`: the last-to-first frame step divided by the largest step inside the loop. A value of 1 or less means the wrap is no bigger than any other frame step.
- **Lossless.** Scenes use their own palette (256 colours or fewer) and encode as exact GIFs at 25 fps. Smooth pieces use RGB H.264 with qp 0 at 30 fps, with no chroma subsampling to smear pixel edges. `build.py` decodes each file and refuses to write it unless it matches the rendered frames exactly.

## Pieces

| Slug | Title | Kind | Loop |
|---|---|---|---|
| night-city | Night City: skyline, flickering windows, blinking aerials, traffic | scene | 8 s |
| synthwave | Synthwave Sunset: striped sun, neon ridge, scrolling grid | scene | 4 s |
| rain-window | Rain on the Window: city bokeh, drops and sliding drips | scene | 6 s |
| aquarium | Aquarium: fish, swaying weed, bubbles, light shafts | scene | 10 s |
| campfire | Campfire: flames, sparks, glowing ground, firelit pines | scene | 4 s |
| snowy-cabin | Snowy Cabin: warm windows, chimney smoke, snowfall | scene | 8 s |
| fireflies | Fireflies: blinking swarm at a moonlit forest edge | scene | 8 s |
| arcade | Arcade Attract: original sprites; the fighter picks off divers | scene | 6 s |
| sakura | Sakura Night: blossom branch over a pink moon, falling petals | scene | 8 s |
| moon-ocean | Moon over the Ocean: waves and a shimmering glitter path | scene | 8 s |
| dusk-peaks | Dusk Peaks: layered ridges under a banded sky | scene | still |
| lava-lamp | Lava Lamp: metaball blobs in magenta and orange | ambient | 10 s |
| aurora | Aurora: curtains over snowy peaks, mirrored in a lake | ambient | 10 s |
| ink | Flowing Ink: domain-warped marbling | ambient | 10 s |
| life | Life: Game of Life from a random soup with colour trails | ambient | 10 s |
| breathing | Breathing Light: three soft colour clouds | ambient | 10 s |
| starfield | Warp Starfield | ambient | 5 s |
| orbits | Orrery: tilted orbits, a moon, a ringed giant | ambient | 10 s |
| bounce | Bounce: a faceted gem that changes colour on every hit, corner hits included | ambient | 10 s |
| kaleidoscope | Kaleidoscope: eightfold mirrored noise | ambient | 8 s |

All sprites, scenes and designs are original. None of them are copyrighted or trademarked characters or logos.

## Paintings and attribution

All of the paintings are public-domain works. Each image comes from a source that marks it public domain or CC0. The canvas is a 2:3 portrait, so each painting is cropped (`frame` in `paintings.json`: crop centre and height, as fractions). Every URL below was downloaded successfully when this list was made.

| Work | Artist | Year | Licence | Source |
|---|---|---|---|---|
| The Great Wave off Kanagawa | Katsushika Hokusai | c. 1830-32 | CC0 (The Met Open Access) | [metmuseum.org 45434](https://www.metmuseum.org/art/collection/search/45434) |
| The Starry Night | Vincent van Gogh | 1889 | Public domain (PD-Art) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Van_Gogh_-_Starry_Night_-_Google_Art_Project.jpg) |
| Girl with a Pearl Earring | Johannes Vermeer | c. 1665 | Public domain (PD-Art) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Meisje_met_de_parel.jpg) |
| Wheat Field with Cypresses | Vincent van Gogh | 1889 | CC0 (The Met Open Access) | [metmuseum.org 436535](https://www.metmuseum.org/art/collection/search/436535) |
| The Kiss | Gustav Klimt | 1907-08 | Public domain (PD-Art) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:The_Kiss_-_Gustav_Klimt_-_Google_Cultural_Institute.jpg) |
| Water Lilies | Claude Monet | 1906 | Public domain (PD-Art; Art Institute of Chicago) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Claude_Monet_-_Water_Lilies_-_1906,_Ryerson.jpg) |
| Fireworks at Ryogoku | Utagawa Hiroshige | 1858 | Public domain (Brooklyn Museum) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Fireworks_at_Ryogoku_(Ryogoku_Hanabi),_No._98_from_One_Hundred_Famous_Views_of_Edo.jpg) |
| Sudden Shower over Shin-Ohashi Bridge and Atake | Utagawa Hiroshige | 1857 | CC0 (The Met, via Commons) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:%C5%8Chashi_Atake_no_y%C5%ABdachi-%E5%90%8D%E6%89%80%E6%B1%9F%E6%88%B8%E7%99%BE%E6%99%AF_%E5%A4%A7%E3%81%AF%E3%81%97%E3%81%82%E3%81%9F%E3%81%91%E3%81%AE%E5%A4%95%E7%AB%8B-Sudden_Shower_over_Shin-%C5%8Chashi_Bridge_and_Atake_(%C5%8Chashi_Atake_no_y%C5%ABdachi),_from_the_series_One_Hundred_Famous_Views_of_Edo_(Meisho_Edo_hyakkei)_MET_DP123602.jpg) |
| Cafe Terrace at Night | Vincent van Gogh | 1888 | Public domain (PD-Art) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Vincent_Willem_van_Gogh_-_Cafe_Terrace_at_Night_(Yorck).jpg) |
| Fine Wind, Clear Morning (Red Fuji) | Katsushika Hokusai | c. 1830-32 | Public domain (PD-Art) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Katsushika_Hokusai_-_Fine_Wind,_Clear_Morning_(Gaif%C5%AB_kaisei)_-_Google_Art_Project.jpg) |
| Sunflowers | Vincent van Gogh | 1888 | Public domain (PD-Art; National Gallery London) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Vincent_van_Gogh_-_Sunflowers_(1888,_National_Gallery_London).jpg) |
| Plum Garden at Kameido | Utagawa Hiroshige | 1857 | Public domain (Rijksmuseum) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:De_pruimenboomgaard_te_Kameido-Rijksmuseum_RP-P-1956-743.jpeg) |

Downloads use 1280 px Wikimedia thumbnails or The Met's web-large images. That is plenty for 128 x 192 and polite to the hosts. They are fetched once into `art/.cache/`, with a descriptive User-Agent and a pause between requests. The Great Wave and Sunflowers have large cream and yellow paper areas, so they draw noticeably more current than the rest of the pack. The wall's power limiter handles that.

## Personal media (optional, never committed)

`art/personal/` is ignored by git. To show your own images, GIFs or clips, put a `sources.json` there. The format is documented at the top of `art/personal.py`; each item has a URL, a portrait crop, an optional background key and grade, and an optional "alive" loop for stills. Then run `python3 art/build.py --personal`. Each item is rendered at native 128 x 192:

- Clips are downscaled with area averaging and keep their timing.
- Pixel art is sampled at its own grid and scaled up by a whole number, so every art pixel stays crisp.
- Stills become a seamless loop: a slow Ken Burns breath, a pulsing rim light, a spider-sense ripple, and an optional web-shot streak.

`artpack.py` uploads these too, with `filter=pixel`, into the playlist named in `sources.json`. They stay out of the other playlists. Check them with `python3 art/preview.py --category personal --sheet /tmp/personal.png`.
