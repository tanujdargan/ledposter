"""DDP and E1.31 input against the simulator build (engine/src/stream.h, docs/protocol.md).

Needs engine/build/ledengine-sim (cd engine && make sim); skipped without it.
"""
import socket
import struct
import threading
import time

import pytest

from test_engine_ipc import BUTTON, ENGINE, FRAME, H, W, engine, preview_conn, read_preview  # noqa: F401

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="engine sim not built (cd engine && make sim)")


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stream_config(ddp_port, e131_port, ddp=True, e131=True, apply_gamma=True):
    b = lambda v: str(v).lower()  # noqa: E731
    return (f"ddp: {{enabled: {b(ddp)}, port: {ddp_port}}}\n"
            f"e131: {{enabled: {b(e131)}, port: {e131_port}, start_universe: 1}}\n"
            f"stream: {{apply_gamma: {b(apply_gamma)}, timeout_s: 1}}\n")


def ddp_packets(rgb, chunk=1440):
    """One frame as DDP packets (RGB 8-bit, output id 1), push flag on the last one."""
    out = []
    for off in range(0, len(rgb), chunk):
        data = rgb[off:off + chunk]
        flags = 0x40 | (0x01 if off + chunk >= len(rgb) else 0)
        out.append(struct.pack("!BBBBIH", flags, len(out) & 0xF, 0x0B, 1, off, len(data)) + data)
    return out


def e131_packet(universe, seq, dmx):
    n = 126 + len(dmx)
    root = struct.pack("!HH12sHI16s", 0x0010, 0, b"ASC-E1.17\0\0\0", 0x7000 | (n - 16), 4, b"\x01" * 16)
    framing = struct.pack("!HI64sBHBBH", 0x7000 | (n - 38), 2, b"pytest", 100, 0, seq, 0, universe)
    dmp = struct.pack("!HBBHHHB", 0x7000 | (n - 115), 2, 0xA1, 0, 1, len(dmx) + 1, 0)
    return root + framing + dmp + dmx


