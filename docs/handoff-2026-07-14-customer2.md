# Handoff — 2026-07-14 — Customer 2 tenancy preparation

## Stop point

Work stopped deliberately after the first, local-only Customer 2 tenancy slice. No production or staging database query, data backfill, Render configuration change, deployment, or push was performed.

**Canonical repository:** `C:\Dev\iot-cloud-commissioning` only. Do not use or modify the OneDrive clone for this work.

**Working branch:** `customer2-tenancy-slice1`.

## What was completed today

1. A separate staging Render service is live and its release gate passed **13/13 checks**. This is a normal pre-production test environment; load testing is deferred and is not the current priority.
2. `staging-release-gate` was merged into `codex/trend-hardening` via PR #29. It is a manual safety tool, not a production feature or a required next task.
3. The Customer 2 read-only audit identified two tenant-boundary escape hatches:
   - `role == "admin"` and `IOT_ADMIN_API_TOKEN` are intentional global, internal-only platform access.
   - A non-admin user with no organization or site memberships currently receives the legacy global-visibility fallback.
4. Customer 2 Slice 1 is implemented locally but **not committed**:
   - adds `REQUIRE_EXPLICIT_MEMBERSHIP` (default `false`);
   - preserves legacy behavior while false;
   - makes a zero-membership non-admin operator/viewer see nothing while true;
   - preserves intentional global access for platform admins and the shared admin token;
   - adds focused helper- and HTTP-level isolation tests;
   - documents the temporary rollout flag and backfill prerequisite.

## Current local changes (do not discard)

Customer 2 Slice 1 files:

- `.env.example`
- `cloud-api/app/access.py`
- `cloud-api/app/config.py`
- `cloud-api/tests/test_site_access.py`
- `cloud-api/tests/test_tenancy_isolation.py` (new)
- `docs/staging-environment-variables.md`
- `docs/technical-debt-register.md`

Focused verification re-run after review:

```powershell
pytest -q cloud-api/tests/test_site_access.py cloud-api/tests/test_tenancy_isolation.py
```

Result: **9 passed**. The implementation report also recorded the cloud suite as passing except for one known, pre-existing UI assertion that CI already excludes.

Unrelated scratch artifacts are also present (for example `.pytest-tmp/`, `patches-2026-07-14/`, and SQLite journal/stale files). Leave them alone; they are not part of this work.

## Customer 2 v1 policy (approved direction)

- Platform admins and `IOT_ADMIN_API_TOKEN` remain internal-only and globally scoped.
- Customer users are scoped `operator` or `viewer` users only.
- No customer receives the shared admin token.
- Internal staff performs onboarding: organization -> sites -> user -> membership -> activation -> gateway provisioning.
- Do not build customer/org-admin self-service in the first Customer 2 slice.

## Important: not Customer 2-ready yet

Slice 1 only protects resource paths that use the existing site-scoping helpers. The audit found remaining role-only routes that can cross tenant boundaries and must be addressed before Customer 2 is activated:

- gateway credentials list/revoke;
- tunnel status/session routes;
- BACnet write-batch create/approve/audit routes;
- commissioning-template import;
- any remaining gateway-update/admin route exposed to customer users.

The next implementation slice should audit and scope these routes, starting with credentials and BACnet write batches. Do not claim multi-tenant readiness until that pass and its cross-organization tests are complete.

## Explicitly deferred

- Staging Levels 1–3 load testing.
- Alert webhook configuration/testing.
- Customer 1 production inventory queries and membership backfill.
- Setting `REQUIRE_EXPLICIT_MEMBERSHIP=true` in staging or production.
- Any Supabase/Render environment changes.

## Tomorrow’s restart sequence

1. Review and, if approved, commit Customer 2 Slice 1 only. Keep `REQUIRE_EXPLICIT_MEMBERSHIP=false`.
2. Start Customer 2 Slice 2: make the remaining customer-reachable credential/tunnel/write/template routes organization/site scoped, with guessed-ID cross-tenant tests.
3. After all customer-reachable paths are scoped, prepare (but do not execute without review) the Customer 1 inventory/backfill and flag-cutover runbook.
4. Only then stage the data migration, verify it, and enable explicit membership as a separate controlled rollout.

