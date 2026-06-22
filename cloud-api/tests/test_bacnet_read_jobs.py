import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test-cloud-api-bacnet-read.db"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["GATEWAY_AUTH_PEPPER"] = "test-pepper"

from app.auth import hash_gateway_token
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EdgeJob, EdgeNode, GatewayCredential, Site
from scripts.create_gateway_credential import DEFAULT_SCOPES


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


client = TestClient(app)


def bacnet_read_request(include_property: bool = True) -> dict[str, object]:
    request = {
        "device_instance": 1,
        "object_type": "analog-value",
        "object_instance": 1,
    }
    if include_property:
        request["property"] = "present-value"
    return request


def create_gateway_token(gateway_id: str = "GW001", token_prefix: str | None = None) -> str:
    token_prefix = token_prefix or f"{gateway_id.lower()}01"
    raw_token = f"iotcc_gw_{token_prefix}_test-secret-value-for-{gateway_id}"
    with SessionLocal() as db:
        site = db.scalar(select(Site).where(Site.site_id == "demo-site"))
        if site is None:
            site = Site(site_id="demo-site", name="demo-site")
            db.add(site)
            db.flush()

        edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == gateway_id))
        if edge_node is None:
            db.add(
                EdgeNode(
                    gateway_id=gateway_id,
                    site_id="demo-site",
                    hostname=f"{gateway_id.lower()}-host",
                    bacnet_port=47814,
                    agent_version="0.1.0",
                    ui_version="0.1.0",
                    sqlite_db_ok=True,
                    queued_upload_count=0,
                    latest_status="online",
                )
            )

        db.add(
            GatewayCredential(
                gateway_id=gateway_id,
                token_prefix=token_prefix,
                token_hash=hash_gateway_token(raw_token),
                scopes=DEFAULT_SCOPES,
            )
        )
        db.commit()

    return raw_token


def auth_headers(raw_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_token}"}


def test_create_claim_and_complete_bacnet_read_job() -> None:
    raw_token = create_gateway_token("GW001")
    create_response = client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "bacnet_read", "request": bacnet_read_request(include_property=False)},
    )

    assert create_response.status_code == 200
    created = create_response.json()
    assert created["status"] == "queued"
    assert created["gateway_id"] == "GW001"
    assert created["request_json"] == {
        "device_instance": 1,
        "object_type": "analog-value",
        "object_instance": 1,
        "property": "present-value",
    }

    next_response = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))

    assert next_response.status_code == 200
    claimed = next_response.json()
    assert claimed["job_id"] == created["job_id"]
    assert claimed["job_type"] == "bacnet_read"
    assert claimed["request"]["property"] == "present-value"

    result_payload = {
        "job_type": "bacnet_read",
        "device_instance": 1,
        "object_type": "analog-value",
        "object_instance": 1,
        "property": "present-value",
        "property_id": 85,
        "value": 72.4,
        "raw_value": "72.4",
        "status": "ok",
    }
    result_response = client.post(
        f"/api/edge/jobs/{created['job_id']}/result",
        headers=auth_headers(raw_token),
        json={"status": "completed", "result": result_payload, "error_message": None},
    )

    assert result_response.status_code == 200
    completed = result_response.json()
    assert completed["status"] == "completed"
    assert completed["result_json"] == result_payload
    with SessionLocal() as db:
        stored_job = db.scalar(select(EdgeJob).where(EdgeJob.job_id == created["job_id"]))
        assert stored_job is not None
        assert stored_job.gateway_id == "GW001"
        assert isinstance(stored_job.gateway_id, str)


def test_invalid_bacnet_read_job_payload_returns_422() -> None:
    response = client.post(
        "/api/edge/jobs",
        json={
            "gateway_id": "GW001",
            "job_type": "bacnet_read",
            "request": {
                "device_instance": 1,
                "object_type": "calendar",
                "object_instance": 1,
            },
        },
    )

    assert response.status_code == 422


def test_bacnet_read_job_poll_without_token_returns_401() -> None:
    client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "bacnet_read", "request": bacnet_read_request()},
    )

    response = client.get("/api/edge/GW001/jobs/next")

    assert response.status_code == 401


def test_bacnet_read_job_poll_with_other_gateway_token_returns_403() -> None:
    raw_token = create_gateway_token("GW002", token_prefix="gw00201")
    client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "bacnet_read", "request": bacnet_read_request()},
    )

    response = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))

    assert response.status_code == 403