def e131_packets(rgb, seq):
    per = 170 * 3
    return [e131_packet(u + 1, seq, rgb[u * per:(u + 1) * per]) for u in range((len(rgb) + per - 1) // per)]


def marked_frame(marks):
    """Black canvas with a few pixels set, {(x, y): (r, g, b)}: stays far under the limiter."""
    f = bytearray(FRAME)
    for (x, y), c in marks.items():
        f[(y * W + x) * 3:(y * W + x) * 3 + 3] = bytes(c)
    return bytes(f)


def px(data, x, y):
    return tuple(data[(y * W + x) * 3:(y * W + x) * 3 + 3])


def preview_where(path, pred, timeout=3.0):
    """The first preview (status, frame) satisfying pred(status, frame)."""
    with preview_conn(path) as s:
        f = s.makefile("rb")
        end = time.monotonic() + timeout
        while True:
            st, data = read_preview(f)
            if pred(st, data):
                return st, data
            assert time.monotonic() < end, st


@pytest.fixture
def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    yield lambda packets, port: [s.sendto(p, ("127.0.0.1", port)) for p in packets]
    s.close()


def test_ddp_takes_over_and_reverts(engine, udp):
    dport, eport = free_udp_port(), free_udp_port()
    e = engine("--pattern", "solid", "--color", "000040", extra=stream_config(dport, eport))
    assert e.ctl.ok("status")["stream"] == {"active": False, "source": None, "fps": 0,
                                            "last_packet_ms_ago": None, "ddp": True, "e131": True}
    frame = marked_frame({(0, 0): (255, 0, 0), (127, 191): (0, 0, 255), (5, 3): (128, 128, 128)})
    end = time.monotonic() + 1.2
    while time.monotonic() < end:  # 20 fps
        udp(ddp_packets(frame), dport)
        time.sleep(0.05)
    st = e.ctl.ok("status")
    assert st["mode"] == "stream" and st["mode_name"] == "ddp"
    assert st["stream"]["active"] and st["stream"]["source"] == "ddp" and st["stream"]["fps"] > 5
    assert 0 <= st["stream"]["last_packet_ms_ago"] < 500
    _, data = preview_where(e.prev_path, lambda s, d: s["mode"] == "stream")
    assert px(data, 0, 0) == (255, 0, 0) and px(data, 127, 191) == (0, 0, 255)
    assert px(data, 5, 3) == (56, 56, 56)  # stream.apply_gamma: through the engine's 2.2 LUT
    assert px(data, 1, 0) == (0, 0, 0)

    # Malformed packets neither change the picture nor keep the stream alive: back after timeout_s (1s).
    t0 = time.monotonic()
    udp([b"\x41\x00\x0b\x01junk", b"\x80" * 20], dport)
    st = e.ctl.wait(lambda s: s["mode"] == "pattern", timeout=3)
    assert time.monotonic() - t0 > 0.5
    assert st["mode_name"] == "solid" and not st["stream"]["active"] and st["stream"]["source"] == "ddp"
    _, data = preview_where(e.prev_path, lambda s, d: s["mode"] == "pattern")
    assert px(data, 0, 0) == (0, 0, 12)  # the solid colour again (0x40 through gamma)


def test_e131_universes_and_set_stream(engine, udp):
    dport, eport = free_udp_port(), free_udp_port()
    e = engine("--pattern", "panel-id", extra=stream_config(dport, eport, ddp=False, apply_gamma=False))
    assert e.ctl.ok("status")["stream"]["ddp"] is False
    last = (W - 1, H - 1)  # in universe 145, which carries only 96 pixels
    frame = marked_frame({(0, 0): (10, 20, 30), (170 % W, 170 // W): (1, 2, 3), last: (0, 200, 0)})
    for seq in range(3):
        for p in e131_packets(frame, seq):
            udp([p], eport)
            time.sleep(0.0002)  # don't overrun the socket buffer
    st = e.ctl.wait(lambda s: s["mode"] == "stream")
    assert st["mode_name"] == "e131"
    _, data = preview_where(e.prev_path, lambda s, d: s["mode"] == "stream")
    assert px(data, 0, 0) == (10, 20, 30)  # apply_gamma false: values as sent
    assert px(data, 170 % W, 170 // W) == (1, 2, 3)  # first pixel of universe 2
    assert px(data, *last) == (0, 200, 0)

    # set_stream turns the receiver off: the wall goes back to its mode right away.
    r = e.ctl.ok("set_stream", e131=False)
    assert (r["ddp"], r["e131"]) == (False, False)
    st = e.ctl.wait(lambda s: s["mode"] == "pattern", timeout=0.5)
    assert st["mode_name"] == "panel-id" and st["stream"]["e131"] is False
    udp(e131_packets(frame, 10), eport)
    time.sleep(0.3)
    assert e.ctl.ok("status")["mode"] == "pattern"
    assert e.ctl.ok("set_stream", ddp=True)["ddp"] is True
    r = e.ctl("set_stream")
    assert r["ok"] is False and "'ddp'" in r["error"]
    r = e.ctl("set_stream", e131="yes")
    assert r["ok"] is False and "'e131' must be true or false" in r["error"]


def test_paused_media_comes_back(engine, udp):
    dport, eport = free_udp_port(), free_udp_port()
    e = engine("--play", "grey", extra=stream_config(dport, eport, e131=False))
    e.ctl.wait(lambda s: s["mode"] == "media")
    for _ in range(5):
        udp(ddp_packets(marked_frame({(0, 0): (255, 255, 255)})), dport)
        time.sleep(0.05)
    e.ctl.wait(lambda s: s["mode"] == "stream")
    st = e.ctl.wait(lambda s: s["mode"] == "media", timeout=3)
    assert st["mode_name"] == "grey"
    _, data = preview_where(e.prev_path, lambda s, d: s["mode"] == "media")
    assert px(data, 0, 0) == px(data, 1, 0) != (0, 0, 0)  # the grey clip, not the streamed dot


def test_reload_config_applies_stream_settings(engine, udp):
    dport, eport = free_udp_port(), free_udp_port()
    e = engine("--pattern", "panel-id")
    assert e.ctl.ok("status")["stream"]["ddp"] is False  # off by default
    e.write_config(user=100, extra=stream_config(dport, eport, e131=False))
    assert e.ctl.ok("reload_config")["restart_needed"] == []
    assert e.ctl.ok("status")["stream"]["ddp"] is True
    udp(ddp_packets(marked_frame({(1, 1): (9, 9, 9)})), dport)
    e.ctl.wait(lambda s: s["mode"] == "stream")

    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # someone else has the E1.31 port
    busy.bind(("0.0.0.0", eport))
    try:
        e.write_config(user=70, extra=stream_config(dport, eport))
        r = e.ctl("reload_config")
        assert r["ok"] is False and f"UDP port {eport}" in r["error"]
        st = e.ctl.wait(lambda s: s["brightness"]["user"] == 70)  # the rest still applied
        assert st["stream"]["ddp"] is True and st["stream"]["e131"] is False
    finally:
        busy.close()


def test_overlay_on_top_and_button_ends_takeover(engine):
    dport, eport = free_udp_port(), free_udp_port()
    e = engine(extra=BUTTON + stream_config(dport, eport, e131=False))
    stop = threading.Event()

    def sender():  # a sender that keeps going, like LedFx
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            while not stop.is_set():
                for p in ddp_packets(marked_frame({(0, 0): (255, 0, 0)})):
                    s.sendto(p, ("127.0.0.1", dport))
                time.sleep(0.05)

    t = threading.Thread(target=sender)
    t.start()
    try:
        e.ctl.wait(lambda s: s["mode"] == "stream")
        e.ctl.ok("show_text", text="hello", seconds=5)  # overlays draw on top of the stream
        st, data = preview_where(e.prev_path, lambda s, d: "text" in s["overlays"] and sum(d) > 5000)  # fades in
        assert st["mode"] == "stream" and px(data, 0, 0) == (255, 0, 0)  # the stream around the text band
        e.ctl.ok("show_text", text="")

        e.ctl.ok("sim_button", ms=60)  # short press: back to the mode, then the next cycle entry
        st = e.ctl.wait(lambda s: s["mode"] == "effect")
        assert st["mode_name"] == "plasma" and not st["stream"]["active"]
        time.sleep(0.5)
        assert e.ctl.ok("status")["mode"] == "effect"  # packets still arrive: the stream stays dismissed
    finally:
        stop.set()
        t.join()
    time.sleep(1.3)  # quiet for timeout_s (1s): the next stream takes over again
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for p in ddp_packets(marked_frame({(0, 0): (0, 255, 0)})):
            s.sendto(p, ("127.0.0.1", dport))
    e.ctl.wait(lambda s: s["mode"] == "stream")
