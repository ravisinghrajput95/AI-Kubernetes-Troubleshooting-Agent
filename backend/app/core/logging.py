"""Logging configuration.

Every line carries a correlation id, injected by a `patcher` rather than by
each call site. A call site that has to remember to add the id is a call site
that will not, and the lines worth correlating are exactly the ones written by
code that knows nothing about the investigation it serves — a collector timing
out on kubectl, the redactor, the grounding validator.
"""

import sys

from loguru import logger

from app.core.correlation import correlation_id

_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{extra[correlation_id]} | {name}:{function}:{line} - {message}"
)


def _inject_correlation(record: dict) -> None:
    # `setdefault`, not assignment: `logger.bind(correlation_id=…)` at a call
    # site is a deliberate override and must win over the ambient value.
    #
    # This is *only* correct because `logger.configure` below sets no default
    # `correlation_id` in `extra`. It did once, and that made the whole
    # mechanism inert: loguru merges the configured `extra` into every record
    # before the patcher runs, so the key was always present, `setdefault`
    # never fired, and every line in the process logged the placeholder while
    # looking exactly like a working correlation id. Adding a default back here
    # silently reverts this feature — `test_operability.py` pins it by asserting
    # on a real record rather than on the function.
    record["extra"].setdefault("correlation_id", correlation_id())


def configure_logging() -> None:
    logger.remove()
    logger.configure(patcher=_inject_correlation)
    logger.add(
        sys.stdout,
        level="INFO",
        format=_FORMAT,
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
