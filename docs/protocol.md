# Engine protocol

How the web app (or a script) talks to the display engine. Two Unix stream sockets, both set in the config:

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
| `set_mode` | see below | `mode`, `mode_name` |
| `reload_config` | none | `restart_needed`: list of section names |
| `set_fake_lux` | `lux`: number >= 0, or `null` | none (simulator build only) |

**`set_brightness`** sets the user brightness (the slider). It lasts until the engine restarts or `reload_config` runs; the web app persists it by writing `brightness.user` to the config.

**`set_auto_brightness`** turns BH1750 auto brightness on or off. Effective brightness is `min(user, auto)` when on and the sensor answers, otherwise `user`.

**`power`** `{"on": false}` outputs black. The engine keeps running: modes and playlists carry on, the sockets stay up, `status` reports `power: false` and 0A.

**`set_mode`** switches what the wall shows, starting the new mode from its beginning:

```json
{"id": 3, "cmd": "set_mode", "mode": "pattern", "pattern": "solid", "color": "ff8000"}
{"id": 4, "cmd": "set_mode", "mode": "media", "media": "sunset"}
{"id": 5, "cmd": "set_mode", "mode": "playlist", "playlist": "demo"}
```

- `pattern`: `panel-id`, `solid`, `cycle`, `rows` or `cols`. `color` is optional: `"RRGGBB"`, `"#RRGGBB"` or an integer `0xRRGGBB` (default white).
- `media`: a media id, i.e. a folder in `$LEDPOSTER_DATA/media` with `meta.json` and `frames.rgb`. It loops. It fails if the item isn't transcoded or was made for another canvas size.
- `playlist`: a name under `playlists:` in the config as loaded (at start or by the last `reload_config`).

The response echoes `mode` and `mode_name` (pattern name, media id or playlist name).

**`reload_config`** re-reads the config file and applies what can change live: power ceiling and calibration, gamma, brightness settings (user, auto, curve, smoothing), `engine.preview_fps` and the playlists. If the running mode is a playlist, it restarts with the new definition. The file is checked first; if it is invalid (bad YAML, a value out of range, the running playlist removed or broken), the response is an error and the old settings stay.

`restart_needed` lists the sections that changed but only take effect after an engine restart: `matrix` (anything about the panels), `sensors`, `engine` (socket paths). Empty list: everything was applied.

```json
{"id": 6, "ok": true, "restart_needed": ["matrix"]}
```

**`set_fake_lux`** (simulator build only; the Pi build answers with an error) stands in for the BH1750 so the auto brightness curve can be tested without hardware. `null` means "no sensor". The value is read by the same polling and smoothing as a real sensor, within one `sensors.bh1750.poll_ms`.

### Status

```json
{"id": 1, "ok": true,
 "mode": "pattern", "mode_name": "solid",
 "fps": 30.3,
 "amps": 12.96, "amps_unlimited": 36.0, "limited": true, "ceiling_amps": 13.0,
 "brightness": {"user": 100, "auto": 64.7, "auto_enabled": true, "effective": 65, "output": 36},
 "lux": 50.0, "sensor": "ok",
 "power": true, "w": 128, "h": 192, "uptime_s": 312}
```

| field | meaning |
|---|---|
| `mode` | `pattern`, `media` or `playlist` |
| `mode_name` | pattern name, media id or playlist name |
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
| `w`, `h` | logical canvas size |
| `uptime_s` | seconds since the engine started |

Current estimate: `sum of R+G+B / (255 * 3 * panel_size²) * amps_full_white_per_panel * output / 100`.

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
