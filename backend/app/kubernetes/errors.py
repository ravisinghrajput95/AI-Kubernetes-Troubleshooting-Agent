"""Shared classification of kubectl failures.

Both the user-facing health summary and the evidence layer need to interpret
kubectl stderr. Keeping the mapping here avoids two divergent copies.
"""

from app.evidence.models import EvidenceStatus

_FRIENDLY_MESSAGES: tuple[tuple[tuple[str, ...], EvidenceStatus, str], ...] = (
    (
        ("not found on path",),
        EvidenceStatus.UNAVAILABLE,
        "kubectl is not installed or is not available on PATH.",
    ),
    (
        ("timed out",),
        EvidenceStatus.TIMEOUT,
        "Kubernetes investigation timed out. Verify cluster connectivity and try again.",
    ),
    (
        ("connection refused", "unable to connect"),
        EvidenceStatus.UNAVAILABLE,
        "Unable to connect to Kubernetes cluster. Verify kubeconfig, cluster access, "
        "and kubectl permissions.",
    ),
    (
        ("forbidden", "unauthorized"),
        EvidenceStatus.FORBIDDEN,
        "kubectl authentication failed. Verify your Kubernetes credentials and permissions.",
    ),
    (
        ("no such file", "kubeconfig"),
        EvidenceStatus.UNAVAILABLE,
        "Kubernetes kubeconfig could not be read. Verify KUBECONFIG_PATH or your "
        "default kubeconfig.",
    ),
)

_DEFAULT_MESSAGE = (
    "Kubernetes investigation failed. Verify kubeconfig, cluster access, and kubectl permissions."
)


def classify_error(error: str) -> tuple[EvidenceStatus, str]:
    """Map raw kubectl stderr to an evidence status and a user-facing message."""
    lowered = (error or "").lower()
    for needles, status, message in _FRIENDLY_MESSAGES:
        if any(needle in lowered for needle in needles):
            return status, message
    return EvidenceStatus.FAILED, _DEFAULT_MESSAGE


_FRIENDLY_TEXTS = frozenset([message for _, _, message in _FRIENDLY_MESSAGES] + [_DEFAULT_MESSAGE])


def friendly_error(error: str) -> str:
    """User-facing message for a kubectl failure.

    Idempotent: an already-translated message is returned unchanged, so it is
    safe to call on values that may have been classified upstream.
    """
    if error in _FRIENDLY_TEXTS:
        return error
    return classify_error(error)[1]
