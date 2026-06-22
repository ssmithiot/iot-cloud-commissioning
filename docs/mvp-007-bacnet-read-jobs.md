# MVP-007 Authenticated BACnet Read Jobs

MVP-007 adds a narrow authenticated BACnet read job path from cloud API to edge gateway and back. The only supported BACnet read in this milestone is `present-value`.

## Job Type

```text
bacnet_read
```

## Request Payload

```json
{
  "device_instance": 1,
  "object_type": "analog-value",
  "object_instance": 1,
  "property": "present-value"
}
```

Validation rules:

- `device_instance` must be an integer.
- `object_type` must be one of `analog-input`, `analog-output`, `analog-value`, `binary-input`, `binary-output`, `binary-value`, `multi-state-input`, `multi-state-output`, or `multi-state-value`.
- `object_instance` must be an integer.
- `property` is optional, defaults to `present-value`, and may only be `present-value`.

## Successful Result Example

```json
{
  "job_type": "bacnet_read",
  "device_instance": 1,
  "object_type": "analog-value",
  "object_instance": 1,
  "property": "present-value",
  "property_id": 85,
  "value": 72.4,
  "raw_value": "72.4",
  "status": "ok"
}
```

## Failure Result Example

```json
{
  "job_type": "bacnet_read",
  "device_instance": 1,
  "object_type": "analog-value",
  "object_instance": 1,
  "property": "present-value",
  "property_id": 85,
  "status": "error",
  "error": "human readable error message",
  "raw_output": "raw BACnet CLI output when useful"
}
```

A failed edge execution is posted to the cloud as job status `failed` with the structured error result stored in `result_json` when available.

## Auth Expectations

Gateway-facing job endpoints remain gateway-token authenticated:

- Missing bearer token returns HTTP 401.
- A valid token for the matching gateway can claim and complete that gateway's job.
- A valid token for a different gateway returns HTTP 403.

Gateway credentials stay server-side in `public.gateway_credentials`. Gateway identity remains text in `public.edge_nodes.gateway_id` and related job records.

## Edge Execution Boundary

The edge agent polls FastAPI through `cloud_url`, executes BACnet locally, and posts the result back to FastAPI with its gateway API token.

BACnet read execution uses the configured BACnet CLI command:

```yaml
bacnet:
  default_port: 47814
  bacrp_path: bacrp
  timeout_sec: 10
```

The edge maps `present-value` to BACnet property ID `85` before invoking the CLI. The subprocess call is built as an argument list, captures stdout and stderr, enforces timeout, and handles missing command, timeout, startup failure, nonzero exit, and parse failure.

The edge agent must not connect directly to Supabase or Postgres and must not use Supabase service-role keys. BACnet traffic remains local to the edge gateway LAN.
