# Acceptance

The nine criteria from `docs/spec.md`, each with the exact procedure on the installed wall and what to record. `tools/acceptance.sh` automates the measurable ones from a shell on the Pi (`ssh` in key-only mode, or keyboard + HDMI) and prints a PASS/FAIL table; the rest are eyes-on checks it prompts for. Run it after `system/install.sh` and a reboot, with all six panels.

```sh
sudo /data/app/current/tools/acceptance.sh            # everything it can measure, ~4 minutes
sudo /data/app/current/tools/acceptance.sh boot        # one check (boot, refresh, limiter, transcode, preview, ha, panels)
```

It needs the admin password once (`LEDPOSTER_PASSWORD=...` or it asks) for the API calls, and `mosquitto-clients` on the Pi (`sudo apt install mosquitto-clients`) for the Home Assistant check, with the broker from Settings. Record the table it prints plus the notes below in the test report.

## 1. Cold boot to first frame in under 30s

Procedure: power off at the wall, wait 10s, power on, start a stopwatch at the same moment. Note the time the boot screen (`LED POSTER` + spinner) appears. Then on the Pi:

```sh
systemd-analyze                                   # kernel + userspace time to multi-user
systemd-analyze critical-chain ledposter-engine.service
journalctl -b -u ledposter-engine -o short-monotonic | grep -m1 'first frame'
```

Record: the stopwatch time, and the monotonic seconds of `first frame` (time since the kernel started; add about 2 to 4s of firmware before the kernel, which the stopwatch includes). `acceptance.sh boot` reports the journal number. Pass: stopwatch under 30s. If it is over, see the README's "Boot time" troubleshooting (services still enabled, `isolcpus`, SD card speed).

## 2. Refresh rate of 150Hz or more with all 6 panels at default settings

Procedure: `acceptance.sh refresh` stops the engine, runs it for 8s with `show_refresh_rate: true` on a copy of the config, parses the library's refresh output, and restarts the service. Defaults are `gpio_slowdown 2`, `pwm_bits 9`, `adafruit-hat-pwm`. Record the Hz it prints. Then look at the wall from 3 metres on Panel ID and a solid grey (`Solid colour`, `#808080`): no flicker by eye, no rolling bands when filmed with a phone at 30fps is a bonus note. Pass: >= 150Hz and no visible flicker. If the rate is low, lower `pwm_bits` to 8 (Settings > Matrix) and re-run.

## 3. Current never exceeds the limiter ceiling on a full-white test frame

Procedure: `acceptance.sh limiter` sets `Solid colour` white at user brightness 100 with auto-brightness off, samples the engine's `status` 30 times over 6s and checks every `amps <= ceiling_amps` with `limited: true` and `amps_unlimited` about 36A. Record the max `amps`, `ceiling_amps` (13.0 default), `brightness.output` (36%). If a clamp or USB-PD meter is available, also record the measured 5V current on the panel supply at the same time: it should be at or under the ceiling, which calibrates `amps_full_white_per_panel`. Pass: every sample under the ceiling.

## 4. A 1080p 30s MP4 uploads, transcodes and plays within 2 minutes

Procedure: `acceptance.sh transcode` generates a 1920x1080 30s H.264 clip with ffmpeg on the Pi (a `testsrc2` pattern, about 10MB), uploads it through `POST /api/media` with the clock started before the upload, polls `/api/media/{id}` until `ready` or `failed`, then `POST /api/mode` to play it and confirms the engine `status` shows `mode: media` with that id. Record the total seconds. Do it once more from a phone over Wi-Fi with a real 1080p 30s video (the upload itself is part of the budget): Library > + > pick the clip, note the time from tapping Upload to the tile showing its duration. Pass: both under 120s.

## 5. Preview in the app matches the wall frame for frame within 200ms

Procedure: `acceptance.sh preview` connects to the engine's preview socket and the web app's `/api/preview` WebSocket at the same time, in `Colour cycle` (a new colour every 2s), and measures the delay between the engine emitting a frame whose status changes colour and the WebSocket delivering the same frame bytes; it reports the median and max over 10 changes. That is the server side of the latency (socket -> relay). Then on a phone next to the wall: Home, Colour cycle; the phone's preview and the wall change colour visibly together (a phone video of both at 60fps gives the number: count frames between the two changes, 12 frames = 200ms). Record: median/max from the script, and the observed phone delay. Pass: under 200ms.

## 6. Pulling power 20 times during playback never corrupts the system or media

Procedure: play a playlist with a video and a GIF (`Playlist > Play`), with auto-brightness on. Pull the plug 20 times at random moments at least 40s apart (so each boot completes and playback resumes), including a few during an upload or a settings change. After the last boot:

```sh
sudo /data/app/current/tools/acceptance.sh health   # services active, overlay on, /data clean, cache files consistent
sudo journalctl -b -p err                           # nothing from ledposter-*
sudo tune2fs -l /dev/disk/by-label/ledpostdata | grep -i 'state\|mount count'
```

The `health` check verifies: both services `active`, `/proc/cmdline` has the overlay, `/data` is mounted `noatime`, every media item with `status: ready` has `frames.rgb` of exactly `frames * w * h * 3` bytes and a `meta.json`, no `.tmp`/`.part` files anywhere under `/data`, `config.yaml` parses and `/data/app/current` points at a release with `.ready`. Record: the output, and whether the last mode came back on its own after each boot (it should: `last_mode` in the config). Pass: all green after the 20th pull.

## 7. Fresh unit joins a new Wi-Fi network through the setup hotspot from a phone in under 3 minutes

Procedure: Settings > Factory reset (type the hostname) on a wall that is set up, or a freshly installed one. Start the stopwatch when the wall shows the setup text (`Wi-Fi setup / Join LEDWall-Setup`). On the phone: join `LEDWall-Setup` with the card password, the captive portal opens (or `http://10.42.0.1/`), set the admin password, pick the network, enter its password, wait for `Joined <ssid>` on the wall, move the phone back to the home Wi-Fi, open `http://ledposter.local/`, sign in. Stop the stopwatch when Home shows the live preview. Record the time and anything that was unclear on the phone. Pass: under 180s.

## 8. HA discovers the wall and can turn it on/off, set brightness and change effect

Procedure (needs the M7 MQTT client configured in Settings > Integrations with the HA broker): `acceptance.sh ha` subscribes with `mosquitto_sub` for 5s and checks the retained discovery topics exist (`homeassistant/light/<id>/config`, the current and ambient-light sensors, the playlist select). Then in Home Assistant: Settings > Devices > MQTT shows "LED Poster"; toggle the light (the wall goes black and back), set brightness 30% (the engine `status` shows `brightness.user: 30`), pick an effect from the list (the wall changes, `status.mode_name` matches). Record each result. Pass: discovered and all three controls work.

## 9. Panel ID mode shows numbers 1 to 6 upright in the correct positions

Procedure: Settings > Panel ID test (or `acceptance.sh panels`, which sets the mode and asks). Standing in front of the wall:

```
[1][2]
[4][3]
[5][6]
```

each number upright with an up-arrow pointing up, a border around every panel. Record a photo. Pass: exactly that layout. Anything else is a `matrix.panels` order/rotation issue (README: Wiring).

## Recording

Keep the printed table from `acceptance.sh` (it ends with `ACCEPTANCE: n/9 automated checks passed`), the stopwatch times (1, 4, 7), the photo (9), the HA results (8) and the clamp reading if any (3) together with `git describe` of the release (`Settings > Software`).
