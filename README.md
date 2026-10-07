# ledposter

Software for a 128 x 192 LED wall: six 64x64 P3 HUB75 panels on a Raspberry Pi 3B with an Adafruit RGB Matrix Bonnet. The full spec is in [docs/spec.md](docs/spec.md).

```
engine/   C++ display engine (owns the matrix, frame loop, brightness and limiter, light sensor,
          media playback, control and preview sockets)
web/      Python package `ledposter` (so far: the media transcoder)
config/   example YAML configs (all hardware tunables live here)
vendor/   hzeller/rpi-rgb-led-matrix (git submodule, pinned)
docs/     spec, architecture contracts, engine socket protocol (protocol.md), deviations
```

Status: **milestone 3**, brightness. Milestone 1 gave config-driven matrix init, the 128x192 canvas mapped onto the 6-panel snake, test patterns and the power limiter. Milestone 2 adds the ffmpeg transcoder (any supported upload to cached raw 128x192 RGB24 frames with their timings, fit/fill/stretch, smooth/pixel filters, crop and position, gamma, optional dithering) and engine playback of cached media and playlists. Milestone 3 adds the brightness chain (user brightness, BH1750 auto brightness on a log lux curve with smoothing, `min(user, auto)`, then the power limiter so the estimate never exceeds `power.ceiling_amps`), and the engine's two Unix sockets: control (newline-delimited JSON commands and `status` with the current estimate) and preview (status line + output frame, up to 15fps). See [docs/protocol.md](docs/protocol.md).

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
- BH1750 light sensor on the Pi's I2C1 (the bonnet passes these pins through):

  | BH1750 | Pi |
  |---|---|
  | VCC | 3.3V (pin 1) |
  | GND | GND (pin 9) |
  | SDA | GPIO2 / SDA (pin 3) |
  | SCL | GPIO3 / SCL (pin 5) |
  | ADDR | GND or unconnected: address `0x23` (tied to 3.3V it is `0x5c`; set `sensors.bh1750.address`) |

  Put it where it sees the room, not the wall's own light.

## Pi setup (once)

```sh
sudo apt install -y git build-essential libyaml-cpp-dev nlohmann-json3-dev ffmpeg python3-venv python3-numpy
git clone --recurse-submodules git@github.com:tanujdargan/ledposter.git
```

In `/boot/firmware/config.txt` set `dtparam=audio=off`. Then append `isolcpus=3` to the single line in `/boot/firmware/cmdline.txt`, and reboot. The matrix library pins its refresh thread to core 3, and on-board audio conflicts with the PWM hardware it uses.

## Build and test

