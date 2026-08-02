"""Where a finished investigation is announced, and what is allowed to leave.

§3.7's action egress — "ServiceNow, PagerDuty, Slack, Teams, Jira, GitHub,
behind one outbound interface". One interface, deliberately: a signed JSON POST
that any of those can receive directly or through the glue a customer already
has. Vendor-shaped payloads would put six formats in this repository and make
adding a seventh a code change.

Three rules shape what leaves.

**A destination belongs to a tenant.** Announcing acme's incident into globex's
Slack is the same failure M6 spent a milestone preventing, committed on the way
out instead of on the way in. The tenant is fixed in configuration and a
destination only ever receives its own tenant's investigations.

**A summary, never the result.** The stored result is 2.7 MB on a cluster at
the `MAX_LIST_ITEMS` ceiling (`scripts/payload_bench.py`) and is full of
cluster interior — pod names, log lines, config keys. What a ticket needs is
what happened, how bad, and where to read the rest. Everything here is either
derived (status, severity, confidence) or already model-authored prose that
passed grounding; no evidence payload crosses this boundary.

**The URL comes from configuration and nowhere else.** Never from a payload, a
user, or an investigation. An outbound POST to a caller-chosen address is a
request-forgery primitive with the platform's network position behind it.

    NOTIFY_DESTINATIONS=oncall|https://hooks.example.com/x|sh4red|acme|high

**Pipe-delimited, unlike every other list in this configuration**, and that is
forced rather than chosen. A URL contains colons — in `https://`, and again in
`host:8443` — so the colon-separated shape `API_TOKENS` and `EVENT_SOURCES` use
cannot express one unambiguously. The first draft here split on `:` and
silently truncated `https://hooks.example.com:8443/path` to
`https://hooks.example.com`, which would have sent every notification to the
wrong place while parsing without complaint. `|` must be percent-encoded in a
URL, so it cannot occur in one by accident.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from app.tenancy import DEFAULT_TENANT
from app.tenancy.models import require_tenant_id

# Severity ordering, worst first. A destination names a floor and receives
# everything at or above it.
SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "info")


class DestinationError(Exception):
    """A destination configuration that cannot be used."""


@dataclass(frozen=True, slots=True)
class Destination:
    """One place finished investigations are announced to."""

    name: str
    url: str
    secret: str = ""
    tenant: str = DEFAULT_TENANT
    min_severity: str = "info"
    # Terminal states worth announcing. `succeeded` alone by default: a failed
    # collection is the platform's problem and an operator sees it in the
    # console and in `k8sagent_investigations_total`, whereas paging someone
    # about it would train them to ignore this channel.
    outcomes: tuple[str, ...] = field(default=("succeeded",))

    def accepts(self, outcome: str, severity: str) -> bool:
        if outcome not in self.outcomes:
            return False
        try:
            floor = SEVERITY_ORDER.index(self.min_severity)
            actual = SEVERITY_ORDER.index((severity or "info").lower())
        except ValueError:
            # An unrecognised severity is delivered rather than dropped: a
            # notification nobody expected is recoverable, a silently withheld
            # incident is not.
            return True
        return actual <= floor

    def signature(self, body: bytes) -> str:
        """So a receiver can prove this came from the platform.

        Same construction as inbound event ingress, which means an operator who
        has integrated one has integrated both.
        """
        return hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()


def parse_destinations(raw: str) -> list[Destination]:
    """`name|url|secret[|tenant][|min_severity][|outcomes]` entries.

    Separated by newlines or commas. Pipe-delimited *fields* because a URL
    cannot contain a bare `|` but very much can contain `:` — see the module
    docstring for the bug that established this.
    """
    destinations: list[Destination] = []
    entries = [
        entry.strip()
        for line in (raw or "").splitlines()
        for entry in line.split(",")
        if entry.strip()
    ]

    for entry in entries:
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise DestinationError(
                f"NOTIFY_DESTINATIONS entry {entry!r} is not "
                f"'name|url|secret[|tenant][|min_severity][|outcomes]'. Fields are "
                f"pipe-separated because a URL contains colons."
            )

        name, url = parts[0], parts[1]
        if not url.startswith(("http://", "https://")):
            raise DestinationError(
                f"Destination {name!r} must be an http or https URL, not {url!r}."
            )

        secret = parts[2] if len(parts) > 2 else ""
        tenant = parts[3] if len(parts) > 3 and parts[3] else DEFAULT_TENANT
        severity = parts[4].lower() if len(parts) > 4 and parts[4] else "info"
        outcomes = (
            tuple(part for part in parts[5].split("+") if part)
            if len(parts) > 5 and parts[5]
            else ("succeeded",)
        )

        require_tenant_id(tenant)
        if severity not in SEVERITY_ORDER:
            raise DestinationError(
                f"Destination {name!r} names severity {severity!r}; "
                f"expected one of {', '.join(SEVERITY_ORDER)}."
            )

        destinations.append(
            Destination(
                name=name,
                url=url,
                secret=secret,
                tenant=tenant,
                min_severity=severity,
                outcomes=outcomes,
            )
        )

    return destinations


def build_summary(
    investigation_id: str,
    outcome: str,
    investigation: dict[str, Any] | None,
    diagnosis: dict[str, Any] | None,
    console_url: str = "",
) -> dict[str, Any]:
    """What a ticket needs, and nothing that describes the cluster's interior.

    Explicitly assembled field by field rather than filtered from the result:
    a denylist would leak whatever a future collector adds, an allowlist cannot.
    """
    investigation = investigation or {}
    diagnosis = diagnosis or {}
    severity = str((investigation.get("severity") or {}).get("severity") or "info").lower()

    summary = {
        "investigation_id": investigation_id,
        "outcome": outcome,
        "cluster": str(investigation.get("context") or ""),
        "namespace": str((investigation.get("scope") or {}).get("namespace") or ""),
        "severity": severity,
        "health": str((investigation.get("health") or {}).get("status") or ""),
        # Model-authored prose that already passed grounding, or the
        # deterministic fallback's. Never raw evidence.
        "root_cause": str(diagnosis.get("root_cause") or ""),
        "confidence": diagnosis.get("confidence"),
        "ai_generated": bool(diagnosis.get("ai_generated")),
        "affected_workloads": (investigation.get("severity") or {}).get("affected_workloads"),
        # A link, so the ticket points at the evidence instead of carrying it.
        "url": f"{console_url.rstrip('/')}/investigations/{investigation_id}"
        if console_url
        else "",
    }
    return summary


def encode(summary: dict[str, Any]) -> bytes:
    """Canonical bytes, so the signature a receiver checks is the one we made."""
    return json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
