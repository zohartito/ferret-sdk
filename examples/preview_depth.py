"""Render a saved depth_mm .npy to a normalized grayscale PNG for quick viewing.
Usage: .venv/bin/python examples/preview_depth.py artifacts/live_capture_depth_mm.npy
"""
import struct
import sys
import zlib
from pathlib import Path

import numpy as np


def norm8(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    valid = a[a > 0]
    if valid.size == 0:
        return np.zeros(a.shape, np.uint8)
    lo, hi = np.percentile(valid, 2), np.percentile(valid, 98)
    n = np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    n[a == 0] = 0  # invalid stays black
    return n.astype(np.uint8)


def png8(path: Path, arr8: np.ndarray) -> None:
    h, w = arr8.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(arr8[y].tobytes())

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


def main() -> None:
    npy = Path(sys.argv[1] if len(sys.argv) > 1
               else "artifacts/live_capture_depth_mm.npy")
    depth = np.load(npy)
    out = npy.with_name(npy.stem + "_preview.png")
    png8(out, norm8(depth))
    print(f"saved {out}  ({depth.shape})")


if __name__ == "__main__":
    main()
