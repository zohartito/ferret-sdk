# LEARNINGS — ferret-sdk

Durable learnings appended by the `eval-gate` loop (see `tests/eval_gate.py`).
Distinct from `.claude/lessons.md` (human corrections consolidated by `dream`):
this file is the loop's own machine-appended run log.

Format, newest last, one line per run:
`<ISO-8601-UTC> [eval-gate] <PASS|FAIL|SKIP> — <note>`

## Seed notes
- The golden-baseline eval lives in `tests/eval_reconstruct.py`; its file name is
  NOT matched by pytest's default `test_*.py` glob, so `pytest -q` never runs it.
  The gate targets it explicitly — that is the whole reason this loop exists.
- Regenerate the baseline only after an INTENTIONAL geometry change:
  `python tests/eval_reconstruct.py --update-baseline`, then review the diff.

<!-- loop appends below this line -->
