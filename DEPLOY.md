# DEPLOY.md — hosting felix on AWS (end to end)

The plan to get felix live on AWS for the hackathon judging period (~1 month),
at **~$0 of extra infra** beyond Fargate compute. Read this and approve it
before any deploy code is written — nothing here has been built yet; this is the
map.

> **Identity guardrail (applies to every command below).** This is a personal
> project on the `vedikagadia` GitHub / a personal AWS account. Never use a
> Salesforce identity, and never paste the CockroachDB password or the Gemini
> key into a chat — they live only in your terminal and in the task-def env
> block you fill in locally (never committed).

---

## 0. The shape of the deployment

**Two tasks run in the cloud** (down from three — web+watch are merged):

| Deployable | What it is | ECS shape | Public? |
|---|---|---|---|
| `felix-web` | `python -m src serve` — the API **+** the built React UI on :8000, **and** the CDC watcher on a background thread (`FELIX_RUN_WATCHER=1`) | service, **`desiredCount: 1` pinned** | yes — judges open this |
| `sample-checkout` | the demo target service judges click; has built-in latency + writes metrics | service, `desiredCount: 1` | yes — judges open this |

**One shared datastore:** a CockroachDB Cloud cluster (both tasks connect via
`DATABASE_URL`). The watcher (inside `felix-web`) reads the `metrics` table
`sample-checkout` writes into, over a CHANGEFEED — that's the live alert loop.

