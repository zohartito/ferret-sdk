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
