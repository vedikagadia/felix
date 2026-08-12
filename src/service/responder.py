"""IncidentResponder — the reasoning half of the agent loop (step 2).

`retriever.py` gathers an `EvidencePacket` (deterministic retrieval); this
module hands that packet to an `LLMClient` for a `Diagnosis`, then writes the
diagnosis back to memory (a minimal `incidents` row + its `resolution_steps` +
one `agent_actions` audit row). Retrieval stays deterministic — the LLM only
reasons over exactly what it's handed, and only ids that are verbatim in that
context are allowed to survive as citations.

Talks to the DB only through the repositories it's given (or exposed by the
retriever) — no raw SQL, no connection of its own.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..clients.llm import LLMClient
from ..models import Diagnosis, EvidencePacket, ResolutionStep
from ..store.repositories import ActionRepository, IncidentRepository
from .retriever import Retriever

# Below this L2 distance, the top recalled incident/doc is considered "close
# enough" to the alert that its text is worth mining for an origin-node guess
# when the caller didn't pass one explicitly. Named + documented per the
# contract (Option B origin-node resolution) rather than inlined, so the
# threshold is easy to tune from one place. 0.6 is a conservative cutoff for
# the bge-large/Titan 1024-dim space used here (0 = identical text).
ORIGIN_MATCH_MAX_DISTANCE = 0.6

# Token-extraction passes, most-specific first, used to pull candidate
# code-symbol tokens out of free-text incident/doc content (title, symptoms,
# root_cause, heading, body). This is NOT a symptom->node lookup table — it's
# a generic shape-based scan; resolution against real code_nodes happens via
# GraphRepository.find_node_by_name, which does the actual (fuzzy) matching.
_DOTTED_TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_CAMEL_TOKEN_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]*\b")
_SNAKE_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]*\b")
_MAX_CANDIDATE_TOKENS = 25

_SYSTEM_PROMPT = (
    "You are an SRE incident-response assistant. Use ONLY the provided memory. "
    "Cite ONLY ids that appear in the context. If the evidence points to a "
    "single source, say so. State a confidence 0..1. Never invent ids."
)

_JSON_SCHEMA_HINT = """Return ONLY a JSON object (no prose, no markdown fences) matching exactly:
{
  "summary": "one-line what's wrong",
  "root_cause": "the true cause, or null if unknown",
  "proposed_steps": [
    {"action": "plain English", "command": "exact cmd or null", "outcome": "expected effect"}
  ],
  "cited_incident_ids": ["...ids copied verbatim from the 'id:' fields above..."],
  "cited_change_ids": ["...ids copied verbatim from the 'id:' fields above..."],
  "confidence": 0.0
}"""


class IncidentResponder:
    """Self-orchestrated diagnose loop: recall -> resolve origin -> prompt ->
    reason -> parse defensively -> write back."""

    def __init__(
        self,
        retriever: Retriever,
        llm: LLMClient,
        incident_repo: IncidentRepository,
        action_repo: ActionRepository,
    ):
        self.retriever = retriever
        self.llm = llm
        self.incident_repo = incident_repo
        self.action_repo = action_repo

    # ── the loop ─────────────────────────────────────────────────────────────

    def diagnose(self, alert: str, origin_node: str | None = None) -> Diagnosis:
        # 1. RECALL. If origin_node is given explicitly, Retriever.gather already
        # runs the upstream trace itself (it accepts origin_node and does so
        # internally) — no need to duplicate that here.
        packet = self.retriever.gather(alert, origin_node=origin_node)

        # 2. ORIGIN-NODE RESOLUTION (Option B) — only when the caller didn't
        # already pin a node and gather() therefore didn't trace anything.
        resolved_service: str | None = None
        if origin_node is None and not packet.upstream:
            node = self._resolve_origin_node(packet)
            if node is not None:
                packet.upstream = self.retriever.graph.upstream_callers(node.name, max_depth=4)
                resolved_service = node.service

        if resolved_service is None:
            resolved_service = self._infer_service_from_top_incident(packet)

        # 3. BUILD PROMPT.
        prompt = self._build_prompt(packet)

        # 4. REASON. Let exceptions from the LLM propagate untouched — no writes
        # have happened yet, so a failed call leaves no partial rows.
        result = self.llm.complete(prompt, system=_SYSTEM_PROMPT)

        # 5. PARSE DEFENSIVELY + citation-integrity guard.
        diagnosis = self._parse_diagnosis(result.text, packet)

        # 6. WRITE-BACK — only now that we have a parsed Diagnosis.
        # diagnosis.proposed_steps is typed list[str] in models.py, but at
        # runtime holds whatever the model returned for "proposed_steps" —
        # list[str] OR list[dict] per the contract's schema — so convert
        # defensively rather than assuming either shape.
        title = self._derive_title(alert)
        steps = self._to_resolution_steps(diagnosis.proposed_steps)
        total_tokens = (result.input_tokens or 0) + (result.output_tokens or 0)

        # All three writes (incident, its steps, the audit row) go in ONE
        # transaction so a mid-sequence failure rolls back cleanly instead of
        # leaving an orphan incident with partial/no steps. The repos share the
        # single connection built in cli.py; connection.py documents
        # `with conn.transaction():` as the boundary idiom (autocommit is on
        # otherwise, which is what previously stranded orphan rows).
        with self.incident_repo.conn.transaction():
            incident_id = self.incident_repo.insert_minimal(
                title=title,
                symptoms=alert,
                service=resolved_service,
                severity=None,
            )
            if steps:
                self.incident_repo.add_resolution_steps(incident_id, steps)
            diagnosis.incident_id = incident_id

            self.action_repo.log(
                action_type="diagnose",
                tool_called="respond",
                # ActionRepository.log treats a raw str as PRE-serialized JSON
                # and writes it verbatim to the JSONB column — a bare alert
                # string isn't valid JSON, so wrap it in a dict (which log
                # json.dumps's for us).
                input={"alert": alert},
                output=_diagnosis_to_dict(diagnosis),
                model=result.model,
                tokens=total_tokens or None,
            )

        # 7. return.
        return diagnosis

    # ── origin-node resolution ───────────────────────────────────────────────

    def _resolve_origin_node(self, packet: EvidencePacket):
        """Best-effort Option-B resolution: mine tokens from the top recalled
        incident/doc (only if it's a close match) and resolve the first one
        that maps to a real code node. Returns a CodeNode or None — never
        raises; callers degrade to "skip the trace" on None."""
        candidates: list[str] = []
        if packet.incidents and packet.incidents[0].distance < ORIGIN_MATCH_MAX_DISTANCE:
            inc = packet.incidents[0].item
            text = "\n".join(filter(None, [inc.title, inc.symptoms, inc.root_cause]))
            candidates.extend(_extract_candidate_tokens(text))
        if packet.docs and packet.docs[0].distance < ORIGIN_MATCH_MAX_DISTANCE:
            doc = packet.docs[0].item
            text = "\n".join(filter(None, [doc.heading, doc.body]))
            candidates.extend(_extract_candidate_tokens(text))

        seen: set[str] = set()
        for token in candidates:
            if token in seen:
                continue
            seen.add(token)
            node = self.retriever.graph.find_node_by_name(token)
            if node is not None:
                return node
        return None

    @staticmethod
    def _infer_service_from_top_incident(packet: EvidencePacket) -> str | None:
        """Cheap, non-hardcoded fallback for the incident row's `service`
        column: reuse the same close-match top incident already recalled,
        rather than guessing from the alert text."""
        if packet.incidents and packet.incidents[0].distance < ORIGIN_MATCH_MAX_DISTANCE:
            return packet.incidents[0].item.service
        return None

    # ── prompt construction ──────────────────────────────────────────────────

    def _build_prompt(self, packet: EvidencePacket) -> str:
        # CITATION-ID DECISION: Recall[Incident].item.id / Recall[CodeChange].item.id
        # are UUIDs (seed rows are stored with a uuid5 of the human id, e.g.
        # uuid5(NS, "inc-0001") — see seed/loader.py's _seed_uuid), and nothing
        # exposed to this layer (Retriever / repositories) resolves that UUID
        # back to the human "inc-0001"/"chg-0001" string — there is no reverse
        # lookup method on IncidentRepository/ChangeRepository, and we own only
        # this file. Inventing a mapping (e.g. re-deriving uuid5 candidates)
        # would not be "derivable" from what's actually in the packet, so per
        # the contract's instruction to cite ids that MUST be present verbatim
        # in the context we send, we cite the UUID that IS on the packet's
        # item.id — the same string is printed in the context below and is
        # what we filter model citations against in _parse_diagnosis. This is
        # the retrieval-facing id gap the contract flags for the orchestrator:
        # a follow-up (Team C) could add a human-id passthrough so citations
        # read as "inc-0001" instead of a UUID.
        sections: list[str] = [f"Alert: {packet.alert}", ""]

        sections.append("## Similar past incidents (episodic memory)")
        if not packet.incidents:
            sections.append("(none recalled)")
        for r in packet.incidents:
            inc = r.item
            sections.append(f"- id: {inc.id}  distance: {r.distance:.3f}")
            sections.append(f"  title: {inc.title}")
            sections.append(f"  severity: {inc.severity}  service: {inc.service}")
            sections.append(f"  symptoms: {inc.symptoms}")
            if inc.root_cause:
                sections.append(f"  root_cause: {inc.root_cause}")
        sections.append("")

        sections.append("## Relevant docs")
        if not packet.docs:
            sections.append("(none recalled)")
        for r in packet.docs:
            doc = r.item
            sections.append(f"- distance: {r.distance:.3f}  {doc.doc_title} — {doc.heading}")
            sections.append(f"  {doc.body}")
        sections.append("")

        sections.append("## Recent code changes (last 14 days — the \"what changed?\" signal)")
        if not packet.changes:
            sections.append("(none in window)")
        for r in packet.changes:
            chg = r.item
            sections.append(f"- id: {chg.id}  distance: {r.distance:.3f}  merged_at: {chg.merged_at}")
            sections.append(f"  title: {chg.title}")
            if chg.summary:
                sections.append(f"  summary: {chg.summary}")
        sections.append("")

        sections.append("## Upstream call trace (symptom origin -> who drives it)")
        if not packet.upstream:
            sections.append("(no trace — no origin node resolved)")
        for hit in packet.upstream:
            n = hit.node
            sections.append(f"- depth {hit.depth}: {n.name} ({n.kind})  file={n.file}")
            if n.summary:
                sections.append(f"  summary: {n.summary}")
            if n.source:
                src = n.source if len(n.source) <= 2000 else n.source[:2000] + "\n...(truncated)"
                sections.append(f"  source:\n{src}")
        sections.append("")

        sections.append(_JSON_SCHEMA_HINT)
        return "\n".join(sections)

    # ── defensive parsing + citation-integrity guard ────────────────────────

    def _parse_diagnosis(self, text: str, packet: EvidencePacket) -> Diagnosis:
        obj = _extract_json_object(text)

        if obj is None:
            # Non-JSON (or unparseable) completion: never raise, fall back to a
            # minimal Diagnosis carrying the raw text as the summary.
            return Diagnosis(
                summary=text[:500],
                root_cause=None,
                proposed_steps=[],
                cited_incident_ids=[],
                cited_change_ids=[],
                confidence=None,
            )

        valid_incident_ids = {r.item.id for r in packet.incidents}
        valid_change_ids = {r.item.id for r in packet.changes}

        raw_steps = obj.get("proposed_steps")
        proposed_steps = raw_steps if isinstance(raw_steps, list) else []

        cited_incidents = obj.get("cited_incident_ids")
        cited_incidents = cited_incidents if isinstance(cited_incidents, list) else []
        cited_changes = obj.get("cited_change_ids")
        cited_changes = cited_changes if isinstance(cited_changes, list) else []

        confidence = obj.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            confidence = max(0.0, min(1.0, float(confidence)))
        else:
            confidence = None

        summary = obj.get("summary")
        if not isinstance(summary, str) or not summary:
            summary = text[:500]

        root_cause = obj.get("root_cause")
        if not isinstance(root_cause, str):
            root_cause = None

        return Diagnosis(
            summary=summary,
            root_cause=root_cause,
            proposed_steps=proposed_steps,
            # citation-integrity guard: drop anything not verbatim in the packet.
            cited_incident_ids=[i for i in cited_incidents if isinstance(i, str) and i in valid_incident_ids],
            cited_change_ids=[i for i in cited_changes if isinstance(i, str) and i in valid_change_ids],
            confidence=confidence,
        )

    # ── write-back helpers ───────────────────────────────────────────────────

    @staticmethod
    def _derive_title(alert: str) -> str:
        title = " ".join(alert.split())  # collapse whitespace
        return title if len(title) <= 120 else title[:117] + "..."

    @staticmethod
    def _to_resolution_steps(raw_steps: list[Any]) -> list[ResolutionStep]:
        """proposed_steps may come back as list[str] OR list[dict] (the
        contract's schema wants {"action","command","outcome"} dicts, but a
        model may emit plain strings) — handle both."""
        steps: list[ResolutionStep] = []
        for i, item in enumerate(raw_steps or [], start=1):
            if isinstance(item, dict):
                action = item.get("action")
                if not isinstance(action, str) or not action:
                    action = json.dumps(item)
                command = item.get("command")
                command = command if isinstance(command, str) else None
                outcome = item.get("outcome")
                outcome = outcome if isinstance(outcome, str) else None
            else:
                action = str(item)
                command = None
                outcome = None
            steps.append(ResolutionStep(step_order=i, action=action, command=command, outcome=outcome))
        return steps


# ── module-level helpers (no state, easy to unit test in isolation) ─────────


def _extract_candidate_tokens(text: str) -> list[str]:
    """Pull likely code-symbol-shaped tokens out of free text, most-specific
    shape first: dotted `Class.method` names, then bare CamelCase identifiers,
    then snake_case identifiers. Order of first appearance is preserved within
    each pass; duplicates are dropped. This is a generic shape scan, not a
    symptom->node table — actual resolution happens in GraphRepository."""
    seen: set[str] = set()
    out: list[str] = []
    for pattern in (_DOTTED_TOKEN_RE, _CAMEL_TOKEN_RE, _SNAKE_TOKEN_RE):
        for match in pattern.findall(text):
            if match not in seen:
                seen.add(match)
                out.append(match)
            if len(out) >= _MAX_CANDIDATE_TOKENS:
                return out
    return out


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(text: str) -> dict | None:
    """Best-effort JSON extraction from an LLM completion: prefer a ```json
    fenced block, else take the outermost {...} span. Tolerates anything else
    by returning None (never raises) so callers can fall back.

    When multiple fenced blocks exist (e.g. the model quoted an example before
    its real answer), scan them LAST-first and prefer one that actually looks
    like a Diagnosis (has a "summary" key); the answer block is conventionally
    last. Fall back to the last parseable object, then to the bracket-scan."""
    fenced = [m for m in _FENCED_JSON_RE.findall(text)]
    parsed_fenced: list[dict] = []
    for candidate in reversed(fenced):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if "summary" in obj:
                return obj
            parsed_fenced.append(obj)
    if parsed_fenced:
        return parsed_fenced[0]

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _diagnosis_to_dict(diagnosis: Diagnosis) -> dict[str, Any]:
    """Plain-dict projection of a Diagnosis for the agent_actions JSONB output
    column (ActionRepository.log json.dumps's whatever it's handed)."""
    return {
        "summary": diagnosis.summary,
        "root_cause": diagnosis.root_cause,
        "proposed_steps": diagnosis.proposed_steps,
        "cited_incident_ids": diagnosis.cited_incident_ids,
        "cited_change_ids": diagnosis.cited_change_ids,
        "confidence": diagnosis.confidence,
        "incident_id": diagnosis.incident_id,
    }