**Why web+watch are one task (decided).** They both load the ~1.5GB bge-large
model. Run as two separate processes that would be ~8GB (two copies) ≈ the cost
of two 4GB tasks — no savings. Instead the watcher runs as a **background thread
inside the `serve` process**, sharing the one in-memory model (`get_embedder()`
is a process singleton) → **one ~4GB task, ~$15/mo saved.** `felix-web` stays
**pinned at `desiredCount: 1`**: two watchers on the same changefeed would
double-fire alerts, so it must never scale past 1. (The standalone
`python -m src watch` role still exists for local dev / a future split; the
merged deploy doesn't use it.)

**Model choice (decided):** local embeddings (`EMBED_PROVIDER=local`, bge-large,
1024-dim) + Gemini free-tier key for reasoning (`LLM_PROVIDER=gemini`). **No
Bedrock.** The ~1.3GB embedding model is **baked into the Docker image at build
time** so a task restart never re-downloads it (Fargate local storage is wiped on
every restart).

**Networking (decided):** each public task gets a **public IP** (no load
balancer — saves ~$18/mo). A free **DuckDNS** name points at that IP and the
container **updates the record itself on boot**, so the link survives restarts.
Trade-off: **HTTP only, no HTTPS** (browsers show "Not secure"). See §7.

---

## 1. Prerequisites (what must exist before deploying)

You've done the first two already; listed for completeness.

1. **AWS account + CLI configured.** ✅ done — `felix-deployer`, account
   `287211515912`, region `us-east-1`. Verify: `aws sts get-caller-identity`.
2. **Docker installed and running locally** (to build + push the image).
   Verify: `docker version`.
3. **A CockroachDB Cloud cluster** — ⏳ *you're waiting on admin permission.*
   Requirements when you create it:
   - Put it in a **US region** (co-locate with `us-east-1` ECS to keep latency
     + egress low).
   - Must support the **VECTOR type + vector indexes** and **CHANGEFEED**. CRDB
     Cloud on a recent version (≥ v24.3) has both; the local node we test on is
     v26.2.5, so a current Cloud cluster matches. **Verify both after creating**
     (queries in §5.3) — if CHANGEFEED needs an enterprise flag on Cloud, we'll
     find out here, not at demo time.
   - Grab the connection string from the Console "Connect" dialog. It looks like
     `postgresql://<user>:<pw>@<host>:26257/felix?sslmode=verify-full`.
4. **A Gemini API key** — ✅ you have one (`GEMINI_API_KEY`). Free tier is fine
   for reasoning; note the same 10-req/day cap that bit the demo TTS does *not*
   apply the same way to text generation, but keep an eye on quota during a
   month of judging. If it becomes a problem, enabling billing is <$1.
5. **A DuckDNS account + token** — free. Sign in at duckdns.org (GitHub login),
   create two subdomains (e.g. `felix-demo`, `felix-checkout`), copy the single
   account token. Needed in §6.

> **Deploy order note:** `felix-web` can go up *before* the CockroachDB cluster
> exists **if you leave `FELIX_RUN_WATCHER` unset for that first smoke test** —
> the API opens DB connections lazily (per request), so it boots healthy with a
> placeholder `DATABASE_URL` and only errors when you actually chat. (The
> watcher, by contrast, connects on boot, so enable it only once the DB is
> ready.) So you can validate the whole AWS pipeline now and flip in the real
> `DATABASE_URL` + `FELIX_RUN_WATCHER=1` the moment your cluster is ready.
> `sample-checkout` genuinely needs the DB, so bring it up after.

---

## 2. Code changes (BUILT — this is the blast radius)

All additive; local dev is unchanged (the watcher-in-process is off unless
`FELIX_RUN_WATCHER=1`).

**Containerization**
- `Dockerfile` — one image for all roles. Base `python:3.12-slim`, multi-stage:
  a `node:20-slim` stage runs `npm ci && npm run build` → the runtime stage
  installs `requirements.txt`, **pre-downloads bge-large into `HF_HOME`** so
  first recall doesn't fetch 1.3GB, copies `src/`, `sample_project/`, `sql/`,
  `frontend/dist/`, then drops root (runs as `app`). ~2GB final image.
- `.dockerignore` — keeps `.venv/`, `.crdb-data/`, `docs/`, `demo/out/`,
  `**/node_modules/`, `*.pyc`, `.env`, `.git/` out of the build context.
- `docker/entrypoint.sh` — the **role switch** + DuckDNS self-update:
  - `ROLE=web`   → DuckDNS update → `python -m src serve --host 0.0.0.0 --port 8000` (the watcher rides along in-process when `FELIX_RUN_WATCHER=1`)
  - `ROLE=watch` → `python -m src watch` (standalone; not used by the merged deploy)
  - `ROLE=sample`→ DuckDNS update → `python -m sample_project.server`
  - DuckDNS update = one `curl` to `.../update?domains=$DUCKDNS_DOMAIN&token=$DUCKDNS_TOKEN&ip=` (token fed on stdin via `-K -`, never argv/logs), run only when `DUCKDNS_DOMAIN` is set.

**Source changes for the merged task**
- `src/clients/embedder/__init__.py` — `get_embedder()` is now an
  `lru_cache`'d **process singleton**. This is the linchpin: the watcher thread
  and every API request share ONE bge-large instance, so merging web+watch is
  one ~4GB task, not ~8GB. (Model still loads lazily on first embed.)
- `src/service/watcher.py` — extracted `build_watcher(conn, stream_conn)` (the
  wiring both the CLI and the API use) and added `BackgroundWatcher`: runs the
  watcher on a daemon thread with its OWN two DB connections, best-effort so it
  can never crash the web server; `stop()` tears it down on SIGTERM. Also wraps
  the `SET CLUSTER SETTING kv.rangefeed.enabled` in try/except for managed Cloud.
- `src/api/app.py` — a FastAPI `lifespan` starts `BackgroundWatcher` on boot
  **iff `FELIX_RUN_WATCHER` is set**, and stops it on shutdown. Off by default,
  so local `serve` is still a plain API.
- `src/cli.py` — `_cmd_watch` now calls the shared `build_watcher`; still traps
  SIGTERM for a clean standalone shutdown.

**The interactive sample service**
- `sample_project/server.py` — stdlib `http.server` wrapping `CheckoutAPI`:
  `GET /` (a "Run checkout" button), `POST /checkout` (runs the call graph +
  writes a `checkout_latency_ms` + `pool_in_use` row via `MetricRepository` and
  the shared `metric_shape`), `GET /health`. A `TrafficPrimer` daemon thread
  emits a sample every 0.5s so the watcher's window stays past `MIN_SAMPLES=30`
  — a judge's few clicks then tip p99 over threshold within seconds. Folded into
  the same task (no extra container/cost).
- `sample_project/metric_shape.py` — the latency/pool shape shared by `run.py`
  and `server.py` (extracted so both emit the identical series).

**Deploy scripts + config (`deploy/` dir)**
- `deploy/task-def-web.json` (merged web+watch, 4GB, `FELIX_RUN_WATCHER=1`) and
  `deploy/task-def-sample.json` (1GB) — ECS Fargate task defs. Secrets are
  **plain env vars** in the `environment` block (see §3.3); the checked-in files
  carry `REPLACE_WITH_…` placeholders you fill in locally before registering.
- `deploy/build-and-push.sh` — `docker build --platform linux/amd64` → ECR login
  → push. One image; both task defs reference `felix:latest`.

---

## 3. Manual AWS setup (one-time, you run these)

These are console/CLI steps only you can do (they create AWS resources under
your account). I'll give exact commands when we build; this is the checklist so
you know the shape and can eyeball the cost.

1. **ECR repository** (private Docker registry for the image):
   `aws ecr create-repository --repository-name felix`. **Cost:** ~$0 — you pay
   $0.10/GB-month for storage; one ~2GB image ≈ $0.20/mo.
2. **ECS cluster** (Fargate — just an orchestration namespace, no standing cost):
   `aws ecs create-cluster --cluster-name felix`.
3. **Secrets — plain env vars (decided, $0).** No Secrets Manager. The three
   secrets — `DATABASE_URL`, `GEMINI_API_KEY`, `DUCKDNS_TOKEN` — go directly in
   the task-def `environment` block. The checked-in `deploy/*.json` carry
   `REPLACE_WITH_…` placeholders; copy one to `deploy/<name>.local.json`
   (gitignored) and fill the real values there, then register THAT file — never
   commit it. Trade-off:
   the values are visible to anyone with `ecs:DescribeTaskDefinition` on your
   account (just you) and appear in the registered task def — acceptable for a
   personal single-user demo. (Secrets Manager would keep them out of the task
   def for ~$1.20/mo; declined for cost.)
4. **IAM roles** (two, both free):
   - *execution role* (`felix-ecs-execution`) — lets ECS pull from ECR + write
     logs. (No Secrets Manager read needed, since secrets are plain env vars.)
   - *task role* (`felix-ecs-task`) — the container's own AWS identity at
     runtime. With no-Bedrock, the app needs **no AWS API access at all** (Gemini
     + CRDB are both external), so this role is empty/minimal.
5. **Security group** — allow inbound `8000` (felix-web) and `8001` (sample) from
   `0.0.0.0/0` (public demo), plus outbound all (so the tasks reach CRDB Cloud +
   Gemini + DuckDNS).
6. **CloudWatch log group** `/ecs/felix` — free tier covers the volume; a month
   of two chatty-ish tasks is a few cents.

**Rough monthly cost:** Fargate compute dominates. Sizing (see §4): two tasks —
`felix-web` at 0.5 vCPU / 4GB and `sample` at 0.25 vCPU / 1GB, running 24/7 ≈
**$20–30/mo** at on-demand Fargate rates in us-east-1 (the merge saved a second
4GB task). Everything else (ECR ~$0.20, logs, DuckDNS, no ALB, no Secrets
Manager) is **under ~$1/mo combined**. If that Fargate number is too high for a
month, the lever is: stop the tasks when not demoing (the DuckDNS self-update
brings the links back on restart).

---

## 4. Task sizing (memory is the constraint)

The bge-large model + torch want real RAM. Fargate sizes (in the `deploy/*.json`):

| Task | vCPU | Memory | Why |
|---|---|---|---|
| `felix-web` (web + watcher) | 0.5 | **4 GB** | serves the API **and** runs the CDC watcher on a thread — **one** shared bge-large in memory (`get_embedder()` singleton). Both the request path and the watcher embed against the same model. |
| `sample-checkout` | 0.25 | 1 GB | no model — just HTTP + metric writes |

Both are legal Fargate vCPU/memory pairs (0.5 vCPU allows 1–4GB; 0.25 allows
0.5–2GB). **The merge** (web+watch in one task) is what makes this two 4GB-worth
of tasks instead of three: two *separate* processes would each load the model
(~8GB, ≈ two tasks' cost), so the watcher runs in-thread inside `serve` sharing
the one model. The cost: the watcher is no longer independently restartable —
restarting it means restarting the web task. Acceptable for a demo; `felix-web`
stays pinned at `desiredCount: 1` so the changefeed never has two consumers.

---

## 5. Deploy sequence (once code + cluster exist)

### 5.1 Build & push the image
```bash
./deploy/build-and-push.sh          # docker build → ECR (one image, ~2GB)
```

### 5.2 (optional) Smoke-test felix-web BEFORE the DB, watcher off
Register `deploy/task-def-web.json` with `FELIX_RUN_WATCHER` **removed** (or `0`)
and a placeholder `DATABASE_URL`, then:
```bash
aws ecs create-service ... --task-definition felix-web ...   # ROLE=web, watcher OFF
# → note the task's public IP; open http://<ip>:8000 → the UI loads, /health is green
```
This proves the whole pipeline (image, ECR pull, task launch, public IP, static
UI) **before** CockroachDB is ready. Skip this if the cluster is already up (it
is) and go straight to §5.3 with the watcher on.

### 5.3 With the CRDB Cloud cluster ready (it is — see MEMORY)
1. Load schema + seed into it (from your laptop, pointed at the Cloud URL):
   ```bash
   cockroach sql --url "$CLOUD_URL" -f sql/schema.sql
   cockroach sql --url "$CLOUD_URL" -f sql/seed_dump.sql   # 154 rows incl. local-provider vectors
   ```
   The committed `seed_dump.sql` carries **local-embedder** vectors, which is
   exactly what `EMBED_PROVIDER=local` produces at query time — so recall works
   with **no re-seeding**. (Verified locally.)
2. **Verify VECTOR + CHANGEFEED on Cloud** — CHANGEFEED is already confirmed
   working on this Basic cluster; re-run the vector query as a sanity check:
   ```sql
   SELECT title FROM incidents ORDER BY embedding <-> (SELECT embedding FROM incidents LIMIT 1) LIMIT 1;
   ```
3. Copy `deploy/task-def-web.json` → `deploy/task-def-web.local.json`, fill the
   real `DATABASE_URL` + `GEMINI_API_KEY` + `DUCKDNS_TOKEN` (replacing the
   `REPLACE_WITH_…` placeholders), keep `FELIX_RUN_WATCHER=1`, register it, and
   (re)deploy:
   ```bash
   cp deploy/task-def-web.json deploy/task-def-web.local.json   # edit secrets in the .local copy
   aws ecs register-task-definition --cli-input-json file://deploy/task-def-web.local.json
   aws ecs update-service --cluster felix --service felix-web --force-new-deployment
   ```

### 5.4 Bring up sample-checkout
Same pattern — fill the placeholders in a `.local.json` copy, then:
```bash
cp deploy/task-def-sample.json deploy/task-def-sample.local.json   # edit secrets
aws ecs register-task-definition --cli-input-json file://deploy/task-def-sample.local.json
aws ecs create-service ... --task-definition felix-sample --desired-count 1
```

### 5.5 Smoke test the live demo
- Open `http://felix-demo.duckdns.org` → chat, submit the code-only alert → the
  graph-traced diagnosis renders (same as local).
- Open `http://felix-checkout.duckdns.org` → click "Run checkout" a few times →
  within seconds the watcher trips → the alert banner appears in felix-web.

---

## 6. DuckDNS wiring (the free stable-name step)

1. At duckdns.org: create `felix-demo` and `felix-checkout`; copy the token.
2. Put the token in each task def's env block as `DUCKDNS_TOKEN` (plain env var,
   §3.3); `DUCKDNS_DOMAIN` is already set per task (`felix-demo` for web,
   `felix-checkout` for sample).
3. The entrypoint (`docker/entrypoint.sh`) runs on boot, before starting the
   server:
   ```sh
   [ -n "$DUCKDNS_DOMAIN" ] && curl -fsS \
     "https://www.duckdns.org/update?domains=$DUCKDNS_DOMAIN&token=$DUCKDNS_TOKEN&ip="
   ```
   DuckDNS uses the caller's source IP (the task's public IP) when `ip=` is
   empty. On every restart the name re-points itself — the link never goes stale
   for more than the few seconds a restart takes.

