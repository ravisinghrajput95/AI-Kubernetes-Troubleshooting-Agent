package collectors

import (
	"strings"
	"testing"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
)

// What the agent *recorded* about a read, as opposed to what it sent.
//
// `EquivalentCommand` is the evidence spine's answer to "how was this fact
// obtained". On an impersonating agent it omitted the identity entirely, while
// the platform's kubeconfig path recorded `--as <caller>` — so the same read
// through the two transports disagreed about whose RBAC produced it, and a
// human running the recorded command by hand read as themselves instead.
//
// Found by `scripts/provider_diff.py` against a live cluster: status clean,
// content clean, and 33 command differences of exactly one shape.

func commandFor(t *testing.T, impersonate bool, actor *agentv1.Impersonation) string {
	t.Helper()
	_, record := captureBoth(t, impersonate, actor)
	return record.GetEquivalentCommand()
}

func TestTheRecordedCommandNamesWhoTheReadRanAs(t *testing.T) {
	command := commandFor(t, true, &agentv1.Impersonation{Username: "alice@acme.com"})

	if !strings.Contains(command, "--as alice@acme.com") {
		t.Errorf("recorded %q, which does not say the read ran as alice@acme.com", command)
	}
	// Still a command someone can run: the identity is added to the read, not
	// substituted for it.
	if !strings.HasPrefix(command, "kubectl get pods") {
		t.Errorf("recorded %q, which is no longer the read it describes", command)
	}
}

func TestGroupsAreRecordedTheWayKubectlTakesThem(t *testing.T) {
	// `--as-group` repeats. Joining them would record a single group literally
	// named "sre,oncall", which is the same mistake the *headers* had to avoid
	// and which matches no binding when a human runs the command.
	command := commandFor(t, true, &agentv1.Impersonation{
		Username: "alice@acme.com",
		Groups:   []string{"sre", "oncall"},
	})

	if !strings.Contains(command, "--as-group sre") || !strings.Contains(command, "--as-group oncall") {
		t.Errorf("recorded %q, which does not carry both groups separately", command)
	}
	if strings.Contains(command, "sre,oncall") {
		t.Errorf("recorded %q, which joined the groups into one", command)
	}
}

// The two controls. Both matter, and the first matters most: recording an
// identity that was never applied is the same false record pointing the other
// way, and it would also hide a real divergence — a non-impersonating agent
// beside an impersonating kubeconfig genuinely reads as two different
// identities, and provider_diff has to go on saying so.

func TestAnAgentThatDoesNotImpersonateRecordsNoIdentity(t *testing.T) {
	command := commandFor(t, false, &agentv1.Impersonation{Username: "alice@acme.com"})

	if strings.Contains(command, "--as") {
		t.Errorf(
			"recorded %q, claiming a read ran as alice@acme.com when this agent "+
				"sent no impersonation headers at all",
			command,
		)
	}
}

func TestAnUnattributedReadRecordsNoIdentity(t *testing.T) {
	command := commandFor(t, true, &agentv1.Impersonation{})

	if strings.Contains(command, "--as") {
		t.Errorf("recorded %q for a read that named nobody", command)
	}
}

// The header and the record must agree, because they are two statements about
// one fact and only their agreement makes either trustworthy.
func TestTheRecordAgreesWithTheHeaderThatWasSent(t *testing.T) {
	for _, impersonate := range []bool{true, false} {
		header, record := captureBoth(
			t, impersonate, &agentv1.Impersonation{Username: "alice@acme.com"},
		)
		sent := header.Get("Impersonate-User")
		recorded := strings.Contains(record.GetEquivalentCommand(), "--as alice@acme.com")

		if (sent != "") != recorded {
			t.Errorf(
				"impersonate=%v: header %q but recorded %q — one of them is lying",
				impersonate, sent, record.GetEquivalentCommand(),
			)
		}
	}
}
