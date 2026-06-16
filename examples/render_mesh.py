"""Quick headless 3/4-view render of a mesh/cloud to PNG (numpy z-buffer splat,
no OpenGL). For eyeballing results without opening Rhino.
Usage: .venv/bin/python examples/render_mesh.py artifacts/motorcycle_rhino_ready.obj
"""
import struct
import sys
import zlib
from pathlib import Path

import numpy as np
import trimesh


def png8(path: Path, arr8: np.ndarray) -> None:
    h, w = arr8.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(arr8[y].tobytes())

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


def rot(yaw_deg, pitch_deg):
    a, b = np.radians(yaw_deg), np.radians(pitch_deg)
    ry = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    rx = np.array([[1, 0, 0], [0, np.cos(b), -np.sin(b)], [0, np.sin(b), np.cos(b)]])
    return ry @ rx


def main():
    src = Path(sys.argv[1])
    m = trimesh.load(str(src), process=False)
    v = np.asarray(m.vertices, dtype=float)
    v = v - v.mean(0)
    vr = v @ rot(35, 20).T
    x, y, z = vr[:, 0], vr[:, 1], vr[:, 2]

    W = H = 700
    pad = 40
    sx = (x - x.min()) / (np.ptp(x) + 1e-9)
    sy = (y - y.min()) / (np.ptp(y) + 1e-9)
    cols = (pad + sx * (W - 2 * pad)).astype(int)
    rows = ((H - pad) - sy * (H - 2 * pad)).astype(int)
    shade = (40 + 215 * (1 - (z - z.min()) / (np.ptp(z) + 1e-9))).astype(np.uint8)

    order = np.argsort(-z)  # far first; nearer vertices overwrite (win z-buffer)
    img = np.zeros((H, W), np.uint8)
    img[rows[order], cols[order]] = shade[order]

    out = src.with_name(src.stem + "_render.png")
    png8(out, img)
    print(f"saved {out}  (verts={len(v):,})")


if __name__ == "__main__":
    main()
