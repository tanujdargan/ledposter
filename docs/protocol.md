# Engine protocol

How the web app (or a script) talks to the display engine. Two Unix stream sockets, both set in the config (plus the [DDP and E1.31 inputs](#streaming-input-ddp-and-e131) on UDP):

| config key | default | carries |
|---|---|---|
| `engine.socket` | `/run/ledposter/engine.sock` | control: JSON requests and responses |
| `engine.preview_socket` | `/run/ledposter/frames.sock` | preview: status + output frame, one way |

The engine creates the parent directory, replaces a stale socket file left by a crashed engine (it refuses to start the socket if another engine still answers on it), and sets mode `660`. If a socket can't be created, the engine logs why and keeps driving the wall without it.

Whoever adds a command documents it here.

## Control socket

Newline-delimited JSON, both ways. Each request is one JSON object on one line:

```json
{"id": 1, "cmd": "set_brightness", "value": 40}
```

- `id`: any JSON value. It is echoed back so you can match responses. Optional; it comes back as `null` if missing or if the line wasn't valid JSON.
- `cmd`: the command name. Every other field is an argument.

Each request gets exactly one response line, in request order:

```json
{"id": 1, "ok": true}
{"id": 2, "ok": false, "error": "'value' must be a number from 0 to 100"}
```

On `ok: false` nothing was changed. `error` is a sentence meant for a person. A request line can be pipelined: send several and read the responses in order. Lines over 1MB without a newline, or a client that never reads its responses (1MB backlog), get disconnected. At most 32 control clients at once.

Commands take effect from the next frame (within about 100ms). A `status` sent right after a change can still show the frame before it.

### Commands

| cmd | arguments | response fields (besides `id`, `ok`) |
|---|---|---|
| `status` | none | see [Status](#status) |
| `set_brightness` | `value`: number 0 to 100 (rounded to an integer percent) | none |
| `set_auto_brightness` | `enabled`: bool | none |
| `power` | `on`: bool | none |
| `set_mode` | see below | `mode`, `mode_name`, `settings` |
| `list_modes` | none | `modes`, `events`, `fonts`: see [Modes and settings](#modes-and-settings) |
| `overlay` | `event`: `kill`, `killstreak`, `death` or `win` | `ms`: how long it shows |
| `show_text` | `text`: string (max 500 bytes, `""` clears), `seconds`: 0.5 to 86400 (default 5), `color` (optional, default white) | `ms` |
| `ack_wifi_setup` | none | none |
| `reload_config` | none | `restart_needed`: list of section names |
| `set_stream` | `ddp` and/or `e131`: bool | `ddp`, `e131`: whether each receiver is now listening |
| `set_fake_lux` | `lux`: number >= 0, or `null` | none (simulator build only) |
| `sim_button` | `ms`: 1 to 60000, `count`: 1 to 5 (default 1), `gap_ms`: 1 to 5000 (default 100) | none (simulator build only) |

**`set_brightness`** sets the user brightness (the slider). It lasts until the engine restarts or `reload_config` runs; the web app persists it by writing `brightness.user` to the config.

**`set_auto_brightness`** turns BH1750 auto brightness on or off. Effective brightness is `min(user, auto)` when on and the sensor answers, otherwise `user`.

**`power`** `{"on": false}` outputs black. The engine keeps running: modes and playlists carry on, the sockets stay up, `status` reports `power: false` and 0A.

**`set_mode`** switches what the wall shows, starting the new mode from its beginning:

```json
{"id": 3, "cmd": "set_mode", "mode": "pattern", "pattern": "solid", "color": "ff8000"}
{"id": 4, "cmd": "set_mode", "mode": "media", "media": "sunset"}
{"id": 5, "cmd": "set_mode", "mode": "playlist", "playlist": "demo"}
```

```json
{"id": 6, "cmd": "set_mode", "mode": "effect", "effect": "plasma", "settings": {"speed": 2, "colors": ["ff0060", "4000ff"]}}
{"id": 7, "cmd": "set_mode", "mode": "clock", "settings": {"hour24": false, "date": true}}
{"id": 8, "cmd": "set_mode", "mode": "text", "text": "Hello!", "color": "00ff00"}
{"id": 9, "cmd": "set_mode", "mode": "idle"}
```

- `pattern`: `panel-id`, `solid`, `cycle`, `rows` or `cols`. `color` is optional: `"RRGGBB"`, `"#RRGGBB"` or an integer `0xRRGGBB` (default white).
- `media`: a media id, i.e. a folder in `$LEDPOSTER_DATA/media` with `meta.json` and `frames.rgb`. It loops. It fails if the item isn't transcoded or was made for another canvas size.
- `playlist`: a name under `playlists:` in the config as loaded (at start or by the last `reload_config`).
- `effect`: `effect` is one of `spider-sense`, `web-shot`, `uav-radar`, `plasma`, `fire`, `starfield`.
- `clock`, `text`, `idle`: no extra field.
- `boot`: the boot screen ("LED POSTER" and a spinner, milestone 8). What the engine shows when started without a mode flag, until the web app restores the last mode from the config (`last_mode`); not listed by `list_modes`.

Settings: every mode has the settings `list_modes` reports. They go in a `settings` object, or as top-level fields with the same names (`"text": "Hello!"` above), which win over `settings`. Missing settings take their defaults. An unknown name inside `settings`, or a value of the wrong type or out of range, is an error and nothing changes (`'speed' must be a number from 0.1 to 5`). Colors are accepted as `"RRGGBB"`, `"#RRGGBB"` or an integer and always reported as lowercase `"rrggbb"`.

The response echoes `mode`, `mode_name` (pattern name, media id, playlist name, effect id, or `clock`/`text`/`idle`) and `settings`: the resolved settings with defaults filled in (empty for pattern, media and playlist).

**`list_modes`** describes every mode and its settings, so a mode picker can be generated from it. See [Modes and settings](#modes-and-settings).

**`overlay`** plays a short animation on top of whatever is showing (the game hook events). The mode keeps running underneath; after `ms` the overlay is gone. A new event replaces the one playing. The limiter applies to the frame with the overlay on it.

| event | ms | shows |
|---|---|---|
| `kill` | 1500 | white hit marker, then "KILL" in red |
| `killstreak` | 2500 | gold shockwave rings, blinking "KILLSTREAK" |
| `death` | 2000 | red vignette pulsing in from the edges, "DOWN" |
| `win` | 3000 | confetti bursting from the bottom corners, "VICTORY" |

**`show_text`** shows a message in a dark band across the middle of the wall for `seconds`, on top of the mode and any event overlay. It picks the biggest font that fits, word-wraps, and only breaks words that fit no other way. A new `show_text` replaces the current one; `"text": ""` removes it. Meant for the IP at boot and Wi-Fi setup status (milestone 6).

**`ack_wifi_setup`** clears `wifi_setup_requested` in the status (see [Button](#button)). `wifi_setup_count` keeps counting.

**`reload_config`** re-reads the config file and applies what can change live: power ceiling and calibration, gamma, brightness settings (user, auto, curve, smoothing), `engine.preview_fps`, the playlists, and the `ddp`, `e131` and `stream` sections (receivers are reopened only if their settings changed). If a DDP/E1.31 port can't be opened, everything else is still applied and the response is an error naming the port; that receiver stays off. If the running mode is a playlist, it restarts with the new definition. The file is checked first; if it is invalid (bad YAML, a value out of range, the running playlist removed or broken), the response is an error and the old settings stay.

`restart_needed` lists the sections that changed but only take effect after an engine restart: `matrix` (anything about the panels), `sensors`, `engine` (socket paths), `button` (pin, chip, timings; `button.cycle` and `button.skip_playlist_items` are live), `fonts`. Empty list: everything was applied.

```json
{"id": 6, "ok": true, "restart_needed": ["matrix"]}
```

**`set_stream`** turns the DDP and/or E1.31 receivers on or off without touching the config (the web app writes `ddp.enabled` / `e131.enabled` and sends this). Turning a receiver off while it is streaming returns the wall to its mode at once. Opening a port that is in use is an error (`ddp: UDP port 4048: bind: Address already in use`); the other receiver is still applied.

```json
{"id": 7, "cmd": "set_stream", "ddp": true}
{"id": 7, "ok": true, "ddp": true, "e131": false}
```

**`set_fake_lux`** (simulator build only; the Pi build answers with an error) stands in for the BH1750 so the auto brightness curve can be tested without hardware. `null` means "no sensor". The value is read by the same polling and smoothing as a real sensor, within one `sensors.bh1750.poll_ms`.

**`sim_button`** (simulator build only) holds the button down for `ms`, as if a finger were on it. It goes through the same 5ms polling, debounce and press classification as the real GPIO, so `{"ms": 100}` is a short press, `{"ms": 2500}` a long one (with the default `button.long_ms`), and `{"ms": 80, "count": 2, "gap_ms": 100}` a double press. Needs `button.gpio` set in the config; otherwise the button is `off` and this is an error.

### Modes and settings

`list_modes` answers:

```json
{"id": 1, "ok": true,
 "modes": [
  {"mode": "pattern", "label": "Test pattern", "help": "panel test patterns", "settings": [
    {"name": "pattern", "type": "choice", "choices": ["panel-id", "solid", "cycle", "rows", "cols"], "default": "panel-id"},
    {"name": "color", "type": "color", "default": "ffffff", "help": "for solid, rows and cols"}]},
  {"mode": "media", ...}, {"mode": "playlist", ...},
  {"mode": "effect", "effect": "plasma", "label": "Plasma", "help": "flowing sine plasma; colors form a looping palette", "settings": [
    {"name": "speed", "type": "number", "min": 0.1, "max": 5, "default": 1.0, "help": "animation speed, 1 = normal"},
    {"name": "colors", "type": "colors", "min": 1, "max": 6, "default": ["ff0060", "4000ff", "00c0ff", "ff00c0"]},
    {"name": "scale", "type": "number", "min": 0.25, "max": 4, "default": 1.0, "help": "pattern size, higher = smaller waves"}]},
  ...],
 "events": ["kill", "killstreak", "death", "win"],
 "fonts": ["tom-thumb", "5x8", "6x10", "7x13B", "9x18B", "10x20"]}
```

Each mode has `mode` (what `set_mode` takes), `effect` (effects only), `label`, `help` and `settings`. Each setting has `name`, `type`, `default`, maybe `help`, and by type:

| type | value | extra fields |
|---|---|---|
| `number` | number | `min`, `max` |
| `int` | whole number | `min`, `max` |
| `bool` | `true`/`false` | |
| `color` | `"rrggbb"` | |
| `colors` | list of `"rrggbb"` | `min`, `max`: how many |
| `string` | string | `max`: bytes |
| `choice` | one of `choices` | `choices` |

`media` takes a media id (`string`); the web app knows the library. `playlist` choices are the playlists in the config as loaded.

The engine-drawn modes, with their settings besides `speed` (0.1 to 5, default 1, all effects and text) and `colors`:

| mode | settings | default colors |
|---|---|---|
| effect `spider-sense` | `count` 1-8 (3): ripples at once. Three rings per ripple, alternating through colors | `ff1010`, `1040ff` |
| effect `web-shot` | `strands` 5-16 (10), `rings` 2-12 (7). Colors: web, glow (max 2). A 6s cycle: shot, strands, spinning rings, hold, fade | `ffffff`, `8fc8ff` |
| effect `uav-radar` | `blips` 0-16 (6). Colors: sweep, blips (max 2). One turn per 3s | `00ff50`, `ff2020` |
| effect `plasma` | `scale` 0.25-4 (1). Colors loop as a palette | `ff0060`, `4000ff`, `00c0ff`, `ff00c0` |
| effect `fire` | `height` 0.2-1 (0.55): flame height as a fraction of the canvas. Colors run cool to hot after black | `b00000`, `ff5000`, `ffb000`, `ffff90` |
| effect `starfield` | `density` 0.2-3 (1): 1 = a star per 100 pixels. Each star takes one of the colors | `ffffff`, `a0c0ff`, `ffe0a0` |
| `clock` | `hour24` (true), `seconds` (false), `date` (false), `color` (`ffffff`). The Pi's local time zone | |
| `text` | `text` (max 500 bytes, `Hello!`), `color` (`ffffff`), `background` (`000000`), `font` (one of `fonts`, `10x20`), `scale` 1-4 (2 on the wall, 1 on one panel). speed 1 = 30 pixels a second | |
| `idle` | `clock` (true), `hour24` (true), `level` 0.05-1 (0.35): a dark, slow blue plasma with a dim clock | |

Same seed, same pictures: effects take their randomness from a seed (random per `set_mode`), so they never repeat exactly but each one is deterministic in tests.

### Button

One push button on a GPIO (`button:` in the config, README has the wiring). The engine polls it every 5ms; a level counts once it held for `debounce_ms`.

- **Short press** (released before `long_ms`, default 2s; reported `double_ms` after the release, see double press): if external input (DDP/E1.31) is showing, the takeover ends first (the stream is ignored until it stops for `stream.timeout_s`) and the press then works on the mode underneath as below. If a playlist is playing (and `button.skip_playlist_items` is true, the default), its next item; after its last item, or in any other mode, the next entry of `button.cycle`. The cycle continues after the last entry the button picked, even if the web app changed the mode since; an entry that fails (a missing playlist, say) is logged and skipped.
- **Double press** (two short presses, the second starting within `double_ms` of the first release; default 350, 0 turns it off and short presses are reported at once): `button_double_count` goes up by one. The web app watches it and shows the hostname and IP for `network.show_ip_s`. A long or very long press starting inside the window counts as itself (the short press before it is dropped).
- **Long press** (released between `long_ms` and `very_long_ms`): display on/off, like `power`.
- **Very long press** (held for `very_long_ms`, default 10s; fires while still held, the release does nothing): `wifi_setup_requested` becomes true, `wifi_setup_count` goes up by one, `wifi_setup_at` is set, the display turns on and shows "Wi-Fi setup" for 4s. Networking (milestone 6) watches these and clears the flag with `ack_wifi_setup`.

### Status

```json
{"id": 1, "ok": true,
 "mode": "pattern", "mode_name": "solid",
 "fps": 30.3,
 "amps": 12.96, "amps_unlimited": 36.0, "limited": true, "ceiling_amps": 13.0,
 "brightness": {"user": 100, "auto": 64.7, "auto_enabled": true, "effective": 65, "output": 36},
 "lux": 50.0, "sensor": "ok",
 "power": true, "w": 128, "h": 192, "uptime_s": 312,
 "mode_settings": {}, "overlays": [], "button": "ok",
 "wifi_setup_requested": false, "wifi_setup_count": 0, "wifi_setup_at": null, "button_double_count": 0,
 "stream": {"active": false, "source": null, "fps": 0, "last_packet_ms_ago": null, "ddp": true, "e131": false}}
```

| field | meaning |
|---|---|
| `mode` | `pattern`, `media`, `playlist`, `effect`, `clock`, `text`, `idle` or `boot`; `stream` while DDP/E1.31 input has taken over |
| `mode_name` | pattern name, media id, playlist name, effect id, or `clock`/`text`/`idle`/`boot`; `ddp` or `e131` while streaming |
| `mode_settings` | the running mode's resolved settings (empty for pattern, media and playlist) |
| `overlays` | what is drawn on top right now: `[]`, or the event name and/or `"text"`, e.g. `["kill", "text"]` |
| `button` | `ok` (GPIO line open), `missing` (configured but it could not be opened; see the log), `off` (no `button.gpio`), `sim` (simulator) |
| `wifi_setup_requested` | true after a very long button press, until `ack_wifi_setup` |
| `wifi_setup_count` | very long presses since the engine started |
| `wifi_setup_at` | unix time of the last one, or `null` |
| `button_double_count` | double presses since the engine started (the web app shows the IP on each new one) |
| `fps` | frames output per second, measured over the last second. The engine only redraws when the picture changes (and at least 10 times a second), so a still image shows about 10 |
| `amps` | estimated panel current of the last frame, after the limiter. Never above `ceiling_amps` |
| `amps_unlimited` | what the frame would draw at `brightness.effective` without the limiter |
| `limited` | true when the limiter turned this frame down |
| `ceiling_amps` | `power.ceiling_amps` |
| `brightness.user` | the user setting, 0 to 100 |
| `brightness.auto` | what the BH1750 curve asks for (smoothed), or `null` with no sensor reading. Reported even when auto is off |
| `brightness.auto_enabled` | auto brightness switch |
| `brightness.effective` | `min(user, auto)` if auto is on and the sensor answers, else `user` |
| `brightness.output` | the percent actually sent to the panels after the limiter, 0 when power is off |
| `lux` | latest sensor reading (unsmoothed), or `null` |
| `sensor` | `ok`, `missing` (not found or read error; the engine retries every 5s), `fake` (simulator with `set_fake_lux`) |
| `power` | false after `power {"on": false}` |
| `stream.active` | external input is showing (the mode underneath is paused) |
| `stream.source` | `ddp` or `e131`: where the last complete frame came from, `null` if none yet |
| `stream.fps` | complete stream frames received per second, over the last second |
| `stream.last_packet_ms_ago` | ms since the last valid packet, `null` if none yet |
| `stream.ddp`, `stream.e131` | whether each receiver is listening |
| `w`, `h` | logical canvas size |
| `uptime_s` | seconds since the engine started. The web app uses it to restore the last mode once per engine start when `mode` is `boot` |

The engine also logs `first frame` to stderr (the journal under systemd) once, when the first frame reached the panels: `journalctl -b -u ledposter-engine -o short-monotonic | grep 'first frame'` is the cold-boot measurement in docs/ACCEPTANCE.md.

Current estimate: `sum of R+G+B / (255 * 3 * panel_size²) * amps_full_white_per_panel * output / 100`.

## Streaming input (DDP and E1.31)

Off by default (spec: Security). Turned on by `ddp.enabled` / `e131.enabled` in the config, or `set_stream`. Both listen on all interfaces.

| config key | default | |
|---|---|---|
| `ddp.port` | 4048 | |
| `e131.port` | 5568 | |
| `e131.start_universe` | 1 | universe of the top-left pixel |
| `e131.multicast` | false | also join 239.255.x.y for each universe |
| `stream.apply_gamma` | true | incoming values are sRGB-like (WLED, LedFx) and go through `display.gamma`; false: shown as sent |
| `stream.timeout_s` | 5 | after this long without a valid packet the wall goes back to its mode |

**Pixel order**: RGB, 3 bytes per pixel, row-major from the top-left of the logical canvas (the same order as the preview frame): pixel `i` is at `x = i % w`, `y = i / w`. On the full wall that is 128 x 192 = 24576 pixels, 73728 bytes. In WLED/LedFx/xLights terms: a 128 wide, 192 high matrix, horizontal rows, starting top-left, not serpentine.

**DDP**: version 1 header (10 bytes, 14 with the timecode flag); data type RGB 8-bit (`0x0B`; `0x01` and `0x00` are taken as RGB too, as LedFx and others send them); destination id 1 (or 0). `offset` is a byte offset into the frame; data past the end is clipped. If a sender ever sets the push flag, frames are shown on push; senders that never set it get every packet shown as it arrives. Queries, replies, other ids and data types, and packets shorter than their header + length are ignored.

**E1.31**: data packets only (root vector 4, framing vector 2, DMP set-property with start code 0); every layer's flags/length must match the packet. Universe `start_universe + k` carries pixels `170k` to `170k + 169` in channels 1-510 (channels 511-512 unused). The full wall is 145 universes; the last one carries 96 pixels (288 channels). A packet whose sequence number is up to 20 behind the last one for its universe (or equal) is dropped. Priority is ignored (one source at a time), preview-data and stream-terminated packets are ignored, and synchronization packets are not needed. A frame is shown when every universe has arrived, or when a universe arrives a second time (so a sender covering only part of the canvas still works; universes it doesn't send keep their last values).

**Takeover**: the first complete frame replaces the current mode (`status.mode` becomes `stream`). The mode underneath is paused, not restarted: `timeout_s` after the last valid packet (or at once when the receivers are turned off) it carries on where it was. A `set_mode` while streaming takes effect when the stream ends. Power, brightness and the limiter apply to streamed frames like any other. Receivers run on their own thread; the frame loop only copies the newest complete frame.

## Preview socket

One way, engine to client. Clients send nothing (anything sent is ignored; closing the socket disconnects). Each message is:

1. One line of JSON, ending in `\n`: the same object as the `status` response, without `id` and `ok`.
2. Exactly `w * h * 3` bytes: the frame as the panels show it, RGB24, row-major, top-left first, on the logical canvas (before the chain mapping). That is after gamma, user/auto brightness and the limiter, and black when power is off. Each value is `v * output / 100` (integer division), which is what the matrix library puts on the panel.

Read the line, parse `w` and `h` from it, then read exactly that many bytes. Repeat.

- At most `engine.preview_fps` (default 15) messages a second, and only when the frame was redrawn, so a still picture sends about 10 a second.
- A new client starts with the next frame.
- Slow clients drop frames: while a client is still receiving a message, newer frames are skipped for it, and it then gets the newest one. A message is never cut short or interleaved. A client that stops reading never slows the engine or the other clients.
- At most 8 preview clients; more are disconnected straight away. The web app should hold one connection and fan it out over its WebSocket.

Example (Python):

```python
import json, socket
s = socket.socket(socket.AF_UNIX)
s.connect("/run/ledposter/frames.sock")
f = s.makefile("rb")
while True:
    status = json.loads(f.readline())
    frame = f.read(status["w"] * status["h"] * 3)
```
