# Security Model

This document captures security boundaries for the current MVP and the Supabase-ready target architecture.

## Current MVP

- FastAPI is the only cloud API adapter.
- Edge gateways call the FastAPI API through `cloud_url`.
- Edge gateways store local state in SQLite.
- Edge-facing FastAPI endpoints require gateway API tokens.
- Operator/admin FastAPI endpoints require `IOT_ADMIN_API_TOKEN`.
- No live Supabase credentials are committed.
- No user login or portal access is implemented yet.

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

The admin/operator token is configured with `IOT_ADMIN_API_TOKEN` in the FastAPI environment. It protects cloud/operator routes such as gateway listing, job creation, job listing, and cloud-side gateway provisioning. This token is not a gateway credential and must never be copied to edge gateways.

Current protected route groups:

- Gateway token: `POST /api/edge/heartbeat`
- Gateway token: `GET /api/edge/{gateway_id}/jobs/next`
- Gateway token: `POST /api/edge/jobs/{job_id}/result`
- Admin token: `GET /api/edge/gateways`
- Admin token: `POST /api/edge/jobs`
- Admin token: `GET /api/edge/jobs`
- Admin token: `POST /api/admin/gateways/provision`

The cloud-side provisioning endpoint can create or update the site and gateway identity and issue a new gateway token. The response includes the raw gateway token once; the server cannot recover the raw token later.

## Future Supabase Auth And RLS

Supabase Auth will represent human users for the future web portal. Portal-facing tables should use Row Level Security. MVP migrations enable RLS where appropriate but do not add broad public policies.

Expected future model:

- `profiles` maps authenticated users to app-level profile records.
- `organization_memberships` defines organization access.
- `site_permissions` scopes access to sites.
- `edge_node_permissions` scopes access to individual gateways where needed.
- `audit_events` records security-relevant actions.

Until auth is implemented, server-side API access remains the only supported path.

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

- User roles and permission levels.
- Portal session handling.
- Replacement of the shared admin token with user-scoped portal auth.
- RLS policy test harness.
- Storage bucket layout.
- Realtime channel authorization.
