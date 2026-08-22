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
