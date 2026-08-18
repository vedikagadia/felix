"""Domain models — the vocabulary the rest of felix speaks in.

Repositories return these dataclasses instead of raw DB tuples, so the service
and CLI layers never index into positional rows (`r[6]`) and never care about
column order. Each memory source has a model; recall results wrap a model with
its vector distance (`Recall`), and the graph wrapper adds a traversal depth
(`GraphHit`). `EvidencePacket` is everything the evidence gatherer assembles for one
alert; `Diagnosis` is what the reasoning layer will produce from it (step 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar


# ── tenancy ──────────────────────────────────────────────────────────────────


@dataclass
class Project:
    """One onboarded project (tenant). Every memory row carries this project's
    `id` (slug); recall is always scoped to one. The built-in demo is 'sample'."""

    id: str
    display_name: str
    source_kind: str = "path"  # 'path' | 'git' | 'builtin'
    source_ref: str | None = None
    created_at: datetime | None = None
    last_synced: datetime | None = None


# ── memory-source records ────────────────────────────────────────────────────


@dataclass
class ResolutionStep:
    step_order: int
    action: str
    command: str | None = None
    outcome: str | None = None


@dataclass
class Incident:
    id: str
    title: str
    symptoms: str
    root_cause: str | None = None
    service: str | None = None
    severity: str | None = None
    tags: list[str] = field(default_factory=list)
    occurred_at: datetime | None = None
    resolution_steps: list[ResolutionStep] = field(default_factory=list)
    # Human feedback on a live-diagnosed incident: "helpful" | "not_helpful" |
    # None (unreviewed). Drives whether the incident is embedded/recallable.
    feedback: str | None = None


@dataclass
class RunbookStep:
    """One step of a curated runbook — field-identical to ResolutionStep, but
    part of authored procedure rather than an incident's recorded resolution."""

    step_order: int
    action: str
    command: str | None = None
    outcome: str | None = None


@dataclass
class Runbook:
    """A curated, reusable playbook recalled by MEANING (vector search on its
    trigger text), distinct from `incidents` (episodic history). Mirrors
    Incident: a parent row embedded on (title + symptoms) + ordered child steps."""

    id: str
    title: str
    symptoms: str  # the trigger text embedded for vector recall
    service: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    steps: list[RunbookStep] = field(default_factory=list)

@dataclass
class DocChunk:
    id: str
    doc_title: str
    heading: str | None
    body: str
    doc_type: str | None = None
    source_path: str | None = None


@dataclass
class CodeChange:
    id: str
    commit_sha: str
    merged_at: datetime
    title: str
    summary: str | None = None
    author: str | None = None
    files_changed: list[str] = field(default_factory=list)
    services_affected: list[str] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)


@dataclass
class CodeNode:
    id: str
    name: str
    kind: str
    file: str | None = None
    service: str | None = None
    source: str | None = None
    summary: str | None = None
    last_commit: str | None = None


@dataclass
class CodeEdge:
    src_id: str
    dst_id: str
    kind: str


# ── recall wrappers ──────────────────────────────────────────────────────────

T = TypeVar("T")


@dataclass
class Recall(Generic[T]):
    """A recalled record plus its L2 distance from the query vector.
    Lower distance = closer match (0 = identical text)."""

    item: T
    distance: float


@dataclass
class GraphHit:
    """A code node reached during a graph traversal, with the shallowest hop
    depth at which it was reached (0 = the start node itself)."""

    node: CodeNode
    depth: int


# ── live-metric health ───────────────────────────────────────────────────────


@dataclass
class NodeHealth:
    """The health of ONE service node against ONE configured check. Records
    which signal breached and the observed value vs. the threshold it crossed."""

    service: str  # the node's service name (metrics.service)
    metric: str  # the metric evaluated (metrics.metric)
    intent: str  # which MetricQueryBuilder intent breached: "p99"|"avg"|"error_rate"|"latest"
    observed: float  # the computed value for `intent` over the window
    threshold: float  # the configured level `observed` is compared against
    breached: bool  # True iff observed >= threshold
    sample_count: int  # how many samples backed `observed` (0 => no data)


# ── assembled evidence + reasoning output ────────────────────────────────────


@dataclass
class EvidencePacket:
    """Everything the evidence gatherer collects for one alert — the input the reasoning
    layer will diagnose over."""

    alert: str
    incidents: list[Recall[Incident]] = field(default_factory=list)
    docs: list[Recall[DocChunk]] = field(default_factory=list)
    changes: list[Recall[CodeChange]] = field(default_factory=list)
    upstream: list[GraphHit] = field(default_factory=list)
    # Live-metric correlation (populated when the alert names a known service):
    # the breached health checks across the alerting service's downstream
    # dependency set, and any curated runbooks recalled for the alert text.
    topology_health: list[NodeHealth] = field(default_factory=list)
    runbooks: list[Recall[Runbook]] = field(default_factory=list)


