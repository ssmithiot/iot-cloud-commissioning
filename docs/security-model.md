# Security Model

This document captures security boundaries for the current MVP and the Supabase-ready target architecture.

## Current MVP

- FastAPI is the only cloud API adapter.
- Edge gateways call the FastAPI API through `cloud_url`.
- Edge gateways store local state in SQLite.
- No live Supabase credentials are committed.
- No user login or portal access is implemented yet.

## Credential Rules

- Do not commit Supabase service-role keys.
- Do not commit Supabase anon keys.
- Do not commit real database URLs.
- Do not commit customer, site, or gateway secrets.
- Edge gateways must not hold Supabase service-role keys.
- Edge gateways must not connect directly to Postgres.
- Edge gateways should authenticate to a cloud API endpoint in a future MVP.

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

- Gateway authentication strategy.
- User roles and permission levels.
- Portal session handling.
- RLS policy test harness.
- Storage bucket layout.
- Realtime channel authorization.
