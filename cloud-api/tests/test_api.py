from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


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


def test_create_claim_and_complete_job() -> None:
    create_response = client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "echo", "request": {"message": "hello edge"}},
    )

    assert create_response.status_code == 200
    created = create_response.json()
    assert created["status"] == "queued"
    assert created["request_json"] == {"message": "hello edge"}

    next_response = client.get("/api/edge/GW001/jobs/next")

    assert next_response.status_code == 200
    claimed = next_response.json()
    assert claimed["job_id"] == created["job_id"]
    assert claimed["job_type"] == "echo"
    assert claimed["request"] == {"message": "hello edge"}

    empty_response = client.get("/api/edge/GW001/jobs/next")
    assert empty_response.status_code == 200
    assert empty_response.json() is None

    result_response = client.post(
        f"/api/edge/jobs/{created['job_id']}/result",
        json={"status": "completed", "result": {"echo": True}, "error_message": None},
    )

    assert result_response.status_code == 200
    completed = result_response.json()
    assert completed["status"] == "completed"
    assert completed["result_json"] == {"echo": True}
    assert completed["completed_at"] is not None


def test_job_result_can_mark_failed() -> None:
    create_response = client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "unknown", "request": {}},
    )
    job_id = create_response.json()["job_id"]

    result_response = client.post(
        f"/api/edge/jobs/{job_id}/result",
        json={"status": "failed", "result": None, "error_message": "Unknown job_type: unknown"},
    )
    jobs_response = client.get("/api/edge/jobs")

    assert result_response.status_code == 200
    assert result_response.json()["status"] == "failed"
    assert result_response.json()["error_message"] == "Unknown job_type: unknown"
    assert jobs_response.status_code == 200
    assert jobs_response.json()[0]["job_id"] == job_id
