from collections.abc import Sequence
from typing import Any

from app.providers.base import OutputFormat, ProviderResult, ReadVerb, ResourceRequest

LOG_FAILURE_KEYWORDS = (
    "exception",
    "error",
    "failed",
    "failure",
    "connection refused",
    "connection timed out",
    "missing",
    "environment variable",
    "env var",
    "imagepull",
    "back-off",
    "startup",
    # The two words an application most often uses to name its own death, and
    # neither was here: `FATAL: config key DB_HOST is not set` matched nothing.
    "fatal",
    "panic",
    # The same markers `signal_rules.OOM_LOG_MARKERS` looks for, which could only
    # ever see them in lines something else had already matched.
    "oomkilled",
    "out of memory",
    "outofmemory",
    "cannot allocate memory",
)

# Reading every pod's logs on a large broken cluster is its own outage. The
# limit predates M5 and is unchanged; it now bounds a batch rather than a loop.
MAX_PODS = 10


class LogsCollector:
    """Log reads for the pods the pod inspector flagged.

    Unlike the inspectors this fans out over a variable number of targets, so
    its `requests` takes the pods rather than the scope. The pairing is still
    positional: request *i* is `pods[i]`.
    """

    def requests(self, problematic_pods: list[dict[str, Any]]) -> list[ResourceRequest]:
        return [
            ResourceRequest(
                verb=ReadVerb.LOGS,
                name=pod["name"],
                namespace=pod.get("namespace", "default"),
                # Logs are text. `OutputFormat` defaults to JSON, and on the
                # kubeconfig path that default is what decides whether the
                # executor calls `json.loads` on the result — so this read used
                # to fail for exactly the pods that had anything to say, and
                # succeed for the silent ones whose empty output parsed as
                # `{}`. The failure carried no reason, because kubectl had
                # exited 0 with nothing on stderr.
                #
                # `PreviousPodLogsCollector` had it right; the baseline read did
                # not, and the two are the same read. Found by comparing an
                # agent-served investigation against a kubeconfig-served one of
                # the same cluster during a soak — the agent path was
                # unaffected, which is why no test caught it.
                output=OutputFormat.TEXT,
                options={"tail": 120, "all_containers": True},
            )
            for pod in self.targets(problematic_pods)
        ]

    def targets(self, problematic_pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The pods that will actually be read, in request order."""
        return [pod for pod in problematic_pods[:MAX_PODS] if pod.get("name")]

    def analyse(
        self,
        problematic_pods: list[dict[str, Any]],
        results: Sequence[ProviderResult],
    ) -> dict[str, Any]:
        pod_logs = []

        for pod, result in zip(self.targets(problematic_pods), results, strict=True):
            pod_logs.append(
                {
                    "name": pod["name"],
                    "namespace": pod.get("namespace", "default"),
                    "status": pod.get("status"),
                    "success": result.success,
                    "relevant_lines": self._relevant_lines(result.text),
                    "last_lines": [line[:500] for line in result.text.splitlines()[-20:]],
                    "error": result.error if not result.success else "",
                }
            )

        return {
            "checked_pods": len(pod_logs),
            "logs": pod_logs,
        }

    def _relevant_lines(self, logs: str) -> list[str]:
        """Lines that name a failure, and only those.

        When nothing matched this used to return the log's last twenty lines
        under the same key, and everything downstream reads the key as "failure
        lines found": `logs.error_pattern` fired at HIGH quoting
        `"starting checkout service"`, and the confidence engine added twenty
        points for "Pod logs contain relevant failure lines". The tail is still
        kept, as `last_lines`, for a reader — not as a finding.
        """
        return [
            line[:500]
            for line in logs.splitlines()
            if any(keyword in line.lower() for keyword in LOG_FAILURE_KEYWORDS)
        ][:25]
