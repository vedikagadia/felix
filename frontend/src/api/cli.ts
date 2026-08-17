/**
 * The CLI-panel seam. The terminal itself talks to the backend over a WebSocket
 * (a real PTY — see `src/api/terminal.py`), so this module only (a) reports
 * whether the terminal is available and whether `ccloud` is installed/authed on
 * the API host, and (b) derives the WS URL from VITE_API_URL.
 *
 * There's no meaningful mock: a terminal needs a real shell on a real host, so
 * in mock mode (no VITE_API_URL) the panel renders an explanatory placeholder
 * rather than a fake shell.
 */

import { usingMock } from "./client";
import type { CliStatus } from "./types";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;

/** The WebSocket URL for the PTY, or null in mock mode (no backend). */
export function cliWsUrl(): string | null {
  if (usingMock || !API_URL) return null;
  // http(s)://host:port → ws(s)://host:port/cli/ws
  const ws = API_URL.replace(/^http/i, "ws").replace(/\/+$/, "");
  return `${ws}/cli/ws`;
}

export async function fetchCliStatus(): Promise<CliStatus> {
  if (usingMock || !API_URL) {
    return {
      enabled: false,
      ccloud_installed: false,
      ccloud_path: null,
      account: null,
      cluster_id: null,
    };
  }
  const res = await fetch(`${API_URL}/cli/status`);
  if (!res.ok) throw new Error(`Backend returned ${res.status} ${res.statusText}`);
  return (await res.json()) as CliStatus;
}
