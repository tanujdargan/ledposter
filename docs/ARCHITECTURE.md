# Architecture contracts

Binding decisions that every milestone builds on. `docs/spec.md` is the source of truth for *what*. This file covers *how the pieces meet*. If a milestone needs to break a contract or a spec requirement, record it in `docs/DEVIATIONS.md` with the reason instead of silently changing it.

## Repo layout

```
engine/    C++17 display engine (Makefile). `make test` runs anywhere; `make` builds for the Pi;
           `make sim` builds the same engine with a stub matrix (no rpi-rgb-led-matrix) for Mac/Linux dev
web/       Python 3 package `ledposter` (FastAPI backend, transcoder, MQTT, networking helpers)
web/static vanilla JS PWA, no build step, served by FastAPI
system/    install.sh, systemd units, polkit rules, the root-side scripts (bin/), NetworkManager/avahi/journald/apt drop-ins (M6, M8)
tools/     game_hook.py (PC side of the game hook), acceptance.sh (on-device acceptance checks, docs/ACCEPTANCE.md)
config/    example configs
docs/      spec, this file, protocol.md, ui-references.md, DEVIATIONS.md
```

## Data and config

- Writable data dir is `/data` (env `LEDPOSTER_DATA` overrides it for dev and tests). The root filesystem is a read-only overlay, so nothing outside `/data` survives a reboot (system/install.sh, docs/DEVIATIONS.md). Its layout and ownership are part of the threat model (the web app runs as `ledwall`; root-run units and the root-run engine read from here, so the `ledwall` user must not be able to swap anything root uses; `ledwall` and `ledwall-build` are system accounts the installer creates, distinct from the Pi's login user, which may be called `ledposter` and is never changed):

  | path | owner, mode | written by | what |
  |---|---|---|---|
  | `/data` | `root:ledwall` 1775 | installer | sticky: the web app adds and replaces its own files here, but cannot rename or remove root's entries |
  | `config.yaml`, `secret`, `setup-password` | `ledwall` 640/600/600 | web app (installer makes the password) | the config, the session key, the hotspot card password |
  | `firewall.rules`, `ssh/mode`, `ssh/authorized_keys`, `maintenance.conf`, `maintenance-request`, `factory-reset`, `update-request.json`, `net-rollback` | `ledwall` | web app | the inputs of the root-side units: read by root without following symlinks (`ledposter-sys input`, `updater.read_input`: regular file owned by `ledwall`, under 64KB), every line matched against a strict pattern, used as data only |
  | `media/` | `ledwall` 755 | web app (transcoder) | the media cache, read by the engine after it dropped root |
  | `logs/` | `root` 755 | root units | `update.log`, `maintenance.log` |
  | `logs/web/` | `ledwall` 755 | web app | `web.log` (rotated) |
  | `state/` | `root` 755 | root units | `hostname`, `nm/` (Wi-Fi profiles, bind-mounted into `/etc/NetworkManager/system-connections`, 700), `overlay-wanted`, `maintenance-pending`, `fake-hwclock.data` |
  | `app/` | `root` 755 | installer, `ledposter-update.service` (root) | `releases/<tag>/` checkouts with their built engine and venv (unpacked from a `tools/build-release.sh` tarball, or built on the Pi), `current` and `previous` symlinks, `update.json` (progress, readable by the app), `pending.json`, `origin`, `known_hosts`, `deploy_key` (`root:ledwall` 640: read-only on GitHub, readable so the app can list tags), `build/` (home of `ledwall-build`, 700) |

  The web app reads and executes `app/current` and reads `app/update.json` and `state/maintenance-pending`; it writes nothing under `app/`, `state/` or `logs/`. Root creates the `ledwall`-owned directories itself (`mkdir` then `chown`, never on an existing path) and never `chown`s, `rm -rf`s through, or writes into a path the app owns, except the `rm -rf` of the app's top-level entries at a factory reset (coreutils `rm` does not follow symlinks).
- `/data/config.yaml` is the single versioned YAML config (`version:` key). Only the web app writes it, atomically (tmp file, fsync, rename, fsync dir). The engine only reads it: at start, and again on the `reload_config` command.
- Every hardware tunable (slowdown, PWM bits, chain mapping, limiter ceiling, GPIO pins, I2C bus/address, socket paths) lives in config, never in code.
- Exception, because the config is the `ledwall` user's file: what the engine does **as root** (where it binds its sockets, which account it drops to) comes from its command line (`--socket`, `--preview-socket`, `--user`, `--group` in the root-owned `ledposter-engine.service`) or built-in defaults, never from `config.yaml`; the config's `engine.socket`/`preview_socket`/`user`/`group` keys apply only when the engine is not root (dev, `make sim`), and are what the web app connects to. As root the engine also refuses root as the drop target, refuses socket paths outside one root-owned, non-world-writable directory, never chowns, chmods or unlinks by path (mode via umask, group via the effective gid, only sockets are ever replaced), and exits if it is still root after the matrix started. Everything else it reads from the config before the drop (fonts dir, button chip, I2C bus, media) is opened read-only.

## Media cache (written by the web app's transcoder, read by the engine)

- `/data/media/<id>/frames.rgb` holds raw RGB24 frames back to back, `w*h*3` bytes each. `w`/`h` is the logical canvas (128x192 on the full wall).
- `/data/media/<id>/meta.json` contains `{"w":128,"h":192,"durations_ms":[...],"fps_cap":60, ...item settings}`.
- `/data/media/<id>/thumb.png` is a thumbnail at wall resolution.
- Frames are stored **already gamma corrected and dithered**. The engine never decodes media formats in the frame loop. It reads raw frames only (mmap or buffered read).

## Gamma and power

- The engine disables rpi-rgb-led-matrix's own luminance correction (`matrix->set_luminance_correct(false)`) and owns gamma itself. Cached media is pre-gamma'd. Generative, clock and text output is passed through the engine's gamma LUT (`display.gamma`, default 2.2) before output.
- So pixel values reaching the panel are linear PWM duty, and the limiter's current estimate (linear in RGB sum × hardware brightness) is accurate, not just conservative.
- Brightness: `effective = min(user, auto)`, then the limiter scales each frame so its estimate never exceeds `power.ceiling_amps`.

## Colorlight receiving card

The bonnet tops out around 140-200Hz refresh on six chained panels, which flickers on dim content. Since 2026-10-24 the wall is driven by a Colorlight 5A-75B card instead: its FPGA refreshes the panels (1920Hz+) and the Pi only sends frames over Ethernet (a USB gigabit adapter, `eth1`: the card needs a gigabit link and the Pi 3B's own port is 100Mbit). `ledengine --colorlight eth1` selects it, from a systemd drop-in (`/etc/systemd/system/ledposter-engine.service.d/colorlight.conf`), so removing the drop-in falls back to the bonnet. The flag is root-time like the socket paths: the raw socket is opened as root, then the engine drops to `--user` itself.

`engine/src/colorlight.h` sends BGR rows, then "show frame" twice, on a fixed 60Hz grid from its own thread, whatever the frame loop does: the card blanks the whole screen now and then on an uneven or 30Hz stream. `matrix.panels[i]` is the card's slot i (`card_lut` in wall.h). The card's own settings live on the card, written once with LEDVision: firmware 13.39 *Normal* (the PWM build shows nothing on these ICN2037 panels), cabinet 128x192, rows 0-63/64-127/128-191 on ports J1/J2/J3 (J5-J7 repeat them, J4/J8 carry nothing). `tools/colorlight.py` talks to the card directly for bring-up.

## Engine ↔ web app IPC

- **Control:** Unix stream socket `engine.socket` (default `/run/ledposter/engine.sock`; the root-run engine binds its `--socket` flag, see above), newline-delimited JSON. Request `{"id":1,"cmd":"status", ...}` gets the response `{"id":1,"ok":true, ...}` or `{"id":1,"ok":false,"error":"..."}`. Every command is listed in `docs/protocol.md`; whoever adds a command documents it there.
- **Preview:** Unix stream socket `engine.preview_socket` (default `/run/ledposter/frames.sock`). Each connected client receives at most 15 messages a second, each one JSON status line (`{"mode":..,"fps":..,"amps":..,"limited":..,"w":..,"h":..}\n`) followed by exactly `w*h*3` raw bytes of the frame *as output* (after brightness and limiter). Slow clients drop frames; they never block the engine.
- The frame loop never blocks on I/O. Socket handling runs on its own thread and hands commands to the loop through a mutex-guarded queue.
- The engine survives the web app dying, and the reverse: the web app reconnects with backoff and shows "engine offline".

## Ownership

- **Engine:** matrix, frame loop, modes, playlist playback, limiter, BH1750, button, DDP/E1.31 receivers, overlays, on-wall text (IP, setup status).
- **Web app:** auth, REST API (`/api/...`, OpenAPI at `/docs`), preview WebSocket relay, uploads and transcoding, config writes, schedule, MQTT/HA, networking (NetworkManager via `nmcli`), updates.
- **Root-side units (M6/M8):** the web app runs as the `ledwall` user and never gets root. Anything that needs it (DHCP rollback, `/etc/hosts`, firewall, SSH mode, update/rollback, factory reset, restarts, reboot, the maintenance window) is a fixed systemd unit in `system/systemd/` that the user may start through polkit (`system/polkit/`) and that reads its inputs from a file under `/data` written by the app. No sudo, no transient units. The root side treats those files as hostile input (table above): no `source`/`eval`, no unquoted expansion, no `xargs` or `sh -c`, no symlink following; and nothing root executes (`/data/app`, the scripts in `/usr/local/bin`, the units) is writable by `ledwall`. Builds (make, venv, pip) run as a third user, `ledwall-build`, so root never runs code from PyPI either.

## Milestone hand-off rules

- Each milestone ends as one or more commits on `main`, tagged `mN` and pushed (`git push origin main mN`). Checking out `mN` must build and run on its own.
- README gets a "Testing milestone N on the Pi" section with exact commands and expected results.
- Tests: `cd engine && make test` (no hardware), `cd web && python -m pytest` (no hardware, no root). Keep them passing at every tag.
- Spec rules that always apply: never decode video in the frame loop; never let estimated current exceed the ceiling; tunables in config.
