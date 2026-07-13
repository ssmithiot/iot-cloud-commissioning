# Cloud Platform Staging Load-Test Plan

Target: a **separate staging Render service + separate Supabase project** (per `docs/staging-trend-validation-checklist.md`). Never production. No production gateways, tunnels, credentials, or database URLs.

Harness: `tools/staging_load_harness.py`
- requires explicit `--base-url` and `--confirm-staging`
- refuses known production hosts (`iot-cloud-api-dev.onrender.com`)
- synthetic IDs only (`LOADTEST-<run>-NNNN`, site `loadtest-site-<run>`)
- emits machine-readable JSON (counts, p50/p95/p99 latency, status codes, transport errors)
- run levels manually, one at a time; nothing runs automatically

Evidence to capture at every level: harness JSON output; Render CPU/memory graphs; Supabase connection count and slow-query log; `iot-cloud-api.requests` log latencies; `/health/db` and `/health/schema` before/after; row counts of `edge_heartbeats`, `edge_jobs`, `point_trend_samples` before/after (retention verification).

Global stop conditions (abort the level immediately):
- sustained 5xx rate > 2%
- p95 latency > 2 s for heartbeat or job-poll
- Supabase connections at plan limit
- Render instance restart during the run
- any error implicating data integrity (duplicate claims, lost samples)

---

## Level 1 — Functional concurrency (smoke)
- Gateways: 5 synthetic. Users: 1 operator reader. Duration: 5 min. Cycle: 10 s.
- `python tools/staging_load_harness.py --base-url <staging> --confirm-staging --admin-token $STAGING_ADMIN_TOKEN --gateways 5 --duration-sec 300 --heartbeat-interval-sec 10 --operators 1 --output level1.json`
- Expected: 0 errors; p95 < 300 ms on all operations; heartbeat rows appear; retention leaves rows intact (all recent).
- Verify by hand: `/api/ui/gateways` shows exactly the 5 LOADTEST gateways under the loadtest site; one job created via admin token is claimed on the next poll.

## Level 2 — Moderate sustained load
- Gateways: 50. Users: 3 operator readers. Duration: 30 min. Cycle: 10 s (≈15 req/s aggregate — models ~150 gateways at production 30 s cadence).
- Expected: flat latency over the run (no drift = no connection/session leak); Supabase connections stable ≤ pool+overflow; no 5xx.
- Evidence focus: `edge_heartbeats` growth rate matches gateway count; request log p95 by route.

## Level 3 — Burst load
- Gateways: 200 with cycle 5 s for 10 min (≈80 req/s burst — models a fleet-wide retry storm / mass reconnect).
- Expected: pool timeout behavior visible but bounded (503/timeouts < 5% acceptable during burst); full recovery to Level-2 latencies within 2 min after burst ends; **no duplicate job claims** (create 20 jobs mid-burst, verify each claimed exactly once via job history).
- Stop early if Supabase connection limit is hit — record the ceiling; that number becomes the documented single-instance limit.

## Level 4 — Endurance
- Gateways: 50, cycle 10 s, duration 8 h (or overnight). Users: 1 reader.
- Expected: zero memory growth trend on Render; `edge_heartbeats` bounded by retention after the window passes; log volume acceptable; no latency drift.
- Evidence focus: Render memory graph slope; heartbeat retention actually pruning (row count plateaus).

## Level 5 — Failure and recovery
Manual scenarios, one at a time, at Level-1 scale:
1. **Render restart mid-run** (manual deploy): expect gateway workers to see transport errors, then full recovery ≤ 2 cycles; no stuck `claimed` jobs from the restart window (record any — input to the Phase 2B stale-job reaper).
2. **Database pause** (Supabase pause/resume in staging): expect 5xx during pause, `/health/db` failing, clean recovery after resume with `pool_pre_ping` reconnecting; no crashed process.
3. **Retry storm**: stop the harness for 10 min, restart with all gateways at once; expect the thundering-herd cycle to spread naturally (edge jitter) and no 5xx beyond the first seconds.
4. **Duplicate results**: submit the same job result twice via curl with a staging gateway token; record current last-write-wins behavior (baseline for the Phase 2B terminal-state guard).
5. **Staging-config safety**: run the harness against the production URL — expect hard refusal (verifies the guard); confirm staging `.env` has no production values.

## Cleanup after each session
Delete `LOADTEST-*` gateways, credentials, site, heartbeats, and jobs from staging; revoke the staging admin token if it was shared; archive the JSON outputs next to the level name in the staging validation records.
