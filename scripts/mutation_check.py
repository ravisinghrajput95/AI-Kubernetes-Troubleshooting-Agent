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
        name="presence-liveness-frozen-at-write-time",
        why=(
            "GET /agents on a multi-worker deployment returned the presence record "
            "as written at the last heartbeat, so online and seconds_since_seen were "
            "frozen: an agent frozen with SIGSTOP read 'online, seen 0s ago' for 43 "
            "seconds and the console's red 'Agent silent' state never appeared."
        ),
        path="app/gateway/presence.py",
        old="            records.append(_as_of(record, now, elapsed))\n",
        new="            records.append(record)  # mutation: liveness as written\n",
        tests="tests/test_agent_presence.py",
    ),
    Mutation(
        name="stale-threshold-equal-to-presence-ttl",
        why=(
            "AGENT_STALE_SECONDS and PRESENCE_TTL_SECONDS were both 45, so a record "
            "expired at the instant it would have read silent. Stale must sit strictly "
            "between the heartbeat and the TTL."
        ),
        path="app/gateway/timing.py",
        old="AGENT_STALE_SECONDS = 30.0\n",
        new="AGENT_STALE_SECONDS = 45.0  # mutation: as shipped\n",
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="lease-renewed-under-a-different-identity",
        why=(
            "The claim recorded worker_identity() (WORKER_ID, else hostname:pid) "
            "and the watchdog renewed with settings.worker_id, which nothing in "
            "the repository sets. The renewal matched no row, so every distributed "
            "investigation that outlived JOB_LEASE_SECONDS was reaped as a dead "
            "worker while still running — reproduced live against a frozen agent, "
            "reaped at 71s half a second before the run completed."
        ),
        path="app/jobs/runner.py",
        old="                        lease_worker,\n",
        new="                        settings.worker_id,  # mutation: as shipped\n",
        tests="tests/test_lease_renewal.py",
    ),
    Mutation(
        name="settling-retries-a-divergence-away",
        why=(
            "The integration job's differential refusal guard went red twice in "
            "three runs on churn alone, so a refused comparison is now collected "
            "again. That retry is only safe if it keys on the refusal: retrying "
            "on anything short of a clean comparison would re-collect a real "
            "divergence until the cluster happened not to show it — a directional, "
            "repeating defect retried out of existence by the fix for a flake."
        ),
        path="tests/differential.py",
        old="        if comparison.divergences or comparison.refusal() is None:",
        new="        if not comparison.divergences and comparison.refusal() is None:",
        tests="tests/test_differential_control.py",
    ),
    Mutation(
        name="settling-passes-a-cluster-that-never-settles",
        why=(
            "The other half of the retry. Returning a fresh, empty Comparison "
            "when every attempt was refused would turn 'the cluster never held "
            "still' into 'nothing differed' — the exclusion-with-no-floor that "
            "Comparison.refusal() exists to prevent, reintroduced one level up."
        ),
        path="tests/differential.py",
        old="    return comparison, attempts\n",
        new="    return Comparison(), attempts  # mutation: a refused run passes\n",
        tests="tests/test_differential_control.py",
    ),
    Mutation(
        name="progress-reporting-blocks-the-event-loop",
        why=(
            "F28: a progress event is a committed Postgres row plus a Redis "
            "publish, and every call site is a coroutine on the event loop, so "
            "reporting it inline stopped the worker advancing any task at all "
            "— HTTP, SSE and every attached agent's gRPC stream — around fifty "
            "times per investigation. Measured against real Postgres and Redis: "
            "2,592 of 2,688 store calls on the loop, blocked 97% of wall clock, "
            "and 48 investigations took 10.04s against 5.06s once dispatched. "
            "The whole test suite passed with the defect present, because no "
            "test had ever driven a ProgressReporter — every progress test "
            "called store.publish() directly, so the bridge was covered at "
            "neither end."
        ),
        path="app/jobs/runner.py",
        old="""    async def report(self, message: str, **data) -> None:
        await asyncio.to_thread(
            self._store.publish,
            self._job_id,
            JobEvent(JobEventType.PROGRESS, message, data=data),
        )""",
        new="""    async def report(self, message: str, **data) -> None:
        self._store.publish(  # mutation: back on the event loop
            self._job_id,
            JobEvent(JobEventType.PROGRESS, message, data=data),
        )""",
        tests="tests/test_progress_reporting.py",
    ),
    Mutation(
        name="churn-is-not-divergence",
        why=(
            "F26: the differential suite compared two live reads and called "
            "any difference a provider divergence, in the same words. It "
            "failed the required integration-verify job on da5de44 with "
            "`k8s.deployments.unhealthy_deployments differs` because the "
            "platform's own Deployment was replacing a pod between the two "
            "reads. The bracketing control is what tells the two apart, and "
            "it lives in a suite that skips unless a cluster is present — so "
            "only the hermetic tests can see it regress."
        ),
        path="tests/differential.py",
        old="    return {path for path in set(a) | set(b) if a.get(path, _MISSING) != b.get(path, _MISSING)}",
        new="    return set()  # mutation: nothing is churn, every cluster change is a defect",
        tests="tests/test_differential_control.py",
    ),
    Mutation(
        name="a-liveness-kill-is-not-an-oom",
        why=(
            "`exit_code == 137 or reason == 'OOMKilled'` — and 137 is "
            "128 + SIGKILL, which a failed liveness probe also produces. "
            "Against a live cluster the platform reported 'terminated for "
            "exceeding its memory limit' for a container whose spec carried "
            "`resources: {}`, with a probe-failure event in the same "
            "investigation. The existing test was named "
            "`test_exit_code_137_confirms_an_oom_kill` while its fixture "
            "always supplied the reason too, so it passed either way."
        ),
        path="app/analysis/deep_signal_rules.py",
        old="                if reason == OOM_TERMINATION_REASON:",
        new="                if exit_code == SIGKILL_EXIT_CODE or reason == OOM_TERMINATION_REASON:",
        tests="tests/test_deep_signals.py",
    ),
    Mutation(
        name="refutation-outranks-severity",
        why=(
            "A hypothesis takes the severity of its triggering signal, and "
            "severity was the primary sort key — so REFUTE_PENALTY moved "
            "`confidence` and nothing else whenever the refuted hypothesis had "
            "the more severe trigger, which is the normal case because a "
            "symptom is more alarming than the marker of its cause. "
            "Contradicting evidence could not change which hypothesis was "
            "reported as the root cause, only the number beside it."
        ),
        path="app/analysis/hypothesis_rules.py",
        old="                not item.refuting_signal_ids,\n                item.confidence,",
        new="                item.confidence,",
        tests="tests/test_analysis_engine.py",
    ),
    Mutation(
        name="a-fault-symptom-does-not-refute-its-own-cause",
        why=(
            "`storage.claim_blocking_pod` listed EVENT_SCHEDULING_FAILURE as "
            "refuting, but a pod mounting an unbound claim is unschedulable and "
            "the scheduler says exactly that — so the signal fired because the "
            "claim was blocking the pod and argued against the hypothesis "
            "saying so. Inert while severity outranked refutation; a live "
            "PVC-with-no-StorageClass fault then reported the symptom instead "
            "of the cause."
        ),
        path="app/analysis/hypothesis_rules.py",
        old="        refuting=frozenset(),",
        new="        refuting=frozenset({SignalType.EVENT_SCHEDULING_FAILURE}),",
        tests="tests/test_analysis_engine.py",
    ),
    Mutation(
        name="cache-size-recording-cannot-raise",
        why=(
            "`app/observability` has one rule — instrumentation that can fail "
            "the thing it measures turns an observability bug into an outage — "
            "and the first version of the cache-size recorder coerced "
            "`stats['evictions']` to int *outside* `_safe`, so a probe handed "
            "None raised into the collection wave that called it. Its own test "
            "caught it, and then the obvious fix (`or 0`) made the wrapper "
            "untestable: the mutation survived until the test was given an "
            "input only the wrapper can absorb."
        ),
        path="app/observability/metrics.py",
        old="    _safe(lambda: _record_evictions(stats))",
        new="    _record_evictions(stats)  # mutation: arithmetic outside the guard",
        tests="tests/test_cache_size_metrics.py",
    ),
    Mutation(
        name="soak-refuses-a-trend-through-a-host-disturbance",
        why=(
            "The soak publishes resident memory as start/peak/end plus a "
            "second-half trend, and the envelope quotes those trends as "
            "evidence of no leak. A run where both workers' RSS fell together "
            "at minute 15 — worker-2 to 35 MB against a 124 MB peak — reported "
            "`start 118.5 MB, end 77.2 MB, trend +8.4 MB/h`: growth credited to "
            "a process that ended 41 MB lower, with the trough shown nowhere "
            "because only the peak was printed."
        ),
        path="../scripts/soak_bench.py",
        old="        if falls and all(falls) and len(falls) == len(workers):",
        new="        if False:  # mutation: no fall is ever the host's doing",
        tests="tests/test_soak_memory_report.py",
    ),
    Mutation(
        name="sse-check-counts-only-live-frames",
        why=(
            "The SSE incremental-delivery check compared the client's whole "
            "arrival span against the platform's whole emission span. The "
            "investigation is submitted before the stream opens and "
            "`subscribe()` replays the backlog, so events emitted before the "
            "connection existed arrive in one burst — shortening one side and "
            "leaving the other alone, so a *faster* platform reads as a "
            "buffered blob. It failed the required integration-verify job at "
            "49.87% against a 50% threshold, one run after passing at 57%."
        ),
        path="../scripts/verify_deployment.py",
        old="        if emission + offset > opened + BACKLOG_SLACK_SECONDS",
        new="        if True  # mutation: count the backlog burst as live delivery",
        tests="tests/test_sse_delivery_check.py",
    ),
    Mutation(
        name="both-providers-are-bracketed",
        why=(
            "One bracket sees the cluster move but not a provider that is "
            "nondeterministic in itself, because that provider is read once. "
            "`kubectl logs --all-containers` fetches each container "
            "concurrently and has no stable order — 22 init-first, 7 "
            "sidecar-first, 1 app-first over 30 live reads of an unchanging "
            "pod — where the agent enumerates in spec order. An agent-only "
            "bracket would call that a divergence."
        ),
        path="tests/differential.py",
        old="    if other_control is not None:\n        churning |= unstable(other, other_control)",
        new="    if False:  # mutation: the other provider is read once and trusted\n        churning |= unstable(other, other_control)",
        tests="tests/test_differential_control.py",
    ),
    Mutation(
        name="churn-exclusion-has-a-floor",
        why=(
            "The other direction of the same fix, and the quieter one. An "
            "exclusion with no floor passes hardest exactly when the cluster "
            "is least readable: discount every difference as churn and the "
            "suite proves nothing forever while reporting green. Same shape "
            "as an over-strict grounding check routing everything to the "
            "deterministic fallback with 20/20 golden cases still passing."
        ),
        path="tests/differential.py",
        old="        if self.total == 0:\n            return None",
        new="        if True:\n            return None  # mutation: no comparison is ever refused",
        tests="tests/test_differential_control.py",
    ),
    Mutation(
        name="forked-read-keeps-its-stderr",
        why=(
            "F22: gRPC's fork handlers wrote to the stderr `capture_output` "
            "collects a kubectl read's own error from, so a failed read on a "
            "gateway worker reported `ev_poll_posix.cc:593` instead of what "
            "kubectl said. The fix is an import-order property — the variable "
            "is read at gRPC's first initialisation — so an import added "
            "above it makes it inert with every other test still passing."
        ),
        path="app/__init__.py",
        old='os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")',
        new="pass  # mutation: the fork handlers are back",
        tests="tests/test_forked_reads.py",
    ),
    Mutation(
        name="presence-failopen-is-counted",
        why=(
            "F23: M8a's refusal fails open when the presence index cannot be "
            "read — measured at 1 investigation in 1,168 over an hour — and "
            "nothing counted it. `cluster_access_total` cannot: a fail-open "
            "and a correct local read are both `provider=kubeconfig`, so the "
            "10% fleet alert is ~116x above it and structurally blind."
        ),
        path="app/services/investigation_service.py",
        old='        metrics.agent_presence_failopen()\n        return ""',
        new='        return ""',
        tests="tests/test_metrics.py",
    ),
    Mutation(
        name="auth-mode-has-no-default",
        why=(
            "`AUTH_MODE` defaulted to `disabled`, so `ALLOW_INSECURE_NO_AUTH="
            "true` on its own served every endpoint unauthenticated — the "
            "acknowledgement selecting the mode as a side effect of "
            "acknowledging it, with nobody ever choosing `disabled`. An "
            "`AUTH_MODE` that failed to arrive chose it too, silently."
        ),
        path="app/core/config.py",
        old='auth_mode: str = Field(default="", validation_alias="AUTH_MODE")',
        new='auth_mode: str = Field(default="disabled", validation_alias="AUTH_MODE")',
        tests="tests/test_auth.py",
    ),
    Mutation(
        name="auth-mode-unset-is-not-resolved",
        why=(
            "The same defect from the other side: a fallback in "
            "`build_authenticator` resolves an unset mode to `disabled` even "
            "with no default in the settings, which leaves the guard above it "
            "present, correct-looking and unreachable."
        ),
        path="app/auth/authenticators.py",
        old='    mode = (config.auth_mode or "").strip().lower()',
        new='    mode = (config.auth_mode or "disabled").strip().lower()',
        tests="tests/test_auth.py",
    ),
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
        old=(
            "        if get_agent_registry().get(context) is not None:\n"
            "            return presence.worker_id\n"
        ),
        # Re-anchored after F21 nested this block under
        # `agent_gateway_enabled`. The registry lookup is unchanged; only
        # its indentation moved. Removing the enclosing `if` instead would
        # take the import with it and fail on a NameError rather than on
        # the routing assertion — a different test passing for a different
        # reason, which is what re-anchoring exists to avoid.
        new="        if False:\n            return presence.worker_id\n",
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
        old="    if _agent_was_revoked(context):",
        new="    if False and _agent_was_revoked(context):",
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
        old="if not self.enabled or not result.success or result.not_found:",
        new="if not self.enabled or result.not_found:",
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
        name="previous-logs-ask-for-the-current-container",
        why=(
            "The agent compares its parameters literally "
            '(`parameters["previous"] == "true"`), and Python\'s `str(True)` '
            'is `"True"`, so the option never matched. Losing it does not '
            "fail the read — the log endpoint serves the *current* container "
            "instead — so the agent path filed the running container's output "
            "under `k8s.pod.logs.previous` with status OK, on exactly the "
            "CrashLoopBackOff investigations where the previous instance is the "
            "only thing that says why it crashed, and counted it as a usable "
            "read. Found by diffing an agent-served investigation against a "
            "kubeconfig-served one of the same namespace in the same minute; "
            "neither the kind tables nor the differential suite could see it."
        ),
        path="app/providers/remote_agent.py",
        old="        parameters[str(key)] = _parameter_value(value)",
        new="        parameters[str(key)] = str(value)",
        tests="tests/test_provider_parity.py",
    ),
    Mutation(
        name="previous-serialised-as-something-plausible",
        why=(
            "The same defect wearing a value that is not a Python repr. A test "
            'asserting merely `!= "True"` passes for `"1"`, `"yes"` or '
            '`""` — every one of which leaves the agent serving the current '
            'container exactly as the defect did. Only the literal `"true"` '
            "works, so that is what is asserted, and this is what proves it."
        ),
        path="app/providers/remote_agent.py",
        old='        return "true" if value else "false"',
        new='        return "1" if value else "0"',
        tests="tests/test_provider_parity.py",
    ),
    Mutation(
        name="remediation-invents-the-object-it-cannot-name",
        why=(
            "`workload.missing_configuration` fires from `pod.config_error` "
            "alone — a pod in CreateContainerConfigError, which names the pod "
            "and the namespace and nothing it references — so `kind` and "
            "`name` fell back to `ConfigMap` and `<name>` silently. The plan "
            'then asserted "ConfigMap payments/<name> is referenced by the '
            'pod but does not exist" as a finding, generated a '
            "`<name>-configmap.yaml` containing `name: <name>`, and handed the "
            "operator `kubectl get configmap <name> -n payments`. The kind was "
            "a guess that could as easily have been Secret, which costs that "
            'branch its "values are never generated" note. `MemoryLimitRule` '
            "already had the right shape: say so, and refuse to propose a "
            "value evidence does not support. Found by reading a rendered PDF, "
            "which is where an operator meets it."
        ),
        path="app/remediation/rules.py",
        old='        identified = bool(attributes.get("name"))',
        new="        identified = True",
        tests="tests/test_remediation.py",
    ),
    Mutation(
        name="agent-list-reads-are-uncapped",
        why=(
            "F5's ceiling lived inside `KubectlExecutor`, so it applied to the "
            "kubeconfig path and nowhere else. `RemoteAgentProvider` carried a "
            "`_truncations` list that was initialised and never appended to — "
            "it existed to satisfy the protocol — so an agent-reached cluster "
            "was read with no ceiling at all and `collection_limits.truncated` "
            "reported `false` for a read that had never been bounded. Measured "
            "live at MAX_LIST_ITEMS=3 on a ten-pod namespace: kubeconfig "
            "returned 3 pods and four truncation records, the agent returned 10 "
            "and none. The same cluster read two ways disagreed about how many "
            "pods it has, and the transport real fleets use was the unbounded "
            "one. This removes the cap from the agent path again."
        ),
        path="app/providers/remote_agent.py",
        old="            data, truncation, _total = cap_items(data, command, settings.max_list_items)",
        new="            truncation = None",
        tests="tests/test_list_limit_parity.py",
    ),
    Mutation(
        name="agent-truncation-is-silent",
        why=(
            "The half that makes the evidence lie rather than merely shrink. A "
            "capped read that records nothing is indistinguishable from a "
            "complete read of a smaller cluster — which is exactly what "
            "`truncated: false` was claiming — so the cap and its record are "
            "two separate things to get wrong and this is the second."
        ),
        path="app/providers/remote_agent.py",
        old="                self._truncations.append(truncation)",
        new="                pass  # mutation: capped, and said nothing",
        tests="tests/test_list_limit_parity.py",
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
    Mutation(
        name="soak-guard-share",
        why=(
            "The soak's vacuity guard was an absolute count, so a 60-minute "
            "run in which Docker Desktop killed the kind cluster four minutes "
            "in cleared a floor of 60 with 81 usable investigations out of "
            "1,172 — a platform failing 93% of the time — and published memory "
            "trends taken from an hour of 'Unable to connect'. A floor cannot "
            "see a share."
        ),
        path="../scripts/soak_bench.py",
        old='    if share < state["min_share"]:',
        new="    if False:",
        tests="tests/test_soak_guard.py",
    ),
    Mutation(
        name="soak-guard-continuity",
        why=(
            "The same run, seen the other way round: 81 good investigations is "
            "the same 7% whether they were spread over the hour or all landed "
            "before the cluster died. Offered load drops when a cluster dies "
            "slowly, so a run can hold a high success *rate* while measuring "
            "nothing after minute ten. Only a timeline separates them."
        ),
        path="../scripts/soak_bench.py",
        old='    if timeline["longest_gap"] > allowed_gap:',
        new="    if False:",
        tests="tests/test_soak_guard.py",
    ),
    Mutation(
        name="soak-guard-trailing-gap",
        why=(
            "Written the same day as the check above and inert on arrival. The "
            "gap list was built only *between* good investigations, so run 3 — "
            "whose last usable investigation was at minute 4 of 60 — reported a "
            "longest quiet gap of six seconds. It was caught by running the "
            "guard against the real run's shape, not by reading it; the share "
            "check happened to fire and hid it."
        ),
        path="../scripts/soak_bench.py",
        old="    marks = [started, *good, started + elapsed]",
        new="    marks = [started, *good]",
        tests="tests/test_soak_guard.py",
    ),
    Mutation(
        name="f21-refusal-needs-no-gateway",
        why=(
            "F21. The presence index was installed inside the "
            "`agent_gateway_enabled` branch of `app/state.py`, so on a worker "
            "with no gateway of its own `select_provider` never consulted it "
            "and answered an agent-held cluster from a local context that "
            "merely shares the name — with no tenant, which is the "
            "cross-tenant harm M8a's refusal exists to prevent. Unreachable "
            "in the shipped topology, reachable mid-rollout."
        ),
        path="app/services/investigation_service.py",
        old="    holder = _fleet_holder(context)\n    if holder:",
        new=(
            '    holder = _fleet_holder(context) if settings.agent_gateway_enabled else ""\n'
            "    if holder:"
        ),
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="f21-fleet-index-is-the-state-backends",
        why=(
            "The wiring half of F21, which behaviour cannot see: every routing "
            "test installs a presence index by hand, so they all pass whether "
            "or not startup would ever create one. That was the shape of the "
            "original defect — the routing logic was right and the wiring was "
            "wrong somewhere else entirely."
        ),
        path="app/state.py",
        old="    install_fleet_index(database, bus, worker)\n\n",
        new="",
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="f21-revocation-off-the-default-path",
        why=(
            "F21's fix moved the revocation check out from behind the gateway "
            "flag, which put it on the single-process path — where "
            "`get_enrolment_store()` lazily builds a file store and the check "
            "*refuses on a read failure*. Ungated, an unreadable "
            "AGENT_IDENTITY_DIR fails every investigation on the "
            "getting-started path. The first test written for this guard used "
            "a store with no records and passed with the guard removed; only "
            "one that raises can tell the two apart."
        ),
        path="app/services/investigation_service.py",
        old=(
            "    if not (settings.agent_gateway_enabled or settings.distributed_state):\n"
            "        return False\n\n"
        ),
        new="",
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="guidance-for-the-leader-not-the-selection",
        why=(
            "The plan, commands, prevention and follow-up were built for the "
            "deterministic leader even when a grounded model selected another "
            "hypothesis, so a root cause about one resource shipped with a "
            "remediation plan for a different one."
        ),
        path="app/ai/root_cause_analyzer.py",
        old="        top = hypothesis or analysis.top_hypothesis\n",
        new="        top = analysis.top_hypothesis  # mutation: ignore the selection\n",
        tests="tests/test_fix_recommendation.py",
    ),
    Mutation(
        name="no-playbook-for-create-container-config-error",
        why=(
            "No playbook triggered on CreateContainerConfigError, so the "
            "reference resolver never ran for the canonical missing-ConfigMap "
            "fault: the plan said no evidence named the object while the pod's "
            "events did, and called a ReplicaSet-owned pod unmanaged."
        ),
        path="app/playbooks/kubernetes.py",
        old="    ConfigurationPlaybook(),\n",
        new="",
        tests="tests/test_configuration_investigation.py",
    ),
    Mutation(
        name="failed-reference-read-reported-absent",
        why=(
            "A refused or unsupported ConfigMap/Secret read was recorded as the "
            "object not existing. Live, a Secret that existed became the root "
            "cause of every run on both providers — Forbidden under the shipped "
            "read-only RBAC, unsupported through the agent."
        ),
        path="app/analysis/deep_signal_rules.py",
        old=(
            '                if reference.get("exists") is None:\n'
            "                    continue\n"
            '                if reference.get("exists") is False:\n'
        ),
        new='                if not reference.get("exists"):\n',
        tests="tests/test_configuration_investigation.py",
    ),
    Mutation(
        name="agent-named-404-not-recognised-as-absent",
        why=(
            "The resolver recognised absence by kubectl's stderr wording; an "
            "agent reports a named 404 as EMPTY with no text, so through an "
            "agent a missing ConfigMap could not be named."
        ),
        path="app/providers/remote_agent.py",
        old="                record.status == evidence_pb2.EVIDENCE_STATUS_EMPTY\n",
        new="                False  # mutation: named EMPTY is not absence\n",
        tests="tests/test_not_found_parity.py",
    ),
    Mutation(
        name="uncollected-owner-reported-as-no-owner",
        why=(
            "With no pod spec collected, remediation read its fallback-to-pod "
            "target as an observed bare pod and said 'No controller owns' a "
            "pod whose ReplicaSet was one read away."
        ),
        path="app/remediation/context.py",
        old='        return self.target.kind != "Pod" or self.pod_spec() is not None\n',
        new="        return True  # mutation: ownership always known\n",
        tests="tests/test_remediation.py",
    ),
    Mutation(
        name="deep-round-always-claimed-necessary",
        why=(
            "Every report with a playbook round said 'the initial pass alone "
            "would not have reached this conclusion', including those whose "
            "baseline had already selected the same cause."
        ),
        path="app/reports/composer.py",
        old="        if before is None or not selected:\n",
        new=(
            "        if True:  # mutation: unconditional claim\n"
            '            return "the initial pass alone would not have reached this conclusion."\n'
            "        if before is None or not selected:\n"
        ),
        tests="tests/test_configuration_investigation.py",
    ),
    Mutation(
        name="report-namespace-of-the-first-pod",
        why=(
            "The report and Reports table filed an investigation under the "
            "first problematic pod's namespace — local-path-storage for a "
            "diagnosis of payments/checkout-svc."
        ),
        path="app/services/history_service.py",
        old="    def _namespace(self, investigation: dict[str, Any], diagnosis: dict[str, Any]) -> str:\n",
        new=(
            "    def _namespace(self, investigation: dict[str, Any], diagnosis: dict[str, Any]) -> str:\n"
            '        return investigation["pods"]["problematic_pods"][0]["namespace"]  # mutation\n'
        ),
        tests="tests/test_report_namespace.py",
    ),
    Mutation(
        name="primary-namespace-in-hash-order",
        why=(
            "'Primary namespace affected' was next(iter(set)), which Python "
            "orders by a per-process random hash: three seeds, two answers."
        ),
        path="app/services/investigation_service.py",
        old='        primary = min(affected, key=lambda name: (-affected[name], name)) if affected else "none"\n',
        new='        primary = next(iter(set(affected)), "none")  # mutation\n',
        tests="tests/test_report_namespace.py",
    ),
    Mutation(
        name="resource-scope-ignored-when-ranking",
        why=(
            "'Investigate deployment checkout' returned a root cause about the "
            "notifier pod, because a resource scope narrows two reads and "
            "ranking considered every pod in the namespace."
        ),
        path="app/analysis/engine.py",
        old="        in_scope = _scope_predicate(scope)\n",
        new="        in_scope = None  # mutation: scope ignored\n",
        tests="tests/test_scoped_diagnosis.py",
    ),
    Mutation(
        name="limits-rule-matches-a-label",
        why=(
            "ResourceLimitsRule matched a security check by its label text, in "
            "another module, so renaming labels that asserted outcomes would "
            "have silently removed the missing-limits signal."
        ),
        path="app/analysis/signal_rules.py",
        old=(
            '                if item.get("id") == "resource_limits"\n'
            '                or item.get("label") == "Missing Resource Limits"\n'
        ),
        new='                if item.get("label") == "Missing Resource Limits"\n',
        tests="tests/test_security_checks.py",
    ),
    Mutation(
        name="agents-endpoint-claims-worker-scope",
        why=(
            "GET /agents said scope 'worker' for six milestones after presence "
            "made it answer for the fleet, and /connect told operators so "
            "above a list holding another worker's agent."
        ),
        path="app/api/agents.py",
        old='        "scope": "fleet",\n',
        new='        "scope": "worker",\n',
        tests="tests/test_agent_presence.py",
    ),
    Mutation(
        name="cache-stores-absence",
        why=(
            "An agent's named 404 is a *successful* EMPTY read, so without this "
            "a 'ConfigMap does not exist' would be served from cache for the "
            "length of the TTL — to the operator who has just created it."
        ),
        path="app/providers/cache.py",
        old="if not self.enabled or not result.success or result.not_found:",
        new="if not self.enabled or not result.success:",
        tests="tests/test_not_found_parity.py",
    ),
    Mutation(
        name="node-restart-read-as-crash-loop",
        why=(
            "After a host restart every container's last termination was "
            "Unknown/255 with one more restart, and the API server, etcd and "
            "healthy workloads were all reported in CrashLoopBackOff."
        ),
        path="app/kubernetes/pod_inspector.py",
        old='NOT_A_CRASH = frozenset({"Completed", "Unknown"})\n',
        new='NOT_A_CRASH = frozenset({"Completed"})\n',
        tests="tests/test_pods_after_node_restart.py",
    ),
    Mutation(
        name="restart-history-never-becomes-history",
        why=(
            "A container that crashed four times while its node booted, then ran "
            "Ready for 23 minutes, was reported in CrashLoopBackOff for as long "
            "as the pod lived."
        ),
        path="app/kubernetes/pod_inspector.py",
        old="        return started is not None and observed_at - started >= STABLE_AFTER\n",
        new="        return False  # mutation: stability never proven\n",
        tests="tests/test_pods_after_node_restart.py",
    ),
    Mutation(
        name="graph-edges-cite-a-kind-not-a-record",
        why=(
            "Edge rules read `evidence_id`, a key deep entries never had, and "
            "cited the bare kind `k8s.pod.spec`; the console showed a citation "
            "chip with no record behind it."
        ),
        path="app/graph/edge_rules.py",
        old='    evidence_id = entry.get("id")\n',
        new='    evidence_id = entry.get("evidence_id") or "k8s.pod.spec"  # mutation\n',
        tests="tests/test_citations_resolve.py",
    ),
    Mutation(
        name="collected-evidence-reported-missing",
        why=(
            "Lessons Learned listed 'Container exit code and termination reason' "
            "as missing on a page showing the exit code from the pod spec the "
            "playbook round had collected."
        ),
        path="app/ai/root_cause_analyzer.py",
        old="            gaps.extend(item for item in outstanding(selected, investigation) if item not in gaps)\n",
        new="            gaps.extend(item for item in selected.missing_evidence if item not in gaps)\n",
        tests="tests/test_scoped_diagnosis.py",
    ),
    Mutation(
        name="rationale-names-a-reason-that-did-not-apply",
        why=(
            "The rationale always cited severity — 'ranked critical rather than "
            "critical' — for a cause the investigation's scope put first."
        ),
        path="app/analysis/incidents.py",
        old="    if scoped_resource and top.id in scoped and most_confident.id not in scoped:\n",
        new="    if False:  # mutation: scope never explains\n",
        tests="tests/test_scoped_diagnosis.py tests/test_reasoning_quality.py",
    ),
    Mutation(
        name="rationale-silent-when-only-breadth-ranked",
        why=(
            "Two faults on different pods, both 80% and critical, and the report "
            "said nothing about why one was the root cause and the other its "
            "alternative: the order was set by how many signals each rested on."
        ),
        path="app/analysis/incidents.py",
        old='        return _tie(top, hypotheses) if selected is None or top.id == leader.id else ""\n',
        new='        return ""\n',
        tests="tests/test_reasoning_quality.py",
    ),
    Mutation(
        name="tie-claims-breadth-decided-when-it-tied",
        why=(
            "Through the agent the report said the leader 'rests on more signals "
            "(15 against 15)': with breadth equal too, rank() orders by id."
        ),
        path="app/analysis/incidents.py",
        old="    if len(top.supporting_signal_ids) > len(runner_up.supporting_signal_ids):\n",
        new="    if True:  # mutation: breadth always the reason\n",
        tests="tests/test_reasoning_quality.py",
    ),
    Mutation(
        name="log-tail-reported-as-failure-lines",
        why=(
            "With no keyword matched the collector returned the log's tail under "
            "relevant_lines, so the fleet page read 'logs report: \"starting "
            "checkout service\"' as a HIGH error pattern."
        ),
        path="app/kubernetes/logs_collector.py",
        old="        ][:25]\n",
        new="        ][:25] or [line[:500] for line in logs.splitlines()[-20:]]\n",
        tests="tests/test_log_lines_are_failures.py",
    ),
    Mutation(
        name="fatal-is-not-a-failure-keyword",
        why="`FATAL: config key DB_HOST is not set` matched no keyword at all.",
        path="app/kubernetes/logs_collector.py",
        old='    "fatal",\n',
        new="",
        tests="tests/test_log_lines_are_failures.py",
    ),
    Mutation(
        name="api-server-service-reported-selectorless",
        why=(
            "default/kubernetes has no selector on every cluster, and every "
            "whole-cluster investigation carried network.no_selector for it."
        ),
        path="app/kubernetes/network_inspector.py",
        old="                if endpoint_count == 0:\n",
        new="                if True:  # mutation: every selector-less service\n",
        tests="tests/test_selectorless_services.py",
    ),
    Mutation(
        name="report-status-claims-an-incident-lifecycle",
        why=(
            "Every report's Status read Open, and a healthy run's read Resolved, "
            "for a ticket state nothing tracks."
        ),
        path="app/reports/rendering.py",
        old='        return _FINDING_STATUS.get(health, "Unknown")\n',
        new='        return "Resolved" if health == "healthy" else "Open"\n',
        tests="tests/test_report_metadata.py",
    ),
    Mutation(
        name="deployment-and-its-pods-counted-twice",
        why=(
            "Eight broken Deployments and nine of their pods were '17 workload(s) "
            "affected', and one Deployment with two failing replicas met the "
            "Critical threshold of three."
        ),
        path="app/services/investigation_service.py",
        old="    return len(unhealthy_deployments) + len(unowned) + len(workload_findings)\n",
        new="    return len(unhealthy_deployments) + len(problematic_pods) + len(workload_findings)\n",
        tests="tests/test_affected_workloads.py",
    ),
    Mutation(
        name="cluster-identity-lost-on-regenerate",
        why=(
            "One kind cluster reached through a kubeconfig and two agents was "
            "'the same failure on 3 clusters'. Node UIDs tell the names apart "
            "only if both history builders record them."
        ),
        path="app/services/history_service.py",
        old=(
            '            "scope": dict(investigation.get("scope") or {}),\n'
            "            # Which cluster this was, beneath its name: see\n"
            "            # `InvestigationService._cluster_identity`. Both builders, as scope.\n"
            '            "node_uids": list((investigation.get("cluster_identity") or {}).get("node_uids") or []),\n'
            '            "confidence": int(diagnosis.get("confidence", 0)),\n'
        ),
        new=(
            '            "scope": dict(investigation.get("scope") or {}),\n'
            '            "confidence": int(diagnosis.get("confidence", 0)),\n'
        ),
        tests="tests/test_cluster_identity.py",
    ),
    Mutation(
        name="one-refuted-pod-refutes-the-pool",
        why=(
            "fraud-scorer's OOM kill 'argued against' checkout failing on startup, "
            "because the rule pooled every pod with a BackOff event."
        ),
        path="app/analysis/hypothesis_rules.py",
        old="        if standing:\n",
        new="        if False:  # mutation: refute the pool\n",
        tests="tests/test_analysis_engine.py",
    ),
    Mutation(
        name="access-check-asks-about-the-read",
        why="Every plan's access check was `can-i get`, which a read-only operator passes.",
        path="app/remediation/models.py",
        old="        verb = changes[0] if changes else self.verbs[0]\n",
        new="        verb = self.verbs[0]\n",
        tests="tests/test_remediation_safety.py",
    ),
    Mutation(
        name="rollback-from-a-file-never-written",
        why="The service plan rolled back from checkout-svc-before.yaml, which no step wrote.",
        path="app/remediation/rules.py",
        old="                _capture_current(service),\n",
        new="",
        tests="tests/test_remediation_safety.py",
    ),
    Mutation(
        name="support-from-unrelated-workloads",
        why=(
            "checkout's startup failure rested on archiver's and ledger's unavailable "
            "replicas too, and that count broke a three-way tie."
        ),
        path="app/analysis/hypothesis_rules.py",
        old="        supporting = [signal for signal in supporting if _about(signal, triggering, signals)]\n",
        new="",
        tests="tests/test_analysis_engine.py",
    ),
    Mutation(
        name="selectorless-match-supported-by-any-pod",
        why=(
            "checkout-svc selects a label no pod has, and was 92% on the strength of "
            "archiver's pending pod and gateway's failing probe."
        ),
        path="app/analysis/hypothesis_rules.py",
        old="    if services and services <= {\n",
        new="    if False and services <= {\n",
        tests="tests/test_reasoning_quality.py",
    ),
    Mutation(
        name="event-about-another-object-supports-a-workload",
        why="A Warning on the archive-data claim supported notifier's missing ConfigMap and broke a tie.",
        path="app/analysis/hypothesis_rules.py",
        old='    if workloads_only and signal.type.startswith("event.") and kind not in {"Pod", "Deployment"}:\n',
        new="    if False:  # mutation: events about any object count\n",
        tests="tests/test_analysis_engine.py",
    ),
    Mutation(
        name="restart-history-said-in-the-present-tense",
        why=(
            "Minutes after a node came back, three pods kubectl printed Running and "
            "Ready were reported 'in CrashLoopBackOff', one as the root cause."
        ),
        path="app/kubernetes/pod_inspector.py",
        old="            return reason, any(now for found, now in candidates if found == reason)\n",
        new="            return reason, True  # mutation: history reads as now\n",
        tests="tests/test_recently_restarted_pods.py",
    ),
    Mutation(
        name="pod-status-tense-ignored-by-the-signal",
        why="The inspector recorded which tense applied and the signal said 'is in' regardless.",
        path="app/analysis/signal_rules.py",
        old='            reported_now = pod.get("reported_now", True)\n',
        new="            reported_now = True  # mutation: always the present tense\n",
        tests="tests/test_recently_restarted_pods.py",
    ),
    Mutation(
        name="refutation-across-unrelated-resources",
        why=(
            "Refuting signals matched by type anywhere in the namespace, so "
            "notifier's missing ConfigMap was reported as evidence against "
            "checkout failing on startup."
        ),
        path="app/analysis/hypothesis_rules.py",
        old="            if signal.type in self.refuting and signal.target.key in triggered\n",
        new="            if signal.type in self.refuting\n",
        tests="tests/test_analysis_engine.py",
    ),
    Mutation(
        name="history-item-without-scope-on-live-save",
        why=(
            "The fleet card and cluster page took the newest run whatever it was "
            "asked about; recording scope fixed it only if the live save wrote "
            "it, and the first version wrote it on the regenerate path alone."
        ),
        path="app/services/history_service.py",
        old=(
            "            # console's fix would have read nothing on every live save.\n"
            '            "scope": dict(investigation.get("scope") or {}),\n'
        ),
        new="            # console's fix would have read nothing on every live save.\n",
        tests="tests/test_report_namespace.py",
    ),
    Mutation(
        name="rationale-computed-and-rendered-nowhere",
        why=(
            "selection_rationale was computed since the audit asked for it and "
            "rendered only by a panel no route mounts, so the report showed a "
            "less confident cause SELECTED with no reason given."
        ),
        path="app/reports/composer.py",
        old='        body = [explanation, str(diagnosis.get("selection_rationale") or "")]\n',
        new="        body = [explanation]\n",
        tests="tests/test_scoped_diagnosis.py",
    ),
    Mutation(
        name="reaper-reaps-straight-after-a-store-outage",
        why=(
            "With Postgres paused past the lease, no worker could renew, and the "
            "first reaper tick after recovery failed a live investigation as a "
            "dead worker; the console showed Failed while it succeeded."
        ),
        path="app/jobs/consumer.py",
        old="                if time.monotonic() - self._store_answering_since >= settings.job_lease_seconds:\n",
        new="                if True:  # mutation: reap on the first tick after recovery\n",
        tests="tests/test_reaper_after_store_outage.py",
    ),
    Mutation(
        name="reaper-ignores-a-query-that-hung",
        why=(
            "The reaper whose query was in flight when Postgres paused saw no "
            "error — the query waited out the pause and returned — and reaped "
            "the live job on that tick."
        ),
        path="app/jobs/consumer.py",
        old="                    or answered - probe_started > settings.job_lease_seconds / 3\n",
        new="                    or False  # mutation: only raised errors count\n",
        tests="tests/test_reaper_after_store_outage.py",
    ),
    Mutation(
        name="success-keeps-the-reapers-error",
        why=(
            "A job reaped while its worker was alive completed onto the reaper's "
            "message and read `succeeded` and 'worker stopped' at once."
        ),
        path="app/jobs/store.py",
        old='        job.error = ""\n',
        new="",
        tests="tests/test_job_store_contract.py",
    ),
    Mutation(
        name="hung-redis-takes-the-fleet-out-of-rotation",
        why=(
            "A paused Redis accepted the connection and never answered; the "
            "whole readiness check timed out and every worker reported the store "
            "unavailable, a fleet-wide outage for a degradation."
        ),
        path="app/jobs/distributed.py",
        old="    thread.join(PROBE_DEADLINE_SECONDS)\n",
        new="    thread.join()  # mutation: wait for a hung dependency\n",
        tests="tests/test_operability.py",
    ),
    Mutation(
        name="unreadable-agent-index-reads-as-no-agents",
        why=(
            "With Redis paused GET /agents answered items: [] on the worker "
            "holding an agent's stream, and the console said no agent had "
            "connected."
        ),
        path="app/gateway/presence.py",
        old=(
            '            logger.warning("Could not read agent presence: {error}", error=exc)\n'
            "            return None\n"
        ),
        new=(
            '            logger.warning("Could not read agent presence: {error}", error=exc)\n'
            "            return []\n"
        ),
        tests="tests/test_agent_presence.py",
    ),
    Mutation(
        name="away-agent-answered-from-a-missing-kubeconfig-context",
        why=(
            "With the worker holding an agent's stream frozen, the re-offered "
            "investigation fell back to the platform's kubeconfig for a cluster "
            "it has no context for, and failed telling the operator to check "
            "their kubeconfig."
        ),
        path="app/services/investigation_service.py",
        old="    if _enrolled_agent_is_away(context):\n",
        new="    if False:  # mutation: never refuse for an away agent\n",
        tests="tests/test_agent_routing.py",
    ),
    Mutation(
        name="agent-network-error-becomes-kubectl-advice",
        why=(
            "An agent whose API server was unreachable returned dial tcp i/o "
            "timeout for every read, recorded as 'Verify kubeconfig, cluster "
            "access, and kubectl permissions' with the reason discarded."
        ),
        path="app/kubernetes/errors.py",
        old="    if any(needle in lowered for needle in _NETWORK_NEEDLES):\n",
        new="    if False:  # mutation: network errors fall to the kubectl default\n",
        tests="tests/test_agent_api_server_unreachable.py",
    ),
    Mutation(
        name="failed-run-reported-as-success",
        why=(
            "Every report was saved with the literal status 'success', so a run "
            "that collected nothing read 'Status: success' under a Failed badge."
        ),
        path="app/services/investigation_runner.py",
        old='            "failed" if collection_failure(investigation) else "success",\n',
        new='            "success",\n',
        tests="tests/test_investigation_service.py",
    ),
    Mutation(
        name="presence-age-compares-two-workers-clocks",
        why=(
            "last_seen from the stream-holding worker was aged against the "
            "reading worker's clock, so a lagging writer's healthy agents read "
            "silent from every other replica."
        ),
        path="app/gateway/presence.py",
        old="    if written is not None and elapsed_since_write is not None:\n",
        new="    if False:  # mutation: reader clock against writer clock\n",
        tests="tests/test_agent_presence.py",
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
