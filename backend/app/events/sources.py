"""Who is allowed to make this platform do work, without being a person.

M9's event ingress. §3.7 asks for "signed webhooks that *trigger*
investigations — this is what turns the product from human-invoked to
autonomous", and the interesting part is not the signature.

**The interesting part is that an alert has no human, and impersonation is what
makes "the platform cannot see more than you can" true rather than
aspirational.** `KubectlExecutor._impersonation_args` returns nothing for an
absent or anonymous principal, so an alert-triggered investigation with no
identity would read as the platform's own service account — quietly obtaining
access no authenticated user could ask for, through the one door that has no
user behind it.

So a source is not a secret, it is an **identity**: a subject and groups that
the investigation is impersonated as, exactly like a person's. The customer
gives that identity its own Kubernetes RBAC and the property holds unchanged.
A source configured without a subject is refused at startup rather than
silently promoted to the service account.

    EVENT_SOURCES=alertmanager:s3cr3t:alerts@acme.com:sre|platform:acme

`name:secret:subject[:groups][:tenant]` — the same shape as `API_TOKENS`, for
the same reason: it is already the shape an operator of this platform has
learned.

**The tenant comes from configuration, never from the payload.** An alert is
attacker-adjacent — anything that can post to a monitoring stack can influence
its labels — so a payload that could name its own tenant would be a
cross-tenant trigger. The source's tenant is fixed when it is configured, which
also means a source can only ever reach clusters its own tenant owns, because
`AgentRegistry` is keyed by `(tenant, cluster)`.
"""

import hashlib
import hmac
from dataclasses import dataclass, field

from app.auth.models import Principal
from app.tenancy import DEFAULT_TENANT
from app.tenancy.models import require_tenant_id

# How far out of date a signed request may be.
#
# A signature makes a body unforgeable, not un-replayable: a captured request
# stays valid forever without this, and replaying it is a work amplifier
# against a platform whose whole cost model is "an investigation reads a
# production cluster". Five minutes is Alertmanager's own retry horizon.
SIGNATURE_TOLERANCE_SECONDS = 300


class EventSourceError(Exception):
    """A source configuration that cannot be used."""


@dataclass(frozen=True, slots=True)
class EventSource:
    """One system allowed to trigger investigations, and who it acts as."""

    name: str
    secret: str
    subject: str
    groups: tuple[str, ...] = field(default=())
    tenant: str = DEFAULT_TENANT

    def principal(self) -> Principal:
        """The identity investigations from this source are impersonated as.

        `auth_method="event"` so an audit line, and a Kubernetes audit line,
        can tell an automated trigger from a person holding the same subject.
        """
        return Principal(
            subject=self.subject,
            groups=self.groups,
            auth_method="event",
            tenant=self.tenant,
        )

    def signature(self, body: bytes, timestamp: str) -> str:
        """The expected signature for this body at this time.

        The timestamp is inside the signed material, not beside it — signing
        only the body would let an attacker replay a captured request with a
        fresh timestamp and keep a valid signature.
        """
        payload = timestamp.encode() + b"." + body
        return hmac.new(self.secret.encode(), payload, hashlib.sha256).hexdigest()

    def verify(self, body: bytes, timestamp: str, provided: str, now: float) -> None:
        """Raise unless this request is authentic and recent."""
        try:
            sent_at = float(timestamp)
        except (TypeError, ValueError) as exc:
            raise EventSourceError("Missing or unusable timestamp") from exc

        if abs(now - sent_at) > SIGNATURE_TOLERANCE_SECONDS:
            raise EventSourceError("Request timestamp is outside the accepted window")

        if not hmac.compare_digest(self.signature(body, timestamp), provided or ""):
            raise EventSourceError("Signature does not match")


def parse_sources(raw: str) -> dict[str, EventSource]:
    """`name:secret:subject[:groups][:tenant]` entries, comma separated.

    Every failure here is a refusal rather than a skip. A source that silently
    did not load is a webhook returning 404 to a monitoring system that will
    log it once and never mention it again.
    """
    sources: dict[str, EventSource] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue

        parts = entry.split(":")
        if len(parts) < 3 or not all(parts[:3]):
            raise EventSourceError(
                f"EVENT_SOURCES entry {entry!r} is not "
                f"'name:secret:subject[:groups][:tenant]'. The subject is required: "
                f"an investigation with no identity runs as the platform's service "
                f"account rather than as the caller, which is exactly the access "
                f"impersonation exists to prevent."
            )

        name, secret, subject = parts[0], parts[1], parts[2]
        groups = tuple(group for group in parts[3].split("|") if group) if len(parts) > 3 else ()
        tenant = parts[4].strip() if len(parts) > 4 and parts[4].strip() else DEFAULT_TENANT
        require_tenant_id(tenant)

        if name in sources:
            raise EventSourceError(f"EVENT_SOURCES defines {name!r} twice")

        sources[name] = EventSource(
            name=name, secret=secret, subject=subject, groups=groups, tenant=tenant
        )

    return sources


_sources: dict[str, EventSource] | None = None


def get_sources() -> dict[str, EventSource]:
    global _sources
    if _sources is None:
        from app.core.config import settings

        _sources = parse_sources(settings.event_sources)
    return _sources


def reset_sources() -> None:
    """Test seam, and the hook startup uses after validating configuration."""
    global _sources
    _sources = None
