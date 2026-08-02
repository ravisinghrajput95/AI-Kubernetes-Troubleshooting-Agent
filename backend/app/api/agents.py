"""Onboarding a cluster, from the console.

Until now the only way to connect a cluster was `python -m app.agentctl` on the
platform host plus a hand-copied binary. That is a defensible answer for one
operator and a bad one for a fleet, and it left the console with no story at
all for "add a cluster".

M4b kept token minting off the HTTP surface on purpose: an unauthenticated
endpoint that mints cluster-enrolment credentials is worse than the problem it
solves. That reasoning has not changed — it is the *precondition* that changed.
These endpoints sit behind the same `require_principal` dependency as the rest
of the API, and refuse to mint anything at all when the platform is running
with authentication disabled. Local development still has the CLI, which is
protected by whatever protects shell access.

The manifest this returns is generated rather than templated on disk, because
it has to carry a token that exists only in the response that produced it.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from app.audit.logger import get_audit_log
from app.auth.dependencies import require_principal
from app.auth.models import Principal
from app.authz.dependencies import require_permission
from app.core.config import settings
from app.security.identity import IdentityError, require_cluster_id

# Router level, so a new agent endpoint is authorised by default. Minting an
# enrolment token now needs `cluster.enrol` (admin and owner) on top of the
# authentication refusal below, which is about the deployment rather than the
# caller and therefore stays.
router = APIRouter(tags=["agents"], dependencies=[Depends(require_permission)])


class EnrolmentRequest(BaseModel):
    cluster_id: str = Field(min_length=1, max_length=127)
    ttl_minutes: int = Field(default=60, ge=1, le=24 * 60)


def _require_gateway() -> None:
    if not settings.agent_gateway_enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "No agent gateway is running. Set AGENT_GATEWAY_PORT to accept "
                "agent connections, then reload."
            ),
        )


def _require_authentication(principal: Principal) -> None:
    """Refuse to mint enrolment credentials on an open platform.

    The credential this endpoint produces enrols a cluster into the fleet. On a
    backend running with AUTH_MODE=disabled, anyone who can reach the port
    could mint one, which is a materially worse exposure than the read access
    that mode already concedes.
    """
    if principal.anonymous or settings.auth_mode == "disabled":
        raise HTTPException(
            status_code=403,
            detail=(
                "Enrolment tokens cannot be minted while authentication is "
                "disabled. Set AUTH_MODE=token or AUTH_MODE=oidc, or use "
                "`python -m app.agentctl issue-token` on the platform host."
            ),
        )


@router.get("/agents")
def list_agents(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    """Agents attached to this worker, and what they can do."""
    from app.api.investigate import connected_agents

    agents = connected_agents()
    return {
        "items": agents,
        "gateway_enabled": settings.agent_gateway_enabled,
        "trust_domain": settings.agent_trust_domain,
        # Named so the console can be honest: a fleet spread across workers
        # shows only this worker's agents until M8 routes by stream ownership.
        "scope": "worker",
    }


@router.get("/agents/ca")
def agent_ca(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    """The CA bundle an agent must trust to verify this gateway."""
    _require_gateway()

    from app.gateway.server import build_identity_service

    service = build_identity_service()
    return {
        "trust_domain": service.trust_domain,
        "ca_bundle": service.ca_bundle_pem.decode("utf-8"),
    }


@router.post("/agents/enrolment", status_code=201)
def create_enrolment(
    request: EnrolmentRequest,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Mint a single-use bootstrap token and the manifest that uses it.

    The token is returned exactly once — the platform stores only its SHA-256
    digest and cannot show it again.
    """
    _require_gateway()
    _require_authentication(principal)

    try:
        cluster_id = require_cluster_id(request.cluster_id.strip())
    except IdentityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from datetime import timedelta

    from app.gateway.server import build_identity_service
    from app.security.enrolment import get_enrolment_store

    service = build_identity_service()
    token = get_enrolment_store().issue_token(cluster_id, timedelta(minutes=request.ttl_minutes))

    get_audit_log().record_action("agents.enrolment.create", principal, target=cluster_id)
    logger.info(
        "Minted an enrolment token for cluster {cluster} on behalf of {subject}",
        cluster=cluster_id,
        subject=principal.subject,
    )

    ca_bundle = service.ca_bundle_pem.decode("utf-8")
    endpoints = _endpoints()

    return {
        "cluster_id": cluster_id,
        "token": token,
        "expires_in_minutes": request.ttl_minutes,
        "ca_bundle": ca_bundle,
        **endpoints,
        "manifest": _manifest(cluster_id, token, ca_bundle, endpoints),
        "docker_command": _docker_command(cluster_id, token, endpoints),
    }


def _endpoints() -> dict[str, str]:
    host = settings.agent_gateway_advertise or f"<platform-host>:{settings.agent_gateway_port}"
    enrolment_port = settings.agent_enrolment_port or settings.agent_gateway_port + 1
    enrolment_host = host.rsplit(":", 1)[0] if ":" in host else host
    return {
        "gateway_endpoint": host,
        "enrolment_endpoint": f"{enrolment_host}:{enrolment_port}",
    }


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.strip().splitlines())


