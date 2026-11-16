"""Point-cloud and mesh reconstruction for the Ferret depth scanner.

This module turns metric depth frames into 3-D geometry:

    depth_to_pointcloud(depth_mm)        single frame  -> Nx3 points (meters)
    fuse(depth_frames, poses)            multi-view    -> trimesh.Trimesh
    save_ply(path, points)               write a point cloud as PLY
    save_obj(path, mesh)                 write a mesh as OBJ

Fusion uses Open3D's TSDF volume when Open3D is importable (the high-quality
path). When it is not, it falls back to a pure numpy point merge followed by a
trimesh surface reconstruction (alpha shape / convex hull). Both paths return a
``trimesh.Trimesh`` so downstream code (meshpipe) sees one type.

All point coordinates are float32 and in METERS, matching ``ferret.calib``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import trimesh

# --- optional heavy dependency -------------------------------------------------
try:  # pragma: no cover - presence depends on the environment
    import open3d as o3d

    _HAS_OPEN3D = True
except ImportError:  # pragma: no cover
    o3d = None
    _HAS_OPEN3D = False


# --- calibration ---------------------------------------------------------------
# Device calibration (from the scanner log). focalPix is for the native 1280x800
# IR sensor and must be scaled to the depth resolution. These constants are the
# fallback used when ferret.calib is not yet available; when it is, we defer to it
# so the whole SDK shares one source of truth.
_FOCAL_PIX_NATIVE = 956.671
_NATIVE_IR_WIDTH = 1280
_DEPTH_VALUE_SCALE = 0.2  # raw uint16 -> millimeters
_TSDF_MAX_RESOLUTION = 256  # voxels per side of the uniform TSDF cube (~130 MB)


def _intrinsics(width: int, height: int) -> tuple[float, float, float, float]:
    """Return (fx, fy, cx, cy) in pixels for a depth frame of (width, height).

    Prefers ``ferret.calib.intrinsics`` so all modules agree; falls back to the
    known device constants if that module is not present yet. ``calib`` returns
    an ``Intrinsics`` namedtuple, so we read its fields rather than unpacking.
    """
    try:
        from ferret import calib  # local import: calib may be built separately

        intr = calib.intrinsics(width, height)
        return intr.fx, intr.fy, intr.cx, intr.cy
    except (ImportError, AttributeError):
        focal = _FOCAL_PIX_NATIVE * (width / _NATIVE_IR_WIDTH)
        return focal, focal, width / 2.0, height / 2.0


def _project(
    depth_mm: np.ndarray, return_mask: bool = False
) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    """Back-project a depth-in-millimeters image to Nx3 points in meters.

    Invalid pixels (depth <= 0 or non-finite) are dropped. Uses
    ``ferret.calib.depth_to_points`` when available, otherwise an internal
    pinhole projection with the device intrinsics.

    If ``return_mask`` is True, also returns the HxW boolean validity mask so a
    caller can sample an aligned image (colors) at exactly the kept pixels.
    """
    depth_mm = np.asarray(depth_mm, dtype=np.float32)
    height, width = depth_mm.shape

    try:
        from ferret import calib

        intr = calib.intrinsics(width, height)
        points, valid = calib.depth_to_points(depth_mm, intr, return_mask=True)
        points = np.asarray(points, dtype=np.float32)
        return (points, valid) if return_mask else points
    except (ImportError, AttributeError):
        pass

    fx, fy, cx, cy = _intrinsics(width, height)
    valid = np.isfinite(depth_mm) & (depth_mm > 0.0)
    rows, cols = np.nonzero(valid)
    z_m = depth_mm[rows, cols] * 0.001  # mm -> m
    x_m = (cols.astype(np.float32) - cx) * z_m / fx
    y_m = (rows.astype(np.float32) - cy) * z_m / fy
    points = np.stack((x_m, y_m, z_m), axis=1).astype(np.float32)
    if points.size == 0:
        points = np.empty((0, 3), dtype=np.float32)
    return (points, valid) if return_mask else points


# --- public API ----------------------------------------------------------------
def depth_to_pointcloud(
    depth_mm: np.ndarray,
    colors: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Convert one metric depth frame to a point cloud.

    Args:
        depth_mm: HxW float array of depths in millimeters; 0/invalid dropped.
        colors:   optional HxW(x3) image aligned to ``depth_mm``; sampled at the
                  valid pixels and returned as Nx3 uint8.

    Returns:
        (points, point_colors) where points is Nx3 float32 in meters and
        point_colors is Nx3 uint8 or None.
    """
    depth_mm = np.asarray(depth_mm, dtype=np.float32)
    points, valid = _project(depth_mm, return_mask=True)

    if colors is None:
        return points, None

    # Sample colors at EXACTLY the pixels that became points. ``valid`` is the
    # same mask the projection used, so colors and points stay in lock-step even
    # when near/far outliers are dropped by the calibration range filter.
    colors = np.asarray(colors)
    rows, cols = np.nonzero(valid)
    if colors.ndim == 2:
        sampled = colors[rows, cols][:, None].repeat(3, axis=1)
    else:
        sampled = colors[rows, cols]
    return points, sampled.astype(np.uint8)


