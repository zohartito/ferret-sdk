# ferret-sdk — Claude Code project memory

## What this is
Tapping a **Creality CR-Scan Ferret** 3D scanner from code (and ultimately an LLM)

## How to verify a change
- Tests: `pytest -q`
- Lint:  `(none detected)`

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
