# Cluster agent

A small binary, one per customer cluster. Its entire job is to turn an evidence
*request* into an evidence *record*.

It dials **out** to the platform and receives work on that connection. No
customer opens an inbound port into a production cluster, which is the binding
constraint the whole transport is designed around (ADR-004) — not throughput,
and not schema.

## What it does and does not do

Does: Kubernetes API reads, read-only policy enforcement, and the translation
from an evidence *kind* to an API path.

Does not: AI, prompts, reports, scheduling, history, or any investigation logic.
Every capability added here multiplies by the size of the fleet and has to be
upgraded across it (ADR-002).

## Two properties worth understanding before changing anything

**It refuses a kind it does not know.** The platform names a kind of evidence;
it cannot describe an operation. A compromised platform can therefore ask for
evidence this agent already knows how to collect, and nothing else. That is the
security property, not a validation step — see `internal/policy`.

**It reads raw JSON, not typed objects.** `client-go`'s typed structs drop
fields the agent's compiled-in schema does not know and reorder keys on
re-marshal, which would make an agent's evidence differ from the same read
performed locally. Raw reads are byte-identical to what `kubectl -o json`
returns, which is what makes the two paths comparable — and they still avoid
the subprocess-per-call that motivated leaving kubectl behind.

## Status

M4a: the transport, proven end to end. Registration and mTLS identity (ADR-005)
are M4b — today the agent presents a bootstrap token on the stream and the
gateway listens in plaintext, which is only acceptable on a trusted network.
