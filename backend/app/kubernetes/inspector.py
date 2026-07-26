from app.kubernetes.pod_inspector import PodInspector


def inspect_pods():
    return PodInspector().inspect()


def inspect_nodes():
    return {"healthy": True, "message": "Node inspection is not implemented in this phase"}
