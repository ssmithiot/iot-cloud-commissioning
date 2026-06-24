# MVP-009 Hosted API GW006 Smoke

MVP-009 records the first hosted cloud API smoke test with GW006 running the edge agent against Render.

## Hosted Cloud API

- Hosted FastAPI URL: `https://iot-cloud-api-dev.onrender.com`
- `GET /health` returned `{"status":"ok"}`.
- `GET /health/db` returned `{"status":"ok"}`.
- FastAPI remains the cloud API adapter. Edge gateways call HTTPS API endpoints only.

## GW006 Runtime State

- Gateway ID: `GW006`
- Site ID: `DEV-CLONE-MASTER`
- Gateway LAN IP: `192.168.1.200`
- GW006 edge agent is installed and running.
- GW006 heartbeat is accepted by the hosted API.
- Cloud commissioning BACnet runtime uses UDP `47814`.
- Existing legacy runtime on UDP `47808` must not be touched.
- Local Edge BACnet Discovery & Commissioning UI has priority over cloud BACnet jobs.
- Cloud BACnet jobs yield when `/tmp/iot-cloud-commissioning-bacnet-47814.lock` exists.

## Credential Boundary

GW006 must not contain:

- `DATABASE_URL`
- `GATEWAY_AUTH_PEPPER`
- Supabase or Postgres credentials
- Supabase service-role keys

GW006 contains only the gateway API token in:

```text
/etc/iot-cx-agent/edge-agent.env
```

That file must contain only the edge-facing token configuration:

```text
GATEWAY_API_TOKEN=<GW006 token>
```

Do not copy cloud database credentials, auth pepper values, Supabase API keys, or service-role keys to GW006.

## Runtime Check Result

GW006 claimed and completed a hosted `bacnet_runtime_check` job.

- Job ID: `job-cffd4d00b4384a018f499dcb6442bb5c`
- Job status: `completed`
- Result status: `ok`
- BACnet port: `47814`
- Lock held: `false`
- Lock path: `/tmp/iot-cloud-commissioning-bacnet-47814.lock`
- `bacwi` exists: `true`
- `bacwi` executable: `true`
- `bacwi` path: `/home/swadmin/bacnet-stack/bin/bacwi`
- `bacrp` exists: `true`
- `bacrp` executable: `true`
- `bacrp` path: `/home/swadmin/bacnet-stack/bin/bacrp`
- Claimed at: `2026-06-24T21:57:32.765651Z`
- Completed at: `2026-06-24T21:57:34.347626Z`
- Errors: none

## Clone Safety

GW006 currently has an active gateway token. The clone master must not be cloned with that token unchanged.

Before cloning or imaging this gateway, remove or replace:

- `/etc/iot-cx-agent/edge-agent.env`
- `gateway_id` in `/etc/iot-cx-agent/agent.yaml`
- hostname if needed
- any site-specific metadata

Each cloned gateway must receive a unique gateway identity and a unique edge-facing API token.
