# -*- coding: utf-8 -*-
"""Generate brief.ico -- a two-column 5+5 mark for the desktop shortcut.

Pure stdlib: builds 32bpp BGRA DIBs by hand and packs them into an .ico.
Drawn at 4x and box-downsampled so the edges are anti-aliased.
"""
import os
import struct

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief.ico")
SS = 4  # supersample factor


def rounded_rect_alpha(x, y, size, radius):
    """Coverage of a rounded square with corner radius `radius` (0..1)."""
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    dx, dy = x - cx, y - cy
    if dx * dx + dy * dy <= radius * radius:
        return 1.0
    return 0.0


def draw(size):
    S = size * SS
    r_out = S * 0.22
    r_dot = S * 0.062
    cols = (S * 0.335, S * 0.665)
    rows = [S * 0.215 + i * (S * 0.57 / 4.0) for i in range(5)]
    dot_colors = ((255, 255, 255), (240, 180, 41))   # AI white, business amber

    # supersampled buffer: (r, g, b, a) as floats 0..1
    buf = [[(0.0, 0.0, 0.0, 0.0)] * S for _ in range(S)]
    for y in range(S):
        for x in range(S):
            a = rounded_rect_alpha(x + 0.5, y + 0.5, S, r_out)
            if a <= 0:
                continue
            t = y / float(S)
            r = (0x25 + (0x14 - 0x25) * t) / 255.0
            g = (0x63 + (0x2f - 0x63) * t) / 255.0
            b = (0xeb + (0x8f - 0xeb) * t) / 255.0
            for ci, cxp in enumerate(cols):
                for cyp in rows:
                    dx, dy = (x + 0.5) - cxp, (y + 0.5) - cyp
                    if dx * dx + dy * dy <= r_dot * r_dot:
                        cr, cg, cb = dot_colors[ci]
                        r, g, b = cr / 255.0, cg / 255.0, cb / 255.0
            buf[y][x] = (r, g, b, a)

    # box downsample SS x SS -> 1
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            sr = sg = sb = sa = 0.0
            for j in range(SS):
                for i in range(SS):
                    pr, pg, pb, pa = buf[y * SS + j][x * SS + i]
                    sr += pr * pa
                    sg += pg * pa
                    sb += pb * pa
                    sa += pa
            n = SS * SS
            if sa > 0:
                row.append((sr / sa, sg / sa, sb / sa, sa / n))
            else:
                row.append((0.0, 0.0, 0.0, 0.0))
        out.append(row)
    return out


def to_bgra_rows(px, size):
    raw = bytearray()
    for y in range(size - 1, -1, -1):            # DIBs are bottom-up
        for x in range(size):
            r, g, b, a = px[y][x]
            raw += bytes((int(round(b * 255)), int(round(g * 255)),
                          int(round(r * 255)), int(round(a * 255))))
    return bytes(raw)


def build(path):
    sizes = [16, 24, 32, 48, 64, 128, 256]
    blobs, entries = [], []
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        px = draw(size)
        xor = to_bgra_rows(px, size)
        rowbytes = ((size + 31) // 32) * 4
        and_mask = bytes(rowbytes * size)        # alpha channel carries the shape
        dib = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                          len(xor) + len(and_mask), 0, 0, 0, 0)
        blob = dib + xor + and_mask
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                                   len(blob), offset))
        offset += len(blob)
        blobs.append(blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(sizes)))
        for e in entries:
            f.write(e)
        for b in blobs:
            f.write(b)
    return path


if __name__ == "__main__":
    p = build(OUT)
    print("wrote %s (%d bytes)" % (p, os.path.getsize(p)))