```sh
cd engine
make test        # mapping, limiter, brightness curve, pattern, media, playlist and IPC parsing tests;
                 # runs on any machine (needs nlohmann-json, header only)
make             # on the Pi: builds the library and ./ledengine
make sim         # Mac/Linux dev: build/ledengine-sim, same engine with a stub matrix
                 # (brew install yaml-cpp nlohmann-json); --dump out.ppm saves the output frame

cd ../web
python3 -m venv --system-site-packages .venv   # --system-site-packages reuses apt's numpy on the Pi
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                  # transcoder tests (fixtures made with ffmpeg), and engine
                                               # socket tests against build/ledengine-sim (skipped if not built)
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

The engine prints its estimated current draw and output brightness once a second, plus `(limited)` when the limiter is turning the frame down. Full white on six panels is about 36A unlimited, so the limiter holds it at `power.ceiling_amps` (13A): output brightness 36%, 12.96A.

The flags only pick the starting mode. While it runs, the engine listens on `engine.socket` for commands (`status`, `set_brightness`, `set_auto_brightness`, `power`, `set_mode`, `reload_config`) and streams its output on `engine.preview_socket`; both are in [docs/protocol.md](docs/protocol.md). From a shell, with `socat`:

```sh
echo '{"id":1,"cmd":"status"}' | sudo socat - UNIX-CONNECT:/run/ledposter/engine.sock
echo '{"id":2,"cmd":"set_brightness","value":40}' | sudo socat - UNIX-CONNECT:/run/ledposter/engine.sock
```

Brightness: `effective = min(brightness.user, auto)` where auto comes from the BH1750 through `brightness.auto_curve` (or is skipped when `brightness.auto` is false or the sensor is missing), then the limiter lowers it per frame if the estimate would pass the ceiling. The result goes to the panels as the matrix library's brightness percent.

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

## Testing milestone 3 on the Pi

Enable I2C and install the tools (once):

```sh
sudo raspi-config nonint do_i2c 0      # or: sudo raspi-config > Interface Options > I2C > Yes
sudo apt install -y i2c-tools socat jq
sudo reboot
```

With the BH1750 wired as in [Wiring assumptions](#wiring-assumptions):

```sh
i2cdetect -y 1
```

Expected: `23` in the grid (row `20:`, column `3`). Nothing there means a wiring problem; fix it before going on.

Pull, build and test:

```sh
cd ~/ledposter && git pull && git submodule update
cd engine && make test && make && make sim
cd ../web && .venv/bin/python -m pytest -q
```

Expected: `all engine tests passed`, and pytest reports `18 passed` (the engine socket tests run against `build/ledengine-sim`).

Update the data dir config (the new `brightness:`, `sensors:` and `engine:` sections), then run the engine on the one wired panel. One panel at full white is only about 6A, so lower the ceiling to see the limiter work:

```sh
export LEDPOSTER_DATA=~/lpdata
cp ../config/single-panel.yaml $LEDPOSTER_DATA/config.yaml
sed -i 's/ceiling_amps: 13.0/ceiling_amps: 3.0/' $LEDPOSTER_DATA/config.yaml
cd ../engine && sudo -E ./ledengine --pattern solid --color ffffff
```

Expected log: `light sensor: ok, N lux`, then once a second `est 3.00A (limited), brightness 50%`. In a dim room auto brightness may already be under 50%; then the line shows that lower brightness without `(limited)`.

In a second terminal, a status helper:

```sh
ctl() { echo "$1" | sudo socat - UNIX-CONNECT:/run/ledposter/engine.sock | jq -c .; }
ctl '{"id":1,"cmd":"status"}'
```

Things to check and report (paste the `status` lines):

1. **Sensor**: `"sensor":"ok"` and a plausible `lux` (room light: 50 to 500; phone torch on it: thousands; covered with a finger: under 5).
2. **Auto brightness**: `while sleep 1; do ctl '{"cmd":"status"}' | jq -c '[.lux, .brightness]'; done`. Cover the sensor: `auto` glides down to 10 over a few seconds (no jump), and the panel dims. Torch on it: `auto` goes to 100. Report whether the speed (`smoothing_s`) and the curve (`lux_low`, `lux_high`, `min_pct`) feel right in the room.
3. **User brightness**: `ctl '{"cmd":"set_brightness","value":20}'`: the panel dims, `effective` is at most 20, and `amps` drops to about 1.2A, `limited` false. `ctl '{"cmd":"set_auto_brightness","enabled":false}'`: brightness follows only the user setting.
4. **Limiter**: back to `set_brightness` 100 with auto off. `amps` stays at or under 3.0 with `limited: true` and `amps_unlimited` about 6. If you have a USB-C PD meter or a clamp meter on the 5V line, report the real current at full white with `ceiling_amps` back at 13 (the estimate assumes 6A per panel at full white, `amps_full_white_per_panel`).
5. **Power off/on**: `ctl '{"cmd":"power","on":false}'` turns the panel black while the log keeps running; `on:true` brings it back.
6. **Modes over the socket**: `ctl '{"cmd":"set_mode","mode":"pattern","pattern":"panel-id"}'`, then `{"cmd":"set_mode","mode":"media","media":"example-gif"}` and `{"cmd":"set_mode","mode":"playlist","playlist":"demo"}`.
7. **Preview**: `sudo socat -u UNIX-CONNECT:/run/ledposter/frames.sock - | head -c 200000 | head -1` prints one status line; the bytes after it are the frame.
8. **Sensor unplugged while running**: pull the BH1750's VCC wire. The log says `light sensor: ... auto brightness follows the user setting` once, the wall keeps going at the user brightness, `status` shows `"lux":null,"sensor":"missing"`. Plug it back: within about 5s, `light sensor: ok`.
9. **Reload**: edit `brightness.user` in `$LEDPOSTER_DATA/config.yaml`, then `ctl '{"cmd":"reload_config"}'` applies it (`restart_needed: []`). Change `gpio_slowdown` and reload: `restart_needed: ["matrix"]`.

With all six panels use `config.example.yaml` as is (13A ceiling): full white shows `est 12.96A (limited), brightness 36%`.

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
- **`light sensor: /dev/i2c-1: No such file or directory`**: I2C is off. `sudo raspi-config nonint do_i2c 0` and reboot.
- **`light sensor: no answer at 0x23`**: `i2cdetect -y 1` doesn't show `23` either. Check VCC is 3.3V, SDA/SCL aren't swapped, and the ADDR pin (high = `0x5c`, set `sensors.bh1750.address`). Long wires (over about 30cm) need slower I2C or shorter cables.
- **Auto brightness pumps or reacts to the wall itself**: the sensor sees the panels. Turn it away from the wall, or raise `smoothing_s`.
- **Wall too dim at night / too bright by day**: tune `brightness.auto_curve` (`min_pct`, `lux_low`, `lux_high`) and `reload_config`.
- **`control socket off: ... Permission denied`**: the engine can't create `/run/ledposter`. Run it with `sudo`, or point `engine.socket` and `engine.preview_socket` at a writable path for dev.
- **`another engine is already listening here`**: an engine is still running (`pgrep -a ledengine`); stop it first.
- **`socat: ... Permission denied` from a client**: the sockets are mode 660, owned by whoever runs the engine. Use `sudo` for now (the systemd setup in milestone 8 gives the web app its group).
