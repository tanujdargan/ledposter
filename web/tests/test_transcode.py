import json
import subprocess

import numpy as np
import pytest

from ledposter.transcode import TranscodeError, transcode

W, H = 128, 192


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def frames(out):
    meta = json.loads((out / "meta.json").read_text())
    raw = (out / "frames.rgb").read_bytes()
    return meta, np.frombuffer(raw, np.uint8).reshape(-1, meta["h"], meta["w"], 3)


def check_size(out):
    """The spec's transcoder output size rule: one w*h*3 frame per duration."""
    meta, _ = frames(out)
    assert (meta["w"], meta["h"]) == (W, H)
    assert (out / "frames.rgb").stat().st_size == len(meta["durations_ms"]) * W * H * 3
    assert (out / "thumb.png").stat().st_size > 0
    return meta


@pytest.fixture
def gif(tmp_path):
    """3-frame GIF with delays patched to 100, 500, 250 ms."""
    p = tmp_path / "var.gif"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=96x64:r=1:d=3", str(p))
    d = bytearray(p.read_bytes())
    i = 0
    for cs in (10, 50, 25):
        i = d.find(b"\x21\xf9\x04", i)  # graphic control extension; delay at +4, centiseconds
        d[i + 4:i + 6] = cs.to_bytes(2, "little")
        i += 1
    p.write_bytes(d)
    return p


def test_gif_delays_preserved(gif, tmp_path):
    transcode(str(gif), tmp_path / "out")
    meta = check_size(tmp_path / "out")
    assert meta["durations_ms"] == [100, 500, 250]
    assert meta["filter"] == "smooth"


def test_1080p_capped_to_60fps(tmp_path):
    src = tmp_path / "hd.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=120:d=2", "-c:v", "libx264",
           "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src))
    meta = transcode(str(src), tmp_path / "out")
    check_size(tmp_path / "out")
    d = meta["durations_ms"]
    assert len(d) == 120  # 240 source frames, every other one kept
    assert sum(d) == 2000
    assert min(d) >= 16


def test_pixel_art_auto_pixel_and_exact_2x(tmp_path):
    rng = np.random.default_rng(1)
    art = rng.integers(0, 256, (96, 64, 3), dtype=np.uint8)
    src = tmp_path / "art.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", "64x96", "-i", "pipe:0", str(src)], input=art.tobytes(), check=True)
    meta = transcode(str(src), tmp_path / "out", gamma=1.0)
    assert meta["filter"] == "pixel"
    assert meta["durations_ms"] == [10000]
    _, f = frames(tmp_path / "out")
    assert np.array_equal(f[0], art.repeat(2, 0).repeat(2, 1))
    check_size(tmp_path / "out")


def test_fit_letterboxes_4x3_jpg(tmp_path):
    src = tmp_path / "photo.jpg"
    ffmpeg("-f", "lavfi", "-i", "color=white:s=640x480", "-frames:v", "1", str(src))
    meta = transcode(str(src), tmp_path / "fit")
    assert meta["filter"] == "smooth"
    _, f = frames(tmp_path / "fit")
    # 640x480 -> 128x96, centred: 48 black rows above and below
    assert f[0, :48].max() == 0 and f[0, 144:].max() == 0
    assert f[0, 50:142].min() > 240
    transcode(str(src), tmp_path / "fill", scale="fill")
    _, f = frames(tmp_path / "fill")
    assert f[0].min() > 240  # fill crops instead: no bars


def test_gamma_and_dither(tmp_path):
    src = tmp_path / "grey.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "128x192",
                    "-i", "pipe:0", str(src)], input=bytes([0x40]) * (W * H * 3), check=True)
    transcode(str(src), tmp_path / "g")
    _, f = frames(tmp_path / "g")
    assert np.all(f == round((0x40 / 255) ** 2.2 * 255))  # 12.3 -> 12
    transcode(str(src), tmp_path / "d", dither=True)
    _, f = frames(tmp_path / "d")
    assert set(np.unique(f)) == {12, 13}
    assert abs(f.mean() - (0x40 / 255) ** 2.2 * 255) < 0.1  # dither keeps the mean level


def test_unsupported_format(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("hello")
    with pytest.raises(TranscodeError) as e:
        transcode(str(src), tmp_path / "out")
    for fmt in ("GIF", "APNG", "PNG", "JPG", "WebP", "MP4", "WebM", "MOV"):
        assert fmt in str(e.value)


def test_corrupt_file_reports_error(tmp_path):
    src = tmp_path / "broken.mp4"
    src.write_bytes(b"\0" * 1000)
    with pytest.raises(TranscodeError, match="Supported formats"):
        transcode(str(src), tmp_path / "out")
    assert not (tmp_path / "out" / "meta.json").exists()


def test_size_limit(tmp_path):
    src = tmp_path / "huge.mp4"
    with open(src, "wb") as f:
        f.truncate(200 * 1024 * 1024 + 1)
    with pytest.raises(TranscodeError, match="200MB limit"):
        transcode(str(src), tmp_path / "out")


def test_transparency_becomes_black(tmp_path):
    art = np.zeros((192, 128, 4), np.uint8)
    art[..., 0] = 255  # red everywhere, but only the top half is opaque
    art[:96, :, 3] = 255
    src = tmp_path / "alpha.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                    "-s", "128x192", "-i", "pipe:0", str(src)], input=art.tobytes(), check=True)
    transcode(str(src), tmp_path / "out")
    _, f = frames(tmp_path / "out")
    assert np.all(f[0, :96] == [255, 0, 0]) and f[0, 96:].max() == 0
