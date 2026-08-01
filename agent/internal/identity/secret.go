package identity

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Keys inside the Secret. Named like files so an operator reading
// `kubectl get secret -o yaml` sees something familiar.
const (
	secretCertKey = "agent.crt"
	secretKeyKey  = "agent.key"
	secretCAKey   = "ca.crt"
)

// SecretStore keeps the agent's identity in a Kubernetes Secret.
//
// This is the default inside a cluster, and the reason is portability rather
// than elegance. The obvious alternative — a PersistentVolumeClaim — does not
// work everywhere a customer's clusters actually are:
//
//   - **EKS on Fargate** has no EBS. A ReadWriteOnce claim never binds, so the
//     pod sits in ContainerCreating for ever with nothing in the agent's logs
//     to explain it, because the agent never starts.
//   - **A cluster with no default StorageClass** does the same thing, and plenty
//     of hardened clusters have none.
//   - **GKE Autopilot** will provision one, but it is a disk and an hourly cost
//     for 64Mi of certificate.
//   - **A rescheduled pod** on a multi-zone cluster can land where a
//     zonal ReadWriteOnce volume cannot follow.
//
// A Secret has none of those failure modes: every conformant cluster has the
// API, it survives restarts and reschedules, it is namespaced and it costs
// nothing. k3s, kind, EKS, GKE, AKS, OpenShift and a Raspberry Pi all behave
// the same way.
//
// **This does not weaken the read-only guarantee.** Evidence collection is
// still incapable of mutation — `ReadVerb` cannot express one and the agent's
// kind allowlist refuses anything it does not recognise. What this needs is a
// *namespace-scoped* Role granting `get` and `update` on exactly one Secret
// name in the agent's own namespace. The ClusterRole that reads the customer's
// cluster stays get/list/watch and nothing else, so
// `kubectl describe clusterrole k8s-ops-agent-read` still tells the truth, and
// the agent's ability to write is bounded to its own credential.
type SecretStore struct {
	client    kubernetes.Interface
	namespace string
	name      string
}

func NewSecretStore(client kubernetes.Interface, namespace, name string) *SecretStore {
	return &SecretStore{client: client, namespace: namespace, name: name}
}

func (s *SecretStore) get(ctx context.Context) (*corev1.Secret, error) {
	return s.client.CoreV1().Secrets(s.namespace).Get(ctx, s.name, metav1.GetOptions{})
}

// Exists reports whether this agent has already enrolled.
//
// A Secret that exists but has no certificate in it is *not* enrolled: the
// manifest creates it empty so the agent needs no `create` permission, and
// treating that as an identity would send the agent into a reconnect loop with
// no credential instead of enrolling once.
func (s *SecretStore) Exists() bool {
	secret, err := s.get(context.Background())
	if err != nil {
		return false
	}
	return len(secret.Data[secretCertKey]) > 0 &&
		len(secret.Data[secretKeyKey]) > 0 &&
		len(secret.Data[secretCAKey]) > 0
}

func (s *SecretStore) Load() (*Material, error) {
	secret, err := s.get(context.Background())
	if err != nil {
		return nil, fmt.Errorf("read secret %s/%s: %w", s.namespace, s.name, err)
	}
	return NewMaterial(
		secret.Data[secretCertKey],
		secret.Data[secretKeyKey],
		secret.Data[secretCAKey],
	)
}

// Save writes the identity back, creating the Secret only if it is absent.
//
// Update rather than create-or-replace: the manifest owns the object, and
// replacing it would discard labels or annotations a customer's tooling put
// there. Create is attempted only for the case where the agent was installed
// without the manifest — `docker run` against a kubeconfig, say.
func (s *SecretStore) Save(certPEM, keyPEM, caPEM []byte) error {
	ctx := context.Background()
	data := map[string][]byte{
		secretCertKey: certPEM,
		secretKeyKey:  keyPEM,
		secretCAKey:   caPEM,
	}

	existing, err := s.get(ctx)
	if apierrors.IsNotFound(err) {
		_, err = s.client.CoreV1().Secrets(s.namespace).Create(ctx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: s.name, Namespace: s.namespace},
			Type:       corev1.SecretTypeOpaque,
			Data:       data,
		}, metav1.CreateOptions{})
		if err != nil {
			return fmt.Errorf("create secret %s/%s: %w", s.namespace, s.name, err)
		}
		return nil
	}
	if err != nil {
		return fmt.Errorf("read secret %s/%s: %w", s.namespace, s.name, err)
	}

	if existing.Data == nil {
		existing.Data = map[string][]byte{}
	}
	for key, value := range data {
		existing.Data[key] = value
	}

	if _, err := s.client.CoreV1().Secrets(s.namespace).Update(
		ctx, existing, metav1.UpdateOptions{},
	); err != nil {
		return fmt.Errorf("update secret %s/%s: %w", s.namespace, s.name, err)
	}
	return nil
}
