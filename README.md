# IOT Cloud Commissioning

IOT Cloud Commissioning is the foundation for an enterprise commissioning platform where edge gateways connect outbound to a cloud API. The edge keeps local runtime state in SQLite. The cloud stores enterprise gateway and heartbeat records in PostgreSQL.

## Architecture

- `cloud-api/`: FastAPI service with SQLAlchemy models for organizations, sites, edge nodes, and heartbeat history.
- `edge-agent/`: Python package `iot-cx-agent` for Ubuntu gateways.
- `shared/`: placeholder for future shared contracts.
- `deploy/`: systemd unit for the edge agent.
- `scripts/`: installation helpers.
- `docs/`: architecture notes.

MVP-001 supports outbound edge heartbeats. MVP-002 adds a cloud-to-edge job framework where the cloud queues work, the edge agent polls outbound for one job at a time, executes a handler, stores local job history in SQLite, and posts the result back to the cloud.

MVP-003 adds a minimal `bacnet_discover` job. Discovery runs locally on the edge gateway through a configured `bacwi` command, and the structured result is posted back through the existing cloud job result endpoint. BACnet UDP stays local to the edge gateway and is not exposed to the cloud. BACnet writes, point trending, user login, and a full web UI are intentionally outside this scope.

MVP-005 connects the FastAPI cloud adapter to Supabase Postgres through `DATABASE_URL`. Supabase Postgres is the target cloud database, while FastAPI remains the current thin API adapter. Supabase Auth, Row Level Security, Storage, Realtime, and selected Edge Functions are planned platform services for later MVPs. No live Supabase credentials, anon keys, service-role keys, or real database URLs are committed.

MVP-006 adds gateway API credentials for edge-facing FastAPI endpoints. Edge gateways still receive only a gateway API token and continue to call HTTPS API endpoints; they never receive Supabase, Postgres, or privileged database credentials.

MVP-012 protects operator/admin FastAPI endpoints with `IOT_ADMIN_API_TOKEN` and adds a cloud-side gateway provisioning endpoint so office provisioning can create/update the gateway identity and issue a gateway token without using database shell snippets.

MVP-013 adds browser login/signup plus the backend foundation for friendly operator access. Supabase Auth owns email/password signup and email confirmation, while FastAPI verifies Supabase user JWTs and stores app roles in `operator_users`. The admin automation token remains available for scripts and emergency access. The app pages are served by FastAPI at `/login`, `/signup`, `/app`, and `/admin/users`.

## Local Setup

Use Python 3.10 or newer. For local development, create one virtual environment per service or reuse a single development environment.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r cloud-api/requirements.txt
pip install -r edge-agent/requirements.txt
pip install -e edge-agent
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r cloud-api\requirements.txt
pip install -r edge-agent\requirements.txt
pip install -e edge-agent
```

## Docker Compose Startup

```bash
docker compose up --build
```

The API will be available at `http://localhost:8000`.

## Supabase Setup

The `supabase/` folder contains reviewable SQL migrations for the future Supabase Postgres schema:

- `supabase/migrations/0001_core_schema.sql`
- `supabase/migrations/0002_edge_jobs.sql`
- `supabase/migrations/0003_security_foundation.sql`
- `supabase/migrations/0004_future_features.sql`

These files are applied with the Supabase CLI when developing against a Supabase project. They prepare the schema direction for current cloud records, future portal users and permissions, audit events, report files, trend upload placeholders, point samples, and BACnet device summaries.

FastAPI remains the active cloud API adapter. Edge gateways continue to call `cloud_url` only and must not connect directly to Supabase Postgres. Selected endpoints may later move to Supabase Edge Functions without changing the edge agent's outbound API contract.

For Supabase Auth-backed operator login, configure email confirmation in Supabase Auth and set these FastAPI environment variables on the cloud service:

```text
SUPABASE_JWT_SECRET=<supabase-project-jwt-secret>
SUPABASE_JWT_AUDIENCE=authenticated
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<supabase-anon-public-key>
SUPABASE_JWKS_URL=
```

`SUPABASE_URL` and `SUPABASE_ANON_KEY` are public browser values. `SUPABASE_JWT_SECRET` is server-side only and is used for legacy `HS256` projects. For newer Supabase JWT signing keys, FastAPI verifies `RS256` and `ES256` user JWTs from `${SUPABASE_URL}/auth/v1/.well-known/jwks.json`; set `SUPABASE_JWKS_URL` only when an explicit override is needed. Do not configure any of these values on edge gateways.

In the Supabase dashboard Auth URL Configuration, set Site URL to:

```text
https://iot-cloud-api-dev.onrender.com
```

Add the production app URLs to the redirect allow list, including:

```text
https://iot-cloud-api-dev.onrender.com/login
```

The signup UI sends `emailRedirectTo` as `${window.location.origin}/login`. Confirmation emails should not point to localhost in production.

