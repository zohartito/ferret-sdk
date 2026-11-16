"""Tests for ferret.calib against the REAL captured depth frame.

The real frame ``probe/rpc_depth.npy`` is a uint16 array (800x640) of raw depth
values where ``mm = value * 0.2`` and 0 / 65535 are invalid. These tests verify
the intrinsics scaling and that back-projection produces a physically sane
metric point cloud, then dump the cloud to a temporary PLY.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ferret.calib import (
    DEPTH_VALUE_SCALE,
    FOCAL_PIX_NATIVE,
    NATIVE_IR_WIDTH,
    depth_to_points,
    intrinsics,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_DEPTH_NPY = PROJECT_ROOT / "probe" / "rpc_depth.npy"

# The real captured frame is git-ignored (*.npy) and absent on a fresh checkout
# / in CI. Tests that need it are skipped (not failed) when it is missing.
requires_real_depth = pytest.mark.skipif(
    not REAL_DEPTH_NPY.is_file(),
    reason=f"real depth fixture missing (git-ignored): {REAL_DEPTH_NPY}",
)


def _load_real_depth_mm() -> np.ndarray:
    """Load the real raw frame and convert to millimetres (float32)."""
    raw = np.load(REAL_DEPTH_NPY)
    assert raw.dtype == np.uint16, f"expected uint16, got {raw.dtype}"
    assert raw.shape == (800, 640), f"expected (800, 640), got {raw.shape}"
    return raw.astype(np.float32) * DEPTH_VALUE_SCALE


def _write_ply(path: Path, points: np.ndarray) -> None:
    """Write an Nx3 float point cloud as a binary little-endian PLY."""
    path.parent.mkdir(parents=True, exist_ok=True)
    verts = np.ascontiguousarray(points, dtype="<f4")
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {verts.shape[0]}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "end_header\n"
    )
    with path.open("wb") as fh:
        fh.write(header.encode("ascii"))
        fh.write(verts.tobytes())


def test_intrinsics_scaling():
    """fx == fy and scale linearly with width relative to the native sensor."""
    intr = intrinsics(640, 800)
    expected_focal = FOCAL_PIX_NATIVE * (640 / NATIVE_IR_WIDTH)
    assert intr.fx == pytest.approx(expected_focal)
    assert intr.fy == pytest.approx(expected_focal)
    assert intr.fx == pytest.approx(478.3355, abs=1e-3)
    assert intr.cx == pytest.approx(320.0)
    assert intr.cy == pytest.approx(400.0)

    # At native width the focal length is exactly focalPix.
    native = intrinsics(1280, 800)
    assert native.fx == pytest.approx(FOCAL_PIX_NATIVE)


def test_intrinsics_rejects_nonpositive():
    with pytest.raises(ValueError):
        intrinsics(0, 800)
    with pytest.raises(ValueError):
        intrinsics(640, -1)


@requires_real_depth
def test_point_count_matches_valid_pixels():
    """Every in-range valid pixel becomes exactly one point."""
    depth_mm = _load_real_depth_mm()
    intr = intrinsics(depth_mm.shape[1], depth_mm.shape[0])
    points = depth_to_points(depth_mm, intr)

    # Reproduce the validity mask independently from the raw frame.
    raw = np.load(REAL_DEPTH_NPY)
    depth_m = raw.astype(np.float64) * DEPTH_VALUE_SCALE / 1000.0
    expected_valid = (
        (raw != 0)
        & (raw != 65535)
        & (depth_m >= 0.05)
        & (depth_m <= 8.0)
    )

    assert points.shape[1] == 3
    assert points.dtype == np.float32
    assert points.shape[0] == int(expected_valid.sum())
    assert points.shape[0] > 0


@requires_real_depth
def test_metric_bounds_are_physically_sane():
    """Z (and thus the whole cloud) sits in a believable close-range band."""
    depth_mm = _load_real_depth_mm()
    intr = intrinsics(depth_mm.shape[1], depth_mm.shape[0])
    points = depth_to_points(depth_mm, intr)

    z = points[:, 2]
    assert np.all(np.isfinite(points))
    assert z.min() >= 0.05, f"closest point {z.min():.3f} m too near"
    assert z.max() <= 8.0, f"farthest point {z.max():.3f} m too far"
    # The Ferret is a close-range scanner; the bulk should be within a few metres.
    assert np.median(z) < 4.0, f"median depth {np.median(z):.3f} m implausibly far"

    # X/Y extents must be bounded by the field of view at the max depth, so they
    # cannot exceed Z by a wild margin given fx ~ 478 px over a 640 px frame.
    max_half_fov_ratio = (max(intr.cx, intr.cy)) / min(intr.fx, intr.fy)
    assert np.abs(points[:, 0]).max() <= z.max() * max_half_fov_ratio + 1e-3
    assert np.abs(points[:, 1]).max() <= z.max() * max_half_fov_ratio + 1e-3


@requires_real_depth
def test_writes_real_pointcloud_artifact(tmp_path):
    """Persist the real cloud to a PLY artifact and sanity-check the file."""
    depth_mm = _load_real_depth_mm()
    intr = intrinsics(depth_mm.shape[1], depth_mm.shape[0])
    points = depth_to_points(depth_mm, intr)

    artifact_ply = tmp_path / "real_depth_pointcloud.ply"
    _write_ply(artifact_ply, points)
    assert artifact_ply.exists()

    header = artifact_ply.read_bytes()[:200].decode("ascii", errors="replace")
    assert "ply" in header
    assert f"element vertex {points.shape[0]}" in header
