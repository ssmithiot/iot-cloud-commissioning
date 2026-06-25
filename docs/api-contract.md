# Cloud-Edge API Contract

This contract documents the current FastAPI cloud-edge API. FastAPI is the active adapter today, but each endpoint should remain compatible with a future implementation in Supabase Edge Functions or another server-side API layer.

All timestamps are UTC ISO 8601 strings. JSON bodies use `application/json`.

## Authentication

Gateway-facing endpoints require a gateway API token:

```text
Authorization: Bearer iotcc_gw_<token_prefix>_<secret>
```

Operator/admin endpoints accept either the server-side admin API token or a verified Supabase Auth user JWT with an active app role:

```text
Authorization: Bearer <IOT_ADMIN_API_TOKEN>
```

```text
Authorization: Bearer <supabase_user_access_token>
```

Supabase Auth owns email/password signup and email confirmation. FastAPI verifies the Supabase JWT with the server-side `SUPABASE_JWT_SECRET`, then checks local `operator_users` role and status. New confirmed users register as `pending` until an admin assigns an active role.

`IOT_ADMIN_API_TOKEN`, `SUPABASE_JWT_SECRET`, `GATEWAY_AUTH_PEPPER`, database URLs, Supabase keys, Postgres credentials, and service-role keys are cloud/server-side only. They must not be installed on edge gateways.

## POST /api/auth/register

Purpose: create or refresh a pending local operator profile for a confirmed Supabase Auth user.

Authentication: requires `Authorization: Bearer <supabase_user_access_token>`. The automation admin token is rejected for this endpoint.

Request: no body.

Response:

```json
{
  "email": "operator@example.com",
  "display_name": null,
  "role": "pending",
  "status": "pending",
  "supabase_user_id": "supabase-user-uuid",
  "last_login_at": null,
  "created_at": "2026-06-25T00:00:00Z",
  "updated_at": "2026-06-25T00:00:00Z"
}
```

Success behavior: creates a pending user row if missing. Admin approval is still required before the user can access operator routes.

## GET /api/auth/me

Purpose: return the current authenticated operator context.

Authentication: requires `Authorization: Bearer <IOT_ADMIN_API_TOKEN>` or `Authorization: Bearer <supabase_user_access_token>`.

Response:

```json
{
  "email": "operator@example.com",
  "role": "operator",
  "status": "active",
  "auth_type": "supabase_user"
}
```

The automation token returns `role: admin`, `status: active`, and `auth_type: admin_token`.

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

Authentication: requires `Authorization: Bearer iotcc_gw_<token_prefix>_<secret>`.

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

Failure behavior: invalid payloads return validation errors. Missing, invalid, expired, or revoked gateway tokens return HTTP 401. A valid gateway token for a different gateway returns HTTP 403. Database failures return server errors.

Future compatibility notes: keep the payload stable for Edge Functions. Server-side code should write to Postgres; gateways should never write directly to Supabase tables.

## GET /api/edge/{gateway_id}/jobs/next

Purpose: allow an edge gateway to poll outbound for the next queued job.

Authentication: requires `Authorization: Bearer iotcc_gw_<token_prefix>_<secret>`.

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

Failure behavior: missing, invalid, expired, or revoked gateway tokens return HTTP 401. A valid gateway token for a different gateway returns HTTP 403. Database failures return server errors.

Future compatibility notes: this endpoint needs an atomic claim operation when scaled. Supabase RPC or an Edge Function backed by a transaction is a good future fit.

## POST /api/edge/jobs/{job_id}/result

Purpose: receive a job completion or failure result from the edge gateway.

Authentication: requires `Authorization: Bearer iotcc_gw_<token_prefix>_<secret>`.

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

Failure behavior: missing, invalid, expired, or revoked gateway tokens return HTTP 401. A valid gateway token for a gateway that does not own the job returns HTTP 403. Unknown jobs return HTTP 404. Invalid status values return validation errors.

Future compatibility notes: keep status values stable: `queued`, `claimed`, `completed`, `failed`.

## POST /api/edge/jobs

Purpose: create a job for a gateway. This is an operator/admin workflow endpoint.

Authentication: requires `Authorization: Bearer <IOT_ADMIN_API_TOKEN>` or an active Supabase user with `admin` or `operator` role.

Request:

```json
{
  "gateway_id": "GW001",
  "job_type": "bacnet_discover",
  "request": {
    "bacnet_port": 47814,
    "timeout_sec": 10
  }
}
```

