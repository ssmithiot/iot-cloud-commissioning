# Supabase Plan

Supabase Postgres is the target cloud system of record for IOT Cloud Commissioning. MVP-004 prepares schema and documentation without connecting to a live Supabase project.

## What Exists Now

- FastAPI cloud API.
- SQLAlchemy models for current MVP tables.
- Docker/PostgreSQL development support.
- Edge agent heartbeat and job polling.
- SQLite edge runtime database.

## What MVP-004 Adds

- Reviewable SQL migrations under `supabase/migrations/`.
- RLS preparation for portal-facing data.
- Security and API contract documentation.
- Future table placeholders for auth, permissions, files, trends, and BACnet device records.

## What MVP-004 Does Not Do

- No live Supabase project connection.
- No Supabase credentials.
- No user login.
- No frontend.
- No trend upload pipeline.
- No direct edge-to-Supabase access.

## Migration Strategy

The SQL migrations are intended for review before applying to a real Supabase project:

1. `0001_core_schema.sql`: current organization, site, edge node, and heartbeat tables.
2. `0002_edge_jobs.sql`: current cloud-to-edge job table.
3. `0003_security_foundation.sql`: profiles, memberships, permissions, and audit events.
4. `0004_future_features.sql`: file metadata, trend upload placeholders, point samples, and BACnet device placeholders.

When a real Supabase project exists, compare these migrations with the active SQLAlchemy model and decide whether FastAPI should continue to manage migrations or defer cloud schema management to Supabase migrations.

## Future Edge Functions

Some FastAPI endpoints may later move to Supabase Edge Functions:

- Gateway heartbeat ingestion.
- Job claim transaction.
- Job result posting.
- Portal job creation.

The edge agent should not need to know whether an endpoint is served by FastAPI, Edge Functions, or another API adapter. It should keep using `cloud_url`.

## Future Vercel Portal

Vercel is the planned host for the web portal. The portal should use Supabase Auth for user sessions and call either:

- Supabase directly where RLS policies are complete and tested.
- Server-side API endpoints where privileged actions or gateway workflows are needed.

## Readiness Checklist

- Keep SQL migrations free of live secrets.
- Keep RLS enabled without broad public policies.
- Keep edge gateway APIs server-side.
- Keep gateway credentials separate from browser user credentials.
- Add policy tests before exposing portal data directly through Supabase clients.
