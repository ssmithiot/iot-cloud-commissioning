# GW032 trend backlog incident — July 13, 2026

## Outcome

The active cloud trend configuration count for `GW032` is now **zero**. The edge agent will no longer create new trend samples for the retired points.

This document records the production defect and the required follow-up work. It intentionally does **not** implement a fix.

## What happened

An operator enabled one-minute trends for approximately 356 points on GW032. The edge upload queue grew to more than 15,000 pending uploads.

Observed gateway state:

- `16,692` pending `trend_sample` queue rows.
- `10,700` uploaded `trend_sample` queue rows.
- No queued jobs were involved.
- Pending samples spanned approximately 83 minutes.
- The edge agent repeatedly logged 20-second timeouts to the hosted trend-sync API while heartbeats later succeeded.
- The tunnel continued to work and is not implicated.

## Root cause

Point removal is a **soft delete**:

- `DELETE /api/ui/points/{point_id}` marks the saved point `enabled = false` and sets its lifecycle state to `retired`.
- `POST /api/ui/points/bulk-remove` does the same for bulk removal.

However, the associated `PointTrendConfig` remains `enabled = true`.

The edge configuration endpoint currently selects enabled trend configurations by gateway only:

```python
select(PointTrendConfig).where(
    PointTrendConfig.gateway_id == gateway_id,
    PointTrendConfig.enabled.is_(True),
)
```

It does not require the associated `SavedBacnetPoint.enabled` to be true. Therefore, retired points disappear from the cloud tree but remain in the edge agent's trend workload.

Evidence:

- The authenticated cloud tree endpoint returned zero visible saved points for GW032.
- The authenticated edge trend-config endpoint returned 256 enabled configurations.
- After disabling enabled configurations whose points were retired, the edge endpoint returned zero configurations.

## Immediate production recovery performed

The stale configurations were disabled directly in the cloud database with the following targeted statement:

```sql
UPDATE point_trend_configs AS trend
SET enabled = false,
    updated_at = NOW()
FROM saved_bacnet_points AS point
WHERE trend.point_id = point.id
  AND trend.gateway_id = 'GW032'
  AND point.enabled = false
  AND trend.enabled = true;
```

This changes only trend configurations for retired GW032 points. It does not delete trend history, change BACnet data, or modify tunnel configuration.

## Required code changes

1. **Do not send retired/disabled points to the edge.**

   Update `GET /api/edge/{gateway_id}/trend-configs` to join or otherwise require `SavedBacnetPoint.enabled.is_(True)` in addition to `PointTrendConfig.enabled.is_(True)`.

2. **Disable a point's trend configuration during retirement.**

   In both individual and bulk point retirement routes, set the related `PointTrendConfig.enabled = false` in the same database transaction.

3. **Disable trends when a device is retired.**

   Retiring a saved controller already retires its points. It must also disable all trend configurations for those points.

4. **Add an administrative repair operation.**

   Provide a safe, explicit maintenance command or protected admin endpoint to disable all trend configurations for retired/disabled points. It must be idempotent and report its affected count.

5. **Bound the edge queue and expose its health.**

   The installed 0.1.5 agent allowed the trend queue to grow without a maximum. Keep the in-progress hardening work for a bounded local backlog, retry backoff, and separate trend-backlog heartbeat metrics. Do not deploy it until staging validation is complete.

6. **Make trend ingestion resilient under slow cloud responses.**

   Preserve idempotency, use bounded upload batches, and retain retry/backoff diagnostics. A transient trend upload failure must not trigger unlimited local storage growth.

## Required tests

- Retiring one point disables its trend config and removes it from the edge trend-config response.
- Bulk-retiring points disables all associated trend configs.
- Retiring a device disables trends on all its points.
- A retired point with a deliberately enabled legacy trend config is excluded by the edge trend-config endpoint.
- Re-running the administrative repair operation is safe and reports zero changes after the first run.
- Edge queue tests cover capacity reached, upload timeout, retry delay, successful recovery, and no new samples after the cloud returns an empty config list.
- Cloud endpoint test verifies the UI tree and edge trend-config endpoint cannot disagree about retired points.

## Remaining GW032 cleanup

The local SQLite queue still contains stale pending trend samples from this incident. Once the cloud count remains zero after one more edge loop, back up the GW032 edge database and delete only pending rows where `item_type = 'trend_sample'`, then restart `iot-cx-agent.service` so the tunnel reconnects. Do not delete jobs or replace the entire SQLite database.

## Fix implemented — 2026-07-13 (branch `codex/trend-hardening`, pending review/deploy)

Required code changes 1–4 are implemented locally with the required tests:

1. `GET /api/edge/{gateway_id}/trend-configs` now joins `SavedBacnetPoint` and requires `enabled.is_(True)` on both the config and the point.
2. `DELETE /api/ui/points/{point_id}` and `POST /api/ui/points/bulk-remove` disable the related trend configs in the same transaction (`_disable_trend_configs_for_points`).
3. Device retirement (`DELETE /api/ui/devices/{device_id}`) disables trend configs for all of the device's points in the same transaction.
4. Idempotent repair endpoint: `POST /api/admin/maintenance/disable-retired-trend-configs[?gateway_id=GW###]` (admin only) reports `disabled_count`; re-running reports zero. This supersedes the raw SQL used during recovery.

Items 5–6 are the Phase 1 trend-hardening work already on this branch (bounded edge backlog, retry backoff, backlog heartbeat telemetry, idempotent bounded ingestion), awaiting staging validation before deploy. The Phase 2/3 alerting groundwork (`trend_backlog` alert on `trend_oldest_pending_at` age) would have flagged this incident within one evaluation cycle.

Tests: `cloud-api/tests/test_trend_config_retirement.py` (6 tests covering every scenario in "Required tests" above, including the exact legacy defect state and tree/edge agreement). Full cloud regression green apart from the one documented pre-existing workspace-UI failure.

Still outstanding from this incident: GW032 edge SQLite pending-row cleanup (operator, per "Remaining GW032 cleanup"), and the GW032 token rotation (security note below) — the new credential list/revoke endpoints on this branch make rotation API-driven once deployed; until then use provision + manual revoke.

## Security note

During diagnosis, a gateway credential file was mistakenly sourced as a shell environment file and the terminal echoed its raw value. Rotate the GW032 gateway API token as part of follow-up recovery, using the normal provisioning/token rotation process. Do not place gateway tokens in tickets, logs, source control, or this document.
