#!/usr/bin/env python3
"""Colorlight 5A-75B bring-up tool: talks to the card directly over raw Ethernet (protocol as in
FPP's ColorLight-5a-75.cpp; the engine's output is engine/src/colorlight.h). Run on the Pi as root
with the engine stopped (or the card gets two streams). SECS=n sets how long patterns run.
  colorlight.py IF discover                  firmware version, cabinet size, packet count
  colorlight.py IF fill R G B [W H]          solid colour (RGB; sent BGR)
  colorlight.py IF pattern NAME [W H]        numbers | panels | strips | bars | grad
  colorlight.py IF check                     numbers through LAYOUT: should read 1-6 upright
  colorlight.py IF play FRAMES.RGB [FPS]     loop a wall clip (/data/media/<id>/frames.rgb) via LAYOUT
  colorlight.py IF list EACH_S FRAMES.RGB... rotate clips
  colorlight.py IF channels|rates|strobe|variants|blink   the protocol experiments of 2026-10-24
Found that night: the card wants BGR, "show frame" twice (firmware 13.39), and a steady 60Hz
stream (30Hz or uneven made it blank the whole screen); its ports J1-J3 (= J5-J7) carry rows
0-63 / 64-127 / 128-191 and J4/J8 nothing.
"""
import socket, struct, sys, time

import os
DIM = float(os.environ.get('DIM', 1))  # scale pattern brightness (power tests)
DST, SRC = bytes.fromhex("112233445566"), bytes.fromhex("222233445566")


FONT = [s.split("|") for s in (
    "..#..|.##..|..#..|..#..|..#..|.###.", "####.|....#|.###.|#....|#....|#####", "####.|....#|.###.|....#|....#|####.",
    "#..#.|#..#.|#####|...#.|...#.|...#.", "#####|#....|####.|....#|....#|####.", ".###.|#....|####.|#...#|#...#|.###.")]


def pkt(ptype, data, size):
    p = bytearray(size)
    p[0:6], p[6:12], p[12] = DST, SRC, ptype
    p[13:13 + len(data)] = data
    return bytes(p)


def discover(ifn):
    rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0805))
    rx.bind((ifn, 0)); rx.settimeout(0.4)
    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW); tx.bind((ifn, 0))
    found = 0
    for r in range(8):
        d = bytearray(271); d[3] = r
        for _ in range(3): tx.send(pkt(0x07, d, 284))
        got = None
        t0 = time.time()
        while time.time() - t0 < 0.5:
            try: b = rx.recv(2000)
            except socket.timeout: break
            if len(b) > 1000 and b[12] == 0x08: got = b; break
        if not got: break
        d = got[13:]
        found += 1
        print(f"receiver {d[85]}: 5A card id 0x{d[0]:02x}, firmware v{d[2]}.{d[3]}, "
              f"cabinet {(d[21] << 8) | d[22]}x{(d[23] << 8) | d[24]}, "
              f"packets {struct.unpack('>I', d[38:42])[0]}, uptime {struct.unpack('>I', d[46:50])[0] / 1000:.1f}s")
        print("  raw[0:96]:", got[13:109].hex(" "))
    if not found: print("no receiver answered on", ifn)


def send_frame(ifn, rows, w, h, bright=0xFF, seconds=0, v=None, hz=60):
    v = v or {}
    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW); tx.bind((ifn, 0))
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
    pk = [] if v.get('nobright') else [pkt(0x0A, bytes([bright, bright, bright, 0xFF]), 77)]
    for y in range(h):
        row = rows[y]
        for off in range(0, w, 497):
            n = min(497, w - off)
            hdr = bytes([y >> 8, y & 255, off >> 8, off & 255, n >> 8, n & 255, 0x08, v.get('b7', 0x88)])
            pk.append(pkt(0x55, hdr + row[off * 3:(off + n) * 3], 13 + 8 + n * 3))
    sd = bytearray(99); sd[0] = v.get('s0', 0x07); sd[22] = bright; sd[23] = v.get('s23', 0x05); sd[25] = sd[26] = sd[27] = bright
    sync = pkt(0x01, sd, 112)
    t0 = time.time(); t_end = t0 + seconds; k = 0
    while True:  # resend on a fixed hz grid so the card gets an even stream
        for p in pk: tx.send(p)
        for _ in range(v.get('syncs', 2)): tx.send(sync)  # v13+ firmware wants it twice; older (v9) must get it once
        k += 1
        if time.time() >= t_end: break
        time.sleep(max(0, t0 + k / hz - time.time()))
    if seconds: print(f"sent {k} frames in {time.time() - t0:.1f}s = {k / (time.time() - t0):.0f} fps")


