# DEPLOY.md — hosting felix on AWS (end to end)

The plan to get felix live on AWS for the hackathon judging period (~1 month),
at the **lowest defensible Fargate cost** (Spot, one task, no load balancer).
Read this and approve it before any deploy code is written; the runbook below
is written to be run in order.

> **Identity guardrail (applies to every command below).** This is a personal
> project on the `vedikagadia` GitHub / a personal AWS account. Never use a
> Salesforce identity, and never paste the CockroachDB password, the Gemini
> key, or the MCP API key into a chat — they live only in Secrets Manager/SSM
> and your terminal (never committed, never in a task-def `environment` block).

> **Citation convention.** Every AWS CLI command below is grounded in an
> official AWS doc, cited inline as `# doc: <url>`. Where a command isn't
> covered by a doc we've actually verified, it's marked `# TODO verify` instead
> of guessed — check the AWS CLI reference before running it.

---

## 0. The shape of the deployment

**One task runs in the cloud.** The web task and the watcher were already
merged (they share one in-memory embedding model); the sample traffic driver
is now folded into the same task too, so there is no separate sample
task/service at all:

| Deployable | What it is | ECS shape | Public? |
|---|---|---|---|
| `felix-web` | `python -m src serve` — the API **+** the built React UI on :8000, **the CDC watcher** on a background thread (`FELIX_RUN_WATCHER=1`), **and** the sample checkout traffic driver on a background thread (`FELIX_RUN_SAMPLE=1`) | service, **`desiredCount: 1` pinned** | yes — judges open this |

**One shared datastore:** a CockroachDB Cloud cluster (`DATABASE_URL`). The
in-process traffic driver writes `checkout_latency_ms` / `payment_latency_ms`
/ `enqueue_latency_ms` rows into `metrics`; the in-process watcher holds a
CHANGEFEED on that same table — so the alert loop fires with **zero manual
steps** for a judge. `docker/entrypoint.sh` now only switches between two
roles, `web` and `watch` (the standalone `watch` role is for local dev / a
future split; the merged deploy uses `web` only).

**Why everything piles into one task (decided).** The watcher and the API
already share the ~1.5GB bge-large model (`get_embedder()` is a process
singleton) — running it as a second process would double that memory for no
reason. The sample traffic driver needs no model at all, just a DB connection,
so adding it as a third background thread costs threads, not gigabytes.
Net result: **one task, not two or three.** `felix-web` stays pinned at
`desiredCount: 1` — two watchers on the same changefeed would double-fire
alerts, so it must never scale past 1 (this also matters for the Spot tradeoff
below).

**Model choice (unchanged):** local embeddings (`EMBED_PROVIDER=local`,
bge-large, 1024-dim) + Gemini free-tier key for reasoning (`LLM_PROVIDER=gemini`).
The ~1.3GB embedding model is baked into the Docker image at build time so a
task restart never re-downloads it (Fargate's local storage is wiped on every
restart).

**Compute cost (decided): Fargate Spot, single task.** Instead of on-demand
`--launch-type FARGATE`, the service is created with a **capacity-provider
strategy** that puts 100% of its tasks on `FARGATE_SPOT` (~70% cheaper than
on-demand). The explicit tradeoff, spelled out because it's easy to miss:

> **A single-task `FARGATE_SPOT` service does not fall back to on-demand.** If
> AWS reclaims the Spot capacity, the task stops and the service **stays down
> until Spot capacity is available again** — there's no second task to absorb
> the interruption, and Fargate does not silently substitute on-demand
> capacity for you. Spot interruption itself is graceful (a 2-minute warning
> delivered as `SIGTERM` to the task, plus an EventBridge event), but recovery
> is not guaranteed on a timeline. For a $-conscious demo this is an accepted
> tradeoff, not an oversight — see §6 for the mitigation if it becomes a
> problem during judging.

**Networking (unchanged):** the task gets a **public IP** (no load balancer —
saves ~$18/mo). A free **DuckDNS** name points at that IP and the container
updates the record itself on boot, so the link survives restarts. Trade-off:
**HTTP only, no HTTPS** (browsers show "Not secure"). See §7.

---

## 1. Prerequisites (what must exist before deploying)

