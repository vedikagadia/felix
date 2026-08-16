import { useState } from "react";
import type {
  AlertPayload,
  ChatRequest,
  ChatResponse,
  Diagnosis,
  EvidencePacket,
  Incident,
  SessionResponse,
} from "./api/types";
import { sendChatStream, usingMock } from "./api/client";
import { getSession } from "./api/alerts";
import { AlertBanner } from "./components/AlertBanner";
import { AlertComposer } from "./components/AlertComposer";
import { ChatThread } from "./components/ChatThread";
import { EvidencePanel } from "./components/EvidencePanel";
import { IncidentsPage } from "./components/IncidentsPage";

export interface Turn {
  id: number;
  request: ChatRequest;
  response?: ChatResponse;
  /** Recall result, delivered before `response` while the model still streams. */
  evidence?: EvidencePacket;
  /** The model's output as it streams in, before the final parsed diagnosis. */
  streamingText?: string;
  error?: string;
  pending: boolean;
}

let nextId = 1;

const EMPTY_EVIDENCE = (alert: string): EvidencePacket => ({
  alert,
  incidents: [],
  docs: [],
  changes: [],
  upstream: [],
});

/**
 * Fold a loaded triage session into a single chat Turn: the synthesized alert
 * as the user side, the reconstructed diagnosis as felix's reply. Evidence is
 * empty for a seeded turn (the session endpoint doesn't re-run recall); a
 * follow-up through /chat/stream fills the panel live.
 */
function seededTurn(id: number, session: SessionResponse): Turn {
  const diagnosis: Diagnosis = session.diagnosis ?? {
    summary: session.alert,
    root_cause: null,
    proposed_steps: [],
    cited_incident_ids: [],
    cited_change_ids: [],
    confidence: null,
    incident_id: session.incident_id,
  };
  return {
    id,
    request: { alert: session.alert, origin_node: session.origin_node, session_id: session.session_id },
    response: {
      response_type: "diagnosis",
      diagnosis,
      message: null,
      evidence: session.evidence ?? EMPTY_EVIDENCE(session.alert),
      session_id: session.session_id,
    },
    pending: false,
  };
}

export function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  // The active-incident conversation. null = the next alert opens a fresh
  // incident; set = follow-ups continue the same conversation (multi-turn).
  const [sessionId, setSessionId] = useState<string | null>(null);
  // The evidence id (incident/change) currently focused via a citation link —
  // hovering a diagnosis citation chip or an evidence card sets it, so the two
  // panes highlight each other. null = nothing focused.
  const [activeCitation, setActiveCitation] = useState<string | null>(null);
  // Which page is showing: the live triage chat, or the incident library.
  const [view, setView] = useState<"chat" | "incidents">("chat");
  // Text to drop into the composer from the library's "Ask AI". `nonce` bumps
  // on every click so re-asking the same incident re-fills the box.
  const [prefill, setPrefill] = useState<{ text: string; nonce: number }>({ text: "", nonce: 0 });

  const activeTurn = turns.find((t) => t.id === activeId) ?? null;

  function newIncident() {
    setSessionId(null);
  }

  // "Ask AI" from the incident library: jump to the chat, start a fresh
  // conversation, and pre-fill the composer with this incident's symptoms so
  // the operator can diagnose the same class of problem live.
  function askAI(incident: Incident) {
    setSessionId(null);
    setPrefill((p) => ({ text: incident.symptoms, nonce: p.nonce + 1 }));
    setView("chat");
  }

  // Open the triage session behind a clicked alert: load its pre-seeded turns,
  // render them in the existing chat as one Turn (the synthesized alert + the
  // agent's diagnosis), and adopt its session_id so the composer's next message
  // is a follow-up through the same /chat/stream path — no forked rendering.
  async function openAlert(alert: AlertPayload) {
    const existing = turns.find((t) => t.request.session_id === alert.session_id);
    if (existing) {
      setActiveId(existing.id);
      setSessionId(alert.session_id);
      return;
    }

    let session: SessionResponse;
    try {
      session = await getSession(alert.session_id);
    } catch (e) {
      const id = nextId++;
      setTurns((prev) => [
        ...prev,
        {
          id,
          request: { alert: alert.summary, session_id: alert.session_id },
          error: e instanceof Error ? e.message : String(e),
          pending: false,
        },
      ]);
      setActiveId(id);
      return;
    }

    const id = nextId++;
    setTurns((prev) => [...prev, seededTurn(id, session)]);
    setActiveId(id);
    setSessionId(session.session_id);
  }

  async function submit(req: ChatRequest) {
    const id = nextId++;
    // Continue the current conversation if one is open.
    const outbound: ChatRequest = { ...req, session_id: sessionId ?? undefined };
    setTurns((prev) => [...prev, { id, request: outbound, pending: true }]);
    setActiveId(id);
    setBusy(true);

    // Stream the turn: evidence fills the panel first, deltas render live, then
    // the final response swaps in the parsed diagnosis. onDone/onError are
    // terminal; setBusy is cleared once the stream settles.
    try {
      await sendChatStream(outbound, {
        onEvidence: (evidence) =>
          setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, evidence } : t))),
        onDelta: (text) =>
          setTurns((prev) =>
            prev.map((t) =>
              t.id === id ? { ...t, streamingText: (t.streamingText ?? "") + text } : t,
            ),
          ),
        onDone: (response) => {
          // Adopt the conversation id so the next message is a follow-up.
          setSessionId(response.session_id ?? null);
          setTurns((prev) =>
            prev.map((t) => (t.id === id ? { ...t, response, pending: false } : t)),
          );
        },
        onError: (error) =>
          setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, error, pending: false } : t))),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <span className="app__logo">🦊</span>
          <div>
            <h1>felix</h1>
            <p>SRE incident-memory agent</p>
          </div>
        </div>
        <nav className="app__nav">
          <button
            type="button"
            className={`app__navlink ${view === "chat" ? "is-active" : ""}`}
            onClick={() => setView("chat")}
          >
            Triage
          </button>
          <button
            type="button"
            className={`app__navlink ${view === "incidents" ? "is-active" : ""}`}
            onClick={() => setView("incidents")}
          >
            Incident library
          </button>
        </nav>
        {usingMock && (
          <span className="badge badge--mock" title="Set VITE_API_URL to use a real backend">
            mock mode
          </span>
        )}
      </header>

      {view === "incidents" ? (
        <main className="app__body app__body--single">
          <IncidentsPage onAskAI={askAI} />
        </main>
      ) : (
        <>
          <AlertBanner onSelect={openAlert} activeSessionId={sessionId} />

          <main className="app__body">
            <section className="chat">
              <ChatThread
                turns={turns}
                activeId={activeId}
                onSelect={setActiveId}
                activeCitation={activeCitation}
                onCitationFocus={setActiveCitation}
              />
              <AlertComposer
                onSubmit={submit}
                disabled={busy}
                continuing={sessionId !== null}
                onNewIncident={newIncident}
                prefill={prefill.text}
                prefillKey={prefill.nonce}
              />
            </section>

            <aside className="evidence">
              <EvidencePanel
                turn={activeTurn}
                activeCitation={activeCitation}
                onCitationFocus={setActiveCitation}
              />
            </aside>
          </main>
        </>
      )}
    </div>
  );
}
