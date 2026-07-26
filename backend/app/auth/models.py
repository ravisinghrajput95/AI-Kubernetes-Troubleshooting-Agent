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

    @property
    def anonymous(self) -> bool:
        return self.subject == ANONYMOUS_SUBJECT

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "groups": list(self.groups),
            "email": self.email,
            "auth_method": self.auth_method,
        }


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