1. **AWS account + CLI configured.** ✅ done — `felix-deployer`, account
   `287211515912`, region `us-east-1`.
2. **Docker installed and running locally** (to build + push the image).
3. **A CockroachDB Cloud cluster** carrying the VECTOR + CHANGEFEED
   capability (see the existing project docs for how this was provisioned).
   Connection string from the Console "Connect" dialog:
   `postgresql://<user>:<pw>@<host>:26257/felix?sslmode=verify-full`.
4. **A Gemini API key** (`GEMINI_API_KEY`) — free tier is fine for reasoning;
   keep an eye on quota during a month of judging (see §7).
5. **CockroachDB Cloud Managed MCP Server details**, from the Console "Connect
   via MCP" dialog: `CRDB_MCP_URL` (`https://cockroachlabs.cloud/mcp`) and
   `CRDB_MCP_CLUSTER_ID`. Optionally a service-account bearer token
   (`CRDB_MCP_API_KEY`) if the org has issued one — **this is what lets the
   DB-overview tab work headlessly on the deployed task**, with no browser OAuth
   consent available on a headless Fargate task. Without it, `GET /db/overview`
   degrades to `{connected:false}` rather than failing the deploy — it's an
   enhancement, not a hard blocker.
6. **A DuckDNS account + token** — free. Sign in at duckdns.org, create one
   subdomain (e.g. `felix-demo`), copy the account token.

> **Deploy order note:** `felix-web` can go up *before* the CockroachDB
> cluster exists if you leave `FELIX_RUN_WATCHER` and `FELIX_RUN_SAMPLE` unset
> for that first smoke test — the API opens DB connections lazily (per
> request), so it boots healthy with a placeholder `DATABASE_URL` and only
> errors when you actually chat. The watcher and the traffic driver both
> connect to the DB on boot, so enable them only once the cluster is ready.

---

## 2. Code changes (blast radius — being finalized alongside this doc)

**Containerization**
- `Dockerfile` — one image, multi-stage (`node:20-slim` builds the React
  bundle; the runtime stage installs `requirements.txt`, pre-downloads
  bge-large into `HF_HOME` so first recall doesn't fetch 1.3GB, copies `src/`,
  `sample_project/`, `sql/`, `frontend/dist/`, drops root).
- `docker/entrypoint.sh` — the role switch is now just **two** roles:
  - `ROLE=web`   → DuckDNS self-update → `python -m src serve --host 0.0.0.0 --port 8000`
    (the watcher rides along in-process when `FELIX_RUN_WATCHER=1`; the sample
    traffic driver rides along in-process when `FELIX_RUN_SAMPLE=1`)
  - `ROLE=watch` → `python -m src watch` (standalone; not used by the merged deploy)
  - There is **no** `ROLE=sample` and **no** `sample_project.server` — the
    traffic driver that used to be a standalone HTTP service is now a
    background thread inside the `web` role, gated by `FELIX_RUN_SAMPLE`.

**Source changes for the merged task**
- `src/clients/embedder/__init__.py` — `get_embedder()` is an `lru_cache`'d
  process singleton, so the watcher thread, the sample-traffic thread, and
  every API request share one bge-large instance.
- `src/service/watcher.py` — `BackgroundWatcher` runs the watcher on a daemon
  thread with its own DB connections, best-effort so it can never crash the
  web server.
- `src/api/app.py` — the FastAPI `lifespan` starts `BackgroundWatcher` iff
  `FELIX_RUN_WATCHER` is set, and starts the sample traffic driver thread iff
  `FELIX_RUN_SAMPLE` is set, stopping both on shutdown. **Both are off by
  default**, so local `serve` is unchanged unless you opt in — `FELIX_RUN_SAMPLE`
  in particular defaults to unset in local dev; it's a deploy-only convenience
  so the CDC alert self-fires for judges with no manual "go click things"
  step.
- `sample_project/run.py` — the traffic driver logic (calls
  `CheckoutHandler.process` in a loop with the timing probe attached) now runs
  either as the standalone CLI driver (local dev, `python -m sample_project.run`)
  or as the in-process background thread the web task starts under
  `FELIX_RUN_SAMPLE=1` — same code path, two callers.

