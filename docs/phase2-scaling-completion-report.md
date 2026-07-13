# Phase 2 Completion Report — Cloud Platform Scaling Foundations

Date: 2026-07-12. Branch: `codex/trend-hardening`. All work local and uncommitted.

## 1. Executive summary

The platform's durable state is already multi-instance-ready (PostgreSQL for everything that matters); the binding constraints are the process-local tunnel registries, single-worker deployment, and a handful of hot-path inefficiencies. This phase documented the full architecture, closed two tenant-boundary gaps, added evidence-backed safe foundations (pool controls, job-claim locking, auth-write throttling, JWKS caching, hot-path indexes, heartbeat retention, request timing logs), and delivered a staging load-test plan plus a guarded synthetic-load harness. Nothing in the tunnel, BACnet-write, or production paths was modified.

## 2. Architecture and bottleneck findings

Full detail in `docs/cloud-platform-scaling-assessment.md`. Highlights: single uvicorn worker with migrations at container start; zero background work (all periodicity is edge/browser-driven); tunnel state process-local (the horizontal-scaling blocker); auth previously wrote to the DB on every authenticated request; job claiming was racy across workers; heartbeat history grew unbounded; JWKS fetched per RS256 request; legacy edge endpoints bypassed site scoping.

## 3. Files modified or added

Modified (5): `cloud-api/app/config.py` (pool/retention/telemetry settings), `cloud-api/app/database.py` (env-controlled pool for server DBs), `cloud-api/app/auth.py` (telemetry throttle, JWKS cache), `cloud-api/app/main.py` (timing middleware, locked job claim, scoped legacy endpoints, heartbeat retention), `cloud-api/app/models.py` (two composite indexes), plus `.env.example`.

Added: `cloud-api/alembic/versions/0016_scaling_foundations.py`, `cloud-api/tests/test_scaling_foundations.py` (17 tests), `tools/staging_load_harness.py`, `tools/tests/test_staging_load_harness.py` (6 tests), `docs/cloud-platform-scaling-assessment.md`, `docs/cloud-platform-staging-load-test-plan.md`, `docs/cloud-platform-failure-recovery.md`, this report.

## 4. Schema and migration changes

Migration `0016_scaling_foundations` (guarded, reversible; verified upgrade → downgrade → upgrade on a fresh database): `ix_edge_jobs_gateway_status_created` on `edge_jobs(gateway_id, status, created_at)` — job-poll path; `ix_edge_heartbeats_gateway_timestamp` on `edge_heartbeats(gateway_id, timestamp_utc)` — heartbeat-trend reads and retention deletes. No column or data changes.

## 5. Tests run and results

- New: `test_scaling_foundations.py` 17 passed; `test_staging_load_harness.py` 6 passed.
- Regression: cloud `test_api.py` 181 tests → 180 passed, 1 failed (pre-existing "Remove device" UI assertion, present at HEAD, unrelated); `test_bacnet_read_jobs` + schema suites 7 passed; edge suite 57 passed (excluding `test_version.py` — needs Python ≥3.11 in the sandbox — and `test_bacnet.py`, whose exec-bit stubs are Windows-only; both pass on Windows per Phase 1 analysis).
- Migration 0016 round-trip: clean.
- One existing test initially broke on the new job-creation check (`test_job_creation_normalizes_bacnet_load_points_to_47814` creates jobs pre-heartbeat); resolved by preserving the legacy admin pre-provisioning flow and enforcing existence+scope for scoped operators only. No committed tests were modified.

## 6. Measured results

None claimed — no load environment exists in this session. All scale numbers in the assessment are labeled PROJECTED or UNKNOWN pending staging runs.

## 7. Configured limits (after this phase)

DB pool 5 (+10 overflow, 30 s timeout, 1800 s recycle, non-SQLite only); auth telemetry writes ≥60 s apart; heartbeat history retained 30 days; trend samples 90 days; trend batches ≤500; trend reads ≤5000; heartbeat-trend ≤720; job lists ≤200; gateway-update lists ≤500; tunnel request timeout 900 s. All env-overridable and pydantic-validated.

## 8. Projected scenarios (projections, not supported claims)

10 gateways ≈ 1 req/s; 100 ≈ 10 req/s; 1,000 ≈ 100 req/s and ~6,000 writes/min (needs pool/worker measurement — staging Level 2/3); 5,000 ≈ 500 req/s (beyond a single sync worker; requires Stage C/D). Model assumptions in assessment §7.

## 9. User and tenant isolation findings

Site-scoping helpers are centralized and consistently applied on `/api/ui/*`. Closed this phase: `/api/edge/gateways` (listed all gateways to any operator) and `POST /api/edge/jobs` (no gateway scope check). Documented, unchanged: zero-membership operators retain platform-wide visibility (legacy fallback in `access.py`) — retiring it is the key Phase 2C item and needs approval; no durable audit log yet for role/membership changes.

## 10. Database connection and pooling findings

Engine had no explicit pool config (a prior pool-controls commit was reverted by the 0.1.5 restore); now env-controlled with conservative defaults. Auth wrote+committed on every request (largest hidden write load at fleet scale) — throttled. Supabase pooler compatibility and plan connection limits are UNKNOWN → staging validation required before raising pool sizes. N+1 patterns recorded (gateway-updates listing, job-result device/point upserts, per-sample idempotency SELECTs) — Phase 2B/3 items.

## 11. Multi-instance readiness

Ready: jobs (FOR UPDATE SKIP LOCKED), heartbeats, trends, auth, weather cache, JWKS cache. Not ready: tunnel registries (process-local), Dockerfile self-migration (race on concurrent start). No other process-local state, threads, or background tasks found.

## 12. Tunnel scaling constraint

Tunnel WebSockets and console sessions are process-local; multi-worker or multi-instance deployment breaks tunnel traffic today. Decision: remain single-instance/single-worker until the tunnel is extracted into its own service (Stage D boundary documented in the assessment). No tunnel code was touched.

## 13. Immediate risks

Pre-existing UI test failure at HEAD; stale `claimed` jobs have no reaper (gateway death mid-job leaves them forever); duplicate job results overwrite (last-write-wins); zero-membership full-access fallback; Dockerfile migration race if instance count is ever raised without the Stage B change.

## 14. Deferred work and justification

Rate limiting (needs shared backend; process-local limiter would be false safety), Redis/caching (no measured need), message broker (contention inherently low with per-gateway serial polling), pagination on gateway/site lists (UI coordination; harmless at current fleet size), tenant-fallback retirement (behavior change needing approval), Dockerfile CMD change (deploy-coordinated), OpenTelemetry (no backend to receive it yet).

## 15. Recommended staging load-test sequence

Levels 1 → 2 → 3 on the staging deployment per `docs/cloud-platform-staging-load-test-plan.md`, combined with the trend-hardening staging checklist; Level 4 overnight; Level 5 failure drills last. Capture harness JSON + Render/Supabase evidence for each.

## 16. Recommended next implementation objective

Phase 2B: stale-job reaping at poll time, job-result terminal-state guard, gateway-updates N+1 fix, `/health/db` pool gauge, DB-backed login-failure lockout, `ENVIRONMENT` flag on `/health`, then execute staging Levels 1–3 and feed measurements back into the assessment.

## 17. Git status

Branch `codex/trend-hardening`; all changes uncommitted and local. Modified: the 5 app files + `.env.example` (plus the pre-existing trend-hardening working-tree changes from Phase 1). Untracked: migrations 0015/0016, new test files, harness, and the five docs. Not committed, not pushed, not deployed.