@dataclass
class Diagnosis:
    """The reasoning layer's output for one alert (produced in step 2)."""

    summary: str
    root_cause: str | None = None
    proposed_steps: list[str] = field(default_factory=list)
    cited_incident_ids: list[str] = field(default_factory=list)
    cited_change_ids: list[str] = field(default_factory=list)
    confidence: float | None = None
    incident_id: str | None = None
    # The model's ranking of the evidence CLASSES by how much each informed this
    # diagnosis, most-useful first — a subset/permutation of EVIDENCE_CLASSES.
    # Drives the evidence panel's section order (most-relevant class on top), so
    # the layout adapts per alert rather than being fixed. Empty when the model
    # didn't rank (older payloads / defensive fallback) → panel uses its default
    # order. Validated in IncidentDiagnoser._build_diagnosis against the known
    # class names; unknown names are dropped.
    evidence_order: list[str] = field(default_factory=list)


# The evidence classes the reasoning layer may rank in `Diagnosis.evidence_order`
# (and the frontend arranges the panel by). Kept beside the model so the prompt,
# the parse-time validation, and the serializer share one source of truth.
EVIDENCE_CLASSES = ("incidents", "docs", "changes", "topology_health", "upstream", "runbooks")


@dataclass
class Message:
    """A conversational agent reply — the *other* response shape besides a full
    `Diagnosis`. Used for follow-ups where a rigid diagnosis is overkill (e.g.
    the engineer asks "how do I rerun the service?"): felix answers directly in
    Markdown (`text`), optionally citing memory, with no forced
    root_cause/steps/confidence. The model chooses `Diagnosis` vs `Message` per
    turn via a `"type"` discriminator; see IncidentDiagnoser._parse_response."""

    text: str  # GitHub-flavored Markdown
    cited_incident_ids: list[str] = field(default_factory=list)
    cited_change_ids: list[str] = field(default_factory=list)
    incident_id: str | None = None
    # Same evidence-class ranking a Diagnosis carries — a follow-up still recalls
    # memory, so the panel re-orders its sections per turn. Empty → default order.
    # See Diagnosis.evidence_order and IncidentDiagnoser._parse_evidence_order.
    evidence_order: list[str] = field(default_factory=list)


# The two response shapes the reasoning layer can produce for one turn.
AgentResponse = Diagnosis | Message


@dataclass
class DiagnosisResult:
    """An agent response plus the evidence packet it was reasoned over.

    `IncidentResponder.respond()` returns this so a single gather serves both
    the response and the evidence display (the CLI's blocks [1]-[5], the API's
    /chat response). The response is a `Diagnosis` (first turns, and re-diagnoses)
    or a lightweight `Message` (conversational follow-ups) — see `response_type`.
    `IncidentResponder.diagnose()` still returns just the `Diagnosis` for callers
    that don't need the packet.

    `session_id` is the working-memory `active_incidents.id` this turn belongs
    to — returned so the caller can echo it on the next turn to continue the
    same conversation (multi-turn follow-ups)."""

    response: AgentResponse
    evidence: EvidencePacket
    session_id: str | None = None

    @property
    def response_type(self) -> str:
        """The discriminator the API/frontend switch on: "message" or "diagnosis"."""
        return "message" if isinstance(self.response, Message) else "diagnosis"

    @property
    def diagnosis(self) -> Diagnosis | None:
        """Back-compat accessor: the Diagnosis when this turn produced one, else
        None (a conversational `message` follow-up). First-turn results are
        always diagnoses, so single-turn callers (CLI, tests, CDC watcher) can
        rely on it being present."""
        return self.response if isinstance(self.response, Diagnosis) else None


# ── working memory: active incident conversations ────────────────────────────


@dataclass
class ActiveIncidentTurn:
    """One message in an active-incident conversation transcript."""

    turn_order: int
    role: str  # "user" | "agent"
    content: str


@dataclass
class ActiveIncident:
    """A live, in-flight incident conversation (working memory).

    Distinct from `Incident` (episodic long-term memory): this is the session
    the multi-turn loop appends turns to, linked to the episodic `incident_id`
    written on the first diagnosis."""

    id: str
    alert: str
    origin_node: str | None = None
    incident_id: str | None = None
    status: str = "open"
    turns: list[ActiveIncidentTurn] = field(default_factory=list)
    source: str = "chat"  # 'chat' | 'cdc' — how the session was opened


@dataclass
class LLMResult:
    """One completion from an LLMClient — mirrors what Embedder returns for
    embeddings, but for a text generation call."""

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
