"""LIVE end-to-end: capture a real depth frame from the CR-Scan Ferret over the
OEM daemon's RPC (via ferret.capture.FerretCamera), back-project it to a metric
point cloud (ferret.calib + ferret.reconstruct), and save artifacts.

This is the live counterpart to examples/e2e_offline.py — it proves the
refactored ferret package works against the physical device, not just saved data.

Requirements: CrealityScan's RPCServer daemon running, scanner plugged in.
Run:  .venv/bin/python examples/live_capture.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from ferret import calib, reconstruct          # noqa: E402
from ferret.capture import FerretCamera        # noqa: E402

ARTIFACTS = PROJECT / "artifacts"


def main() -> int:
    ARTIFACTS.mkdir(exist_ok=True)
    print("=" * 70)
    print("FERRET SDK  --  LIVE CAPTURE -> POINT CLOUD")
    print("=" * 70)
    print("opening FerretCamera (live daemon RPC; no USB claim, no root)...")

    with FerretCamera() as cam:
        print(f"  device RPC suffix : {cam._suffix}")
        depth_mm = cam.get_depth_mm()

    h, w = depth_mm.shape
    valid = depth_mm[depth_mm > 0]
    print(f"  depth frame       : {h} x {w}")
    print(f"  valid pixels      : {100 * (depth_mm > 0).mean():.1f}%  ({valid.size:,})")
    if valid.size:
        print(f"  depth mm p25/50/75: {int(np.percentile(valid, 25))} / "
              f"{int(np.percentile(valid, 50))} / {int(np.percentile(valid, 75))}")
        print(f"  center pixel mm   : {depth_mm[h // 2, w // 2]:.0f}")

    intr = calib.intrinsics(w, h)
    points = calib.depth_to_points(depth_mm, intr)
    ply = reconstruct.save_ply(ARTIFACTS / "live_capture_pointcloud.ply", points)
    np.save(ARTIFACTS / "live_capture_depth_mm.npy", depth_mm)
    print(f"  points            : {len(points):,}")
    print(f"  saved PLY         : {ply}  ({ply.stat().st_size:,} bytes)")
    print(f"  saved depth array : {ARTIFACTS / 'live_capture_depth_mm.npy'}")

    ok = valid.size > 1000 and len(points) > 1000
    print("\n" + "=" * 70)
    print("LIVE RESULT:", "PASS — real metric depth captured via ferret.capture"
          if ok else "FAIL — frame empty/too sparse (point scanner at an object ~25cm)")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
