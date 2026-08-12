# felix — frontend

A React + TypeScript (Vite) chat UI for felix, the SRE incident-memory agent.
You type an alert; felix replies with a diagnosis and proposed steps, and the
right-hand panel shows the memory it recalled — similar past incidents, relevant
docs, recent code changes, and the upstream code-graph trace.

## Quick start

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

With no backend configured the app runs in **mock mode**: it returns canned
responses for felix's two planted puzzles (try the example chips, or any alert
mentioning "pool"/"exhausted" or "slow"/"latency"). A `mock mode` badge shows in
the header.

## Wiring up the real backend

The UI talks to the backend through exactly one file — `src/api/client.ts` —
which `POST`s to `${VITE_API_URL}/chat`. To go live:

1. Build a small HTTP wrapper around `IncidentResponder.diagnose` (FastAPI/Flask)
   that accepts a `ChatRequest` and returns a `ChatResponse`. Both shapes are
   defined in **`src/api/types.ts`** and mirror `src/models.py`:

   ```
   POST /chat
   body: { "alert": string, "origin_node"?: string, "k"?: number }
   200:  { "diagnosis": Diagnosis, "evidence": EvidencePacket }
   ```

   `Diagnosis` ≈ `src/models.py:Diagnosis`; `EvidencePacket` ≈
   `src/models.py:EvidencePacket` (incidents/docs/changes as `{item, distance}`,
   upstream as `{node, depth}`).

2. Point the frontend at it:

   ```bash
   cp .env.example .env.local
   # then set one of:
   #   VITE_API_URL=http://localhost:8000   (backend on its own port; enable CORS for the dev origin)
   #   VITE_API_URL=/api                    (use the Vite dev proxy in vite.config.ts — no CORS needed)
   ```

3. `npm run dev` again. The `mock mode` badge disappears once `VITE_API_URL` is
   set. Set `VITE_USE_MOCK=true` to force mock mode even with a URL configured.

## Layout

```
src/
  api/
    types.ts     # the request/response contract (mirrors src/models.py)
    client.ts    # the ONLY network seam: real fetch, or mock fallback
    mock.ts      # canned responses for the two planted puzzles (delete when live)
  components/
    AlertComposer.tsx   # alert input + optional origin-node advanced field
    ChatThread.tsx      # the message thread (user alerts + felix replies)
    DiagnosisCard.tsx   # renders one Diagnosis (summary, root cause, steps, citations)
    EvidencePanel.tsx   # the right panel: incidents/docs/changes/graph trace
  App.tsx        # state + two-column layout
  main.tsx       # React entry
  styles.css     # all styling (dark SRE-console theme)
```

## Scripts

- `npm run dev` — dev server with HMR
- `npm run build` — typecheck + production build to `dist/`
- `npm run preview` — serve the production build
- `npm run typecheck` — types only, no emit
