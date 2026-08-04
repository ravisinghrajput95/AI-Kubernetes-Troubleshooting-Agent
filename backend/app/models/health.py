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


class ReadinessResponse(BaseModel):
    """Whether this worker should be sent traffic.

    `checks` names each dependency and whether it answered, so a 503 says which
    one — "not ready" with no detail sends an operator to the logs of a process
    that is, by definition, not serving.
    """

    status: str
    reason: str
    checks: dict[str, str] = {}