# Physical layout, read off the numbers photo (2026-10-24): wall position (row, col) -> (panel slot in the
# sent 128x192 frame, how that panel is turned). A panel turned 90 CW shows rot90(src, -1), so we send
# rot90(target, +k) to cancel it. Slots are row-major: slot i = sent rows 64*(i//2).., cols 64*(i%2)..
LAYOUT = {(0, 0): (4, 1), (0, 1): (5, 2),     # k = quarter turns CCW to send (= panel's CW turn)
          (1, 0): (3, 1), (1, 1): (2, 2),
          (2, 0): (0, 0), (2, 1): (1, 0)}


def remap(frame):
    """Target image (192, 128, 3) as the wall should look -> frame to send."""
    import numpy as np
    out = np.empty_like(frame)
    for (r, c), (slot, k) in LAYOUT.items():
        blk = frame[r * 64:(r + 1) * 64, c * 64:(c + 1) * 64]
        sr, sc = slot // 2, slot % 2
        out[sr * 64:(sr + 1) * 64, sc * 64:(sc + 1) * 64] = np.rot90(blk, k)
    return out


def play(ifn, path, fps, secs):
    """Loop a wall clip (/data/media/<id>/frames.rgb: raw rgb24 128x192) for secs at a fixed fps."""
    import numpy as np
    w, h = 128, 192
    data = open(path, "rb").read(); fb = w * h * 3; n = len(data) // fb
    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW); tx.bind((ifn, 0))
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
    hdrs = [pkt(0x55, bytes([y >> 8, y & 255, 0, 0, 0, w, 0x08, 0x88]), 21) for y in range(h)]
    sd = bytearray(99); sd[0] = 0x07; sd[22] = 0xFF; sd[23] = 0x05; sd[25] = sd[26] = sd[27] = 0xFF
    sync, bri = pkt(0x01, sd, 112), pkt(0x0A, bytes([255, 255, 255, 255]), 77)
    t0 = time.time(); k = 0; t_end = t0 + secs
    while time.time() < t_end:
        f = np.frombuffer(data, np.uint8, fb, (k % n) * fb).reshape(h, w, 3)
        b = remap(f)[:, :, ::-1].tobytes()   # wall layout, then RGB -> BGR
        tx.send(bri)
        for y in range(h): tx.send(hdrs[y] + b[y * w * 3:(y + 1) * w * 3])
        tx.send(sync); tx.send(sync)
        k += 1
        time.sleep(max(0, t0 + k / fps - time.time()))
    print(f"played {k} frames in {time.time() - t0:.1f}s = {k / (time.time() - t0):.1f} fps")


