# Completion Report — Trend Hardening Verification

Date: 2026-07-12
Branch: `codex/trend-hardening` (all work local, uncommitted)

## Summary

The uncommitted work on `codex/trend-hardening` already implements all five remaining charter items. The work was audited end-to-end, the migration verified to round-trip, test suites run, and one small additive doc fix made. No functional gaps found.

Charter item status:

1. **Edge trend validation** — done. `_positive_int` validation for all four trend config values (`trend_upload_batch_size`, `trend_queue_max_pending_samples`, `trend_upload_retry_base_sec`, `trend_upload_retry_max_sec`); covered by tests.
2. **Backlog health end-to-end** — done. `trend_queue_status()` (db.py) → `collect_status()` (status.py) → heartbeat payload (main.py) → `HeartbeatIn` schema → `EdgeNode`/`EdgeHeartbeat` models → migration 0015 → `GatewayOut` + heartbeat-trend endpoint. Tested on both edge and cloud sides.
3. **Ingestion hardening** — done. Batch bounded 1–500, in-batch duplicate rejection (422), per-sample idempotency, `quality`/`source`/`received_at` metadata, migration 0015, tests.
4. **Retrieval hardening** — done. Composite index `(point_id, sampled_at)`, bounded query (limit ≤ 5000, `since` filter), retention pruning on upload (`TREND_RETENTION_DAYS`, default 90), tests.
5. **Staging checklist** — done. `docs/staging-trend-validation-checklist.md` covers separate Render/Supabase, no production gateway/tunnel.

## Files modified (this session)

- `.env.example` — documented optional `TREND_RETENTION_DAYS` (referenced by the staging checklist but previously undocumented). Only change made this session; the rest of the diff was pre-existing work that was verified.

## Tests executed

- Edge: full suite minus `test_version.py` — **65 passed**, 6 failed (all `test_bacnet.py`)
- Cloud: full `test_api.py` (181 tests, chunked) — **180 passed**, 1 failed
- Cloud: `test_bacnet_read_jobs`, `test_legacy_schema_reconciliation`, `test_schema_governance` — **7 passed**
- Migration 0015: upgrade → verify columns/index → downgrade → upgrade on fresh SQLite — **clean round-trip**

## Test results — failures are all environmental or pre-existing

- 6 edge `test_bacnet.py` failures: test sandbox is Linux; stub `bacwi`/`bacrp` files lack the exec bit that Windows implies. Not trend-related.
- `test_version.py`: needs `tomllib` (Python 3.11+); sandbox has 3.10.
- `test_gateway_workspace_contains_discovery_progress_ui`: expects "Remove device" in workspace HTML; absent from `ui.py` at HEAD too — **pre-existing on the branch**, unrelated to trend work. Recorded, not fixed (scope discipline).

## Remaining work

Execute the staging validation checklist against a real staging deployment (needs Render + Supabase staging environments). Re-run the edge and cloud suites on Windows to confirm the bacnet tests pass there.

## Risks

- Retention delete runs inside every successful upload transaction and scans by `sampled_at` across all gateways; fine at current volume, revisit in scaling phase.
- Idempotency check is one SELECT per sample; acceptable for ≤500-sample batches, a candidate for batching later.
- Pre-existing "Remove device" test failure will show up in any full CI run.

## Recommended next objective

Run staging validation per the checklist; once signed off, begin Phase 2 scaling foundations (additive only).
