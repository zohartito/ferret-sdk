"""End-to-end SIMULATED proof: fakeferret daemon -> FerretCamera -> calib ->
reconstruct -> meshpipe -> watertight OBJ + rendered PNG. No hardware, no
ports below 20010.

One command proves the full SDK against a bit-faithful daemon simulation:

    .venv/bin/python examples/e2e_simulated.py

Quantitative gates (asserted in code, non-zero exit on failure):
  * every captured frame decodes through the real RPC path;
  * median scene depth matches the synthesised standoff within 1%;
  * recovered bbox within 2% of the known box (the repo's synthetic-box
    standard, cf. tests/eval_reconstruct_baseline.json max_dim_rel_error);
  * cleaned mesh is watertight and a single connected component;
  * component count strictly reduced by clean_mesh.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:  # allow `python examples/e2e_simulated.py`
    sys.path.insert(0, str(PROJECT))

from fakeferret import FakeFerretDaemon, depth_u16_payload  # noqa: E402
from ferret.capture import FerretCamera                     # noqa: E402
from ferret import calib, meshpipe, reconstruct             # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_mesh import png8  # noqa: E402  (sibling example helper)

ARTIFACTS = PROJECT / "artifacts"

# --- synthetic scene (matches the repo's golden-box standard) --------------------
BOX_EXTENTS_MM = np.array([80.0, 60.0, 50.0])
N_VIEWS = 12
STANDOFF_MM = 600.0
DEPTH_W, DEPTH_H = 640, 800
_FOCAL = calib.FOCAL_PIX_NATIVE * (DEPTH_W / calib.NATIVE_IR_WIDTH)
FX = FY = _FOCAL
CX, CY = DEPTH_W / 2.0, DEPTH_H / 2.0


def _render_depth(angle: float, half_extents_mm: np.ndarray) -> np.ndarray:
    """Per-pixel analytic ray cast of a Y-rotated box (mm, +Z forward).

    Like a real depth sensor, every pixel centre casts a ray; the nearest
    positive slab intersection wins. Pure numpy slab test, no raycaster.
    """
    uu, vv = np.meshgrid(np.arange(DEPTH_W), np.arange(DEPTH_H))
    dirs = np.stack(
        ((uu - CX) / FX, (vv - CY) / FY, np.ones_like(uu, dtype=np.float64)),
        axis=-1,
    )
    origin = np.zeros(3)
    center = np.array([0.0, 0.0, STANDOFF_MM])
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    rot = np.array([[cos_a, 0.0, sin_a],
                    [0.0, 1.0, 0.0],
                    [-sin_a, 0.0, cos_a]])  # box local -> world

    t_enter = np.full(dirs.shape[:2], -np.inf)
    t_exit = np.full(dirs.shape[:2], np.inf)
    for axis in range(3):
        normal = rot[:, axis]
        dn = dirs @ normal                     # HxW ray·normal
        c = float(center @ normal)
        with np.errstate(divide="ignore", invalid="ignore"):
            t1 = (c - half_extents_mm[axis]) / dn
            t2 = (c + half_extents_mm[axis]) / dn
        near = np.fmin(t1, t2)
        far = np.fmax(t1, t2)
        parallel = np.abs(dn) < 1e-12
        # Parallel rays: no hit unless the origin sits inside this slab.
        inside_slab = abs(origin @ normal - c) <= half_extents_mm[axis]
        near = np.where(parallel, -np.inf, near)
        far = np.where(parallel, np.where(inside_slab, np.inf, -np.inf), far)
        t_enter = np.fmax(t_enter, near)
        t_exit = np.fmin(t_exit, far)

    hit = (t_exit >= t_enter) & (t_exit > 0.0)
    depth = np.where(hit, np.fmax(t_enter, 0.0) * dirs[..., 2], 0.0)
    return depth.astype(np.float32)


def build_scene_frames() -> tuple[list[bytes], list[np.ndarray]]:
    """Render the box from N turntable angles as wire-format Y16 payloads.

    Returns ``(payloads, depth_arrays)`` — the arrays are the ground truth the
    payloads encode, used as the per-frame capture gate reference.
    """
    half = BOX_EXTENTS_MM / 2.0
    frames, truths = [], []
    for i in range(N_VIEWS):
        angle = 2.0 * np.pi * i / N_VIEWS
        depth = _render_depth(angle, half)
        frames.append(depth_u16_payload(depth))
        truths.append(depth)
    return frames, truths


def turntable_poses() -> list[np.ndarray]:
    """world_from_camera transforms matching the rendered views."""
    standoff_m = np.array([0.0, 0.0, STANDOFF_MM * 0.001])
    poses = []
    for i in range(N_VIEWS):
        angle = 2.0 * np.pi * i / N_VIEWS
        rotation = trimesh.transformations.rotation_matrix(angle, [0, 1, 0])[:3, :3]
        pose = np.eye(4)
        pose[:3, :3] = rotation.T
        pose[:3, 3] = -rotation.T @ standoff_m
        poses.append(pose)
    return poses


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("FERRET SDK  --  SIMULATED END-TO-END (fakeferret daemon)")
    print("=" * 70)

    # 1. Start the simulator with the synthetic box scene (port 20010+ only).
    frames, truths = build_scene_frames()
    daemon = FakeFerretDaemon(
        port=20100, suffix="2-2-10", frames=frames, warmup_frames=1,
    ).start()
    print(f"[fakeferret] serving {len(frames)} Y16 frames "
          f"({len(frames[0]):,} B each) on 127.0.0.1:{daemon.bound_port}")

    # 2. Capture via the UNMODIFIED FerretCamera public API.
    log = PROJECT / "logs" / "fakeferret_daemon.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("2-2-1.0 | CR-Scan Ferret Depth Camera\n")
    captured = []
    try:
        cam = FerretCamera(host="127.0.0.1", port=daemon.bound_port,
                           log_path=log).open()
        for _ in range(N_VIEWS):
            captured.append(cam.get_depth_mm())
        cam.close()
    finally:
        daemon.stop()
    print(f"[capture]   {len(captured)} frames decoded via FerretCamera.get_depth_mm")

    # 3. Quantitative capture gate: each frame matches its own rendered truth
    #    (the RPC + Y16 quantisation path may not shift metric scale).
    medians = np.array([float(np.median(f[f > 0])) for f in captured])
    expected = np.array([float(np.median(d[d > 0])) for d in truths])
    standoff_err = float(np.max(np.abs(medians - expected) / expected))
    print(f"[gate]      per-view median depth vs scene truth: max err "
          f"{standoff_err * 100:.3f}% (scene medians ~"
          f"{float(np.median(expected)):.1f} mm at {STANDOFF_MM:.0f} mm standoff)")
    if standoff_err > 0.01:
        print("FAIL: captured depth drifted >1% from the synthesised scene")
        return 1

    # 4. Reconstruct + clean. The deterministic synthetic scene is noise-free,
    #    so a small stray island (the multipath "floaters" real scans produce)
    #    is injected into the raw mesh to exercise component reduction.
    raw_mesh = reconstruct.fuse(captured, turntable_poses(), prefer_tsdf=False)
    island = trimesh.creation.box(extents=[0.010, 0.010, 0.010])  # metres
    island.apply_translation([0.0, -0.150, 0.0])  # clear of the hull
    raw_mesh = trimesh.util.concatenate([raw_mesh, island])
    raw_path = reconstruct.save_obj(ARTIFACTS / "e2e_sim_raw.obj", raw_mesh)
    stats = meshpipe.clean_mesh(raw_path, ARTIFACTS / "e2e_sim_clean.obj",
                                target_faces=20_000)
    clean = trimesh.load(ARTIFACTS / "e2e_sim_clean.obj", force="mesh",
                         process=False)

    # 5. Geometry gates (in code, not prose).
    measured_mm = np.sort(clean.extents * 1000.0)
    expected_mm = np.sort(BOX_EXTENTS_MM)
    rel_err = float(np.max(np.abs(measured_mm - expected_mm) / expected_mm))

    print(f"[gate]      recovered bbox  : {np.round(measured_mm, 2)} mm "
          f"(expected {expected_mm.tolist()}, max rel err {rel_err * 100:.2f}%)")
    print(f"[gate]      watertight      : {stats['watertight']}")
    print(f"[gate]      components      : {stats['components_before']} -> "
          f"{stats['components_after']}")
    print(f"[gate]      faces           : {stats['faces_before']:,} -> "
          f"{stats['faces_after']:,}")

    failures = []
    if rel_err > 0.02:
        failures.append(f"bbox off by {rel_err:.3%} (>2%)")
    if not stats["watertight"]:
        failures.append("cleaned mesh not watertight")
    if stats["components_after"] != 1:
        failures.append(f"{stats['components_after']} components remain")
    if not stats["components_before"] > stats["components_after"]:
        failures.append("component count not reduced by clean_mesh")

    # 6. Rendered proof PNG (headless numpy splat, no OpenGL).
    v = np.asarray(clean.vertices, dtype=float)
    v -= v.mean(0)
    yaw, pitch = np.radians(35), np.radians(20)
    ry = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0],
                   [-np.sin(yaw), 0, np.cos(yaw)]])
    rx = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)],
                   [0, np.sin(pitch), np.cos(pitch)]])
    vr = v @ (ry @ rx).T
    W = H = 700
    pad = 40
    cols = (pad + (vr[:, 0] - vr[:, 0].min()) /
            (np.ptp(vr[:, 0]) + 1e-9) * (W - 2 * pad)).astype(int)
    rows = ((H - pad) - (vr[:, 1] - vr[:, 1].min()) /
            (np.ptp(vr[:, 1]) + 1e-9) * (H - 2 * pad)).astype(int)
    shade = (40 + 215 * (1 - (vr[:, 2] - vr[:, 2].min()) /
                         (np.ptp(vr[:, 2]) + 1e-9))).astype(np.uint8)
    order = np.argsort(-vr[:, 2])
    img = np.zeros((H, W), np.uint8)
    img[rows[order], cols[order]] = shade[order]
    png_path = ARTIFACTS / "e2e_sim_render.png"
    png8(png_path, img)
    print(f"[artifacts] {ARTIFACTS / 'e2e_sim_clean.obj'}  "
          f"+ {png_path.name} ({png_path.stat().st_size:,} B)")

    print("\n" + "=" * 70)
    if failures:
        print("E2E RESULT: FAIL —", "; ".join(failures))
        print("=" * 70)
        return 1
    print("E2E RESULT: PASS — simulated scan reproduced the box within all gates")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
