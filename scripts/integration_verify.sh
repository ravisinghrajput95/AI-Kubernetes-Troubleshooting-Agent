#!/usr/bin/env bash
#
# Stand the platform up on kind against real dependencies, then assert it works.
#
# This is the harness behind the `integration-verify` CI job, and it is a script
# rather than a list of workflow steps so that the thing CI runs is the thing a
# laptop runs. Every defect this exists to catch was found by a human standing
# something up by hand; a harness that can only be reproduced by pushing to a
# branch would not have caught any of them either.
#
#   scripts/integration_verify.sh              # create, install, assert, destroy
#   scripts/integration_verify.sh --keep       # leave the cluster up afterwards
#   scripts/integration_verify.sh --skip-build # reuse an existing k8s-agent-backend:test
#   scripts/integration_verify.sh --verify-only  # assert against a cluster already up
#
# Every kubectl and helm command pins --kube-context. `current-context` is
# process-global machine state that anything can change underneath a long
# experiment, and during the §21 rolling-upgrade work it silently switched to an
# unrelated live GKE cluster mid-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-k8s-agent-verify}"
KCTX="kind-${CLUSTER_NAME}"
NAMESPACE=k8s-agent
RELEASE=k8s-agent
IMAGE=k8s-agent-backend:test
INGRESS_ADDR="${INGRESS_ADDR:-http://127.0.0.1:8080}"
API_TOKEN="${API_TOKEN:-verify-token}"
API_SUBJECT="ci@example.com"

# Pinned so a harness that passed yesterday is not silently running different
# software today. Bumping these is a reviewed diff.
INGRESS_NGINX_REF="controller-v1.11.3"
METRICS_SERVER_REF="v0.7.2"
PROM_OPERATOR_REF="v0.76.2"

KEEP=0
SKIP_BUILD=0
VERIFY_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --verify-only) VERIFY_ONLY=1; KEEP=1; SKIP_BUILD=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

