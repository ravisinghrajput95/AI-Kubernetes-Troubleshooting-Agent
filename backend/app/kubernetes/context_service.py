from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor


class KubernetesContextService:
    def __init__(self) -> None:
        self.kubectl = KubectlExecutor()

    def list_contexts(self) -> dict[str, Any]:
        current = self.kubectl.run(["config", "current-context"])
        contexts = self.kubectl.run(["config", "get-contexts", "-o", "name"])
        config = self.kubectl.run(["config", "view", "-o", "json"], parse_json=True)

        if not contexts.success:
            return {
                "items": [],
                "current_context": "",
                "error": self._friendly_error(contexts.stderr),
            }

        current_context = current.stdout.strip() if current.success else ""
        context_to_cluster = self._context_cluster_map(config.data if config.success else {})
        items = [
            {
                "name": line.strip(),
                "cluster": context_to_cluster.get(line.strip(), line.strip()),
                "current": line.strip() == current_context,
            }
            for line in contexts.stdout.splitlines()
            if line.strip()
        ]

        return {
            "items": items,
            "current_context": current_context,
            "error": "",
        }

    def _context_cluster_map(self, config: Any) -> dict[str, str]:
        if not isinstance(config, dict):
            return {}

        mapping = {}
        for item in config.get("contexts", []):
            name = item.get("name")
            cluster = item.get("context", {}).get("cluster")
            if name and cluster:
                mapping[name] = cluster

        return mapping

    def _friendly_error(self, error: str) -> str:
        lowered = error.lower()
        if "not found" in lowered:
            return "kubectl is not installed or is not available on PATH."
        if "kubeconfig" in lowered or "no such file" in lowered:
            return "Unable to read kubeconfig. Verify KUBECONFIG_PATH or your default kubeconfig."
        return "Unable to load Kubernetes contexts from kubeconfig."
