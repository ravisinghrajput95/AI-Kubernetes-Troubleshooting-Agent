"""Action egress: telling something else that an investigation finished.

`destinations.py` carries what is allowed to leave — a summary, never the
result; a destination belongs to a tenant; the URL comes from configuration and
nowhere else.

`dispatcher.py` carries the rule that a notification can never fail an
investigation.
"""

from app.notify.destinations import (
    SEVERITY_ORDER,
    Destination,
    DestinationError,
    build_summary,
    encode,
    parse_destinations,
)
from app.notify.dispatcher import announce, deliver

_destinations: list[Destination] | None = None


def get_destinations() -> list[Destination]:
    global _destinations
    if _destinations is None:
        from app.core.config import settings

        _destinations = parse_destinations(settings.notify_destinations)
    return _destinations


def reset_destinations() -> None:
    """Test seam, and the hook startup uses after validating configuration."""
    global _destinations
    _destinations = None


__all__ = [
    "SEVERITY_ORDER",
    "Destination",
    "DestinationError",
    "announce",
    "build_summary",
    "deliver",
    "encode",
    "get_destinations",
    "parse_destinations",
    "reset_destinations",
]
