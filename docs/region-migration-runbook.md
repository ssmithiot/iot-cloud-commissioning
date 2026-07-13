# Region Migration Runbook — Render (Oregon) → Virginia (us-east-1)

**Why:** measured 2026-07-13: ~70ms TCP round-trip between the Render app
instance and the Supabase database (`aws-1-us-east-1`, Virginia). Every SQL
statement pays it. Same-region latency is ~1–2ms — a 30–70× speedup on every
database interaction (heartbeats 900ms → ~30ms; dashboards seconds → instant).
Root cause of the 2026-07-13 performance incident chain.

**Constraint:** Render cannot change a service's region in place — this is a
new-service migration. **Both services can run simultaneously against the same
database**, which makes this low-risk and incremental: nothing is cut over
until it's proven.

**Do not do this the same day as other changes. Pick a calm morning.**

## Phase 0 — Prep (no user impact)

1. Commit/push any pending work; note the exact production commit.
2. Decide the new service name (e.g. `iot-cloud-api`). Its URL will be
   `https://<name>.onrender.com`.
3. **Strongly recommended:** register a custom domain (e.g.
   `api.iofteam.com`) to attach to the new service. With a custom domain, the
   backend can move again in the future without ever touching the fleet.
4. Verify the gateway-side config mechanism: `cloud_url` lives in
   `/etc/iot-cx-agent/agent.yaml` on each gateway; changing it = edit + service
   restart (SSH / upgrade-webapp run).

## Phase 1 — Build the Virginia service (no user impact)

1. Render → New Web Service → same repo, branch `main` → **Region: Virginia
   (US East)**.
2. Copy ALL env vars from the old service (Environment tab → use
   `docs/staging-environment-variables.md` as the checklist of what exists).
   Same `DATABASE_URL` (6543), same pepper, same admin token — this is the
   same production, relocated.
3. Pre-deploy command: `cd cloud-api && alembic upgrade head` (no-op if
   schema current; keeps future releases automatic).
4. Deploy; verify:
   ```
   python tools/release_smoke_check.py --base-url https://<new>.onrender.com \
     --expect-environment production --read-only
   ```
5. Latency probe from the NEW service's Shell (expect ~1–3ms):
   ```
   python3 -c "import socket,time
   for i in range(5):
       t=time.time(); s=socket.create_connection(('aws-1-us-east-1.pooler.supabase.com',6543),timeout=5); s.close(); print(round((time.time()-t)*1000,1),'ms')"
   ```
6. Log in via the new URL; confirm dashboard/workspace speed. Watch
   `duration_ms` in the new service's logs — heartbeat-path endpoints should
   be tens of ms.

Note: Supabase Auth → URL Configuration → add the new URL to the redirect
allow-list (and Site URL once cutover completes).

## Phase 2 — Move the browser users (minutes, reversible)

Tell internal users to use the new URL (bookmark swap). Old URL keeps working
throughout — same database, so both views are identical and live.

## Phase 3 — Move the fleet (batched, zero data loss)

Gateways buffer during any gap, so a config change + restart loses nothing.

1. **Canary:** pick 2–3 online gateways. Per gateway (SSH or upgrade webapp):
   ```
   sudo sed -i 's|^cloud_url:.*|cloud_url: https://<new>.onrender.com|' /etc/iot-cx-agent/agent.yaml
   sudo systemctl restart iot-cx-agent.service
   ```
2. Verify each canary: heartbeat 200s in the NEW service's logs, online in
   the UI, trend backlog draining, one job round-trip.
3. Wait 24h. Then batches of ~20 with the same verification (the post-update
   health gate applies if driven through the update queue).
4. Track progress: gateways still hitting the OLD service = its access log.

## Phase 4 — Decommission Oregon

1. When the old service's logs show zero traffic for 48h: **Suspend** (don't
   delete) the old service for a week as an instant rollback.
2. After a quiet week: delete it. (Deleting frees the old `onrender.com`
   subdomain, but do NOT plan to reuse it — the custom domain is the answer.)
3. Update Supabase Auth Site URL to the final address. Update
   `PRODUCTION_RESOURCE_FINGERPRINTS` on staging to include the new host.
   Update the harness/smoke-check FORBIDDEN host lists in
   `tools/staging_load_harness.py` and `tools/release_smoke_check.py`.

## Rollback at any phase

Old service untouched until Phase 4: rollback = point browsers/gateways back
at the old URL. Nothing schema- or data-related changes in this migration.

## Expected results (from 2026-07-13 measurements)

| Path | Before (cross-region) | After (same region, projected) |
|---|---|---|
| TCP RTT app→DB | ~70ms | ~1–3ms |
| Heartbeat request | ~900ms | ~30–50ms |
| Single-query endpoints | ~350ms | ~10–20ms |
| Trend-config poll (with N+1 fix) | ~0.5–2s | ~50ms |
| Dashboard full load | seconds–minutes | < 1s |

## Standing rule (added to staging docs)

**All services and databases live in the same region: us-east-1 / Virginia.**
Staging Render service and staging Supabase project included. Region is now a
line item on the production readiness checklist.
