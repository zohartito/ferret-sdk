"""Ferret MCP server.

Exposes the offline Ferret pipeline as MCP tools so an agent (e.g. Claude
Code) can run depth statistics, point-cloud reconstruction, mesh cleanup, and
a turntable rotation stub without touching the live scanner.

Install (wire into Claude Code):
    claude mcp add ferret -- /Users/zohartito/projects/ferret-sdk/.venv/bin/python /Users/zohartito/projects/ferret-sdk/ferret_mcp.py

The tool callables are also importable for testing (see tests/test_mcp.py):
    from ferret_mcp import depth_stats, reconstruct_pointcloud, clean_mesh, ...
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

# depth_mm = raw_uint16 * DEPTH_VALUE_SCALE; 0 and 65535 are invalid/no-return.
DEPTH_VALUE_SCALE = 0.2
INVALID_RAW = {0, 65535}

ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = ROOT / "artifacts"

mcp = FastMCP("ferret")


def _load_depth_mm(depth_npy_path: str) -> np.ndarray:
    """Load a saved depth frame and return it as float32 millimetres.

    Accepts either a raw uint16 sensor frame (scaled by DEPTH_VALUE_SCALE) or
    a frame already stored in millimetres. Invalid pixels (raw 0 / 65535) are
    set to 0.0.
    """
    path = Path(depth_npy_path)
    if not path.is_file():
        raise FileNotFoundError(f"depth npy not found: {path}")

    raw = np.load(path)
    if raw.dtype == np.uint16:
        invalid = (raw == 0) | (raw == 65535)
        depth_mm = raw.astype(np.float32) * DEPTH_VALUE_SCALE
        depth_mm[invalid] = 0.0
        return depth_mm
    return raw.astype(np.float32)


@mcp.tool()
def depth_stats(depth_npy_path: str) -> dict:
    """Compute coverage and depth percentiles for a saved depth frame.

    Args:
        depth_npy_path: Path to a .npy depth frame (uint16 raw or float mm).

    Returns:
        dict with shape, valid pixel count, valid percentage, and the p25/p50/
        p75 depth percentiles in millimetres (computed over valid pixels only).
    """
    depth_mm = _load_depth_mm(depth_npy_path)
    valid_mask = depth_mm > 0.0
    valid_count = int(valid_mask.sum())
    total = int(depth_mm.size)
    valid = depth_mm[valid_mask]

    if valid_count == 0:
        p25 = p50 = p75 = None
    else:
        p25, p50, p75 = (float(v) for v in np.percentile(valid, [25, 50, 75]))

    return {
        "path": str(Path(depth_npy_path)),
        "shape": list(depth_mm.shape),
        "valid_pixels": valid_count,
        "total_pixels": total,
        "valid_pct": round(100.0 * valid_count / total, 2) if total else 0.0,
        "p25_mm": p25,
        "p50_mm": p50,
        "p75_mm": p75,
    }


@mcp.tool()
def reconstruct_pointcloud(depth_npy_path: str, out_name: str = "") -> dict:
    """Reconstruct a point cloud from a depth frame and write a PLY.

    Delegates to ferret.reconstruct for projection/export. The PLY is written
    into the artifacts directory.

    Args:
        depth_npy_path: Path to a .npy depth frame.
        out_name: Optional output filename (e.g. "scan.ply"). Defaults to the
            input stem with a .ply extension.

    Returns:
        dict with the written PLY path and the point count.
    """
    from ferret import reconstruct  # lazy: sibling module, imported on use only

    depth_mm = _load_depth_mm(depth_npy_path)

    result = reconstruct.depth_to_pointcloud(depth_mm)
    points = result[0] if isinstance(result, tuple) else result
    points = np.asarray(points, dtype=np.float32)

    stem = out_name or f"{Path(depth_npy_path).stem}.ply"
    if not stem.endswith(".ply"):
        stem += ".ply"
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ARTIFACTS_DIR / stem

    reconstruct.save_ply(str(out_path), points)

    return {
        "ply_path": str(out_path),
        "num_points": int(points.shape[0]),
    }


@mcp.tool()
def clean_mesh(in_path: str, out_path: str) -> dict:
    """Clean a mesh: largest component, fill holes, recenter, Z-up, decimate.

    Delegates to ferret.meshpipe.clean_mesh.

    Args:
        in_path: Path to the input mesh (e.g. .obj / .ply / .stl).
        out_path: Path to write the cleaned mesh.

    Returns:
        dict with the output path and whether the file was produced.
    """
    from ferret import meshpipe  # lazy: sibling module, imported on use only

    meshpipe.clean_mesh(in_path, out_path)
    return {
        "out_path": str(out_path),
        "written": Path(out_path).is_file(),
    }


@mcp.tool()
def turntable_rotate(degrees: float) -> dict:
    """Rotate the turntable (stub).

    Real hardware control is provided by hardware/turntable/turntable_control.py.
    This stub validates the request and returns ok so the agent workflow can be
    exercised offline without a serial-connected stepper.

    Args:
        degrees: Rotation amount in degrees.

    Returns:
        dict echoing the requested rotation with status "ok".
    """
    return {
        "status": "ok",
        "degrees": float(degrees),
        "note": "stub: no hardware moved (see turntable_control.py for real impl)",
    }


@mcp.tool()
def list_artifacts() -> dict:
    """List inspectable output files in the artifacts directory.

    Returns:
        dict with the artifacts directory and a list of {name, bytes} entries
        (PLY/STL/OBJ/PNG and any other files), sorted by name.
    """
    if not ARTIFACTS_DIR.is_dir():
        return {"artifacts_dir": str(ARTIFACTS_DIR), "files": []}

    files = [
        {"name": p.name, "bytes": p.stat().st_size}
        for p in sorted(ARTIFACTS_DIR.iterdir())
        if p.is_file() and p.name != ".gitkeep"
    ]
    return {"artifacts_dir": str(ARTIFACTS_DIR), "files": files}


if __name__ == "__main__":
    mcp.run()
