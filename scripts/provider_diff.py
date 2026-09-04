"""Does an agent-reached cluster produce the same investigation as a kubeconfig one?

This is the check that has found more real defects than anything else in the
repository, and until now it existed only as a sentence in `CLAUDE.md` telling
you to do it by hand. Four defects came out of it:

- the baseline pod-log read had no `OutputFormat.TEXT`, so it failed for every
  pod that had anything to say and succeeded for the silent ones;
- `previous` reached the agent as `"True"` where it compares against `"true"`,
  so "previous container logs" held the *current* container's output, status OK;
- `all_containers` was sent and never read, so any pod with a sidecar lost its
  logs entirely on the agent path (F24);
- `MAX_LIST_ITEMS` applied on one provider only, so an agent-reached cluster was
  read with no ceiling and reported `truncated: false` for it (F25).

**Three comparisons, because each is a separate net and each has caught
something the other two missed.**

- *status*  — every evidence id and its status. Catches a read that fails on one
  provider and not the other. Found the `OutputFormat.TEXT` defect.
- *command* — the `equivalent_command` each record carries. Catches a read that
  succeeds on both while asking for different things. Found F25, after status
  had come back clean across four scopes.
- *content* — the structure and the resource identities of each derived section.
  Catches a read that succeeds on both, asks the same thing, and returns
  different data. This is the net that F24's `previous` sibling slipped through:
  the wrong container's logs are a *successful* read of the right shape.

Usage — you control the agent between the two captures, because which provider
serves a cluster is decided by whether its agent is connected:

    # with the agent connected
    python scripts/provider_diff.py capture --out /tmp/agent.json \\
        --namespace payments --context kind-my-cluster

    # stop the agent, wait for the platform to notice, then
    python scripts/provider_diff.py capture --out /tmp/kube.json \\
        --namespace payments --context kind-my-cluster

    python scripts/provider_diff.py compare /tmp/agent.json /tmp/kube.json

**It refuses to report a clean comparison that means nothing**, which is not
hypothetical: a Docker Desktop crash took the cluster out mid-session and both
providers returned eleven records of `Unable to connect`. Every status matched,
every command matched, every section was structurally identical, and the run was
worth nothing. `compare` therefore requires the two captures to come from
*different* providers and both to have collected usable evidence, and says so
rather than printing zeros.

**Churn is not divergence, and the way to tell is to run it twice.** Two
captures seconds apart against crash-looping pods legitimately differ in ages,
restart counts and which workloads are currently "unhealthy". Volatile fields
are excluded by name (and the exclusions are printed, so an over-broad one is
visible rather than silent). For anything left, the test is direction: a real
divergence is stable and always favours the same provider, while churn swaps
sides between runs. That is exactly how a flapping OOMKilled deployment was
told apart from a defect — it appeared only under the agent in one run and only
under the kubeconfig in the next.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# Fields that legitimately differ between two captures seconds apart. Named
# rather than pattern-matched so the list can be read and argued with.
VOLATILE = frozenset(
    {
        "age",
        "age_seconds",
        "collected_at",
        "duration_ms",
        "timestamp",
        "last_seen",
        "restart_count",
        "restarts",
        "count",
        "line_count",
        "lines",
        "relevant_lines",
        "message",
        "first_seen",
        "last_timestamp",
        "started_at",
        "created_at",
        "usage",
        "cpu",
        "memory",
        "percent",
        "value",
        "seconds",
        "oldest_evidence_seconds",
        "hits",
        "misses",
        "generated_at",
        "id",
        "investigation_id",
        "elapsed",
    }
)

SECTIONS = (
    "pods",
    "deployments",
    "nodes",
    "network",
    "storage",
    "workloads",
    "security",
    "topology",
    "metrics",
    "graph",
    "overview",
    "events",
)


def _call(base, token, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def capture(args):
    scope = {"refresh": True}
    for field in ("namespace", "context", "resource_kind", "resource_name"):
        if getattr(args, field, None):
            scope[field] = getattr(args, field)

    job = _call(args.base, args.token, "/investigations", scope)
    status = ""
    for _ in range(args.timeout // 2):
        status = _call(args.base, args.token, f"/investigations/{job['id']}/status").get(
            "status", ""
        )
        if status in ("succeeded", "failed"):
            break
        time.sleep(2)

    result = _call(args.base, args.token, f"/investigations/{job['id']}")
    with open(args.out, "w") as handle:
        json.dump(result, handle)

    investigation = _investigation(result)
    coverage = investigation.get("evidence_coverage", {})
    print(
        f"{status}: provider={investigation.get('cluster_access', {}).get('provider')} "
        f"records={coverage.get('total')} usable={coverage.get('usable')} -> {args.out}"
    )
    return 0 if status == "succeeded" else 1


def _investigation(result):
    return result.get("result", {}).get("investigation") or result.get("investigation") or {}


def _key_paths(node, prefix="", out=None):
    """Every key path in a structure, list indices collapsed.

    A property of the code that built the section, not of the cluster at that
    instant, so churn does not move it.
    """
    if out is None:
        out = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in VOLATILE:
                _key_paths(value, f"{prefix}.{key}", out)
    elif isinstance(node, list):
        out.add(prefix + "[]")
        for item in node[:50]:
            _key_paths(item, prefix + "[]", out)
    else:
        out.add(prefix)
    return out


def _identities(node, out=None):
    """Every `namespace/name` a section mentions."""
    if out is None:
        out = set()
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str) and name:
            out.add(f"{node.get('namespace') or ''}/{name}")
        for value in node.values():
            _identities(value, out)
    elif isinstance(node, list):
        for item in node:
            _identities(item, out)
    return out


# Flags that differ because the two providers *render* a command differently,
# not because they read different things. Named with reasons, the way
# `test_provider_parity.py` names the parameters the agent does not read — a
# harness that reports thirty cosmetic differences trains you to skip its
# output, which is the same way a flaky required CI job stops being a gate.
#
# The evidence id already pins the kind and the target, since that is how a
# record is matched back to its request. So what is worth comparing is the
# options: those are what change *what comes back*, and a missing one is what
# both the `previous` defect and F24 turned out to be.
RENDERING_ONLY = {
    "--context": "the agent has no notion of a kubeconfig context",
    "--chunk-size": "kubeconfig paging; the agent sends `limit` in the query instead",
    "--no-headers": "kubectl top is text on one path and a metrics API list on the other",
    "-o": "output format is decided by the kind, not by the request",
    "-A": "added by the kubeconfig renderer for cluster-scoped reads; the scope is identical",
    "--all-namespaces": "same as -A",
}


def _semantic_flags(command):
    """The options a command carries that change what comes back.

    A flag's value may be attached (`--field-selector=x`) or a separate token
    (`--field-selector x`) — the two providers do not agree on which, so both
    are folded to one form. Getting that wrong drops the value on one side and
    reports every selector as a difference, which is what the first version did.
    """
    if not command:
        return None

    tokens = command.split()
    flags = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("-"):
            continue

        if "=" in token:
            name, value = token.split("=", 1)
        else:
            name = token
            value = ""
            # A following non-flag token is this flag's value.
            if index < len(tokens) and not tokens[index].startswith("-"):
                value = tokens[index]
                index += 1

        if name in RENDERING_ONLY:
            continue
        flags.add(f"{name}={value}" if value else name)
    return flags


def _records(investigation):
    return {
        e["id"]: (e.get("status"), e.get("command") or "")
        for e in investigation.get("evidence", [])
    }


def compare(args):
    with open(args.left) as handle:
        left = json.load(handle)
    with open(args.right) as handle:
        right = json.load(handle)
    a, b = _investigation(left), _investigation(right)

    pa = a.get("cluster_access", {}).get("provider")
    pb = b.get("cluster_access", {}).get("provider")
    ua = (a.get("evidence_coverage") or {}).get("usable") or 0
    ub = (b.get("evidence_coverage") or {}).get("usable") or 0

    print(f"  {args.left}: provider={pa} usable={ua}")
    print(f"  {args.right}: provider={pb} usable={ub}")

    # The vacuity guard. A cluster that was down gives two identical piles of
    # nothing, and every comparison below reports perfect parity.
    if pa == pb:
        print(
            f"\nREFUSED: both captures were served by {pa!r}. This compares "
            f"providers; two captures from the same one cannot show a divergence.",
            file=sys.stderr,
        )
        return 2
    if not ua or not ub:
        print(
            f"\nREFUSED: usable evidence was {ua} and {ub}. A capture that "
            f"collected nothing matches any other capture that collected "
            f"nothing — check the cluster is up before trusting a clean result.",
            file=sys.stderr,
        )
        return 2

    ra, rb = _records(a), _records(b)
    shared = sorted(set(ra) & set(rb))
    findings = 0

    only_a, only_b = sorted(set(ra) - set(rb)), sorted(set(rb) - set(ra))
    if only_a or only_b:
        findings += 1
        print(f"\n[evidence set] {len(only_a)} only in {pa}, {len(only_b)} only in {pb}")
        for i in only_a[:8]:
            print(f"    only {pa}: {i}")
        for i in only_b[:8]:
            print(f"    only {pb}: {i}")

    status_diffs = [(i, ra[i][0], rb[i][0]) for i in shared if ra[i][0] != rb[i][0]]
    print(f"\n[status]  {len(shared)} shared records, {len(status_diffs)} differences")
    for i, x, y in status_diffs:
        findings += 1
        print(f"    {i}\n      {pa}={x}  {pb}={y}")

    command_diffs = []
    for i in shared:
        fa, fb = _semantic_flags(ra[i][1]), _semantic_flags(rb[i][1])
        if fa is None or fb is None or fa == fb:
            continue
        command_diffs.append((i, sorted(fa - fb), sorted(fb - fa)))

    print(
        f"\n[command] {len(command_diffs)} option differences "
        f"(ignoring {', '.join(sorted(RENDERING_ONLY))} as rendering)"
    )
    for i, only_a, only_b in command_diffs:
        findings += 1
        print(f"    {i}")
        if only_a:
            print(f"       only {pa}: {only_a}")
        if only_b:
            print(f"       only {pb}: {only_b}")

    print(f"\n[content] excluding volatile fields: {', '.join(sorted(VOLATILE))}")
    for section in SECTIONS:
        if section not in a and section not in b:
            continue
        ka, kb = _key_paths(a.get(section)), _key_paths(b.get(section))
        ia, ib = _identities(a.get(section)), _identities(b.get(section))
        if ka != kb:
            findings += 1
            print(f"    [{section}] structure differs")
            if ka - kb:
                print(f"       only {pa}: {sorted(ka - kb)[:8]}")
            if kb - ka:
                print(f"       only {pb}: {sorted(kb - ka)[:8]}")
        if ia != ib:
            findings += 1
            print(f"    [{section}] identities differ")
            if ia - ib:
                print(f"       only {pa}: {sorted(ia - ib)[:8]}")
            if ib - ia:
                print(f"       only {pb}: {sorted(ib - ia)[:8]}")

    print(f"\n=> {findings} difference(s).")
    if findings:
        print(
            "Run it again before believing any of them. A real divergence is "
            "directional and repeats; churn swaps sides between runs."
        )
    return 1 if findings else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8778")
    parser.add_argument("--token", default="soaktok")
    sub = parser.add_subparsers(dest="mode", required=True)

    cap = sub.add_parser("capture", help="run one investigation and save it")
    cap.add_argument("--out", required=True)
    cap.add_argument("--namespace")
    cap.add_argument("--context")
    cap.add_argument("--resource-kind", dest="resource_kind")
    cap.add_argument("--resource-name", dest="resource_name")
    cap.add_argument("--timeout", type=int, default=180)
    cap.set_defaults(func=capture)

    cmp_ = sub.add_parser("compare", help="diff two captures")
    cmp_.add_argument("left")
    cmp_.add_argument("right")
    cmp_.set_defaults(func=compare)

    args = parser.parse_args()
    try:
        return args.func(args)
    except urllib.error.URLError as error:
        print(f"could not reach {args.base}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