def build(name, w, h):
    """Test pattern rows (BGR bytes) for a w x h frame."""
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            if name == "grad":       # dim-to-bright ramps: R / G / B / white bands, the flicker torture test
                v = x * 255 // (w - 1); band = y * 4 // h
                c = [(v, 0, 0), (0, v, 0), (0, 0, v), (v, v, v)][band]
            elif name == "numbers":  # panel i (row-major in the 128x192 image) shows a big digit i+1 + corner mark
                pi = (y // 64) * (w // 64) + x // 64
                base = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)][pi % 6]
                fx, fy = (x % 64 - 14) // 7, (y % 64 - 8) // 8   # 5x6 font cells, 7x8 px each, from (14, 8)
                on = 0 <= fx < 5 and 0 <= fy < 6 and FONT[pi % 6][fy][fx] == "#"
                c = (255, 255, 255) if on else tuple(v // 4 for v in base)
                if x % 64 < 6 and y % 64 < 6: c = (255, 255, 255)   # white square = panel's top-left
            elif name == "strips":   # rows 0-63 red, 64-127 green, 128-191 blue: which chain gets which third
                c = [(255, 0, 0), (0, 255, 0), (0, 0, 255)][min(2, y // 64)]
            elif name == "panels":   # each 64x64 panel its own colour + a white border, to see the mapping
                pi = (y // 64) * (w // 64) + x // 64
                c = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)][pi % 6]
                if x % 64 in (0, 63) or y % 64 in (0, 63): c = (255, 255, 255)
                if x % 64 < 8 and y % 64 < 8: c = (0, 0, 0)   # black corner marks each panel's top-left
            elif name == "edges":    # each panel's native edges on dim grey: row 0 red, row 63 green,
                c = (40, 40, 40)     # col 0 blue, col 63 yellow; a dead/shifted/ghosting line stands out
                if x % 64 == 0: c = (0, 0, 255)
                if x % 64 == 63: c = (255, 255, 0)
                if y % 64 == 0: c = (255, 0, 0)
                if y % 64 == 63: c = (0, 255, 0)
            else:                    # bars: R G B W vertical bars
                c = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)][x * 4 // w]
            row += bytes(int(v * DIM) for v in c[::-1])  # the card takes BGR
        rows.append(bytes(row))
    return rows


def main():
    ifn, cmd, *a = sys.argv[1:]
    if cmd == "discover": return discover(ifn)
    secs = int(__import__("os").environ.get("SECS", 60))
    if cmd == "blink":  # whole screen red / blue, swapping every second: any change at all is unmissable
        w, h = (int(a[0]), int(a[1])) if len(a) > 1 else (128, 192)
        for i in range(30):
            c = bytes([255, 0, 0] if i % 2 == 0 else [0, 0, 255])
            send_frame(ifn, [c * w] * h, w, h, seconds=1)
        return
    if cmd == "variants":  # 20 s each: A red, B green, C blue, D white; the colour seen names the variant
        w, h = 256, 256
        for col, v in [((255, 0, 0), dict(s0=0x00, s23=0x00)),          # A: as a Colorlight sender card
                       ((0, 255, 0), dict(nobright=1, syncs=2)),        # B: no brightness pkt, sync twice
                       ((0, 0, 255), dict(b7=0x80)),                    # C: row header 08 80
                       ((255, 255, 255), dict(syncs=2, b7=0x80, s0=0x00, s23=0x00))]:  # D: all of them
            send_frame(ifn, [bytes(col) * w] * h, w, h, seconds=20, v=v)
        return
    if cmd == "check":  # the numbers pattern drawn as the target image, sent through remap(): should read 1-6 upright
        import numpy as np
        w, h = 128, 192
        rows = build("numbers", w, h)
        t = np.frombuffer(b"".join(rows), np.uint8).reshape(h, w, 3)[:, :, ::-1]   # back to RGB
        o = remap(np.ascontiguousarray(t))[:, :, ::-1]
        send_frame(ifn, [o[y].tobytes() for y in range(h)], w, h, seconds=secs)
        return
    if cmd == "list":  # rotate through clips: list EACH_S file...
        each, files = float(a[0]), a[1:]
        t_end = time.time() + secs; i = 0
        while time.time() < t_end:
            play(ifn, files[i % len(files)], 60, min(each, t_end - time.time())); i += 1
        return
    if cmd == "play":
        return play(ifn, a[0], int(a[1]) if len(a) > 1 else 60, secs)
    if cmd == "rates":  # 10 s each, full packet set; colours in BGR byte order
        w, h = 128, 192
        for c, hz in [((0, 0, 255), 60), ((0, 255, 0), 120), ((255, 0, 0), 250)]:  # RED 60, GREEN 120, BLUE max
            send_frame(ifn, [bytes(c) * w] * h, w, h, seconds=10, hz=hz)
        return
    if cmd == "strobe":  # 10 s each, streaming at 30 Hz; colours in BGR byte order. Which ones strobe?
        w, h = 128, 192
        for c, v in [((0, 0, 255), dict(nobright=1)),             # RED:   no brightness packet, sync x2
                     ((0, 255, 0), dict(syncs=1)),                # GREEN: brightness packet, sync x1
                     ((255, 0, 0), dict(nobright=1, syncs=1))]:   # BLUE:  neither
            send_frame(ifn, [bytes(c) * w] * h, w, h, seconds=10, v=v)
        return
    if cmd == "channels":  # 8 s each with only byte 0, then 1, then 2 of every pixel at full: maps the colour order
        w, h = 128, 192
        for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
            send_frame(ifn, [bytes(c) * w] * h, w, h, seconds=8)
            time.sleep(1)
        return
    if cmd == "fill":
        r, g, b = map(int, a[:3]); w, h = (int(a[3]), int(a[4])) if len(a) > 4 else (128, 192)
        rows = [bytes([b, g, r]) * w for _ in range(h)]  # BGR
    else:
        name = a[0]; w, h = (int(a[1]), int(a[2])) if len(a) > 2 else (128, 192)
        rows = build(name, w, h)
    send_frame(ifn, rows, w, h, seconds=secs)


if __name__ == "__main__":
    main()
