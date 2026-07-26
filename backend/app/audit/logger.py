"""Audit trail.

`executed_commands` records what a single investigation ran; it is not an audit
log. This records *who* did *what*, to *which* resource, and whether it
succeeded — the questions a compliance review actually asks.

Events are append-only JSON lines. A separate sink from application logging
keeps the trail intact when log levels change, and makes shipping it to a SIEM a
matter of tailing one file.
"""

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.auth.models import Principal
from app.core.config import settings

_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class AuditEvent:
    action: str
    outcome: str
    actor: str
    actor_groups: tuple[str, ...] = ()
    auth_method: str = ""
    target: str = ""
    source_ip: str = ""
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "action": self.action,
            "outcome": self.outcome,
            "actor": self.actor,
            "actor_groups": list(self.actor_groups),
            "auth_method": self.auth_method,
            "target": self.target,
            "source_ip": self.source_ip,
            "detail": self.detail,
            "metadata": self.metadata,
        }


class AuditLog:
    def __init__(self, path: str | None = None) -> None:
        raw = path if path is not None else settings.audit_log_path
        self.path = Path(raw) if raw else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: AuditEvent) -> None:
        """Write one event. Never raises — losing an audit line must not fail a
        request, but the failure itself is logged loudly."""
        payload = json.dumps(event.to_dict(), separators=(",", ":"))

        if self.path is None:
            logger.bind(audit=True).info("AUDIT {payload}", payload=payload)
            return

        try:
            with _WRITE_LOCK, open(self.path, "a", encoding="utf-8") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            logger.error("Failed to write audit event: {error}", error=str(exc))
            logger.bind(audit=True).info("AUDIT {payload}", payload=payload)

    def record_action(
        self,
        action: str,
        principal: Principal,
        outcome: str = "success",
        target: str = "",
        source_ip: str = "",
        detail: str = "",
        **metadata: Any,
    ) -> None:
        self.record(
            AuditEvent(
                action=action,
                outcome=outcome,
                actor=principal.subject,
                actor_groups=principal.groups,
                auth_method=principal.auth_method,
                target=target,
                source_ip=source_ip,
                detail=detail,
                metadata=metadata,
            )
        )


_audit_log: AuditLog | None = None


def get_audit_log() -> AuditLog:
    global _audit_log
    if _audit_log is None:
        _audit_log = AuditLog()
    return _audit_log


def reset_audit_log() -> None:
    """Test seam."""
    global _audit_log
    _audit_log = None
