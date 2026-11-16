"""Tests for ferret.reconstruct.

Two levels of verification:

1. REAL: back-project the captured scanner frame (probe/rpc_depth.npy) into a
   point cloud, write a temporary real_recon.ply, and assert it is non-empty with
   physically sane bounds.

2. SYNTHETIC turntable: take a known box primitive, render it from 12 turntable
   angles into synthetic depth frames with the device intrinsics, fuse with the
   known inverse-rotation poses, and assert the reconstructed bounding box
   matches the original within 10 percent. Writes a temporary synthetic_recon.obj.

The synthetic renderer is a pure-numpy pinhole z-buffer (no raycaster), so the
suite has no dependency on rtree / pyembree / Open3D.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
import pytest

from ferret import reconstruct

PROJECT = Path(__file__).resolve().parents[1]
REAL_DEPTH = PROJECT / "probe" / "rpc_depth.npy"

# Device intrinsics for a depth frame, matching ferret.calib / reconstruct.
DEPTH_W, DEPTH_H = 640, 800
_FOCAL = 956.671 * (DEPTH_W / 1280)
FX = FY = _FOCAL
CX, CY = DEPTH_W / 2.0, DEPTH_H / 2.0
DEPTH_VALUE_SCALE = 0.2  # raw uint16 -> mm


def _render_depth(vertices_world: np.ndarray) -> np.ndarray:
    """Pinhole z-buffer render of mesh vertices to an HxW depth image in mm.

    A camera at the origin looks down +Z. The nearest vertex wins each pixel.
    Returns float32 depths in millimeters (0 = no surface).
    """
    x, y, z = vertices_world[:, 0], vertices_world[:, 1], vertices_world[:, 2]
    front = z > 1.0
    x, y, z = x[front], y[front], z[front]

    cols = np.round(FX * x / z + CX).astype(int)
    rows = np.round(FY * y / z + CY).astype(int)
    inside = (cols >= 0) & (cols < DEPTH_W) & (rows >= 0) & (rows < DEPTH_H)
    cols, rows, z = cols[inside], rows[inside], z[inside]

    depth = np.zeros((DEPTH_H, DEPTH_W), dtype=np.float32)
    far_first = np.argsort(-z)  # paint far first so near surfaces overwrite
    depth[rows[far_first], cols[far_first]] = z[far_first]
    return depth


def _make_turntable(extents_mm, n_views: int = 12, standoff_mm: float = 600.0):
    """Render a box from n_views turntable angles and build matching poses.

    Returns (frames, poses, original_mesh). The poses are the world_from_camera
    transforms (4x4, meters) that bring every frame back into the object frame.
    """
    box = trimesh.creation.box(extents=extents_mm)
    for _ in range(4):  # dense surface samples for a clean z-buffer render
        box = box.subdivide()

    standoff_m = np.array([0.0, 0.0, standoff_mm * 0.001])
    frames, poses = [], []
    for i in range(n_views):
        angle = 2.0 * np.pi * i / n_views
        rot_y = trimesh.transformations.rotation_matrix(angle, [0, 1, 0])
        rotation = rot_y[:3, :3]

        view = box.copy()
        view.apply_transform(rot_y)
        view.apply_translation([0.0, 0.0, standoff_mm])
        frames.append(_render_depth(np.asarray(view.vertices)))

        # P_cam = R @ P_obj + t  ->  P_obj = R.T @ (P_cam - t)
        world_from_cam = np.eye(4)
        world_from_cam[:3, :3] = rotation.T
        world_from_cam[:3, 3] = -rotation.T @ standoff_m
        poses.append(world_from_cam)

    return frames, poses, box


# --- real data -----------------------------------------------------------------
@pytest.mark.skipif(not REAL_DEPTH.exists(), reason="real depth frame missing")
def test_real_depth_to_pointcloud(tmp_path):
    raw = np.load(REAL_DEPTH)
    assert raw.dtype == np.uint16 and raw.shape == (800, 640)

    depth_mm = raw.astype(np.float32) * DEPTH_VALUE_SCALE
    depth_mm[(raw == 0) | (raw == 65535)] = 0.0  # mark invalid as drop

    points, colors = reconstruct.depth_to_pointcloud(depth_mm)
    assert colors is None
    assert points.shape[0] > 50_000, f"too few points: {points.shape[0]}"
    assert points.shape[1] == 3 and points.dtype == np.float32
    assert np.isfinite(points).all()

    # Bounds should be physically sane: depths roughly 0.1 m .. 8 m.
    z = points[:, 2]
    assert z.min() > 0.0
    assert 0.1 < z.max() < 8.0, f"z range looks wrong: {z.min()}..{z.max()}"
    # Lateral spread must be non-trivial (a real scene, not a single column).
    assert np.ptp(points[:, 0]) > 0.05 and np.ptp(points[:, 1]) > 0.05

    out = reconstruct.save_ply(tmp_path / "real_recon.ply", points)
    assert out.exists() and out.stat().st_size > 0

    # Round-trip: the PLY must reload with the same point count.
    reloaded = trimesh.load(out)
    assert len(reloaded.vertices) == points.shape[0]


# --- synthetic turntable -------------------------------------------------------
def test_synthetic_turntable_fuse_recovers_dimensions(tmp_path):
    expected = np.array([80.0, 60.0, 50.0])  # mm
    frames, poses, original = _make_turntable(expected, n_views=12)

    assert len(frames) == 12 and len(poses) == 12
    assert all((f > 0).sum() > 300 for f in frames), "a view rendered too sparse"

    # Dimensional recovery is checked against the deterministic point-merge path.
    # TSDF (Open3D) is the right tool for dense real depth, but it legitimately
    # over-carves these single-pixel-deep synthetic point samples, so the bbox
    # claim is made on the geometry-faithful merge path.
    mesh = reconstruct.fuse(frames, poses, prefer_tsdf=False)
    assert isinstance(mesh, trimesh.Trimesh)
    assert len(mesh.faces) > 0

    measured = mesh.extents * 1000.0  # meters -> mm
    rel_err = np.abs(measured - expected) / expected
    assert (rel_err < 0.10).all(), (
        f"bbox off >10%: measured {measured} vs expected {expected} "
        f"(rel err {rel_err})"
    )

    out = reconstruct.save_obj(tmp_path / "synthetic_recon.obj", mesh)
    assert out.exists() and out.stat().st_size > 0

    # Sanity: original box extents are exactly the expected ones.
    assert np.allclose(original.extents, expected)

    # Report numbers so they show up in pytest -s output.
    print(
        f"\n[synthetic] expected mm {expected} | measured mm "
        f"{np.round(measured, 2)} | rel err {np.round(rel_err * 100, 1)}%"
    )


@pytest.mark.skipif(not reconstruct._HAS_OPEN3D, reason="Open3D not installed")
def test_open3d_tsdf_path_runs():
    """The Open3D TSDF fusion path runs end-to-end and yields a real mesh.

    We do not assert tight bbox recovery here: TSDF needs dense surface depth,
    and the synthetic point samples make it over-carve. This test guards that
    the high-quality path is wired correctly (intrinsics, extrinsics, integrate,
    extract) and returns a non-empty trimesh.
    """
    frames, poses, _ = _make_turntable(np.array([80.0, 60.0, 50.0]), n_views=12)
    mesh = reconstruct.fuse(frames, poses, voxel_mm=4.0, prefer_tsdf=True)
    assert isinstance(mesh, trimesh.Trimesh)
    assert len(mesh.vertices) > 0 and len(mesh.faces) > 0


def test_depth_to_pointcloud_drops_invalid():
    depth = np.zeros((4, 4), dtype=np.float32)
    depth[1, 1] = 500.0
    depth[2, 2] = 600.0
    points, _ = reconstruct.depth_to_pointcloud(depth)
    assert points.shape == (2, 3)
    # 500 mm and 600 mm -> 0.5 m and 0.6 m on the z axis.
    assert np.allclose(np.sort(points[:, 2]), [0.5, 0.6], atol=1e-5)


def test_colors_stay_aligned_with_points_when_outliers_dropped():
    """Regression: colors must use the SAME validity mask as the points.

    A frame with a below-floor (10 mm) and an above-ceiling (12 m) pixel plus one
    in-range pixel must yield exactly one point AND one color, and that color must
    be the in-range pixel's color (not desynced by a looser depth>0 mask).
    """
    depth = np.zeros((4, 4), dtype=np.float32)
    depth[0, 0] = 10.0       # below MIN_VALID_DEPTH_M -> dropped
    depth[1, 1] = 500.0      # in range -> kept
    depth[2, 2] = 12_000.0   # above MAX_VALID_DEPTH_M -> dropped
    colors = np.zeros((4, 4, 3), dtype=np.uint8)
    colors[1, 1] = (200, 100, 50)

    points, point_colors = reconstruct.depth_to_pointcloud(depth, colors)
    assert points.shape[0] == point_colors.shape[0] == 1
    assert tuple(point_colors[0]) == (200, 100, 50)


def test_pose_application_shapes():
    pts = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    rot_z = trimesh.transformations.rotation_matrix(np.pi / 2, [0, 0, 1])[:3, :3]
    rotated = reconstruct._apply_pose(pts, rot_z)
    assert np.allclose(rotated[0], [0.0, 1.0, 0.0], atol=1e-6)

    transform = np.eye(4)
    transform[:3, 3] = [10.0, 0.0, 0.0]
    moved = reconstruct._apply_pose(pts, transform)
    assert np.allclose(moved[0], [11.0, 0.0, 0.0], atol=1e-6)

    with pytest.raises(ValueError):
        reconstruct._apply_pose(pts, np.eye(2))


def test_fuse_rejects_mismatched_lengths():
    frame = np.zeros((8, 8), dtype=np.float32)
    frame[4, 4] = 500.0
    with pytest.raises(ValueError):
        reconstruct._merge_points([frame, frame], [np.eye(4)])
