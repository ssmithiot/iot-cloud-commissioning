# Architecture

IOT Cloud Commissioning uses an outbound edge architecture. Edge gateways run local commissioning logic and connect to the cloud API over HTTPS. BACnet traffic remains local to each gateway.

## Current MVP Shape

- `cloud-api/` is a FastAPI service that exposes the cloud-edge API.
- The cloud database model is implemented with SQLAlchemy.
- `edge-agent/` runs on Ubuntu gateways as `iot-cx-agent`.
- Edge runtime state, offline history, heartbeat logs, job history, and future queues live in SQLite.
- The edge agent calls `cloud_url` endpoints only.
- Provisioned gateways can keep an outbound WebSocket tunnel open for cloud-proxied access to the local gateway UI.
- The cloud can queue work through edge jobs.
- BACnet discovery is executed locally on the edge through the configured `bacwi` command.

## Target Cloud Platform

Supabase Postgres is the long-term cloud system of record. FastAPI is currently a thin replaceable cloud API adapter. The API adapter owns cloud-edge request validation, gateway authentication later, and server-side database access.

Future platform services:

- Supabase Auth for web portal users.
- Supabase Row Level Security for portal-facing tables.
- Supabase Storage for files and reports.
- Supabase Realtime for selected user-facing updates.
- Supabase Edge Functions for selected cloud-edge or portal endpoints where it is a good fit.
- Vercel as the future web portal host.

## Edge Boundary

Edge agents must not connect directly to Postgres. Edge agents must not use Supabase service-role keys. Edge agents must call cloud API endpoints only.

This keeps the gateway contract stable if FastAPI endpoints later move to Supabase Edge Functions or another API adapter. It also keeps database credentials and privileged Supabase keys out of gateway deployments.

## Data Boundary

Cloud:

- Organizations, sites, edge nodes, heartbeats, jobs, and future portal data live in Supabase Postgres.
- Server-side API code uses privileged database access.
- Browser-facing access will use Supabase Auth and RLS policies when the web portal exists.

Edge:

- SQLite remains the runtime and offline database.
- BACnet device discovery and future BACnet operations stay local.
- The edge uploads summaries and results through API calls.

## Runtime Flow

1. Edge agent sends heartbeat to the cloud API.
2. Cloud API records latest gateway status and heartbeat history.
3. Provisioned edge agent opens the outbound gateway UI tunnel when enabled.
4. Edge agent polls for a queued job.
5. Cloud API marks the job claimed and returns the payload.
6. Edge agent executes the job locally.
7. Edge agent stores local job history in SQLite.
8. Edge agent posts the result back to the cloud API.
9. Cloud API stores result JSON in the cloud database.

## Non-Goals For Current MVPs

- No direct edge access to Supabase.
- No live Supabase credentials in the repository.
- Browser login/signup is handled through Supabase Auth and FastAPI-served MVP pages.
- No web portal yet.
- No trend upload pipeline yet.
- No remote BACnet writes yet.