**Deploy scripts + config (`deploy/` dir)**
- `deploy/task-def-web.json` — the **only** task def now (4GB, `FELIX_RUN_WATCHER=1`,
  `FELIX_RUN_SAMPLE=1`). There is no `deploy/task-def-sample.json` anymore.
  Secrets are **Secrets Manager / SSM references** in a `secrets` block, not
  plaintext `environment` entries — see §3 step 5 and step 7 for exactly which
  keys move where.
- `deploy/build-and-push.sh` — `docker build --platform linux/amd64` → ECR
  login → push. One image; the one task def references `felix:latest`.

---

## 3. Task sizing

| Task | vCPU | Memory | Why |
|---|---|---|---|
| `felix-web` (web + watcher + sample driver) | 0.5 | **4 GB** | bge-large (~1.5GB resident) + two background threads (CDC watcher, sample traffic driver) sharing the one model singleton. |

`cpu=512` (0.5 vCPU) is a valid Fargate pairing for 1–4GB of memory; `4096`
(4GB) is the top of that range and is what the model needs headroom for. This
is the **least-cost sizing that still fits the model** — dropping to 2GB or
3GB risks an OOM kill under `sentence-transformers` + torch; going to `cpu=1024`
would allow more memory but there's no CPU-bound reason to pay for it here.
(Doc: [task-cpu-memory-error](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html))

Since there is no longer a separate sample task, this also means **no second
1GB task cost** compared to the earlier three-task design — the whole demo is
one Fargate task.

---

## 4. Deploy runbook (ordered, one-time steps + the repeatable build/deploy)

Run these in order. Steps 1–6 are one-time account setup; step 7 onward is
what you re-run on every image update.

### Step 1 — ECR: create the repo, then build + push
```bash
# one-time
aws ecr create-repository --repository-name felix --region us-east-1
# doc: https://docs.aws.amazon.com/AmazonECR/latest/userguide/getting-started-cli.html

# repeatable — deploy/build-and-push.sh wraps the next four commands:
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin 287211515912.dkr.ecr.us-east-1.amazonaws.com
# doc: https://docs.aws.amazon.com/AmazonECR/latest/userguide/getting-started-cli.html
docker build --platform linux/amd64 -t felix:latest .
docker tag felix:latest 287211515912.dkr.ecr.us-east-1.amazonaws.com/felix:latest
docker push 287211515912.dkr.ecr.us-east-1.amazonaws.com/felix:latest
# doc: https://docs.aws.amazon.com/AmazonECR/latest/userguide/getting-started-cli.html
```
URI shape: `{account-id}.dkr.ecr.{region}.amazonaws.com/{repo}:{tag}` — matches
`deploy/build-and-push.sh` as written; `docker login` needs the literal
`--username AWS`.

### Step 2 — CloudWatch log group
`deploy/task-def-web.json`'s `logConfiguration` does **not** set
`awslogs-create-group` (it defaults to `false`), and the managed
`AmazonECSTaskExecutionRolePolicy` from step 3 does **not** grant
`logs:CreateLogGroup` — so if `/ecs/felix` doesn't already exist, the first
task launch fails trying to create it. Rather than widen the execution role
with a log-group-creation permission it doesn't otherwise need, pre-create the
group yourself, once, before registering the task def:
```bash
aws logs create-log-group --log-group-name /ecs/felix --region us-east-1
# doc: https://docs.aws.amazon.com/cli/latest/reference/logs/create-log-group.html

aws logs put-retention-policy --log-group-name /ecs/felix --retention-in-days 14
# doc: https://docs.aws.amazon.com/cli/latest/reference/logs/put-retention-policy.html
```
The retention policy caps log storage cost — without it, CloudWatch Logs keeps
everything forever by default. Do this **before** step 7
(`register-task-definition`) and step 9 (`create-service`), since the task
will fail to start if the group isn't there yet.

```json
"logConfiguration": {
  "logDriver": "awslogs",
  "options": {
    "awslogs-group": "/ecs/felix",
    "awslogs-region": "us-east-1",
    "awslogs-stream-prefix": "web"
  }
}
```
This is what `deploy/task-def-web.json` uses — no `awslogs-create-group`, by
design, so the execution role stays minimal. Doc:
[using_awslogs](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html),
[specify-log-config](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/specify-log-config.html)

