"""Cloud mirror for Edge-owned local trend samples (0.2.0).

The gateway owns local trend configuration and history. The cloud only stores
copies of readings the gateway already took, keyed by the gateway-generated
event_id so an upload retry cannot duplicate a reading.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test-cloud-api.db"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["GATEWAY_AUTH_PEPPER"] = "test-pepper"
os.environ["IOT_ADMIN_API_TOKEN"] = "test-admin-token"
os.environ["SUPABASE_JWT_SECRET"] = "test-supabase-jwt-secret"

from app.auth import hash_gateway_token
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EdgeLocalTrendSample, EdgeNode, GatewayCredential, Site
from scripts.create_gateway_credential import DEFAULT_SCOPES


client = TestClient(app)
ADMIN = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


def create_gateway(gateway_id: str = "GW006") -> str:
    token_prefix = f"{gateway_id.lower()}01"
    raw_token = f"iotcc_gw_{token_prefix}_test-secret-value-for-{gateway_id}"
    with SessionLocal() as db:
        if db.scalar(select(Site).where(Site.site_id == "demo-site")) is None:
            db.add(Site(site_id="demo-site", name="demo-site"))
            db.flush()
        db.add(
            EdgeNode(
                gateway_id=gateway_id,
                site_id="demo-site",
                hostname=f"{gateway_id.lower()}-host",
                bacnet_port=47814,
                agent_version="0.2.0",
                ui_version="0.2.0",
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


def sample(**overrides) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "group_name": "AHU-01 verification",
        "device_instance": 1103,
        "object_type": "analog-value",
        "object_instance": 7,
        "object_name": "Supply Temp",
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "value_text": "71.5",
        "status": "ok",
        "read_source": "rpm-bulk",
        "error_text": None,
    }
    payload.update(overrides)
    return payload


def gateway_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_gateway_can_upload_local_trend_samples() -> None:
    token = create_gateway()

    response = client.post(
        "/api/edge/GW006/local-trend-samples",
        headers=gateway_headers(token),
        json=[sample(), sample(object_instance=8, value_text="68.0")],
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": 2, "duplicates": 0}
    with SessionLocal() as db:
        rows = db.scalars(select(EdgeLocalTrendSample)).all()
    assert len(rows) == 2
    assert {row.group_name for row in rows} == {"AHU-01 verification"}


def test_resending_the_same_event_id_is_ignored() -> None:
    token = create_gateway()
    batch = [sample()]

    first = client.post("/api/edge/GW006/local-trend-samples", headers=gateway_headers(token), json=batch)
    second = client.post("/api/edge/GW006/local-trend-samples", headers=gateway_headers(token), json=batch)

    assert first.json() == {"accepted": 1, "duplicates": 0}
    assert second.json() == {"accepted": 0, "duplicates": 1}
    with SessionLocal() as db:
        assert len(db.scalars(select(EdgeLocalTrendSample)).all()) == 1


def test_a_batch_with_duplicate_event_ids_is_rejected() -> None:
    token = create_gateway()
    duplicate = sample()

    response = client.post(
        "/api/edge/GW006/local-trend-samples",
        headers=gateway_headers(token),
        json=[duplicate, dict(duplicate)],
    )

    assert response.status_code == 422


def test_failed_and_missing_reads_are_stored_with_their_quality() -> None:
    token = create_gateway()

    response = client.post(
        "/api/edge/GW006/local-trend-samples",
        headers=gateway_headers(token),
        json=[
            sample(status="missing", value_text=None, error_text="present-value absent"),
            sample(status="error", value_text=None, error_text="device timeout", object_instance=9),
        ],
    )

    assert response.status_code == 200
    with SessionLocal() as db:
        rows = db.scalars(select(EdgeLocalTrendSample).order_by(EdgeLocalTrendSample.object_instance)).all()
    assert [row.status for row in rows] == ["missing", "error"]
    assert all(row.value_text is None for row in rows)
    assert rows[1].error_text == "device timeout"


def test_another_gateways_credential_is_refused() -> None:
    create_gateway("GW006")
    other_token = create_gateway("GW015")

    response = client.post(
        "/api/edge/GW006/local-trend-samples",
        headers=gateway_headers(other_token),
        json=[sample()],
    )

    assert response.status_code == 403
    with SessionLocal() as db:
        assert db.scalars(select(EdgeLocalTrendSample)).all() == []


def test_upload_requires_gateway_authentication() -> None:
    create_gateway()

    response = client.post("/api/edge/GW006/local-trend-samples", json=[sample()])

    assert response.status_code in {401, 403}


def test_samples_older_than_retention_are_pruned() -> None:
    token = create_gateway()
    stale = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()

    client.post("/api/edge/GW006/local-trend-samples", headers=gateway_headers(token), json=[sample(sampled_at=stale)])
    client.post("/api/edge/GW006/local-trend-samples", headers=gateway_headers(token), json=[sample()])

    with SessionLocal() as db:
        rows = db.scalars(select(EdgeLocalTrendSample)).all()
    assert len(rows) == 1


def test_operator_can_read_the_mirror_but_it_is_read_only() -> None:
    token = create_gateway()
    client.post("/api/edge/GW006/local-trend-samples", headers=gateway_headers(token), json=[sample()])

    response = client.get("/api/ui/gateways/GW006/local-trend-samples", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["group_name"] == "AHU-01 verification"
    assert body[0]["gateway_id"] == "GW006"
    # The cloud offers no way to create, edit or delete an Edge trend group.
    assert {route.path for route in app.routes if "local-trend" in route.path} == {
        "/api/edge/{gateway_id}/local-trend-samples",
        "/api/ui/gateways/{gateway_id}/local-trend-samples",
    }
