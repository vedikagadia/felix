/**
 * The single seam between the UI and the backend.
 *
 * Behaviour is driven by env (see .env.example):
 *   - VITE_API_URL unset  → MOCK mode (canned responses, no server needed)
 *   - VITE_API_URL set    → POST `${VITE_API_URL}/chat`
 *   - VITE_USE_MOCK=true  → force mock even when VITE_API_URL is set
 *
 * The backend you build only has to accept a ChatRequest and return a
 * ChatResponse (src/api/types.ts). Nothing else in the app talks to the network.
 */

import type {
  ChatRequest,
  ChatResponse,
  FeedbackResponse,
  IncidentHit,
  StreamHandlers,
} from "./types";
import {
  mockChat,
  mockChatStream,
  mockListIncidents,
  mockSearchIncidents,
  mockSubmitFeedback,
} from "./mock";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;
const FORCE_MOCK = import.meta.env.VITE_USE_MOCK === "true";

// An empty-but-set VITE_API_URL ("") means "same-origin relative paths" — the
// deployed bundle is served by the API itself, so `${API_URL}/chat` resolves to
// `/chat` on the same host. Only a genuinely UNSET value (undefined) selects
// mock mode. (`!API_URL` would wrongly treat "" as unset and force the shipped
// production build into mock mode — the bug that showed canned data in deploy.)
export const usingMock = FORCE_MOCK || API_URL === undefined;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function sendChat(req: ChatRequest): Promise<ChatResponse> {
  if (usingMock) {
    return mockChat(req);
  }

  let res: Response;
  try {
    res = await fetch(`${API_URL}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  } catch (e) {
    throw new ApiError(
      `Could not reach the felix backend at ${API_URL}. Is it running? (${
        e instanceof Error ? e.message : String(e)
      })`,
    );
  }

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiError(
      `Backend returned ${res.status} ${res.statusText}${body ? `: ${body.slice(0, 300)}` : ""}`,
      res.status,
    );
  }

  return (await res.json()) as ChatResponse;
}

/**
 * Streaming twin of `sendChat`: POSTs to `/chat/stream` and drives `handlers`
 * from the Server-Sent Events the backend emits (evidence → deltas → done).
 *
 * EventSource can't POST a body, so we read the response body as a stream and
 * parse SSE frames by hand. Never throws — failures are delivered via
 * `handlers.onError` so the caller has one place to handle them.
 */
export async function sendChatStream(req: ChatRequest, handlers: StreamHandlers): Promise<void> {
  if (usingMock) {
    return mockChatStream(req, handlers);
  }

  let res: Response;
  try {
    res = await fetch(`${API_URL}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(req),
    });
  } catch (e) {
    handlers.onError(
      `Could not reach the felix backend at ${API_URL}. Is it running? (${
        e instanceof Error ? e.message : String(e)
      })`,
    );
    return;
  }

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    handlers.onError(
      `Backend returned ${res.status} ${res.statusText}${body ? `: ${body.slice(0, 300)}` : ""}`,
    );
    return;
  }
  if (!res.body) {
    handlers.onError("Streaming response had no body.");
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line. Dispatch each complete frame
      // and keep any trailing partial frame in the buffer for the next chunk.
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        dispatchFrame(frame, handlers);
      }
    }
  } catch (e) {
    handlers.onError(
      `Stream interrupted: ${e instanceof Error ? e.message : String(e)}`,
    );
  }
}

/** Browse the whole incident library (unranked, newest-first). */
export async function listIncidents(limit = 200): Promise<IncidentHit[]> {
  if (usingMock) return mockListIncidents();

  const res = await fetch(`${API_URL}/incidents?limit=${limit}`);
  if (!res.ok) {
    throw new ApiError(`Backend returned ${res.status} ${res.statusText}`, res.status);
  }
  const body = (await res.json()) as { incidents: IncidentHit[] };
  return body.incidents;
}

/** Semantic search over the incident library (CockroachDB VECTOR ranking). */
export async function searchIncidents(q: string, k = 12): Promise<IncidentHit[]> {
  if (usingMock) return mockSearchIncidents(q);

  const res = await fetch(`${API_URL}/incidents/search?q=${encodeURIComponent(q)}&k=${k}`);
  if (!res.ok) {
    throw new ApiError(`Backend returned ${res.status} ${res.statusText}`, res.status);
  }
  const body = (await res.json()) as { incidents: IncidentHit[] };
  return body.incidents;
}

/**
 * Record 👍/👎 feedback on a diagnosed incident. 👍 promotes it into recallable
 * memory (the backend embeds it); 👎 keeps it out. Keyed on the diagnosis's
 * `incident_id`. No-ops gracefully in mock mode.
 */
export async function submitFeedback(incidentId: string, helpful: boolean): Promise<FeedbackResponse> {
  if (usingMock) return mockSubmitFeedback(incidentId, helpful);

  const res = await fetch(`${API_URL}/incidents/${encodeURIComponent(incidentId)}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ helpful }),
  });
  if (!res.ok) {
    throw new ApiError(`Backend returned ${res.status} ${res.statusText}`, res.status);
  }
  return (await res.json()) as FeedbackResponse;
}

/** Parse one SSE frame (`event:` + one or more `data:` lines) and route it. */
function dispatchFrame(frame: string, handlers: StreamHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (dataLines.length === 0) return;

  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return; // ignore an unparseable frame rather than killing the stream
  }

  const d = data as Record<string, unknown>;
  if (event === "evidence") handlers.onEvidence?.(d.evidence as ChatResponse["evidence"]);
  else if (event === "delta") handlers.onDelta?.(String(d.text ?? ""));
  else if (event === "done") handlers.onDone(data as ChatResponse);
  else if (event === "error") handlers.onError(String(d.error ?? "stream error"));
}