---

## 7. Known trade-offs & risks (so nothing surprises you at demo time)

- **HTTP only, no HTTPS.** DuckDNS maps name→IP; it doesn't terminate TLS. Judges
  get `http://…` and a "Not secure" label. If a judge's browser hard-blocks HTTP
  (rare), the fallback is a Cloudflare Tunnel sidecar (needs a domain + a second
  container — the option you declined for simplicity). We can add it later if
  needed without changing app code.
- **Restart window.** When Fargate replaces a task (redeploy, crash, or AWS
  maintenance — expect maybe once or twice over a month), there are a few seconds
  where the DuckDNS name points at the dead IP until the new task's entrypoint
  updates it. Fine for a demo; not for production.
- **Watcher priming.** `MIN_SAMPLES=30` means the sample service's background
  self-traffic must have been running ~15–30s before a judge's clicks can trip
  the alert. The task starts that loop on boot, so by the time a judge arrives
  it's primed.
- **Gemini quota over a month.** Free-tier text limits are higher than the TTS
  cap but not infinite. If judges hammer it, enable Gemini billing (<$1) — one
  toggle, no redeploy (it reads the same key).
- **Watcher restart is coupled to web.** Because the watcher runs in-thread
  inside `serve`, you can't bounce it alone — restarting the watcher means a new
  `felix-web` deployment. Fine for a demo; the standalone `watch` role is still
  in the image if you ever want to split them back out.
- **Cost.** Fargate 24/7 for two tasks is the real spend (~$20–30/mo). If that's
  too much for the judging window, stop the services between demos and restart
  them (the DuckDNS self-update means the links come back on their own).
- **CHANGEFEED on Cloud** — confirmed working on the Basic cluster (no enterprise
  license needed). Everything else is verified against the local v26.2.5 node.

---

## 8. Decisions (all resolved)

1. **Sizes/cost** — ✅ **merge web+watch** into one 4GB task (thread-shared
   model), + a 1GB sample task ≈ $20–30/mo. Watcher not independently
   restartable (accepted).
2. **Secrets** — ✅ **plain env vars** ($0), in the task-def `environment` block.
   No Secrets Manager.
3. **Sample page** — a bare "Run checkout" button (shipped in
   `sample_project/server.py`). Can be dressed up later if the demo wants it.
