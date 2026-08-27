#!/usr/bin/env python3
"""Re-run this repository's mutation tests, so they survive inattention.

Every load-bearing invariant here was mutation-tested by hand: revert the real
defect, watch the check go red, restore. That discipline found seven defects in
one session — including three checks that had *just been written*, looked
correct, and guarded nothing. It is also the discipline that decays first,
because a passing suite feels like evidence and a mutation not run leaves no
trace.

`docs/PRODUCTION_READINESS.md` has listed **automated mutation testing** as a
gap since the audit. This is the narrow, honest version of closing it.

**Not a general mutation fuzzer, deliberately.** `mutmut` and `cosmic-ray`
mutate everything and grade a whole suite, which on this codebase would spend
minutes rediscovering that most lines are covered and produce a score nobody
acts on. What is worth keeping is the specific pairing of *a defect that
actually shipped* with *the test written to catch it* — a regression suite for
the tests themselves. Each entry below is a real bug this project had.

**A mutation that fails to apply reports "survived" identically to a missing
test**, which is why every entry is anchored on an exact string that must be
present exactly once, and why a failed application is an error rather than a
skip. That is not hypothetical: it is the trap this script exists to keep
someone from walking into at 2am.

    python scripts/mutation_check.py            # all of them
    python scripts/mutation_check.py --list     # what is covered
    python scripts/mutation_check.py -k revoked # one, by name

Restores every file on the way out, including on Ctrl-C.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"


@dataclass(frozen=True)
class Mutation:
    """A defect that shipped, and the test that must object to it."""

    name: str
    why: str
    path: str
    old: str
    new: str
    tests: str

    @property
    def file(self) -> Path:
        return BACKEND / self.path


MUTATIONS = [
    Mutation(
        name="metrics-content-type",
        why=(
            "2f60f76: the generator and the content type came from different "
            "modules, so every response advertised OpenMetrics and carried a "
            "body that is not one. A real Prometheus rejected every scrape; "
            "curl saw 200 and 16 KB of correct exposition."
        ),
        path="app/observability/metrics.py",
        old=(
            "from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram\n"
            "from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST, "
            "generate_latest"
        ),
        new=(
            "from prometheus_client import (\n"
            "    CollectorRegistry,\n"
            "    Counter,\n"
            "    Gauge,\n"
            "    Histogram,\n"
            "    generate_latest,\n"
            ")\n"
            "from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST"
        ),
        tests="tests/test_metrics.py",
    ),
    Mutation(
        name="loki-tenant-header",
        why=(
            "The X-Scope-OrgID header was built correctly and never passed to "
            "the client. A test inspecting the object passes; only one "
            "asserting on what reached the wire fails."
        ),
        path="app/integrations/loki.py",
        old="async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:",
        new="async with httpx.AsyncClient(timeout=self.timeout) as client:",
        tests="tests/test_observability.py",
    ),
    Mutation(
        name="agent-affinity-local-registry",
        why=(
            "§21 defect 6: `agent_affinity` asked the presence index without "
            "asking the local registry first, so a submission landing on the "
            "worker holding the stream went to the shared queue — the one case "
            "that must not be un-pinned."
        ),
        path="app/jobs/runner.py",
        old="    if get_agent_registry().get(context) is not None:\n        return presence.worker_id\n\n",
        new="",
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="revoked-agent-refusal",
        why=(
            "A revoked agent leaves no presence record, so the fallback read a "
            "local context that merely shared the cluster's name — the opposite "
            "of what revoking asked for."
        ),
        path="app/services/investigation_service.py",
        old="        if _agent_was_revoked(context):",
        new="        if False and _agent_was_revoked(context):",
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="revoked-vs-disconnected",
        why=(
            "The second half of the same fix, and the one that survived its "
            "first mutation. Refusing for *disconnected* agents too would turn "
            "every flap into an outage; the two versions diverge only for a "
            "certificate that expired without ever being revoked."
        ),
        path="app/services/investigation_service.py",
        old="    if not any(record.revoked for record in records):\n        return False",
        new="    if not records:\n        return False",
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="sse-heartbeat-ownership",
        why=(
            "The SSE stream had no ownership check: any authenticated caller "
            "who guessed an id received another user's live progress. "
            "Authentication was applied at the router and authorisation simply "
            "was not."
        ),
        path="app/api/investigate.py",
        # Anchored on the preceding `get_summary` line, because the same
        # ownership check appears twice in this file and the script refuses an
        # ambiguous anchor rather than guessing which one it meant.
        old=(
            "    job = store.get_summary(investigation_id)\n"
            "    if job is None or not _may_read_job(job, _visible_owner(principal)):\n"
            '        raise HTTPException(status_code=404, detail="Investigation job not found")\n'
            "\n"
            "    after_seq = _resume_position(request)"
        ),
        new=(
            "    job = store.get_summary(investigation_id)\n"
            "    if job is None:\n"
            '        raise HTTPException(status_code=404, detail="Investigation job not found")\n'
            "\n"
            "    after_seq = _resume_position(request)"
        ),
        tests="tests/test_authz.py tests/test_auth.py",
    ),
]


def apply(mutation: Mutation) -> str:
    """Write the mutation, returning the original text. Refuses to guess."""
    original = mutation.file.read_text()
    occurrences = original.count(mutation.old)
    if occurrences != 1:
        raise SystemExit(
            f"\n{mutation.name}: its anchor appears {occurrences} times in "
            f"{mutation.path}, expected exactly 1.\n"
            f"The code moved under this mutation. Re-anchor it — do NOT delete "
            f"it, because a mutation that cannot be applied is indistinguishable "
            f"from one nothing catches, and that is the whole failure this "
            f"script guards against.\n"
        )
    mutation.file.write_text(original.replace(mutation.old, mutation.new, 1))
    return original


def run_tests(selector: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-x",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *selector.split(),
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        # A non-zero exit is the *expected* outcome here — it means the test
        # objected to the defect — so this must never raise.
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", default="", help="only mutations whose name contains this")
    parser.add_argument("--list", action="store_true", help="show what is covered and exit")
    args = parser.parse_args()

    selected = [m for m in MUTATIONS if args.k in m.name]
    if args.list:
        for mutation in selected:
            print(f"{mutation.name:32} {mutation.path}")
        return 0
    if not selected:
        print(f"no mutation matches {args.k!r}")
        return 2

    # A copy of every file involved, restored no matter how this exits. The
    # alternative — trusting the happy path to put things back — leaves a
    # mutated working tree behind on the first Ctrl-C.
    backup = Path(tempfile.mkdtemp(prefix="mutation-check-"))
    touched = {m.file for m in selected}
    for path in touched:
        shutil.copy2(path, backup / path.name)

    survived: list[Mutation] = []
    try:
        for mutation in selected:
            print(f"\n\033[1m{mutation.name}\033[0m  ({mutation.path})")
            apply(mutation)
            result = run_tests(mutation.tests)
            mutation.file.write_text((backup / mutation.file.name).read_text())

            if result.returncode != 0:
                summary = next(
                    (line for line in reversed(result.stdout.splitlines()) if "failed" in line),
                    "tests failed",
                )
                print(f"  \033[32mCAUGHT\033[0m  {summary.strip()}")
            else:
                survived.append(mutation)
                print(f"  \033[31mSURVIVED\033[0m  {mutation.tests} passed with the defect present")
                print(f"           {mutation.why}")
    finally:
        for path in touched:
            shutil.copy2(backup / path.name, path)
        shutil.rmtree(backup, ignore_errors=True)

    print("\n" + "=" * 72)
    print(f"{len(selected) - len(survived)} caught, {len(survived)} survived")
    for mutation in survived:
        print(f"\n\033[31mSURVIVED\033[0m {mutation.name}\n  {mutation.why}")
    print("=" * 72)
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
