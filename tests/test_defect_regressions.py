"""Regression tests for defects found in the gauntlet defect hunt.

Each test here reproduces a REAL defect that failed on the pre-fix code:
  D2  disparity_to_depth_mm leaks absurd far points (no upper depth bound)
  D3  FerretCamera._decode_depth silently decodes truncated Y14 frames as Y16
  D4  FerretCamera.open() leaks the RPC socket when start_stream fails

See GAUNTLET_REPORT.md for root causes and severities.
"""
from __future__ import annotations

import numpy as np
import pytest

from ferret import capture


# --- D2 -----------------------------------------------------------------------
def test_disparity_rejects_absurd_far_depth():
    """A raw disparity just above the zero-disparity term yields an ~286 m
    'depth'. The close-range band documented on disparity_to_depth_mm must
    reject it (0.0), not hand garbage geometry to reconstruction."""
    raw = 437  # raw*unit - disp_offset = 0.5 -> depth ~ 286,000 mm
    out = capture.disparity_to_depth_mm(np.array([[raw]], dtype=np.float32))
    assert out[0, 0] == 0.0


def test_disparity_keeps_in_band_depth():
    """The band fix must not eat legitimate returns (z0 stays valid)."""
    calib = capture.DEFAULT_CALIB
    target_disp = calib.baseline * calib.focal_pix / calib.z0
    raw = (target_disp + calib.disp_offset) / calib.unit
    out = capture.disparity_to_depth_mm(np.array([[raw]], dtype=np.float32))
    assert out[0, 0] == pytest.approx(calib.z0, rel=1e-4)


# --- D3 -----------------------------------------------------------------------
def _full_y14_frame(seed: int = 7) -> bytes:
    rng = np.random.default_rng(seed)
    frame = rng.integers(0, 1 << 14, size=(800, 1280), dtype=np.uint16)
    return capture.pack_y14(frame)


def test_decode_depth_rejects_truncated_y14():
    """A Y14 payload truncated to a multiple of 1280 bytes is still even, so
    the old dispatcher handed it to the Y16 640-wide heuristic and returned
    silent garbage instead of raising."""
    full = _full_y14_frame()
    assert len(full) == 1_792_000
    truncated = full[:-1280]  # even size; u16 count divisible by 640
    cam = capture.FerretCamera()
    with pytest.raises(ValueError, match="frame size"):
        cam._decode_depth(truncated)


def test_decode_depth_still_accepts_both_complete_modes():
    """Strict dispatch must keep accepting complete Y14 and complete Y16
    frames (the daemon delivers either depending on processing mode)."""
    cam = capture.FerretCamera()
    y14 = np.full((800, 1280), 872, dtype=np.uint16)  # maps near z0
    depth = cam._decode_depth(capture.pack_y14(y14))
    assert depth.shape == (800, 1280)
    assert float(depth[400, 640]) == pytest.approx(657.34, rel=1e-3)

    y16 = np.full(512_000, 1000, dtype="<u2")  # 1,024,000-byte computed frame
    depth16 = cam._decode_depth(y16.tobytes())
    assert depth16.shape == (800, 640)
    assert float(depth16[0, 0]) == pytest.approx(200.0)
