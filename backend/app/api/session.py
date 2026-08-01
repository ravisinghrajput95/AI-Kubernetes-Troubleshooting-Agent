"""Who the caller is, and how to stop being them.

`/health` cannot answer either question: it is deliberately unauthenticated so
container probes can reach it, which means it knows the *mode* but never the
identity. The console was showing "Bearer token" under "This session" as a
result — accurate about the mechanism and silent about the person, which on a
shared deployment is the only part anyone wants.

Sign-out has the same shape of gap. Discarding the console's copy of a token
ends the session locally and leaves the identity provider's session untouched,
so the next sign-in is a silent redirect straight back in. That is fine for a
static API token, which has no session to end, and wrong for OIDC. The provider
publishes an end-session endpoint in its discovery document; this hands it to
the console rather than making the console guess at the issuer's URL shape.
"""

from typing import Any

import httpx
from fastapi import APIRouter, Depends
from loguru import logger

from app.auth.dependencies import require_principal
from app.auth.models import Principal
from app.core.config import settings

router = APIRouter(tags=["session"], dependencies=[Depends(require_principal)])

# Discovery is fetched once and kept. The end-session endpoint changes at the
# cadence of a provider migration, and re-fetching it on every settings page
# view would add a third-party round trip to a page load.
_discovery: dict[str, Any] | None = None


def _end_session_endpoint() -> str:
    """Where to send a browser to end the provider's session, if it says.

    Absent is a normal answer: not every provider implements RP-initiated
    logout, and inventing a URL for one that does not would produce a 404 at
    the exact moment a user is trying to leave.
    """
    global _discovery

    if settings.auth_mode.strip().lower() != "oidc" or not settings.oidc_issuer:
        return ""

    if _discovery is None:
        url = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            response = httpx.get(url, timeout=5.0)
            response.raise_for_status()
            _discovery = response.json()
        except Exception as exc:
            # A provider that cannot be reached must not break the settings
            # page; the console falls back to clearing its own credential.
            logger.warning("Could not read OIDC discovery from {url}: {error}", url=url, error=exc)
            _discovery = {}

    return str(_discovery.get("end_session_endpoint", ""))


def reset_discovery() -> None:
    """Test seam: forget the cached discovery document."""
    global _discovery
    _discovery = None


@router.get("/me")
def describe_session(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    """The signed-in caller, as the console should display them."""
    return {
        "subject": principal.subject,
        "email": principal.email,
        "groups": list(principal.groups),
        "tenant": principal.tenant,
        "auth_method": principal.auth_method,
        "anonymous": principal.anonymous,
        # Empty unless the provider publishes one. The console treats it as
        # "also send the browser here", never as the only step.
        "end_session_url": _end_session_endpoint(),
        # Whether this deployment isolates tenants at all, so the console can
        # show the tenant only where it means something.
        "multi_tenant": settings.multi_tenant,
    }
