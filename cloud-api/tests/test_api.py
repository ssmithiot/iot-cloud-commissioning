from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_heartbeat_creates_gateway_and_history() -> None:
    payload = {
        "gateway_id": "GW001",
        "site_id": "demo-site",
        "hostname": "edge-demo",
        "lan_ip": "192.168.1.10",
        "bacnet_port": 47814,
        "agent_version": "0.1.0",
        "ui_version": "0.1.0",
        "sqlite_db_ok": True,
        "queued_upload_count": 0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    heartbeat = client.post("/api/edge/heartbeat", json=payload)
    gateways = client.get("/api/edge/gateways")

    assert heartbeat.status_code == 200
    assert heartbeat.json()["gateway_id"] == "GW001"
    assert heartbeat.json()["status"] == "online"
    assert gateways.status_code == 200
    assert gateways.json()[0]["gateway_id"] == "GW001"
    assert gateways.json()[0]["site_id"] == "demo-site"

