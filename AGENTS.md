# ferret-sdk — Claude Code project memory

## What this is
Tapping a **Creality CR-Scan Ferret** 3D scanner from code (and ultimately an LLM)

## How to verify a change
- Install: Python 3.12, `python -m pip install -r requirements-lock.txt` (FER-01/13).
- Tests: `pytest -q`
- Lint:  `(none detected)`
- MCP mesh acceptance must produce a mesh; macOS uses parent RSS monitoring
  (50 ms sampling, 1 GiB termination threshold), CPU/file limits and a timeout (FER-04).
- Capture tests use fake RPC only. Depth defaults to explicit Y14; select Y16
  only for a known computed-depth daemon mode. IR switches to IR_RIGHT/Y8 (FER-02).
- The depth probe accepts an explicit Y14/Y16 output mode and saves float32 mm;
  its fake-RPC regression tests must not connect to the daemon (FER-03).
- Turntable step_sequence returns an iterator: each next() moves once, then
  yields for capture. Tests use fake serial transports only (FER-05).
- Mesh recovery may close only holes introduced by decimating a previously
  watertight mesh; original openings obey max_hole_edges, including zero (FER-06).
- Fresh-clone E2E uses examples/synthetic_depth.py when private captures are
  absent; outputs use ignored artifacts/offline-demo/ or --output-dir (FER-07).
- watch_folder waits for two unchanged size/mtime polls and retries failures;
  only successful cleanup marks a file processed (FER-09).
- MCP clean_mesh publishes exclusively into ARTIFACTS_DIR by default. Explicit
  overwrite=True permits regular-file replacement; all ancestors reject symlinks (FER-08).

## Self-improving loop (close the loop)
Run an agent until it verifies its own output, then commit:
```
python3 ~/.agent-kit/verify_loop.py --task "<what to do>" --verify "pytest -q" --cwd . --max-iters 5 --commit
```

## Layout
`artifacts/`, `examples/`, `ferret/`, `hardware/`, `logs/`, `probe/`, `tests/`

## Loops
- **`eval-gate` (EVENT, on push)** — golden-baseline reconstruction eval as a push gate.
  - Runner: `tests/eval_gate.py` (emits `loop-result/v1` JSON to stdout; exit code = gate).
  - Workflow: `.github/workflows/eval-gate.yml` runs it on push (paths-filtered) via `--ci`.
  - Why it exists: `tests/eval_reconstruct.py` is outside pytest's default `test_*.py`
    glob, so `pytest -q` (the PR CI) never runs the golden baseline. This gate targets it.
  - Baseline: `tests/eval_reconstruct_baseline.json` — regenerate only on an INTENTIONAL
    geometry change with `python tests/eval_reconstruct.py --update-baseline`.
  - Flywheel: local runs append one line to `.claude/LEARNINGS.md`; `--ci`/`--dry-run` skip it.

## Conventions
- Follow CONVENTIONS.md (if present) and the global ~/.claude/CLAUDE.md.
- Make surgical, single-purpose changes; match existing style.

## Lessons
Corrections, gotchas, and "don't do X again" notes accumulate in `.claude/lessons.md`
and are consolidated by the `dream` skill. Read them before non-trivial work.

## Agent rules (all harnesses)
- Determinism boundary: anything that must be EXACT (money, geometry, safety limits, scoring, pricing) lives in code; the model only orchestrates and judges.
- Keep this file current: when you change how this repo is run, tested, or structured, update AGENTS.md in the same commit. Keep it under ~150 lines.
