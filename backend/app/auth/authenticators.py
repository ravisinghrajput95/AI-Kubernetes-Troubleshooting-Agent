"""Pluggable authentication.

An open-source platform cannot assume an identity provider exists, so the
authenticator is selected by configuration:

- `oidc`     — validate a bearer JWT against the provider's JWKS. Production.
- `token`    — shared secrets mapped to identities. Small or air-gapped installs.
- `disabled` — no authentication. Local development only, and refused at startup
               unless explicitly acknowledged, because this service holds a
               kubeconfig.

There is no default. An unset `AUTH_MODE` is refused rather than resolved,
because a mode nobody chose is how a deployment ends up open without anyone
having said so.
"""

import hmac
from typing import Protocol

import jwt
from jwt import PyJWKClient
from loguru import logger

from app.auth.models import ANONYMOUS, AuthenticationError, Principal, TokenRecord
from app.core.config import Settings, settings
from app.tenancy import DEFAULT_TENANT
from app.tenancy.models import require_tenant_id


class Authenticator(Protocol):
    mode: str

    def authenticate(self, credential: str | None) -> Principal: ...


class DisabledAuthenticator:
    """No authentication. Every caller is anonymous."""

    mode = "disabled"

    def authenticate(self, credential: str | None) -> Principal:
        return ANONYMOUS


class StaticTokenAuthenticator:
    """Shared bearer tokens mapped to identities.

    Configured as `API_TOKENS=tok1:alice@example.com:platform|sre:acme` —
    comma separated entries of `token:subject[:group|group][:tenant]`.

    The tenant is last and optional so every existing configuration keeps
    working and lands in the default tenant, which is what a single-tenant
    deployment wants anyway.
    """

    mode = "token"

    def __init__(self, records: list[TokenRecord]) -> None:
        if not records:
            raise ValueError("AUTH_MODE=token requires API_TOKENS to define at least one token")
        self._records = records

    @classmethod
    def from_config(cls, raw: str) -> "StaticTokenAuthenticator":
        records = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValueError("API_TOKENS entries must be 'token:subject[:group|group]'")
            groups = (
                tuple(group for group in parts[2].split("|") if group) if len(parts) > 2 else ()
            )
            tenant = parts[3].strip() if len(parts) > 3 and parts[3].strip() else DEFAULT_TENANT
            require_tenant_id(tenant)
            records.append(
                TokenRecord(token=parts[0], subject=parts[1], groups=groups, tenant=tenant)
            )
        return cls(records)

    def authenticate(self, credential: str | None) -> Principal:
        if not credential:
            raise AuthenticationError("Missing bearer token")

        # Constant-time comparison against every configured token, so a failure
        # does not leak which prefix matched via timing.
        matched: TokenRecord | None = None
        for record in self._records:
            if hmac.compare_digest(record.token, credential):
                matched = record

        if matched is None:
            raise AuthenticationError("Invalid bearer token")

        return Principal(
            subject=matched.subject,
            groups=matched.groups,
            auth_method=self.mode,
            tenant=matched.tenant,
        )


