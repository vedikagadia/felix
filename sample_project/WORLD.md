# WORLD.md — the ground-truth bible for the felix demo

**This file is authoritative.** Every seeded artifact (incidents, resolution
steps, docs, code_changes, code graph) MUST conform to the names, logs, and
facts here. If an authoring agent needs a component/command/log line that isn't
here, it must use one that IS here — not invent a new one. The consistency
verifier checks all corpora against this document.

---

## 1. The service

`checkout_service` — a small payment/checkout service. Lives in
`sample_project/checkout_service/`. Language: Python. No real business logic;
it exists to produce a realistic call graph, logs, docs, and incident history.

## 2. Canonical modules, classes, and functions

Use these EXACT names. (`file` → symbols)

| File | Classes | Key functions/methods |
|---|---|---|
| `config.py` | — | `load()`; constants: `DB_POOL_SIZE=10`, `DB_CONNECT_TIMEOUT_SECONDS=5`, `PAYMENT_MAX_RETRIES=8`, `PAYMENT_BASE_DELAY_SECONDS=0.5`, `PAYMENT_GATEWAY_TIMEOUT_SECONDS=30`, `QUEUE_MAX_DEPTH=5000` |
| `db.py` | `ConnectionPool`, `_Connection`, `ConnectionPoolExhausted` | `ConnectionPool.acquire()`, `ConnectionPool.release()`, `get_pool()` |
| `payment_gateway.py` | `PaymentClient`, `GatewayTimeout` | `PaymentClient.charge(order_id, amount)`, `PaymentClient._call_gateway(...)` |
| `fulfillment_queue.py` | `FulfillmentQueue`, `QueueFull` | `FulfillmentQueue.enqueue(order_id)`, `FulfillmentQueue.depth()`, `get_queue()` |
| `checkout.py` | `CheckoutHandler`, `CheckoutError` | `CheckoutHandler.process(order_id, amount)` |
| `api.py` | `CheckoutAPI` | `CheckoutAPI.post_checkout(request)`, `CheckoutAPI.get_health(request)` |
| `metrics.py` | `MetricsEmitter` | `MetricsEmitter.emit_latency(name, value_ms)`, `emit_count(...)`, `get_emitter()`; constant `LATENCY_AGGREGATION="avg"` |

Services/components referred to in incidents use these names: **checkout-api**,
**checkout-handler**, **payment-gateway**, **fulfillment-queue**,
**connection-pool**, **fulfillment-worker** (the downstream consumer of the
queue; external to this repo).

## 3. Call graph (authoritative — the AST parser must reproduce this)

```
CheckoutAPI.post_checkout  -> CheckoutHandler.process
CheckoutAPI               (imports) checkout.py
CheckoutHandler.process    -> ConnectionPool.acquire / _Connection.close (db.py)
CheckoutHandler.process    -> PaymentClient.charge (payment_gateway.py)
CheckoutHandler.process    -> FulfillmentQueue.enqueue (fulfillment_queue.py)
CheckoutHandler.__init__   -> PaymentClient(), get_queue()
PaymentClient.charge       -> PaymentClient._call_gateway
PaymentClient              (imports) config.py
ConnectionPool             (imports) config.py
FulfillmentQueue           (imports) config.py
```

Blast-radius fact: `checkout-handler` is the hub. Anything wrong in
`payment-gateway`, `db`/`connection-pool`, or `fulfillment-queue` surfaces
through `checkout-handler` and then `checkout-api`.

## 4. Canonical log lines (use these exact event strings)

- `config.loaded pool_size=... payment_max_retries=...`
- `db.pool.acquire in_use=... size=...`
- `db.pool.exhausted in_use=... size=...`   ← the connection-exhaustion signal
- `db.pool.release in_use=...`
- `payment.call order_id=... amount=...`
- `payment.retry order_id=... attempt=.../... backoff=...s`   ← retry storm signal
- `payment.exhausted order_id=... attempts=...`
- `fulfillment.enqueued order_id=... depth=...`
- `fulfillment.queue.full depth=... max=...`   ← queue saturation signal
- `checkout.start order_id=... amount=...`
- `checkout.success order_id=...`
- `checkout.payment_failed order_id=... err=...`
- `checkout.enqueue_failed order_id=... err=...`
- `api.request path=... order_id=...`
- `api.error path=... order_id=... err=...`
- `metrics.latency name=... value_ms=... agg=...`

## 5. THE TWO PLANTED ROOT CAUSES (critical)

### Planted incident A — "code-only" (solvable ONLY by reading the code graph)
- **Symptom:** During traffic spikes, checkout requests fail with
  `ConnectionPoolExhausted` / `db.pool.exhausted`. Looks like a database
  capacity problem. Restarting or scaling the DB does NOT fix it.
- **True root cause:** `CheckoutHandler.process` acquires a `ConnectionPool`
  connection at the start and holds it until `finally`, i.e. across the entire
  `PaymentClient.charge()` call. When the gateway is slow, `charge()` retries up
  to `PAYMENT_MAX_RETRIES` (8) with exponential backoff, holding the scarce
  connection (`DB_POOL_SIZE`=10) for tens of seconds. Under load the pool
  drains and unrelated requests fail. **The DB is a victim, not the cause.**
- **Why code-only:** no single log line or past incident names this. You must
  trace `checkout-handler → connection-pool` AND `checkout-handler →
  payment-gateway.charge (retry loop)` in the code to see the connection is held
  across the retries. This is the ~10% "analyze the codebase" case.
- **Correct resolution:** move the DB acquire/release to wrap only the DB work,
  NOT the payment call (acquire after charge, or use a separate short-lived
  connection); optionally lower `PAYMENT_MAX_RETRIES`.

### Planted incident B — "merge-only" (solvable ONLY by a recent code_changes record)
- **Symptom:** On-call reports customers complaining about slow checkouts, but
  the checkout latency dashboard/alert looks green. Alerts did NOT fire.
- **True root cause:** a recent merge changed `metrics.py`
  `LATENCY_AGGREGATION` from `"p99"` to `"avg"`. The mean hides tail latency, so
  p99-based dashboards/alerts went blind. Nothing in `metrics.py` is wrong in
  isolation — the problem is that the aggregation *changed recently*.
- **Why merge-only:** the current code reads fine; past incidents don't cover
  it. Only the `code_changes` record ("switched checkout latency metric from p99
  to avg") reveals it. This is the "what changed recently?" case.
- **Correct resolution:** revert `LATENCY_AGGREGATION` to `"p99"`; add a test
  asserting the aggregation for SLO metrics.

## 6. Rules for authoring agents

- Reference ONLY names/logs/facts in this file. No invented components/commands.
- The ~12–15 incidents should span the real failure modes: payment retry storms
  (`payment.retry` / `payment.exhausted`), queue saturation
  (`fulfillment.queue.full`), config/deploy regressions, plus routine issues.
- Include EXACTLY the two planted incidents A and B above, and make sure A is
  only explainable via the code graph and B only via a code_changes record.
- `code_changes` seed MUST include the culprit merge for B (p99→avg) and should
  include a merge that touched `PAYMENT_MAX_RETRIES` (relevant to A's severity).
- Every incident's `resolution_steps` must use real commands/components.
- Docs (architecture / setup / runbook) describe THIS service and topology.
