"""Camera intrinsics and depth -> metric point projection for the Ferret.

The depth frames the daemon returns are pre-computed metric depth: a uint16
array where ``depth_mm = value * DEPTH_VALUE_SCALE`` and 0 / 65535 mark invalid
pixels (no return). This module turns those frames into a metric point cloud.

Calibration (from ScannerStreamSDK.log / FerretDevice.cpp):
    focalPix = 956.671   px, for the NATIVE 1280x800 IR sensor
    baseline = 149.79    mm  (stereo baseline; not needed for pre-computed depth)
    z0       = 657.34    mm
    dispOffset = 218
    unit     = 0.5
    depthValueScale = 0.2     # u16 depth * 0.2 -> millimetres

Because ``focalPix`` is defined for the 1280-wide IR sensor, it must be scaled
to whatever resolution the depth frame actually arrives at:
    fx = fy = focalPix * (depth_width / 1280)
The principal point is taken as the image centre (cx = W/2, cy = H/2), which is
the convention the daemon's own pipeline uses.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

# --- Device calibration constants (see module docstring) ---------------------
FOCAL_PIX_NATIVE = 956.671  # px, calibrated for the 1280-wide IR sensor
NATIVE_IR_WIDTH = 1280
DEPTH_VALUE_SCALE = 0.2  # raw u16 depth value -> millimetres

# Sentinel raw u16 values that mean "no measurement". In the mm domain the high
# sentinel (65535) arrives as 13107 mm and is rejected by MAX_VALID_DEPTH_M, so
# valid_mask only checks the 0 sentinel explicitly. Kept for documentation.
INVALID_DEPTH_VALUES = (0, 65535)

# Plausibility bounds for a returned metric depth, in metres. The Ferret is a
# close-range scanner; anything outside this band is noise rather than geometry.
MIN_VALID_DEPTH_M = 0.05
MAX_VALID_DEPTH_M = 8.0

_MM_PER_M = 1000.0


class Intrinsics(NamedTuple):
    """Pinhole camera intrinsics in pixels, for a specific image size."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int


def intrinsics(width: int, height: int) -> Intrinsics:
    """Return pinhole intrinsics for a depth frame of ``width`` x ``height``.

    The calibrated focal length is for the native 1280-wide IR sensor, so it is
    scaled by ``width / 1280`` to match the depth resolution. fx == fy because
    the sensor pixels are square. The principal point is the image centre.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"width and height must be positive, got {width}x{height}")

    focal = FOCAL_PIX_NATIVE * (width / NATIVE_IR_WIDTH)
    return Intrinsics(
        fx=focal,
        fy=focal,
        cx=width / 2.0,
        cy=height / 2.0,
        width=width,
        height=height,
    )


def valid_mask(depth_mm: np.ndarray) -> np.ndarray:
    """Return the HxW boolean mask of pixels that survive validity filtering.

    A pixel is valid only if its metric depth is finite, is not a raw sentinel
    (0 mm), and falls within ``[MIN_VALID_DEPTH_M, MAX_VALID_DEPTH_M]``. This is
    the single source of truth for "which pixels become points", so callers that
    need to sample an aligned image (e.g. colors) at exactly the kept pixels can
    reuse it instead of recomputing a looser predicate.

    Note: ``depth_mm`` is millimetres, so the raw-65535 sentinel arrives as
    13107 mm; the range bound (> 8 m) rejects it, which is why only the 0 mm
    sentinel needs an explicit check here.
    """
    depth = np.asarray(depth_mm, dtype=np.float64)
    depth_m = depth / _MM_PER_M
    mask = np.isfinite(depth)
    mask &= depth != 0  # 0 mm sentinel; the high sentinel is caught by the range
    mask &= depth_m >= MIN_VALID_DEPTH_M
    mask &= depth_m <= MAX_VALID_DEPTH_M
    return mask


def depth_to_points(
    depth_mm: np.ndarray,
    intr: Intrinsics,
    return_mask: bool = False,
) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    """Back-project a metric-depth frame to an Nx3 point cloud in METERS.

    ``depth_mm`` is the depth in millimetres (HxW float or int). Invalid pixels
    are dropped: any pixel that is non-finite, holds a raw sentinel value
    (0 or 65535 mm), or whose metric depth falls outside the plausible range.

    Returns a float32 array of shape (N, 3) with columns (X, Y, Z) in metres,
    in the camera frame: +X right, +Y down, +Z forward (out of the lens).

    If ``return_mask`` is True, also returns the HxW boolean validity mask so a
    caller can sample an aligned image (e.g. colors) at exactly the kept pixels.
    """
    depth = np.asarray(depth_mm)
    if depth.ndim != 2:
        raise ValueError(f"depth_mm must be 2-D HxW, got shape {depth.shape}")
    if (depth.shape[1], depth.shape[0]) != (intr.width, intr.height):
        raise ValueError(
            f"depth frame {depth.shape[1]}x{depth.shape[0]} does not match "
            f"intrinsics {intr.width}x{intr.height}"
        )

    depth = depth.astype(np.float64, copy=False)
    depth_m = depth / _MM_PER_M

    valid = valid_mask(depth)

    rows, cols = np.nonzero(valid)  # rows = v (y), cols = u (x)
    z = depth_m[rows, cols]
    x = (cols - intr.cx) * z / intr.fx
    y = (rows - intr.cy) * z / intr.fy

    points = np.column_stack((x, y, z)).astype(np.float32)
    if return_mask:
        return points, valid
    return points
