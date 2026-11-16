"""Ferret MCP server.

Exposes the offline Ferret pipeline as MCP tools so an agent (e.g. Claude
Code) can run depth statistics, point-cloud reconstruction, mesh cleanup, and
a turntable rotation stub without touching the live scanner.

Install (wire into Claude Code):
    claude mcp add ferret -- /path/to/ferret-sdk/.venv/bin/python /path/to/ferret-sdk/ferret_mcp.py

The tool callables are also importable for testing (see tests/test_mcp.py):
    from ferret_mcp import depth_stats, reconstruct_pointcloud, clean_mesh, ...
"""

from __future__ import annotations

import os
import stat
import sys
import time
from multiprocessing import get_context
from pathlib import Path
from queue import Empty

import numpy as np
from mcp.server.mcpserver import MCPServer

# depth_mm = raw_uint16 * DEPTH_VALUE_SCALE; 0 and 65535 are invalid/no-return.
DEPTH_VALUE_SCALE = 0.2
INVALID_RAW = {0, 65535}

ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = ROOT / "artifacts"

# These tools run in a long-lived local MCP process.  They intentionally accept
# files selected by the client, so cap every operation before it can allocate or
# export data proportional to an input-controlled shape.
MAX_DEPTH_FILE_BYTES = 64 * 1024 * 1024
MAX_POINTCLOUD_POINTS = 1_000_000
MAX_DEPTH_ELEMENTS = MAX_POINTCLOUD_POINTS
MAX_DEPTH_DIMENSION = 4_096
MAX_PLY_BYTES = 32 * 1024 * 1024
PLY_WRITE_CHUNK_BYTES = 64 * 1024
MAX_MESH_INPUT_BYTES = 64 * 1024 * 1024
MESH_TIMEOUT_SECONDS = 45
MESH_SHUTDOWN_GRACE_SECONDS = 2
MESH_ADDRESS_SPACE_BYTES = 1_024 * 1024 * 1024
MESH_MEMORY_POLL_SECONDS = 0.05
MESH_OUTPUT_BYTES = 64 * 1024 * 1024
MESH_OUTPUT_SUFFIXES = {".obj", ".ply", ".stl", ".off", ".glb"}

mcp = MCPServer("ferret")


def _load_depth_mm(depth_npy_path: str) -> np.ndarray:
    """Load a saved depth frame and return it as float32 millimetres.

    Accepts either a raw uint16 sensor frame (scaled by DEPTH_VALUE_SCALE) or
    a frame already stored in millimetres. Invalid pixels (raw 0 / 65535) are
    set to 0.0.
    """
    path = Path(depth_npy_path)
    if not path.is_file():
        raise FileNotFoundError(f"depth npy not found: {path}")
    if path.suffix.lower() != ".npy":
        raise ValueError("depth input must be a .npy file")
    if path.stat().st_size > MAX_DEPTH_FILE_BYTES:
        raise ValueError(
            f"depth input exceeds {MAX_DEPTH_FILE_BYTES} byte MCP limit"
        )

    # mmap parses only the NPY header here.  Validate its shape and dtype before
    # any dtype conversion, mask, or percentile creates full-sized arrays.
    raw = np.load(path, mmap_mode="r", allow_pickle=False)
    if not isinstance(raw, np.ndarray):
        raise ValueError("depth input must contain one numeric NPY array")
    if raw.ndim != 2 or any(
        dimension <= 0 or dimension > MAX_DEPTH_DIMENSION
        for dimension in raw.shape
    ):
        raise ValueError("depth frame must be a bounded, non-empty 2-D array")
    if raw.size > MAX_DEPTH_ELEMENTS:
        raise ValueError(
            f"depth frame exceeds {MAX_DEPTH_ELEMENTS} element MCP limit"
        )
    if raw.dtype.kind not in "uif" or raw.dtype.itemsize > 8:
        raise ValueError("depth frame must use a supported real numeric dtype")
    if raw.dtype == np.uint16:
        invalid = (raw == 0) | (raw == 65535)
        depth_mm = raw.astype(np.float32) * DEPTH_VALUE_SCALE
        depth_mm[invalid] = 0.0
        return depth_mm
    return raw.astype(np.float32)


