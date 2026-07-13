# Cloud Platform Scaling Assessment

Date: 2026-07-12. Branch: `codex/trend-hardening` (local, uncommitted).
Scope: Phase 2 — cloud platform, application, user, and gateway scaling foundations.
All statements below are labeled: **VERIFIED** (repository evidence), **CONFIGURED** (a limit set in code/config), **INFERRED** (risk deduced from code, not measured), **PROJECTED** (load model, not a supported claim), or **UNKNOWN** (requires staging measurement).

---

## 1. Current runtime topology

**VERIFIED from repository evidence:**

- Single FastAPI application (`cloud-api/app/main.py`, ~3,200 lines, 76 HTTP routes + 1 WebSocket + tunnel proxy routes).
- `cloud-api/Dockerfile` CMD: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000` — **one uvicorn process, no `--workers` flag, migrations run at container start**. The platform currently assumes exactly one instance of one worker.
- Database: SQLAlchemy 2.x sync engine → Supabase PostgreSQL (`DATABASE_URL`/`CLOUD_DATABASE_URL`). SQLite is used only for local dev/tests. **No SQLite in cloud production paths.**
- Startup (`lifespan`): verifies Alembic head via `require_current_schema` (or `create_all` in dev). No background tasks, no schedulers, no cleanup threads — **zero background work in the cloud app**; all periodic activity is driven by edge agents and browser requests.
- Edge cycle (VERIFIED in `edge-agent/iot_cx_agent/main.py`): every `heartbeat_interval_sec` (default 30 s) each gateway sends 1 heartbeat POST + 1 job poll GET + 0..n trend upload POSTs + 1 trend-config GET when trends are enabled.
- UI delivery: server-rendered HTML strings from `app/ui.py` (7,241 lines) served inline; no CDN, no static file mount.
- Weather: `urlopen` to Open-Meteo inside the request path, cached in the `site_weather` table with 30-minute TTL (DB-backed cache — multi-instance safe).
- Auth: gateway bearer tokens (HMAC-peppered hash lookup), operator Supabase JWTs (HS256 via shared secret, RS256/ES256 via JWKS), plus a static admin API token.

## 2. Single-instance vs multi-instance classification

| Finding | Location | Classification |
|---|---|---|
| `TunnelManager._tunnels` (gateway WS registry) | `app/tunnel.py` | **Single-instance only.** Process-local dict keyed by gateway_id. |
| `TunnelSessionManager._sessions` (console sessions) | `app/tunnel.py` | **Single-instance only.** Process-local dict; sessions invisible to other instances. |
| `GatewayTunnel.pending` (in-flight tunnel futures) | `app/tunnel.py` | Single-instance by design (asyncio futures); acceptable — request/response affinity to the connected socket is inherent. |
| Weather cache | `site_weather` table | **Safe multi-instance** (DB-backed, TTL check per read). |
| JWKS client cache (added this phase) | `app/auth.py` | Safe multi-instance (public key material, per-process cache). |
| Request-scoped dicts in handlers | `app/main.py` | Safe (request-local). |
| Alembic at container start | `Dockerfile` CMD | **Multi-instance risk**: two instances starting concurrently would race `alembic upgrade head`. Migration authority should be the Render pre-deploy hook only (commit e6a8b20 intended this; the Dockerfile CMD still self-migrates). |
| Job claiming | `claim_next_job` | Was racy across workers/instances; now uses `FOR UPDATE SKIP LOCKED` (this phase). Safe on Postgres, no-op on SQLite. |
| Everything else (heartbeats, jobs, trends, sites, users) | PostgreSQL | Safe multi-instance. |

**Conclusion (INFERRED):** the only true blockers to running >1 worker or >1 instance are (a) the tunnel registries and (b) migration-at-start. See §9.

## 3. Database connections and transactions

- **CONFIGURED:** `pool_pre_ping=True`. As of this phase: `DB_POOL_SIZE` (default 5), `DB_MAX_OVERFLOW` (10), `DB_POOL_TIMEOUT` (30 s), `DB_POOL_RECYCLE` (1800 s) — applied to non-SQLite URLs only. Prior to this phase the engine used implicit SQLAlchemy defaults with no recycle.
- Per-request session via `get_db()` dependency; sessions close in `finally`. No long-lived transactions found. Tunnel proxy requests hold **no** DB connection during gateway round-trips (auth happens before proxying) — VERIFIED in the tunnel console route: session dependency is only used in session-creation, not per proxied byte.
- **Write amplification (VERIFIED, mitigated this phase):** `require_gateway_auth` committed `last_used_at` on *every* edge request and `require_operator_auth` committed `last_login_at` on *every* UI request. Now throttled to once per `AUTH_TELEMETRY_MIN_INTERVAL_SEC` (default 60 s).
- **N+1 patterns (INFERRED, recorded — not fixed this phase):**
  - `ui_list_gateway_updates`: one gateway+site query per update row (≤500), and a missing gateway raises 404 aborting the whole listing.
  - `receive_job_result` for `bacnet_load_points`/`bacnet_discover`: one SELECT per discovered device/point. Bounded by BACnet device sizes in practice; revisit in Phase 2B.
  - Trend upload idempotency: one SELECT per sample (≤500/batch). Acceptable now; batch as a Phase 3 item.
- **PgBouncer/transaction pooling (UNKNOWN):** Supabase's pooled connection string runs PgBouncer in transaction mode; `pool_pre_ping` and session-level features are compatible with the current code (no session-level advisory locks, no LISTEN/NOTIFY, no temp tables). **Must be validated in staging.** Do not raise pool sizes until the actual Supabase plan connection limit is confirmed.
- Migration locking (INFERRED): index creation in 0016 uses plain `CREATE INDEX` (locks writes on large tables in Postgres). Tables are currently small; for large production tables future indexes should use `CONCURRENTLY` via autocommit — documented for Phase 3.

## 4. Users, organizations, sites, tenants

**Model (VERIFIED):** `OperatorUser` (role: admin/operator/viewer; status: pending/active/…), `Organization`, `OrganizationMembership`, `Site.organization_id`, `SiteMembership`. Gateways (`EdgeNode`) belong to sites via `site_id`; devices/points/jobs/trends belong to gateways.

**Authorization flow (VERIFIED):** central helpers — `visible_site_ids`, `require_site_access`, `_require_gateway_site_access`, `_require_device_site_access`, `_require_point_site_access`, `_scoped_gateway_statement` — used consistently across `/api/ui/*`. Scoped misses return 404 (anti-enumeration).

**Gaps found and closed this phase:**
- `/api/edge/gateways` listed **all** gateways to any active operator regardless of scope → now uses `_scoped_gateway_statement`.
- `POST /api/edge/jobs` accepted any `gateway_id` from any operator/viewer-excluded role → scoped operators now require site access and cannot probe unknown gateway IDs; platform admins retain the legacy pre-provisioning flow (documented behavior).

**Remaining tenant risks (documented, NOT changed):**
- **Legacy full-access fallback (VERIFIED, `app/access.py`):** an active operator with *zero* memberships sees *everything* ("existing operators predate memberships"). This is an intentional compatibility choice, but it means tenant isolation is opt-in per operator. **Directive for Phase 2C:** add a `scoped_by_default` cutover — e.g., a settings flag or a backfill migration assigning explicit memberships, then remove the fallback. Requires approval (behavior change).
- Site auto-creation on heartbeat (`receive_heartbeat` creates unknown sites) means a gateway credential controls site placement; acceptable while provisioning is admin-controlled.
- No durable audit log for role changes/membership changes (only BACnet writes are audited). Phase 2C item.
- Invitation/disabled/deleted user flows: status field exists (`pending`, `active`); no deleted-user cleanup of memberships was found — recorded as Phase 2C.

**Boundary tests:** `test_scaling_foundations.py` adds scoped-operator tests for the legacy endpoints; `test_api.py` already covers workspace-route scoping. Trends, tunnel sessions, and credentials are covered transitively by `_require_*_site_access` on their routes (verified by call-site audit; per-route tests remain a Phase 2C directive).

## 5. High-traffic request paths (bounds after this phase)

| Endpoint | Queries | Bound | Ordering | Notes |
|---|---|---|---|---|
| POST `/api/edge/heartbeat` | ~5 + history insert + retention delete | payload validated | n/a | Full-row update every 30 s per gateway; changed-field-only update is a Phase 2B optimization. |
| GET `/api/edge/{id}/jobs/next` | 1 SELECT (locked) + claim commit | 1 row | created_at, id | Composite index added (0016). |
| POST `/api/edge/{id}/trend-samples` | ≤500 idempotency SELECTs + insert + retention delete | 1–500 batch | n/a | Hardened in Phase 1. |
| GET `/api/ui/points/{id}/trend` | 1 | limit ≤5000 | sampled_at | Indexed (0015). |
| GET `/api/ui/gateways` | 1 + joinedload site | **unbounded rows** | gateway_id | Acceptable at ≤~1,000 gateways; pagination directive below. |
| GET `/api/ui/gateways/{id}/heartbeat-trend` | 1 | limit ≤720 | timestamp_utc desc | Composite index added (0016). |
| GET `/api/edge/jobs` | 1 (+scope subquery) | limit ≤200 | created_at desc, id desc | Deterministic. |
| GET `/api/ui/gateway-updates` | 1 + N gateway lookups | limit ≤500 | requested_at desc | N+1 recorded (Phase 2B). |
| Tunnel proxy routes | 0 during proxying | body streamed to edge | n/a | 900 s timeout CONFIGURED. |

**Directive for Claude Code (Phase 2B, needs approval on API shape):** add optional `limit`/`offset` (or keyset) parameters to `/api/ui/gateways` and `/api/ui/sites` with generous defaults so existing UI behavior is unchanged, then teach the UI to page.

## 6. Rate limiting and abuse protection (design only — no code this phase)

No shared rate-limit backend exists; a process-local limiter would silently break at >1 instance, so none was added. Staged design:

1. **Now (already effective):** payload bounds (trend 500/batch, job list 200, heartbeat-trend 720), gateway credential auth on all edge routes, per-gateway scoping of all edge writes.
2. **Stage B (single shared DB, no new infra):** DB-backed counters for *low-frequency, high-value* operations only — login failures per email (lockout/backoff), provisioning calls, tunnel-session creation. These tolerate one SELECT+UPSERT per attempt.
3. **Stage C (requires Redis or equivalent — do not add yet):** token-bucket limits on heartbeat/job-poll/trend-upload per gateway credential. Threshold to justify Redis: sustained >50 req/s aggregate edge traffic (≈1,500 gateways at 30 s cycle) or a demonstrated retry-storm incident in staging.
4. Distinguish causes before throttling gateways: retry storms are visible via `trend_max_upload_attempt_count` and heartbeat gaps (telemetry already carried). Malfunction ≠ abuse; prefer 429 + Retry-After for gateways, hard lockouts only for credentials-stuffing patterns.

## 7. Heartbeat and fleet load model (PROJECTED — not supported claims)

Assumptions: 30 s heartbeat cycle; each cycle = 1 heartbeat + 1 job poll + 1 trend-config poll (when trends enabled); ~3 DB writes per heartbeat (node update, history insert, retention delete) after this phase's auth-telemetry throttle.

| Gateways | Req/s (edge) | Writes/min | Heartbeat rows/day (30 d retained) | Notes |
|---|---|---|---|---|
| 10 | 1 | 60 | 28.8 k | Trivial. |
| 100 | 10 | 600 | 288 k | Fine on defaults. |
| 1,000 | 100 | 6,000 | 2.88 M | Pool defaults (5+10) likely saturate under sync workers — **needs staging Level 2/3 measurement**; heartbeat interval may need to lengthen or ingestion needs its own workers. |
| 5,000 | 500 | 30,000 | 14.4 M | Beyond a single sync uvicorn process (INFERRED). Requires multi-instance (Stage C), separate ingestion service (Stage D), and heartbeat interval review. |

Implemented this phase: heartbeat history retention (`HEARTBEAT_RETENTION_DAYS`, default 30, per-gateway pruning on the indexed `(gateway_id, timestamp_utc)` path) and the composite polling index. Recorded for Phase 2B: skip full-row `edge_nodes` update when nothing changed; move history insert to sampled cadence (e.g., 1-in-N) if row volume becomes a measured problem.

## 8. Cloud-to-edge job lifecycle

VERIFIED: create (`queued`) → poll/claim (`claimed`, now under `FOR UPDATE SKIP LOCKED`) → result (`completed`/`failed`). Timeout, cancellation, retry, and stale-job reaping **do not exist** — a gateway that dies after claiming leaves the job `claimed` forever (VERIFIED: no reaper anywhere).

- Duplicate claims across instances: **fixed this phase** (row lock).
- Duplicate result submission: last write wins, idempotent for identical payloads; terminal-state guard is a Phase 2B directive (reject result for already-completed jobs with 409, verify edge retry behavior first).
- Stale claimed jobs: Phase 2B directive — `claimed_at` timeout requeue or fail, driven by an admin endpoint or opportunistic check at poll time (no background worker exists; prefer poll-time reaping to preserve the zero-background-work architecture).
- PostgreSQL-backed job queue threshold (documented): with per-gateway serial polling, contention is inherently low. A dedicated broker is unjustified below ~10 k jobs/min or multi-consumer fan-out requirements. Revisit only with staging evidence.

## 9. Tunnel Scaling Constraint

**The tunnel is the binding constraint on horizontal scaling. No tunnel code was modified.**

- Gateway tunnels are WebSockets registered in a **process-local** dict (`tunnel_manager`); console sessions live in a second process-local dict (`tunnel_session_manager`).
- Therefore: browser tunnel traffic must reach the *same process* that holds the gateway's WebSocket.
  - Multiple uvicorn **workers** on one instance: **breaks tunnels** (worker A holds the socket, worker B gets the proxy request) — sticky sessions do not help across workers behind a single port.
  - Multiple Render **instances**: breaks tunnels the same way; Render's load balancer offers no gateway-aware affinity.
- **Decision (documented):** the tunnel remains **single-instance, single-worker** for now. All other traffic could scale horizontally *if* tunnels are extracted.
- **Future extraction boundary (Stage D):** a dedicated tunnel service (own Render service) owning gateway WebSockets + console sessions, addressed by the main app via internal HTTP with the gateway → tunnel-instance mapping in Postgres or Redis. Threshold to act: >1 instance needed for API load, or tunnel session memory/CPU measurably degrading API latency (staging Level 3 evidence).
- Memory per session (UNKNOWN — measure in staging): pending-request futures + base64 body buffering per proxied request; large exports through the tunnel buffer whole bodies in memory (INFERRED risk, recorded).

## 10. UI and browser behavior

VERIFIED: no periodic background polling timers; polling occurs only while a job is in flight (2–2.5 s interval, bounded by job completion) with stop conditions. Gateway list and workspace load fully server-rendered; client sorts in memory. Risks recorded for Phase 2B/4: full-fleet gateway list payload growth (pagination directive §5), workspace point tables render all rows (incremental rendering directive), no visibility-aware polling (add `document.hidden` check when polling is introduced anywhere new).

## 11. Caching classification

| Data | Classification |
|---|---|
| Site metadata, store hours | Safe for short-lived process cache; invalidation risk low; **not needed yet** (single indexed query). |
| Gateway online status | Must not be cached beyond heartbeat staleness windows (already time-derived per request). |
| User permissions / visible_site_ids | Request-local caching only (already effectively per-request); shared caching = invalidation risk on membership change. |
| Weather | Already DB-cached, 30 min TTL. Correct pattern. |
| Templates, device/object metadata | Safe candidates later; no measured need. |
| Trend query results | Do not cache (Phase 4 will aggregate server-side instead). |
| Tunnel status | Never cache (liveness). |
| JWKS keys | Cached this phase (process-local, safe). |

**No Redis.** Nothing above justifies shared-cache infrastructure yet.

## 12. Observability added this phase

- `iot-cloud-api.requests` logger: per-request `method, route-template, status, duration_ms` (health checks excluded; templates not raw paths, so log cardinality stays bounded).
- Existing signals retained: gateway heartbeat telemetry columns (incl. trend backlog health), `/health`, `/health/db`, `/health/schema`.
- Deferred (documented path): OpenTelemetry SDK + OTLP exporter is the preferred future integration (FastAPI + SQLAlchemy auto-instrumentation); adopt only when there is a place to send the data. Do not add Prometheus + OTel + APM simultaneously.
- Gaps recorded for Phase 2B: DB pool gauge (SQLAlchemy pool status on `/health/db`), auth-failure counters, tunnel session counters (observability-only additions are pre-approved by charter for tunnel code? — **no**: tunnel counters must be derived from outside tunnel.py, e.g., in the routes that call it).

## 13–18. Companion documents

- Load-test plan: `docs/cloud-platform-staging-load-test-plan.md` (levels 1–5, stop conditions, evidence).
- Harness: `tools/staging_load_harness.py` (staging-only guards; refuses `iot-cloud-api-dev.onrender.com`).
- Failure/recovery: `docs/cloud-platform-failure-recovery.md`.
- Roadmap (Phase 2A–2D, 3–6): below.

---

## Deployment scaling roadmap

**Stage A — current (single instance, single worker).** Validate staging baselines with the harness (Levels 1–2); confirm Supabase pooler compatibility; watch `iot-cloud-api.requests` latencies. Exit criteria: baseline numbers recorded.

**Stage B — multiple uvicorn workers, one instance.** Blocked by: tunnel registry (worker-local). Precondition: either accept tunnel breakage in staging tests to measure everything else, or extract tunnels first. Also: remove `alembic upgrade head` from Dockerfile CMD in favor of the pre-deploy hook (small change, needs deploy coordination — **not done this phase**).

**Stage C — multiple Render instances.** Blocked by tunnel affinity (§9). Everything else is ready: jobs (row-locked), heartbeats/trends (plain DB writes), auth (stateless + DB), weather (DB cache).

**Stage D — service separation.** Candidate boundaries in priority order: (1) tunnel service — unblocks B and C; (2) gateway ingestion API (heartbeat+trend+jobs) — isolates fleet load from UI latency, threshold ~1,000 gateways sustained; (3) background worker — only when scheduled retention/aggregation outgrows opportunistic pruning, expected in Phase 3; (4) trend query service — Phase 4 scale.

## Prioritized platform roadmap

- **Phase 2A (done this phase):** pool controls, auth-telemetry throttling, JWKS caching, job-claim locking, legacy-endpoint tenant scoping, hot-path indexes, heartbeat retention, request timing logs, load harness + plans.
- **Phase 2B (next, evidence-driven):** stale-job reaping at poll time; result-submission terminal-state guard; `ui_list_gateway_updates` N+1 fix + missing-gateway tolerance; changed-fields-only heartbeat update; DB pool gauge on `/health/db`; login-failure lockout counters (DB-backed); Dockerfile CMD migration removal (deploy-coordinated).
- **Phase 2C (tenants):** membership backfill + retire the zero-membership full-access fallback (approval required); per-resource boundary tests (trends, tunnel sessions, credentials, templates); durable audit records for role/membership/credential changes; user disable/delete lifecycle.
- **Phase 2D (horizontal):** tunnel service extraction; multi-worker enablement; multi-instance verification via staging Levels 3–5.
- **Phase 3 (trend engine):** partitioning, scheduled retention/aggregation worker, downsampling, `CREATE INDEX CONCURRENTLY` discipline, batched idempotency.
- **Phase 4 (visualization):** server-side time-window/zoom aggregation endpoints, bounded rendering, exports.
- **Phase 5 (AI/analytics):** anomaly/fault detection over the Phase 3 engine with evidence traceability.
- **Phase 6 (enterprise):** multi-site dashboards, fleet health, tenant administration, reporting, support tooling.

## Unknowns requiring staging load tests

1. Supabase pooled-connection behavior under `pool_pre_ping` + 15 concurrent connections (pool 5 + overflow 10).
2. Sustainable heartbeat throughput of one sync uvicorn worker (requests/s at p95 < 500 ms).
3. Tunnel session memory footprint and proxy latency under concurrent console use.
4. Retention-delete cost inside heartbeat/trend transactions at realistic history sizes.
5. Render restart behavior with in-flight tunnel sessions (expected: sessions drop, gateways reconnect via edge retry loop — verify timing).
