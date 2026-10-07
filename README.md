# ledposter

Software for a 128 x 192 LED wall: six 64x64 P3 HUB75 panels on a Raspberry Pi 3B with an Adafruit RGB Matrix Bonnet. The full spec is in [docs/spec.md](docs/spec.md).

```
engine/   C++ display engine (owns the matrix, frame loop, limiter, media playback)
web/      Python package `ledposter` (so far: the media transcoder)
config/   example YAML configs (all hardware tunables live here)
vendor/   hzeller/rpi-rgb-led-matrix (git submodule, pinned)
docs/     spec
```

Status: **milestone 2**, the media pipeline. Milestone 1 gave config-driven matrix init, the 128x192 canvas mapped onto the 6-panel snake, test patterns and the power limiter. Milestone 2 adds the ffmpeg transcoder (any supported upload to cached raw 128x192 RGB24 frames with their timings, fit/fill/stretch, smooth/pixel filters, crop and position, gamma, optional dithering) and engine playback of cached media and playlists.

## Wiring assumptions

- Adafruit Bonnet, E jumper soldered to **8** (required for 64x64 panels).
- `hardware_mapping: adafruit-hat-pwm` assumes the GPIO4 to GPIO18 jumper wire is soldered on the bonnet (gives flicker-free PWM). If you haven't soldered it, set `adafruit-hat`.
- Panels chained in a snake, all upright, viewed from the front:

  ```
  [1][2]    1 is plugged into the bonnet
  [4][3]
  [5][6]
  ```

  Rewired differently? Edit `matrix.panels` in the config. Code stays the same.

## Pi setup (once)

```sh
sudo apt install -y git build-essential libyaml-cpp-dev nlohmann-json3-dev ffmpeg python3-venv python3-numpy
git clone --recurse-submodules git@github.com:tanujdargan/ledposter.git
```

In `/boot/firmware/config.txt` set `dtparam=audio=off`. Then append `isolcpus=3` to the single line in `/boot/firmware/cmdline.txt`, and reboot. The matrix library pins its refresh thread to core 3, and on-board audio conflicts with the PWM hardware it uses.

## Build and test

```sh
cd engine
make test        # mapping, limiter, pattern, media and playlist tests; runs on any machine
make             # on the Pi: builds the library and ./ledengine
make sim         # Mac/Linux dev: build/ledengine-sim, same engine with a stub matrix
                 # (brew install yaml-cpp nlohmann-json); --dump out.ppm saves the output frame

cd ../web
python3 -m venv --system-site-packages .venv   # --system-site-packages reuses apt's numpy on the Pi
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                  # transcoder tests; fixtures are made with ffmpeg
```

`make test` also writes `build/panel-id.ppm`, a render of what the panel-ID pattern should look like on the wall.

## Run

```sh
sudo ./ledengine --config ../config/single-panel.yaml --pattern panel-id
sudo ./ledengine --config ../config/config.example.yaml --pattern panel-id
```

Patterns:

| `--pattern` | shows | catches |
|---|---|---|
| `panel-id` (default) | border, up-arrow and chain number per panel | wrong chain order or rotation in config |
| `solid --color RRGGBB` | one color | dead pixels, color channel swaps |
| `cycle` | red, green, blue, white, 2s each | missing channels, power sag on white |
| `rows` | one lit row walking down | dead address lines (A-E) |
| `cols` | one lit column walking right | shift-register / data faults |

The engine reads `$LEDPOSTER_DATA/config.yaml` and `$LEDPOSTER_DATA/media` (`LEDPOSTER_DATA` defaults to `/data`). `--play MEDIA_ID` loops one cached item; `--playlist NAME` plays a playlist from the config's `playlists:` section (per-item `duration_s`, `loop`, `shuffle`).

The engine prints its estimated current draw once a second, plus `(limited)` when the limiter is scaling the frame down. Full white on six panels is about 36A unlimited, so the limiter holds it at `power.ceiling_amps` (13A).

## Media cache

`python -m ledposter.transcode SRC OUT_DIR` turns a GIF, APNG, PNG, JPG, WebP, MP4, WebM or MOV (up to 200MB) into `OUT_DIR/frames.rgb` (raw frames, `w*h*3` bytes each), `meta.json` (size, per-frame `durations_ms`, settings) and `thumb.png`. The media id is the folder name under `$LEDPOSTER_DATA/media`.

