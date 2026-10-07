# Architecture contracts

Binding decisions that every milestone builds on. `docs/spec.md` is the source of truth for *what*. This file covers *how the pieces meet*. If a milestone needs to break a contract or a spec requirement, record it in `docs/DEVIATIONS.md` with the reason instead of silently changing it.

## Repo layout

```
engine/    C++17 display engine (Makefile). `make test` runs anywhere; `make` builds for the Pi;
           `make sim` builds the same engine with a stub matrix (no rpi-rgb-led-matrix) for Mac/Linux dev
web/       Python 3 package `ledposter` (FastAPI backend, transcoder, MQTT, networking helpers)
web/static vanilla JS PWA, no build step, served by FastAPI
system/    systemd units, install script, NetworkManager/avahi/ufw/overlayfs setup (M6, M8)
config/    example configs
docs/      spec, this file, protocol.md, ui-references.md, DEVIATIONS.md
```

## Data and config

- Writable data dir is `/data` (env `LEDPOSTER_DATA` overrides it for dev and tests). It holds `config.yaml`, `media/`, `logs/`.
- `/data/config.yaml` is the single versioned YAML config (`version:` key). Only the web app writes it, atomically (tmp file, fsync, rename, fsync dir). The engine only reads it: at start, and again on the `reload_config` command.
- Every hardware tunable (slowdown, PWM bits, chain mapping, limiter ceiling, GPIO pins, I2C bus/address, socket paths) lives in config, never in code.

## Media cache (written by the web app's transcoder, read by the engine)

- `/data/media/<id>/frames.rgb` holds raw RGB24 frames back to back, `w*h*3` bytes each. `w`/`h` is the logical canvas (128x192 on the full wall).
- `/data/media/<id>/meta.json` contains `{"w":128,"h":192,"durations_ms":[...],"fps_cap":30, ...item settings}`.
- `/data/media/<id>/thumb.png` is a thumbnail at wall resolution.
- Frames are stored **already gamma corrected and dithered**. The engine never decodes media formats in the frame loop. It reads raw frames only (mmap or buffered read).

## Gamma and power

- The engine disables rpi-rgb-led-matrix's own luminance correction (`matrix->set_luminance_correct(false)`) and owns gamma itself. Cached media is pre-gamma'd. Generative, clock and text output is passed through the engine's gamma LUT (`display.gamma`, default 2.2) before output.
- So pixel values reaching the panel are linear PWM duty, and the limiter's current estimate (linear in RGB sum × hardware brightness) is accurate, not just conservative.
- Brightness: `effective = min(user, auto)`, then the limiter scales each frame so its estimate never exceeds `power.ceiling_amps`.

## Engine ↔ web app IPC

- **Control:** Unix stream socket `engine.socket` (default `/run/ledposter/engine.sock`), newline-delimited JSON. Request `{"id":1,"cmd":"status", ...}` gets the response `{"id":1,"ok":true, ...}` or `{"id":1,"ok":false,"error":"..."}`. Every command is listed in `docs/protocol.md`; whoever adds a command documents it there.
- **Preview:** Unix stream socket `engine.preview_socket` (default `/run/ledposter/frames.sock`). Each connected client receives at most 15 messages a second, each one JSON status line (`{"mode":..,"fps":..,"amps":..,"limited":..,"w":..,"h":..}\n`) followed by exactly `w*h*3` raw bytes of the frame *as output* (after brightness and limiter). Slow clients drop frames; they never block the engine.
- The frame loop never blocks on I/O. Socket handling runs on its own thread and hands commands to the loop through a mutex-guarded queue.
- The engine survives the web app dying, and the reverse: the web app reconnects with backoff and shows "engine offline".

## Ownership

- **Engine:** matrix, frame loop, modes, playlist playback, limiter, BH1750, button, DDP/E1.31 receivers, overlays, on-wall text (IP, setup status).
- **Web app:** auth, REST API (`/api/...`, OpenAPI at `/docs`), preview WebSocket relay, uploads and transcoding, config writes, schedule, MQTT/HA, networking (NetworkManager via `nmcli`), updates.

## Milestone hand-off rules

- Each milestone ends as one or more commits on `main`, tagged `mN` and pushed (`git push origin main mN`). Checking out `mN` must build and run on its own.
- README gets a "Testing milestone N on the Pi" section with exact commands and expected results.
- Tests: `cd engine && make test` (no hardware), `cd web && python -m pytest` (no hardware, no root). Keep them passing at every tag.
- Spec rules that always apply: never decode video in the frame loop; never let estimated current exceed the ceiling; tunables in config.
