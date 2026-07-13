# Production Readiness Checklist

**Required before every production deployment.** Print or copy per release; attach the completed copy to the release log entry. All items must be checked or explicitly waived with a written reason.

Release ref/tag: ______ · Date: ______ · Operator: ______ · Approver: ______

## Database
- [ ] Alembic head on the release ref recorded: ______ (must match staging-validated head)
- [ ] Migration round-trip verified locally (upgrade → downgrade → upgrade)
- [ ] All migrations in this release are additive/guarded (no destructive DDL without separate approval)
- [ ] Supabase backup/PITR status confirmed within the last week (dashboard screenshot or note)
- [ ] No manual schema changes were made outside Alembic since last release (`/health/schema` was `ok` before deploy)

## Render
- [ ] Deploying the identical ref that passed staging (no cherry-picks after validation)
- [ ] Pre-deploy command runs `alembic upgrade head`; Dockerfile/pre-deploy migration authority unchanged since last release
- [ ] Previous known-good deploy identified for rollback: ______
- [ ] Operator available for 60 minutes post-deploy

## Supabase
- [ ] Production project untouched by staging activity (no LOADTEST-*/STG-* rows)
- [ ] Connection count headroom confirmed (current vs plan limit)
- [ ] Auth Site URL / redirect allow-list unchanged or intentionally updated

## Authentication
- [ ] `IOT_ADMIN_API_TOKEN` unchanged (or rotation planned with all scripts updated)
- [ ] `GATEWAY_AUTH_PEPPER` unchanged (rotation invalidates every gateway token — never rotate casually; see DR runbook)
- [ ] Staging tokens verified NOT to authenticate against production (worksheet §3 evidence from staging cycle)

## Environment verification
- [ ] Production service sets `ENVIRONMENT=production` (first release after the identity feature: add it)
- [ ] `PRODUCTION_RESOURCE_FINGERPRINTS` on staging includes production host + Supabase ref
- [ ] `.env`/Render env vars reviewed against `staging-environment-variables.md` classifications — no staging values in production

## Gateway provisioning
- [ ] No provisioning changes in this release, or provisioning re-tested in staging (worksheet §4)
- [ ] Gateway credential issuance path untested changes: none

## Heartbeat verification (post-deploy)
- [ ] ≥95% of expected fleet heartbeating within 2 intervals (spot-check UI + summary endpoint)
- [ ] Heartbeat telemetry fields present (backlog counts, versions)

## Trend verification (post-deploy)
- [ ] At least one production point shows fresh samples after deploy
- [ ] No abnormal `trend_pending_upload_count` growth across spot-checked gateways (edge backlogs draining)

## Job verification (post-deploy)
- [ ] One benign job (e.g. bacnet_read on a known point) queued, claimed, completed on a spot-check gateway

## Tunnel verification (post-deploy)
- [ ] Tunnel status endpoint reachable; one gateway tunnel reconnected (expected after restart) and console opens
- [ ] No tunnel code changed in this release (charter rule) — confirm diff

## Health endpoints
- [ ] `GET /health` → ok + correct environment + expected version
- [ ] `GET /health/db` → ok
- [ ] `GET /health/schema` → ok, revisions match release head

## Rollback readiness
- [ ] Rollback procedure read within last 3 releases (DR runbook §Render)
- [ ] Rollback decision criteria understood: failed metrics = roll back, no debate

## Documentation
- [ ] Release log entry drafted (ref, head, metrics evidence)
- [ ] Known-issues list reviewed and updated (below)
- [ ] Any operator-facing behavior change noted for support

## Support contacts
- [ ] Platform owner reachable: ______
- [ ] Render/Supabase account access confirmed (not locked out)
- [ ] Field technician contact for gateway-side issues: ______

## Known issues (standing)
- Pre-existing test failure: `test_gateway_workspace_contains_discovery_progress_ui` (UI assertion, tracked in debt register)
- Tunnel consoles drop on deploy/restart (process-local sessions) — users must reopen; expected
- Production shows `environment: development` until `ENVIRONMENT=production` is set (first release after identity feature)
- No alerting yet: post-deploy monitoring is manual for 30–60 minutes

## Acceptance criteria (gate)
- [ ] Every Release Metric in `release-process.md` is green (attach smoke-check JSON)
- [ ] All sections above checked or waived in writing
- [ ] Approver sign-off recorded

**If any item cannot be checked and is not waived: do not deploy.**
