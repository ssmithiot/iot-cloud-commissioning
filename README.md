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

MVP-004 prepares the repository for Supabase without connecting to a live project. Supabase Postgres is the target cloud database, while FastAPI remains the current thin API adapter. Supabase Auth, Row Level Security, Storage, Realtime, and selected Edge Functions are planned platform services for later MVPs. No live Supabase credentials, anon keys, service-role keys, or real database URLs are committed.

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

## Supabase Readiness

The `supabase/` folder contains reviewable SQL migrations for the future Supabase Postgres schema:

- `supabase/migrations/0001_core_schema.sql`
- `supabase/migrations/0002_edge_jobs.sql`
- `supabase/migrations/0003_security_foundation.sql`
- `supabase/migrations/0004_future_features.sql`

These files are not applied automatically and do not require the Supabase CLI to run tests. They prepare the schema direction for current cloud records, future portal users and permissions, audit events, report files, trend upload placeholders, point samples, and BACnet device summaries.

FastAPI remains the active cloud API adapter. Edge gateways continue to call `cloud_url` only and must not connect directly to Supabase Postgres. Selected endpoints may later move to Supabase Edge Functions without changing the edge agent's outbound API contract.

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

For PostgreSQL, set `CLOUD_DATABASE_URL` from `.env.example`, then run:

```bash
cd cloud-api
alembic upgrade head
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
- `POST /api/edge/heartbeat`
- `GET /api/edge/gateways`
- `POST /api/edge/jobs`
- `GET /api/edge/{gateway_id}/jobs/next`
- `POST /api/edge/jobs/{job_id}/result`
- `GET /api/edge/jobs`

Example heartbeat:

```bash
curl -X POST http://localhost:8000/api/edge/heartbeat \
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
curl http://localhost:8000/api/edge/gateways
```

Create an echo job:

```bash
curl -X POST http://localhost:8000/api/edge/jobs \
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
curl http://localhost:8000/api/edge/jobs
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
  -H "Content-Type: application/json" \
  -d '{"gateway_id":"GW001","job_type":"echo","request":{"message":"hello edge"}}'
```

After the next agent polling loop, confirm the cloud has the completed result:

```bash
curl http://localhost:8000/api/edge/jobs
```

The completed `echo` job includes `result_json` with the original request, `gateway_id`, and `agent_version`.

## Verify BACnet Discovery Job Flow

Start the cloud API and run the edge agent as above. Then create a discovery job:

```bash
curl -X POST http://localhost:8000/api/edge/jobs \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":"GW001","job_type":"bacnet_discover","request":{"port":47814,"timeout_sec":10}}'
```

After the next agent polling loop, list jobs:

```bash
curl http://localhost:8000/api/edge/jobs
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
