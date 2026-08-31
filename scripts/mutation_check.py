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
    Mutation(
        name="cache-dates-evidence-now",
        why=(
            "F18. The collection cache's one load-bearing promise: a record "
            "built from a reused read carries the age of the *read*. Drop the "
            "backdating and every warm investigation dates forty-second-old "
            "facts `now` — a false citation on every conclusion, with a green "
            "suite and a faster benchmark. The first of the new invariants "
            "rather than a defect that shipped; the point of writing it down "
            "is that it is the one which would ship silently."
        ),
        path="app/collectors/scheduler.py",
        old=(
            "        if window is not None and window.oldest is not None "
            "and window.oldest < collected_at:\n"
            "            collected_at = window.oldest"
        ),
        new="        pass",
        tests="tests/test_collection_cache.py",
    ),
    Mutation(
        name="cache-key-drops-the-tenant",
        why=(
            "M6 keyed `AgentRegistry` on `(tenant, cluster)` because two "
            "customers may both call a cluster `prod`. A cache keyed on the "
            "name alone undoes that in one dictionary, and the symptom is one "
            "tenant's pod list cited in another's report."
        ),
        path="app/providers/cache.py",
        old=(
            "    return _SCOPE_SEP.join(\n"
            "        (current_tenant(), type(provider).__name__, provider.cluster_id, identity)\n"
            "    )"
        ),
        new=(
            "    return _SCOPE_SEP.join((type(provider).__name__, provider.cluster_id, identity))"
        ),
        tests="tests/test_collection_cache.py",
    ),
    Mutation(
        name="cache-stores-failures",
        why=(
            "A cached FORBIDDEN goes on refusing after the RBAC that caused it "
            "is fixed, and `app/kubernetes/access.py` reads exactly those "
            "statuses to tell a locked door from a broken cluster. Measured "
            "against a real cluster: every one of a warm run's 13 misses was a "
            "failure, so this is the normal path and not an edge case."
        ),
        path="app/providers/cache.py",
        old="if not self.enabled or not result.success:",
        new="if not self.enabled:",
        tests="tests/test_collection_cache.py",
    ),
    Mutation(
        name="cache-hides-the-transport",
        why=(
            "`cluster_access` asks what the provider *is*, and a wrapper is "
            "neither an agent nor a kubeconfig. Without `underlying()` an agent "
            "fleet reports every investigation as `kubeconfig` — the exact M8a "
            "regression `cluster_access_total` was added to make visible, "
            "reintroduced by the thing meant to make it faster."
        ),
        path="app/providers/cache.py",
        old="    return provider.inner if isinstance(provider, CachingProvider) else provider",
        new="    return provider",
        tests="tests/test_collection_cache.py",
    ),
    Mutation(
        name="cache-window-is-not-shared",
        why=(
            "`asyncio` copies the context when it creates a task, and "
            "`LocalKubectlProvider.fetch_many` gathers its reads into child "
            "tasks. A window the scheduler holds by value rather than by "
            "reference is never written to by the provider, so every record is "
            "dated `now` and every test that inspects the window object still "
            "passes. Same family as `require_principal` having to stay `async` "
            "and the correlation id needing a mutable holder."
        ),
        path="app/providers/cache.py",
        old="    window = FreshnessWindow()\n    token = _window.set(window)",
        new="    token = _window.set(FreshnessWindow())\n    window = FreshnessWindow()",
        tests="tests/test_collection_cache.py",
    ),
    Mutation(
        name="agent-cannot-serve-a-read",
        why=(
            "F7. Eight deep-investigation reads named a resource the agent had "
            "no kind for — EndpointSlice, Ingress, `configmap` singular against "
            "a plural key, and five more. Each degraded silently: the collector "
            "records a non-usable record and the investigation succeeds, so an "
            "agent cluster was quietly shallower than the same cluster read "
            "locally. This removes one key back."
        ),
        path="app/providers/remote_agent.py",
        old='    (ReadVerb.GET, "endpointslices"): "k8s.endpointslices",\n',
        new="",
        tests="tests/test_provider_parity.py",
    ),
    Mutation(
        name="parity-check-sees-nothing",
        why=(
            "The vacuity guard on the parity check itself. A recorder that "
            "captured no reads satisfies every assertion in that file — the "
            "parametrised test collapses to zero cases and reports green. Same "
            "failure as `fleet_bench.py` printing a platform result from an "
            "AttributeError, and as a scrape check with zero targets."
        ),
        path="tests/test_provider_parity.py",
        old="        self.requests.extend(requests)",
        new="        pass",
        tests="tests/test_provider_parity.py",
    ),
    Mutation(
        name="agent-404-on-a-list-is-not-empty",
        why=(
            "The agent mapped every 404 to EMPTY, which the platform counts as "
            "*usable*. metrics-server is absent from most clusters, so an agent "
            "reported 'we looked and there is no usage' where kubectl reported "
            "'we could not look' — inflating evidence completeness, and with it "
            "the confidence of a diagnosis that had seen less. Found by running "
            "tests/test_agent_transport.py against a real cluster, which "
            "nothing in CI does."
        ),
        # The mapping lives in Go; `test_metrics_parity.py` carries a tripwire
        # reading that source, so this mutation is observable from pytest.
        # `agent/internal/collectors/status_test.go` is the primary check.
        path="../agent/internal/collectors/collector.go",
        old=(
            "\tcase apierrors.IsNotFound(err) && named:\n"
            "\t\treturn agentv1.EvidenceStatus_EVIDENCE_STATUS_EMPTY\n"
            "\tcase apierrors.IsNotFound(err):\n"
            "\t\treturn agentv1.EvidenceStatus_EVIDENCE_STATUS_UNAVAILABLE"
        ),
        new=(
            "\tcase apierrors.IsNotFound(err):\n"
            "\t\treturn agentv1.EvidenceStatus_EVIDENCE_STATUS_EMPTY"
        ),
        tests="tests/test_metrics_parity.py",
    ),
    Mutation(
        name="agent-impersonation-drift",
        why=(
            "Impersonation is what makes 'the platform cannot see more than you "
            "can' true, and it was decided twice. The local path declined for an "
            "anonymous caller; the agent path sent principal.subject regardless, "
            "so an unauthenticated deployment asked the cluster to read as a user "
            "named 'anonymous'. Inert only while the agent discarded the field — "
            "and every read would have been refused the moment it stopped."
        ),
        path="app/providers/remote_agent.py",
        old=(
            "    identity = identity_for(principal)\n"
            '    subject, groups = identity if identity else ("", ())'
        ),
        new=(
            '    subject = principal.subject if principal else ""\n'
            "    groups = principal.groups if principal else ()"
        ),
        tests="tests/test_auth.py",
    ),
    Mutation(
        name="agent-refusal-says-nothing",
        why=(
            "client-go reports 'unknown' for every error on a raw request, and "
            "the agent reads raw on purpose. Losing the API server's own sentence "
            "makes an investigation degraded by one caller's narrow RBAC "
            "indistinguishable from one degraded by a broken cluster — the single "
            "distinction app/kubernetes/access.py exists to draw. Caught here by "
            "a source tripwire; the behaviour itself is pinned in Go."
        ),
        path="../agent/internal/collectors/collector.go",
        old='\tif message := statusMessage(body); message != "" {\n\t\treturn message\n\t}\n',
        new="",
        tests="tests/test_provider_parity.py",
    ),
    Mutation(
        name="anthropic-system-prompt-inline",
        why=(
            "F11's provider abstraction. PromptBuilder emits OpenAI's shape, "
            "where the system prompt is a message; Anthropic takes it as a "
            "top-level parameter and rejects that role in messages. Copying the "
            "OpenAI body across is the obvious mistake and fails every request."
        ),
        path="app/ai/providers/anthropic.py",
        old="        system, conversation = split_system(messages)",
        new='        system, conversation = "", [dict(m) for m in messages]',
        tests="tests/test_llm_providers.py",
    ),
    Mutation(
        name="llm-provider-inference-order",
        why=(
            "An unset LLM_PROVIDER infers from whichever key is set, OpenAI "
            "first. That order is today's behaviour preserved, not a "
            "preference: a deployment migrating between providers has both keys "
            "set for a while, and must keep going to OpenAI until it says "
            "otherwise. Same discipline as RBAC_DEFAULT_ROLE=admin."
        ),
        path="app/ai/providers/factory.py",
        old=(
            '    if config.openai_api_key:\n        return "openai"\n'
            '    if config.anthropic_api_key:\n        return "anthropic"'
        ),
        new=(
            '    if config.anthropic_api_key:\n        return "anthropic"\n'
            '    if config.openai_api_key:\n        return "openai"'
        ),
        tests="tests/test_llm_providers.py",
    ),
    Mutation(
        name="agent-record-pairing",
        why=(
            "fetch_many matched records to requests by kind alone and took "
            "them in arrival order, so a wave of pod-log reads could file one "
            "pod's logs under another pod's name. Measured over an hour "
            "against a real agent: 5.5% of pod-log entries carried another "
            "pod's result, counting only the ones detectable because the "
            "message named a different pod. The target was on the wire the "
            "whole time."
        ),
        path="app/providers/remote_agent.py",
        old="            by_slot.setdefault(_slot(record.kind, record.target), []).append(record)",
        new='            by_slot.setdefault((record.kind, "", None), []).append(record)',
        tests="tests/test_remote_agent_matching.py",
    ),
    Mutation(
        name="baseline-pod-logs-are-text",
        why=(
            "The baseline log read left OutputFormat at its JSON default, so "
            "the kubeconfig path ran json.loads over log text: the read failed "
            "for every pod that had output and succeeded for the silent ones, "
            "with an empty reason. Through an agent the same read worked, so "
            "the two providers disagreed about the most useful evidence a "
            "CrashLoopBackOff has."
        ),
        path="app/kubernetes/logs_collector.py",
        old="                output=OutputFormat.TEXT,\n",
        new="",
        tests="tests/test_remote_agent_matching.py",
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
