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

SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
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
        return redacted
