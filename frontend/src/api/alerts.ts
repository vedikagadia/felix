/**
 * The alert-delivery seam: the ONLY place that knows HOW live CDC alerts arrive.
 *
 * Today it polls `GET /alerts` every 3s. The documented SSE upgrade (see
 * .orchestration/CDC_INTERFACE.md §9) swaps `subscribeToAlerts`'s body for an
 * EventSource with no change to its signature or to any caller — that is the
 * whole point of routing every consumer through this one function.
 *
 * No other module may fetch `/alerts` directly (a reviewer enforces this);
 * banner/dashboard code subscribes here instead.
 */

import { usingMock } from "./client";
import { appendProject } from "./projects";
import type { AlertPayload, SessionResponse } from "./types";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;
const ALERT_POLL_MS = 3000;

export function subscribeToAlerts(
  onNewAlerts: (alerts: AlertPayload[]) => void,
): () => void {
  // Mock mode has no watcher process; degrade to a no-op subscription so the
  // banner simply stays empty rather than hammering a nonexistent endpoint.
  if (usingMock) {
    return () => {};
  }

  let stopped = false;

  async function poll(): Promise<void> {
    try {
      const res = await fetch(appendProject(`${API_URL}/alerts`));
      if (!res.ok) return;
      const body = (await res.json()) as { alerts?: AlertPayload[] };
      if (!stopped) onNewAlerts(body.alerts ?? []);
    } catch {
      // Swallow: the banner must degrade quietly when the backend is down.
    }
  }

  void poll(); // fire immediately; don't make the operator wait a full interval
  const timer = setInterval(() => void poll(), ALERT_POLL_MS);

  return () => {
    stopped = true;
    clearInterval(timer);
  };
}

/**
 * Load a triage session's transcript + reconstructed diagnosis so the chat can
 * render its pre-seeded turns and continue via `/chat/stream`. Throws on a
 * transport or non-2xx failure so the click handler can surface it.
 */
export async function getSession(id: string): Promise<SessionResponse> {
  if (!API_URL) {
    throw new Error("Cannot load a session in mock mode (VITE_API_URL is unset).");
  }
  const res = await fetch(appendProject(`${API_URL}/sessions/${id}`));
  if (!res.ok) {
    throw new Error(`Backend returned ${res.status} ${res.statusText} for session ${id}.`);
  }
  return (await res.json()) as SessionResponse;
}
