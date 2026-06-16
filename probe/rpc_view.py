#!/usr/bin/env python3
"""Pull one frame via RPC and render it multiple ways so we can identify
format/resolution (IR Y8 vs 16-bit depth). Usage: rpc_view.py [sensor] [fmt]"""
import sys
import time
import struct
import zlib
from pathlib import Path
import numpy as np
from rpc_client import MsgpackRpcClient, RpcError

OUT = Path(__file__).resolve().parent
DEV = "2-2-10"
W, H, FPS = 1280, 800, 30


def m(n):
    return f"{n}{DEV}"


def png8(path, arr8):
    h, w = arr8.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(arr8[y].tobytes())

    def ck(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)))
        f.write(ck(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(ck(b"IEND", b""))


def norm8(a):
    a = a.astype(np.float32)
    v = a[a > 0]
    if v.size == 0:
        return a.astype(np.uint8)
    lo, hi = np.percentile(v, 2), np.percentile(v, 98)
    n = np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    n[a == 0] = 0
    return n.astype(np.uint8)


def main():
    sensor = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    fmt = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    c = MsgpackRpcClient(port=10010, timeout=8.0)
    c.call(m("ob_rpc_start_stream"), sensor, fmt, W, H, FPS, timeout=8.0)
    raw = None
    for _ in range(150):
        fr = c.call(m("ob_rpc_get_frame"), timeout=5.0)
        slots = [bytes(x) for x in fr if isinstance(x, (bytes, bytearray)) and len(x) > 1000]
        if slots:
            raw = max(slots, key=len)
            break
        time.sleep(0.04)
    c.close()
    if not raw:
        print("no frame")
        return
    n = len(raw)
    print(f"sensor={sensor} fmt={fmt} frame_bytes={n}")

    # 8-bit @ 1280x800
    if n == W * H:
        a8 = np.frombuffer(raw, np.uint8).reshape(H, W)
        print(f"  u8[{H}x{W}]: {a8.min()}..{a8.max()} mean={a8.mean():.0f}")
        png8(OUT / "view_u8_1280x800.png", norm8(a8))
    # 16-bit candidates
    if n % 2 == 0:
        u16 = np.frombuffer(raw, "<u2")
        v = u16[u16 > 0]
        print(f"  u16 ({u16.size} vals): {u16.min()}..{u16.max()} "
              f"nz%={100*(u16>0).mean():.1f}"
              + (f" nzmedian={int(np.median(v))}" if v.size else ""))
        for (hh, ww) in [(800, 640), (640, 800), (800, 1280)]:
            if hh * ww == u16.size:
                png8(OUT / f"view_u16_{hh}x{ww}.png", norm8(u16.reshape(hh, ww)))
                print(f"  saved view_u16_{hh}x{ww}.png")
    print("done")


if __name__ == "__main__":
    main()
