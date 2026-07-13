# Cloud Platform Failure Modes and Recovery

Scope: current single-instance topology (see `docs/cloud-platform-scaling-assessment.md`). For each mode: detection, expected behavior, operator action, future hardening.

## Render restarts / instance replacement
- Detection: Render deploy/restart events; gap in `iot-cloud-api.requests` logs; gateways report heartbeat failures locally (`heartbeat_attempts` in edge SQLite).
- Expected: all in-memory tunnel registrations and console sessions are lost. Gateways reconnect their tunnel WebSocket automatically (edge tunnel client retry loop); consoles must be reopened by the user. Heartbeats/jobs/trends resume on the next edge cycle — edge buffering (SQLite queue) prevents trend loss.
- Operator action: none for edge data; re-open tunnel consoles.
- Hardening: tunnel service extraction (Stage D); session re-establishment UX.

## One app worker dies (current: the only worker)
- Same as a restart. Render restarts the process. No shared state is corrupted because all durable state is in Postgres.

## Database connections exhausted
- Detection: `/health/db` failures; pool-timeout errors (30 s) in logs; latency spike on all routes.
- Expected: requests fail with 500 after `DB_POOL_TIMEOUT`; `pool_pre_ping` recovers stale connections but cannot create capacity.
- Operator action: check Supabase connection count vs plan limit; reduce `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` if multiple services share the database; identify slow queries in Supabase dashboard.
- Hardening: pool gauge on `/health/db` (Phase 2B); per-route query budgets.

## Supabase temporarily unavailable
- Detection: `/health/db` fails; all edge requests 5xx; gateways accumulate local backlog (`trend_pending_upload_count` rises after recovery reports it).
- Expected: cloud returns 500s but stays up; edge agents buffer trends in SQLite (bounded backlog, Phase 1) and retry heartbeats each cycle; no data loss within edge backlog bounds.
- Operator action: wait/monitor Supabase status; after recovery confirm backlog drains (heartbeat telemetry shows pending counts falling).
- Hardening: explicit 503 (vs 500) for DB-down, Retry-After hints.

## Simultaneous gateway retries (thundering herd)
- Detection: request-rate spike in logs after an outage window.
- Expected: heartbeats and polls are cheap (indexed, now mostly read-only auth); trend uploads are bounded (500/batch) with exponential edge backoff from Phase 1.
- Operator action: none unless stop conditions from the load-test plan appear.
- Hardening: per-credential rate limits once a shared backend exists (assessment §6).

## Migration fails during deploy
- Detection: pre-deploy hook failure (deploy aborts) or, in the current Dockerfile path, container crash-loop with `require_current_schema` RuntimeError.
- Expected: old version keeps serving if the deploy aborts pre-swap; a half-applied migration is possible only within a non-transactional DDL batch (all current migrations are idempotent with existence guards — 0015/0016 pattern).
- Operator action: inspect Alembic error; migrations are guarded so re-running `alembic upgrade head` after fixing is safe; never run migrations from two places at once.
- Hardening: remove the `alembic upgrade head` from Dockerfile CMD (single migration authority: pre-deploy) — Phase 2B, deploy-coordinated.

## Tunnel session interrupted (browser or gateway side)
- Detection: `TunnelUnavailable` responses on console routes; gateway tunnel reconnect logged.
- Expected: pending proxied requests fail fast (`fail_pending`); gateway re-registers on reconnect, replacing the old registration; console session tokens remain valid until TTL and resume once the gateway socket is back.
- Operator action: refresh the console page; check gateway connectivity if reconnect does not occur.
- Hardening: none this phase (tunnel unchanged by charter).

## Gateway submits duplicate job results
- Detection: job history shows repeated completion timestamps (currently overwritten — last write wins).
- Expected: identical payloads are effectively idempotent; differing payloads overwrite (recorded risk).
- Operator action: none currently.
- Hardening: terminal-state guard returning 409 (Phase 2B, after verifying edge retry semantics).

## Trend upload retried by the edge
- Expected: idempotent by (point_id, sampled_at) — retries return existing samples without duplication (Phase 1 hardened, tested).

## User repeats a request (double-click, refresh)
- Reads: safe. Job creation: creates duplicate jobs (idempotency key is a Phase 2B candidate for UI-triggered jobs). BACnet writes: protected by the staged/approval batch flow (unchanged). Provisioning: repeat returns 409/creates second credential — verify in staging Level 1 and record.

## Background cleanup fails
- There is no background cleanup; retention runs opportunistically inside heartbeat/trend-upload transactions. A failing retention delete fails that request loudly (500) and is retried on the next cycle by design. If retention deletes ever grow slow, they will surface as heartbeat latency — the request-timing log isolates this immediately.

## Staging config accidentally references production
- Detection: pre-run checklist (staging validation checklist); harness refuses known production hosts; `GET /health` environment field and `/health/schema` revision set do not match expectations.
- Expected: the harness guard blocks the obvious case; environment separation (separate Render + Supabase) blocks the rest.
- Operator action: verify `CLOUD_DATABASE_URL`, admin token, and pepper are staging-specific before any test; rotate any credential that crossed environments.
- Hardening: add an `ENVIRONMENT=staging|production` setting surfaced on `/health` so both humans and the harness can verify the target (Phase 2B candidate).
