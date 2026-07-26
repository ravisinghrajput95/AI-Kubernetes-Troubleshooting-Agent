"""Read-only command policy for every cluster interaction.

The platform is diagnostic: it must never mutate a cluster it is investigating.
Enforcing that here — rather than by convention in each inspector — means a new
collector cannot accidentally introduce a mutating call.

`classify_command` serves a second purpose: commands *displayed* to an operator
are executed by a human, so the platform must be able to say which of them
change state, and must refuse to vouch for strings it cannot parse.
"""

from enum import StrEnum

READ_ONLY_VERBS = frozenset(
    {
        "api-resources",
        "api-versions",
        "auth",
        "cluster-info",
        "config",
        "describe",
        "events",
        "explain",
        "get",
        "logs",
        "rollout",
        "top",
        "version",
    }
)

# Sub-verbs that are read-only for otherwise mixed commands. `rollout` is the
# important case: `status` and `history` only observe, while `undo`, `restart`
# and `pause` mutate — so the verb cannot be allowed or denied wholesale.
ALLOWED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "auth": frozenset({"can-i", "whoami"}),
    "config": frozenset({"current-context", "get-contexts", "get-clusters", "view"}),
    "rollout": frozenset({"status", "history"}),
}


class CommandClass(StrEnum):
    """How a command string should be presented to an operator."""

    READ_ONLY = "read-only"
    MUTATING = "mutating"
    UNRECOGNISED = "unrecognised"


class UnsafeKubectlCommand(RuntimeError):
    """Raised when a command outside the read-only policy is attempted."""


def classify_command(command: str) -> CommandClass:
    """Classify a command string that will be shown to an operator.

    Anything that is not a recognisable `kubectl` invocation is `UNRECOGNISED`
    rather than assumed safe. Commands reach humans who may run them, so an
    unparseable string is treated as suspicious, not benign.
    """
    text = (command or "").strip()
    if not text:
        return CommandClass.UNRECOGNISED

    # Only inspect the first invocation; pipelines are classified by their head.
    first = text.splitlines()[0].strip()
    if not first.startswith("kubectl "):
        return CommandClass.UNRECOGNISED

    args = [part for part in first[len("kubectl ") :].split() if not part.startswith("-")]
    if not args:
        return CommandClass.UNRECOGNISED

    try:
        assert_read_only(args)
    except UnsafeKubectlCommand:
        return CommandClass.MUTATING
    return CommandClass.READ_ONLY


def assert_read_only(args: list[str]) -> None:
    """Validate kubectl arguments against the read-only policy.

    Raises `UnsafeKubectlCommand` for anything not explicitly permitted. This is
    a programming-error guard: the collection scheduler contains the failure and
    records it as failed evidence rather than aborting the investigation.
    """
    if not args:
        raise UnsafeKubectlCommand("Refusing to run kubectl with no arguments")

    verb = args[0]
    if verb not in READ_ONLY_VERBS:
        raise UnsafeKubectlCommand(
            f"kubectl verb '{verb}' is not permitted; this platform is read-only"
        )

    allowed_subcommands = ALLOWED_SUBCOMMANDS.get(verb)
    if allowed_subcommands is None:
        return

    subcommand = args[1] if len(args) > 1 else ""
    if subcommand not in allowed_subcommands:
        attempted = f"{verb} {subcommand}".strip()
        raise UnsafeKubectlCommand(
            f"kubectl '{attempted}' is not permitted; "
            f"allowed: {', '.join(sorted(allowed_subcommands))}"
        )
