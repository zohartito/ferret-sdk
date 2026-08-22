"""Tests for ferret_mcp without needing a live MCP client.

Verifies the expected tools are registered on the FastMCP server and exercises
the underlying tool callables directly against the real probe depth frame.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ferret_mcp  # noqa: E402

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


def test_depth_stats_rejects_array_above_element_limit(tmp_path, monkeypatch):
    npy = tmp_path / "too_many.npy"
    np.save(npy, np.ones((3, 3), dtype=np.uint16))
    monkeypatch.setattr(ferret_mcp, "MAX_DEPTH_ELEMENTS", 4)

    with pytest.raises(ValueError, match="element MCP limit"):
        ferret_mcp.depth_stats(str(npy))


def test_reconstruct_rejects_before_projection_when_depth_exceeds_point_cap(
    tmp_path, monkeypatch
):
    depth = tmp_path / "too_many.npy"
    np.save(depth, np.ones((3, 3), dtype=np.uint16))
    monkeypatch.setattr(ferret_mcp, "MAX_DEPTH_ELEMENTS", 4)
    called = False

    from ferret import reconstruct

    def projection_must_not_run(_depth):
        nonlocal called
        called = True
        raise AssertionError("projection allocated before the depth cap")

    monkeypatch.setattr(reconstruct, "depth_to_pointcloud", projection_must_not_run)
    with pytest.raises(ValueError, match="element MCP limit"):
        ferret_mcp.reconstruct_pointcloud(str(depth), "bounded.ply")
    assert called is False
    assert ferret_mcp.MAX_DEPTH_ELEMENTS <= ferret_mcp.MAX_POINTCLOUD_POINTS


@pytest.mark.parametrize("out_name", ["../escape.ply", "/tmp/escape.ply", "dir\\escape.ply"])
def test_reconstruct_rejects_non_basename_output(out_name):
    # Name validation happens before the selected input can trigger projection.
    with pytest.raises(ValueError, match="simple .ply filename"):
        ferret_mcp.reconstruct_pointcloud("not-read.npy", out_name=out_name)


def test_reconstruct_refuses_existing_artifact_or_symlink(tmp_path, monkeypatch):
    depth = tmp_path / "depth.npy"
    np.save(depth, np.array([[1000]], dtype=np.uint16))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(ferret_mcp, "ARTIFACTS_DIR", artifacts)

    first = ferret_mcp.reconstruct_pointcloud(str(depth), out_name="scan.ply")
    assert Path(first["ply_path"]).is_file()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        ferret_mcp.reconstruct_pointcloud(str(depth), out_name="scan.ply")

    target = tmp_path / "outside.ply"
    target.write_bytes(b"keep")
    (artifacts / "link.ply").symlink_to(target)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        ferret_mcp.reconstruct_pointcloud(str(depth), out_name="link.ply")
    assert target.read_bytes() == b"keep"


def test_reconstruct_streams_ply_without_trimesh_payload(tmp_path, monkeypatch):
    depth = tmp_path / "depth.npy"
    np.save(depth, np.full((8, 8), 1000, dtype=np.uint16))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(ferret_mcp, "ARTIFACTS_DIR", artifacts)
    monkeypatch.setattr(ferret_mcp, "PLY_WRITE_CHUNK_BYTES", 7)
    original_write = os.write
    write_sizes: list[int] = []

    def traced_write(file_fd, data):
        write_sizes.append(len(data))
        return original_write(file_fd, data)

    def payload_must_not_materialize(*_args, **_kwargs):
        raise AssertionError("trimesh payload materialization must not be used")

    monkeypatch.setattr(os, "write", traced_write)
    monkeypatch.setattr(trimesh.PointCloud, "export", payload_must_not_materialize)
    result = ferret_mcp.reconstruct_pointcloud(str(depth), "streamed.ply")

    output = Path(result["ply_path"])
    assert output.read_bytes().startswith(b"ply\nformat binary_little_endian")
    assert len(write_sizes) > 2
    assert max(write_sizes) <= ferret_mcp.PLY_WRITE_CHUNK_BYTES


def test_reconstruct_removes_owned_partial_file_after_write_failure(tmp_path, monkeypatch):
    depth = tmp_path / "depth.npy"
    np.save(depth, np.array([[1000]], dtype=np.uint16))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(ferret_mcp, "ARTIFACTS_DIR", artifacts)

    def failed_write(_file_fd, _data):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "write", failed_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        ferret_mcp.reconstruct_pointcloud(str(depth), "partial.ply")
    assert not (artifacts / "partial.ply").exists()


def test_ply_fstat_failure_closes_fd_and_preserves_swapped_replacement(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    unrelated = artifacts / "unrelated.ply"
    unrelated.write_bytes(b"keep")
    monkeypatch.setattr(ferret_mcp, "ARTIFACTS_DIR", artifacts)
    original_open = os.open
    original_close = os.close
    created_fds: list[int] = []
    closed_fds: list[int] = []

    def tracked_open(*args, **kwargs):
        file_fd = original_open(*args, **kwargs)
        if args[0] == "fstat-fail.ply":
            created_fds.append(file_fd)
        return file_fd

    def tracked_close(file_fd):
        closed_fds.append(file_fd)
        return original_close(file_fd)

    created_path = artifacts / "fstat-fail.ply"

    def failed_fstat(_file_fd):
        created_path.unlink()
        created_path.write_bytes(b"replacement")
        raise OSError("simulated fstat failure")

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    monkeypatch.setattr(os, "fstat", failed_fstat)
    with pytest.raises(OSError, match="simulated fstat failure"):
        ferret_mcp._write_ply_exclusive(
            "fstat-fail.ply", np.zeros((1, 3), dtype=np.float32)
        )
    assert created_fds and created_fds[0] in closed_fds
    assert created_path.read_bytes() == b"replacement"
    assert unrelated.read_bytes() == b"keep"


def test_ply_fstat_failure_retains_unknown_partial_and_closes_fd(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(ferret_mcp, "ARTIFACTS_DIR", artifacts)
    original_close = os.close
    closed_fds: list[int] = []

    def tracked_close(file_fd):
        closed_fds.append(file_fd)
        return original_close(file_fd)

    monkeypatch.setattr(os, "close", tracked_close)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _file_fd: (_ for _ in ()).throw(OSError("simulated fstat failure")),
    )
    with pytest.raises(OSError, match="simulated fstat failure"):
        ferret_mcp._write_ply_exclusive(
            "unknown-partial.ply", np.zeros((1, 3), dtype=np.float32)
        )
    partial = artifacts / "unknown-partial.ply"
    assert partial.is_file() and partial.stat().st_size == 0
    assert closed_fds


def test_reconstruct_oversize_export_leaves_no_partial_file(tmp_path, monkeypatch):
    depth = tmp_path / "depth.npy"
    np.save(depth, np.array([[1000]], dtype=np.uint16))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(ferret_mcp, "ARTIFACTS_DIR", artifacts)
    monkeypatch.setattr(ferret_mcp, "MAX_PLY_BYTES", 1)

    with pytest.raises(ValueError, match="point-cloud export exceeds"):
        ferret_mcp.reconstruct_pointcloud(str(depth), "too-large.ply")
    assert not (artifacts / "too-large.ply").exists()


def test_clean_mesh_runs_in_bounded_worker(tmp_path):
    source = tmp_path / "source.obj"
    output = tmp_path / "clean.obj"
    trimesh.creation.icosphere(subdivisions=1).export(source)

    try:
        result = ferret_mcp.clean_mesh(str(source), str(output))
    except RuntimeError as exc:
        # This macOS interpreter cannot apply RLIMIT_AS.  The MCP endpoint must
        # reject rather than silently run native mesh processing without limits.
        assert "required resource limits failed" in str(exc)
        assert not output.exists()
    else:
        assert result["written"] is True
        assert output.is_file()


def test_mesh_worker_refuses_when_resource_limits_cannot_be_applied(monkeypatch):
    results: list[tuple[str, str]] = []

    class Queue:
        def put(self, item):
            results.append(item)

    def limits_unavailable():
        raise RuntimeError("resource limits unavailable")

    monkeypatch.setattr(ferret_mcp, "_apply_mesh_resource_limits", limits_unavailable)
    ferret_mcp._mesh_worker("unused", "unused", Queue())

    assert results[0][0] == "error"
    assert "resource limits unavailable" in results[0][1]


def test_stop_worker_escalates_and_rejects_unkillable_worker():
    class UnkillableWorker:
        def __init__(self):
            self.calls: list[str] = []

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

        def join(self, _timeout):
            self.calls.append("join")

        def is_alive(self):
            return True

    worker = UnkillableWorker()
    with pytest.raises(RuntimeError, match="could not be terminated safely"):
        ferret_mcp._stop_worker(worker)
    assert worker.calls == ["terminate", "join", "kill", "join"]


def test_bounded_mesh_failure_removes_owned_partial_tempfile(tmp_path, monkeypatch):
    source = tmp_path / "source.obj"
    source.write_text("v 0 0 0\n")
    output = tmp_path / "clean.obj"

    class Queue:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

        def get(self, timeout):
            assert timeout == 1
            return self.items.pop(0)

        def close(self):
            pass

        def join_thread(self):
            pass

    class Worker:
        exitcode = 0

        def __init__(self, args):
            self.args = args

        def start(self):
            Path(self.args[1]).write_bytes(b"partial")
            self.args[2].put(("error", "simulated worker failure"))

        def join(self, _timeout):
            pass

        def is_alive(self):
            return False

    class Context:
        def Queue(self, maxsize):
            assert maxsize == 1
            return Queue()

        def Process(self, target, args):
            assert target is ferret_mcp._mesh_worker
            return Worker(args)

    monkeypatch.setattr(ferret_mcp, "get_context", lambda _method: Context())
    with pytest.raises(RuntimeError, match="simulated worker failure"):
        ferret_mcp._bounded_clean_mesh(str(source), str(output))
    assert not output.exists()
    assert not list(tmp_path.glob(".clean.*.obj"))


def test_timed_out_mesh_removes_owned_partial_tempfile(tmp_path, monkeypatch):
    source = tmp_path / "source.obj"
    source.write_text("v 0 0 0\n")
    output = tmp_path / "clean.obj"

    class Queue:
        def close(self):
            pass

        def join_thread(self):
            pass

    class Worker:
        exitcode = None

        def __init__(self):
            self.alive = True
            self.terminated = False

        def start(self):
            pass

        def join(self, _timeout):
            pass

        def terminate(self):
            self.terminated = True
            self.alive = False

        def kill(self):
            raise AssertionError("terminate should have stopped this worker")

        def is_alive(self):
            return self.alive

    class Context:
        def __init__(self):
            self.worker = Worker()

        def Queue(self, maxsize):
            assert maxsize == 1
            return Queue()

        def Process(self, target, args):
            assert target is ferret_mcp._mesh_worker
            Path(args[1]).write_bytes(b"partial")
            return self.worker

    context = Context()
    monkeypatch.setattr(ferret_mcp, "get_context", lambda _method: context)
    with pytest.raises(TimeoutError, match="exceeded MCP time limit"):
        ferret_mcp._bounded_clean_mesh(str(source), str(output))
    assert context.worker.terminated is True
    assert not output.exists()
    assert not list(tmp_path.glob(".clean.*.obj"))


def test_oversize_mesh_output_removes_owned_partial_tempfile(tmp_path, monkeypatch):
    source = tmp_path / "source.obj"
    source.write_text("v 0 0 0\n")
    output = tmp_path / "clean.obj"
    monkeypatch.setattr(ferret_mcp, "MESH_OUTPUT_BYTES", 1)

    class Queue:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

        def get(self, timeout):
            assert timeout == 1
            return self.items.pop(0)

        def close(self):
            pass

        def join_thread(self):
            pass

    class Worker:
        exitcode = 0

        def __init__(self, args):
            self.args = args

        def start(self):
            Path(self.args[1]).write_bytes(b"too large")
            self.args[2].put(("ok", {}))

        def join(self, _timeout):
            pass

        def is_alive(self):
            return False

    class Context:
        def Queue(self, maxsize):
            assert maxsize == 1
            return Queue()

        def Process(self, target, args):
            assert target is ferret_mcp._mesh_worker
            return Worker(args)

    monkeypatch.setattr(ferret_mcp, "get_context", lambda _method: Context())
    with pytest.raises(RuntimeError, match="bounded owned output"):
        ferret_mcp._bounded_clean_mesh(str(source), str(output))
    assert not output.exists()
    assert not list(tmp_path.glob(".clean.*.obj"))


@pytest.mark.parametrize("failure", ["queue", "process"])
def test_mesh_setup_failure_removes_owned_tempfile(tmp_path, monkeypatch, failure):
    source = tmp_path / "source.obj"
    source.write_text("v 0 0 0\n")
    output = tmp_path / "clean.obj"

    class Queue:
        def close(self):
            pass

        def join_thread(self):
            pass

    class Context:
        def Queue(self, maxsize):
            assert maxsize == 1
            if failure == "queue":
                raise RuntimeError("simulated queue construction failure")
            return Queue()

        def Process(self, target, args):
            assert target is ferret_mcp._mesh_worker
            raise RuntimeError("simulated process construction failure")

    monkeypatch.setattr(ferret_mcp, "get_context", lambda _method: Context())
    with pytest.raises(RuntimeError, match=f"simulated {failure} construction failure"):
        ferret_mcp._bounded_clean_mesh(str(source), str(output))
    assert not output.exists()
    assert not list(tmp_path.glob(".clean.*.obj"))


def test_mesh_tempfile_keeps_destination_suffix_and_exports(tmp_path):
    destination = tmp_path / "clean.obj"
    directory_fd = ferret_mcp._open_directory(tmp_path)
    try:
        temporary_name, identity = ferret_mcp._new_mesh_tempfile(directory_fd, destination)
        temporary_path = tmp_path / temporary_name
        assert temporary_path.suffix == ".obj"
        trimesh.creation.box().export(temporary_path)
        assert temporary_path.is_file() and temporary_path.stat().st_size > 0
    finally:
        ferret_mcp._remove_owned_file(directory_fd, temporary_name, identity)
        os.close(directory_fd)


def test_mesh_temp_fstat_failure_closes_fd_and_preserves_swapped_replacement(
    tmp_path, monkeypatch
):
    unrelated = tmp_path / "unrelated.obj"
    unrelated.write_bytes(b"keep")
    original_open = os.open
    original_close = os.close
    created_fds: list[int] = []
    closed_fds: list[int] = []

    def tracked_open(*args, **kwargs):
        file_fd = original_open(*args, **kwargs)
        if isinstance(args[0], str) and args[0].startswith(".clean."):
            created_fds.append(file_fd)
        return file_fd

    def tracked_close(file_fd):
        closed_fds.append(file_fd)
        return original_close(file_fd)

    def failed_fstat(_file_fd):
        created = tmp_path / ".clean.replacement.obj"
        for original in tmp_path.glob(".clean.*.obj"):
            original.unlink()
        created.write_bytes(b"replacement")
        raise OSError("simulated fstat failure")

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    monkeypatch.setattr(os, "fstat", failed_fstat)
    directory_fd = ferret_mcp._open_directory(tmp_path)
    try:
        with pytest.raises(OSError, match="simulated fstat failure"):
            ferret_mcp._new_mesh_tempfile(directory_fd, tmp_path / "clean.obj")
    finally:
        os.close(directory_fd)
    assert created_fds and created_fds[0] in closed_fds
    assert (tmp_path / ".clean.replacement.obj").read_bytes() == b"replacement"
    assert unrelated.read_bytes() == b"keep"


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
def test_reconstruct_pointcloud_writes_ply(tmp_path, monkeypatch):
    # Hermetic: point ARTIFACTS_DIR at tmp_path so the run never collides with
    # the committed artifacts/test_mcp_scan.ply (_write_ply_exclusive refuses
    # to overwrite via O_EXCL).
    monkeypatch.setattr(ferret_mcp, "ARTIFACTS_DIR", tmp_path)
    result = ferret_mcp.reconstruct_pointcloud(str(REAL_DEPTH), out_name="test_mcp_scan.ply")

    ply_path = Path(result["ply_path"])
    assert ply_path.is_file(), "reconstruct_pointcloud did not produce a PLY"
    assert ply_path.stat().st_size > 0
    assert result["num_points"] > 0
