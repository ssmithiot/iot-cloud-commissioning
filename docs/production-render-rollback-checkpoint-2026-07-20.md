# Production Render rollback checkpoint — Item 2 (2026-07-20)

Purpose: capture and validate the production Render rollback checkpoint
BEFORE the Edge-authoritative cloud deploy. Nothing here deploys, changes
env vars, or writes data. All findings below came from public read-only
`/health` endpoints — no credentials were used (the repo `.env` holds no
Render key; `.secrets/` holds only a GW006 gateway token, unusable for Render).

## Verified by direct inspection (read-only)

| Fact | Value | How verified |
|---|---|---|
| Actual production service | `iot-cloud-api-dev.onrender.com` | `GET /health` → `environment: production` |
| Stale URL confirmed dead | `iot-cloud-api.onrender.com` | `GET /health` → empty (no service) |
| Staging service | `iot-cloud-api-staging.onrender.com` | `GET /health/schema` → ok |
| **Production current migration head** | **`0021_gateway_ui_only_updates`** | `GET /health/schema` current_revisions |
| Production `auto_create_tables` | **`true`** (status `development_auto_create`) | `GET /health/schema` |
| Staging migration head | `0022_edge_local_trend_samples`, auto_create `false` | `GET /health/schema` |
| Candidate branch | `codex/gw006-edge-mirror-staging` | git ls-remote |
| Candidate head (pre-fix) | `7a368c5` | git |
| **Candidate head (with pilot fix + runbook)** | **`e970d2e`** | git (this session) |
| Migration path prod→candidate | `0021 → 0022` (single additive step) | `alembic upgrade head` on fresh db + down/up round-trip of 0022, verified this session |

## Standing risk found (surface before deploy — do NOT change without approval)

Production runs with `AUTO_CREATE_TABLES=true` (`/health/schema` status
`development_auto_create`). This is the escape hatch left on from the
2026-07-13 incident. Consequences for this deploy:
- The Alembic pre-deploy still applies `0022` (migration_authority: alembic),
  so the forward path is fine.
- But the schema drift gate is effectively bypassed while it's on, and
  `create_all` may create model tables outside migration control.
- Recommended (separate, deliberate change — not part of this deploy):
  after the pilot, realign `AUTO_CREATE_TABLES=false` on production so the
  schema gate protects it again. Flagged only; not changed here.

## Blocked — requires Render dashboard/account access (no repo credential exists)

These four Item-2 sub-steps cannot be done from this environment. Exact
operator steps:

1. **Record current production deployment ID + commit.**
   Render dashboard → service `iot-cloud-api-dev` → *Events*/*Deploys* tab →
   the live deploy. Record: deploy ID (`dep-...`) and the Git commit SHA it
   built. (Public `/health` cannot expose the SHA; `version` is a static
   `0.1.0`. Expect the commit to be one whose Alembic head is `0021` — i.e.
   at or before `0021_gateway_ui_only_updates`, before `0022` existed.)

2. **Name the pre-production checkpoint.** That live deploy IS the rollback
   target. Note its `dep-...` ID as `ROLLBACK_TARGET`. (Render keeps prior
   deploys; no snapshot to create — just record the ID.)

3. **Confirm the rollback path.** In the same Deploys list, confirm the
   previous successful deploy shows a **Rollback** action. Do not click it;
   just confirm it's available and record its `dep-...` ID.

4. **Record candidate deployment health (post-deploy, later).** After the
   manual deploy of `e970d2e`, record the new `dep-...` ID and confirm
   `/health/schema` current_revisions = `["0022_edge_local_trend_samples"]`.

## Database-level rollback note

`0022_edge_local_trend_samples` has a working `downgrade` (verified
down→up round-trip this session), and it is additive (new Edge-sample
storage), so a Render deploy rollback to the `0021` deploy does not require
a destructive DB downgrade to remain functional — the older code simply
ignores the new table. Prefer Render deploy rollback over DB downgrade;
keep the new table (it holds Edge-owned history).

## Status

- Production service, current head, and migration path: **VERIFIED (read-only).**
- Deployment ID / checkpoint / rollback-button confirmation: **BLOCKED on
  Render dashboard access** — operator steps above.
- No production change of any kind was made.