class OIDCAuthenticator:
    """Validates a bearer JWT against the provider's published JWKS.

    Signature, issuer, audience and expiry are all verified. The username and
    groups claims are configurable because providers disagree on their names.
    """

    mode = "oidc"

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        username_claim: str = "email",
        groups_claim: str = "groups",
        tenant_claim: str = "",
        jwk_client: PyJWKClient | None = None,
    ) -> None:
        if not issuer or not audience:
            raise ValueError("AUTH_MODE=oidc requires OIDC_ISSUER and OIDC_AUDIENCE")

        self.issuer = issuer
        self.audience = audience
        self.username_claim = username_claim
        self.groups_claim = groups_claim
        # Empty means every authenticated user belongs to the default tenant,
        # which is the single-tenant deployment. When set, the claim is
        # authoritative: a token issued by the provider decides the tenant, and
        # nothing in the request can override it.
        self.tenant_claim = tenant_claim
        # Cached and refreshed by PyJWKClient; a key rotation does not require
        # a restart.
        self._jwks = jwk_client or PyJWKClient(
            jwks_url or f"{issuer.rstrip('/')}/.well-known/jwks.json",
            cache_keys=True,
        )

    def authenticate(self, credential: str | None) -> Principal:
        if not credential:
            raise AuthenticationError("Missing bearer token")

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(credential)
            claims = jwt.decode(
                credential,
                signing_key.key,
                algorithms=["RS256", "RS512", "ES256", "ES384"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Token has expired") from exc
        except jwt.InvalidTokenError as exc:
            logger.warning("Rejected OIDC token: {error}", error=str(exc))
            raise AuthenticationError("Invalid token") from exc
        except Exception as exc:  # JWKS fetch failures and similar
            logger.error("OIDC validation failed: {error}", error=str(exc))
            raise AuthenticationError("Token could not be validated") from exc

        subject = claims.get(self.username_claim) or claims.get("sub")
        if not subject:
            raise AuthenticationError("Token carries no usable identity claim")

        raw_groups = claims.get(self.groups_claim) or []
        groups = tuple(str(group) for group in raw_groups) if isinstance(raw_groups, list) else ()

        return Principal(
            subject=str(subject),
            groups=groups,
            email=str(claims.get("email", "")),
            auth_method=self.mode,
            tenant=self._tenant(claims),
        )

    def _tenant(self, claims: dict) -> str:
        """The tenant this token asserts, validated.

        A malformed claim is a rejected login rather than a fallback to the
        default tenant: silently placing a user from an unrecognised
        organisation into the shared default is precisely the mistake this
        milestone exists to prevent.
        """
        if not self.tenant_claim:
            return DEFAULT_TENANT

        raw = str(claims.get(self.tenant_claim, "")).strip()
        if not raw:
            raise AuthenticationError(
                f"Token carries no {self.tenant_claim!r} claim, so the tenant is unknown."
            )
        try:
            return require_tenant_id(raw)
        except Exception as exc:
            raise AuthenticationError("Token carries an unusable tenant claim.") from exc


def build_authenticator(config: Settings | None = None) -> Authenticator:
    """Select the authenticator for the configured mode.

    A misconfiguration fails at startup rather than silently degrading to no
    authentication — the failure mode that matters here is being open, not
    being closed.
    """
    config = config or settings
    mode = (config.auth_mode or "").strip().lower()

    # Unset is not a mode. It used to resolve to `disabled`, which meant
    # `ALLOW_INSECURE_NO_AUTH=true` alone was enough to serve every endpoint
    # unauthenticated — the acknowledgement selecting the mode as a side
    # effect of acknowledging it. It also meant an `AUTH_MODE` that never
    # arrived chose the insecure mode rather than saying so.
    if not mode:
        raise ValueError(
            "AUTH_MODE is not set and this service holds a kubeconfig, so it "
            "will not choose for you. Set AUTH_MODE=oidc (bearer tokens "
            "validated against your identity provider), AUTH_MODE=token "
            "(static bearer tokens in API_TOKENS), or AUTH_MODE=disabled "
            "together with ALLOW_INSECURE_NO_AUTH=true for local development "
            "only. Read SECURITY.md before exposing this beyond localhost."
        )

    if mode == "oidc":
        return OIDCAuthenticator(
            issuer=config.oidc_issuer,
            audience=config.oidc_audience,
            jwks_url=config.oidc_jwks_url,
            username_claim=config.oidc_username_claim,
            groups_claim=config.oidc_groups_claim,
            tenant_claim=config.oidc_tenant_claim,
        )

    if mode == "token":
        return StaticTokenAuthenticator.from_config(config.api_tokens)

    if mode == "disabled":
        if not config.allow_insecure_no_auth:
            raise ValueError(
                "AUTH_MODE=disabled leaves every endpoint unauthenticated while this "
                "service holds a kubeconfig. Set ALLOW_INSECURE_NO_AUTH=true to "
                "acknowledge this for local development, or configure AUTH_MODE=oidc "
                "or AUTH_MODE=token."
            )
        logger.warning(
            "AUTHENTICATION IS DISABLED. Every endpoint is open and this process "
            "can read whatever its kubeconfig can reach. Never expose this beyond "
            "localhost."
        )
        return DisabledAuthenticator()

    raise ValueError(f"Unknown AUTH_MODE '{mode}'; expected oidc, token or disabled")
