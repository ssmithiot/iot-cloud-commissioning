# Safe Scale Development Roadmap

## Rule zero

The current production tunnel is a protected operational dependency. Scale
development must not change its code, routes, configuration, WebSocket
protocol, or deployment behavior. In particular, this branch does not modify:

- `cloud-api/app/tunnel.py`;
- the edge-agent tunnel client;
- gateway tunnel configuration or service units;
- existing tunnel proxy routes.

All replica-related tunnel work is developed as a separate adapter and tested
outside production before it can be considered for a merge.

## What can progress now without disturbing production

| Workstream | Safe now | Production effect |
|---|---|---|
| Database budgets | Explicit per-process pool limits and pool health | Already deployed; prevents one process from taking the pool down |
| Observability | Request IDs, structured operational logs, readiness and capacity health | Additive headers/endpoints only |
| Deployment | Single pre-deploy migration, revision gate, rollback/runbook | Existing tunnel remains unchanged |
| Load testing | Local/staging workload generator and acceptance reports | No production workload until explicitly requested |
| Replica control plane | Interface, Redis-backed registry/relay prototype, failure tests | Isolated; not wired into current tunnel |
| Workers | Separate process contracts for maintenance/retention/report work | No BACnet execution moves into cloud workers |

## Delivery order

### 1. Measure and protect the current single-instance system

- Add request correlation IDs to every HTTP response and application log.
- Add a readiness endpoint that reports database/schema readiness and local
  connection-pool pressure.
- Document and alert on gateway heartbeat age, queued-job age, database pool
  pressure, HTTP error rate, and API latency.
- Keep one API instance. Do not configure horizontal autoscaling.

### 2. Build a repeatable staging/load environment

- Add an opt-in local scale compose profile: PostgreSQL, Redis, one API, and
  independently started test API instances.
- Add a load generator that uses synthetic API traffic only. It must not create
  BACnet commands or point writes.
- Record baseline throughput, p95 latency, database connections, errors, and
  recovery time after API restart.

### 3. Build the future replica control plane in isolation

- Define a `TunnelRegistry` interface; do not replace `TunnelManager`.
- Create a Redis-backed development implementation for ownership TTL,
  console-session records, and bounded request relay envelopes.
- Prove: a request reaches the owning tunnel process; owner loss fails fast;
  reconnect takes ownership safely; expired sessions cannot be reused.
- Gate any production adoption behind a separate feature flag and a live-gateway
  test plan approved before activation.

### 4. Separate cloud-only background work

- Introduce a worker deployment only for cloud maintenance workloads such as
  retention, rollups, exports, and notification delivery.
- Keep edge job execution and all BACnet traffic on the gateway.
- Use durable job records, bounded retries, idempotency keys, and dead-letter
  visibility before enabling worker concurrency.

### 5. Scale deliberately

Only after the replica control plane passes staging failure tests:

1. add a second API instance in staging;
2. run load and disconnect/reconnect tests;
3. verify no tunnel request is routed to the wrong owner;
4. deploy one production canary instance with an immediate rollback path;
5. expand only after operational metrics remain healthy.

## Explicit non-goals for this work

- no rewrite of the working tunnel;
- no production Redis, worker, or replica provisioning without approval;
- no automatic BACnet writes or cloud-to-device network path;
- no replacement of PostgreSQL/Alembic migration authority;
- no claim of customer capacity without a measured load report.
