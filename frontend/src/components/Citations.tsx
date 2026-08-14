import type { EvidencePacket } from "../api/types";

/**
 * The cited-memory chips on a diagnosis/message card — one chip per cited
 * incident/change, labelled with the recalled record's title (citations are
 * guaranteed to be in the packet: the backend's citation guard drops any id
 * that isn't). Hovering or clicking a chip focuses the matching evidence card
 * in the panel (and clicking first selects this turn so the panel shows it) —
 * the diagnosis→evidence half of the bidirectional link. The evidence→diagnosis
 * half lives in EvidencePanel, which sets the same `activeCitation`.
 */
export function Citations({
  incidentIds,
  changeIds,
  evidence,
  activeCitation,
  onCitationFocus,
  onShowEvidence,
}: {
  incidentIds: string[];
  changeIds: string[];
  evidence?: EvidencePacket;
  activeCitation: string | null;
  onCitationFocus: (id: string | null) => void;
  onShowEvidence: () => void;
}) {
  const chips: { id: string; label: string; kind: "inc" | "chg" }[] = [];
  for (const id of incidentIds) {
    const hit = evidence?.incidents.find((r) => r.item.id === id);
    chips.push({ id, kind: "inc", label: hit?.item.title ?? id });
  }
  for (const id of changeIds) {
    const hit = evidence?.changes.find((r) => r.item.id === id);
    chips.push({ id, kind: "chg", label: hit?.item.title ?? id });
  }
  if (chips.length === 0) return null;

  return (
    <div className="cites">
      <span className="cites__label">grounded in</span>
      {chips.map((c) => (
        <button
          key={c.id}
          type="button"
          className={`cite cite--${c.kind} ${activeCitation === c.id ? "is-active" : ""}`}
          title={c.label}
          onMouseEnter={() => onCitationFocus(c.id)}
          onMouseLeave={() => onCitationFocus(null)}
          onFocus={() => onCitationFocus(c.id)}
          onBlur={() => onCitationFocus(null)}
          onClick={() => {
            onShowEvidence();
            onCitationFocus(c.id);
          }}
        >
          {c.label.length > 32 ? c.label.slice(0, 31) + "…" : c.label}
        </button>
      ))}
    </div>
  );
}
