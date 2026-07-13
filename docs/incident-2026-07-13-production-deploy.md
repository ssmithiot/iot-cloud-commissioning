# Incident — 2026-07-13 production deploy (two defects, one morning)

**Outcome:** resolved same day. Production runs current code (`/health` reports
`environment: production, version: 0.1.0`), fleet healthy throughout, no data
lost (edge store-and-forward buffered through every outage window).

## Timeline (CDT)

| Time | Event |
|---|---|
| ~08:56 | First deploy of consolidated branch (`e42ad2c`) goes live. Migrations 0015–0017 apply. Edge fleet immediately healthy on new code. |
| ~08:57 | Browser dashboard loads but all data requests stall. `/health` unreachable from browsers. Edge traffic 200s continuously. |
| ~09:30 | Root cause #1 found in logs: response validation failures — `received_at: Input should be a valid datetime, input: None`. |
| ~09:34 | Hotfix `e5233e0` deployed (schema tolerance + migration 0018 backfill). Dashboard populates fully. |
| ~09:37–09:45 | Root cause #2 emerges: `EMAXCONNSESSION ... pool_size: 15` — Supabase session-mode pooler client ceiling. Deploy churn (multiple rollovers, lingering sessions) exhausts it; intermittent 500s on all DB-touching requests. |
| ~09:45–10:15 | Mitigations: app pool capped (`DB_POOL_SIZE`), then attempt to move to transaction pooler (port 6543) with compatibility commit `626970e`. Instability persists during churn; `/health` intermittently unreachable. |
| ~10:45–11:05 | Rollback attempts. `e42ad2c` retry fails on schema gate (DB at 0018, code expects 0017) — the gate working as designed. `AUTO_CREATE_TABLES=true` set as the documented escape hatch; `DATABASE_URL` returned to port 5432. |
| ~11:10 | A redeploy lands with flag active; service stabilizes on current code, session pooler, backfilled data. Workspace fast, tunnel connected, upload queues zero. |

## Defect 1 — NULL `received_at` on historical rows

Production tables predate strict Alembic governance; the models' NOT NULL on
`edge_heartbeats.received_at` was never enforced there, so years of rows carry
NULL. The release's strict response schemas (`GatewayHeartbeatTrendOut`,
`PointTrendSampleOut`) rejected those rows, failing every dashboard load.
**Why no test caught it:** every test database is created from the models,
where the constraint exists — the defect state was unrepresentable outside
production.

**Fix (shipped, `e5233e0`):** schemas tolerate `received_at: None`; migration
0018 backfills from `timestamp_utc`/`created_at`; schema-layer regression
tests added.

**Lesson:** staging must run against a restored copy of production data, not
just production-shaped schema. Added to the staging validation expectations.

## Defect 2 — Supabase session-pooler client ceiling (15)

The session-mode pooler allows 15 clients. The app's pool (5+10=15) could
consume all of them; every deploy briefly runs two instances plus a pre-deploy
migration process, and killed instances' sessions linger until timeout.
Result: `EMAXCONNSESSION` connection failures during and after deploy churn —
including pre-deploy migrations deadlocking against the live app.

**Fixes:**
- Shipped (`626970e`): psycopg auto-prepared statements disabled for
  PostgreSQL, making the app safe behind transaction-mode poolers (port 6543)
  where the client ceiling is effectively removed. First live attempt on 6543
  showed residual request stalls under churn — **not yet root-caused**; do not
  retry 6543 until reproduced and explained in staging.
- Operational: app pool sized to 5+5 (max 10) leaving ≥5 session-pooler slots
  of headroom; recommended split of `MIGRATION_DATABASE_URL` for pre-deploy.

**Lesson:** connection budgets are infrastructure limits, not app config —
document per environment: app max connections + concurrent-deploy overlap +
migration process must fit inside the pooler ceiling.

## Also proven today (the good news)

- Edge store-and-forward: zero data loss across every outage window; backlogs
  drained automatically. The Phase 1 design did exactly what it promised.
- The schema drift gate refused to boot mismatched code twice — both times
  correctly. `AUTO_CREATE_TABLES=true` worked as the documented escape hatch.
- Migration 0018's backfill retroactively made even the buggy `e42ad2c`
  viable — additive, idempotent migrations bought recovery options.
- Request-timing logs were invisible when needed (uvicorn-only logging
  config); fixed in `626970e`.

## Addendum (same day, afternoon) — the underlying cause of the performance chain

After stabilization, per-request timing logs plus `pg_stat_activity` analysis
revealed the common denominator behind the slow dashboards, pool exhaustion,
QueuePool timeouts, and the instance health-check failure: **the Render
service runs cross-region from the database** (~70ms TCP RTT measured from
the instance shell to `aws-1-us-east-1.pooler.supabase.com`; same-region is
~1–3ms). Every SQL statement pays the round trip: heartbeats (~7 statements)
≈900ms; the edge trend-config poll lazy-loaded point details per config
(N+1), reaching ~50s per request and pinning transaction-pooler backends
"idle in transaction" between statements. Database CPU was 2% throughout —
the bottleneck was distance, not load.

Fixes: (a) N+1 eliminated with a joined load (statements per config poll:
N+1 → 2) — in repo; (b) planned migration of the Render service to Virginia
per `docs/region-migration-runbook.md`; (c) standing rule added to staging
docs and the readiness checklist: all services and databases share one
region.

Lesson: this latency existed since the service was created, but light
traffic and no per-request timing kept it invisible. The first day of real
observability found it within hours.

## Follow-ups

1. **Alignment deploy (calm day):** deploy `626970e` (or later) on port 5432,
   verify, then set `AUTO_CREATE_TABLES=false` — restoring full strict-schema
   protection. Confirm live commit hash == pushed head.
2. **Staging first (in progress):** all future releases follow
   `docs/release-process.md`; staging must include restored production data.
3. **Transaction pooler (6543):** reproduce the stall in staging under
   synthetic load (the harness exists) before any second production attempt.
4. Set `MIGRATION_DATABASE_URL` + pre-deploy split so migrations never compete
   with the app for pooler slots.
5. Update `production-readiness-checklist.md` known-issues and the DR runbook
   with the pooler-budget rule (done alongside this report).
