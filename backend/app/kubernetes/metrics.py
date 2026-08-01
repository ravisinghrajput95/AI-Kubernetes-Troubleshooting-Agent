"""One shape for resource usage, whichever provider measured it.

`kubectl top` and the metrics API do not return the same thing, and pretending
otherwise was the last real obstacle to running the full collector set through
an agent:

- **kubectl top** prints text, and computes a percentage for nodes by dividing
  usage by allocatable capacity — capacity the metrics API never returned to it.
- **metrics.k8s.io** returns usage as quantities (`120500000n`, `1443Mi`) and
  nothing else.

Two ways to close that gap. Teaching the Go agent to reproduce kubectl's column
layout would put a formatting contract in a binary shipped to a thousand
clusters, and make the evidence differ for reasons nobody could see — the exact
trap M4a avoided by reading raw JSON rather than typed objects.

So the percentage is **derived on the platform, for both providers, from node
allocatable capacity the platform already collects as evidence**. kubectl's own
percentage column is parsed and discarded. That costs a rounding difference
against `kubectl top` and buys two things worth more: the two paths agree, and
a number that used to arrive as an opaque column is now computed from something
citable.
"""

import re
from typing import Any

# Quantity suffixes, as Kubernetes writes them.
_CPU_SUFFIX = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0}
_MEMORY_SUFFIX = {
    "": 1,
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
}

_QUANTITY = re.compile(r"^([0-9.]+)([A-Za-z]*)$")


def parse_cpu(value: str) -> float | None:
    """A CPU quantity in cores. `250m` → 0.25, `120500000n` → 0.1205."""
    match = _QUANTITY.match(str(value).strip())
    if not match:
        return None
    number, suffix = match.groups()
    scale = _CPU_SUFFIX.get(suffix)
    if scale is None:
        return None
    try:
        return float(number) * scale
    except ValueError:
        return None


def parse_memory(value: str) -> int | None:
    """A memory quantity in bytes. `1443Mi` → 1513095168."""
    match = _QUANTITY.match(str(value).strip())
    if not match:
        return None
    number, suffix = match.groups()
    scale = _MEMORY_SUFFIX.get(suffix)
    if scale is None:
        return None
    try:
        return int(float(number) * scale)
    except ValueError:
        return None


def format_cpu(cores: float | None) -> str:
    """Back to kubectl's display form, so the console shows what it always did."""
    if cores is None:
        return ""
    return f"{round(cores * 1000)}m"


def format_memory(byte_count: int | None) -> str:
    if byte_count is None:
        return ""
    return f"{round(byte_count / (1024 * 1024))}Mi"


def percent(used: float | None, available: float | None) -> int | None:
    if used is None or not available:
        return None
    return round(used / available * 100)


def node_usage_from_text(lines: list[str]) -> list[dict[str, Any]]:
    """`kubectl top nodes --no-headers` → usage records.

    Columns are `name cpu cpu% memory memory%`. The two percentage columns are
    read past and dropped: they are recomputed from allocatable capacity so the
    local and remote paths cannot disagree.
    """
    records = []
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        records.append(
            {
                "name": parts[0],
                "cpu_cores": parse_cpu(parts[1]),
                "memory_bytes": parse_memory(parts[3]),
            }
        )
    return records


def pod_usage_from_text(lines: list[str]) -> list[dict[str, Any]]:
    """`kubectl top pods -A --no-headers` → usage records.

    Columns are `namespace name cpu memory`.
    """
    records = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        records.append(
            {
                "namespace": parts[0],
                "name": parts[1],
                "cpu_cores": parse_cpu(parts[2]),
                "memory_bytes": parse_memory(parts[3]),
            }
        )
    return records


def node_usage_from_api(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A `NodeMetricsList` from metrics.k8s.io → the same usage records."""
    records = []
    for item in payload.get("items", []) or []:
        usage = item.get("usage", {}) or {}
        records.append(
            {
                "name": item.get("metadata", {}).get("name", "unknown"),
                "cpu_cores": parse_cpu(usage.get("cpu", "")),
                "memory_bytes": parse_memory(usage.get("memory", "")),
            }
        )
    return records


def pod_usage_from_api(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A `PodMetricsList` → usage records, summed across containers.

    `kubectl top pods` reports one row per pod, so the containers are added up
    here to match. Without this a multi-container pod would show only its first
    container remotely and its total locally.
    """
    records = []
    for item in payload.get("items", []) or []:
        metadata = item.get("metadata", {})
        cpu_total = 0.0
        memory_total = 0
        seen = False
        for container in item.get("containers", []) or []:
            usage = container.get("usage", {}) or {}
            cpu = parse_cpu(usage.get("cpu", ""))
            memory = parse_memory(usage.get("memory", ""))
            if cpu is not None:
                cpu_total += cpu
                seen = True
            if memory is not None:
                memory_total += memory
                seen = True
        records.append(
            {
                "namespace": metadata.get("namespace", "default"),
                "name": metadata.get("name", "unknown"),
                # Rounded to nanocores, the resolution the API actually reports.
                # Without this, summing containers accumulates binary float
                # error — 100m + 20m is 0.12000000000000001, not 0.12 — and the
                # agent's record stops being equal to the one kubectl's single
                # pre-summed column produces. The formatted output would still
                # have matched, which is exactly why it is worth fixing here
                # rather than discovering later that only the display agreed.
                "cpu_cores": round(cpu_total, 9) if seen else None,
                "memory_bytes": memory_total if seen else None,
            }
        )
    return records


def allocatable_by_node(node_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Allocatable CPU and memory per node, from raw node objects.

    Allocatable rather than capacity, because that is what `kubectl top`
    divides by and what a scheduler can actually place against.
    """
    table = {}
    for node in node_items:
        allocatable = node.get("status", {}).get("allocatable", {}) or {}
        table[node.get("metadata", {}).get("name", "")] = {
            "cpu_cores": parse_cpu(allocatable.get("cpu", "")),
            "memory_bytes": parse_memory(allocatable.get("memory", "")),
        }
    return table