Response: full job record with status `queued`.

Success behavior: creates a queued job for the target gateway.

Failure behavior: missing or invalid operator credentials return HTTP 401. A read-only viewer token returns HTTP 403. Invalid payloads return validation errors.

Future compatibility notes: this endpoint may become a portal API or an Edge Function. It should remain server-side and should not expose privileged database credentials to browsers or gateways.

## GET /api/edge/jobs

Purpose: list recent jobs for debugging and verification.

Authentication: requires `Authorization: Bearer <IOT_ADMIN_API_TOKEN>` or an active Supabase user with `admin`, `operator`, or `viewer` role.

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

Failure behavior: missing or invalid admin credentials return HTTP 401. Database failures return server errors. Future portal access should require user authentication and RLS-compatible filtering.

Future compatibility notes: this debug endpoint may later split into portal-facing job history and internal gateway APIs.

## GET /api/edge/gateways

Purpose: list known gateways and latest status.

Authentication: requires `Authorization: Bearer <IOT_ADMIN_API_TOKEN>` or an active Supabase user with `admin`, `operator`, or `viewer` role.

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

Failure behavior: missing or invalid admin credentials return HTTP 401. Database failures return server errors. Future portal access should filter by organization, site, and user permissions.

Future compatibility notes: when Supabase Auth and RLS are active, this can be served through a portal API using user-scoped access. Gateway-facing endpoints should remain gateway-authenticated.

## POST /api/admin/gateways/provision

Purpose: create or update the cloud-side site and gateway identity, then issue a gateway API token for office provisioning.

Authentication: requires `Authorization: Bearer <IOT_ADMIN_API_TOKEN>` or an active Supabase user with `admin` role.

Request:

```json
{
  "gateway_id": "GW007",
  "site_id": "customer-site",
  "hostname": "GW007",
  "lan_ip": "192.168.1.50",
  "bacnet_port": 47814,
  "agent_version": "0.1.0",
  "ui_version": "0.1.0"
}
```

`bacnet_port` defaults to `47814`. `agent_version` and `ui_version` default to `0.1.0`.

Response:

```json
{
  "gateway_id": "GW007",
  "site_id": "customer-site",
  "hostname": "GW007",
  "lan_ip": "192.168.1.50",
  "bacnet_port": 47814,
  "agent_version": "0.1.0",
  "ui_version": "0.1.0",
  "gateway_api_token": "iotcc_gw_<token_prefix>_<secret>",
  "token_prefix": "<token_prefix>"
}
```

Success behavior: creates the site if missing, creates or updates the gateway identity, stores only the token prefix and server-side HMAC token hash, and returns the raw gateway token once in the response.

Failure behavior: missing or invalid admin credentials return HTTP 401. Invalid payloads return validation errors. Database failures return server errors.

Security notes: the response does not expose `GATEWAY_AUTH_PEPPER`, database credentials, Supabase keys, Postgres credentials, or service-role keys. Save the returned gateway token directly into the gateway provisioning flow; the raw token cannot be recovered later from the database.

## GET /api/admin/users

Purpose: list app operator users for the admin user-management page.

Authentication: requires `Authorization: Bearer <IOT_ADMIN_API_TOKEN>` or an active Supabase user with `admin` role.

Response:

```json
[
  {
    "email": "operator@example.com",
    "display_name": "Office Operator",
    "role": "operator",
    "status": "active",
    "supabase_user_id": "supabase-user-uuid",
    "last_login_at": "2026-06-25T00:00:00Z",
    "created_at": "2026-06-25T00:00:00Z",
    "updated_at": "2026-06-25T00:00:00Z"
  }
]
```

## PUT /api/admin/users/{email}

Purpose: create or update a local app role for a Supabase Auth user.

Authentication: requires `Authorization: Bearer <IOT_ADMIN_API_TOKEN>` or an active Supabase user with `admin` role.

Request:

```json
{
  "email": "operator@example.com",
  "role": "operator",
  "status": "active",
  "display_name": "Office Operator",
  "supabase_user_id": "supabase-user-uuid"
}
```

Allowed roles: `admin`, `operator`, `viewer`, `pending`.

Allowed statuses: `active`, `pending`, `disabled`.

Success behavior: creates or updates the local role record. It does not create the Supabase Auth account or send confirmation email; Supabase owns signup and confirmation.