For MVP-005 development:

1. Create a Supabase project.
2. Copy the Session pooler connection string on port `5432`.
3. Copy `.env.example` to `.env` and set `DATABASE_URL` to the Session pooler URL.
4. Link the project with `npx supabase link --project-ref <project-ref>`.
5. Preview migrations with `npx supabase db push --dry-run`.
6. Apply migrations with `npx supabase db push`.
7. Run tests with `pytest` from `cloud-api/` and `edge-agent/`.
8. Start FastAPI with `uvicorn app.main:app --reload` from `cloud-api/`.

## Gateway Authentication

Gateway API tokens use this format:

```text
iotcc_gw_<token_prefix>_<secret>
```

Edge requests authenticate with:

```text
Authorization: Bearer iotcc_gw_<token_prefix>_<secret>
```

FastAPI stores only `token_prefix` and an HMAC-SHA256 `token_hash` in `public.gateway_credentials`. The HMAC key is the server-side `GATEWAY_AUTH_PEPPER` environment variable. Do not configure this pepper on edge gateways.

Create a gateway credential after the gateway exists in `public.edge_nodes`:

```bash
cd cloud-api
python scripts/create_gateway_credential.py GW001 --label "GW001 edge agent"
```

The script prints the full token once. Store it securely, then configure the edge agent with `GATEWAY_API_TOKEN` in the service environment. A local YAML `gateway_api_token` value is supported for development, but environment configuration is preferred.

For normal office provisioning, use the admin provisioning endpoint instead:

```bash
curl -X POST http://localhost:8000/api/admin/gateways/provision \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gateway_id": "GW001",
    "site_id": "demo-site",
    "hostname": "GW001",
    "lan_ip": "192.168.1.10",
    "bacnet_port": 47814
  }'
```

This endpoint creates or updates the site and gateway identity, stores only the gateway token prefix and server-side HMAC hash, and returns the raw gateway token once.

Authenticated endpoints:

- `POST /api/edge/heartbeat`
- `GET /api/edge/{gateway_id}/jobs/next`
- `POST /api/edge/jobs/{job_id}/result`
- `WEBSOCKET /api/edge/tunnels/{gateway_id}`

Credential revocation is handled by setting `public.gateway_credentials.revoked_at`. Expiration is handled by setting `expires_at`; expired or revoked credentials receive HTTP 401.

## Admin Operator Authentication

Cloud/operator routes require:

```text
Authorization: Bearer <IOT_ADMIN_API_TOKEN>
```

Protected operator endpoints:

- `GET /api/edge/gateways`
- `GET /api/ui/sites`
- `GET /api/ui/sites/{site_id}`
- `GET /api/ui/gateways/{gateway_id}/direct-connect`
- `GET /api/ui/gateways/{gateway_id}/tunnel-status`
- `PATCH /api/ui/sites/{site_id}`
- `POST /api/edge/jobs`
- `GET /api/edge/jobs`
- `POST /api/admin/gateways/provision`

`IOT_ADMIN_API_TOKEN` is a server-side cloud API secret. Do not install it on edge gateways.

## Gateway UI Tunnel

Gateway configuration uses an outbound tunnel, not Cradlepoint port forwarding. A provisioned edge agent opens a gateway-authenticated WebSocket to:

```text
/api/edge/tunnels/{gateway_id}
```

The operator dashboard Configure link opens a browser shell at:

```text
/gateways/{gateway_id}/tunnel/
```

The shell uses the logged-in Supabase browser session to call authenticated tunnel status APIs. Address-bar navigation does not attach bearer tokens to raw proxy requests, so normal users should see friendly tunnel status UI instead of raw auth JSON.

The protected proxy relay path is:

```text
/gateways/{gateway_id}/tunnel/proxy/{path}
```

When a real outbound session exists, the edge agent proxies that traffic to its local gateway UI, configured with:

```yaml
tunnel_enabled: true
local_ui_url: http://127.0.0.1:5000
```

Current state: Cloud Tunnel is future scope for the next remote-console slice unless an actual gateway tunnel client/session is present. When no gateway tunnel session is connected, the protected proxy route must keep returning a friendly disconnected response such as `{"detail":"Gateway tunnel is not connected"}`. Do not fake tunnel connectivity.

## Direct Connect

Direct Connect is separate from the cloud tunnel. It is a new-tab browser link to a configured Cradlepoint/cellular host and external port, usually:

```text
http://<direct_connect_host>:5002
```

It is not a cloud proxy and does not store gateway UI passwords. Admin users can edit site information; operators and viewers are read-only by default. Direct Connect metadata is stored on `sites`:

- `name`
- `address` legacy/free-form compatibility field
- `address_street`
- `address_city`
- `address_state`
- `address_postal_code`
- `cradlepoint_ip`
- `direct_connect_host`
- `direct_connect_port` default `5002`
- `gateway_ui_port` informational default `5000`
- `store_hours_monday_friday`
- `store_hours_saturday`
- `store_hours_sunday`
- `network_status_notes`

