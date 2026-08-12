"""Domain models — the vocabulary the rest of felix speaks in.

Repositories return these dataclasses instead of raw DB tuples, so the service
and CLI layers never index into positional rows (`r[6]`) and never care about
column order. Each memory source has a model; recall results wrap a model with
its vector distance (`Recall`), and the graph wrapper adds a traversal depth
(`GraphHit`). `EvidencePacket` is everything the retriever assembles for one
alert; `Diagnosis` is what the reasoning layer will produce from it (step 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar


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


# ── assembled evidence + reasoning output ────────────────────────────────────


@dataclass
class EvidencePacket:
    """Everything the retriever gathers for one alert — the input the reasoning
    layer will diagnose over."""

    alert: str
    incidents: list[Recall[Incident]] = field(default_factory=list)
    docs: list[Recall[DocChunk]] = field(default_factory=list)
    changes: list[Recall[CodeChange]] = field(default_factory=list)
    upstream: list[GraphHit] = field(default_factory=list)


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


@dataclass
class LLMResult:
    """One completion from an LLMClient — mirrors what Embedder returns for
    embeddings, but for a text generation call."""

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
