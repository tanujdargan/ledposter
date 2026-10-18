# LED Wall Software Spec

Oct 6, 2026 · @Tanuj Dargan

## Overview

A Raspberry Pi 3B drives a 128 x 192 pixel LED wall (six P3 64x64 HUB75 panels, 2 wide x 3 tall) and exposes a web app on the home network to control it: play GIFs, videos, images and animations, switch modes, set brightness and schedules, and see a live preview of what the wall is showing.

**Hardware this runs on**

- Raspberry Pi 3B, Raspberry Pi OS Lite 64-bit, Adafruit RGB Matrix Bonnet (E jumper on pin 8)
- 6 panels chained in a snake: panel 1 top-left, 2 top-right, 3 middle-right, 4 middle-left, 5 bottom-left, 6 bottom-right, all upright
- Driver chips: Chipone ICN2037BP (standard, no special init)
- Power: 100W USB-C PD at 20V, buck to 5V, about 15A budget for panels after the Pi
- BH1750 light sensor on I2C, one momentary push button on a free GPIO

**On resolution**

The wall is physically 128 x 192 pixels; nothing can display more than that. What we can do is accept media at any resolution and downscale it well, so the wall supports two content styles side by side:

- **Downscaled media:** photos, GIFs and video at any source resolution, resampled with area averaging, gamma corrected and optionally dithered. At 3ft behind a diffuser this reads as a soft, real image, not pixel art.
- **Native pixel art:** animations authored at exactly 128 x 192 (or 64 x 96 scaled 2x), shown with nearest-neighbour so every pixel stays crisp.

The content pipeline treats both as first-class, and a per-item setting picks the scaling mode.

## Architecture

Two processes run on the Pi: a C++ display engine that owns the panels and a Python web app that handles everything people and other systems talk to. Keeping them separate means a web app crash or a slow upload never stutters the wall.

&#91;embedded content: software architecture · 2 processes on the Pi\]

Streams from WLED or LedFx go straight to the engine for low latency; everything else enters through the web app and reaches the engine as commands over the Unix socket, with frames coming back for the live preview.

## Display engine

The engine owns the matrix, renders a 128 x 192 RGB canvas at a steady frame rate, and is the only process that touches the GPIO.

**Matrix setup**

- Library: hzeller rpi-rgb-led-matrix, C++ core (Python bindings acceptable only if the frame loop stays native)
- Options: rows 64, cols 64, chain 6, parallel 1, `adafruit-hat-pwm` mapping (falls back to `adafruit-hat` via config), gpio slowdown configurable (default 2), PWM bits configurable (default 9)
- Pixel mapper: logical 128 x 192 portrait canvas mapped onto the physical snake chain; mapping lives in config so a rewired chain needs no code change
- Runs pinned to isolated CPU core 3; audio disabled

**Media pipeline**

- Accepts GIF, APNG, PNG, JPG, WebP, MP4, WebM, MOV uploads up to 200MB
- Transcodes on upload with ffmpeg into a cached raw frame file: 128 x 192 RGB24, original frame timings kept, capped at 60fps (about 74KB per frame)
- Scaling modes per item: `fit` (letterbox, black bars), `fill` (crop to fill), `stretch`, plus filter `smooth` (area) or `pixel` (nearest)
- Per-item crop and position, set from the web app
- Gamma correction and optional ordered dithering applied at transcode time
- Playback reads cached frames from disk, never decodes video live

**Brightness limiter**

- Every frame, estimate panel current from the sum of RGB values (calibrated constant, about 30W per panel at full white)
- If the estimate exceeds the configured ceiling (default 13A at 5V), scale that frame's brightness down to fit; never exceed the ceiling
- User brightness, auto brightness from the BH1750, and the limiter combine as min(user, auto) then the limiter on top

**Modes**

- Media player: single item or playlist, loop, shuffle, per-item duration
- Generative effects: spider-sense ripple, web-shot trace, UAV radar sweep, plasma, fire, starfield
- Clock and idle screens
- Text scroller
- Panel ID test pattern: big number and up-arrow per panel, plus solid color and row/column sweeps
- External input (DDP/E1.31 stream) that takes over while frames arrive and returns to the previous mode 5s after they stop

