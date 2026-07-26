from typing import Any


class FixRecommendationEngine:
    def recommend(self, investigation: dict[str, Any]) -> dict[str, Any]:
        text = self._evidence_text(investigation)
        pod = self._first_problem_pod(investigation)
        deployment = self._first_deployment(investigation)

        if "database_url" in text or "environment variable" in text or "missing env" in text:
            target = deployment or pod
            return {
                "fix": "Add the missing environment variable to the affected workload and restart the pods.",
                "kubectl_commands": self._deployment_commands(target),
                "prevention": "Validate required environment variables in CI and add startup checks that fail with clear messages.",
                "next_steps": self._safe_next_steps(target),
            }

        if "imagepullbackoff" in text or "errimagepull" in text or "failedpull" in text:
            target = deployment or pod
            return {
                "fix": "Correct the image name, tag, or imagePullSecret for the affected workload.",
                "kubectl_commands": self._image_commands(target),
                "prevention": "Pin valid image tags and verify registry credentials before rollout.",
                "next_steps": self._safe_next_steps(target),
            }

        if "crashloopbackoff" in text or "back-off" in text:
            target = deployment or pod
            return {
                "fix": "Inspect the application startup failure, fix the failing configuration or dependency, then restart the workload.",
                "kubectl_commands": self._restart_commands(target),
                "prevention": "Add readiness probes, startup probes, and deployment smoke tests for startup dependencies.",
                "next_steps": self._safe_next_steps(target),
            }

        if "failedscheduling" in text or "pending" in text:
            return {
                "fix": "Review scheduling constraints, resource requests, taints, tolerations, and available node capacity.",
                "kubectl_commands": [
                    "kubectl describe pod <pod-name> -n <namespace>",
                    "kubectl describe nodes",
                    "kubectl top nodes",
                ],
                "prevention": "Set realistic resource requests and monitor cluster capacity before deploying.",
                "next_steps": [
                    "Confirm whether resource requests, taints, affinity, or node pressure blocked scheduling.",
                    "Scale capacity or adjust scheduling constraints before restarting the workload.",
                ],
            }

        if "failedmount" in text:
            return {
                "fix": "Verify the referenced ConfigMap, Secret, volume, or PersistentVolumeClaim exists and is mountable.",
                "kubectl_commands": [
                    "kubectl describe pod <pod-name> -n <namespace>",
                    "kubectl get configmap,secret,pvc -n <namespace>",
                ],
                "prevention": "Deploy configuration and storage dependencies before rolling out workloads.",
                "next_steps": [
                    "Identify the exact missing or unbound dependency from pod events.",
                    "Create or correct the dependency before restarting the pod.",
                ],
            }

        if "no ready endpoints" in text or "selector may not match" in text:
            return {
                "fix": "Update the Service selector so it matches the labels on healthy pods.",
                "kubectl_commands": [
                    "kubectl describe service <service-name> -n <namespace>",
                    "kubectl get pods -n <namespace> --show-labels",
                    "kubectl edit service <service-name> -n <namespace>",
                ],
                "prevention": "Keep Service selectors and pod labels defined together in reviewable manifests.",
                "next_steps": [
                    "Compare Service selectors with healthy pod labels.",
                    "Patch the Service selector only after confirming the intended backend pods.",
                ],
            }

        return {
            "fix": "Review the collected pod, event, deployment, and networking evidence for the first failing workload.",
            "kubectl_commands": [
                "kubectl get pods -A",
                "kubectl get events -A --sort-by=.lastTimestamp",
                "kubectl get deployments -A",
            ],
            "prevention": "Add health checks, clear rollout alerts, and deployment validation for Kubernetes manifests.",
            "next_steps": [
                "Review warning events and failing workload conditions first.",
                "Collect additional logs or metrics for any workload with unclear evidence.",
            ],
        }

    def _evidence_text(self, investigation: dict[str, Any]) -> str:
        return str(investigation).lower()

    def _first_problem_pod(self, investigation: dict[str, Any]) -> dict[str, Any]:
        pods = investigation.get("pods", {}).get("problematic_pods", [])
        return pods[0] if pods else {}

    def _first_deployment(self, investigation: dict[str, Any]) -> dict[str, Any]:
        deployments = investigation.get("deployments", {}).get("unhealthy_deployments", [])
        return deployments[0] if deployments else {}

    def _deployment_commands(self, target: dict[str, Any]) -> list[str]:
        namespace = target.get("namespace", "<namespace>")
        name = target.get("name", "<deployment-name>")
        if not self._is_deployment(target):
            return [
                f"kubectl describe pod {name} -n {namespace}",
                f"kubectl logs {name} -n {namespace} --tail=120 --all-containers=true",
                f"kubectl get pod {name} -n {namespace} -o jsonpath='{{.metadata.ownerReferences}}'",
            ]

        return [
            f"kubectl edit deployment {name} -n {namespace}",
            f"kubectl rollout restart deployment {name} -n {namespace}",
            f"kubectl rollout status deployment {name} -n {namespace}",
        ]

    def _image_commands(self, target: dict[str, Any]) -> list[str]:
        namespace = target.get("namespace", "<namespace>")
        name = target.get("name", "<deployment-name>")
        if not self._is_deployment(target):
            return [
                f"kubectl describe pod {name} -n {namespace}",
                f"kubectl get pod {name} -n {namespace} -o jsonpath='{{.spec.containers[*].image}}'",
                "kubectl get imagepullsecrets -A",
            ]

        return [
            f"kubectl edit deployment {name} -n {namespace}",
            f"kubectl rollout status deployment {name} -n {namespace}",
        ]

    def _restart_commands(self, target: dict[str, Any]) -> list[str]:
        namespace = target.get("namespace", "<namespace>")
        name = target.get("name", "<deployment-name>")
        if not self._is_deployment(target):
            return [
                f"kubectl logs {name} -n {namespace} --tail=120 --all-containers=true",
                f"kubectl describe pod {name} -n {namespace}",
                f"kubectl get pod {name} -n {namespace} -o jsonpath='{{.metadata.ownerReferences}}'",
            ]

        return [
            f"kubectl rollout restart deployment {name} -n {namespace}",
            f"kubectl rollout status deployment {name} -n {namespace}",
        ]

    def _is_deployment(self, target: dict[str, Any]) -> bool:
        return "desired_replicas" in target or "available_replicas" in target

    def _safe_next_steps(self, target: dict[str, Any]) -> list[str]:
        namespace = target.get("namespace", "<namespace>")
        name = target.get("name", "<workload-name>")
        return [
            f"Confirm {namespace}/{name} is the affected workload before changing manifests.",
            "Run the suggested read-only commands and preserve output for rollback review.",
            "Apply the fix through source-controlled manifests where possible.",
        ]
