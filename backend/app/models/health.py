from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Unauthenticated liveness response.

    `auth_mode` and `insecure` are here so the console can render the sign-in
    the backend actually requires, and can warn when a deployment is accepting
    unauthenticated requests. Neither reveals anything a single unauthenticated
    request would not — the mode is observable from whether that request
    succeeds. No issuer, audience or token material is exposed.
    """

    status: str
    service: str
    auth_mode: str = "disabled"
    insecure: bool = False
