package identity

import (
	"context"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

// The agent has to run on EKS, GKE, AKS, k3s, kind, OpenShift and whatever a
// customer built themselves. A PersistentVolumeClaim does not: Fargate has no
// EBS, plenty of hardened clusters have no default StorageClass, and a zonal
// volume cannot follow a rescheduled pod. Each of those looks the same from
// outside — a pod stuck in ContainerCreating and an agent that never logged
// anything, because it never started.
//
// These pin the Secret-backed store that replaced it.

func materialFor(t *testing.T) (cert, key, ca []byte) {
	t.Helper()
	cert, key, _ = issue(t, time.Now().Add(-time.Hour), 90*24*time.Hour)
	ca, _, _ = issue(t, time.Now().Add(-time.Hour), 3650*24*time.Hour)
	return cert, key, ca
}

func emptySecret() *corev1.Secret {
	// What the manifest creates: present, so the agent needs no permission to
	// create Secrets, and empty, so it knows it has not enrolled yet.
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "identity", Namespace: "agent"},
		Type:       corev1.SecretTypeOpaque,
	}
}

func TestAnEmptySecretIsNotAnIdentity(t *testing.T) {
	// The distinction the manifest depends on. Treating a pre-created empty
	// Secret as an enrolled identity would send the agent into a reconnect
	// loop with no credential instead of enrolling once.
	client := fake.NewSimpleClientset(emptySecret())
	store := NewSecretStore(client, "agent", "identity")

	if store.Exists() {
		t.Fatal("an empty Secret should not count as an enrolled identity")
	}
}

func TestAMissingSecretIsNotAnIdentity(t *testing.T) {
	store := NewSecretStore(fake.NewSimpleClientset(), "agent", "identity")

	if store.Exists() {
		t.Fatal("a Secret that does not exist should not count as an identity")
	}
}

func TestTheIdentityRoundTripsThroughASecret(t *testing.T) {
	client := fake.NewSimpleClientset(emptySecret())
	store := NewSecretStore(client, "agent", "identity")
	cert, key, ca := materialFor(t)

	if err := store.Save(cert, key, ca); err != nil {
		t.Fatalf("save: %v", err)
	}
	if !store.Exists() {
		t.Fatal("the identity should be present after saving")
	}

	material, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if material.Leaf.Subject.CommonName != "test-cluster" {
		t.Fatalf("unexpected subject %q", material.Leaf.Subject.CommonName)
	}
	if _, err := material.RootPool(); err != nil {
		t.Fatalf("root pool: %v", err)
	}
}

func TestSavingUpdatesRatherThanReplaces(t *testing.T) {
	// The manifest owns the object. Replacing it would discard labels or
	// annotations a customer's tooling put there — a GitOps controller's
	// ownership markers, for instance.
	existing := emptySecret()
	existing.Labels = map[string]string{"app.kubernetes.io/managed-by": "argocd"}
	client := fake.NewSimpleClientset(existing)
	store := NewSecretStore(client, "agent", "identity")

	cert, key, ca := materialFor(t)
	if err := store.Save(cert, key, ca); err != nil {
		t.Fatalf("save: %v", err)
	}

	saved, err := client.CoreV1().Secrets("agent").Get(
		context.Background(), "identity", metav1.GetOptions{},
	)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if saved.Labels["app.kubernetes.io/managed-by"] != "argocd" {
		t.Fatal("saving the identity discarded labels the cluster's tooling owns")
	}
}

func TestRenewalOverwritesTheStoredCertificate(t *testing.T) {
	client := fake.NewSimpleClientset(emptySecret())
	store := NewSecretStore(client, "agent", "identity")

	first, firstKey, ca := materialFor(t)
	if err := store.Save(first, firstKey, ca); err != nil {
		t.Fatalf("save: %v", err)
	}

	second, secondKey, _ := issue(t, time.Now(), 90*24*time.Hour)
	if err := store.Save(second, secondKey, ca); err != nil {
		t.Fatalf("resave: %v", err)
	}

	material, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	reloaded, _ := ParseCertificate(second)
	if material.Leaf.SerialNumber.Cmp(reloaded.SerialNumber) != 0 {
		t.Fatal("the store kept the old certificate after a renewal")
	}
}

func TestSavingCreatesTheSecretWhenTheManifestDidNot(t *testing.T) {
	// `docker run` against a kubeconfig, or any install that skipped the
	// manifest. The in-cluster path pre-creates it so the agent needs no
	// create permission, but the agent should not be unusable without it.
	client := fake.NewSimpleClientset()
	store := NewSecretStore(client, "agent", "identity")

	cert, key, ca := materialFor(t)
	if err := store.Save(cert, key, ca); err != nil {
		t.Fatalf("save: %v", err)
	}
	if !store.Exists() {
		t.Fatal("the identity should be present after creating the Secret")
	}
}

func TestASecretStoreSatisfiesTheStoreInterface(t *testing.T) {
	// Both stores are used interchangeably by enrolment and renewal; the
	// compiler is the right place to assert that.
	var _ Store = NewSecretStore(fake.NewSimpleClientset(), "agent", "identity")
	var _ Store = NewStore(t.TempDir())
}