Live Supabase changes should be applied with the SQL Editor scripts in `supabase/migrations/0008_site_direct_connect.sql` and `supabase/migrations/0009_site_split_address.sql`.

Live smoke status: Direct Connect / Site Info passed. The site info form saves split address fields, gateway list/detail display site information correctly, the Direct Connect button appears after host/port configuration, and the button opens the forwarded gateway UI through the configured host/port. Direct Connect remains the working access path for now and stays separate from Cloud Tunnel.

Recommended tag for this slice:

```text
mvp-014b-direct-connect-site-management
```

Next planning slice:

```text
MVP-014C: real bacnet_load_points edge-agent job plus UI point-tree population
```

Related docs:

- `docs/architecture.md`
- `docs/api-contract.md`
- `docs/security-model.md`
- `docs/supabase-plan.md`

## Run Cloud API Locally

For quick local development without PostgreSQL, the API defaults to SQLite:

```bash
cd cloud-api
uvicorn app.main:app --reload
```

For Supabase Postgres, set `DATABASE_URL` from `.env.example`, push the Supabase migrations, then run:

```bash
cd cloud-api
uvicorn app.main:app --reload
```

## Run Edge Agent Locally

Copy the example config and use a local SQLite path:

```bash
cp edge-agent/config.example.yaml edge-agent/agent.local.yaml
```

Edit `edge-agent/agent.local.yaml` and uncomment or add:

```yaml
sqlite_path: ./edge.db
bacnet:
  default_port: 47814
  bacwi_path: bacwi
  timeout_sec: 10
```

Send one heartbeat:

```bash
iot-cx-agent --config edge-agent/agent.local.yaml --once
```

Run continuously:

```bash
iot-cx-agent --config edge-agent/agent.local.yaml
```

The agent sends a heartbeat first, then polls for one queued job each loop.

## API Endpoints

- `GET /health`
- `GET /health/db`
- `POST /api/edge/heartbeat`
- `GET /api/edge/gateways` (admin token)
- `POST /api/edge/jobs` (admin token)
- `GET /api/edge/{gateway_id}/jobs/next`
- `POST /api/edge/jobs/{job_id}/result`
- `GET /api/edge/jobs` (admin token)
- `POST /api/admin/gateways/provision` (admin token)

Example heartbeat:

```bash
curl -X POST http://localhost:8000/api/edge/heartbeat \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
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
  }'
```

List gateways:

```bash
curl http://localhost:8000/api/edge/gateways \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN"
```

Create an echo job:

```bash
curl -X POST http://localhost:8000/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gateway_id": "GW001",
    "job_type": "echo",
    "request": {
      "message": "hello edge"
    }
  }'
```

Create a BACnet discovery job:

```bash
curl -X POST http://localhost:8000/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gateway_id": "GW001",
    "job_type": "bacnet_discover",
    "request": {
      "port": 47814,
      "timeout_sec": 10
    }
  }'
```

For real BACnet discovery, install `bacwi` on the gateway or set `bacnet.bacwi_path` to the full command path in `agent.yaml`.

List recent jobs:

```bash
curl http://localhost:8000/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN"
```

## Verify Echo Job Flow

Start the cloud API:

```bash
docker compose up --build
```

In another terminal, run the edge agent continuously:

```bash
iot-cx-agent --config edge-agent/agent.local.yaml
```

Create the echo job:

```bash
curl -X POST http://localhost:8000/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":"GW001","job_type":"echo","request":{"message":"hello edge"}}'
```

After the next agent polling loop, confirm the cloud has the completed result:

```bash
curl http://localhost:8000/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN"
```

The completed `echo` job includes `result_json` with the original request, `gateway_id`, and `agent_version`.

## Verify BACnet Discovery Job Flow

Start the cloud API and run the edge agent as above. Then create a discovery job:

```bash
curl -X POST http://localhost:8000/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":"GW001","job_type":"bacnet_discover","request":{"port":47814,"timeout_sec":10}}'
```

After the next agent polling loop, list jobs:

```bash
curl http://localhost:8000/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN"
```

If `bacwi` is installed and reachable from the edge agent, the completed job includes discovered devices in `result_json`. If `bacwi` is missing, exits with an error, or times out, the job is marked `failed` with a clear `error_message`.

## Run Tests

```bash
cd cloud-api
pytest
```

```bash
cd edge-agent
pytest
```

## Edge Deployment

The agent uses:

- Config path: `/etc/iot-cx-agent/agent.yaml`
- SQLite path: `/var/lib/iot-cx-agent/edge.db`
- Systemd service: `iot-cx-agent.service`

Install helper:

```bash
sudo scripts/install-edge-agent.sh
```

The installer creates required directories, avoids overwriting existing config, and installs the systemd unit when possible. It does not include secrets.
