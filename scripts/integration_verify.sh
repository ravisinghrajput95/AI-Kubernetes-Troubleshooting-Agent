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
ADMIN_TOKEN="${ADMIN_TOKEN:-verify-admin-token}"
ADMIN_SUBJECT="admin@example.com"
AGENT_IMAGE=k8s-agent-agent:test
AGENT_CLUSTER_ID="${AGENT_CLUSTER_ID:-verify-agent}"

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

  # **A completion sentinel, because the exit status alone is not trustworthy.**
  #
  # On bash 3.2 — which macOS ships — a `set -u` violation aborts the script but
  # leaves `$?` at 0 inside the EXIT trap. So this function tore the cluster
  # down and exited **0** on a run that had failed before reaching a single
  # assertion. A harness reporting success on a broken run is the worst defect
  # it can have, and it is invisible: the log said "deleting cluster" and the
  # exit code said fine.
  #
  # Reproduced in five lines rather than reasoned about, then fixed here: only
  # the last line of the happy path sets COMPLETED, so *any* abnormal exit —
  # `set -e`, `set -u`, a signal — reports failure.
  if [ "${COMPLETED:-0}" -ne 1 ] && [ "$code" -eq 0 ]; then
    code=1
    echo "the run did not reach the end but exited 0; reporting failure" >&2
  fi
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
    step "building $IMAGE and $AGENT_IMAGE"
    docker build -t "$IMAGE" "$REPO_ROOT/backend"
    docker build -t "$AGENT_IMAGE" "$REPO_ROOT/agent"
  fi
  docker image inspect "$IMAGE" >/dev/null
  docker image inspect "$AGENT_IMAGE" >/dev/null

  step "creating kind cluster $CLUSTER_NAME"
  if kind get clusters | grep -qx "$CLUSTER_NAME"; then
    kind delete cluster --name "$CLUSTER_NAME"
  fi
  kind create cluster --name "$CLUSTER_NAME" --config "$REPO_ROOT/deploy/verify/kind-cluster.yaml" --wait 120s

  step "loading images into the cluster"
  kind load docker-image "$IMAGE" "$AGENT_IMAGE" --name "$CLUSTER_NAME"

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

  # A CA the whole release shares. Without one each replica generates its own
  # development CA, so an agent enrolled against replica A cannot connect to
  # replica B — and the harness would be exercising a single-replica deployment
  # while claiming a fleet.
  step "minting the agent CA"
  # EC P-256, not RSA. `CertificateAuthority.load` refuses anything else —
  # agents and the gateway both expect P-256 — and it refuses at *startup*, so
  # an RSA key here is a CrashLoopBackOff rather than a confusing handshake
  # failure later. Found by supplying one.
  CA_DIR="$(mktemp -d)"
  openssl ecparam -name prime256v1 -genkey -noout -out "$CA_DIR/ca.key" 2>/dev/null
  openssl req -x509 -new -key "$CA_DIR/ca.key" -days 2 \
    -subj "/CN=k8s-agent-verify-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$CA_DIR/ca.crt" 2>/dev/null
  k -n "$NAMESPACE" create secret generic k8s-agent-ca \
    --from-file=ca.crt="$CA_DIR/ca.crt" --from-file=ca.key="$CA_DIR/ca.key" \
    --dry-run=client -o yaml | k apply -f -
  rm -rf "$CA_DIR"

  step "creating the platform's secrets"
  k -n "$NAMESPACE" create secret generic k8s-agent-state \
    --from-literal=DATABASE_URL="postgresql://k8sagent:k8sagent@postgres.${NAMESPACE}.svc:5432/k8sagent" \
    --from-literal=REDIS_URL="redis://redis.${NAMESPACE}.svc:6379/0" \
    --dry-run=client -o yaml | k apply -f -
  # Two tokens, and the split is deliberate. The harness runs as `operator`,
  # because a caller holding every permission cannot tell a working permission
  # table from an absent one. Enrolling a cluster needs `admin` — M6.5 put fleet
  # mutation there on purpose — so it gets its own subject, granted the role by
  # `rbacctl` after install, and used for exactly one call.
  k -n "$NAMESPACE" create secret generic k8s-agent-tokens \
    --from-literal=API_TOKENS="${API_TOKEN}:${API_SUBJECT},${ADMIN_TOKEN}:${ADMIN_SUBJECT}" \
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

  # Enrol a real agent, through the endpoint a customer actually uses.
  #
  # Deliberately `POST /agents/enrolment` and the manifest it generates, rather
  # than a manifest kept in this repository. A hand-written one would drift from
  # what the platform emits and would verify the harness rather than the
  # product — the same reason the observability fixtures are captured from a
  # real backend instead of written by hand.
  step "granting admin to $ADMIN_SUBJECT so it may enrol a cluster"
  # This was `-o name | head -1`, and it took the job down with exit 141 on a
  # release commit that touched nothing near it.
  #
  # 141 is 128+13, SIGPIPE. `head -1` closes the pipe after the first line, and
  # if the writer is still writing it dies of SIGPIPE — which `set -o pipefail`
  # then promotes to the script's exit status. Measured directly: under
  # pipefail, `seq 1 2000000 | head -1` exits 141 every time and `seq 1 3 |
  # head -1` exits 0 every time. The race is whether the writer finishes before
  # `head` goes away, which is why two replicas' worth of output passed for
  # months and lost once under CI load.
  #
  # That the pipeline is the source is deduction rather than measurement — the
  # step logged its heading and then failed, and this was the only pipeline in
  # it — but the fix costs nothing and removes the only candidate: jsonpath asks
  # the API server for one name and needs no pipe at all.
  POD="$(k -n "$NAMESPACE" get pod -l app.kubernetes.io/name=k8s-agent \
    -o jsonpath='{.items[0].metadata.name}')"
  if [ -z "$POD" ]; then
    echo "no k8s-agent pod to grant through — the release is not running" >&2
    exit 1
  fi
  k -n "$NAMESPACE" exec "$POD" -- \
    python -m app.rbacctl grant --subject "$ADMIN_SUBJECT" --role admin

  step "enrolling cluster $AGENT_CLUSTER_ID"
  MANIFEST="$(mktemp)"
  python3 "$REPO_ROOT/scripts/verify_deployment.py" \
    --context "$KCTX" --namespace "$NAMESPACE" --release "$RELEASE" \
    --ingress "$INGRESS_ADDR" --host k8s-agent.local \
    --prometheus-host prometheus.local --token "$ADMIN_TOKEN" \
    --enrol "$AGENT_CLUSTER_ID" --enrol-out "$MANIFEST"

  # The image is the one thing that must change: the manifest names a published
  # tag and this cluster has a locally built one loaded into it.
  sed -i.bak \
    -e "s#image: .*k8s-ops-agent.*#image: ${AGENT_IMAGE}#" \
    -e "s#image: ${AGENT_IMAGE}#image: ${AGENT_IMAGE}\n          imagePullPolicy: Never#" \
    "$MANIFEST"
  k apply -f "$MANIFEST"
  rm -f "$MANIFEST" "$MANIFEST.bak"

  step "waiting for the agent to connect"
  k -n k8s-ops-agent rollout status deployment/k8s-ops-agent --timeout=300s
