"""What a tenant is."""

import re
from dataclasses import dataclass


class TenantError(Exception):
    """A tenant identifier that cannot be used."""


# The same shape rule as a cluster id, for the same reason: this value becomes
# a database key, a certificate subject path, a Redis key prefix and a log
# field. A value carrying `/`, whitespace or a quote could name one tenant in
# one place and another somewhere else.
TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def valid_tenant_id(tenant_id: str) -> bool:
    return bool(TENANT_ID.match(tenant_id or ""))


def require_tenant_id(tenant_id: str) -> str:
    if not valid_tenant_id(tenant_id):
        raise TenantError(
            f"{tenant_id!r} is not a usable tenant id: lowercase letters, "
            f"digits and dashes only, up to 63 characters."
        )
    return tenant_id


@dataclass(frozen=True, slots=True)
class Tenant:
    """An organisation whose data is isolated from every other tenant's."""

    id: str
    name: str = ""

    def __post_init__(self) -> None:
        require_tenant_id(self.id)

    @property
    def display_name(self) -> str:
        return self.name or self.id