def _manifest(cluster_id: str, token: str, ca_bundle: str, endpoints: dict[str, str]) -> str:
    """A complete, apply-able install for one cluster.

    Read-only by construction on the cluster side as well as the agent's own:
    the ClusterRole grants `get`/`list`/`watch` and nothing else, so even a
    compromised agent binary could not mutate the cluster it runs in. That is
    the same argument the agent's own kind allowlist makes, made again where
    the customer can verify it with `kubectl describe clusterrole`.
    """
    return f"""# Connects "{cluster_id}" to the AI Kubernetes Ops platform.
#
# The token below is single-use and enrols this cluster once. The agent
# generates its own private key inside the cluster and never transmits it;
# what it sends is a certificate signing request.
#
#   kubectl apply -f this-file.yaml
#
apiVersion: v1
kind: Namespace
metadata:
  name: k8s-ops-agent
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: k8s-ops-agent
  namespace: k8s-ops-agent
---
# Read-only, and verifiable as such: no create, update, patch or delete.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8s-ops-agent-read
rules:
  - apiGroups: [""]
    resources:
      - pods
      - pods/log
      - events
      - services
      - endpoints
      - configmaps
      - persistentvolumeclaims
      - persistentvolumes
      - namespaces
      - nodes
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "daemonsets", "replicasets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses", "networkpolicies"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes", "pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: k8s-ops-agent-read
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: k8s-ops-agent-read
subjects:
  - kind: ServiceAccount
    name: k8s-ops-agent
    namespace: k8s-ops-agent
---
apiVersion: v1
kind: Secret
metadata:
  name: k8s-ops-agent-bootstrap
  namespace: k8s-ops-agent
type: Opaque
stringData:
  # Single use. Spent the first time the agent registers; the certificate it
  # receives is what authenticates it from then on.
  bootstrap-token: "{token}"
  ca.crt: |
{_indent(ca_bundle, 4)}
---
# The agent's own credential lives here. Created empty so the agent needs no
# permission to create Secrets — only to update this one.
#
# A Secret rather than a PersistentVolumeClaim, because a claim does not work
# everywhere clusters actually are: EKS on Fargate has no EBS and a
# ReadWriteOnce claim never binds, a cluster with no default StorageClass does
# the same, and a zonal volume cannot follow a rescheduled pod. Every one of
# those failures looks like a pod stuck in ContainerCreating with nothing in
# the agent's logs, because the agent never starts. Every conformant cluster
# has the Secret API.
apiVersion: v1
kind: Secret
metadata:
  name: k8s-ops-agent-identity
  namespace: k8s-ops-agent
type: Opaque
---
# Namespaced, and scoped to one object by name.
#
# The ClusterRole above stays get/list/watch — `kubectl describe clusterrole
# k8s-ops-agent-read` still shows a role that cannot mutate anything in your
# cluster. This Role lets the agent write exactly one thing: its own
# certificate, in its own namespace, and nothing else.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: k8s-ops-agent-identity
  namespace: k8s-ops-agent
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["k8s-ops-agent-identity"]
    verbs: ["get", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: k8s-ops-agent-identity
  namespace: k8s-ops-agent
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: k8s-ops-agent-identity
subjects:
  - kind: ServiceAccount
    name: k8s-ops-agent
    namespace: k8s-ops-agent
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: k8s-ops-agent
  namespace: k8s-ops-agent
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: k8s-ops-agent
  template:
    metadata:
      labels:
        app: k8s-ops-agent
    spec:
      serviceAccountName: k8s-ops-agent
      # Satisfies the restricted Pod Security Standard, which GKE Autopilot,
      # OpenShift and any cluster enforcing `restricted` will reject a pod
      # without. Nothing here is optional on those platforms.
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: agent
          image: {settings.agent_image}
          args:
            - "--cluster={cluster_id}"
            - "--gateway={endpoints["gateway_endpoint"]}"
            - "--enrol={endpoints["enrolment_endpoint"]}"
            - "--ca-file=/etc/k8s-ops-agent/ca.crt"
          env:
            - name: AGENT_BOOTSTRAP_TOKEN
              valueFrom:
                secretKeyRef:
                  name: k8s-ops-agent-bootstrap
                  key: bootstrap-token
            # Names the Secret the agent keeps its identity in. Without a
            # writable volume — and there is deliberately none — this is the
            # only place it can persist one.
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
            seccompProfile:
              type: RuntimeDefault
          resources:
            requests:
              cpu: 25m
              memory: 64Mi
            limits:
              memory: 256Mi
          volumeMounts:
            - name: bootstrap
              mountPath: /etc/k8s-ops-agent
              readOnly: true
      volumes:
        - name: bootstrap
          secret:
            secretName: k8s-ops-agent-bootstrap
"""


def _docker_command(cluster_id: str, token: str, endpoints: dict[str, str]) -> str:
    """For a cluster you can already reach with a kubeconfig, or a laptop."""
    return (
        "docker run --rm \\\n"
        "  -v $HOME/.kube/config:/kubeconfig:ro \\\n"
        "  -v k8s-ops-agent-identity:/var/lib/k8s-ops-agent \\\n"
        "  -e KUBECONFIG=/kubeconfig \\\n"
        f"  {settings.agent_image} \\\n"
        f"  --cluster={cluster_id} \\\n"
        f"  --gateway={endpoints['gateway_endpoint']} \\\n"
        f"  --enrol={endpoints['enrolment_endpoint']} \\\n"
        f"  --bootstrap-token={token} \\\n"
        f"  --identity-dir=/var/lib/k8s-ops-agent"
    )
