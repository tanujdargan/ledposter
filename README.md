# ledposter

Software for a 128 x 192 LED wall: six 64x64 P3 HUB75 panels on a Raspberry Pi 3B with an Adafruit RGB Matrix Bonnet. The full spec is in [docs/spec.md](docs/spec.md).

```
engine/   C++ display engine (owns the matrix, frame loop, brightness and limiter, light sensor,
          media playback, effects, clock, text, overlays, the button, control and preview sockets)
web/      Python package `ledposter`: FastAPI web app + REST API, transcoder, engine client
web/static  the PWA (vanilla JS, no build step)
config/   example YAML configs (all hardware tunables live here)
tools/    game_hook.py: sends game events to the wall from a PC (stdlib only)
vendor/   hzeller/rpi-rgb-led-matrix (git submodule, pinned)
docs/     spec, architecture contracts, engine socket protocol (protocol.md), UI references, deviations
```

Status: **milestone 5**, effects, clock, text, overlays, the button and the game hook. Milestone 1 gave config-driven matrix init, the 128x192 canvas mapped onto the 6-panel snake, test patterns and the power limiter. Milestone 2 added the ffmpeg transcoder and engine playback of cached media and playlists. Milestone 3 added the brightness chain, BH1750 auto brightness and the control/preview sockets. Milestone 4 added the web app: login, live preview over WebSocket, controls, media library with upload and transcode status, playlist editor, schedule, settings, and a REST API with OpenAPI at `/docs`. Milestone 5 adds the engine-drawn modes: six generative effects (spider-sense ripple, web-shot trace, UAV radar sweep, plasma, fire, starfield), a clock, an idle screen and a text scroller, each with settings (speed, colors, ...) that the engine describes (`list_modes`) and the web app's mode picker is built from; overlays on top of any mode (game hook events `kill`, `killstreak`, `death`, `win` via `POST /api/event` and `tools/game_hook.py`, and `show_text` messages); and the push button (short press: next mode or playlist item, 2s: display on/off, 10s: Wi-Fi setup request).

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
- Push button (momentary, normally open) between **GPIO25** and GND, no resistor (the engine turns on the Pi's internal pull-up, so pressed reads low):

  | Button | Pi |
  |---|---|
  | one leg | GPIO25 (pin 22) |
  | other leg | GND (pin 20, right next to it) |

  GPIO25 is free with both `adafruit-hat` and `adafruit-hat-pwm` (they use GPIO 4/18, 5, 6, 12, 13, 16, 17, 20, 21, 22, 23, 24, 26, 27; the BH1750 uses 2 and 3). The bonnet covers the header: solder the two wires to pins 22 and 20 from under the Pi, or put a stacking header between the Pi and the bonnet and use its pins. Another free pin works too (7, 8, 9, 10, 11, 19): set `button.gpio` to its BCM number. Keep the wires short or twisted; the default 30ms `debounce_ms` handles normal contact bounce.

## Pi setup (once)

```sh
sudo apt install -y git build-essential libyaml-cpp-dev nlohmann-json3-dev ffmpeg python3-venv python3-numpy
git clone --recurse-submodules git@github.com:tanujdargan/ledposter.git
```

In `/boot/firmware/config.txt` set `dtparam=audio=off`. Then append `isolcpus=3` to the single line in `/boot/firmware/cmdline.txt`, and reboot. The matrix library pins its refresh thread to core 3, and on-board audio conflicts with the PWM hardware it uses.

## Build and test

```sh
cd engine
make test        # mapping, limiter, brightness curve, pattern, media, playlist, IPC parsing, effect,
                 # text, overlay and button tests; runs on any machine (needs nlohmann-json, header
                 # only, and the vendor submodule for its font parser and fonts). With AddressSanitizer
                 # off the Pi, so an out-of-range pixel write fails the run
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

The engine reads `$LEDPOSTER_DATA/config.yaml` and `$LEDPOSTER_DATA/media` (`LEDPOSTER_DATA` defaults to `/data`). `--play MEDIA_ID` loops one cached item; `--playlist NAME` plays a playlist from the config's `playlists:` section (per-item `duration_s`, `loop`, `shuffle`). `--effect NAME` starts an effect with its default settings (`spider-sense`, `web-shot`, `uav-radar`, `plasma`, `fire`, `starfield`), `--mode clock` or `--mode idle` the clock or idle screen, `--text "Hello"` the text scroller.

Text, the clock and on-wall messages use the BDF fonts that come with rpi-rgb-led-matrix (`vendor/rpi-rgb-led-matrix/fonts`), parsed by that library's own font code. The engine loads them at start from the checkout it was built in; set `fonts.dir` if the fonts live elsewhere. The clock uses the Pi's time zone (`sudo raspi-config nonint do_change_timezone Europe/London`, then restart the engine). The idle screen is a dark, slow blue plasma with a dim clock: something for "on, but nobody is watching", which the schedule (web app) can switch to.

The engine prints its estimated current draw and output brightness once a second, plus `(limited)` when the limiter is turning the frame down. Full white on six panels is about 36A unlimited, so the limiter holds it at `power.ceiling_amps` (13A): output brightness 36%, 12.96A.

The flags only pick the starting mode. While it runs, the engine listens on `engine.socket` for commands (`status`, `set_brightness`, `set_auto_brightness`, `power`, `set_mode`, `list_modes`, `overlay`, `show_text`, `ack_wifi_setup`, `reload_config`) and streams its output on `engine.preview_socket`; both are in [docs/protocol.md](docs/protocol.md). From a shell, with `socat`:

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
## Web app

```sh
cd web
.venv/bin/pip install -r requirements.txt
LEDPOSTER_DATA=~/lpdata .venv/bin/python -m ledposter --port 8080   # port defaults to config web.port
```

Open `http://<pi>:8080/`. The first visit asks for an admin password (stored as a scrypt hash in `config.yaml`); everything under `/api` and the app itself need the session cookie or an API token (`Authorization: Bearer lp_...`, created in Settings). The API is documented at `/docs` (OpenAPI at `/openapi.json`); every button in the UI is one of those endpoints. The live preview is a WebSocket at `/api/preview`: one text message with the engine status, then one binary frame of `w*h*3` RGB bytes, up to 15 a second.

Uploads go to `$LEDPOSTER_DATA/media/<id>/` and are transcoded one at a time by a background worker with `ledposter.transcode`; the item's settings (scale, filter, crop, position, speed, dither) can be changed in the editor, which re-transcodes. Playlists are written to the config's `playlists:` section in the engine's format, followed by `reload_config`. The schedule (on/off times, quiet-hours brightness cap) is enforced by the web app's own scheduler task. If the engine is down the app keeps working for the library and settings and shows "engine offline" until it reconnects.

The Modes screen is built from the engine's `list_modes` (`GET /api/modes`): media, the six effects, clock, text, idle and the test patterns, each with its settings sheet (speed slider, colour list, text with a character count, toggles). Changes apply live. While the engine is offline it shows the last list it got.

### Game hook

`POST /api/event` with `{"event": "kill"}` (also `killstreak`, `death`, `win`) plays a 1.5 to 3s overlay on top of whatever the wall shows. Use an API token from Settings (`Authorization: Bearer lp_...`). Unknown events get a 400; more than 10 events in 5 seconds from one token get a 429. From a PC, `tools/game_hook.py` sends them (Python 3.8+, no packages):

```sh
export LEDPOSTER_URL=http://ledposter.local LEDPOSTER_TOKEN=lp_...
python3 tools/game_hook.py kill
your-detector | python3 tools/game_hook.py --stdin    # one event name per line
```

How a game's kills and deaths are detected is up to you (out of scope for v1): call `game_hook.send("kill")` from your own script, or print event names into `--stdin`.

Dev without a Pi: `python tests/fake_engine.py /tmp/lpsock` serves the two sockets with a moving test image; point `engine.socket`/`engine.preview_socket` at them. Or build the real engine with `cd engine && make sim`.

## Testing milestone 4 on the Pi

Pull, install the new Python packages and run the tests:

```sh
cd ~/ledposter && git pull && git submodule update
cd web && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Expected: `46 passed` (transcoder, auth, config, media, playlists, schedule, preview relay against a fake engine).

Start the engine (M3) and then the web app on port 8080 (systemd units come in M8), with the same data dir:

```sh
export LEDPOSTER_DATA=~/lpdata
cd ~/ledposter/engine && sudo -E ./ledengine &            # or your usual way of running it
cd ~/ledposter/web && .venv/bin/python -m ledposter --port 8080
```

On your phone (same Wi-Fi) open `http://<pi-ip>:8080/` and check:

- First visit: "Set an admin password" screen; after Continue you land on Home. Open the URL in a private window: it asks to sign in, and `curl http://<pi-ip>:8080/api/status` answers `401`.
- Home: the preview shows what the panel shows (dots view, toggle to flat), with `mode · fps · A` pills under it. Move the brightness slider: the panel follows live, the status `brightness.user` changes. Power toggle in the header turns the panel off and on.
- Modes: tap Panel ID, Colour cycle, Row sweep: the panel switches right away and the card gets the amber active state. Solid colour's "..." opens the colour sheet.
- Library: upload a GIF and a phone video. The tile shows "Converting..." then the thumbnail with duration. A 1080p 30s MP4 should be ready in under 2 minutes (report the time). Upload a `.txt` renamed `.mp4`: the tile gets a red "!" and the sheet says what went wrong plus the formats that work.
- Tap a ready item: the editor previews it; switch Fit/Fill, move the position sliders (the preview updates client-side), Save re-converts it. "Play on wall" shows it on the panel.
- Playlist: New, + Add two items, drag the handle to reorder, tap a duration chip, Play. The engine's log shows the playlist name; `config.yaml` has it under `playlists:`.
- Schedule: set Off to two minutes from now and enable it: the panel turns off at that time. Enable quiet hours covering now with max 10%: the slider's value stays but the panel dims and Home says "Quiet hours: capped at 10%".
- Settings: change the admin password (other devices get signed out), create an API token and try `curl -H "Authorization: Bearer lp_..." http://<pi-ip>:8080/api/status`, revoke it and the same call answers `401`. Matrix: change slowdown and Save: the toast says a restart is needed and `config.yaml` has the new value. Panel ID test shows the pattern.
- Install: Chrome/Safari "Add to Home Screen" gives a standalone dark app. Stop the engine (`Ctrl-C`): the app shows "Engine offline", the library still opens; start it again and the preview resumes within a few seconds.

Report: the transcode time for your biggest video, whether the preview keeps up with the panel (it should be within about 200ms), and anything that looked wrong on your phone's screen size.

## Testing milestone 5 on the Pi

Wire the button as in [Wiring assumptions](#wiring-assumptions) (GPIO25 pin 22, GND pin 20). Pull, build and test:

```sh
cd ~/ledposter && git pull && git submodule update --init
cd engine && make test && make && make sim
cd ../web && .venv/bin/python -m pytest -q
```

Expected: `all engine tests passed`, and pytest reports `63 passed` (the engine socket tests now also cover `list_modes`, every new mode, overlays, `show_text` and the simulated button; the web tests cover the mode picker, `/api/event` and `tools/game_hook.py`, one of them against `build/ledengine-sim`).

Add the new `button:` section to the data dir config (copy it from `config/single-panel.yaml`, or copy the whole file again and redo your edits), then start the engine on the one wired panel:

```sh
export LEDPOSTER_DATA=~/lpdata
cd ~/ledposter/engine && sudo -E ./ledengine --effect plasma
```

Expected log: `button: GPIO25 on /dev/gpiochip0`, no `fonts: ... missing` line. In a second terminal, with the `ctl` helper from milestone 3:

```sh
ctl() { echo "$1" | sudo socat - UNIX-CONNECT:/run/ledposter/engine.sock | jq -c .; }
```

Things to check and report:

1. **Effects**: each of these for ~20s; the panel shows the effect, `status` shows `"fps"` close to 30 (report it per effect, and `top -H -p $(pgrep ledengine)` CPU for fire and plasma):
   ```sh
   for e in spider-sense web-shot uav-radar plasma fire starfield; do
     ctl "{\"cmd\":\"set_mode\",\"mode\":\"effect\",\"effect\":\"$e\"}"; sleep 20
     ctl '{"cmd":"status"}' | jq -c '[.mode_name, .fps, .amps]'
   done
   ```
   Spider-sense: red/blue ring shockwaves from random points. Web-shot: a white line shoots in, strands burst out, a web is spun, fades, repeats every 6s. UAV radar: green sweep with a fading trail, red blips lighting up as it passes. Plasma: flowing pink/blue/cyan blobs. Fire: flames from the bottom edge. Starfield: stars flying out from the middle. Report anything that looks wrong, too dim, or flickers.
2. **Settings**: `ctl '{"cmd":"set_mode","mode":"effect","effect":"fire","settings":{"speed":3,"height":0.9,"colors":["0000ff","00ffff","ffffff"]}}'` gives tall, fast blue fire. `{"cmd":"set_mode","mode":"effect","effect":"fire","settings":{"speed":9}}` answers `'speed' must be a number from 0.1 to 5` and changes nothing.
3. **Clock**: `ctl '{"cmd":"set_mode","mode":"clock","settings":{"hour24":false,"date":true,"color":"ff8000"}}'` shows the local time in orange with AM/PM and the date; check it matches `date` on the Pi.
4. **Text**: `ctl '{"cmd":"set_mode","mode":"text","text":"Hello from the LED wall!","color":"00ff00","speed":1.5}'` scrolls right to left, smoothly, and starts again after it leaves.
5. **Idle**: `ctl '{"cmd":"set_mode","mode":"idle"}'`: a dark blue slowly moving background with a dim clock. Report whether it is too dim or too bright in your room (`settings: {"level": 0.6}` to compare).
6. **Overlays**: on top of any mode, `ctl '{"cmd":"overlay","event":"kill"}'` (also `killstreak`, `death`, `win`): a 1.5 to 3s animation, then the mode shows as before. `ctl '{"cmd":"show_text","text":"192.168.1.50","seconds":10}'` shows the text in a band for 10s. With `set_mode` pattern `solid` `ffffff` and `ceiling_amps: 3.0` as in milestone 3, an overlay never pushes `amps` over 3.
7. **Button**: short presses step through `button.cycle` (plasma, fire, spider-sense, web-shot, radar, starfield, clock; the `demo` playlist goes item by item; then idle). Hold 2 to 10s and let go: the display goes off; again: back on. Hold 10s: "Wi-Fi setup" appears while you are still holding, and `ctl '{"cmd":"status"}' | jq -c '[.wifi_setup_requested, .wifi_setup_count]'` shows `[true,1]`. The log prints `button: short press` etc. for each. Report any missed or double presses.
8. **List**: `ctl '{"cmd":"list_modes"}' | jq '.modes[].mode'` lists all modes; `jq '.modes[] | select(.effect=="plasma")'` shows the plasma settings schema.

9. **Web app** (engine still running; start the web app as in milestone 4, `--port 8080`): the Modes tab shows Media, Effects, Clock and text, Test patterns. Tap Fire: the panel switches and the card turns amber. Its "..." sheet: drag Speed, change or add a colour, Flame height; each change shows on the panel within about a second. Text scroller: type a message and press Enter. After the update, an installed home-screen app may need one reopen to load the new screens.
10. **Game hook**: create an API token in Settings, then from a PC on the same network:
    ```sh
    python3 tools/game_hook.py --url http://<pi-ip>:8080 --token lp_... kill
    ```
    prints `kill: ok, 1.5s on the wall` and the panel shows the hit marker and "KILL". `... dance` is refused by the script; `curl -s -X POST -H "Authorization: Bearer lp_..." -H 'Content-Type: application/json' -d '{"event":"dance"}' http://<pi-ip>:8080/api/event` answers 400. Sending 12 events in a row: the last two answer `429: Too many events, slow down (rate limited)`.

With all six panels, use `config.example.yaml`; the effects fill the 128x192 canvas.

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
- **`button: /dev/gpiochip0: ...` or `button: GPIO25 ...: Device or resource busy`**: another program holds the line (`gpioinfo` from `gpiod` shows the owner), or `button.gpio` is a pin the matrix uses. Pick a free pin (Wiring assumptions). `status` shows `"button": "missing"`; the wall keeps running.
- **Button does nothing**: check the log for `button: GPIO25 on /dev/gpiochip0` at start. With `gpiod` installed, `gpioget gpiochip0 25` (engine stopped) reads 1 released and 0 pressed. Phantom presses: longer or untwisted wires picking up noise; raise `debounce_ms`.
- **`fonts: N of 6 missing in ...`**: the vendor submodule isn't checked out (`git submodule update --init`) or `fonts.dir` is wrong. Without fonts, text and clock modes can't be set and overlays show no words.
- **Clock shows the wrong time**: set the time zone (`sudo raspi-config nonint do_change_timezone ...`) and restart the engine; check `timedatectl` shows NTP synchronized.
- **`socat: ... Permission denied` from a client**: the sockets are mode 660, owned by whoever runs the engine. Use `sudo` for now (the systemd setup in milestone 8 gives the web app its group).