def _output_name(out_name: str, depth_npy_path: str) -> str:
    """Return one safe PLY basename, rejecting path syntax on every platform."""
    name = out_name or f"{Path(depth_npy_path).stem}.ply"
    if not name.endswith(".ply"):
        name += ".ply"
    if (
        not name
        or "\x00" in name
        or Path(name).is_absolute()
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise ValueError("out_name must be a simple .ply filename")
    return name


def _open_directory(path: Path) -> int:
    """Open a directory without accepting a symlink replacement."""
    path.mkdir(parents=True, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("output directory must be a real directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _open_artifacts_dir() -> int:
    """Open ARTIFACTS_DIR without accepting a symlink replacement."""
    return _open_directory(ARTIFACTS_DIR)


def _remove_owned_file(directory_fd: int, name: str, identity: tuple[int, int]) -> None:
    """Remove a partial file only if it is still the file this request created."""
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _write_limited(file_fd: int, data: memoryview, written: int) -> int:
    """Write in chunks while enforcing the PLY byte budget cumulatively."""
    view = data.cast("B")
    while view:
        remaining = MAX_PLY_BYTES - written
        if remaining <= 0:
            raise ValueError(f"point-cloud export exceeds {MAX_PLY_BYTES} byte MCP limit")
        count = min(len(view), PLY_WRITE_CHUNK_BYTES, remaining)
        sent = os.write(file_fd, view[:count])
        if sent <= 0:
            raise OSError("short write while exporting point cloud")
        written += sent
        view = view[sent:]
    return written


def _write_ply_exclusive(name: str, points: np.ndarray) -> Path:
    """Stream a bounded PLY, never overwriting files or following links."""
    points = np.asarray(points, dtype=np.dtype("<f4"), order="C")
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("point cloud must have shape Nx3")
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {points.shape[0]}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "end_header\n"
    ).encode("ascii")
    if len(header) + points.nbytes > MAX_PLY_BYTES:
        raise ValueError(f"point-cloud export exceeds {MAX_PLY_BYTES} byte MCP limit")

    directory_fd = _open_artifacts_dir()
    file_fd: int | None = None
    identity: tuple[int, int] | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite artifact: {name}") from exc
        except (NotImplementedError, TypeError) as exc:
            raise RuntimeError(
                "secure artifact creation requires directory-descriptor support"
            ) from exc
        try:
            created = os.fstat(file_fd)
        except BaseException:
            # Without an identity, any later pathname check/unlink can race a
            # replacement.  Retain this rare unknown partial rather than risk
            # deleting another file; the finally block still closes file_fd.
            raise
        identity = (created.st_dev, created.st_ino)
        written = _write_limited(file_fd, memoryview(header), 0)
        written = _write_limited(file_fd, memoryview(points), written)
        if written != len(header) + points.nbytes:
            raise OSError("incomplete point-cloud export")
        os.fsync(file_fd)
    except BaseException:
        if identity is not None:
            _remove_owned_file(directory_fd, name, identity)
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)
    return ARTIFACTS_DIR / name


def _apply_mesh_resource_limits() -> None:
    """Apply mandatory hard limits before parsing an MCP-selected mesh."""
    try:
        import resource

        limits = [
            (resource.RLIMIT_CPU, MESH_TIMEOUT_SECONDS),
            (resource.RLIMIT_FSIZE, MESH_OUTPUT_BYTES),
        ]
        # macOS does not support lowering RLIMIT_AS. The parent monitors RSS
        # there; CPU and file-size limits remain mandatory in the worker.
        if sys.platform != "darwin":
            limits.append((resource.RLIMIT_AS, MESH_ADDRESS_SPACE_BYTES))
        for limit, requested in limits:
            _soft, hard = resource.getrlimit(limit)
            effective = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
            if effective <= 0:
                raise ValueError("platform has no usable resource limit")
            # Retain the platform's hard ceiling: lowering it would fail when
            # the inherited soft limit is still higher (notably on macOS).
            resource.setrlimit(limit, (effective, hard))
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        raise RuntimeError(
            "bounded mesh cleanup is unavailable: required resource limits failed"
        ) from exc


def _mesh_worker(in_path: str, out_path: str, results) -> None:
    """Run native mesh work in a process the caller can terminate."""
    try:
        _apply_mesh_resource_limits()
        from ferret import meshpipe

        results.put(("ok", meshpipe.clean_mesh(in_path, out_path)))
    except BaseException as exc:
        results.put(("error", f"mesh cleanup failed safely: {type(exc).__name__}: {exc}"))


def _stop_worker(worker) -> None:
    """Terminate a timed-out worker, escalating to kill and proving it stopped."""
    worker.terminate()
    worker.join(MESH_SHUTDOWN_GRACE_SECONDS)
    if worker.is_alive():
        worker.kill()
        worker.join(MESH_SHUTDOWN_GRACE_SECONDS)
    if worker.is_alive():
        raise RuntimeError("mesh cleanup worker could not be terminated safely")


def _wait_mesh_worker(worker) -> None:
    """Wait within the wall-clock budget; supervise resident memory on macOS.

    RSS is sampled every 50 ms, so this is a termination threshold, not an
    allocation-time ceiling. Monitoring errors fail closed in the caller.
    """
    if sys.platform != "darwin":
        worker.join(MESH_TIMEOUT_SECONDS)
        return
    import psutil

    deadline = time.monotonic() + MESH_TIMEOUT_SECONDS
    while worker.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        worker.join(min(MESH_MEMORY_POLL_SECONDS, remaining))
        if not worker.is_alive():
            return
        try:
            resident = psutil.Process(worker.pid).memory_info().rss
        except psutil.NoSuchProcess:
            worker.join(MESH_SHUTDOWN_GRACE_SECONDS)
            if worker.is_alive():
                raise RuntimeError("mesh memory monitor lost the running worker")
            return
        if resident > MESH_ADDRESS_SPACE_BYTES:
            _stop_worker(worker)
            raise RuntimeError("mesh cleanup exceeded MCP resident memory limit")


def _new_mesh_tempfile(directory_fd: int, destination: Path) -> tuple[str, tuple[int, int]]:
    """Reserve an output owned by this request for worker-only mesh export."""
    for _ in range(32):
        # Keep the final suffix so trimesh chooses the requested exporter rather
        # than treating this as an unsupported ``.tmp`` file.
        name = f".{destination.stem}.{os.urandom(16).hex()}{destination.suffix}"
        try:
            file_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
        try:
            try:
                created = os.fstat(file_fd)
            except BaseException:
                # See the PLY constructor above: close the exclusive fd, but
                # retain an unknown-identity path instead of racing an unlink.
                raise
            return name, (created.st_dev, created.st_ino)
        finally:
            os.close(file_fd)
    raise RuntimeError("could not reserve safe temporary mesh output")


def _bounded_clean_mesh(in_path: str, out_path: str) -> dict:
    source = Path(in_path)
    if not source.is_file():
        raise FileNotFoundError(f"mesh input not found: {source}")
    if source.stat().st_size > MAX_MESH_INPUT_BYTES:
        raise ValueError(f"mesh input exceeds {MAX_MESH_INPUT_BYTES} byte MCP limit")

    destination = Path(out_path)
    if destination.suffix.lower() not in MESH_OUTPUT_SUFFIXES:
        raise ValueError(
            f"mesh output must use one of: {', '.join(sorted(MESH_OUTPUT_SUFFIXES))}"
        )

    directory_fd: int | None = None
    temporary_name: str | None = None
    identity: tuple[int, int] | None = None
    results = None
    worker = None
    moved = False
    try:
        directory_fd = _open_directory(destination.parent)
        temporary_name, identity = _new_mesh_tempfile(directory_fd, destination)
        temporary_path = destination.parent / temporary_name
        context = get_context("spawn")
        results = context.Queue(maxsize=1)
        worker = context.Process(
            target=_mesh_worker,
            args=(str(source), str(temporary_path), results),
        )
        worker.start()
        _wait_mesh_worker(worker)
        if worker.is_alive():
            _stop_worker(worker)
            raise TimeoutError("mesh cleanup exceeded MCP time limit and was terminated")
        if worker.exitcode != 0:
            raise RuntimeError("mesh cleanup worker exited without a safe result")
        try:
            status, result = results.get(timeout=1)
        except Empty as exc:
            raise RuntimeError("mesh cleanup worker exited without a safe result") from exc
        if status != "ok":
            raise RuntimeError(result)
        output = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(output.st_mode)
            or (output.st_dev, output.st_ino) != identity
            or output.st_size > MESH_OUTPUT_BYTES
        ):
            raise RuntimeError("mesh cleanup did not produce a bounded owned output file")
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        moved = True
        return result
    finally:
        if worker is not None and worker.is_alive():
            _stop_worker(worker)
        if (
            not moved
            and directory_fd is not None
            and temporary_name is not None
            and identity is not None
        ):
            _remove_owned_file(directory_fd, temporary_name, identity)
        if results is not None:
            try:
                results.close()
            finally:
                results.join_thread()
        if directory_fd is not None:
            os.close(directory_fd)


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

    name = _output_name(out_name, depth_npy_path)
    depth_mm = _load_depth_mm(depth_npy_path)

    result = reconstruct.depth_to_pointcloud(depth_mm)
    points = result[0] if isinstance(result, tuple) else result
    points = np.asarray(points, dtype=np.float32)
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] > MAX_POINTCLOUD_POINTS:
        raise ValueError(f"point cloud exceeds {MAX_POINTCLOUD_POINTS} point MCP limit")

    out_path = _write_ply_exclusive(name, points)

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
    _bounded_clean_mesh(in_path, out_path)
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