fi

# Revoking the agent's certificate is the last thing the run does, and it is
# destructive: a second --verify-only pass against the same cluster would find
# an agent that can no longer serve and report the earlier checks as failures.
# So the local iterate loop leaves the certificate valid; a full run does not.
# A scalar, not an array: macOS ships bash 3.2, where `"${arr[@]}"` on an empty
# array is an *unbound variable* under `set -u`. An empty scalar expands to
# nothing and is safe on both.
SKIP_REVOCATION=""
if [ "$VERIFY_ONLY" -eq 1 ]; then
  SKIP_REVOCATION="--skip-revocation"
fi

# The differential agent suite, against this cluster.
#
# It is the M4 exit criterion — an investigation collected through an agent must
# produce the same evidence as the same read performed locally — and until now
# it ran nowhere: **nothing set `K8S_AGENT_CLUSTER_INTEGRATION`**, not this
# script and not CI, so 36 tests including both certificate-renewal checks
# waited on someone remembering. That is the standing the mutation tests had
# before `scripts/mutation_check.py`, and it cost a real defect: running it by
# hand found the agent reporting an *absent* metrics-server as an empty result,
# which made an uninstalled metrics-server read as an idle cluster.
#
# It belongs here rather than in `verify_deployment.py` for the reason
# `docs/INTEGRATION_VERIFICATION.md` gives: it needs a second product to agree
# with us — a real API server answering a real Go binary — and is only
# observable from outside the process. It runs its own gateway and its own
# agent against this cluster, sharing nothing with the chart-deployed one.
#
# Placed *after* the deployment verification on purpose: those assertions are
# the established ones, and a newer check must not be able to stop them
# reporting.
run_differential_suite() {
  local suite="$REPO_ROOT/backend/tests/test_agent_transport.py"

  # The venv on a laptop, the runner's interpreter in CI. Named rather than
  # guessed, because a `python3` without pytest would *skip* the whole suite
  # and exit 0 — the vacuous pass this file guards against everywhere else.
  local py="python3"
  if [ -x "$REPO_ROOT/backend/.venv/bin/python" ]; then
    py="$REPO_ROOT/backend/.venv/bin/python"
  fi
  if ! "$py" -c "import pytest" >/dev/null 2>&1; then
    echo "the differential suite needs pytest and the backend dependencies." >&2
    echo "  laptop: cd backend && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
    return 1
  fi

  local bin_dir
  bin_dir="$(mktemp -d)"
  (cd "$REPO_ROOT/agent" && go build -o "$bin_dir/k8s-agent" ./cmd/agent)

  local junit="$bin_dir/results.xml"
  (
    cd "$REPO_ROOT/backend"
    K8S_AGENT_CLUSTER_INTEGRATION=1 \
    AGENT_BINARY="$bin_dir/k8s-agent" \
    AGENT_TEST_CONTEXT="$KCTX" \
      "$py" -m pytest -q "$suite" --junitxml="$junit"
  )

  # **The suite skips itself on a missing binary or a missing variable, and a
  # fully-skipped run exits 0.** So the count is checked, not the exit status —
  # the same guard the alert-series and scrape-target checks carry, and the
  # reason `fleet_bench.py` refuses to print a result from a run that asserted
  # nothing.
  python3 - "$junit" <<'PY'
import sys, xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suite = root if root.tag == "testsuite" else root.find("testsuite")
total = int(suite.get("tests", 0))
skipped = int(suite.get("skipped", 0))
ran = total - skipped
print(f"  {ran} of {total} differential tests ran ({skipped} skipped)")
if ran < 25:
    sys.exit(
        f"only {ran} of {total} ran. The suite skips itself when "
        f"K8S_AGENT_CLUSTER_INTEGRATION is unset or AGENT_BINARY is missing, and a "
        f"fully-skipped run exits 0 — which is indistinguishable from a passing one."
    )
PY
  rm -rf "$bin_dir"
}

step "verifying the deployment"
python3 "$REPO_ROOT/scripts/verify_deployment.py" \
  --context "$KCTX" \
  --namespace "$NAMESPACE" \
  --release "$RELEASE" \
  --ingress "$INGRESS_ADDR" \
  --host k8s-agent.local \
  --prometheus-host prometheus.local \
  --token "$API_TOKEN" \
  --agent-cluster "$AGENT_CLUSTER_ID" \
  $SKIP_REVOCATION

step "differential agent suite (a real Go agent against this cluster)"
run_differential_suite

# The happy path ends here, and nothing else sets this. See `cleanup`.
COMPLETED=1
