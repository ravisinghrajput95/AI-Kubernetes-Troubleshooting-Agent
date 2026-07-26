import pytest

from app.kubernetes.command_policy import UnsafeKubectlCommand, assert_read_only


@pytest.mark.parametrize(
    "args",
    [
        ["get", "pods", "-A", "-o", "json"],
        ["logs", "web-0", "-n", "prod", "--tail=120"],
        ["top", "nodes", "--no-headers"],
        ["config", "current-context"],
        ["config", "get-contexts", "-o", "name"],
        ["config", "view", "-o", "json"],
        ["describe", "pod", "web-0"],
        ["auth", "can-i", "get", "pods"],
        ["rollout", "status", "deployment", "web"],
        ["rollout", "history", "deployment", "web"],
    ],
)
def test_permits_read_only_commands(args):
    assert_read_only(args)


@pytest.mark.parametrize(
    "args",
    [
        ["rollout", "undo", "deployment/web"],
        ["rollout", "restart", "deployment/web"],
        ["rollout", "pause", "deployment/web"],
    ],
)
def test_rejects_mutating_rollout_subcommands(args):
    """`rollout` is mixed: observing is safe, changing is not."""
    with pytest.raises(UnsafeKubectlCommand):
        assert_read_only(args)


@pytest.mark.parametrize(
    "args",
    [
        ["delete", "pod", "web-0"],
        ["apply", "-f", "manifest.yaml"],
        ["patch", "deployment", "web"],
        ["scale", "deployment", "web", "--replicas=0"],
        ["exec", "web-0", "--", "sh"],
        ["drain", "node-1"],
        ["rollout", "undo", "deployment/web"],
        [],
    ],
)
def test_rejects_mutating_commands(args):
    with pytest.raises(UnsafeKubectlCommand):
        assert_read_only(args)


def test_rejects_mutating_subcommand_of_allowed_verb():
    with pytest.raises(UnsafeKubectlCommand):
        assert_read_only(["config", "set-context", "prod"])
