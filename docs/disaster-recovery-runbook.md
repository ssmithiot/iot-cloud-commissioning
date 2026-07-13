# Disaster Recovery Runbook

Operator-facing procedures. Extends `cloud-platform-failure-recovery.md` (failure-mode analysis) with step-by-step actions. Keep both: that document explains *what happens*, this one says *what you do*.

## 1. Render deployment rollback

**When:** failed post-deploy validation, error-rate spike, fleet heartbeat failures after a deploy.

1. Render dashboard → the service → Deploys → previous known-good deploy → **Redeploy**.
2. Wait for health: `GET /health` → ok + expected environment; `/health/db` ok.
3. `/health/schema`: with additive-only migrations (policy), the older code runs safely against the newer schema. If it reports drift-refusal at startup, the release contained a non-additive change — go to §5.
4. Spot-check 3 gateways heartbeating; open one trend chart.
5. Record: what was rolled back, why, evidence, in the release log. Root-cause before retrying.

## 2. Supabase recovery

**When:** database unavailable, data corruption, accidental destructive operation.

- *Transient outage:* the app returns 5xx; edges buffer (trend queue + retry) and recover alone. Action: monitor Supabase status; after recovery confirm `/health/db`, then watch fleet backlog counts fall over the next hours. No manual intervention on gateways.
- *Data recovery:* Supabase dashboard → Backups → restore per plan capability (PITR if enabled). **Before restoring:** stop the Render service (suspend) so the app doesn't write mid-restore; after restore, verify `alembic_version` matches the deployed release head (restores from before a migration require deploying the matching older ref or re-running `alembic upgrade head`); resume service; verify §1 steps 2–4.
- *Post-restore reconciliation:* trend samples uploaded after the backup point are gone from the cloud but also purged from edges (edge deletes after successful upload) — that window is lost; note it in the incident record. Jobs in-flight at the restore point may resurrect as `queued`/`claimed`; cancel or let gateways re-execute benign reads.

## 3. Gateway recovery

Field procedures live in `gateway-field-guide.md` §3 (software), §2 (hardware swap). Operator summary: restart → reinstall-preserving-config → reset local DB (loses unsent samples only) → rotate credential → reimage. Cloud-side data (identity, saved inventory, history) survives all of these.

## 4. Credential compromise

**Gateway token exposed:**
1. Immediately revoke: `GET /api/admin/gateways/<id>/credentials` to find it, then `POST /api/admin/credentials/<credential_id>/revoke` (admin token). Takes effect on the gateway's next request.
2. Re-provision the gateway for a fresh token; install per field guide §4.
3. Review that gateway's recent activity (jobs submitted, trend uploads) for anomalies; a gateway token can only touch its own gateway's data [FACT — per-route binding], so blast radius is that gateway.

**Admin token exposed:**
1. Rotate `IOT_ADMIN_API_TOKEN` in Render env (new random value) and redeploy/restart.
2. Update every script/operator copy; staging keeps its own separate token.
3. Review recent admin-route activity in request logs. Blast radius is total — treat as a full incident, audit provisioning and user changes since exposure.

**Pepper (`GATEWAY_AUTH_PEPPER`) exposed:** the pepper alone doesn't grant access (tokens are also required), but rotate deliberately: rotating invalidates *every* gateway token at once. Plan: schedule a rotation window; re-provision all gateways (fleet-wide token reissue) in batches; only then change the pepper. Do NOT change the pepper first — that bricks the fleet's auth instantly. (A dual-pepper verification window is future work — debt register.)

## 5. Database migration rollback

**When:** a deploy's migration failed midway, or a bad migration must be reversed.

1. All project migrations are idempotent/guarded [FACT — 0015/0016 pattern]; a failed `upgrade head` can be re-run safely after fixing the cause. Prefer fix-forward.
2. To reverse deliberately: `alembic downgrade <previous-revision>` using the production `DATABASE_URL` from a trusted machine, service suspended.
3. Redeploy the matching code ref (schema check will hold you honest at startup).
4. Never run migrations from two places concurrently; migration authority is the pre-deploy hook.

## 6. Failed deployment (won't start)

Symptoms: crash-loop, health never green. Causes seen by design: schema drift refusal, staging-guard refusal (staging only), missing env var (pydantic validation error names the variable).
1. Read Render logs — all three failure modes state their cause explicitly [FACT — RuntimeError messages, pydantic errors].
2. Env-var problem → fix env, redeploy same ref. Schema problem → §5. Otherwise → §1 rollback.

## 7. Partial deployment

Single instance means deploys are atomic swaps; "partial" applies to: (a) migration applied but deploy then failed → old code + new additive schema = safe; complete the deploy or roll back code only. (b) Edge fleet mid-rollout when a problem is found → stop the batch, roll updated gateways back per field guide §7, record versions from heartbeat data (the version distribution *is* your partial-state inventory).

## 8. Gateway reconnect storms

After cloud downtime, all gateways retry on their next cycles; load spreads over the heartbeat interval naturally and trend backoff prevents upload stampedes [FACT — edge retry design]. Action: none, unless 5xx persists >5 min after recovery — then check DB connection headroom (`/health/db`, Supabase dashboard).

## 9. Trend recovery

- Edge-side: buffered and retried automatically within the bounded backlog; watch `trend_pending_upload_count` fall.
- Backlog at cap (`trend_queue_max_pending_samples`): oldest unsent samples are dropped by design — record the gap; do not "recover" by clearing the queue (that loses more).
- Cloud-side gaps after restore: see §2 reconciliation.
- Duplicate-safe: retries can never duplicate samples [FACT — idempotent ingestion].

## 10. Job recovery

- Stuck `claimed` job (gateway died mid-execution): non-write jobs are requeued automatically at the gateway's next poll once claimed longer than `JOB_CLAIM_TIMEOUT_SEC` (default 600 s). **BACnet write jobs are deliberately excluded** — they stay `claimed` for manual review: check the write-batch status view and decide; never blind-retry BACnet writes.
- Duplicate result submissions overwrite (last-write-wins) [FACT]; if results look wrong, check `completed_at` ordering in job history.
- After cloud restore (§2): audit `queued`/`claimed` jobs; cancel anything actuating (writes) unless verified still intended.

## Contact / escalation template

| Role | Who | When |
|---|---|---|
| Platform operator (deploys, Render/Supabase) | ______ | Any cloud incident |
| Engineering (DB row surgery, migrations) | ______ | §4 revocations, §5 |
| Field technician dispatch | ______ | Gateway hardware/site issues |
