"""Pluggable authentication.

An open-source platform cannot assume an identity provider exists, so the
authenticator is selected by configuration:

- `oidc`     — validate a bearer JWT against the provider's JWKS. Production.
- `token`    — shared secrets mapped to identities. Small or air-gapped installs.
- `disabled` — no authentication. Local development only, and refused at startup
               unless explicitly acknowledged, because this service holds a
               kubeconfig.
"""

import hmac
from typing import Protocol

import jwt
from jwt import PyJWKClient
from loguru import logger

from app.auth.models import ANONYMOUS, AuthenticationError, Principal, TokenRecord
from app.core.config import Settings, settings


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

    Configured as `API_TOKENS=tok1:alice@example.com:platform,sre` — comma
    separated entries of `token:subject[:group,group]`.
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
            records.append(TokenRecord(token=parts[0], subject=parts[1], groups=groups))
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
        jwk_client: PyJWKClient | None = None,
    ) -> None:
        if not issuer or not audience:
            raise ValueError("AUTH_MODE=oidc requires OIDC_ISSUER and OIDC_AUDIENCE")

        self.issuer = issuer
        self.audience = audience
        self.username_claim = username_claim
        self.groups_claim = groups_claim
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
        )


def build_authenticator(config: Settings | None = None) -> Authenticator:
    """Select the authenticator for the configured mode.

    A misconfiguration fails at startup rather than silently degrading to no
    authentication — the failure mode that matters here is being open, not
    being closed.
    """
    config = config or settings
    mode = (config.auth_mode or "disabled").strip().lower()

    if mode == "oidc":
        return OIDCAuthenticator(
            issuer=config.oidc_issuer,
            audience=config.oidc_audience,
            jwks_url=config.oidc_jwks_url,
            username_claim=config.oidc_username_claim,
            groups_claim=config.oidc_groups_claim,
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
