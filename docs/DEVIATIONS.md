# Deviations

Places where the build differs from `docs/spec.md` or `docs/ARCHITECTURE.md`, and why.

## M2: animated WebP on Raspberry Pi OS Bookworm

Spec: the media pipeline accepts WebP. Bookworm ships ffmpeg 5.1, whose WebP decoder handles still images only (it skips the `ANIM`/`ANMF` chunks). Still WebP works everywhere. Animated WebP works with newer ffmpeg builds (verified with 9.0 on macOS), but on the Pi the transcoder rejects it with "Animated WebP is not supported by this system's ffmpeg; convert it to GIF or MP4" plus the supported-format list.

Fix options, if needed: decode animated WebP with Pillow (`python3-pil`) as a fallback, or install a newer ffmpeg build. Neither was done in M2, to keep the dependencies at ffmpeg plus numpy.

## M4: config comments are not preserved

Architecture: one versioned YAML config, written atomically by the web app. The web app rewrites `config.yaml` with PyYAML `safe_dump`, which drops the comments found in `config/config.example.yaml`. Keys, values and `version` are preserved and the file is written atomically with mode 600. Comments live in the example config and the README instead; a comment-preserving library (ruamel.yaml) was not added to keep dependencies short.

## M4: `websockets` added to the web app's dependencies

The M4 plan listed fastapi, uvicorn, python-multipart and pyyaml as the new dependencies. Plain `uvicorn` cannot serve WebSockets (it logs "No supported WebSocket library detected" and answers 404), so `websockets` is required for the preview relay at `/api/preview`. Pure Python, also packaged as `python3-websockets` on Bookworm.

## M4: per-item speed is applied to the cached frame timings

Spec: per-item settings include speed. The transcoder has no speed option and the engine belongs to M3, so the web app applies speed itself: after a transcode it divides every `durations_ms` entry in `meta.json` by `speed` (minimum 1 ms) and records `speed` in the file. The engine plays the adjusted timings unchanged, so no engine support is pending; it only means `durations_ms` in the cache is the playback timing rather than the source timing.

## M4: the preview renderer approximates the transcoder

Spec: uploads show a preview through the dot renderer with the chosen scaling mode and crop applied before they are sent to the wall. Until an item is transcoded (or while its settings are being changed), the browser emulates fit/fill/stretch, crop, position and the pixel/smooth filter with canvas `drawImage` (bilinear, not area averaging; no gamma or dither). Once the cache matches the settings the editor shows real cached frames from `GET /api/media/{id}/frames`, which is exact.

## M6: static IP rollback uses a static systemd timer, not `systemd-run`

Spec: automatic rollback to DHCP if the wall is unreachable 2 minutes after a static IP is applied. The plan suggested `systemd-run --on-active=120`. That needs `org.freedesktop.systemd1.manage-units` for *transient* units, which lets the caller run any command as root under a chosen unit name, so granting it to the `ledposter` service user would be a root path in disguise. Instead `system/systemd/ledposter-net-rollback.timer` (OnActiveSec=120) and `.service` are fixed units installed by M8; the web app only `systemctl restart`s the timer before applying and `stop`s it on confirm. The polkit rule (`system/polkit/49-ledposter-network.rules`) allows exactly those verbs on exactly those units, so no sudoers drop-in is needed. The connection to revert is written to `$LEDPOSTER_DATA/net-rollback` by the web app; `system/bin/ledposter-net rollback` (root, run by the service) reads it and is a no-op when the file was removed by a confirm. Same guarantee as the plan: the revert does not depend on the web app being alive.

The same pattern updates `/etc/hosts` after a hostname change (`hostnamectl` does not touch it): `ledposter-hosts.service` runs `ledposter-net hosts` as root. An alternative is to install `libnss-myhostname` and skip the file; M8 can pick either.

## M6: the IP is shown on the wall at boot, from Settings, and on a button press pending an engine hook

Spec: show the current IP on the wall for 10s at boot and on a button press. Boot: the web app sends `show_text` once the engine is reachable and the network is up (the web app starts at boot, so this is "at boot" plus every web app restart). Settings has "Show address on the wall". The button's short (next mode), long (power) and very long (10s, Wi-Fi setup) presses are all taken by the spec, so the choice is: **a double press shows the hostname and IP for 10s**. That needs the engine to report it; the web app watches the engine status for a `button_double_presses` counter (pending, M5/M7 engine change, like `wifi_setup_requested`) and sends `show_text`. Until the engine has it, boot and Settings cover the requirement. Not done in M6 because engine/ is M5's.

## M6: on a fresh wall the admin password is set before Wi-Fi is joined

Spec: admin password set during first-time setup; the plan listed the flow as join Wi-Fi, then set the password. On the setup hotspot only the card holder can reach the wall, but the moment it joins the home LAN with no admin password, anyone on that LAN could claim it through the open first-run `/api/auth/setup` (the M4 behaviour). So the setup wizard (`/setup`) asks for the admin password as step 1 when none is set, then the network list and password. The API rule the plan asked for is still in place: setup endpoints (`/api/network/setup`, `/api/network/wifi*`) are reachable without login only while the hotspot is up **and** no admin password exists; everything else stays behind login at all times.

Threat model, briefly:
- Hotspot: WPA2-PSK with the per-unit password from the card (`$LEDPOSTER_DATA/setup-password`, generated at install, 10 unambiguous characters, never shown on the wall). Joining it is the trust boundary for first-run setup. It is only up when nobody has set up Wi-Fi yet or someone holding the physical button asked for it.
- While the hotspot is up the captive portal probes redirect to `/setup`; the setup page is static and public like `/login`, the data is not. A wall that already has an admin password requires login even on the hotspot.
- Wi-Fi passwords go to NetworkManager (`nmcli ... password`), which stores them in `/etc/NetworkManager/system-connections` (root only); they are not written to the config or logs. Process arguments are visible to local users while `nmcli` runs; acceptable on a single-user appliance.
- The static IP change and hostname change require login; the rollback only ever moves a connection back to DHCP.
- The `ledposter` user gets NetworkManager and hostname polkit actions and start/stop of two fixed units: it can change the wall's network, nothing else.

## M6: how the engine's `show_text` and `wifi_setup_requested` are used

M6 was built in parallel with M5 and verified against `docs/protocol.md` as M5 built it. The networking code only touches the engine through `Engine.show_text(text, seconds)`, `Engine.setup_request(status)` and `Engine.ack_wifi_setup()` in `web/ledposter/engine.py`. `show_text` takes `text` (max 500 bytes, the renderer wraps and honours `\n`) and `seconds` (0.5 to 86400); the wall texts stay well under both. `wifi_setup_requested` is a boolean that stays true until `ack_wifi_setup`: the web app raises the hotspot when it sees it, then acks; if the hotspot cannot come up it still acks (otherwise the flag would re-trigger every poll) and does not retry for 30s. The engine shows its own "Wi-Fi setup" text for 4s on the hold; the web app's hotspot text replaces it a few seconds later.
