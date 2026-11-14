"""End-to-end OFFLINE pass for the Ferret SDK.

Chains the whole pipeline without ever touching the live scanner:

    Stage A  REAL frame   probe/rpc_depth.npy
             -> ferret.calib.intrinsics + depth_to_points
             -> ferret.reconstruct.save_ply
             -> artifacts/e2e_real_pointcloud.ply

    Stage B  SYNTHETIC turntable (box rendered from 12 angles)
             -> ferret.reconstruct.fuse  (numpy point-merge path)
             -> raw mesh artifacts/e2e_synth_raw.obj
             -> ferret.meshpipe.clean_mesh
             -> final solid artifacts/e2e_synth_clean.obj

Run it:
    /path/to/ferret-sdk/.venv/bin/python examples/e2e_offline.py

It prints a numbered summary with concrete numbers for every stage and exits
non-zero if any stage fails, so it doubles as a CI smoke test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:  # allow `python examples/e2e_offline.py`
    sys.path.insert(0, str(PROJECT))

from ferret import calib, meshpipe, reconstruct  # noqa: E402  (after sys.path)

REAL_DEPTH = PROJECT / "probe" / "rpc_depth.npy"  # noqa: E402
ARTIFACTS = PROJECT / "artifacts"

# Device constant: raw uint16 sensor value * 0.2 -> millimetres.
DEPTH_VALUE_SCALE = 0.2

# Synthetic turntable box, in millimetres. A known ground-truth shape lets us
# measure reconstruction accuracy end-to-end.
BOX_EXTENTS_MM = np.array([80.0, 60.0, 50.0])
N_VIEWS = 12
STANDOFF_MM = 600.0


def _load_real_depth_mm() -> np.ndarray:
    """Load the captured scanner frame as float32 millimetres (0 = invalid)."""
    raw = np.load(REAL_DEPTH)
    invalid = (raw == 0) | (raw == 65535)
    depth_mm = raw.astype(np.float32) * DEPTH_VALUE_SCALE
    depth_mm[invalid] = 0.0
    return depth_mm


def stage_a_real_pointcloud() -> dict:
    """Back-project the REAL captured frame to a metric point cloud + PLY."""
    depth_mm = _load_real_depth_mm()
    height, width = depth_mm.shape

    intr = calib.intrinsics(width, height)
    points = calib.depth_to_points(depth_mm, intr)

    out = reconstruct.save_ply(ARTIFACTS / "e2e_real_pointcloud.ply", points)

    z = points[:, 2]
    return {
        "frame_shape": (height, width),
        "intrinsics": (round(intr.fx, 3), round(intr.fy, 3), intr.cx, intr.cy),
        "num_points": int(points.shape[0]),
        "z_min_m": float(z.min()),
        "z_med_m": float(np.median(z)),
        "z_max_m": float(z.max()),
        "ply_path": out,
        "ply_bytes": out.stat().st_size,
    }


def _render_depth(vertices_world: np.ndarray, intr: calib.Intrinsics) -> np.ndarray:
    """Pinhole z-buffer render of mesh vertices to an HxW depth image (mm).

    Camera at the origin looks down +Z; the nearest vertex wins each pixel.
    """
    x, y, z = vertices_world[:, 0], vertices_world[:, 1], vertices_world[:, 2]
    front = z > 1.0
    x, y, z = x[front], y[front], z[front]

    cols = np.round(intr.fx * x / z + intr.cx).astype(int)
    rows = np.round(intr.fy * y / z + intr.cy).astype(int)
    inside = (cols >= 0) & (cols < intr.width) & (rows >= 0) & (rows < intr.height)
    cols, rows, z = cols[inside], rows[inside], z[inside]

    depth = np.zeros((intr.height, intr.width), dtype=np.float32)
    far_first = np.argsort(-z)  # paint far first so near surfaces overwrite
    depth[rows[far_first], cols[far_first]] = z[far_first]
    return depth


def _make_turntable(intr: calib.Intrinsics) -> tuple[list, list, trimesh.Trimesh]:
    """Render the box from N_VIEWS turntable angles with inverse-rotation poses."""
    box = trimesh.creation.box(extents=BOX_EXTENTS_MM)
    for _ in range(4):  # dense surface samples for a clean z-buffer render
        box = box.subdivide()

    standoff_m = np.array([0.0, 0.0, STANDOFF_MM * 0.001])
    frames, poses = [], []
    for i in range(N_VIEWS):
        angle = 2.0 * np.pi * i / N_VIEWS
        rot_y = trimesh.transformations.rotation_matrix(angle, [0, 1, 0])
        rotation = rot_y[:3, :3]

        view = box.copy()
        view.apply_transform(rot_y)
        view.apply_translation([0.0, 0.0, STANDOFF_MM])
        frames.append(_render_depth(np.asarray(view.vertices), intr))

        # P_cam = R @ P_obj + t  ->  P_obj = R.T @ (P_cam - t)
        world_from_cam = np.eye(4)
        world_from_cam[:3, :3] = rotation.T
        world_from_cam[:3, 3] = -rotation.T @ standoff_m
        poses.append(world_from_cam)

    return frames, poses, box


def stage_b_synthetic_mesh() -> dict:
    """Render a turntable scan, fuse to a mesh, then clean it to a final OBJ."""
    intr = calib.intrinsics(640, 800)
    frames, poses, box = _make_turntable(intr)

    raw_mesh = reconstruct.fuse(frames, poses, prefer_tsdf=False)
    raw_path = reconstruct.save_obj(ARTIFACTS / "e2e_synth_raw.obj", raw_mesh)

    measured = raw_mesh.extents * 1000.0  # m -> mm
    rel_err = np.abs(measured - BOX_EXTENTS_MM) / BOX_EXTENTS_MM

    clean_path = ARTIFACTS / "e2e_synth_clean.obj"
    stats = meshpipe.clean_mesh(raw_path, clean_path)

    return {
        "n_views": N_VIEWS,
        "expected_mm": BOX_EXTENTS_MM,
        "measured_mm": np.round(measured, 2),
        "rel_err_pct": np.round(rel_err * 100.0, 1),
        "raw_faces": int(len(raw_mesh.faces)),
        "raw_path": raw_path,
        "clean_path": clean_path,
        "clean_bytes": clean_path.stat().st_size,
        "clean_stats": stats,
    }


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("FERRET SDK  --  OFFLINE END-TO-END PASS")
    print("=" * 70)

    a = stage_a_real_pointcloud()
    print("\n[Stage A] REAL depth -> calib -> reconstruct -> PLY")
    print(f"  input frame      : {a['frame_shape'][0]}x{a['frame_shape'][1]}  "
          f"({REAL_DEPTH.name})")
    print(f"  intrinsics fx,fy : {a['intrinsics'][0]}, {a['intrinsics'][1]} px"
          f"  (cx={a['intrinsics'][2]}, cy={a['intrinsics'][3]})")
    print(f"  points           : {a['num_points']:,}")
    print(f"  z range (m)      : {a['z_min_m']:.4f} .. "
          f"{a['z_med_m']:.4f} (med) .. {a['z_max_m']:.4f}")
    print(f"  PLY              : {a['ply_path']}  ({a['ply_bytes']:,} bytes)")

    b = stage_b_synthetic_mesh()
    print("\n[Stage B] SYNTHETIC turntable -> fuse -> clean_mesh -> OBJ")
    print(f"  views fused      : {b['n_views']}")
    print(f"  expected mm      : {b['expected_mm']}")
    print(f"  measured mm      : {b['measured_mm']}")
    print(f"  rel error %      : {b['rel_err_pct']}")
    print(f"  raw mesh faces   : {b['raw_faces']:,}  -> {b['raw_path']}")
    cs = b["clean_stats"]
    print(f"  clean_mesh       : faces {cs['faces_before']:,} -> "
          f"{cs['faces_after']:,}, components {cs['components_before']} -> "
          f"{cs['components_after']}, watertight={cs['watertight']}, "
          f"euler={cs['euler_number']}")
    print(f"  final OBJ        : {b['clean_path']}  ({b['clean_bytes']:,} bytes)")

    # Hard pass/fail gates so this script is a usable smoke test.
    ok = True
    if a["num_points"] < 50_000:
        print("\nFAIL: real point cloud too sparse")
        ok = False
    if not (0.1 < a["z_max_m"] < 8.0):
        print("\nFAIL: real depth z range implausible")
        ok = False
    if (b["rel_err_pct"] > 10.0).any():
        print("\nFAIL: synthetic bbox off by >10%")
        ok = False
    if cs["components_after"] != 1 or not cs["watertight"]:
        print("\nFAIL: cleaned mesh not a single watertight solid")
        ok = False

    print("\n" + "=" * 70)
    print("E2E RESULT:", "PASS (clean)" if ok else "FAIL")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