**Controls on the frame**

- Button short press: next mode or playlist item; long press (2s): display on/off; very long press (10s): enter Wi-Fi setup mode

## Web app

A mobile-first web app served by the Pi at `http://ledposter.local`, installable as a PWA so it opens like a phone app.

**Design references:** before designing each screen, search Mobbin for real-app references (smart-home device control, media library grids, playlist editors, Wi-Fi setup flows) and base the layout on them. List the references used for each screen.

**Live preview (the visualizer)**

- The engine publishes the current output frame over a WebSocket at up to 15fps
- The app draws it on a canvas at the wall's 2:3 aspect, each pixel as a soft round dot on black so it looks like the real wall, with a toggle for a flat pixel view
- Shows mode name, fps, estimated current draw and whether the limiter is active
- Uploads show a preview through the same renderer before they are sent to the wall, with the chosen scaling mode and crop applied

**Controls**

- Power on/off, brightness slider, auto-brightness toggle
- Mode picker with per-mode settings (speed, colors, text)
- Playlist editor: drag to reorder, per-item duration, loop and shuffle
- Schedule: on/off times, and a quiet-hours brightness cap

**Media library**

- Upload from phone or desktop, drag and drop on desktop
- Grid of thumbnails rendered at wall resolution, with duration and type
- Per-item settings: scaling mode, filter, crop and position, speed, dithering
- Upload progress and transcode status; a failed transcode says what went wrong and which formats work
- Pixel art upload path: files already at 128 x 192 or 64 x 96 default to `pixel` filter

**Settings**

- Wi-Fi network, hostname, admin password
- Matrix tuning: slowdown, PWM bits, chain mapping, limiter ceiling
- MQTT broker details, DDP on/off, game hook token
- Firmware/software version, update button, restart, factory reset
- Panel ID test mode button

**API**

- Every app action goes through a documented REST API (`/api/...`, OpenAPI spec), so scripts and Home Assistant can do anything the app can

## Networking and integrations

The wall is reached by name, `ledposter.local`, not by a hard-coded IP. A static IP set on the Pi breaks on any network where that address is taken or outside the subnet, and you don't know your friend's router.

**Addressing**

- DHCP by default, with a stable DHCP client ID so most routers hand back the same address after reboots
- mDNS/Avahi advertises `ledposter.local` and an `_http._tcp` service, so phones and laptops find it without knowing the IP
- Optional static IP in Settings, validated against the current subnet before it is applied, with automatic rollback to DHCP if the wall is unreachable after 2 minutes
- The setup page tells the owner how to add a DHCP reservation in their router for a truly fixed address
- Show the current IP on the wall for 10s at boot and on a button press

**First-time Wi-Fi setup**

- No known network at boot, or the 10s button press: the Pi starts a hotspot `LEDWall-Setup` with a password printed on a card in the box
- Captive portal page lists nearby networks; picking one and entering its password joins it, then the hotspot shuts off
- The wall shows setup status as text: hotspot up, joining, joined with IP, failed with reason
- Implemented with NetworkManager (default on Bookworm), not dhcpcd

**Home Assistant and MQTT**

- MQTT client with HA discovery: one `light` entity (on/off, brightness, effect list = modes and playlists), plus sensors for current draw and ambient light, and a `select` for playlist
- Works through HA with Alexa, Google Home and Apple Home

**Streaming input**

- DDP (UDP 4048) and E1.31/sACN receivers, 128 x 192 RGB, so WLED, LedFx (audio reactive) and xLights can drive the wall

**Game hook**

- `POST /api/event` with a token: named events (`kill`, `killstreak`, `death`, `win`) trigger short overlay animations on top of the current mode
- A small companion script for the PC sends these events; how it detects game events is out of scope for v1

## Security, reliability and acceptance

The wall must survive having its plug pulled at any moment and must not be controllable by anyone on the network without a password.

**Security**

- Web app and API behind a login; admin password set during first-time setup, stored hashed
- API tokens for HA, scripts and the game hook, revocable in Settings
- MQTT with username and password; DDP/E1.31 off by default
- SSH key-only, or disabled, with a toggle in Settings
- `ufw` allows only HTTP, mDNS, MQTT outbound and DDP/E1.31 when enabled
- `unattended-upgrades` for security patches

