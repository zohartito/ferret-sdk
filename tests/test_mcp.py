"""Tests for ferret_mcp without needing a live MCP client.

Verifies the expected tools are registered on the FastMCP server and exercises
the underlying tool callables directly against the real probe depth frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ferret_mcp

REAL_DEPTH = ROOT / "probe" / "rpc_depth.npy"

# The real depth frame is git-ignored (*.npy) and absent on a fresh checkout /
# in CI. Tests that need it are skipped (not failed) when it is missing.
requires_real_depth = pytest.mark.skipif(
    not REAL_DEPTH.is_file(),
    reason=f"real depth fixture missing (git-ignored): {REAL_DEPTH}",
)

EXPECTED_TOOLS = {
    "depth_stats",
    "reconstruct_pointcloud",
    "clean_mesh",
    "turntable_rotate",
    "list_artifacts",
}


def _registered_tool_names() -> set[str]:
    return {tool.name for tool in ferret_mcp.mcp._tool_manager.list_tools()}


def test_server_named_ferret():
    assert ferret_mcp.mcp.name == "ferret"


def test_expected_tools_registered():
    registered = _registered_tool_names()
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"missing tools: {missing}"


@requires_real_depth
def test_depth_stats_on_real_frame():
    stats = ferret_mcp.depth_stats(str(REAL_DEPTH))

    assert stats["shape"] == [800, 640]
    assert stats["total_pixels"] == 800 * 640
    assert 0 < stats["valid_pixels"] <= stats["total_pixels"]
    assert 0.0 < stats["valid_pct"] <= 100.0
    # Percentiles ordered and within the scanner's plausible metric range.
    assert stats["p25_mm"] <= stats["p50_mm"] <= stats["p75_mm"]
    assert 0.0 < stats["p25_mm"] < 10000.0


def test_depth_stats_invalid_pixels_excluded(tmp_path):
    # Raw uint16 frame: 0 and 65535 are invalid and must not count as valid.
    frame = np.zeros((4, 4), dtype=np.uint16)
    frame[0, 0] = 65535  # invalid
    frame[0, 1] = 1000   # valid -> 200.0 mm
    frame[0, 2] = 2000   # valid -> 400.0 mm
    npy = tmp_path / "tiny.npy"
    np.save(npy, frame)

    stats = ferret_mcp.depth_stats(str(npy))
    assert stats["valid_pixels"] == 2
    assert stats["p50_mm"] == pytest.approx(300.0)


def test_turntable_rotate_stub():
    result = ferret_mcp.turntable_rotate(90)
    assert result["status"] == "ok"
    assert result["degrees"] == 90.0


def test_list_artifacts_shape():
    result = ferret_mcp.list_artifacts()
    assert "artifacts_dir" in result
    assert isinstance(result["files"], list)


@requires_real_depth
@pytest.mark.skipif(
    pytest.importorskip("ferret.reconstruct", reason="ferret.reconstruct not built yet")
    is None,
    reason="ferret.reconstruct not built yet",
)
def test_reconstruct_pointcloud_writes_ply():
    result = ferret_mcp.reconstruct_pointcloud(str(REAL_DEPTH), out_name="test_mcp_scan.ply")

    ply_path = Path(result["ply_path"])
    assert ply_path.is_file(), "reconstruct_pointcloud did not produce a PLY"
    assert ply_path.stat().st_size > 0
    assert result["num_points"] > 0