| option | default | |
|---|---|---|
| `--scale fit\|fill\|stretch` | `fit` | fit letterboxes with black, fill crops, stretch distorts |
| `--filter smooth\|pixel` | auto | area averaging or nearest neighbour; auto picks `pixel` for sources of exactly 128x192 or 64x96 |
| `--crop x,y,w,h` | none | in source pixels, applied before scaling |
| `--position x,y` | `0.5,0.5` | anchor for fit/fill: `0,0` top-left, `1,1` bottom-right |
| `--gamma G` | 2.2 | baked into the frames; keep it equal to `display.gamma` |
| `--dither` | off | 4x4 ordered dither, smooths dark gradients |
| `--fps-cap N` | 30 | faster sources drop frames; total duration is kept |
| `--w W --h H` | 128 192 | canvas size; use `--w 64 --h 64` for `single-panel.yaml` |

Frame timings are kept (GIF delays, video timestamps). Stills are stored as one 10s frame. About 74KB per frame: a 30s 30fps clip is 66MB.

## Testing milestone 2 on the Pi

Pull, then install the new packages and build:

```sh
cd ~/ledposter && git pull && git submodule update
sudo apt install -y nlohmann-json3-dev ffmpeg python3-venv python3-numpy
cd engine && make test && make
cd ../web && python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Expected: `all engine tests passed`, and pytest reports `9 passed`.

Set up a data dir in your home (no root needed for files), with media for the one wired panel (64x64 canvas):

```sh
export LEDPOSTER_DATA=~/lpdata
mkdir -p $LEDPOSTER_DATA/media
cp ../config/single-panel.yaml $LEDPOSTER_DATA/config.yaml
# any GIF/photo/video of yours works; these are generated
ffmpeg -v error -f lavfi -i "testsrc2=s=1920x1080:r=30:d=30" -c:v libx264 -pix_fmt yuv420p /tmp/hd30.mp4
ffmpeg -v error -f lavfi -i smptebars=s=640x480 -frames:v 1 /tmp/bars.jpg
time .venv/bin/python -m ledposter.transcode /tmp/hd30.mp4 $LEDPOSTER_DATA/media/example-gif --w 64 --h 64
.venv/bin/python -m ledposter.transcode /tmp/bars.jpg $LEDPOSTER_DATA/media/example-photo --w 64 --h 64
```

Expected: `900 frames, 30.00s, 64x64 fit/smooth` in under about a minute (the acceptance budget for upload + transcode + play is 2 minutes). Report the `time` output. Then play (`sudo -E` keeps `LEDPOSTER_DATA`):

```sh
cd ../engine
sudo -E ./ledengine --play example-photo     # colour bars, letterboxed: black bands above and below
sudo -E ./ledengine --play example-gif       # moving test card, smooth, loops every 30s
sudo -E ./ledengine --playlist demo          # the 30s clip, then the bars for 15s, repeat
sudo -E ./ledengine --pattern panel-id       # still works; colours now pass through display.gamma
```

Things to check and report:

- Video plays at an even speed with no stutter (top-right clock of the test card counts seconds in real time).
- Colours look natural, not washed out or crushed. Dark tones are darker than in M1 because gamma is applied once by us and the library's own correction is now off. Try `--dither` on a dark photo or gradient and compare.
- A file of yours from each format you care about (phone video, GIF, photo). Failures print a reason and the supported formats.
- Refusal: `sudo -E ./ledengine --config ../config/config.example.yaml --play example-photo` (128x192 canvas, 64x64 media) exits with `media is 64x64 but the canvas is 128x192; transcode it again for this layout`.

With all six panels, transcode without `--w/--h` and use `config.example.yaml`.

## Troubleshooting

- **Blank panel with `adafruit-hat-pwm`**: the GPIO4 to GPIO18 mod isn't there. Use `adafruit-hat`.
- **Only the top half lights, or the image repeats every 32 rows**: E jumper not soldered to 8.
- **Flicker, sparkles or garbage**: raise `gpio_slowdown` (try 3 or 4).
- **Refresh rate too low**: set `show_refresh_rate: true` to see it. Lower `pwm_bits` (8 or 7) or `gpio_slowdown`.
- **`snd_bcm2835` error at start**: audio is still on; see Pi setup.
- **Numbers in the wrong place, or upside down**: fix `matrix.panels` order or `rotate` until `panel-id` shows 1 to 6 upright as in the diagram.
- **Transcode fails with "File content (...) does not match its .x extension"**: the file was renamed; save it with its real extension.
- **Animated WebP fails on the Pi**: Bookworm's ffmpeg 5.1 only decodes still WebP. Convert to GIF or MP4 first (see docs/DEVIATIONS.md).
- **`media is WxH but the canvas is ...`**: the cache was made for another layout. Transcode again with `--w/--h` matching the config.
- **Panels brown out or the Pi resets on white**: lower `ceiling_amps`. Or measure one panel at full white and set `amps_full_white_per_panel` to the real value.