### Step 3 — IAM execution role (+ the extra policy secrets reads need)
The **execution role** is what ECS itself assumes to pull the image and ship
logs — it is *not* the app's own AWS identity (that's the optional task role,
step 4).

```bash
aws iam create-role --role-name ecsTaskExecutionRole \
  --assume-role-policy-document file://ecs-tasks-trust-policy.json
# doc: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html

aws iam attach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
# doc: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html
```

`ecs-tasks-trust-policy.json` (create this file locally — it is not, and
should not be, checked into the repo):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ecs-tasks.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

**The managed `AmazonECSTaskExecutionRolePolicy` does NOT grant permission to
read Secrets Manager secrets or SSM parameters** — that's a separate,
additional grant you must attach yourself, because §3.5 of this runbook moves
`DATABASE_URL`, `GEMINI_API_KEY`, `DUCKDNS_TOKEN`, and `CRDB_MCP_API_KEY` out of
plaintext `environment` and into a `secrets` block (Secrets Manager, referenced
by ARN). Without this extra policy, the task fails to start with a
`ResourceInitializationError` pulling secrets.

**These 4 Resource ARNs must exactly match the 4 `valueFrom` ARNs in
`deploy/task-def-web.json`'s `secrets` block** — Secrets Manager names are
case-sensitive, so `felix/DATABASE_URL` (task def) and `felix/database-url`
(a mismatched policy or `create-secret` call) are two different resources; a
mismatch here is a `ResourceInitializationError` at task launch, not an IAM
403 you'd notice earlier.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/DATABASE_URL",
        "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/GEMINI_API_KEY",
        "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/DUCKDNS_TOKEN",
        "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/CRDB_MCP_API_KEY"
      ]
    }
  ]
}
```
(add `"kms:Decrypt"` on the CMK's ARN too if the secrets are encrypted with a
customer-managed key rather than the default `aws/secretsmanager` key)

```bash
aws iam put-role-policy --role-name ecsTaskExecutionRole \
  --policy-name felix-read-secrets \
  --policy-document file://felix-read-secrets-policy.json
