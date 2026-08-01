from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller.

    `subject` and `groups` are passed to Kubernetes as impersonation headers, so
    they must be the identity the cluster knows — not an internal user id.
    """

    subject: str
    groups: tuple[str, ...] = ()
    email: str = ""
    auth_method: str = "unknown"
    # The organisation this caller belongs to. Every row they cause to be
    # written is stamped with it, and every row they can read is filtered by
    # it. In a single-tenant deployment everyone shares `default`.
    tenant: str = "default"

    @property
    def anonymous(self) -> bool:
        return self.subject == ANONYMOUS_SUBJECT

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "groups": list(self.groups),
            "email": self.email,
            "auth_method": self.auth_method,
            "tenant": self.tenant,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "Principal | None":
        """Rebuild a caller from its stored form.

        Needed when a worker other than the one that received the request runs
        the investigation: the cluster reads must still be impersonated as the
        original caller, so `groups` has to survive the round trip.
        """
        if not payload:
            return None
        return cls(
            subject=payload.get("subject", ""),
            groups=tuple(payload.get("groups") or ()),
            email=payload.get("email", ""),
            auth_method=payload.get("auth_method", "unknown"),
            # Absent on principals serialised before M6; those belong to the
            # single tenant that existed at the time.
            tenant=payload.get("tenant") or "default",
        )


ANONYMOUS_SUBJECT = "anonymous"

ANONYMOUS = Principal(subject=ANONYMOUS_SUBJECT, auth_method="disabled")


class AuthenticationError(Exception):
    """Raised when a request carries no valid credential."""

    def __init__(self, detail: str = "Not authenticated") -> None:
        super().__init__(detail)
        self.detail = detail


class AuthorizationError(Exception):
    """Raised when an authenticated caller may not access a resource."""

    def __init__(self, detail: str = "Not permitted") -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class TokenRecord:
    """A configured static token and the identity it maps to."""

    token: str
    subject: str
    groups: tuple[str, ...] = field(default=())
    tenant: str = "default"
