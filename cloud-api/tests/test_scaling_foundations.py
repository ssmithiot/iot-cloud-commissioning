"""Phase 2 scaling-foundation tests: pooling, auth telemetry throttling,
tenant boundaries on legacy edge endpoints, heartbeat retention, and
request-timing observability."""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt
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

from app.auth import hash_gateway_token, require_job_operator_auth, require_operator_auth, AdminAuthContext
from app.config import Settings
from app.database import Base, SessionLocal, engine, pool_engine_kwargs
from app.main import app
from app.models import EdgeHeartbeat, EdgeNode, GatewayCredential, OperatorUser, Site, utc_now
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


def user_headers(email: str = "operator@example.com", user_id: str | None = None) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "aud": "authenticated",
            "exp": now + timedelta(minutes=15),
            "iat": now,
            "sub": user_id or str(uuid4()),
            "email": email,
            "role": "authenticated",
        },
        "test-supabase-jwt-secret",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def heartbeat_payload(gateway_id: str = "GW001", site_id: str = "demo-site") -> dict[str, object]:
    return {
        "gateway_id": gateway_id,
        "site_id": site_id,
        "hostname": "edge-demo",
        "lan_ip": "192.168.1.10",
        "bacnet_port": 47814,
        "agent_version": "0.1.1",
        "ui_version": "0.1.0",
        "sqlite_db_ok": True,
        "queued_upload_count": 0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def create_gateway(gateway_id: str = "GW001", site_id: str = "demo-site") -> str:
    token_prefix = f"{gateway_id.lower()}01"
    raw_token = f"iotcc_gw_{token_prefix}_test-secret-value-for-{gateway_id}"
    with SessionLocal() as db:
        site = db.scalar(select(Site).where(Site.site_id == site_id))
        if site is None:
            site = Site(site_id=site_id, name=site_id)
            db.add(site)
            db.flush()
        if db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == gateway_id)) is None:
            db.add(
                EdgeNode(
                    gateway_id=gateway_id,
                    site_id=site_id,
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


def scoped_operator_override(visible_site_id: str = "demo-site") -> AdminAuthContext:
    with SessionLocal() as db:
        operator = OperatorUser(
            supabase_user_id=str(uuid4()),
            email="scoped-ops@example.com",
            role="operator",
            status="active",
        )
        db.add(operator)
        db.flush()
        site = db.scalar(select(Site).where(Site.site_id == visible_site_id))
        assert site is not None
        from app.models import SiteMembership

        db.add(SiteMembership(site_uuid=site.id, operator_user_id=operator.id, role="operator"))
        db.commit()
        operator_id = str(operator.id)
    return AdminAuthContext(auth_type="supabase_user", role="operator", operator_user_id=operator_id)


# --- Database pool configuration -------------------------------------------


def test_pool_settings_load_and_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "20")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "5")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "10")
    monkeypatch.setenv("DB_POOL_RECYCLE", "900")
    loaded = Settings()
    assert loaded.db_pool_size == 20
    assert loaded.db_max_overflow == 5
    assert loaded.db_pool_timeout_sec == 10
    assert loaded.db_pool_recycle_sec == 900


def test_pool_settings_reject_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    with pytest.raises(Exception):
        Settings()


def test_pool_kwargs_apply_to_server_databases_only() -> None:
    assert pool_engine_kwargs("sqlite:///./test.db") == {}
    kwargs = pool_engine_kwargs("postgresql+psycopg://u:p@host:5432/db")
    assert kwargs["pool_size"] >= 1
    assert kwargs["max_overflow"] >= 0
    assert kwargs["pool_timeout"] >= 1
    assert kwargs["pool_recycle"] >= 60


# --- Auth telemetry throttling ----------------------------------------------


def test_gateway_last_used_at_writes_are_throttled() -> None:
    raw_token = create_gateway("GW001")
    assert client.post("/api/edge/heartbeat", json=heartbeat_payload(), headers=auth_headers(raw_token)).status_code == 200
    with SessionLocal() as db:
        credential = db.scalar(select(GatewayCredential).where(GatewayCredential.gateway_id == "GW001"))
        first_seen = credential.last_used_at
        assert first_seen is not None

    assert client.post("/api/edge/heartbeat", json=heartbeat_payload(), headers=auth_headers(raw_token)).status_code == 200
    with SessionLocal() as db:
        credential = db.scalar(select(GatewayCredential).where(GatewayCredential.gateway_id == "GW001"))
        assert credential.last_used_at == first_seen  # unchanged within throttle window

    # Once the recorded value ages past the interval, the next request updates it.
    with SessionLocal() as db:
        credential = db.scalar(select(GatewayCredential).where(GatewayCredential.gateway_id == "GW001"))
        credential.last_used_at = utc_now() - timedelta(seconds=3600)
        db.commit()
    assert client.post("/api/edge/heartbeat", json=heartbeat_payload(), headers=auth_headers(raw_token)).status_code == 200
    with SessionLocal() as db:
        credential = db.scalar(select(GatewayCredential).where(GatewayCredential.gateway_id == "GW001"))
        refreshed = credential.last_used_at
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=timezone.utc)
        assert refreshed > utc_now() - timedelta(seconds=60)


