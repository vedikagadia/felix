/**
 * Multi-project seam: the active-project store + the /projects endpoints.
 *
 * felix's memory is namespaced by a project slug (a `project` column on every
 * top-level table). This module holds the *active* project (persisted in
 * localStorage so a reload keeps your place) and exposes `appendProject(url)`,
 * which the other seams (client/metrics/alerts) run every scoped URL through so
 * one switch in the header re-scopes the whole app.
 *
 * The built-in demo is `sample`; onboarded repos/dirs get their own slug via
 * `POST /projects/onboard`. Mock mode (no backend) synthesizes a project list
 * and no-ops onboarding so the switcher is demoable offline.
 */

import { usingMock } from "./client";
import type { OnboardRequest, OnboardResult, Project } from "./types";

const API_URL = import.meta.env.VITE_API_URL as string | undefined;

/** The built-in demo project — felix's seeded sample_project corpus. */
export const DEFAULT_PROJECT = "sample";

const STORAGE_KEY = "felix:project";

/** The project the UI is currently scoped to (localStorage-backed). */
export function getActiveProject(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_PROJECT;
  } catch {
    return DEFAULT_PROJECT;
  }
}

/** Persist the active project. Callers re-key the pages so they re-fetch. */
export function setActiveProject(slug: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, slug);
  } catch {
    // ignore storage failures (private mode, quota) — the switch just won't persist
  }
}

/**
 * Append the active project as a `?project=` (or `&project=`) query param.
 * Every project-scoped seam runs its URL through this — the backend reads
 * `project` as a query param uniformly for both GET and POST, so this works for
 * fetch bodies and EventSource URLs alike. NOT for global endpoints
 * (`/metrics/config`, `/db/*`, `/cli/*`), which aren't project-scoped.
 */
export function appendProject(url: string): string {
  const project = getActiveProject();
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}project=${encodeURIComponent(project)}`;
}

/** Every onboarded project (the demo first, then newest-first). */
export async function fetchProjects(): Promise<Project[]> {
  if (usingMock) return mockListProjects();

  const res = await fetch(`${API_URL}/projects`);
  if (!res.ok) throw new Error(`Backend returned ${res.status} ${res.statusText}`);
  const body = (await res.json()) as { projects: Project[] };
  return body.projects;
}

/**
 * Onboard a project (local path or git URL). Blocking on the backend (clone +
 * parse + embed + ingest), so the caller should show a spinner. Resolves with
 * the per-source row counts, or throws on a bad source (missing path, clone
 * failure, or the reserved `sample` slug → HTTP 400).
 */
export async function onboardProject(req: OnboardRequest): Promise<OnboardResult> {
  if (usingMock) return mockOnboardProject(req);

  const res = await fetch(`${API_URL}/projects/onboard`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Backend returned ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as OnboardResult;
}

// ── mock (no backend) ─────────────────────────────────────────────────────────

function mockListProjects(): Promise<Project[]> {
  return Promise.resolve([
    {
      id: DEFAULT_PROJECT,
      display_name: "Checkout demo (sample)",
      source_kind: null,
      source_ref: null,
      created_at: "2026-08-01T00:00:00Z",
      last_synced: "2026-08-01T00:00:00Z",
    },
  ]);
}

function mockOnboardProject(req: OnboardRequest): Promise<OnboardResult> {
  const slug = (req.project || req.name || "demo-project")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return Promise.resolve({
    project: slug,
    display_name: req.name || slug,
    source_kind: /^(https?:\/\/|git@)/i.test(req.source) ? "git" : "local",
    source_ref: req.source,
    counts: { code: 0, changes: 0, docs: 0, runbooks: 0 },
  });
}