def _apply_pose(points: np.ndarray, pose: np.ndarray) -> np.ndarray:
    """Transform Nx3 points by a 4x4 (or 3x3) pose matrix.

    A 4x4 matrix is treated as a full rigid transform (world_from_camera); a 3x3
    matrix is treated as a pure rotation.
    """
    pose = np.asarray(pose, dtype=np.float64)
    if pose.shape == (3, 3):
        return (points @ pose.T).astype(np.float32)
    if pose.shape == (4, 4):
        rotated = points @ pose[:3, :3].T
        return (rotated + pose[:3, 3]).astype(np.float32)
    raise ValueError(f"pose must be 3x3 or 4x4, got {pose.shape}")


def _merge_points(
    depth_frames: Sequence[np.ndarray],
    poses: Sequence[np.ndarray],
) -> np.ndarray:
    """Project every frame and stack into one world-space point cloud."""
    if len(depth_frames) != len(poses):
        raise ValueError(
            f"got {len(depth_frames)} frames but {len(poses)} poses"
        )
    clouds = []
    for depth, pose in zip(depth_frames, poses):
        pts = _project(depth)
        if pts.size:
            clouds.append(_apply_pose(pts, pose))
    if not clouds:
        return np.empty((0, 3), dtype=np.float32)
    return np.concatenate(clouds, axis=0).astype(np.float32)


def _mesh_from_points(points: np.ndarray) -> trimesh.Trimesh:
    """Reconstruct a surface mesh from an unordered point cloud (numpy path).

    Tries an alpha shape (concave hull) first because it follows the true object
    surface; falls back to the convex hull if the alpha shape degenerates.
    """
    if points.shape[0] < 4:
        raise ValueError("need at least 4 points to reconstruct a mesh")

    cloud = trimesh.PointCloud(points)
    # Pitch the alpha radius to the point-cloud scale so it works at any unit.
    extent = float(np.linalg.norm(np.ptp(points, axis=0)))
    alpha = max(extent / 25.0, 1e-6)

    mesh = None
    try:
        candidate = cloud.alpha_shape(alpha=alpha)  # type: ignore[attr-defined]
        if candidate is not None and len(candidate.faces) > 0:
            mesh = candidate
    except Exception:
        mesh = None

    if mesh is None:
        mesh = cloud.convex_hull

    return mesh


def fuse(
    depth_frames: Sequence[np.ndarray],
    poses: Sequence[np.ndarray],
    voxel_mm: float = 4.0,
    prefer_tsdf: bool = True,
) -> trimesh.Trimesh:
    """Fuse multiple depth frames into a single watertight-ish mesh.

    Args:
        depth_frames: sequence of HxW depth images in millimeters.
        poses:        per-frame world_from_camera transform (4x4) or rotation
                      (3x3). For a turntable, the object is rotated, so the pose
                      that brings every frame into a common world frame is the
                      inverse of the turntable rotation.
        voxel_mm:     TSDF voxel size in millimeters (Open3D path only); coarsened
                      when the scene needs more than _TSDF_MAX_RESOLUTION voxels
                      per side. ``mesh.metadata["fusion_backend"]`` records which
                      path built the mesh ("open3d_tsdf" or "numpy_points").
        prefer_tsdf:  use Open3D TSDF when available. Set False to force the
                      numpy point-merge + hull path, which is preferable for
                      sparse / point-sampled depth where TSDF over-carves.

    Returns:
        trimesh.Trimesh in METERS.
    """
    if prefer_tsdf and _HAS_OPEN3D:
        try:
            mesh = _fuse_open3d(depth_frames, poses, voxel_mm)
            mesh.metadata["fusion_backend"] = "open3d_tsdf"
            return mesh
        except Exception:
            # Any TSDF hiccup (degenerate intrinsics, empty frame) -> numpy path.
            pass

    points = _merge_points(depth_frames, poses)
    mesh = _mesh_from_points(points)
    mesh.metadata["fusion_backend"] = "numpy_points"  # FER-10: callers can tell which path ran
    return mesh


