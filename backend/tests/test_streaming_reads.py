"""The list read that never holds the list (F5's remaining half).

Every other test of the executor replaces `run` with a fake, so none of them
reaches a subprocess, a pipe or a parser — which is exactly how the spike this
closes went unmeasured for nine milestones. These drive a real `kubectl` on
PATH: a script that prints what the API server would.
"""

import json
import os
import sys
import textwrap

import pytest

from app.core.config import settings
from app.kubernetes.kubectl_executor import KubectlExecutor


@pytest.fixture
def fake_kubectl(tmp_path, monkeypatch):
    """Put a `kubectl` on PATH that prints whatever a test wants."""

    def install(body: str) -> None:
        script = tmp_path / "kubectl"
        script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
        script.chmod(0o755)
        monkeypatch.setitem(os.environ, "PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    return install


def listing(count: int) -> str:
    return f"""
        import json, sys
        pod = {{
            "metadata": {{"name": "web", "namespace": "prod"}},
            "spec": {{"containers": [{{"name": "web", "image": "reg/web:1"}}]}},
            "status": {{"phase": "Running"}},
        }}
        sys.stdout.write('{{"apiVersion": "v1", "kind": "PodList", "items": [')
        for index in range({count}):
            if index:
                sys.stdout.write(",")
            body = dict(pod)
            body["metadata"] = {{**pod["metadata"], "name": f"web-{{index}}"}}
            sys.stdout.write(json.dumps(body))
        sys.stdout.write('], "metadata": {{"resourceVersion": "42"}}}}')
    """


def test_a_list_read_is_capped_as_it_arrives(fake_kubectl, monkeypatch):
    monkeypatch.setattr(settings, "max_list_items", 5)
    fake_kubectl(listing(50))
    executor = KubectlExecutor(context="test")

    result = executor.run(["get", "pods", "-A", "-o", "json"], parse_json=True)

    assert result.success
    assert [item["metadata"]["name"] for item in result.data["items"]][:2] == ["web-0", "web-1"]
    assert len(result.data["items"]) == 5
    assert result.total_items == 50
    assert result.truncated is True
    assert executor.truncations == [
        {"command": result.command and " ".join(result.command), "returned": 50, "retained": 5}
    ]
    # The document around the items survives: it is what says which read this was.
    assert result.data["kind"] == "PodList"
    assert result.data["metadata"] == {"resourceVersion": "42"}


def test_the_text_a_caller_sees_is_the_document_it_was_given(fake_kubectl, monkeypatch):
    """`text` is re-parsed by the collection cache on every serve, so it may
    not disagree with `data` about what this read returned."""
    monkeypatch.setattr(settings, "max_list_items", 3)
    fake_kubectl(listing(20))

    result = KubectlExecutor(context="test").run(["get", "pods", "-o", "json"], parse_json=True)

    assert json.loads(result.stdout) == result.data


def test_a_read_under_the_cap_is_unchanged(fake_kubectl, monkeypatch):
    monkeypatch.setattr(settings, "max_list_items", 100)
    fake_kubectl(listing(4))

    result = KubectlExecutor(context="test").run(["get", "pods", "-o", "json"], parse_json=True)

    assert len(result.data["items"]) == 4
    assert result.truncated is False
    assert result.total_items == 4


def test_a_failed_read_keeps_its_stderr_and_exit_code(fake_kubectl):
    fake_kubectl(
        """
        import sys
        sys.stderr.write("Error from server (Forbidden): pods is forbidden\\n")
        sys.exit(1)
        """
    )

    result = KubectlExecutor(context="test").run(["get", "pods", "-o", "json"], parse_json=True)

    assert result.success is False
    assert result.return_code == 1
    assert "Forbidden" in result.stderr
    assert result.data is None


def test_output_that_is_not_json_is_a_failure_not_a_short_list(fake_kubectl):
    fake_kubectl(
        """
        import sys
        sys.stdout.write('{"items": [{"a": 1}, {"b"')
        """
    )

    result = KubectlExecutor(context="test").run(["get", "pods", "-o", "json"], parse_json=True)

    assert result.success is False
    assert result.data is None


def test_a_command_that_never_finishes_is_killed(fake_kubectl, monkeypatch):
    monkeypatch.setattr(settings, "kubectl_timeout_seconds", 1)
    fake_kubectl(
        """
        import sys, time
        sys.stdout.write('{"items": [')
        sys.stdout.flush()
        time.sleep(30)
        """
    )

    result = KubectlExecutor(context="test").run(["get", "pods", "-o", "json"], parse_json=True)

    assert result.return_code == 124
    assert result.stderr == "kubectl command timed out"


def test_a_noisy_stderr_does_not_deadlock_the_read(fake_kubectl, monkeypatch):
    """A child that fills the stderr pipe while this end reads stdout blocks
    forever unless something drains it."""
    monkeypatch.setattr(settings, "max_list_items", 2)
    fake_kubectl(
        """
        import json, sys
        sys.stderr.write("W" * 1_000_000)
        sys.stdout.write(json.dumps({"kind": "PodList", "items": [{"n": n} for n in range(200)]}))
        """
    )

    result = KubectlExecutor(context="test").run(["get", "pods", "-o", "json"], parse_json=True)

    assert result.success
    assert result.total_items == 200
    assert len(result.stderr) == 1_000_000