**Reliability**

- Read-only root filesystem (overlayfs); media, config and logs on a separate writable partition mounted with `noatime`
- systemd units for the engine and web app, restart on failure, engine starts first and shows the boot screen
- Engine survives the web app crashing, and the reverse
- Config as one versioned YAML file, written atomically
- Software update pulls a tagged release and can roll back to the previous one

**Acceptance criteria**

- [ ] Cold boot to first frame on the wall in under 30s
- [ ] Refresh rate of 150Hz or more with all 6 panels at default settings, no visible flicker by eye
- [ ] Current never exceeds the limiter ceiling on a full-white test frame
- [ ] A 1080p 30s MP4 uploads, transcodes and plays within 2 minutes
- [ ] Preview in the app matches the wall frame for frame within 200ms
- [ ] Pulling power 20 times during playback never corrupts the system or media
- [ ] Fresh unit joins a new Wi-Fi network through the setup hotspot from a phone in under 3 minutes
- [ ] HA discovers the wall and can turn it on/off, set brightness and change effect
- [ ] Panel ID mode shows numbers 1 to 6 upright in the correct positions

## Build prompt

Paste this into a coding agent along with this whole doc. It asks for the work in milestones, so you can test each on the one wired panel before the rest of the parts arrive.

```text
You are building the software for an LED wall: a Raspberry Pi 3B (Raspberry Pi OS Lite 64-bit, Bookworm) with an Adafruit RGB Matrix Bonnet driving six 64x64 P3 HUB75 panels as one 128x192 portrait canvas. The attached spec is the source of truth; follow it exactly and ask before changing any requirement.

Stack:
- Display engine: C++ on hzeller/rpi-rgb-led-matrix. Owns the matrix, runs the frame loop, modes, playlist, brightness limiter, DDP/E1.31 receivers and button/sensor input. Pinned to isolated core 3.
- Web app: Python 3 + FastAPI (backend), a small Svelte or vanilla JS PWA (frontend, built to static files). ffmpeg for transcoding.
- Engine and web app talk over a Unix socket: JSON commands one way, frames and status back for the live preview.
- MQTT via paho-mqtt with Home Assistant discovery. NetworkManager for Wi-Fi and the setup hotspot. Avahi for ledposter.local.
- systemd units, read-only root via overlayfs, writable /data partition for media, config and logs.

Work in milestones. After each one, stop, list what to run on the Pi to test it, and wait for my results:
1. Engine skeleton: matrix init from a YAML config, logical 128x192 canvas mapped to the 6-panel snake, panel ID test mode, solid colors, row/column sweeps. Must also run with chain=1 for single-panel testing.
2. Media pipeline: ffmpeg transcode to cached raw 128x192 RGB24 frames with timings, fit/fill/stretch, smooth/pixel filters, gamma, dithering. Engine plays cached files and playlists.
3. Brightness limiter and BH1750 auto brightness, with current estimate exposed in status.
4. Web app: login, live preview over WebSocket, controls, media library with upload/transcode status, playlist editor, schedule, settings, REST API with OpenAPI.
5. Generative modes, clock, text scroller, button handling, game hook overlay events.
6. Networking: mDNS, stable DHCP, optional validated static IP with rollback, setup hotspot + captive portal.
7. MQTT/HA discovery, DDP and E1.31 input.
8. Hardening: overlayfs, systemd, ufw, SSH settings, unattended-upgrades, update with rollback, an install script that sets up a fresh Pi from scratch.

Rules:
- Never decode video in the frame loop.
- Never let estimated current exceed the configured ceiling.
- All tunables (slowdown, PWM bits, chain mapping, limiter ceiling, GPIO pins) live in config, not code.
- Include tests for the canvas-to-chain mapping, the limiter math and the transcoder output size.
- Keep a README with setup, wiring assumptions and troubleshooting.
- UI: before designing each screen of the web app, search Mobbin for reference screens from real apps (smart-home device control, media library grids, playlist editors, Wi-Fi setup flows), base the layout on them, and list the references you used.
```
