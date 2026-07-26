# Contributing

Thanks for considering a contribution.

## Getting set up

```bash
# Backend — use Python 3.12; the pinned pydantic has no 3.14 wheel
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt ruff
python -m pytest

# Frontend
cd frontend
npm ci
npm test
npm run build     # tsc -b — the type gate
```

`kubectl` must be on PATH. `OPENAI_API_KEY` is optional; without it the
deterministic fallback runs and everything still works.

## Before opening a pull request

```bash
cd backend  && ruff check . && ruff format --check . && python -m pytest -q
cd frontend && npm test && npm run build
```

CI runs exactly this. Please do not weaken a test to make it pass — if a test is
wrong, say so in the PR and fix the assertion deliberately.

## Design rules this codebase holds to

These are load-bearing. A change that breaks one needs an explicit argument.

1. **The platform never mutates a cluster.** Every cluster call goes through the
   read-only allowlist. Remediation is generated as text for a human to review.
2. **Nothing is asserted without evidence.** Signals carry mandatory provenance;
   panels and reports render empty states rather than placeholder content.
   Inventing plausible-looking output is a correctness bug, not a cosmetic one.
3. **Degradation is data.** A backend being unavailable is recorded as evidence
   with a reason, never swallowed and never presented as a healthy result.
4. **The model selects and explains; it does not act.** Commands are
   deterministic. Model output is accepted only after citation validation.
5. **Redaction happens at the collection boundary**, so every consumer sees the
   same scrubbed payload.

## Adding things

- **A collector** — see [docs/EVIDENCE_ARCHITECTURE.md](docs/EVIDENCE_ARCHITECTURE.md)
- **A failure mode** (signal + hypothesis) — see [docs/REASONING_ARCHITECTURE.md](docs/REASONING_ARCHITECTURE.md)
- **A playbook** — see [docs/PLAYBOOKS.md](docs/PLAYBOOKS.md)
- **A remediation rule** — see [docs/REMEDIATION.md](docs/REMEDIATION.md). Safety
  tests are parameterised over every registered rule, so a new one is held to the
  same guarantees automatically.

## Where help is most wanted

See [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md). The highest
value items right now:

- **Authentication and Kubernetes impersonation** (F13) — the one remaining P0
- **Paginated cluster reads** (F5) — currently unbounded on large clusters
- **LLM evaluation harness** with golden investigations (F11)
- **Real cluster fixtures** (kind/envtest) — everything currently runs against a
  hand-built fake

## Commit and PR style

Explain *why*, not just *what*. If you found a bug, add the regression test in
the same PR and reference it in the description.
