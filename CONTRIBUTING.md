# Contributing

Thanks for considering a contribution.

## Getting set up

```bash
# Backend — CI runs Python 3.12 and 3.13; 3.12 matches the Docker image
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

`kubectl` must be on PATH. A model key is optional; without one the
deterministic fallback runs and everything still works. The test suite ignores
any key in `backend/.env`, so it never calls a model.

## Before opening a pull request

```bash
cd backend  && ruff check . && ruff format --check . && python -m pytest -q && python -m evals
cd frontend && npm test && npm run build
python scripts/mutation_check.py --suite backend    # if you touched an invariant
```

CI runs these and more: the mutation suites for the backend, console and
Terraform, an integration job that stands the chart up on kind, a Terraform
apply to kind, a dependency audit and a secret scan. Please do not weaken a test
to make it pass — if a test is wrong, say so in the PR and fix the assertion
deliberately. A fix to an invariant should come with a mutation pair in
`scripts/mutation_check.py`: the defect it prevents, and the test that catches it.

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

See [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) and the
*Known gaps* in [CHANGELOG.md](CHANGELOG.md). The highest value items right now:

- **Running it for real** — a pilot deployment against a real cluster and real
  incidents is the gap no amount of code closes
- **Applying the AWS Terraform** (`deploy/terraform/aws`) and reporting what
  broke
- **Scale-out across hosts** — throughput is measured on one machine only
- **Streaming decode on the agent path**, which still builds a whole list in
  memory before capping it
- **Model evaluation beyond OpenAI** — a full `evals.live` run against Claude
  or a self-hosted model on the current grounding check

## Commit and PR style

Explain *why*, not just *what*. If you found a bug, add the regression test in
the same PR and reference it in the description.
