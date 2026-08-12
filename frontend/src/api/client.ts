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

import type { ChatRequest, ChatResponse } from "./types";
import { mockChat } from "./mock";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;
const FORCE_MOCK = import.meta.env.VITE_USE_MOCK === "true";

export const usingMock = FORCE_MOCK || !API_URL;

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