def _fuse_open3d(
    depth_frames: Sequence[np.ndarray],
    poses: Sequence[np.ndarray],
    voxel_mm: float,
) -> trimesh.Trimesh:
    """Open3D scalable TSDF fusion path. Returns a trimesh.Trimesh in meters."""
    if len(depth_frames) != len(poses):
        raise ValueError(
            f"got {len(depth_frames)} frames but {len(poses)} poses"
        )

    height, width = np.asarray(depth_frames[0]).shape
    fx, fy, cx, cy = _intrinsics(width, height)
    o3d_intr = o3d.camera.PinholeCameraIntrinsic(width, height, fx, fy, cx, cy)

    # FER-10: Open3D 0.20.0's ScalableTSDFVolume integrates nothing on macOS arm64
    # (even Open3D's own PrimeSense example), while UniformTSDFVolume works. Size a
    # uniform cube to the posed depth points, with room for the truncation band.
    points = _merge_points(depth_frames, poses)
    if points.shape[0] == 0:
        raise RuntimeError("no valid depth to fuse")
    sdf_trunc = (voxel_mm * 5.0) / 1000.0
    lower = points.min(axis=0).astype(np.float64) - 2.0 * sdf_trunc
    length = float(np.ptp(points, axis=0).max()) + 4.0 * sdf_trunc
    resolution = int(min(_TSDF_MAX_RESOLUTION, max(8, np.ceil(length / (voxel_mm / 1000.0)))))
    volume = o3d.pipelines.integration.UniformTSDFVolume(
        length=length,
        resolution=resolution,
        sdf_trunc=sdf_trunc,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.NoColor,
        origin=lower,
    )

    for depth, pose in zip(depth_frames, poses):
        depth_m = (np.asarray(depth, dtype=np.float32) * 0.001)
        depth_img = o3d.geometry.Image(np.ascontiguousarray(depth_m))
        rgb = o3d.geometry.Image(
            np.zeros((height, width, 3), dtype=np.uint8)
        )
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            rgb,
            depth_img,
            depth_scale=1.0,  # already meters
            depth_trunc=10.0,
            convert_rgb_to_intensity=False,
        )
        # TSDF wants camera_from_world (extrinsic); our pose is world_from_camera.
        pose4 = np.asarray(pose, dtype=np.float64)
        if pose4.shape == (3, 3):
            full = np.eye(4)
            full[:3, :3] = pose4
            pose4 = full
        extrinsic = np.linalg.inv(pose4)
        volume.integrate(rgbd, o3d_intr, extrinsic)

    o3d_mesh = volume.extract_triangle_mesh()
    o3d_mesh.compute_vertex_normals()
    vertices = np.asarray(o3d_mesh.vertices, dtype=np.float64)
    faces = np.asarray(o3d_mesh.triangles, dtype=np.int64)
    if len(faces) == 0:
        raise RuntimeError("TSDF produced an empty mesh")
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.metadata["tsdf_voxel_mm"] = length / resolution * 1000.0
    return mesh


# --- IO ------------------------------------------------------------------------
def save_ply(
    path: str | Path,
    points: np.ndarray,
    colors: np.ndarray | None = None,
) -> Path:
    """Write an Nx3 point cloud to a binary PLY file. Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cloud = trimesh.PointCloud(np.asarray(points, dtype=np.float32), colors=colors)
    cloud.export(path)
    return path


def save_obj(path: str | Path, mesh: trimesh.Trimesh) -> Path:
    """Write a trimesh mesh to an OBJ file. Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(path)
    return path


__all__ = [
    "depth_to_pointcloud",
    "fuse",
    "save_ply",
    "save_obj",
]
