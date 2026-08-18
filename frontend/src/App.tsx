import { useEffect, useState } from "react";
import type {
  AlertPayload,
  ChatRequest,
  ChatResponse,
  Diagnosis,
  EvidencePacket,
  Incident,
  Project,
  SessionResponse,
} from "./api/types";
import { sendChatStream, usingMock } from "./api/client";
import { getSession } from "./api/alerts";
import {
  DEFAULT_PROJECT,
  fetchProjects,
  getActiveProject,
  setActiveProject,
} from "./api/projects";
import { AlertBanner } from "./components/AlertBanner";
import { AlertComposer } from "./components/AlertComposer";
import { ChatThread } from "./components/ChatThread";
import { EvidencePanel } from "./components/EvidencePanel";
import { ReasoningOverlay } from "./components/ReasoningOverlay";
import { IncidentsPage } from "./components/IncidentsPage";
import { LiveMonitoringPage } from "./components/LiveMonitoringPage";
import { DbOverviewPage } from "./components/DbOverviewPage";
import { CliPage } from "./components/CliPage";
import { OnboardPage } from "./components/OnboardPage";

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
  // Which page is showing: the triage chat, the incident library, the live
  // monitoring panel, DB overview, the CLI, or the onboarding form.
  const [view, setView] = useState<
    "chat" | "incidents" | "live" | "db" | "cli" | "onboard"
  >("chat");
  // Multi-project: the memory namespace the whole app is scoped to. `projects`
  // populates the header switcher; `activeProject` is persisted (localStorage)
  // and threaded onto every scoped API call via api/projects.appendProject.
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProjectState] = useState<string>(() => getActiveProject());
  // Text to drop into the composer from the library's "Ask AI". `nonce` bumps
  // on every click so re-asking the same incident re-fills the box.
  const [prefill, setPrefill] = useState<{ text: string; nonce: number }>({ text: "", nonce: 0 });
  // The turn currently playing the reasoning-replay overlay (a fresh diagnosis
  // only — follow-ups skip it). null = no overlay.
  const [replayId, setReplayId] = useState<number | null>(null);

  const activeTurn = turns.find((t) => t.id === activeId) ?? null;
  const replayTurn = replayId !== null ? (turns.find((t) => t.id === replayId) ?? null) : null;

  // Load the project list once for the header switcher (best-effort — the app
  // still works scoped to the persisted/default project if this fails).
  useEffect(() => {
    fetchProjects()
      .then(setProjects)
      .catch(() => {
        /* leave empty; the switcher falls back to the active project alone */
      });
  }, []);

  function newIncident() {
    setSessionId(null);
  }

  // Switch the active memory namespace: persist it, re-scope the app, and reset
  // the in-flight conversation (turns/session belong to the old project). The
  // project-scoped pages re-key on `activeProject`, so they re-fetch fresh.
  function switchProject(slug: string) {
    if (slug === activeProject) return;
    setActiveProject(slug);
    setActiveProjectState(slug);
    setTurns([]);
    setActiveId(null);
    setSessionId(null);
    setReplayId(null);
    setActiveCitation(null);
  }

  // After a successful onboard: refresh the switcher, make sure the new project
  // is listed (mock/back-end race), switch to it, and land on Triage.
  async function handleOnboarded(slug: string, displayName: string) {
    let list = projects;
    try {
      list = await fetchProjects();
    } catch {
      /* keep the current list */
    }
    if (!list.some((p) => p.id === slug)) {
      list = [
        ...list,
        {
          id: slug,
          display_name: displayName,
          source_kind: null,
          source_ref: null,
          created_at: null,
          last_synced: null,
        },
      ];
    }
    setProjects(list);
    switchProject(slug);
    setView("chat");
  }

  // The switcher options always include the active project, even if the list
  // fetch failed or it isn't (yet) in the returned list.
  const projectOptions: Project[] = projects.some((p) => p.id === activeProject)
    ? projects
    : [
        ...projects,
        {
          id: activeProject,
          display_name: activeProject === DEFAULT_PROJECT ? "Checkout demo (sample)" : activeProject,
          source_kind: null,
          source_ref: null,
          created_at: null,
          last_synced: null,
        },
      ];

  // "Ask AI" from the incident library: jump to the chat, start a fresh
  // conversation, and pre-fill the composer with this incident's symptoms so
  // the operator can diagnose the same class of problem live.
  function askAI(incident: Incident) {
    setSessionId(null);
    setPrefill((p) => ({ text: incident.symptoms, nonce: p.nonce + 1 }));
    setView("chat");
  }

  // "Ask felix" from a tripped live-monitoring card: same jump-to-Triage flow,
  // but pre-filled with a synthesized alert built from the live p99/avg numbers
  // so the operator can triage the spike without retyping it.
  function askFelixAbout(alert: string) {
    setSessionId(null);
    setPrefill((p) => ({ text: alert, nonce: p.nonce + 1 }));
    setView("chat");
  }

  // Open the triage session behind a clicked alert. Two cases:
  //  • already diagnosed — render the reconstructed diagnosis as one seeded Turn
  //    (alerting is decoupled, but a session opened via chat already has one).
  //  • raised-but-undiagnosed (a CDC alert: session + user turn, NO incident yet)
  //    — the watcher never called the LLM, so the diagnosis happens HERE, on
  //    open: drive a first-turn /chat/stream into that session so felix reasons
  //    live (evidence panel fills, deltas render) exactly like a fresh incident.
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

    setSessionId(session.session_id);
    if (session.incident_id) {
      // Already diagnosed — just render it.
      const id = nextId++;
      setTurns((prev) => [...prev, seededTurn(id, session)]);
      setActiveId(id);
      return;
    }

    // Undiagnosed CDC alert → diagnose on open. The session already carries the
    // user turn; sending its alert with the session_id routes the backend
    // through the first-turn diagnosis path (a session with no incident yet is
    // NOT treated as a follow-up), minting + linking the incident.
    await streamTurn(
      { alert: session.alert, origin_node: session.origin_node, session_id: session.session_id },
      { overlay: true },
    );
  }

  // Core of a streamed turn: create the Turn, run /chat/stream, and fold each
  // frame (evidence → deltas → done/error) into it. `overlay` plays the
  // reasoning-replay overlay (a fresh diagnosis — first chat turn or a CDC
  // alert's diagnose-on-open); follow-ups pass false.
  async function streamTurn(outbound: ChatRequest, { overlay }: { overlay: boolean }) {
    const id = nextId++;
    setTurns((prev) => [...prev, { id, request: outbound, pending: true }]);
    setActiveId(id);
    setBusy(true);
    if (overlay) setReplayId(id);

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

  async function submit(req: ChatRequest) {
    // Continue the current conversation if one is open; a fresh incident (no
    // session yet) plays the reasoning-replay overlay, a follow-up doesn't.
    await streamTurn({ ...req, session_id: sessionId ?? undefined }, { overlay: !sessionId });
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
          <button
            type="button"
            className={`app__navlink ${view === "live" ? "is-active" : ""}`}
            onClick={() => setView("live")}
          >
            Live monitoring
          </button>
          <button
            type="button"
            className={`app__navlink ${view === "db" ? "is-active" : ""}`}
            onClick={() => setView("db")}
          >
            DB overview
          </button>
          <button
            type="button"
            className={`app__navlink ${view === "cli" ? "is-active" : ""}`}
            onClick={() => setView("cli")}
          >
            CLI
          </button>
          <button
            type="button"
            className={`app__navlink ${view === "onboard" ? "is-active" : ""}`}
            onClick={() => setView("onboard")}
          >
            Onboard
          </button>
        </nav>
        <label className="app__project" title="Active project — scopes all of felix's memory">
          <span className="app__projectlabel">project</span>
          <select
            className="app__projectsel"
            value={activeProject}
            onChange={(e) => switchProject(e.target.value)}
          >
            {projectOptions.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name}
              </option>
            ))}
          </select>
        </label>
        {usingMock && (
          <span className="badge badge--mock" title="Set VITE_API_URL to use a real backend">
            mock mode
          </span>
        )}
      </header>

      {view === "incidents" ? (
        <main className="app__body app__body--single">
          {/* Re-key on the project so the library re-fetches when it changes. */}
          <IncidentsPage key={activeProject} onAskAI={askAI} />
        </main>
      ) : view === "live" ? (
        <main className="app__body app__body--single">
          <LiveMonitoringPage key={activeProject} onAskFelix={askFelixAbout} project={activeProject} />
        </main>
      ) : view === "db" ? (
        <main className="app__body app__body--single">
          <DbOverviewPage />
        </main>
      ) : view === "cli" ? (
        <main className="app__body app__body--single">
          <CliPage />
        </main>
      ) : view === "onboard" ? (
        <main className="app__body app__body--single">
          <OnboardPage onOnboarded={handleOnboarded} />
        </main>
      ) : (
        <>
          {/* Re-key on the project so the alert poll re-subscribes per namespace. */}
          <AlertBanner key={activeProject} onSelect={openAlert} activeSessionId={sessionId} />

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

      {replayTurn && (
        <ReasoningOverlay turn={replayTurn} onDone={() => setReplayId(null)} />
      )}
    </div>
  );
}
