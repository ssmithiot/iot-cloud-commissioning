# Cloud-Edge API Contract

This contract documents the current FastAPI cloud-edge API. FastAPI is the active adapter today, but each endpoint should remain compatible with a future implementation in Supabase Edge Functions or another server-side API layer.

All timestamps are UTC ISO 8601 strings. JSON bodies use `application/json`.

## GET /health

Purpose: report API process health.

Request: no body.

Response:

```json
{
  "status": "ok"
}
```

Success behavior: returns HTTP 200 when the API process can serve requests.

Failure behavior: infrastructure or runtime failures may return non-200 responses.

Future compatibility notes: this endpoint can remain a simple unauthenticated health check. A separate authenticated readiness endpoint can be added later if database checks are needed.

## POST /api/edge/heartbeat

Purpose: receive current gateway status and create heartbeat history.

Request:

```json
{
  "gateway_id": "GW001",
  "site_id": "demo-site",
  "hostname": "edge-demo",
  "lan_ip": "192.168.1.10",
  "bacnet_port": 47814,
  "agent_version": "0.1.0",
  "ui_version": "0.1.0",
  "sqlite_db_ok": true,
  "queued_upload_count": 0,
  "timestamp_utc": "2026-06-19T00:00:00Z"
}
```

Response:

```json
{
  "gateway_id": "GW001",
  "status": "online",
  "latest_heartbeat_at": "2026-06-19T00:00:00Z"
}
```

Success behavior: creates the site if missing, creates or updates the edge node, records heartbeat history, and stores latest status.

Failure behavior: invalid payloads return validation errors. Database failures return server errors. Future gateway authentication failures should return HTTP 401 or 403.

Future compatibility notes: keep the payload stable for Edge Functions. Server-side code should write to Postgres; gateways should never write directly to Supabase tables.

## GET /api/edge/{gateway_id}/jobs/next

Purpose: allow an edge gateway to poll outbound for the next queued job.

Request: path parameter `gateway_id`.

Response when a job exists:

```json
{
  "job_id": "job-abc123",
  "gateway_id": "GW001",
  "job_type": "echo",
  "request": {
    "message": "hello edge"
  }
}
```

Response when no job exists:

```json
null
```

Success behavior: returns the oldest queued job for the gateway and marks it `claimed`.

Failure behavior: database failures return server errors. Future gateway authentication should prevent one gateway from claiming another gateway's jobs.

Future compatibility notes: this endpoint needs an atomic claim operation when scaled. Supabase RPC or an Edge Function backed by a transaction is a good future fit.

## POST /api/edge/jobs/{job_id}/result

Purpose: receive a job completion or failure result from the edge gateway.

Request:

```json
{
  "status": "completed",
  "result": {
    "echo": true
  },
  "error_message": null
}
```

Failed request example:

```json
{
  "status": "failed",
  "result": null,
  "error_message": "BACnet discovery command not found: bacwi"
}
```

Response:

```json
{
  "job_id": "job-abc123",
  "gateway_id": "GW001",
  "job_type": "echo",
  "status": "completed",
  "request_json": {
    "message": "hello edge"
  },
  "result_json": {
    "echo": true
  },
  "error_message": null,
  "created_at": "2026-06-19T00:00:00Z",
  "claimed_at": "2026-06-19T00:00:10Z",
  "completed_at": "2026-06-19T00:00:11Z"
}
```

Success behavior: marks the job `completed` or `failed`, stores result JSON, error message, and completion timestamp.

Failure behavior: unknown jobs return HTTP 404. Invalid status values return validation errors. Future gateway authentication should verify the reporting gateway owns the job.

Future compatibility notes: keep status values stable: `queued`, `claimed`, `completed`, `failed`.

## POST /api/edge/jobs

Purpose: create a job for a gateway. This is a debugging and server-side workflow endpoint today.

Request:

```json
{
  "gateway_id": "GW001",
  "job_type": "bacnet_discover",
  "request": {
    "port": 47814,
    "timeout_sec": 10
  }
}
```

Response: full job record with status `queued`.

Success behavior: creates a queued job for the target gateway.

Failure behavior: invalid payloads return validation errors. Future authorization should limit who can create jobs for an organization or site.

Future compatibility notes: this endpoint may become a portal API, an admin API, or an Edge Function. It should remain server-side and should not expose privileged database credentials to browsers or gateways.

## GET /api/edge/jobs

Purpose: list recent jobs for debugging and verification.

Request: optional `limit` query parameter, default `50`, maximum `200`.

Response:

```json
[
  {
    "job_id": "job-abc123",
    "gateway_id": "GW001",
    "job_type": "echo",
    "status": "completed",
    "request_json": {
      "message": "hello edge"
    },
    "result_json": {
      "echo": true
    },
    "error_message": null,
    "created_at": "2026-06-19T00:00:00Z",
    "claimed_at": "2026-06-19T00:00:10Z",
    "completed_at": "2026-06-19T00:00:11Z"
  }
]
```

Success behavior: returns recent jobs ordered newest first.

Failure behavior: database failures return server errors. Future portal access should require user authentication and RLS-compatible filtering.

Future compatibility notes: this debug endpoint may later split into portal-facing job history and internal gateway APIs.

## GET /api/edge/gateways

Purpose: list known gateways and latest status.

Request: no body.

Response:

```json
[
  {
    "gateway_id": "GW001",
    "site_id": "demo-site",
    "hostname": "edge-demo",
    "lan_ip": "192.168.1.10",
    "bacnet_port": 47814,
    "agent_version": "0.1.0",
    "ui_version": "0.1.0",
    "sqlite_db_ok": true,
    "queued_upload_count": 0,
    "latest_status": "online",
    "latest_heartbeat_at": "2026-06-19T00:00:00Z",
    "updated_at": "2026-06-19T00:00:01Z"
  }
]
```

Success behavior: returns all known gateways and latest status.

Failure behavior: database failures return server errors. Future portal access should filter by organization, site, and user permissions.

Future compatibility notes: when Supabase Auth and RLS are active, this can be served through a portal API using user-scoped access. Gateway-facing endpoints should remain gateway-authenticated.
