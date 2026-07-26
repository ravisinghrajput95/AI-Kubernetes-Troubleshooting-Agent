"""Bounded collection on large clusters.

F5 from the 2026-07-26 review: nine unbounded all-namespace reads, executed
concurrently, so peak memory was the sum. Measured at 10k pods: 10.7 MB of
kubectl output per read and 2.7 MB retained in every report.

Two things are bounded here — what the API server assembles per request
(`--chunk-size`), and what this process retains (`max_list_items`). Peak *parse*
memory is not, and cannot be while kubectl assembles the whole list before
writing it; that ceiling needs a streaming client.
"""

import json

import pytest

from app.core.config import settings
from app.kubernetes.kubectl_executor import KubectlExecutor, KubectlResult


def pods(count: int) -> dict:
    return {
        "items": [
            {
                "metadata": {"name": f"svc-{i}", "namespace": f"team-{i % 50}"},
                "spec": {"nodeName": f"node-{i % 20}", "containers": [{"name": "app"}]},
                "status": {"phase": "Running"},
            }
            for i in range(count)
        ]
    }


class RecordingExecutor(KubectlExecutor):
    """Captures the argv the executor builds, and returns a canned payload."""

    def __init__(self, payload: dict, **kwargs):
        super().__init__(**kwargs)
        self.payload = payload
        self.commands: list[list[str]] = []

    def _invoke(self, command, env):
        self.commands.append(command)
        return json.dumps(self.payload), "", 0


@pytest.fixture
def executor(monkeypatch):
    def build(payload: dict) -> KubectlExecutor:
        instance = KubectlExecutor(context="test")

        def fake_run(args, parse_json=False):
            command = ["kubectl", *instance._chunk_args(args), *args]
            instance.executed_commands.append(" ".join(command))
            data, truncated, total = instance._cap_items(dict(payload), command)
            return KubectlResult(command, True, "", "", 0, data, truncated, total)

        instance.run = fake_run  # type: ignore[method-assign]
        return instance

    return build


class TestChunking:
    def test_list_reads_ask_the_api_server_to_page(self):
        executor = KubectlExecutor()
        args = executor._chunk_args(["get", "pods", "-A", "-o", "json"])
        assert args == [f"--chunk-size={settings.kubectl_chunk_size}"]

    def test_named_reads_are_not_chunked(self):
        """A named read returns one object; paging it is meaningless noise."""
        executor = KubectlExecutor()
        assert executor._chunk_args(["get", "pod", "web-0", "-n", "prod", "-o", "json"]) == []

    @pytest.mark.parametrize(
        "args",
        [
            ["logs", "web-0"],
            ["top", "nodes"],
            ["describe", "secret", "creds"],
            ["config", "get-contexts"],
        ],
    )
    def test_non_list_commands_are_not_chunked(self, args):
        assert KubectlExecutor()._chunk_args(args) == []

    def test_an_explicit_chunk_size_is_respected(self):
        executor = KubectlExecutor()
        assert executor._chunk_args(["get", "pods", "--chunk-size=50", "-o", "json"]) == []

    def test_chunking_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "kubectl_chunk_size", 0)
        assert KubectlExecutor()._chunk_args(["get", "pods", "-A"]) == []


class TestItemCapping:
    def test_small_lists_pass_through_untouched(self, executor, monkeypatch):
        monkeypatch.setattr(settings, "max_list_items", 2000)
        result = executor(pods(10)).run(["get", "pods", "-A", "-o", "json"], True)

        assert result.truncated is False
        assert len(result.data["items"]) == 10

    def test_oversized_lists_are_capped(self, executor, monkeypatch):
        monkeypatch.setattr(settings, "max_list_items", 100)
        instance = executor(pods(5000))
        result = instance.run(["get", "pods", "-A", "-o", "json"], True)

        assert result.truncated is True
        assert result.total_items == 5000
        assert len(result.data["items"]) == 100

    def test_truncation_is_recorded_not_silent(self, executor, monkeypatch):
        monkeypatch.setattr(settings, "max_list_items", 100)
        instance = executor(pods(5000))
        instance.run(["get", "pods", "-A", "-o", "json"], True)

        assert len(instance.truncations) == 1
        record = instance.truncations[0]
        assert record["returned"] == 5000
        assert record["retained"] == 100
        assert "get pods" in record["command"]

    def test_capping_can_be_disabled(self, executor, monkeypatch):
        monkeypatch.setattr(settings, "max_list_items", 0)
        result = executor(pods(5000)).run(["get", "pods", "-A", "-o", "json"], True)

        assert result.truncated is False
        assert len(result.data["items"]) == 5000

    def test_non_list_payloads_are_untouched(self, executor, monkeypatch):
        monkeypatch.setattr(settings, "max_list_items", 1)
        single = {"metadata": {"name": "web-0"}, "spec": {}}
        result = executor(single).run(["get", "pod", "web-0", "-o", "json"], True)

        assert result.truncated is False
        assert result.data["metadata"]["name"] == "web-0"

    def test_retained_payload_shrinks_with_the_cap(self, executor, monkeypatch):
        """The cap is what bounds report size, which was the measured problem."""
        monkeypatch.setattr(settings, "max_list_items", 0)
        full = executor(pods(5000)).run(["get", "pods", "-A", "-o", "json"], True)

        monkeypatch.setattr(settings, "max_list_items", 500)
        capped = executor(pods(5000)).run(["get", "pods", "-A", "-o", "json"], True)

        assert len(json.dumps(capped.data)) < len(json.dumps(full.data)) / 5


class TestReporting:
    def test_a_truncated_investigation_says_so(self):
        from app.ai.root_cause_analyzer import RootCauseAnalyzer
        from app.analysis.models import AnalysisResult

        analyzer = RootCauseAnalyzer()
        gaps = analyzer._evidence_gaps(
            {
                "collection_limits": {
                    "truncated": True,
                    "reads": [
                        {
                            "command": "kubectl get pods -A -o json",
                            "returned": 40000,
                            "retained": 2000,
                        }
                    ],
                },
                "logs": {"logs": [{"relevant_lines": ["x"]}]},
                "metrics": {"available": True},
            },
            AnalysisResult(),
        )

        assert any("2000 of 40000" in gap for gap in gaps)
        assert any("larger than this investigation looked" in gap for gap in gaps)
