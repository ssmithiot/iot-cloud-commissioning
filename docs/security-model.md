# Security Model

This document captures security boundaries for the current MVP and the Supabase-ready target architecture.

## Current MVP

- FastAPI is the only cloud API adapter.
- Edge gateways call the FastAPI API through `cloud_url`.
- Edge gateways store local state in SQLite.
- Edge-facing FastAPI endpoints require gateway API tokens.
- Operator/admin FastAPI endpoints accept either `IOT_ADMIN_API_TOKEN` for automation or a verified Supabase Auth user JWT with an active local app role.
- No live Supabase credentials are committed.
- Supabase Auth owns email/password signup, password storage, and email confirmation.

## Credential Rules

- Do not commit Supabase service-role keys.
- Do not commit Supabase anon keys.
- Do not commit real database URLs.
- Do not commit customer, site, or gateway secrets.
- Do not commit `IOT_ADMIN_API_TOKEN`.
- Edge gateways must not hold Supabase service-role keys.
- Edge gateways must not connect directly to Postgres.
- Edge gateways must not hold `IOT_ADMIN_API_TOKEN`.
- Edge gateways must not hold `GATEWAY_AUTH_PEPPER`.
- Edge gateways authenticate to the cloud API with only their gateway API token.
- Gateway tokens are installed only in `/etc/iot-cx-agent/edge-agent.env`.

## Admin And Gateway API Auth

Gateway API tokens use a server-generated `iotcc_gw_` token. The raw token is returned once during cloud-side provisioning and installed on the gateway. FastAPI stores only the token prefix and a server-side HMAC-SHA256 hash using `GATEWAY_AUTH_PEPPER`.

The admin/operator automation token is configured with `IOT_ADMIN_API_TOKEN` in the FastAPI environment. It remains available for smoke tests, scripts, and emergency administration. This token is not a gateway credential and must never be copied to edge gateways.

Human users authenticate through Supabase Auth. The operator username is the user's email address. Supabase sends and verifies the confirmation email. FastAPI verifies the Supabase JWT, then checks the local `operator_users` row for app role and status. Email confirmation proves email ownership; local role approval decides what the user can do.

JWT verification supports both Supabase signing models:

- Legacy `HS256` tokens verify with server-side `SUPABASE_JWT_SECRET`.
- Newer `RS256` or `ES256` signing-key tokens verify through the Supabase JWKS endpoint derived from `SUPABASE_URL`, or from explicit `SUPABASE_JWKS_URL`.

The browser UI may receive only public Supabase browser configuration:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`

The browser UI must not receive service-role keys, database URLs, `IOT_ADMIN_API_TOKEN`, `GATEWAY_AUTH_PEPPER`, or `SUPABASE_JWT_SECRET`.

Supabase Auth URL Configuration for the live dev deployment must use:

- Site URL: `https://iot-cloud-api-dev.onrender.com`
- Redirect allow list: production app URLs, including `https://iot-cloud-api-dev.onrender.com/login`

The confirmation email redirect must not point to localhost in production. The signup UI sends Supabase `emailRedirectTo` as `${window.location.origin}/login`.

Operator role behavior:

- `pending`: registered but not approved; cannot use operator routes.
- `viewer`: can view gateway and job state.
- `operator`: can view state and queue commissioning jobs.
- `admin`: can manage users and perform admin-only gateway provisioning.
- `disabled`: blocked.

Current protected route groups:

- Gateway token: `POST /api/edge/heartbeat`
- Gateway token: `GET /api/edge/{gateway_id}/jobs/next`
- Gateway token: `POST /api/edge/jobs/{job_id}/result`
- Admin token or active user: `GET /api/edge/gateways`
- Admin token or active admin/operator user: `POST /api/edge/jobs`
- Admin token or active user: `GET /api/edge/jobs`
- Admin token or active admin user: `POST /api/admin/gateways/provision`
- Admin token or active admin user: `GET /api/admin/users`
- Admin token or active admin user: `PUT /api/admin/users/{email}`
- Supabase user token: `POST /api/auth/register`
- Admin token or active user: `GET /api/auth/me`

The cloud-side provisioning endpoint can create or update the site and gateway identity and issue a new gateway token. The response includes the raw gateway token once; the server cannot recover the raw token later.

## Supabase Auth And Future RLS

Supabase Auth represents human users for the future web portal. FastAPI currently verifies Supabase JWTs and enforces local app roles server-side. Portal-facing tables should later use Row Level Security where browser clients read directly from Supabase. MVP migrations enable RLS where appropriate but do not add broad public policies.

Expected future model:

- `operator_users` maps authenticated email users to app-level roles for the current FastAPI portal API.
- `profiles` may later map authenticated users to broader app-level profile records.
- `organization_memberships` defines organization access.
- `site_permissions` scopes access to sites.
- `edge_node_permissions` scopes access to individual gateways where needed.
- `audit_events` records security-relevant actions.

Until browser RLS policies are complete, server-side FastAPI access remains the supported path for privileged commissioning workflows.

## Edge Gateway Boundary

Gateway communication is outbound only:

1. Heartbeat.
2. Job polling.
3. Job result posting.
4. Future upload queues.

BACnet traffic stays local to the gateway LAN. The cloud stores summaries, status, and results, not direct BACnet network access.

## RLS Posture

Supabase migrations enable RLS on tables intended for future portal exposure. They intentionally avoid permissive policies while the auth model is not wired into application code.

Policy work should be explicit and tested later:

- Organization users can read their organizations.
- Site users can read authorized sites.
- Gateway users can read authorized edge nodes and job history.
- Admin roles can manage jobs and permissions.
- Storage access is scoped to organization and site permissions.

## Audit Events

Future server-side code should write audit events for:

- Gateway enrollment.
- Job creation.
- Job claim and completion failures.
- Permission changes.
- File/report upload and deletion.
- Auth-sensitive portal actions.

## Open Items

- Polished portal navigation and UX beyond the minimal MVP-013 pages.
- Replacement of the shared admin token as the normal human workflow.
- RLS policy test harness.
- Storage bucket layout.
- Realtime channel authorization.
