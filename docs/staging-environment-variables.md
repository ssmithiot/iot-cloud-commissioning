# Staging Environment Variables — Complete Checklist

Audited against `cloud-api/app/config.py` on branch `codex/trend-hardening` (includes migrations through `0016_scaling_foundations` and the environment-identity change). Every variable the application reads is listed; nothing else is consumed at runtime.

Classification key: **REQ** required · **OPT** optional (validated default) · **STG** staging-specific value · **SECRET** never log/share · **NEVER-MATCH** must never equal the production value · **STRUCT** structurally same as production, different value.

## Environment identity (new)

| Variable | Class | Staging value | Notes |
|---|---|---|---|
| `ENVIRONMENT` | REQ for staging · STG | `staging` | Literal: development/staging/production. Shown on `/health`. Default `development` if unset. |
| `ALLOW_PRODUCTION_RESOURCES` | OPT | `false` (or unset) | Escape hatch for the startup guard. Never set true in staging. |
| `PRODUCTION_RESOURCE_FINGERPRINTS` | OPT · STRUCT | default + production Supabase project ref | Comma-separated substrings. Default already contains `iot-cloud-api-dev.onrender.com`. **Action: append the production Supabase project ref** once you have it, e.g. `iot-cloud-api-dev.onrender.com,<prod-project-ref>`. |

## Database connection

| Variable | Class | Notes |
|---|---|---|
| `DATABASE_URL` (alias `CLOUD_DATABASE_URL`) | REQ · SECRET · NEVER-MATCH · STG | Staging Supabase pooler string. The startup guard refuses it in staging if it matches a fingerprint. |
| `AUTO_CREATE_TABLES` | REQ | `false` in staging (Alembic is the schema authority; startup fails if schema ≠ head). |
| `DB_POOL_SIZE` | OPT (default 5) | Leave default until Level 2/3 load tests. |
| `DB_MAX_OVERFLOW` | OPT (default 10) | Same. |
| `DB_POOL_TIMEOUT` | OPT (default 30 s) | Same. |
| `DB_POOL_RECYCLE` | OPT (default 1800 s) | Below managed-Postgres idle timeouts. |

## Authentication and users

| Variable | Class | Notes |
|---|---|---|
| `GATEWAY_AUTH_PEPPER` | REQ · SECRET · NEVER-MATCH | Generate fresh for staging. A shared pepper would make production gateway tokens verifiable in staging. |
| `IOT_ADMIN_API_TOKEN` | REQ · SECRET · NEVER-MATCH | Fresh staging token. Verify it does NOT authenticate against production (worksheet step). |
| `SUPABASE_JWT_SECRET` | REQ* · SECRET · NEVER-MATCH · STG | From the **staging** Supabase project. *Required for HS256 logins; the staging project's value. |
| `SUPABASE_JWT_AUDIENCE` | OPT (default `authenticated`) · STRUCT | Same value as production is fine (it is not a secret). |
| `SUPABASE_URL` | REQ for browser login · STG · NEVER-MATCH | `https://<staging-ref>.supabase.co`. |
| `SUPABASE_ANON_KEY` | REQ for browser login · STG · NEVER-MATCH | Staging project anon key (public-ish, still per-project). |
| `SUPABASE_JWKS_URL` | OPT · STG | Leave empty (derived from `SUPABASE_URL`). |

## Gateway tokens

No env vars — gateway credentials are rows created via `POST /api/admin/gateways/provision` (hashed with the staging pepper). Staging tokens therefore cannot work against production and vice versa as long as peppers differ.

## Email / credential workflows

Handled entirely by Supabase Auth (no SMTP config in this app). In the **staging Supabase project**: set Auth Site URL to the staging Render URL; add `https://<staging-service>.onrender.com/login` to redirect allow-list; production URLs must NOT be in the staging allow-list.

## Tenant isolation rollout (temporary)

| Variable | Class | Staging value | Notes |
|---|---|---|---|
| `REQUIRE_EXPLICIT_MEMBERSHIP` | OPT (default `false`) | `false` until Customer 1 backfill is verified complete | Customer 2 prep, added 2026-07-14 (`docs/technical-debt-register.md` Tier 2 #9). While `false`, `app.access.visible_site_ids` keeps the legacy fallback: an active operator/viewer with zero organization and zero site memberships sees every site (fail open). Flipping to `true` makes that same zero-membership case see nothing (fail closed) — do **not** flip in any environment until every active operator/viewer has an explicit membership row, or existing accounts will be locked out with no visible sites. `role=="admin"` operators and the shared admin token are unaffected either way; they retain global access before this fallback is ever reached. No Customer 1 backfill has been run yet — this flag must stay `false` in staging and production until that backfill is executed and verified. |

## Trend and heartbeat retention

| Variable | Class | Notes |
|---|---|---|
| `TREND_RETENTION_DAYS` | OPT (default 90) | Set explicitly in staging (checklist asks for a deliberate value). |
| `HEARTBEAT_RETENTION_DAYS` | OPT (default 30) | Same. |

## Telemetry / status behavior

| Variable | Class | Notes |
|---|---|---|
| `AUTH_TELEMETRY_MIN_INTERVAL_SEC` | OPT (default 60) | Auth telemetry write throttle. |
| `JOB_CLAIM_TIMEOUT_SEC` | OPT (default 600) | Stale-claim requeue window (BACnet writes excluded). |
| `ALERT_WEBHOOK_URL` | OPT · STG | Leave empty for silent groundwork mode; use a staging-only channel if set. Evaluation is triggered by `POST /api/admin/alerts/evaluate` (schedule via Render cron every ~5 min when desired). |
| `ALERT_TREND_BACKLOG_AGE_HOURS` | OPT (default 6) | Backlog alert threshold. |
| `ALERT_MAX_DELIVERIES_PER_RUN` | OPT (default 50) | Delivery ceiling per evaluation. |
| `GATEWAY_STALE_AFTER_SECONDS` | OPT (default 300) | Status thresholds. |
| `GATEWAY_OFFLINE_AFTER_SECONDS` | OPT (default 1800) | Status thresholds. |
| `TUNNEL_REQUEST_TIMEOUT_SEC` | OPT (default 900) | Leave default; no tunnels in staging initially. |

## Not read by the application

`CLOUD_API_HOST` / `CLOUD_API_PORT` appear in `.env.example` for local convenience only — Render supplies the port binding; the Dockerfile CMD hardcodes `0.0.0.0:8000`. Do not add them to Render.

## Render service settings (not env vars)

Pre-deploy command: `alembic upgrade head` (run from `cloud-api/`); service name unmistakably staging (e.g. `iot-cloud-api-staging`); no production custom domain; health check path `/health`.

## What the startup guard can and cannot detect

Can: `DATABASE_URL`, `SUPABASE_URL`, or `SUPABASE_JWKS_URL` containing a listed fingerprint while `ENVIRONMENT=staging`. Cannot: a production value whose fingerprint is not listed, reused peppers/tokens/JWT secrets (hashes and secrets carry no detectable identity), or a production gateway pointed at staging by its own config. Those are covered only by the worksheet checks (token cross-rejection tests, gateway inventory review).
