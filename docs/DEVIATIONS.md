# Deviations

Places where the build differs from `docs/spec.md` or `docs/ARCHITECTURE.md`, and why.

## M2: animated WebP on Raspberry Pi OS Bookworm

Spec: the media pipeline accepts WebP. Bookworm ships ffmpeg 5.1, whose WebP decoder handles still images only (it skips the `ANIM`/`ANMF` chunks). Still WebP works everywhere. Animated WebP works with newer ffmpeg builds (verified with 9.0 on macOS), but on the Pi the transcoder rejects it with "Animated WebP is not supported by this system's ffmpeg; convert it to GIF or MP4" plus the supported-format list.

Fix options, if needed: decode animated WebP with Pillow (`python3-pil`) as a fallback, or install a newer ffmpeg build. Neither was done in M2, to keep the dependencies at ffmpeg plus numpy.