```
Doc: [put-role-policy](https://docs.aws.amazon.com/cli/latest/reference/iam/put-role-policy.html)

If you use SSM Parameter Store instead of Secrets Manager for any of these,
the equivalent grant is `ssm:GetParameters` (+ `secretsmanager:GetSecretValue`
too if the parameter itself references a secret, + `kms:Decrypt` if a CMK is
involved) — same doc.

### Step 4 — Task role (optional)
Only needed if the app makes its own AWS API calls at runtime (it currently
doesn't — Gemini and CockroachDB Cloud are both external to AWS). Create it
the same way as the execution role, trusting `ecs-tasks.amazonaws.com`, with
its own (currently empty/minimal) permissions policy, if/when that changes
(e.g. a future Bedrock switch).
Doc: [task-iam-roles](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html)

### Step 5 — Put the actual secret values in Secrets Manager

**Name case matters.** Secrets Manager names are case-sensitive, and
`deploy/task-def-web.json`'s `secrets` block references these 4 names
verbatim (UPPER_SNAKE, matching the env var each one becomes): `felix/DATABASE_URL`,
`felix/GEMINI_API_KEY`, `felix/DUCKDNS_TOKEN`, `felix/CRDB_MCP_API_KEY`. Create
them with those exact names — a lower-kebab name here (`felix/database-url`)
would create a *different* secret the task def can't find, and the task fails
at launch with `ResourceInitializationError`.

**Don't put real secret values inline on the command line** — they'd land in
your shell history. Put each value in a gitignored local file instead and pass
`--secret-string file://...`:
```bash
# create a gitignored file per secret, e.g.:
echo -n 'postgresql://<user>:<pw>@<host>:26257/felix?sslmode=verify-full' > secret-database-url.txt
# (never commit these — add secret-*.txt / secret*.json to .gitignore)

aws secretsmanager create-secret --name felix/DATABASE_URL --secret-string file://secret-database-url.txt
aws secretsmanager create-secret --name felix/GEMINI_API_KEY --secret-string file://secret-gemini-api-key.txt
aws secretsmanager create-secret --name felix/DUCKDNS_TOKEN --secret-string file://secret-duckdns-token.txt
aws secretsmanager create-secret --name felix/CRDB_MCP_API_KEY --secret-string file://secret-crdb-mcp-api-key.txt
```
Doc: [secretsmanager create-secret](https://docs.aws.amazon.com/cli/latest/reference/secretsmanager/create-secret.html)
— AWS recommends against inline `--secret-string "..."` values since they can
be captured in shell history; use `file://` (or a JSON file, same flag) instead.

Whichever exact command you use, the important part — grounded in the
register-task-definition doc — is the **shape** of what goes in the task def:
a `secrets` array entry per credential, each with the full ARN of the secret
in `valueFrom`:
```json
"secrets": [
  { "name": "DATABASE_URL",     "valueFrom": "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/DATABASE_URL" },
  { "name": "GEMINI_API_KEY",   "valueFrom": "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/GEMINI_API_KEY" },
  { "name": "DUCKDNS_TOKEN",    "valueFrom": "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/DUCKDNS_TOKEN" },
  { "name": "CRDB_MCP_API_KEY", "valueFrom": "arn:aws:secretsmanager:us-east-1:287211515912:secret:felix/CRDB_MCP_API_KEY" }
]
```
Doc: [register-task-definition](https://docs.aws.amazon.com/cli/latest/reference/ecs/register-task-definition.html)
— "Docs warn against plaintext secrets in `environment`"; this is exactly the
credential class that warning is about.

### Step 6 — Security group
First create the group itself (it doesn't exist yet — this is what produces
the `sg-xxxx` id used everywhere below and in step 9's `create-service`):
```bash
aws ec2 create-security-group --group-name felix-web \
  --description "felix web task" --vpc-id vpc-xxxx
# doc: https://docs.aws.amazon.com/cli/latest/reference/ec2/create-security-group.html
```
This returns a `GroupId` — that's your `sg-xxxx`. `vpc-xxxx` must be the same
VPC as the public subnet used in step 9's `--network-configuration`.

Then open inbound `8000`, outbound all (so the task can reach CockroachDB
Cloud, Gemini, DuckDNS, and the CockroachDB Managed MCP Server):
```bash
aws ec2 authorize-security-group-ingress --group-id sg-xxxx \
  --protocol tcp --port 8000 --cidr 0.0.0.0/0
# doc: https://docs.aws.amazon.com/cli/latest/reference/ec2/authorize-security-group-ingress.html
```

> **Security note — `0.0.0.0/0` exposes the API to the entire internet with NO
> app-level authentication.** felix has no auth layer in front of `/chat`,
> `/db/overview`, or (if enabled) `WS /cli/ws` — anyone who finds the IP/DuckDNS
> name can use it. Prefer scoping `--cidr` to your own IP (`<your-ip>/32`) plus
> any judge IPs you know ahead of time, rather than `0.0.0.0/0`, and widen it
> only for the judging window if you must. This is a known limitation — see
> the security callout in §6.

> **CLI-panel security decision point — see §6 below before opening this SG
> broadly.** The security group operates at the port level (8000, all
> protocols on it), and `WS /cli/ws` lives on that same port alongside the
> API — so the security group **cannot** selectively block just the terminal
> route. Read §6 before deciding how to handle this for the public deploy.

### Step 7 — register-task-definition
```bash
aws ecs register-task-definition --cli-input-json file://deploy/task-def-web.local.json
```
Doc: [register-task-definition](https://docs.aws.amazon.com/cli/latest/reference/ecs/register-task-definition.html)

Required for Fargate: `--family`, `--container-definitions`,
`--requires-compatibilities FARGATE`, `--network-mode awsvpc` (mandatory on
Fargate), task-level `cpu`/`memory` (§3's `512`/`4096`). `--execution-role-arn`
is functionally required (ECR pull, CloudWatch logs, secrets injection);
`--task-role-arn` is optional (the app's own AWS calls — currently none).
`environment` entries are `{"name","value"}` plain pairs; `secrets` entries
are `{"name","valueFrom": "<ARN>"}` (step 5). The env split for
`deploy/task-def-web.json`:

| Var | Where | Why |
|---|---|---|
| `ROLE` | `environment` | not sensitive — `web` |
| `FELIX_RUN_WATCHER` | `environment` | not sensitive — `1` |
| `FELIX_RUN_SAMPLE` | `environment` | not sensitive — `1`; runs the traffic driver in-process so the CDC alert self-fires for judges with no manual step. **Off by default in local dev** — this is a deploy-only convenience flag. |
| `EMBED_PROVIDER` / `LLM_PROVIDER` | `environment` | not sensitive |
| `DUCKDNS_DOMAIN` | `environment` | not sensitive — a hostname, not a credential |
| `CRDB_MCP_URL` | `environment` | not sensitive — a public endpoint URL |
| `CRDB_MCP_CLUSTER_ID` | `environment` | not sensitive — an identifier, not a credential; sent as the `mcp-cluster-id` header |
| `FELIX_CLI_ENABLED` | `environment` | not sensitive — set `true` so judges can use the interactive CLI panel. This turns ON the RCE shell knowingly; see §6 for the accepted-risk rationale + teardown guardrails |
| `DATABASE_URL` | **`secrets`** | credential (DB password embedded in the URL) |
| `GEMINI_API_KEY` | **`secrets`** | credential |
| `CRDB_MCP_API_KEY` | **`secrets`** | credential — putting this in `secrets` (rather than leaving it unset and falling back to OAuth) is what **unblocks the DB-overview tab running headlessly**: a Fargate task has no browser to complete the one-time OAuth consent, so without a bearer token `GET /db/overview` will always degrade to `{connected:false}` on a deployed task. |
| `DUCKDNS_TOKEN` | **`secrets`** | credential — treated the same as the others for consistency, even though it's lower-blast-radius than a DB password |

Fargate cpu/memory must be one of the valid pairs — `512` (0.5 vCPU) allows
`{1024, 2048, 3072, 4096}` MiB; we use `4096`. Doc:
[task-cpu-memory-error](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html)

### Step 8 — Create the cluster, then associate Fargate Spot capacity providers
```bash
aws ecs create-cluster --cluster-name felix
# doc: https://docs.aws.amazon.com/cli/latest/reference/ecs/create-cluster.html

aws ecs put-cluster-capacity-providers --cluster felix \
  --capacity-providers FARGATE FARGATE_SPOT \
  --default-capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1
```
Doc: [put-cluster-capacity-providers](https://docs.aws.amazon.com/cli/latest/reference/ecs/put-cluster-capacity-providers.html)

**This call is declarative, not additive** — you must re-list *every*
capacity provider you want associated on *every* call, or the ones you omit
get disassociated from the cluster. `FARGATE` and `FARGATE_SPOT` are globally
available (no `create-capacity-provider` step needed, unlike EC2-backed
capacity providers).

### Step 9 — create-service with the Spot strategy
```bash
aws ecs create-service --cluster felix --service-name felix-web \
  --task-definition felix-web:1 --desired-count 1 \
  --capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1,base=0 \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxxx],securityGroups=[sg-xxxx],assignPublicIp=ENABLED}"
```
Doc: [create-service](https://docs.aws.amazon.com/cli/latest/reference/ecs/create-service.html)

- **`--capacity-provider-strategy` and `--launch-type` are mutually
  exclusive — never pass both.** This is the whole point of switching to Spot:
  do not leave a stray `--launch-type FARGATE` on this command.
- `weight` (0–1000) sets the relative proportion of tasks once `base` is
  satisfied; `base` (only *one* provider in the strategy may set it) is the
  minimum task count that must run on that provider first. At least one
  provider in the strategy must have `weight > 0`. Here there's only one
  provider (`FARGATE_SPOT`) and one task, so `base=0`/`weight=1` simply means
  "run it on Spot." Doc:
  [fargate-capacity-providers](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html)
- **`assignPublicIp=ENABLED` is only reachable if the subnet is public** — its
  route table must have a `0.0.0.0/0` route to an internet gateway. That's a
  **VPC prerequisite**, not an ECS setting; if you put this task in a private
  subnet, the public IP is assigned but unreachable. Doc:
  [task-networking-awsvpc](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-networking-awsvpc.html)
- **One caveat on `assignPublicIp` you may see elsewhere in the docs:** there's
  a line in the ECS docs that `assignPublicIp` must be `DISABLED` when the
  service's `deploymentController` is set to `ECS` (the "external"/blue-green
  edge case) — that does **not** apply here. This service uses the **default
  rolling-update deployment controller** (nothing about `deploymentController`
  is set on the `create-service` call above), and a standard
  `assignPublicIp=ENABLED` is fine under the default controller. Doc:
  [create-service](https://docs.aws.amazon.com/cli/latest/reference/ecs/create-service.html)
  (read the `assignPublicIp` and `deploymentController` parameter descriptions
  together if in doubt).
- `subnets` (required, max 16, all in the same VPC) and `securityGroups`
  (max 5) both come from step 6.

Redeploying after a new image push (not a fresh `create-service`):
```bash
aws ecs update-service --cluster felix --service felix-web --force-new-deployment
# doc: https://docs.aws.amazon.com/cli/latest/reference/ecs/update-service.html
```

### Step 10 — Verify
- `aws ecs describe-services --cluster felix --services felix-web`
  (doc: https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-services.html) /
  `aws ecs list-tasks --cluster felix`
  (doc: https://docs.aws.amazon.com/cli/latest/reference/ecs/list-tasks.html) —
  useful for confirming the task reached
  `RUNNING` and which capacity provider it actually landed on.
- Note its public IP (task ENI), open `http://<ip>:8000/health` — should be
  green.
- Point DuckDNS at it (§5) and open `http://felix-demo.duckdns.org` — chat,
  submit an alert, watch the live-monitoring panel: because `FELIX_RUN_SAMPLE=1`
  is already generating traffic, the watcher should trip on its own within the
  `MIN_SAMPLES` window with **no manual clicking required**.
- **Remember the Spot tradeoff from §0:** if this task disappears without a
  redeploy, check for a Spot interruption before assuming something broke:
  ```bash
  aws ecs describe-tasks --cluster felix --tasks <task-arn>
  ```
  (`--tasks` is required.) Check the enumerated `stopCode` field for the value
  `SpotInterruption` — not the free-text `stoppedReason`, which is
  human-readable and not meant to be matched on programmatically. Doc:
  [describe-tasks](https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-tasks.html)

---

## 5. DuckDNS wiring (the free stable-name step)

1. At duckdns.org: create one subdomain (e.g. `felix-demo`); copy the token.
2. Put the token in the task def's `secrets` block as `DUCKDNS_TOKEN` (§4 step
   5/7); `DUCKDNS_DOMAIN=felix-demo` is a plain `environment` entry.
3. `docker/entrypoint.sh` runs this on boot, before starting the server:
   ```sh
   [ -n "$DUCKDNS_DOMAIN" ] && curl -fsS \
     "https://www.duckdns.org/update?domains=$DUCKDNS_DOMAIN&token=$DUCKDNS_TOKEN&ip="
   ```
   DuckDNS uses the caller's source IP (the task's public IP) when `ip=` is
   empty. On every restart the name re-points itself — the link never goes
   stale for more than the few seconds a restart takes.

---

## 6. Security callout — the CLI panel is effectively RCE (owner decision: ENABLED)

`WS /cli/ws` (`src/api/terminal.py`) bridges a real login shell over the
WebSocket so the browser terminal can run `ccloud` for real. That is, by
design, remote code execution against whatever the container's user can do —
acceptable for local dev where the API binds `127.0.0.1`, but a real exposure
once the same binary is serving `0.0.0.0` behind a public IP on Fargate.

**Decision (owner): the CLI panel is ENABLED in the deploy** —
`FELIX_CLI_ENABLED=true` in the web task's `environment`. The interactive
`ccloud` terminal is a core part of the demo (it's felix's third named
CockroachDB offering made tangible), and judges must be able to open the "CLI"
tab and run commands. The exposure is accepted **knowingly and with eyes open**:

- The security group **cannot** scope this out at the network layer — the
  terminal shares port 8000 with the API, so there's no path-based routing
  without adding an ALB (§0 declined that for cost). Anyone who finds the
  IP/DuckDNS URL while the task is up gets a shell.
- This is defensible **only** because: (a) it's a short, watched judging
  window, (b) on a low-value personal AWS account with nothing sensitive on it,
  and (c) the shell's blast radius is just the container (Fargate task role
  permissions + whatever `ccloud` is authed to). It is **not** a posture to
  leave running unattended for days.

**Operational guardrails while it's live:**
- Tear the service down (`update-service --desired-count 0`, or delete it)
  once judging is over — don't leave an RCE endpoint up indefinitely.
- Keep the Fargate **task role** minimally scoped (it already is — no admin).
- If you ever want to kill the panel without a code change, set
  `FELIX_CLI_ENABLED=false` in the task-def and redeploy: `GET /cli/status`
  then reports `enabled: false` and `WS /cli/ws` refuses connections, and the
  "CLI" tab shows only its explanatory placeholder. The app-level default is
  fail-closed (`config.py` → `false`), so this env var is what turns it ON.

---

## 7. Known trade-offs & risks (so nothing surprises you at demo time)

- **Fargate Spot, single task, no fallback (§0).** If Spot capacity is
  reclaimed, the service is down until Spot capacity returns — there's no
  second task and no automatic promotion to on-demand. Mitigation if this
  bites during judging: temporarily switch the service's
  `--capacity-provider-strategy` to `FARGATE` (on-demand) via
  `update-service`, or add a second task with `base=1` on `FARGATE` as an
  always-on floor (both cost more; neither is done by default).
- **HTTP only, no HTTPS.** DuckDNS maps name→IP; it doesn't terminate TLS.
  Judges get `http://…` and a "Not secure" label. A Cloudflare Tunnel sidecar
  is the fallback if a judge's browser hard-blocks HTTP (needs a domain + a
  second container — declined for simplicity, can be added later without app
  code changes).
- **Restart window.** When Fargate replaces the task (redeploy, crash, Spot
  interruption, AWS maintenance), there's a gap where the DuckDNS name points
  at a dead IP until the new task's entrypoint updates it.
- **CLI panel exposure — ENABLED knowingly for the demo, see §6.** `WS /cli/ws`
  is a live shell reachable by anyone with the URL while the task is up; tear
  the service down once judging ends.
- **Watcher/traffic-driver coupling.** Both now run in-thread inside `serve`,
  so you can't bounce either alone — restarting either means a new
  `felix-web` deployment. The traffic driver starts on boot, so by the time a
  judge arrives the sample window is already primed past `MIN_SAMPLES`.
- **Gemini quota over a month.** Free-tier text limits are higher than some
  other quotas but not infinite; enabling billing (<$1) is a same-key toggle,
  no redeploy needed.
- **Secrets are readable by anyone with `ecs:DescribeTaskDefinition` /
  `secretsmanager:GetSecretValue` on this account** — that's just you, so
  acceptable for a personal single-user demo, but worth remembering these are
  real credentials now living in a real AWS account rather than a local
  `.env`.

---

## 8. Decisions (status)

1. **Compute** — ✅ Fargate **Spot**, one merged task (web + watcher + sample
   traffic driver), 0.5 vCPU / 4GB. Single-task-on-Spot tradeoff accepted
   (§0, §7).
2. **Secrets** — ✅ **Secrets Manager** (or SSM) for `DATABASE_URL`,
   `GEMINI_API_KEY`, `CRDB_MCP_API_KEY`, `DUCKDNS_TOKEN` — referenced by ARN in
   the task def's `secrets` block, never plaintext `environment`. Everything
   else (role flags, hostnames, the MCP URL/cluster id) stays plain
   `environment`.
3. **Sample traffic** — ✅ folded into the web task as a background thread
   gated by `FELIX_RUN_SAMPLE` (off by default locally, on for the deploy).
   No separate task, no separate service, no `sample_project.server`.
4. **CLI panel exposure** — ✅ **ENABLED** (`FELIX_CLI_ENABLED=true`, §6). The
   interactive `ccloud` terminal is part of the demo; the RCE exposure is
   accepted for the watched judging window, with teardown-after-judging as the
   guardrail. App default stays fail-closed (`config.py` → `false`).
