# Staging Environment Setup Checklist

Goal: a staging pair (Render service + Supabase project) fully isolated from production (`iot-cloud-api-dev.onrender.com`). Prerequisite for the trend-hardening staging validation and the Phase 2 load tests.

## 1. Supabase (staging project)

- [ ] Create a **new** Supabase project (free tier is fine). Do not reuse the production project or any of its keys.
- [ ] Record the pooled connection string (Transaction mode pooler) as the staging `DATABASE_URL`; note the plan's connection limit for later pool tuning.
- [ ] Enable email auth the same way production has it; set Auth Site URL to the staging Render URL (not production, not localhost).
- [ ] Copy the JWT secret / JWKS settings into the staging env vars only.

## 2. Render (staging service)

- [ ] Create a **new** Render web service from the same repo/branch you deploy today (production keeps deploying from its own service untouched).
- [ ] Name it unmistakably, e.g. `iot-cloud-api-staging`. Never attach the production custom domain.
- [ ] Set environment variables from `.env.example`, all staging-specific (full classified list: `docs/staging-environment-variables.md`):
  - `ENVIRONMENT=staging` (shown on `/health`; activates the production-resource startup guard)
  - `ALLOW_PRODUCTION_RESOURCES=false` (or leave unset)
  - `PRODUCTION_RESOURCE_FINGERPRINTS=iot-cloud-api-dev.onrender.com,<production-supabase-project-ref>`
  - `DATABASE_URL` → staging Supabase pooler string
  - `GATEWAY_AUTH_PEPPER` → newly generated, different from production
  - `IOT_ADMIN_API_TOKEN` → newly generated, different from production
  - `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET` → staging project values
  - `AUTO_CREATE_TABLES=false`
  - `TREND_RETENTION_DAYS`, `HEARTBEAT_RETENTION_DAYS` → explicit values (defaults 90/30 are fine)
  - Leave `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` at defaults until Level 2/3 load tests say otherwise.
- [ ] Configure the pre-deploy command to run `alembic upgrade head` (same pattern as production) so migrations have a single authority.

## 3. First deploy verification

Use the copy-ready command sequence and record results in `docs/staging-first-deploy-worksheet.md`. Summary:

- [ ] `GET /health` → `{"status":"ok","environment":"staging","version":...}` — the environment field is the identity check.
- [ ] If the service refuses to start with "configured with known production resources", a fingerprint matched: fix the env vars (do not set `ALLOW_PRODUCTION_RESOURCES=true`).
- [ ] `GET /health/db` → ok. `GET /health/schema` → expected revisions include `0017_gateway_alert_states` (once this branch is approved and deployed to staging; until then, current head).
- [ ] Log in with a staging Supabase user; approve it via the admin users page using the staging admin token.
- [ ] Confirm the staging admin token does NOT work against production and vice versa (proves the secrets are distinct).

## 4. Isolation guarantees (must all be true)

- [ ] No production gateway is configured to point at the staging URL.
- [ ] No production tunnel connects to staging; no staging tunnel targets a production gateway.
- [ ] Staging Supabase project contains no production data.
- [ ] Production env vars were never pasted into the staging service (pepper, admin token, DB URL all differ).
- [ ] `tools/staging_load_harness.py` refuses the production host; run it once with the production URL to verify the guard, then only ever use the staging URL.

## 5. Seed a synthetic gateway (no hardware needed)

- [ ] `POST /api/admin/gateways/provision` with the staging admin token, e.g. gateway `STG-GW001`, site `staging-site` — record the returned `gateway_api_token`.
- [ ] Either point a spare/dev edge device at staging with that token, or use the load harness at Level 1 (it provisions `LOADTEST-*` gateways itself).

## 6. Then, in order

1. Run the trend-hardening checklist: `docs/staging-trend-validation-checklist.md`.
2. Run load tests Levels 1–3: `docs/cloud-platform-staging-load-test-plan.md`.
3. Feed measurements back into `docs/cloud-platform-scaling-assessment.md` (replace UNKNOWN/PROJECTED entries with measured values).
4. Clean up `LOADTEST-*` data after each session.

## Cost note

Free/starter tiers are sufficient: staging does not need production-grade compute. Supabase free-tier projects pause after inactivity — resume before test sessions, and treat pause/resume as the Level 5 database-outage drill.
