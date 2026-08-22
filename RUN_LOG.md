# RUN_LOG — gauntlet/ox-alpha

Every command below was actually executed in this session; output is trimmed,
never fabricated.

## Session setup (2026-08-21)

```
$ git -C ~/projects/ferret-sdk status && git branch -a && git log --oneline -5
On branch main
Your branch is up to date with 'origin/main'.
...
4f04e08 chore(deps): update fast-simplification requirement (#31)

$ git checkout -b gauntlet/ox-alpha
Switched to a new branch 'gauntlet/ox-alpha'
```

## Baseline test run (before any change)

```
$ .venv/bin/pytest -q
ferret_mcp.py:175: FileExistsError
=========================== short test summary info ============================
FAILED tests/test_mcp.py::test_reconstruct_pointcloud_writes_ply - FileExists...
1 failed, 70 passed in 6.57s
```

Pre-existing red on `main`: `test_reconstruct_pointcloud_writes_ply` collides
with the COMMITTED `artifacts/test_mcp_scan.ply` (`_write_ply_exclusive` opens
with `O_EXCL` and refuses to overwrite). Investigated as Phase 2 defect D1.

## Ground-truth fixture facts (read-only checks)

```
$ .venv/bin/python -c "import numpy as np; a=np.load('probe/rpc_depth.npy'); ..."
dtype uint16 shape (800, 640) nbytes 1024000
bytes 1024000
sentinel counts: zeros 327874 ffff 0
valid raw min/max 1 35585
```

## Phase 2 — defect hunt

### D1: MCP PLY test collides with its own committed artifact
Root cause: `tests/test_mcp.py:589` writes `test_mcp_scan.ply` through
`ferret_mcp._write_ply_exclusive` (O_EXCL, refuses overwrite,
ferret_mcp.py:171-175); `artifacts/test_mcp_scan.ply` is tracked since 5d05f90
→ guaranteed red on any fresh checkout. Severity: high (CI gate always red).
Fix: monkeypatch `ferret_mcp.ARTIFACTS_DIR` to `tmp_path` in the test.

```
$ .venv/bin/pytest -q                      # after fix
71 passed in 3.70s                         # was: 1 failed, 70 passed
$ .venv/bin/pytest -q tests/test_mcp.py::test_reconstruct_pointcloud_writes_ply
1 passed in 2.68s

```
$ .venv/bin/pytest -q tests/test_defect_regressions.py   # repros BEFORE fixes
FAILED tests/test_defect_regressions.py::test_disparity_rejects_absurd_far_depth
FAILED tests/test_defect_regressions.py::test_decode_depth_rejects_truncated_y14
FAILED tests/test_defect_regressions.py::test_open_closes_socket_when_start_stream_fails
3 failed, 2 passed in 0.09s
```

### D2: disparity_to_depth_mm leaks absurd far points (numeric)
Root cause: ferret/capture.py:157 applied only the 50 mm floor; docstring
promised rejection of the full "plausible close-range band". raw=437 →
disp term 0.5 → depth ≈ 286,599 mm survives into reconstruction.
Severity: high (silent garbage geometry from real sensor dropouts).
Fix: MAX_VALID_DEPTH_MM = 8000.0 clamp, mirroring calib.MAX_VALID_DEPTH_M.

### D3: truncated Y14 frames silently decoded as garbage Y16 (edge case)
Root cause: `FerretCamera._decode_depth` (capture.py:288 old) sent ANY
even-sized blob to decode_y16_depth_mm; its 640-wide fallback reshape
succeeds for most sizes. A Y14 frame cut to a multiple of 1280 bytes
(mid-stream disconnect) decoded to a plausible-looking wrong mm array.
Severity: high (garbage geometry instead of a clear error).
Fix: dispatcher accepts only complete frames — exact 1,792,000-byte Y14 or
the probe-observed Y16 byte sizes {512000,960000,1024000,2048000}.

### D4: open() leaks the RPC socket when start_stream fails (resource)
Root cause: capture.py:239 old — suffix-missing path calls close(), but an
RpcError raised by _start_stream propagated with the socket still open.
Severity: medium (fd leak per failed open against a live daemon).
Fix: try/close()/raise around _start_stream.

### Suite after each fix (every commit green)

```
after D1: 71 passed   after D2: 73 passed
after D3: 75 passed   after D4: 76 passed
```
