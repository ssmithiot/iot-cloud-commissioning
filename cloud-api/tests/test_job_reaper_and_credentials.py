"""Stale-job reaper and credential lifecycle endpoint tests (debt Tier 1 #2, #3)."""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

EDGE_AGENT_PATH = Path(__file__).resolve().parents[2] / "edge-agent"
if str(EDGE_AGENT_PATH) not in sys.path:
    sys.path.append(str(EDGE_AGENT_PATH))

os.environ["DATABASE_URL"] = "sqlite:///./test-cloud-api.db"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["GATEWAY_AUTH_PEPPER"] = "test-pepper"
os.environ["IOT_ADMIN_API_TOKEN"] = "test-admin-token"
os.environ["SUPABASE_JWT_SECRET"] = "test-supabase-jwt-secret"

import jwt

from app.auth import hash_gateway_token
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EdgeJob, EdgeNode, GatewayCredential, OperatorUser, Site, utc_now
from scripts.create_gateway_credential import DEFAULT_SCOPES


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


client = TestClient(app)


def admin_headers(token: str = "test-admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def auth_headers(raw_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_token}"}


def create_gateway(gateway_id: str = "GW001") -> str:
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


def create_job(gateway_id: str = "GW001", job_type: str = "echo") -> str:
    response = client.post(
        "/api/edge/jobs",
        json={"gateway_id": gateway_id, "job_type": job_type, "request": {}},
        headers=admin_headers(),
    )
    assert response.status_code == 200
    return response.json()["job_id"]


def age_claim(job_id: str, seconds: int) -> None:
    with SessionLocal() as db:
        job = db.scalar(select(EdgeJob).where(EdgeJob.job_id == job_id))
        assert job is not None and job.status == "claimed"
        job.claimed_at = utc_now() - timedelta(seconds=seconds)
        db.commit()


# --- Stale-job reaper ---------------------------------------------------------


def test_stale_claimed_job_is_requeued_and_reclaimed() -> None:
    raw_token = create_gateway()
    job_id = create_job()
    first = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))
    assert first.status_code == 200 and first.json()["job_id"] == job_id

    age_claim(job_id, seconds=3600)  # beyond the 600 s default timeout
    reclaimed = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))
    assert reclaimed.status_code == 200
    assert reclaimed.json() is not None and reclaimed.json()["job_id"] == job_id


def test_fresh_claimed_job_is_not_requeued() -> None:
    raw_token = create_gateway()
    job_id = create_job()
    assert client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token)).json()["job_id"] == job_id
    # Immediately poll again: the claim is fresh, nothing to hand out.
    assert client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token)).json() is None


def test_stale_write_batch_job_is_never_requeued() -> None:
    raw_token = create_gateway()
    with SessionLocal() as db:
        db.add(
            EdgeJob(
                job_id=f"job-{uuid4().hex}",
                gateway_id="GW001",
                job_type="bacnet_write_batch",
                status="claimed",
                request_json={},
                claimed_at=utc_now() - timedelta(seconds=7200),
            )
        )
        db.commit()

    assert client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token)).json() is None
    with SessionLocal() as db:
        job = db.scalar(select(EdgeJob).where(EdgeJob.job_type == "bacnet_write_batch"))
        assert job.status == "claimed"  # left for manual review


def test_reaper_only_touches_the_polling_gateway() -> None:
    raw_a = create_gateway("GW001")
    create_gateway("GW002")
    job_a = create_job("GW001")
    job_b = create_job("GW002")
    raw_b = f"iotcc_gw_gw00201_test-secret-value-for-GW002"
    assert client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_a)).json()["job_id"] == job_a
    assert client.get("/api/edge/GW002/jobs/next", headers=auth_headers(raw_b)).json()["job_id"] == job_b
    age_claim(job_a, 3600)
    age_claim(job_b, 3600)

    # GW001 polls: only its own stale job is requeued.
    assert client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_a)).json()["job_id"] == job_a
    with SessionLocal() as db:
        other = db.scalar(select(EdgeJob).where(EdgeJob.job_id == job_b))
        assert other.status == "claimed"


# --- Credential lifecycle endpoints -------------------------------------------


def test_admin_lists_credentials_without_hashes() -> None:
    create_gateway()
    response = client.get("/api/admin/gateways/GW001/credentials", headers=admin_headers())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    credential = body[0]
    assert credential["gateway_id"] == "GW001"
    assert credential["token_prefix"] == "gw00101"
    assert credential["status"] == "active"
    assert "token_hash" not in response.text
    assert "iotcc_gw_" not in response.text  # no raw tokens either


def test_revoked_credential_stops_authenticating_and_revoke_is_idempotent() -> None:
    raw_token = create_gateway()
    credential_id = client.get("/api/admin/gateways/GW001/credentials", headers=admin_headers()).json()[0]["credential_id"]

    first = client.post(f"/api/admin/credentials/{credential_id}/revoke", headers=admin_headers())
    assert first.status_code == 200 and first.json()["status"] == "revoked"

    # The gateway can no longer authenticate with the revoked token.
    poll = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))
    assert poll.status_code == 401

    second = client.post(f"/api/admin/credentials/{credential_id}/revoke", headers=admin_headers())
    assert second.status_code == 200 and second.json()["status"] == "revoked"
    # Same instant (SQLite round-trips lose the timezone suffix; compare parsed).
    first_at = datetime.fromisoformat(first.json()["revoked_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    second_at = datetime.fromisoformat(second.json()["revoked_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    assert second_at == first_at  # unchanged by the second revoke


def test_rotation_flow_new_credential_works_after_old_revoked() -> None:
    old_token = create_gateway()
    provisioned = client.post(
        "/api/admin/gateways/provision",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "site_id": "demo-site", "hostname": "gw001-host"},
    )
    assert provisioned.status_code == 200
    new_token = provisioned.json()["gateway_api_token"]

    credentials = client.get("/api/admin/gateways/GW001/credentials", headers=admin_headers()).json()
    assert len(credentials) == 2
    old_id = next(c["credential_id"] for c in credentials if c["token_prefix"] == "gw00101")
    assert client.post(f"/api/admin/credentials/{old_id}/revoke", headers=admin_headers()).status_code == 200

    assert client.get("/api/edge/GW001/jobs/next", headers=auth_headers(old_token)).status_code == 401
    assert client.get("/api/edge/GW001/jobs/next", headers=auth_headers(new_token)).status_code == 200


def test_credential_endpoints_require_admin_role() -> None:
    create_gateway()
    with SessionLocal() as db:
        db.add(OperatorUser(supabase_user_id=str(uuid4()), email="op@example.com", role="operator", status="active"))
        db.commit()
    now = datetime.now(timezone.utc)
    operator_token = jwt.encode(
        {"aud": "authenticated", "exp": now + timedelta(minutes=15), "iat": now, "sub": str(uuid4()), "email": "op@example.com", "role": "authenticated"},
        "test-supabase-jwt-secret",
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {operator_token}"}
    assert client.get("/api/admin/gateways/GW001/credentials", headers=headers).status_code == 403
    assert client.post(f"/api/admin/credentials/{uuid4()}/revoke", headers=headers).status_code == 403


def test_revoke_unknown_or_malformed_credential_returns_404() -> None:
    assert client.post(f"/api/admin/credentials/{uuid4()}/revoke", headers=admin_headers()).status_code == 404
    assert client.post("/api/admin/credentials/not-a-uuid/revoke", headers=admin_headers()).status_code == 404


def test_credentials_list_unknown_gateway_returns_404() -> None:
    assert client.get("/api/admin/gateways/NOPE/credentials", headers=admin_headers()).status_code == 404