def test_operator_last_login_writes_are_throttled() -> None:
    user_id = str(uuid4())
    with SessionLocal() as db:
        db.add(
            OperatorUser(
                supabase_user_id=user_id,
                email="operator@example.com",
                role="operator",
                status="active",
            )
        )
        db.commit()

    headers = user_headers(user_id=user_id)
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    with SessionLocal() as db:
        operator = db.scalar(select(OperatorUser).where(OperatorUser.email == "operator@example.com"))
        first_login = operator.last_login_at
        assert first_login is not None

    assert client.get("/api/auth/me", headers=headers).status_code == 200
    with SessionLocal() as db:
        operator = db.scalar(select(OperatorUser).where(OperatorUser.email == "operator@example.com"))
        assert operator.last_login_at == first_login  # unchanged within throttle window


# --- Tenant boundaries on legacy edge endpoints ------------------------------


def test_legacy_edge_endpoints_enforce_site_scope() -> None:
    create_gateway("GW001", site_id="demo-site")
    create_gateway("GW002", site_id="hidden-site")
    scoped_auth = scoped_operator_override("demo-site")

    app.dependency_overrides[require_operator_auth] = lambda: scoped_auth
    app.dependency_overrides[require_job_operator_auth] = lambda: scoped_auth
    try:
        listed = client.get("/api/edge/gateways")
        assert listed.status_code == 200
        assert [gateway["gateway_id"] for gateway in listed.json()] == ["GW001"]

        allowed = client.post(
            "/api/edge/jobs",
            json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
        )
        assert allowed.status_code == 200

        denied = client.post(
            "/api/edge/jobs",
            json={"gateway_id": "GW002", "job_type": "echo", "request": {}},
        )
        assert denied.status_code == 404  # hidden tenants are not enumerable
    finally:
        app.dependency_overrides.clear()


def test_job_creation_rejects_unknown_gateway_for_scoped_operators() -> None:
    create_gateway("GW001", site_id="demo-site")
    scoped_auth = scoped_operator_override("demo-site")
    app.dependency_overrides[require_job_operator_auth] = lambda: scoped_auth
    try:
        response = client.post(
            "/api/edge/jobs",
            json={"gateway_id": "NO-SUCH-GW", "job_type": "echo", "request": {}},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()

    # Platform admins keep the legacy pre-provisioning flow: jobs may be
    # queued for gateways that have not yet sent a heartbeat.
    legacy = client.post(
        "/api/edge/jobs",
        json={"gateway_id": "NOT-YET-PROVISIONED", "job_type": "echo", "request": {}},
        headers=admin_headers(),
    )
    assert legacy.status_code == 200


# --- Heartbeat history retention ---------------------------------------------


def test_heartbeat_history_is_pruned_beyond_retention() -> None:
    raw_token = create_gateway("GW001")
    assert client.post("/api/edge/heartbeat", json=heartbeat_payload(), headers=auth_headers(raw_token)).status_code == 200

    with SessionLocal() as db:
        edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == "GW001"))
        db.add(
            EdgeHeartbeat(
                edge_node_id=edge_node.id,
                gateway_id="GW001",
                site_id="demo-site",
                hostname="edge-demo",
                bacnet_port=47814,
                agent_version="0.1.1",
                ui_version="0.1.0",
                sqlite_db_ok=True,
                queued_upload_count=0,
                timestamp_utc=utc_now() - timedelta(days=400),
            )
        )
        db.commit()

    assert client.post("/api/edge/heartbeat", json=heartbeat_payload(), headers=auth_headers(raw_token)).status_code == 200
    with SessionLocal() as db:
        timestamps = list(
            db.scalars(select(EdgeHeartbeat.timestamp_utc).where(EdgeHeartbeat.gateway_id == "GW001"))
        )
        assert timestamps, "recent heartbeats must be retained"
        oldest_allowed = utc_now() - timedelta(days=366)
        for timestamp in timestamps:
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            assert timestamp > oldest_allowed, "heartbeats older than retention must be pruned"


# --- Job claiming order (FOR UPDATE SKIP LOCKED path) ------------------------


def test_job_claiming_still_returns_oldest_queued_job() -> None:
    raw_token = create_gateway("GW001")
    first = client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "echo", "request": {"n": 1}},
        headers=admin_headers(),
    )
    second = client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "echo", "request": {"n": 2}},
        headers=admin_headers(),
    )
    assert first.status_code == 200 and second.status_code == 200

    claimed = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))
    assert claimed.status_code == 200
    assert claimed.json()["job_id"] == first.json()["job_id"]


# --- Request timing observability --------------------------------------------


def test_request_timing_middleware_logs_route_template(caplog: pytest.LogCaptureFixture) -> None:
    # Alembic's fileConfig (run by the schema-reconciliation tests) disables
    # pre-existing loggers; re-enable ours so this test is order-independent.
    logging.getLogger("iot-cloud-api.requests").disabled = False
    with caplog.at_level(logging.INFO, logger="iot-cloud-api.requests"):
        client.get("/health")
        client.get("/api/ui/gateways", headers=admin_headers())

    messages = [record.getMessage() for record in caplog.records if record.name == "iot-cloud-api.requests"]
    assert not any("/health" in message for message in messages), "health checks stay out of request logs"
    assert any("path=/api/ui/gateways" in message and "status=200" in message for message in messages)
