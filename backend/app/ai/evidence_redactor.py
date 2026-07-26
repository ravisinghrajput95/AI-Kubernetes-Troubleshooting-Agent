import re
from typing import Any

SENSITIVE_KEYWORDS = (
    "authorization",
    "bearer",
    "client-key-data",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
)

# Keyword-shaped credentials: `password=…`, `Authorization: Bearer …`.
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
)

# Shape-based detectors. Real pod logs leak credentials that carry no keyword at
# all — a bare JWT, a connection string, a cloud key, a pasted private key. These
# match the credential's own structure rather than the words around it.
SHAPE_PATTERNS = (
    # JSON Web Token: three base64url segments, header always starts `eyJ`.
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # PEM private key block, including its body.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # AWS access key id and the secret that usually accompanies it.
    re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{12,}\b"),
    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)\S+"),
    # Google API key.
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    # GitHub / Slack / Stripe style prefixed tokens.
    re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk_live_[A-Za-z0-9]{16,})\b"
    ),
    # Credentials embedded in a URL: scheme://user:password@host
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@]+(@)"),
)


class EvidenceRedactor:
    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if self._sensitive_key(key) else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self._redact_string(value)
        return value

    def _sensitive_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(keyword in lowered for keyword in SENSITIVE_KEYWORDS)

    def _redact_string(self, value: str) -> str:
        redacted = value
        for pattern in SENSITIVE_PATTERNS:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        for pattern in SHAPE_PATTERNS:
            # Preserve any capture groups (the label, the URL prefix) so the
            # surrounding text stays diagnostically useful.
            redacted = pattern.sub(self._mask, redacted)
        return redacted

    def _mask(self, match: re.Match[str]) -> str:
        """Replace the secret, keeping any surrounding context that was captured."""
        groups = match.groups()
        if not groups:
            return "[REDACTED]"
        if len(groups) == 1:
            return f"{groups[0]}[REDACTED]"
        return f"{groups[0]}[REDACTED]{groups[1]}"