k() { kubectl --context "$KCTX" "$@"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

cleanup() {
  local code=$?
  if [ "$code" -ne 0 ] && [ "$VERIFY_ONLY" -eq 0 ]; then
    step "FAILED (exit $code) — dumping cluster state"
    k get pods -A -o wide || true
    k -n "$NAMESPACE" describe deploy "$RELEASE" 2>/dev/null | tail -40 || true
    k -n "$NAMESPACE" logs -l app.kubernetes.io/name=k8s-agent --tail=120 --all-containers 2>/dev/null || true
  fi
  if [ "$KEEP" -eq 0 ]; then
    step "deleting cluster $CLUSTER_NAME"
    kind delete cluster --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
  else
    echo "cluster $CLUSTER_NAME left running (--keep); delete with: kind delete cluster --name $CLUSTER_NAME"
  fi
  exit $code
}
trap cleanup EXIT

if [ "$VERIFY_ONLY" -eq 0 ]; then

  if [ "$SKIP_BUILD" -eq 0 ]; then
    step "building $IMAGE"
    docker build -t "$IMAGE" "$REPO_ROOT/backend"
  fi
  docker image inspect "$IMAGE" >/dev/null

  step "creating kind cluster $CLUSTER_NAME"
  if kind get clusters | grep -qx "$CLUSTER_NAME"; then
    kind delete cluster --name "$CLUSTER_NAME"
  fi
  kind create cluster --name "$CLUSTER_NAME" --config "$REPO_ROOT/deploy/verify/kind-cluster.yaml" --wait 120s

  step "loading $IMAGE into the cluster"
  kind load docker-image "$IMAGE" --name "$CLUSTER_NAME"

  step "installing ingress-nginx ($INGRESS_NGINX_REF)"
  k apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_REF}/deploy/static/provider/kind/deploy.yaml"

  step "installing metrics-server ($METRICS_SERVER_REF)"
  k apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_REF}/components.yaml"
  # kind's kubelet serving certificate is self-signed and carries no name the
  # metrics-server trusts; without this it never becomes ready and the HPA
  # reports <unknown> forever.
  k -n kube-system patch deployment metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

  step "installing prometheus-operator ($PROM_OPERATOR_REF)"
  k apply --server-side -f \
    "https://github.com/prometheus-operator/prometheus-operator/releases/download/${PROM_OPERATOR_REF}/bundle.yaml"

  step "waiting for the surrounding infrastructure"
  k wait --namespace ingress-nginx --for=condition=Ready pod \
    --selector=app.kubernetes.io/component=controller --timeout=300s
  k -n kube-system rollout status deployment/metrics-server --timeout=300s
  k -n default rollout status deployment/prometheus-operator --timeout=300s

  step "creating namespaces and out-of-band Postgres and Redis"
  k create namespace "$NAMESPACE" --dry-run=client -o yaml | k apply -f -
  k create namespace monitoring --dry-run=client -o yaml | k apply -f -
  k apply -f "$REPO_ROOT/deploy/verify/dependencies.yaml"
  # Waited for before the chart goes in, not concurrently. The platform exits
  # if it cannot reach Postgres at startup, so installing both at once costs a
  # CrashLoopBackOff backoff that `helm --wait` then sits through — which on a
  # slower runner is the difference between a green job and a timeout that
  # looks like a platform failure.
  k -n "$NAMESPACE" rollout status deployment/postgres --timeout=180s
  k -n "$NAMESPACE" rollout status deployment/redis --timeout=180s

  step "creating the platform's secrets"
  k -n "$NAMESPACE" create secret generic k8s-agent-state \
    --from-literal=DATABASE_URL="postgresql://k8sagent:k8sagent@postgres.${NAMESPACE}.svc:5432/k8sagent" \
    --from-literal=REDIS_URL="redis://redis.${NAMESPACE}.svc:6379/0" \
    --dry-run=client -o yaml | k apply -f -
  k -n "$NAMESPACE" create secret generic k8s-agent-tokens \
    --from-literal=API_TOKENS="${API_TOKEN}:${API_SUBJECT}" \
    --dry-run=client -o yaml | k apply -f -

  # A kubeconfig for the cluster the platform is running in, built from a
  # ServiceAccount token.
  #
  # The ServiceAccount holds `impersonate` and nothing else — reads run as the
  # *caller*, and the caller's own binding is what decides what is visible.
  # That is the §21 defect-1 mechanism, exercised rather than documented: strip
  # the impersonate verb from this ClusterRole and every investigation fails.
  step "granting cluster access and building the kubeconfig secret"
  k apply -f "$REPO_ROOT/deploy/verify/cluster-access.yaml"
  SA_TOKEN="$(k -n "$NAMESPACE" create token k8s-agent-reader --duration=8760h)"
  CA_DATA="$(k -n "$NAMESPACE" get configmap kube-root-ca.crt -o jsonpath='{.data.ca\.crt}' | base64 | tr -d '\n')"
  cat > /tmp/verify-kubeconfig <<EOF
apiVersion: v1
kind: Config
current-context: verify
contexts:
  - name: verify
    context: {cluster: verify, user: verify}
clusters:
  - name: verify
    cluster:
      server: https://kubernetes.default.svc
      certificate-authority-data: ${CA_DATA}
users:
  - name: verify
    user:
      token: ${SA_TOKEN}
EOF
  k -n "$NAMESPACE" create secret generic k8s-agent-kubeconfig \
    --from-file=config=/tmp/verify-kubeconfig --dry-run=client -o yaml | k apply -f -
  rm -f /tmp/verify-kubeconfig

  step "installing Prometheus (restrictive ServiceMonitor selector, on purpose)"
  k apply -f "$REPO_ROOT/deploy/verify/prometheus.yaml"

  step "helm install $RELEASE"
  helm --kube-context "$KCTX" upgrade --install "$RELEASE" "$REPO_ROOT/deploy/helm/k8s-agent" \
    --namespace "$NAMESPACE" \
    --values "$REPO_ROOT/deploy/verify/values.yaml" \
    --wait --timeout 5m

  step "waiting for Prometheus"
  k -n monitoring rollout status statefulset/prometheus-verify --timeout=300s
fi

step "verifying the deployment"
python3 "$REPO_ROOT/scripts/verify_deployment.py" \
  --context "$KCTX" \
  --namespace "$NAMESPACE" \
  --release "$RELEASE" \
  --ingress "$INGRESS_ADDR" \
  --host k8s-agent.local \
  --prometheus-host prometheus.local \
  --token "$API_TOKEN"
