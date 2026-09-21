<!--
CONTRIBUTING.md has the full bar. The short version is that a change is held to
a check that could fail without it: a passing suite is not evidence on its own,
which is why scripts/mutation_check.py exists.
-->

## What this changes, and why

## How it was verified

<!-- The command you ran and what it printed. "Tests pass" is not a measurement. -->

- [ ] `ruff check . && ruff format --check .` and `python -m pytest` (from `backend/`)
- [ ] `npm test && npm run build` (from `frontend/`), if the console changed
- [ ] `python scripts/mutation_check.py` — and a new invariant carries a **new pair**:
      the defect put back, the test watched to fail, the fix restored
- [ ] Docs that make a claim about this behaviour are updated, including
      `README.md`'s *Known limitations* if a limitation changed

## What it does not cover

<!-- The honest half: what is still unmeasured, unverified, or left for later. -->
