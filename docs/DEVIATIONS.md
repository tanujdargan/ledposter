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
