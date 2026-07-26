from fastapi import Depends, HTTPException, Request, status

from app.auth.authenticators import Authenticator, build_authenticator
from app.auth.models import AuthenticationError, Principal

_authenticator: Authenticator | None = None


def get_authenticator() -> Authenticator:
    """Process-wide authenticator, built once from configuration."""
    global _authenticator
    if _authenticator is None:
        _authenticator = build_authenticator()
    return _authenticator


def reset_authenticator() -> None:
    """Test seam: forget the cached authenticator."""
    global _authenticator
    _authenticator = None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return None
    return credential.strip()


def require_principal(
    request: Request,
    authenticator: Authenticator = Depends(get_authenticator),
) -> Principal:
    """Authenticate the caller, or reject the request.

    Applied as a router-level dependency so a newly added endpoint is protected
    by default. Forgetting to add it to a route should not be possible.
    """
    try:
        principal = authenticator.authenticate(_bearer(request))
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.detail,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    request.state.principal = principal
    return principal
