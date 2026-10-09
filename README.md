# ledposter

Software for a 128 x 192 LED wall: six 64x64 P3 HUB75 panels on a Raspberry Pi 3B with an Adafruit RGB Matrix Bonnet. The full spec is in [docs/spec.md](docs/spec.md).

```
engine/   C++ display engine (owns the matrix, frame loop, brightness and limiter, light sensor,
          media playback, effects, clock, text, overlays, the button, control and preview sockets)
web/      Python package `ledposter`: FastAPI web app + REST API, transcoder, engine client
web/static  the PWA (vanilla JS, no build step)
config/   example YAML configs (all hardware tunables live here)
tools/    game_hook.py (sends game events from a PC, stdlib only), acceptance.sh (on-device acceptance checks)
system/   install.sh and what it installs on the Pi: systemd units, polkit rules, the root-side scripts
          (bin/ledposter-net, bin/ledposter-sys), avahi, dnsmasq, journald/NetworkManager/apt/sysctl drop-ins
vendor/   hzeller/rpi-rgb-led-matrix (git submodule, pinned)
docs/     spec, architecture contracts, engine socket protocol (protocol.md), UI references, deviations, ACCEPTANCE.md
```

Status: **milestone 8**, hardening and installer: feature complete against docs/spec.md. Milestone 1 gave config-driven matrix init, the 128x192 canvas mapped onto the 6-panel snake, test patterns and the power limiter. Milestone 2 added the ffmpeg transcoder and engine playback of cached media and playlists. Milestone 3 added the brightness chain, BH1750 auto brightness and the control/preview sockets. Milestone 4 added the web app: login, live preview over WebSocket, controls, media library with upload and transcode status, playlist editor, schedule, settings, and a REST API with OpenAPI at `/docs`. Milestone 5 added the engine-drawn modes: six generative effects (spider-sense ripple, web-shot trace, UAV radar sweep, plasma, fire, starfield), a clock, an idle screen and a text scroller, each with settings the engine describes (`list_modes`) and the web app's mode picker is built from; overlays on top of any mode (game hook events `kill`, `killstreak`, `death`, `win` via `POST /api/event` and `tools/game_hook.py`, and `show_text` messages); and the push button (short press: next mode or playlist item, 2s: display on/off, 10s: Wi-Fi setup request). Milestone 6 added networking: `ledposter.local` over Avahi, stable DHCP, an optional static IP with automatic rollback, hostname from Settings, the IP on the wall at boot, and the `LEDWall-Setup` hotspot with a captive portal for first-time Wi-Fi setup. Milestone 7 added Home Assistant over MQTT (discovery: a light with brightness and effects, current and light sensors, a playlist select) and DDP / E1.31 streaming input that takes over the wall while a sender plays; a button double press shows the IP. Milestone 8 hardens the Pi: `system/install.sh` sets up a fresh Raspberry Pi OS Lite card from scratch (dedicated `ledwall` user, `/data` partition, systemd units, polkit, ufw, SSH key-only or off, journald in RAM, read-only root overlay), the engine starts on a boot screen and the web app restores the last mode, software updates to a tagged release with automatic rollback (Settings > Software), a weekly security-update window, factory reset, and `docs/ACCEPTANCE.md` with `tools/acceptance.sh` for the spec's acceptance criteria.

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

## Install on a fresh Pi (milestone 8)

What you need: a Raspberry Pi 3B, a 16GB or bigger SD card, Raspberry Pi OS **Lite 64-bit (Trixie)**, the hardware wired as above, a GitHub account with read access to this private repo, and a computer with Docker (Docker Desktop on a Mac) that builds the release, so the Pi never compiles.

**1. Flash the card.** Raspberry Pi Imager > Raspberry Pi OS Lite (64-bit). In the OS customisation set a hostname (anything; the installer renames it `ledposter`), your user and password, your Wi-Fi, and **enable SSH with public-key authentication** (paste your public key). The user can have any name, `ledposter` included, except `ledwall` and `ledwall-build`: those are the wall's own unprivileged accounts, and the installer stops if a login user already has one of those names. It never changes the login user. Write the card.

**2. Before the first boot, stop the root partition from eating the whole card.** Raspberry Pi OS grows the root filesystem to fill the card on its first boot, and a mounted ext4 root cannot be shrunk later. The wall keeps media, config and the software on a separate `/data` partition, so the root must stay small. With the card still in your computer open the `bootfs` partition, edit `cmdline.txt` and delete the text ` init=/usr/lib/raspi-config/init_resize.sh` (one line, keep the rest). Eject the card. (Skipped it? `install.sh` tells you, and `docs/DEVIATIONS.md` lists the other options.)

**3. First boot.** Put the card in, power the Pi (the panels can stay off), wait a minute, and check `ssh you@<hostname>.local` works with your key.

**4. Build and install from your computer (recommended).** In your checkout of this repo, with Docker running:

```sh
tools/deploy.sh you@<hostname>.local --install-args "--ssh-key 'ssh-ed25519 AAAA... you@laptop'"   # HEAD; key-only SSH
tools/deploy.sh you@<hostname>.local --tag <tag> --install-args "--ssh disabled"   # a release tag; no SSH after the reboot
```

