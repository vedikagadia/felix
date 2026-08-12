import { useState } from "react";
import type { ChatRequest, ChatResponse } from "./api/types";
import { sendChat, usingMock, ApiError } from "./api/client";
import { AlertComposer } from "./components/AlertComposer";
import { ChatThread } from "./components/ChatThread";
import { EvidencePanel } from "./components/EvidencePanel";

export interface Turn {
  id: number;
  request: ChatRequest;
  response?: ChatResponse;
  error?: string;
  pending: boolean;
}

let nextId = 1;

export function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const activeTurn = turns.find((t) => t.id === activeId) ?? null;

  async function submit(req: ChatRequest) {
    const id = nextId++;
    setTurns((prev) => [...prev, { id, request: req, pending: true }]);
    setActiveId(id);
    setBusy(true);

    try {
      const response = await sendChat(req);
      setTurns((prev) =>
        prev.map((t) => (t.id === id ? { ...t, response, pending: false } : t)),
      );
    } catch (e) {
      const error =
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
      setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, error, pending: false } : t)));
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
          <AlertComposer onSubmit={submit} disabled={busy} />
        </section>

        <aside className="evidence">
          <EvidencePanel turn={activeTurn} />
        </aside>
      </main>
    </div>
  );
}
