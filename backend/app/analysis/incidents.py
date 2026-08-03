"""Grouping hypotheses into the separate things that are actually wrong.

A root-cause product answers one question, and that is right when a cluster has
one fault. The audit ran nine simultaneous broken workloads through it and got
back a single root cause and a flat list of ten hypotheses ordered by severity —
correct, and not what an operator holding an incident needs, because nothing in
that list said which entries were *the same problem seen twice* and which were
unrelated outages happening at once.

**An incident is a resource, and its hypotheses are the explanations offered
for it.** Grouping is by the workload each hypothesis is *about* — its target,
with a pod normalised to the workload that owns it, so `archiver-6555564c46-9v2cd`
and `archiver` are one thing.

The first attempt grouped on shared signals instead — two hypotheses rest on a
common observation, so they are one story — and it was wrong in a way only real
data showed. A hypothesis cites *every* signal of its supporting types across
the whole scope: on the audit cluster `rollout.stalled` cited 35 signals
spanning every broken workload. Any two hypotheses therefore shared something,
the components collapsed, and fourteen independent faults came back as one
incident containing all ten hypotheses — a grouping that had merged everything
and so distinguished nothing.

The lesson is about the shape of a hypothesis rather than about grouping:
hypotheses are **scope-wide, not per-resource**. Their citations answer "what
evidence bears on this pattern anywhere in scope", which is the right question
for confidence and the wrong one for identity.

What this deliberately does *not* do is join a Service to the pods behind it.
They are separate resources and appear as separate incidents; the link between
them is already `graph.service_backends_failing`, which names the path it
walked. Inventing a second, weaker version of that here would give two answers
to one question.

This is **additive and derived**. Nothing consumes it to decide a root cause;
`selected_hypothesis` still names the single best explanation and every existing
field keeps its meaning. The incident list is a second view over the same
hypotheses, which is why it cannot disagree with them.
"""

import re
from dataclasses import dataclass
from typing import Any

from app.analysis.models import Hypothesis, Severity
from app.evidence.models import ResourceRef


@dataclass(frozen=True)
class Incident:
    """One independent problem, and every hypothesis that explains part of it."""

    id: str
    title: str
    severity: Severity
    confidence: int
    hypotheses: tuple[Hypothesis, ...]

    @property
    def primary(self) -> Hypothesis:
        return self.hypotheses[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": str(self.severity),
            "confidence": self.confidence,
            "primary_hypothesis": self.primary.id,
            "hypotheses": [item.id for item in self.hypotheses],
            "target": self.primary.target.to_dict(),
            # Carried per incident so a reader can act on the second one
            # without re-deriving what the first was missing.
            "missing_evidence": list(self.primary.missing_evidence),
        }


def group_incidents(hypotheses: tuple[Hypothesis, ...]) -> tuple[Incident, ...]:
    """Partition ranked hypotheses into independent incidents, one per workload.

    Input order is preserved: `hypotheses` arrives already ranked by
    `hypothesis_rules.rank()`, so the first hypothesis in each group is that
    incident's best explanation and groups come out in the order their best
    explanations did. No second ranking is applied, deliberately — a different
    order here would contradict `selected_hypothesis` for the top incident.
    """
    grouped: dict[str, list[Hypothesis]] = {}
    for item in hypotheses:
        grouped.setdefault(_workload_key(item.target), []).append(item)

    incidents = []
    for key, members in grouped.items():
        primary = members[0]
        incidents.append(
            Incident(
                id=key,
                title=primary.title,
                severity=primary.severity,
                confidence=primary.confidence,
                hypotheses=tuple(members),
            )
        )
    return tuple(incidents)


# A pod's own suffix is exactly five characters; a ReplicaSet's
# pod-template-hash is six to ten. Both are lowercase alphanumeric — an earlier
# version excluded `c` from the hash class by a typo, which silently left every
# `web-77bd6cbdf4-2c5ln` ungrouped from its own replicas. Matching on shape
# rather than parsing ownerReferences, because at this layer the target carries
# a name and nothing else.
#
# Requiring *both* lengths before stripping two segments is what keeps a
# workload legitimately named `api-gateway-prod` intact: `prod` is not five
# characters, so nothing is removed.
_SUFFIX = re.compile(r"^[a-z0-9]{5}$")
_HASH = re.compile(r"^[a-z0-9]{6,10}$")


def _workload_key(target: ResourceRef) -> str:
    """The resource an incident is about, with pods folded into their workload.

    Three pods of one Deployment failing the same way is one incident, not
    three. Only pod names are folded: a Service, a claim or a node is already
    the thing itself, and stripping segments from those would merge genuinely
    separate resources that happen to share a prefix.
    """
    namespace = target.namespace or "_cluster"
    if target.kind != "Pod":
        return f"{target.kind}/{namespace}/{target.name}"

    parts = target.name.split("-")
    if len(parts) >= 3 and _SUFFIX.match(parts[-1]) and _HASH.match(parts[-2]):
        parts = parts[:-2]
    elif len(parts) >= 2 and _SUFFIX.match(parts[-1]):
        parts = parts[:-1]
    return f"workload/{namespace}/{'-'.join(parts) or target.name}"


def selection_rationale(hypotheses: tuple[Hypothesis, ...]) -> str:
    """Why the leading hypothesis leads, in a sentence.

    Ranking is by severity first and confidence second, so the selected
    explanation can carry *lower* confidence than one listed below it — the
    audit saw a CRITICAL at 90% chosen over a HIGH at 92%. That is deliberate
    and defensible, and it was invisible: a reader saw the higher number not
    win and had no way to know severity was the tiebreak. An unexplained
    ordering costs more credibility than the ordering gains.

    Returns an empty string when the leader is also the most confident, because
    there is nothing to explain in that case and a sentence saying so is noise.
    """
    if not hypotheses:
        return ""

    top = hypotheses[0]
    most_confident = max(hypotheses, key=lambda item: item.confidence)
    if most_confident.confidence <= top.confidence:
        return ""

    return (
        f"'{top.title}' was selected over '{most_confident.title}' despite lower "
        f"confidence ({top.confidence}% against {most_confident.confidence}%), because "
        f"it is ranked {top.severity} rather than {most_confident.severity}. "
        f"Severity is ordered before confidence: a more serious explanation is "
        f"investigated first even when a less serious one is marginally more likely."
    )
