# IOT Cloud Commissioning

IOT Cloud Commissioning is the foundation for an enterprise commissioning platform where edge gateways connect outbound to a cloud API. The edge keeps local runtime state in SQLite. The cloud stores enterprise gateway and heartbeat records in PostgreSQL.

## Architecture

- `cloud-api/`: FastAPI service with SQLAlchemy models for organizations, sites, edge nodes, and heartbeat history.
- `edge-agent/`: Python package `iot-cx-agent` for Ubuntu gateways.
- `shared/`: placeholder for future shared contracts.
- `deploy/`: systemd unit for the edge agent.
- `scripts/`: installation helpers.
- `docs/`: architecture notes.

MVP-001 supports outbound edge heartbeats only. BACnet discovery, point trending, remote writes, user login, and a full web UI are intentionally outside this first scope.

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
```

Send one heartbeat:

```bash
iot-cx-agent --config edge-agent/agent.local.yaml --once
```

Run continuously:

```bash
iot-cx-agent --config edge-agent/agent.local.yaml
```

## API Endpoints

- `GET /health`
- `POST /api/edge/heartbeat`
- `GET /api/edge/gateways`

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