`deploy.sh` runs `tools/build-release.sh` (a `debian:trixie` arm64 container with the installer's packages builds the engine, runs `make test`, makes the venv at its final path `/data/app/releases/<version>` and byte-compiles: `dist/ledposter-<version>-arm64.tar.zst` + `.sha256`, reused next time), copies it and a checkout of the same version to `~/ledposter-deploy/` on the Pi, and runs `sudo ./system/install.sh --prebuilt ...` there with your `--install-args`, streaming the output. sudo asks for your password in the same terminal (`ssh -t`); `--sudo-pass-file FILE` sends the first line of FILE to `sudo -S` instead, never on a command line. By hand: `tools/build-release.sh [--tag TAG]`, copy the tarball, its `.sha256` and a checkout of that tag to the Pi, then `sudo ./system/install.sh --prebuilt ../ledposter-<version>-arm64.tar.zst ...` in the checkout. The tarball must be the checkout's version (`git describe --tags --always`), its sha256 is checked when the `.sha256` file is next to it, and the build packages (compiler, `-dev` headers) are not installed: the Pi compiles nothing.

**Fallback: build on the Pi.** Without `--prebuilt` the installer builds on the Pi itself (one compile job at idle priority, still slow, and heavy for a Pi 3B on a weak supply): `sudo apt install -y git`, `git clone --recurse-submodules git@github.com:tanujdargan/ledposter.git` (a private repo needs credentials on the Pi once: an SSH key, `ssh -A`, or `https://<token>@github.com/...`), `cd ledposter`, `sudo ./system/install.sh --ssh-key "ssh-ed25519 AAAA... you@laptop"` (or `--ssh disabled`). `sudo ./system/install.sh --dry-run --data-dir /tmp/x` shows every step without doing anything.

Options: `--prebuilt TARBALL`, `--hostname NAME` (default `ledposter`), `--no-overlay` (keep the root writable, for development), `--data-dir PATH` (a directory instead of the partition, testing only; implies `--no-overlay`), `--force-build` (replace the installed release of this version). It is idempotent: run it again after changing anything under `system/`. It installs the apt packages (`system/packages.sh`: runtime, plus build packages unless `--prebuilt`), creates the `ledwall` user (runs the app) and the `ledwall-build` user (compiles releases on the Pi), creates and mounts `/data` (an existing partition labelled `ledpostdata`, or a new one in the free space; `nofail`), unpacks (or builds as `ledwall-build`) the release under `/data/app/releases/<version>` and hands it to root, writes `/data/config.yaml` and the setup card password, installs the systemd units, polkit rules, avahi, dnsmasq, journald, NetworkManager, apt, sysctl (`kernel.sched_rt_runtime_us = -1`) and modprobe (`blacklist snd_bcm2835`) drop-ins, persists Wi-Fi profiles across the overlay, sets the hostname, boot config (`dtparam=audio=off`; `isolcpus=3` and `cpufreq.default_governor=performance` in `cmdline.txt`, each added once; I2C on), disables Bluetooth/ModemManager/triggerhappy/swap/apt timers, applies the firewall and the SSH mode, and finally turns on the read-only root overlay. SSH is left exactly as it was until the end, and the installer refuses to continue if you are connected over SSH without a key and did not pass `--ssh disabled`. The login user is only read (its `~/.ssh/authorized_keys`, for key-only SSH), never changed.

It ends with a summary: **the setup hotspot password** (print it on the card in the box), the SSH mode, and the wall's **deploy key**.

**5. Add the deploy key** so the wall can fetch updates: GitHub > the repo > Settings > Deploy keys > Add, paste the printed `ssh-ed25519 ...` key, read-only. (Alternative: a fine-grained personal access token with read access to the repo's contents, saved as `sudo tee /data/app/token` and `chmod 600`; the deploy key is recommended because it is tied to the one repo and the one wall.) Then `sudo reboot`.

**6. First frame.** Within 30s of power the wall shows `LED POSTER` with a spinner (the boot screen), then the IP for 10s once the web app is up. Open `http://ledposter.local/`, set the admin password, and you are on the Home screen. Run `docs/ACCEPTANCE.md` (`sudo /data/app/current/tools/acceptance.sh`) and the milestone 8 tests below.

Where things are on the Pi: software under `/data/app` (`current` -> `releases/<tag>`, `previous`; owned by root, the app only runs it), config `/data/config.yaml`, media `/data/media`, logs that survive a reboot `/data/logs` (`update.log`, `maintenance.log`, `web/web.log`; system logs are in RAM: `journalctl -b`), the hotspot password `/data/setup-password`, root's persisted state `/data/state`. `/data` itself is `root:ledwall` 1775, so the app adds its own files but cannot touch root's directories (the full table is in `docs/ARCHITECTURE.md`). Everything outside `/data` is read-only; `sudo ledposter-sys overlay off` and a reboot make it writable, `overlay on` locks it again (`docs/DEVIATIONS.md`).

## Development setup (no installer)

```sh
sudo apt install -y git build-essential libyaml-cpp-dev nlohmann-json3-dev ffmpeg python3-venv python3-numpy
git clone --recurse-submodules git@github.com:tanujdargan/ledposter.git
```

For hacking on the Pi without the installer (milestones 1 to 7 were tested this way). In `/boot/firmware/config.txt` set `dtparam=audio=off`. Then append `isolcpus=3` to the single line in `/boot/firmware/cmdline.txt`, and reboot. The matrix library pins its refresh thread to core 3, and on-board audio conflicts with the PWM hardware it uses. On a Mac or Linux box, `make sim` and the fake engine (below) run everything but the panels.

## Build and test

```sh
cd engine
make test        # mapping, limiter, brightness curve, pattern, media, playlist, IPC parsing, effect,
                 # text, overlay, button and DDP/E1.31 parsing tests; runs on any machine (needs nlohmann-json, header
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

Without a mode flag the engine shows the boot screen (`LED POSTER` and a spinner) until the web app sets a mode: on the installed wall the web app restores the last mode from the config a few seconds later. The flags only pick the starting mode. While it runs, the engine listens on `engine.socket` for commands (`status`, `set_brightness`, `set_auto_brightness`, `power`, `set_mode`, `list_modes`, `overlay`, `show_text`, `ack_wifi_setup`, `reload_config`, `set_stream`) and streams its output on `engine.preview_socket`; both are in [docs/protocol.md](docs/protocol.md). From a shell, with `socat`:

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

Dev without a Pi: `python tests/fake_engine.py /tmp/lpsock` serves the two sockets with a moving test image; point `engine.socket`/`engine.preview_socket` at them. Or build the real engine with `cd engine && make sim`. `LEDPOSTER_FAKE_NET=1` (or `offline`) swaps NetworkManager for `tests/fake_net.py`, so the Wi-Fi setup and static IP screens can be exercised on a laptop without touching its network; `offline` boots "without a known network" and raises the (fake) hotspot after `network.setup_timeout_s`.

### Networking (milestone 6)

Everything that touches the OS goes through `web/ledposter/net.py`, which shells out to `nmcli`, `hostnamectl`, `systemctl` and `ping` (NetworkManager is Bookworm's default; dhcpcd is not used). The wall is reached by name:

- **`ledposter.local`**: Avahi advertises the hostname and an `_http._tcp` service (`system/avahi/ledposter.service`). Settings > Hostname changes it (one RFC 1123 label); `hostnamectl` sets it and `ledposter-hosts.service` rewrites `/etc/hosts` as root; Avahi follows.
- **DHCP by default**, with `ipv4.dhcp-client-id mac` and `ipv4.dhcp-send-hostname yes` set on every Wi-Fi profile the app creates or joins, so routers hand back the same lease. Settings explains how to add a DHCP reservation and shows the MAC.
- **Static IP** (Settings > IP address): validated against the current subnet (inside the network, not the network/broadcast address, gateway in the subnet, address not already answering a ping). Before it is applied the app writes `$LEDPOSTER_DATA/net-rollback` and restarts `ledposter-net-rollback.timer` (120s); then it applies the address 1.5s after answering, and the browser is sent to `http://<new ip>/#/settings/confirm`. Signing in there and tapping "Keep this address" (`POST /api/network/static/confirm`) stops the timer. Otherwise `ledposter-net rollback` runs as root and puts the connection back on DHCP, whether or not the web app is alive.
- **IP on the wall**: hostname and IP for `network.show_ip_s` seconds when the web app starts and the engine and network are up, and from Settings > "Show address on the wall" (`POST /api/network/show_ip`). A double button press is the planned trigger (docs/DEVIATIONS.md).
- **First-time setup**: no Wi-Fi or Ethernet within `network.setup_timeout_s` of the web app starting, or the engine reporting a 10s button hold (`wifi_setup_requested`), or Settings > "Start Wi-Fi setup hotspot": the app scans (one radio, so before the AP is up), then raises the WPA2 hotspot `LEDWall-Setup` with the password in `$LEDPOSTER_DATA/setup-password` (created at install and printed on the card; created on first start if missing, with a log line saying where). NM shared mode runs dnsmasq; `system/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf` resolves every name to `10.42.0.1`, and the app answers the OS connectivity probes (`/hotspot-detect.html`, `/generate_204`, `/connecttest.txt`, `/ncsi.txt`, ...) with a redirect to `/setup`, so the phone opens the portal by itself. The wizard: admin password first on a fresh wall, then the network list (signal and lock icons; Refresh drops the hotspot for ~15s), the network's password, and a 4-step progress screen. Joining switches the hotspot off; on failure it comes back with the reason. The wall shows each step as text: hotspot up, joining, joined with IP, or failed with the reason.
- **Auth**: the setup endpoints are open without login only while the hotspot is up on a wall that has no admin password yet. Everything else always needs the session or a token. Threat model in docs/DEVIATIONS.md.

All of it is in the API under `network` at `/docs`: `GET /api/network`, `PUT /api/network/hostname`, `POST /api/network/static`, `/static/confirm`, `/dhcp`, `/show_ip`, `GET /api/network/setup`, `/setup/start`, `/setup/stop`, `GET /api/network/wifi`, `/wifi/scan`, `/wifi/join`.

## Streaming input (WLED, LedFx, xLights)

The engine has a DDP receiver on UDP 4048 and an E1.31/sACN receiver on UDP 5568, both off by default. Turn them on in Settings > Integrations (or `ddp: {enabled: true}` / `e131: {enabled: true}` in the config). While frames arrive they replace whatever the wall shows; 5 seconds after the last packet it carries on with what it was showing. Brightness, power and the current limiter still apply. Details in [docs/protocol.md](docs/protocol.md#streaming-input-ddp-and-e131).

Set the sender up as **one matrix 128 wide, 192 high, RGB, rows left to right starting at the top-left corner, not serpentine** (the engine maps that onto the panel snake itself):

- **WLED** (as a sender, e.g. a WLED instance or WLED's virtual DDP output): add a DDP output to the wall's IP with 24576 LEDs, start 0, RGB. As a 2D setup: one panel, 128 x 192, no serpentine.
- **LedFx**: add a device of type DDP, IP address of the wall, port 4048, pixel count 24576. In the device's matrix settings use 128 columns (rows follow), no snake. LedFx sends sRGB-ish colours; keep "Streamed colours: sRGB (apply gamma)".
- **xLights**: an Ethernet controller at the wall's IP, protocol DDP (port 4048) or E1.31. Model: a horizontal Matrix with **192 strings of 128 nodes** (each string is one row), starting top-left, not serpentine, RGB order. For E1.31: 145 universes of 510 channels starting at the wall's first universe (Settings, default 1), unicast.

With E1.31, universe `start` carries pixels 0-169, `start+1` pixels 170-339, and so on; the 145th carries the last 96 pixels. Unicast needs nothing more. Multicast (`e131.multicast: true`) joins one group per universe, and Linux allows 20 per socket by default: raise it with `sudo sysctl -w net.ipv4.igmp_max_memberships=160` or send unicast.

If colours look washed out or too dark, flip "Streamed colours" (`stream.apply_gamma`): on (default) treats the input as sRGB and applies the wall's gamma; off shows values as sent, for senders that already gamma-correct.

## Home Assistant

The web app connects to an MQTT broker (Settings > Integrations: host, port, username, password, optional TLS) and announces itself with MQTT discovery. In Home Assistant you get one device, "LED Poster", with:

- a **light**: on/off, brightness, and an effect list with the wall's modes and every playlist (`Playlist: name`)
- **Current draw** (A) and **Ambient light** (lx) sensors
- a **Playlist** select

Commands from HA go through the same code as the app's buttons, so the schedule and quiet-hours cap still apply. Settings shows the connection state ("Connected", or the error). The entities show as unavailable while the web app is disconnected from the broker and also while the engine is offline. HA's brightness is the user setting (the app's slider): during quiet hours the wall is capped below it, and HA still shows the value you asked for.

Alexa, Google Home and Apple Home: expose the light through Home Assistant like any other light: Nabu Casa Cloud or the `alexa` / `google_assistant` integrations (Settings > Voice assistants > Expose), and the HomeKit Bridge integration (include the `light` domain or this entity). On/off and brightness work by voice; effects stay in HA.

Topics (for scripts): `ledposter/<device_id>/availability`, `.../light/state` and `.../light/set` (HA JSON light schema), `.../sensors`, `.../playlist/state` and `.../playlist/set`. Discovery configs are under `homeassistant/`. `device_id` defaults to `ledposter_` plus the Pi's MAC address.

## System (milestone 8)

The wall is an appliance: two systemd services, a read-only root, an unprivileged web app, and root-side actions that are fixed units rather than sudo. `docs/ARCHITECTURE.md` has the contracts, `docs/DEVIATIONS.md` the reasoning where it bends the spec.

**Processes.** `ledposter-engine.service` starts as soon as `/data` is mounted (before the network), as root until the matrix library has the GPIO, then as `ledwall` (the unit's `--user`/`--group` flags, with `--socket`/`--preview-socket` in `/run/ledposter`: as root the engine never takes these from `/data/config.yaml`, the app's file); it shows the boot screen until a mode is set and is restarted forever if it dies. `ledposter-web.service` runs as `ledwall` with only `CAP_NET_BIND_SERVICE` (port 80) and `ProtectSystem=strict`; it never waits for the network. Each survives the other crashing: `sudo systemctl kill -s KILL ledposter-web` leaves the wall running, `... ledposter-engine` leaves the app up saying "Engine offline" until the engine is back 2s later on its boot screen, and the app then restores the last mode.

**Root-side actions.** The app runs as `ledwall` and has no sudo. What needs root is a fixed unit the user may start through polkit (`system/polkit/*.rules`), each reading its input from a file at the top of `/data` the app wrote. The root side treats those files as hostile: `ledposter-sys input` reads them without following symlinks and only if they are plain files owned by `ledwall`, every line is matched against a strict pattern, and nothing from them is sourced or handed to a shell (`web/tests/test_rootside.py` runs the scripts against injection attempts). Nothing root executes is writable by `ledwall`:

| file | goes to | why |
|---|---|---|
| `system/systemd/ledposter-engine.service`, `ledposter-web.service` | `/etc/systemd/system/` | the two services |
| `system/systemd/ledposter-boot.service` | same | at boot, before NetworkManager/Avahi: restore the hostname from `/data/state`, guard the overlay marker |
| `system/systemd/ledposter-firewall.service` | same | `ledposter-sys firewall`: ufw reset + the rules in `/data/firewall.rules` (web app, `ledposter.system.firewall_rules`); at boot and on change; leaves the install-time rules alone when `/data` is missing |
| `system/systemd/ledposter-ssh.service` | same | `ledposter-sys ssh`: sshd drop-in (keys only, no root) + the validated keys from `/data/ssh/authorized_keys`, starts or stops `ssh.service`; at boot and on change; keeps the installed key-only setup when `/data` is missing |
| `system/systemd/ledposter-update.service` | same | `web/ledposter/updater.py run` as root: update/rollback from `/data/update-request.json` (checked: plain file, valid tag that exists on the repo); clone as root, make/venv/pip/tests as `ledwall-build`, then root owns the release; at boot it finishes an interrupted switch, but never resumes an interrupted build |
| `system/systemd/ledposter-factory-reset.service` | same | `ledposter-sys factory-reset` when `/data/factory-reset` exists |
| `system/systemd/ledposter-maintenance.timer` + `.service`, `ledposter-maintenance-resume.service` | same | hourly check of the security-update window (`/data/maintenance.conf`, parsed, never sourced; "Run now" writes `/data/maintenance-request`); the two-reboot upgrade (below) |
| `system/systemd/ledposter-net-rollback.timer` + `.service`, `ledposter-hosts.service` | same | milestone 6: DHCP rollback (the service also runs at boot while `/data/net-rollback` exists, so a reboot inside the 120s window still reverts), `/etc/hosts` after a hostname change (persisted to `/data/state/hostname`) |
| `system/polkit/49-ledposter-network.rules`, `50-ledposter-system.rules` | `/etc/polkit-1/rules.d/` | what the `ledwall` user may do: five NetworkManager actions (`network-control`, `settings.modify.system`, `wifi.scan`, `wifi.share.protected/open`), hostname, start/restart exactly these units, reboot via logind |
| `system/bin/ledposter-net`, `ledposter-sys` | `/usr/local/bin/` | the root-side scripts (`sh -n`, shellcheck clean) |
| `system/avahi/ledposter.service` | `/etc/avahi/services/` | `_http._tcp` next to `<hostname>.local` |
| `system/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf` | `/etc/NetworkManager/dnsmasq-shared.d/` | captive DNS while the hotspot is up |
| `system/etc/NetworkManager-ledposter.conf` | `/etc/NetworkManager/conf.d/` | DHCP client id = MAC for every profile, Wi-Fi power save off |
| `system/etc/journald-ledposter.conf` | `/etc/systemd/journald.conf.d/` | journal in RAM (volatile, 32MB cap) |
| `system/etc/apt-52ledposter` | `/etc/apt/apt.conf.d/` | unattended-upgrades: no periodic timers, kernel/firmware blacklisted |
| `system/etc/sysctl-ledposter.conf` | `/etc/sysctl.d/` | `igmp_max_memberships=160` for E1.31 multicast; `sched_rt_runtime_us=-1` (no RT throttling of the refresh thread) |
| `system/etc/modprobe-ledposter-blacklist-audio.conf` | `/etc/modprobe.d/` | `blacklist snd_bcm2835`, with `dtparam=audio=off` (the on-board audio uses the PWM hardware) |

**Firewall.** `ufw` default deny incoming, allow outgoing (MQTT): HTTP (`web.port`), mDNS 5353/udp, DDP 4048/udp and E1.31 5568/udp only while enabled in Settings, SSH 22/tcp only in key-only mode, and the hotspot's DHCP/DNS (67, 53) on the Wi-Fi interface only. The rules are regenerated by the web app whenever the config changes and re-applied at every boot (ufw's own saved rules live on the overlay). `sudo ufw status` shows them.

**SSH.** Settings > Security: Off, or Key only with the authorized keys in a textarea (the installer's `--ssh-key`, or your Imager key, is already there). Key only means `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin no`; log in as the user you installed with. Off stops `ssh.service` and keeps it off across reboots. The app refuses key-only with no key.

**Software updates.** Settings > Software shows the running release (`git describe`), "Check for updates" (tags on the repo newer than the running one, read with the deploy key), "Update to X" and "Roll back to <previous>". An update (`ledposter-update.service`, root): the request in `/data/update-request.json` is checked (a plain file of the app's, a tag that matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` and that `git ls-remote` lists), `git clone --branch <tag>` into `/data/app/releases/<tag>`, then as the `ledwall-build` user `make` the engine (one job, idle CPU and I/O priority, and only with at least 400MB of RAM + swap free; minutes on a Pi 3B, the wall keeps showing), a venv with the requirements (no pip cache), `make test` and an import check; root takes the release over (`chown -R root`), the `current` symlink is switched atomically (temp link + rename), both services restart, and `/api/health` must report the new release with the engine answering within 90s, or it switches back and restarts again. Progress is in the banner at the top of Settings (`/data/app/update.json`, log in `/data/logs/update.log`). A half-built release is never `current`. A power cut during the switch, restart or health check is finished or rolled back at the next boot; one during fetch, build or test is not resumed: at boot the half-built release is deleted and the banner says "Update to X was interrupted (power loss or reboot); still running Y. Start it again from Settings." A wall installed with `--prebuilt` has no compiler, so this in-app update refuses to build and says so: update it with `tools/deploy.sh --tag <tag>` (the in-app updater does not download prebuilt releases, `docs/DEVIATIONS.md`). Releases that change files under `system/` (units, polkit, scripts) also need `sudo ledposter-sys overlay off`, a reboot, `sudo /data/app/current/system/install.sh`, and the overlay back on (`docs/DEVIATIONS.md`). From a shell: `sudo -u ledwall LEDPOSTER_DATA=/data python3 /data/app/current/web/ledposter/updater.py request update m9 && sudo systemctl start ledposter-update` and `updater.py status`.

**Security updates (unattended-upgrades).** Weekly window in Settings > Software > Security updates (default Sunday 04:00, or Run now). The root is read-only, so the wall marks the window, turns the overlay off, reboots, installs Debian security updates on the real root while it keeps showing, turns the overlay back on and reboots again; about 5 minutes with two boot screens. Kernel and firmware packages are excluded (manual: `sudo ledposter-sys overlay off`, reboot, `sudo mount -o remount,rw /boot/firmware && sudo apt full-upgrade`, `sudo ledposter-sys overlay on`, reboot). Log: `/data/logs/maintenance.log`.

**Restarts and factory reset.** Settings > Maintenance: Restart engine, Restart web app, Reboot. Factory reset at the bottom (confirmed twice, the hostname typed) erases config, media, tokens, sessions, logs, SSH keys, Wi-Fi profiles and the hostname, keeps the installed releases and the card password, writes a fresh config and reboots into the setup hotspot.

**Logs.** `journalctl -b -u ledposter-engine` / `-u ledposter-web` for this boot (RAM; gone at reboot). Surviving: `/data/logs/web/web.log` (warnings and errors, rotated at 2MB x 3; the app's directory), `/data/logs/update.log` and `maintenance.log` (root's). Nothing logs per frame.

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

The engine starts as root (`sudo`) but the matrix library drops root right after it starts the panels; from then on the engine runs as the user and group given with `--user NAME --group NAME` (default `daemon`:`daemon`; `ledposter-engine.service` passes `ledwall`:`ledwall`). The sockets, the I2C bus and the button line are opened before that; the sockets are created mode 660 with that group, in `/run/ledposter` (or `--socket PATH --preview-socket PATH`: both in one root-owned directory), so the engine can still remove them after the drop. Running as root, the engine ignores the config's `engine.socket`, `engine.preview_socket`, `engine.user` and `engine.group` (it logs one line per key that differs) and refuses `root` as the target: `config.yaml` is the web app's file, and the web app's user must not be able to steer what the engine does as root (`docs/DEVIATIONS.md`). Those config keys only apply when the engine is not root (`make sim`). So the drop target needs:

- read access to `$LEDPOSTER_DATA` (`config.yaml` for `reload_config`, and the media). If your home dir is mode 700 (`ls -ld ~`), `chmod o+x ~`. The web app writes `config.yaml` mode 600 (640 since m8), so after the web app has saved it `reload_config` fails unless the engine runs as the web app's user: for dev testing run the engine with `--user` and `--group` set to the user that runs the web app (e.g. `sudo ./ledengine --user pi --group pi ...`; `pi` is already in `i2c`). Up to m8 this was `engine.user`/`engine.group` in the config, which the root-run engine no longer reads.
- membership in group `i2c`, so the BH1750 can be reopened after an error (sensor unplugged): `sudo usermod -aG i2c daemon` for dev testing without the installer.
- the web app must run as a user in the `--group` group, or as root, to connect to the sockets.

Tags m3 to m5 predate this: there the engine created its sockets and opened I2C after the drop, so on the Pi it had no control socket. When testing those tags, set `engine.socket` and `engine.preview_socket` to `/tmp/ledposter/engine.sock` and `/tmp/ledposter/frames.sock` (and use those paths below), or test from m6 on. Tags m6 to m8 read the socket paths and the drop user from the config as root (so `engine.user` in the config works there, as that section said); from the commit after m8 on, use the flags.

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
## Testing milestone 4 on the Pi

Pull, install the new Python packages and run the tests:

```sh
cd ~/ledposter && git pull && git submodule update
cd web && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Expected: `46 passed` (transcoder, auth, config, media, playlists, schedule, preview relay against a fake engine).

Start the engine (M3) and then the web app on port 8080 (or the systemd units from the installer), with the same data dir:

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
## Testing milestone 6 on the Pi

Pull and run the tests:

```sh
cd ~/ledposter && git pull && git submodule update
cd web && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest -q
```

Expected: `91 passed` (networking tests run against a fake NetworkManager; nothing on the Pi's network is touched by the tests. Needs `build/ledengine-sim` from `cd engine && make sim` for the engine socket tests, else they are skipped). Then install the M6 system files by hand (`system/install.sh` does all of this on an installed wall) and check the tools are there:

```sh
sudo apt install -y avahi-daemon network-manager   # both already on Raspberry Pi OS Bookworm
sudo install -m 755 system/bin/ledposter-net /usr/local/bin/
sudo install -m 644 system/systemd/ledposter-*.service system/systemd/ledposter-*.timer /etc/systemd/system/
sudo install -m 644 system/avahi/ledposter.service /etc/avahi/services/
sudo install -D -m 644 system/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf /etc/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf
sudo install -m 644 system/polkit/49-ledposter-network.rules /etc/polkit-1/rules.d/
sudo systemctl daemon-reload
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status      # wlan0:wifi:connected:<your network>
```

Create the card password once, then start the engine and the web app as root for now (root needs no polkit; the installer creates the `ledwall` user and the units). Port 80 so `http://ledposter.local` works without a port:

```sh
export LEDPOSTER_DATA=~/lpdata
echo 'yourcardpw' > $LEDPOSTER_DATA/setup-password && chmod 600 $LEDPOSTER_DATA/setup-password
sudo -E ./engine/ledengine &
cd web && sudo -E .venv/bin/python -m ledposter --port 80
```

Check and report:

1. **Boot IP**: within a few seconds of the web app starting the wall shows `ledposter.local` and its IP for 10s. From your phone (same Wi-Fi) open `http://ledposter.local/`: the app loads. `avahi-browse -rt _http._tcp` on a laptop lists "LED Poster on ledposter".
2. **Stable DHCP**: `nmcli -t -f ipv4.dhcp-client-id,ipv4.dhcp-send-hostname con show "<your network>"` shows `mac` and `yes` (set by the app when it joins a network; for a profile that existed before, set it once by hand: `sudo nmcli con modify "<your network>" ipv4.dhcp-client-id mac ipv4.dhcp-send-hostname yes`). Reboot twice: same address each time. Settings > Network shows SSID, signal, `DHCP · <ip>/24`, and the reservation help with the MAC.
3. **Hostname**: Settings > Hostname > `wall-two`. `hostnamectl` shows it, `grep 127.0.1.1 /etc/hosts` shows it, and `http://wall-two.local/` works within ~10s (`avahi-resolve -n wall-two.local`). Change it back.
4. **Static IP, confirmed**: Settings > IP address > Static, pick a free address in your subnet (the sheet refuses one that answers a ping, and anything outside the subnet). Apply: the page counts down 5s and opens `http://<new ip>/#/settings/confirm`; sign in; the amber banner counts down from 2:00; tap "Keep this address". `systemctl list-timers ledposter-net-rollback.timer` is now empty, `ls $LEDPOSTER_DATA/net-rollback` says no such file, `ip -4 addr show wlan0` shows the new address.
5. **Static IP, rolled back**: do it again but close the browser instead of confirming. Within 2 minutes the wall is back on DHCP (`journalctl -u ledposter-net-rollback.service` says `static IP on '<network>' was not confirmed, back to DHCP`) and `http://ledposter.local/` works again. Report how long the gap was.
6. **Hotspot from the app**: Settings > "Start Wi-Fi setup hotspot" (the 10s button hold does the same once the M5 engine reports it). The wall shows `Wi-Fi setup / Join LEDWall-Setup / password: on the card / then open http://10.42.0.1`. On the phone, join `LEDWall-Setup` with the card password: the phone's captive portal sheet opens on the network list (if it doesn't, open `http://10.42.0.1/`). Pick your network, enter a **wrong** password: the wall shows `Joining <ssid>...`, then `Wi-Fi failed / Wrong password / Setup hotspot is back`; the phone, back on the hotspot, shows the failure and Try again. Enter the right password: the wall shows `Joined <ssid> / <ip> / http://ledposter.local`; move the phone to your Wi-Fi and open that address. Time the whole thing from powering on to the app loading (acceptance target: under 3 minutes).
7. **Boot without a network**: `sudo nmcli con modify "<your network>" connection.autoconnect no` and reboot. 60s after the web app starts (`network.setup_timeout_s`) the hotspot comes up on its own and the wall shows the setup text; join it and set up as in 6. Then `sudo nmcli con modify "<your network>" connection.autoconnect yes`.
8. **First run on the hotspot**: `mv $LEDPOSTER_DATA/config.yaml{,.bak}` (or delete `web.admin_password` from it) and repeat 6: the portal asks for an admin password before the network list; `curl http://10.42.0.1/api/network` from the phone's browser answers 401 but `/api/network/setup` answers 200 until the password is set.

If networking breaks (wrong static IP, hotspot stuck), from keyboard + HDMI or the serial console:

```sh
sudo nmcli con down LEDWall-Setup                       # drop the hotspot
sudo nmcli con modify "<your network>" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
sudo nmcli con up "<your network>"                      # back on DHCP
sudo systemctl stop ledposter-net-rollback.timer; sudo rm -f /data/net-rollback ~/lpdata/net-rollback
nmcli device wifi list                                  # what the Pi can see (2.4GHz only on a 3B)
```

`sudo /usr/local/bin/ledposter-net rollback` does the DHCP revert from the note file by hand, and `journalctl -u NetworkManager -n 50` explains failed joins.

## Testing milestone 7 on the Pi

Pull, update the venv (paho-mqtt 2.x comes from pip; Bookworm's apt has 1.6), build and test:

```sh
cd ~/ledposter && git pull && git submodule update --init
cd engine && make test && make && make sim
cd ../web && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Expected: `all engine tests passed`, and pytest reports `108 passed, 1 skipped` (the skipped test needs a `mosquitto` binary; with `sudo apt install -y mosquitto` it is `109 passed`).

**Streaming input.** Start the engine and the web app as for milestone 6. In the app, Settings > Integrations: turn on DDP input and E1.31 input, Save. The engine log says `ddp: listening on UDP 4048` and `e131: listening on UDP 5568, universes 1 to 145` (`1 to 2` on the single 64x64 panel), and `status` has `"stream": {..., "ddp": true, "e131": true}`. On an installed wall the firewall opens 4048 and 5568 by itself when you enable them in Settings.

From a laptop on the same network, either:

- **LedFx** (`pip install ledfx`, then `ledfx --open-ui`): Devices > Add device > DDP, IP of the wall, 24576 pixels (4096 for the single panel). Pick any effect (e.g. Energy) and play music.
- **WLED** sender, or **xLights**: set up as in [Streaming input](#streaming-input-wled-ledfx-xlights).
- No sender at hand: a test pattern from Python on the laptop. It sends a moving red/blue gradient by DDP for 10s (use `W, H = 64, 64` for the single panel):

  ```sh
  python3 - <<'EOF'
  import socket, struct, time
  W, H, WALL = 128, 192, ("ledposter.local", 4048)
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  for n in range(300):
      f = bytes(c for y in range(H) for x in range(W) for c in ((x * 2 + n) % 256, 0, (y + n) % 256))
      for off in range(0, len(f), 1440):
          d = f[off:off + 1440]
          s.sendto(struct.pack("!BBBBIH", 0x40 | (off + 1440 >= len(f)), 0, 0x0B, 1, off, len(d)) + d, WALL)
      time.sleep(1 / 30)
  EOF
  ```

Expected:

1. Within a frame of the first packets the wall shows the stream; the app's preview shows the same, with the pill "Stream · DDP" (or E1.31) and Settings shows "Receiving DDP at N fps". The gradient runs left to right and top to bottom; if it is mirrored, rotated or wraps, the sender's matrix isn't 128 wide, row-major, top-left, not serpentine.
2. Colours: compare with the same content played from the library. If the stream looks washed out or too dark, flip "Streamed colours" and report which looks right for your sender.
3. 5 seconds after the sender stops, the wall goes back to what it was showing, carrying on where it was (a playlist doesn't restart).
4. A short button press while streaming ends the takeover and moves to the next mode as usual; the stream stays off the wall until the sender pauses for 5s. A game hook event (`tools/game_hook.py kill`) plays on top of the stream.
5. Full-white stream (LedFx Single Color, white): `status` shows `limited: true` and `amps` at or under `ceiling_amps`.
6. Turning DDP off in Settings while streaming returns the wall to its mode at once.
7. Report the stream `fps` from `status` with LedFx at 60fps, and whether the wall's own `fps` keeps up (the Pi 3B has to copy and output each frame).

**Double press.** Press the button twice quickly (under 0.35s apart): within about 4s the wall shows `ledposter.local` and the IP for 10s, and `status` counts `button_double_count`. A single short press still goes to the next mode, about 0.35s after release (`button.double_ms`; 0 turns double presses off). Report whether 0.35s feels right.

**Home Assistant.** You need an MQTT broker. If HA runs the Mosquitto add-on, use that; otherwise on the Pi or any Linux box:

```sh
sudo apt install -y mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/passwd ledwall           # pick a password
printf 'listener 1883\npassword_file /etc/mosquitto/passwd\n' | sudo tee /etc/mosquitto/conf.d/ledposter.conf
sudo systemctl restart mosquitto
```

In HA: Settings > Devices & services > Add integration > MQTT, broker host and the same user (with the add-on, HA sets this up itself). Then in the wall's app, Settings > Integrations: MQTT on, broker host, port 1883, username and password, Save.

Expected:

1. Settings shows "Connected" next to MQTT within a few seconds. A wrong password shows "Error: Not authorized" (or "Bad user name or password"); a wrong host "Error: can't reach host:1883, retrying".
2. HA > Settings > Devices shows "LED Poster" (model 128x192 LED wall, software version = the app's) with a light, Current draw, Ambient light and Playlist entities. `mosquitto_sub -h <broker> -u ledwall -P <pw> -t 'ledposter/#' -v` shows the state topics.
3. The light: turn it off and on (the wall follows within a second), move brightness (the wall dims; the app's slider follows), pick an effect (Plasma, Clock, a test pattern, `Playlist: demo`). Changes made in the app show up in HA within about 3s.
4. The Playlist select switches playlists; the sensors show amps and lux (lux unknown with no BH1750).
5. With quiet hours active and capping at 20%, HA brightness 100% leaves the wall at 20% (HA shows the requested 100%, the user setting, not the capped value).
6. Stop the engine: within a few seconds the entities show unavailable (they follow the engine as well as the MQTT connection). Start it: they come back. Stop the web app: unavailable via the broker's last will.
7. Optional: expose the light to Alexa, Google Home or Apple Home through HA (see [Home Assistant](#home-assistant)) and try "turn off the LED Poster" and "set the LED Poster to 30%".

Report: the stream fps from LedFx, how the colours look with and without gamma, and anything HA showed differently from the above.

## Testing milestone 8 on the Pi

This is the first milestone tested from a **fresh SD card** with the installer, not from a checkout in your home directory. Follow [Install on a fresh Pi](#install-on-a-fresh-pi-milestone-8) up to the reboot, with `--ssh-key` so you keep a shell. Then, on your computer, `cd engine && make test && make sim; cd ../web && .venv/bin/python -m pytest -q` should say `all engine tests passed` and `150 passed, 1 skipped` (`151 passed` with a `mosquitto` binary installed; `tests/test_rootside.py` runs the root-side shell scripts with stubbed commands); `shellcheck system/install.sh system/bin/* tools/acceptance.sh` is clean.

Check and report, in this order:

1. **Cold boot.** Power off, power on with a stopwatch. The boot screen (`LED POSTER`, spinner) appears; note the seconds. The hostname and IP follow once the web app is up. `ssh you@ledposter.local`, then `systemd-analyze` and `journalctl -b -u ledposter-engine -o short-monotonic | grep 'first frame'`. Target under 30s on the stopwatch. `systemctl status ledposter-engine ledposter-web` both `active (running)`, `ps -o user,comm -C ledengine` shows `ledwall` (dropped root), `ps -o user,comm -C python` shows `ledwall`. Ownership: `stat -c '%U:%G %a' /data /data/app /data/state /data/logs /data/media` prints `root:ledwall 1775`, `root:root 755`, `root:root 755`, `root:root 755`, `ledwall:ledwall 755`; `sudo -u ledwall test -w /data/app && echo WRITABLE` prints nothing; `sudo -u ledwall /data/app/current/engine/ledengine --help` runs.
2. **Read-only root.** `cat /proc/cmdline` contains `overlayroot=tmpfs` (or `boot=overlay`), `findmnt /` shows `overlay`, `findmnt /data` shows `ext4 ... noatime`, `sudo touch /etc/x` works but is gone after a reboot, `touch /data/x` survives. `sudo ledposter-sys overlay status`.
3. **Last mode comes back.** In the app pick Fire. Reboot. After the boot screen the wall shows Fire again without touching the app. Pull the plug mid-playback 5 times at random moments: every time it comes back on the same mode; `sudo /data/app/current/tools/acceptance.sh health` is green.
4. **Crash isolation.** `sudo systemctl kill -s KILL ledposter-web`: the wall keeps showing; the app is unreachable for about 2s and then loads again. `sudo systemctl kill -s KILL ledposter-engine`: the app shows "Engine offline", the wall goes dark, 2s later the boot screen, then the last mode again.
5. **Firewall.** `sudo ufw status verbose`: deny incoming, allow outgoing, 80/tcp, 5353/udp, 22/tcp (key-only), 67/53 on wlan0, no 4048/5568. From your laptop `nmap -p 22,80,4048,5568 ledposter.local` (or `nc -zv`): 22 and 80 open, the rest filtered. Settings > Integrations > DDP on, Save: `sudo ufw status` now lists 4048/udp within a second (`journalctl -u ledposter-firewall`). Hotspot: Settings > Start Wi-Fi setup hotspot, join it from a phone: the captive portal opens (DHCP and DNS pass). Join your network again from the portal.
6. **SSH.** Settings > Security: your key is listed, mode Key only. `ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password you@ledposter.local` is refused (`Permission denied (publickey)`), `ssh root@ledposter.local` too. Add a second key, Save, log in with it. Switch to Off, Save: `ssh` says connection refused, and still after a reboot; switch back on from the app.
7. **Updates.** Make sure the deploy key is on GitHub (install step 5). Settings > Software > Check for updates: on the m8 wall it says you are on the latest release; "All releases" lists m1 to m8. Pick an older tag under All releases (m7): confirm; the banner walks through Fetching, Building (a few minutes), Testing, Switching, Restarting, Checking, then "Updated: Running m7" with Reload; the wall showed its boot screen briefly during the restart and came back on the last mode. The version row says m7, "Roll back to m8" appears; tap it: switching, restarting, checking, back on m8 within a minute. Then a failing update: `sudo mkdir -p /data/app/releases/m9x && sudo chown ledwall /data/app/releases/m9x` is not needed; instead simulate a bad release with `POST /api/system/update {"target":"nonexistent-tag"}` (from `/docs`): the banner ends red with `git clone failed` and the wall is untouched. Report the build time. Pull the plug during a Building step: after the boot the update continues (`journalctl -u ledposter-update`) and finishes. Pull it during Restarting/Checking: at boot the health check runs and keeps or rolls back.
8. **Security-update window.** Settings > Software > Security updates: set the day/hour to a few minutes from now (or Run now). The wall reboots, comes back with the root writable (`findmnt /`), `journalctl -u ledposter-maintenance-resume -f` shows `apt-get update` and `unattended-upgrade`, then it reboots again with the overlay on. `cat /data/logs/maintenance.log`. About 5 minutes total; the wall showed the last mode in between.
9. **Factory reset.** Settings > Factory reset > Continue > type `ledposter` > Erase everything. The wall shows `Factory reset...`, reboots, and about a minute later shows the `Wi-Fi setup / Join LEDWall-Setup` text. Set it up again from a phone with the card password as in milestone 6 (stopwatch: acceptance criterion 7, under 3 minutes). `ls /data` shows a fresh `config.yaml`, empty `media`, the same `setup-password`, `app/` intact; `ssh` is off (default) until you turn it on in Settings.
10. **Acceptance run.** `sudo LEDPOSTER_PASSWORD=... /data/app/current/tools/acceptance.sh` and the eyes-on items in `docs/ACCEPTANCE.md`. Record the table.

Report: the stopwatch boot time and the `first frame` seconds, the update build time, the maintenance window duration, the acceptance table, and anything the installer printed that was unclear.

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
- **`control socket off: ... Permission denied`**: the engine can't create `/run/ledposter`. Run it with `sudo`, or (not as root) point `engine.socket` and `engine.preview_socket` at a writable path for dev. On m3 to m5 the engine made the sockets after dropping root, so this happens even with `sudo`: use paths under `/tmp` (e.g. `/tmp/ledposter/engine.sock`) or update to m6 or later.
- **`engine.socket in the config is ignored when running as root`**, **`--user root: the engine must drop to a non-root user`**, **`must be owned by uid 0 and not world-writable; refusing to put sockets there`** or **`exists and is not a socket; refusing to replace it`**: as root the engine takes its socket paths and drop account only from `--socket`, `--preview-socket`, `--user`, `--group` (or its defaults), both sockets must be in one root-owned directory such as `/run/ledposter`, the account must not be root, and only a socket is ever replaced at those paths. The config keys count only when the engine is not root. See [Testing milestone 3](#testing-milestone-3-on-the-pi) and `docs/DEVIATIONS.md`.
- **Works at start, then `Permission denied` on media, `reload_config` or the sensor**: after starting the panels the engine runs as `--user`:`--group` (default `daemon`:`daemon`; `engine.user`/`engine.group` in the config up to m8), not root. That user needs read access to `$LEDPOSTER_DATA` (config and media) and the `i2c` group to reopen the BH1750 after an error (`sudo usermod -aG i2c daemon`, then restart the engine). See [Testing milestone 3](#testing-milestone-3-on-the-pi).
- **`another engine is already listening here`**: an engine is still running (`pgrep -a ledengine`); stop it first.
- **`button: /dev/gpiochip0: ...` or `button: GPIO25 ...: Device or resource busy`**: another program holds the line (`gpioinfo` from `gpiod` shows the owner), or `button.gpio` is a pin the matrix uses. Pick a free pin (Wiring assumptions). `status` shows `"button": "missing"`; the wall keeps running.
- **Button does nothing**: check the log for `button: GPIO25 on /dev/gpiochip0` at start. With `gpiod` installed, `gpioget gpiochip0 25` (engine stopped) reads 1 released and 0 pressed. Phantom presses: longer or untwisted wires picking up noise; raise `debounce_ms`.
- **`fonts: N of 6 missing in ...`**: the vendor submodule isn't checked out (`git submodule update --init`) or `fonts.dir` is wrong. Without fonts, text and clock modes can't be set and overlays show no words.
- **Clock shows the wrong time**: set the time zone (`sudo raspi-config nonint do_change_timezone ...`) and restart the engine; check `timedatectl` shows NTP synchronized.
- **`socat: ... Permission denied` from a client**: the sockets are mode 660, group `--group` (`engine.group` in the config when the engine isn't root). Run the client (and the web app) as root or as a user in that group (the systemd setup in milestone 8 gives the web app its group).
- **The phone joins `LEDWall-Setup` but no portal page opens**: open `http://10.42.0.1/` by hand. Check `/etc/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf` is installed (NM only reads it when the hotspot connection starts) and that the web app runs on port 80.
- **`nmcli dev wifi list` is empty while the hotspot is up**: expected, one radio. The portal uses the list scanned before the hotspot came up; Refresh drops the hotspot briefly to rescan.
- **Join fails with "Wrong password" for a correct password**: 5GHz-only network (the Pi 3B is 2.4GHz only), or a WPA3-only network. Check `journalctl -u NetworkManager`.
- **Static IP sheet says "Could not arm the DHCP rollback timer"**: the systemd units are not installed, or the user running the app may not start them (polkit rules). Nothing was changed; install `system/systemd/*` and `system/polkit/*`.
- **Hostname changed but `ledposter.local` still answers**: Avahi caches on the clients for a bit; the old name stops within a minute. If `/etc/hosts` still has the old name, `ledposter-hosts.service` did not run: `sudo systemctl start ledposter-hosts.service`.
- **Stream doesn't take over**: Settings must show DDP/E1.31 on, and `status` has `"stream": {"ddp": true, ...}`. `last_packet_ms_ago` staying `null` means nothing valid arrives: check the sender's IP and port, and the firewall (DDP 4048, E1.31 5568 UDP). The picture shifts or wraps: the sender's matrix isn't 128 wide, row-major, top-left, not serpentine.
- **`ddp: UDP port 4048: bind: Address already in use`**: another program (or another engine) has the port.
- **MQTT "Error: Not authorized" / "Bad user name or password"**: check the broker user in Settings. "can't reach host:port": broker host, port, or TLS on a plain port (1883) or off on a TLS port (8883).
- **HA shows the device but it's unavailable**: the engine is offline, or the web app isn't connected; see Settings.
- **Boot takes longer than 30s to the first frame**: `systemd-analyze blame | head` shows what is slow. The installer disables Bluetooth, ModemManager, triggerhappy, swap and the apt timers; anything else enabled since (`systemctl list-unit-files --state=enabled`) is a candidate. A slow SD card (class 4) adds seconds everywhere; `isolcpus=3` must be in `/proc/cmdline`.
- **`install.sh` says "No room for the data partition"**: the root filled the card on first boot. See step 2 of the install (remove `init_resize.sh` from `cmdline.txt` before the first boot) and `docs/DEVIATIONS.md` for the alternatives.
- **The wall sits on the boot screen forever**: the web app is not up (`systemctl status ledposter-web`, `journalctl -b -u ledposter-web`), or the last mode cannot be restored (a deleted media item: pick a mode in the app once).
- **Something written to `/etc` or `/home` is gone after a reboot**: that is the overlay. Put it under `/data`, or `sudo ledposter-sys overlay off`, reboot, change, `overlay on`, reboot.
- **Wi-Fi forgotten after a reboot**: `/etc/NetworkManager/system-connections` must be the bind mount of `/data/state/nm` (`findmnt /etc/NetworkManager/system-connections`); re-run the installer with the overlay off if it is missing.
- **"Check for updates" says Permission denied (publickey)**: the deploy key in `/data/app/deploy_key.pub` is not on the repo (GitHub > Settings > Deploy keys), or `/data/app/origin` is not the repo's ssh URL.
- **An update failed and the app is gone**: the updater rolls back by itself within about 2 minutes; if the wall stays dark, `journalctl -u ledposter-update` and `cat /data/app/update.json` over SSH; `sudo -u ledwall python3 /data/app/current/web/ledposter/updater.py request rollback && sudo systemctl start ledposter-update` switches back by hand.
- **Update ends with "Refused the update request" or "not a release tag"**: the request file was not a plain file owned by `ledwall`, or the tag is not on the repository (`git ls-remote --tags`). Only tags that exist can be installed; branches and commits cannot.
- **Locked out of SSH**: Settings > Security turns it back on (the app does not need SSH). No app either: keyboard + HDMI, `sudo ledposter-sys overlay off`, reboot, `echo key-only | sudo -u ledwall tee /data/ssh/mode`, your key into `/data/ssh/authorized_keys` (as `ledwall`: root-owned files there are refused), `sudo ledposter-sys ssh`.
- **The wall is dark and `ledposter.local` answers SSH but not HTTP**: `/data` did not mount (`systemctl status data.mount`, `journalctl -b -u data.mount`). The partition mounts `nofail`, so the Pi boots without it; the engine, web app and updater fail (they live on `/data`), SSH keeps the key-only setup from install time, the firewall keeps its install-time rules. Repair: `sudo fsck -y /dev/disk/by-label/ledpostdata`, `sudo mount /data`, `sudo systemctl start ledposter-boot ledposter-engine ledposter-web`; a card with no `ledpostdata` partition at all needs the installer again. A wall installed with `--ssh disabled` has no remote way in while `/data` is missing: keyboard + HDMI.
- **Permission denied under `/data/app` or `/data/state` as `ledwall`**: expected. Those are root's (`docs/ARCHITECTURE.md`); the app writes its requests at the top of `/data` (`update-request.json`, `maintenance-request`, `factory-reset`, `net-rollback`) and its own `media/`, `ssh/`, `logs/web/`.
- **Maintenance window left the root writable**: a failure between the upgrade and re-enabling the overlay. `sudo ledposter-sys overlay status`; `sudo ledposter-sys overlay on` and reboot. `ledposter-boot.service` also repairs this at the next boot when `/data/state/overlay-wanted` exists.
- **`ufw` blocks something you added**: the rules are regenerated from the config by the web app; add ports in `ledposter.system.firewall_rules` (and the regex in `ledposter-sys`), not with `ufw allow` (lost at reboot and overwritten).
