import { useState } from "react";
import type { ChatRequest, ChatResponse, EvidencePacket } from "./api/types";
import { sendChatStream, usingMock } from "./api/client";
import { AlertComposer } from "./components/AlertComposer";
import { ChatThread } from "./components/ChatThread";
import { EvidencePanel } from "./components/EvidencePanel";

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

export function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  // The active-incident conversation. null = the next alert opens a fresh
  // incident; set = follow-ups continue the same conversation (multi-turn).
  const [sessionId, setSessionId] = useState<string | null>(null);

  const activeTurn = turns.find((t) => t.id === activeId) ?? null;

  function newIncident() {
    setSessionId(null);
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
        {usingMock && (
          <span className="badge badge--mock" title="Set VITE_API_URL to use a real backend">
            mock mode
          </span>
        )}
      </header>

      <main className="app__body">
        <section className="chat">
          <ChatThread turns={turns} activeId={activeId} onSelect={setActiveId} />
          <AlertComposer
            onSubmit={submit}
            disabled={busy}
            continuing={sessionId !== null}
            onNewIncident={newIncident}
          />
        </section>

        <aside className="evidence">
          <EvidencePanel turn={activeTurn} />
        </aside>
      </main>
    </div>
  );
}
