# Supabase Schema Plan

This folder contains reviewable SQL migrations for the future Supabase Postgres cloud database.

These migrations are not connected to a live Supabase project yet. They include no service-role keys, anon keys, database URLs, or customer secrets.

Current intent:

- Supabase Postgres is the long-term cloud system of record.
- FastAPI remains the current cloud API adapter.
- Edge gateways keep calling cloud API endpoints only.
- Edge gateways do not connect directly to Supabase or Postgres.
- RLS is enabled for future portal-facing tables, but permissive public policies are intentionally not created.

Migration order:

1. `0001_core_schema.sql`
2. `0002_edge_jobs.sql`
3. `0003_security_foundation.sql`
4. `0004_future_features.sql`
5. `0005_gateway_credentials.sql`

Review these files before applying them to a real Supabase project.
