#!/usr/bin/env python3
"""
Mac-native depth: pull a depth frame from the Ferret via the daemon's msgpack-RPC
(port 10010) — rides the running RPCServer, no USB claim, no root.

The daemon registers per-device RPC methods suffixed with the device's USB
port-path (e.g. ob_rpc_start_stream1-2-1). That suffix CHANGES on re-plug, so we
discover it from the daemon's enumeration log and verify it against the live server.
Usage: ferret_rpc_depth.py [sensor] [format] [Y14|Y16] (default 3 24 Y14)
The last argument identifies the daemon output mode; it is not inferred by size.
"""
import re
import sys
import time
from pathlib import Path
import numpy as np
from rpc_client import MsgpackRpcClient, RpcError
from rpc_view import png8, norm8

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
from ferret.capture import decode_depth_frame, OB_SENSOR_DEPTH, OB_FORMAT_Y14

OUT = Path(__file__).resolve().parent
LOG = Path("/Library/Creality/Log/ScannerStreamSDK.log")
W, H, FPS = 1280, 800, 30
_DEV = None


def m(name):
    return f"{name}{_DEV}"


def candidate_suffixes():
    """Device RPC suffixes derived from the depth-camera USB path in the daemon log,
    most-recent first. The suffix is the '<path>.<iface>' token with the dot before
    the interface removed (e.g. '1-2-1.0' -> '1-2-10'), plus a few fallbacks."""
    cands = []
    try:
        txt = LOG.read_text(errors="ignore")
    except Exception:
        return cands
    for full in reversed(re.findall(
            r'([0-9][0-9.\-]+) \| CR-Scan Ferret Depth Camera', txt)):
        for c in (re.sub(r'\.(\d+)$', r'\1', full),  # 1-2-1.0 -> 1-2-10  (the real one)
                  full.replace('.', ''),
                  full.replace('.', '-'),
                  full.rsplit('.', 1)[0]):
            if c and c not in cands:
                cands.append(c)
    return cands


def discover_suffix(client):
    for suf in candidate_suffixes():
        try:
            client.call(f"ob_get_stream_profile_list{suf}", timeout=3.0)
            return suf
        except RpcError as e:
            if "could not find function" in str(e):
                continue
            return suf  # device matched (errored for another reason)
        except Exception:
            continue
    return None


def main():
    global _DEV
    sensor = int(sys.argv[1]) if len(sys.argv) > 1 else 3   # OB_SENSOR_DEPTH
    fmt = int(sys.argv[2]) if len(sys.argv) > 2 else 24      # OB_FORMAT_Y14

    pixel_format = sys.argv[3] if len(sys.argv) > 3 else "Y14"
    if sensor != OB_SENSOR_DEPTH or fmt != OB_FORMAT_Y14:
        raise ValueError("depth probe requires sensor=3 and stream format=24")
    if pixel_format not in {"Y14", "Y16"}:
        raise ValueError("output pixel format must be Y14 or Y16")

    c = MsgpackRpcClient(port=10010, timeout=8.0)
    print("connected to control port 10010")

    _DEV = discover_suffix(c)
    if not _DEV:
        print("!! could not discover the device RPC suffix — is the daemon up and "
              "the scanner connected? (open CrealityScan once)")
        c.close()
        return
    print(f"device RPC suffix: {_DEV}")

    print(f"start_stream(sensor={sensor}, fmt={fmt}, {W}x{H}@{FPS})")
    try:
        print("  start_stream ->",
              c.call(m("ob_rpc_start_stream"), sensor, fmt, W, H, FPS, timeout=8.0))
    except RpcError as e:
        print("  start_stream error:", e)
        c.close()
        return

    got = None
    for i in range(150):
        fr = c.call(m("ob_rpc_get_frame"), timeout=5.0)
        slots = [bytes(x) for x in fr
                 if isinstance(x, (bytes, bytearray)) and len(x) > 1000]
        if slots:
            if len(slots) != 1:
                c.close()
                raise ValueError("ambiguous RPC frameset: multiple image payloads")
            got = slots[0]
            break
        if i % 20 == 0:
            print(f"  ...waiting for data (get_frame {i})")
        time.sleep(0.04)

    try:  # match the GUI's 5-arg stop so the stream shuts down cleanly
        c.call(m("ob_rpc_stop_stream"), sensor, fmt, W, H, FPS, timeout=3.0)
    except Exception:
        pass
    c.close()

    if not got:
        print("no non-empty frame received.")
        return

    (OUT / "rpc_raw_frame.bin").write_bytes(got)  # raw bytes for offline iteration
    n = len(got)
    print(f"saved raw frame -> rpc_raw_frame.bin ({n} bytes)")
    mm = decode_depth_frame(got, sensor=sensor, pixel_format=pixel_format)
    h2, w2 = mm.shape
    good = mm > 0
    valid = mm[good]
    print(f"\nDEPTH {h2}x{w2}  ({n} bytes)  valid={100*good.mean():.1f}%")
    if valid.size:
        print(f"depth mm  p25/p50/p75 = {int(np.percentile(valid,25))}/"
              f"{int(np.percentile(valid,50))}/{int(np.percentile(valid,75))}")
    print(f"center pixel: {mm[h2//2, w2//2]:.0f} mm")
    np.save(OUT / "rpc_depth.npy", mm)
    png8(OUT / "rpc_depth_preview.png", norm8(np.where(good, mm, 0)))
    print("saved rpc_depth.npy + rpc_depth_preview.png")


if __name__ == "__main__":
    main()
