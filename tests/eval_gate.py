#!/usr/bin/env python3
"""EVENT loop: golden-baseline eval gate for ferret-sdk (runs on push).

Wraps the existing reconstruction regression eval
(``tests/eval_reconstruct.py`` + ``tests/eval_reconstruct_baseline.json``) so a
push can BLOCK on a geometry regression. The eval file is NOT matched by the
default ``pytest`` collection glob (``test_*.py``), so the repo's ``pytest -q``
CI never actually runs it — this gate targets it explicitly.

What it does (deterministic; no LLM, no network):
1. Run ``python tests/eval_reconstruct.py`` to capture the *measured* mesh
   metrics for the current code (the geometry snapshot).
2. Run ``pytest tests/eval_reconstruct.py`` — the actual PASS/FAIL gate against
   the committed golden baseline.
3. Emit one ``loop-result/v1`` JSON object to stdout (machine-consumable).
4. Append one durable line to ``.claude/LEARNINGS.md`` (skipped under --ci /
   --dry-run so CI checkouts stay clean).

Exit code is the gate: 0 = pass, 1 = regression/failure. ``--dry-run`` always
exits 0 (it only proves the gate wiring works).

Usage:
    python tests/eval_gate.py            # local: run gate + append LEARNINGS
    python tests/eval_gate.py --ci       # CI: gate + JSON to stdout, no append
    python tests/eval_gate.py --dry-run  # cheap wiring check, never fails
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = REPO_ROOT / "tests" / "eval_reconstruct.py"
BASELINE_FILE = REPO_ROOT / "tests" / "eval_reconstruct_baseline.json"
LEARNINGS_FILE = REPO_ROOT / ".claude" / "LEARNINGS.md"

LOOP_ID = "eval-gate"
# Hard cap so a hung dependency can never wedge a push.
STEP_TIMEOUT_S = 600


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=STEP_TIMEOUT_S,
    )


def _capture_metrics() -> dict | None:
    """Run the eval as a script to get the measured metrics JSON, or None."""
    try:
        proc = _run([sys.executable, str(EVAL_FILE)])
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _run_pytest_gate() -> dict:
    """Run pytest against the eval file; return {passed, counts, tail}."""
    try:
        proc = _run(
            [sys.executable, "-m", "pytest", str(EVAL_FILE), "-q", "--tb=short"]
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "counts": {}, "tail": "pytest timed out"}

    out = proc.stdout + proc.stderr
    counts = {}
    for label in ("passed", "failed", "skipped", "error", "errors"):
        m = re.search(rf"(\d+)\s+{label}\b", out)
        if m:
            counts[label.rstrip("s")] = int(m.group(1))
    tail = "\n".join(out.strip().splitlines()[-8:])
    return {"passed": proc.returncode == 0, "counts": counts, "tail": tail}


def _append_learning(line: str) -> None:
    LEARNINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LEARNINGS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="ferret-sdk golden-baseline eval gate")
    parser.add_argument("--ci", action="store_true", help="CI mode: no LEARNINGS append")
    parser.add_argument(
        "--dry-run", action="store_true", help="prove wiring, never fail the gate"
    )
    args = parser.parse_args()

    ts = _utc_now()
    metrics = _capture_metrics()
    gate = _run_pytest_gate()

    passed = gate["passed"]
    skipped = gate["counts"].get("skipped", 0)
    if metrics is None and skipped:
        summary = "eval skipped (trimesh/reconstruct unavailable); baseline file valid"
        verdict = "SKIP"
    elif passed:
        summary = "reconstruction metrics within golden-baseline tolerance"
        verdict = "PASS"
    else:
        summary = "REGRESSION: reconstruction drifted outside golden-baseline tolerance"
        verdict = "FAIL"

    learning = (
        f"{ts} [{LOOP_ID}] {verdict} — {summary}"
        + (
            f" (max_dim_rel_error={metrics['raw_mesh']['max_dim_rel_error']})"
            if metrics
            else ""
        )
    )

    result = {
        "schema": "loop-result/v1",
        "repo": "ferret-sdk",
        "loop": LOOP_ID,
        "kind": "event-push",
        "timestamp": ts,
        "passed": bool(passed),
        "summary": summary,
        "metrics": {
            "baseline": str(BASELINE_FILE.relative_to(REPO_ROOT)),
            "pytest": gate["counts"],
            "measured": metrics,
        },
        "learning": learning,
    }

    print(json.dumps(result, indent=2))

    if not args.ci and not args.dry_run:
        _append_learning(learning)

    if args.dry_run:
        return 0
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
