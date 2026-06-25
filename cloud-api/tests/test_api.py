import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test-cloud-api.db"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["GATEWAY_AUTH_PEPPER"] = "test-pepper"
os.environ["IOT_ADMIN_API_TOKEN"] = "test-admin-token"
os.environ["SUPABASE_JWT_SECRET"] = "test-supabase-jwt-secret"

from app.auth import hash_gateway_token
from app.config import Settings
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EdgeNode, GatewayCredential, OperatorUser, Site
from scripts.create_gateway_credential import DEFAULT_SCOPES, create_gateway_credential


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


client = TestClient(app)


def heartbeat_payload(gateway_id: str = "GW001") -> dict[str, object]:
    return {
        "gateway_id": gateway_id,
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


def create_gateway_token(
    gateway_id: str = "GW001",
    token_prefix: str | None = None,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> str:
    from app.database import SessionLocal

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
            edge_node = EdgeNode(
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
            db.add(edge_node)

        db.add(
            GatewayCredential(
                gateway_id=gateway_id,
                token_prefix=token_prefix,
                token_hash=hash_gateway_token(raw_token),
                scopes=DEFAULT_SCOPES,
                revoked_at=revoked_at,
                expires_at=expires_at,
            )
        )
        db.commit()

    return raw_token


def auth_headers(raw_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_token}"}


def admin_headers(token: str = "test-admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def supabase_user_token(email: str = "operator@example.com", user_id: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
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


def user_headers(email: str = "operator@example.com", user_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {supabase_user_token(email=email, user_id=user_id)}"}


def create_operator_user(
    email: str = "operator@example.com",
    role: str = "operator",
    status: str = "active",
    user_id: str | None = None,
) -> str:
    user_id = user_id or str(uuid4())
    with SessionLocal() as db:
        db.add(
            OperatorUser(
                supabase_user_id=user_id,
                email=email.lower(),
                role=role,
                status=status,
            )
        )
        db.commit()
    return user_id


def admin_route_cases() -> list[tuple[str, str, dict[str, object] | None]]:
    return [
        ("GET", "/api/edge/gateways", None),
        ("POST", "/api/edge/jobs", {"gateway_id": "GW001", "job_type": "echo", "request": {}}),
        ("GET", "/api/edge/jobs", None),
        ("POST", "/api/admin/gateways/provision", {"gateway_id": "GW777", "site_id": "test-bench", "hostname": "GW777"}),
    ]


def request_admin_route(method: str, path: str, headers: dict[str, str] | None, json: dict[str, object] | None):
    if method == "GET":
        return client.get(path, headers=headers)
    if method == "POST":
        return client.post(path, headers=headers, json=json)
    raise AssertionError(f"Unsupported test method: {method}")


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_load_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = "postgresql+psycopg://postgres.project-ref:password@example.pooler.supabase.com:5432/postgres?sslmode=require"

    monkeypatch.setenv("DATABASE_URL", database_url)

    assert Settings().database_url == database_url


def test_settings_load_admin_api_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IOT_ADMIN_API_TOKEN", "admin-secret")

    assert Settings().admin_api_token == "admin-secret"


def test_settings_load_supabase_jwt_secret_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "jwt-secret")

    assert Settings().supabase_jwt_secret == "jwt-secret"


def test_settings_load_public_supabase_browser_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-public-key")

    loaded = Settings()

    assert loaded.supabase_url == "https://example.supabase.co"
    assert loaded.supabase_anon_key == "anon-public-key"


def test_openapi_documents_admin_bearer_auth() -> None:
    schema = client.get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["AdminBearer"]["type"] == "http"
    assert schema["components"]["securitySchemes"]["AdminBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/api/edge/gateways"]["get"]["security"] == [{"AdminBearer": []}]
    assert schema["paths"]["/api/edge/jobs"]["post"]["security"] == [{"AdminBearer": []}]
    assert schema["paths"]["/api/edge/jobs"]["get"]["security"] == [{"AdminBearer": []}]
    assert schema["paths"]["/api/admin/gateways/provision"]["post"]["security"] == [{"AdminBearer": []}]
    assert schema["paths"]["/api/admin/users"]["get"]["security"] == [{"AdminBearer": []}]
    assert schema["paths"]["/api/auth/register"]["post"]["security"] == [{"AdminBearer": []}]


def test_database_health() -> None:
    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_redirects_to_login() -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/login", "Login"),
        ("/signup", "Sign Up"),
        ("/auth/check-email", "Check Your Email"),
        ("/auth/waiting-approval", "Waiting For Approval"),
        ("/auth/unauthorized", "Unauthorized"),
        ("/app", "Dashboard"),
        ("/admin/users", "Assign User"),
    ],
)
def test_auth_ui_pages_load(path: str, expected: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert expected in response.text


def test_admin_users_page_uses_session_api_not_manual_token_paste() -> None:
    response = client.get("/admin/users")

    assert response.status_code == 200
    assert "/api/admin/users" in response.text
    assert "Bearer token" not in response.text


def test_protected_ui_contains_unauthenticated_redirect() -> None:
    response = client.get("/app")

    assert response.status_code == 200
    assert 'window.location.assign(statePaths.login)' in response.text
    assert "/api/auth/me" in response.text


def test_public_auth_config_reports_missing_browser_config() -> None:
    response = client.get("/api/auth/public-config")

    assert response.status_code == 200
    assert response.json() == {"supabase_url": None, "supabase_anon_key": None, "configured": False}


def test_public_auth_config_exposes_only_public_supabase_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import settings

    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-public-key")

    response = client.get("/api/auth/public-config")

    assert response.status_code == 200
    assert response.json() == {
        "supabase_url": "https://example.supabase.co",
        "supabase_anon_key": "anon-public-key",
        "configured": True,
    }
    assert "test-admin-token" not in response.text
    assert "test-pepper" not in response.text


def test_heartbeat_without_token_returns_401() -> None:
    response = client.post("/api/edge/heartbeat", json=heartbeat_payload())

    assert response.status_code == 401


def test_heartbeat_with_bad_token_returns_401() -> None:
    response = client.post(
        "/api/edge/heartbeat",
        headers={"Authorization": "Bearer not-a-gateway-token"},
        json=heartbeat_payload(),
    )

    assert response.status_code == 401


def test_heartbeat_creates_gateway_and_history() -> None:
    raw_token = create_gateway_token("GW001")

    heartbeat = client.post("/api/edge/heartbeat", headers=auth_headers(raw_token), json=heartbeat_payload("GW001"))
    gateways = client.get("/api/edge/gateways", headers=admin_headers())

    assert heartbeat.status_code == 200
    assert heartbeat.json()["gateway_id"] == "GW001"
    assert heartbeat.json()["status"] == "online"
    assert gateways.status_code == 200
    assert gateways.json()[0]["gateway_id"] == "GW001"
    assert gateways.json()[0]["site_id"] == "demo-site"


def test_admin_gateways_reject_missing_auth() -> None:
    response = client.get("/api/edge/gateways")

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing admin credentials"


def test_admin_gateways_reject_bad_auth() -> None:
    response = client.get("/api/edge/gateways", headers=admin_headers("not-the-admin-token"))

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid admin credentials"


def test_admin_gateways_reject_raw_token_without_bearer() -> None:
    response = client.get("/api/edge/gateways", headers={"Authorization": "test-admin-token"})

    assert response.status_code == 401


def test_admin_gateways_accept_valid_admin_token() -> None:
    create_gateway_token("GW001")

    response = client.get("/api/edge/gateways", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()[0]["gateway_id"] == "GW001"


def test_register_operator_profile_requires_supabase_user_token() -> None:
    missing_response = client.post("/api/auth/register")
    admin_response = client.post("/api/auth/register", headers=admin_headers())

    assert missing_response.status_code == 401
    assert admin_response.status_code == 403


def test_register_operator_profile_creates_pending_user() -> None:
    user_id = str(uuid4())
    response = client.post("/api/auth/register", headers=user_headers("NewUser@Example.com", user_id))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "newuser@example.com"
    assert body["role"] == "pending"
    assert body["status"] == "pending"
    assert body["supabase_user_id"] == user_id


def test_pending_operator_profile_cannot_call_operator_routes() -> None:
    user_id = create_operator_user("pending@example.com", role="pending", status="pending")

    response = client.get("/api/edge/gateways", headers=user_headers("pending@example.com", user_id))

    assert response.status_code == 401


def test_pending_operator_profile_can_read_me_for_waiting_page() -> None:
    user_id = create_operator_user("pending@example.com", role="pending", status="pending")

    response = client.get("/api/auth/me", headers=user_headers("pending@example.com", user_id))

    assert response.status_code == 200
    assert response.json()["email"] == "pending@example.com"
    assert response.json()["role"] == "pending"
    assert response.json()["status"] == "pending"


def test_disabled_operator_profile_can_read_me_for_unauthorized_page() -> None:
    user_id = create_operator_user("disabled@example.com", role="operator", status="disabled")

    response = client.get("/api/auth/me", headers=user_headers("disabled@example.com", user_id))

    assert response.status_code == 200
    assert response.json()["email"] == "disabled@example.com"
    assert response.json()["status"] == "disabled"


def test_admin_user_management_upserts_and_lists_operator_users() -> None:
    response = client.put(
        "/api/admin/users/operator@example.com",
        headers=admin_headers(),
        json={
            "email": "operator@example.com",
            "role": "operator",
            "status": "active",
            "display_name": "Office Operator",
        },
    )
    listing = client.get("/api/admin/users", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["email"] == "operator@example.com"
    assert response.json()["role"] == "operator"
    assert response.json()["status"] == "active"
    assert listing.status_code == 200
    assert listing.json()[0]["email"] == "operator@example.com"


def test_admin_user_management_rejects_non_admin_user() -> None:
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.get("/api/admin/users", headers=user_headers("operator@example.com", user_id))

    assert response.status_code == 403


def test_active_operator_user_can_call_operator_routes() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.get("/api/edge/gateways", headers=user_headers("operator@example.com", user_id))

    assert response.status_code == 200
    assert response.json()[0]["gateway_id"] == "GW001"


def test_active_operator_user_can_create_jobs() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.post(
        "/api/edge/jobs",
        headers=user_headers("operator@example.com", user_id),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_viewer_user_is_read_only_for_job_creation() -> None:
    user_id = create_operator_user("viewer@example.com", role="viewer", status="active")

    gateway_response = client.get("/api/edge/gateways", headers=user_headers("viewer@example.com", user_id))
    job_response = client.post(
        "/api/edge/jobs",
        headers=user_headers("viewer@example.com", user_id),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    assert gateway_response.status_code == 200
    assert job_response.status_code == 403


def test_active_admin_user_can_manage_users() -> None:
    user_id = create_operator_user("admin@example.com", role="admin", status="active")

    response = client.put(
        "/api/admin/users/operator@example.com",
        headers=user_headers("admin@example.com", user_id),
        json={"email": "operator@example.com", "role": "operator", "status": "active"},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "operator@example.com"


def test_admin_gateways_accept_admin_token_with_incidental_whitespace() -> None:
    create_gateway_token("GW001")

    response = client.get("/api/edge/gateways", headers=admin_headers("  test-admin-token  "))

    assert response.status_code == 200
    assert response.json()[0]["gateway_id"] == "GW001"


@pytest.mark.parametrize(("method", "path", "body"), admin_route_cases())
def test_admin_routes_reject_missing_auth(method: str, path: str, body: dict[str, object] | None) -> None:
    response = request_admin_route(method, path, headers=None, json=body)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), admin_route_cases())
def test_admin_routes_reject_bad_auth(method: str, path: str, body: dict[str, object] | None) -> None:
    response = request_admin_route(method, path, headers=admin_headers("bad-admin-token"), json=body)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), admin_route_cases())
def test_admin_routes_reject_raw_token_without_bearer(
    method: str, path: str, body: dict[str, object] | None
) -> None:
    response = request_admin_route(method, path, headers={"Authorization": "test-admin-token"}, json=body)

    assert response.status_code == 401


def test_gateway_token_cannot_call_admin_routes() -> None:
    raw_token = create_gateway_token("GW001")

    response = client.get("/api/edge/gateways", headers=auth_headers(raw_token))

    assert response.status_code == 401


def test_heartbeat_with_mismatched_gateway_id_returns_403() -> None:
    raw_token = create_gateway_token("GW001")

    response = client.post("/api/edge/heartbeat", headers=auth_headers(raw_token), json=heartbeat_payload("GW002"))

    assert response.status_code == 403


def test_revoked_credential_returns_401() -> None:
    raw_token = create_gateway_token("GW001", revoked_at=datetime.now(timezone.utc))

    response = client.post("/api/edge/heartbeat", headers=auth_headers(raw_token), json=heartbeat_payload("GW001"))

    assert response.status_code == 401


def test_expired_credential_returns_401() -> None:
    raw_token = create_gateway_token("GW001", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    response = client.post("/api/edge/heartbeat", headers=auth_headers(raw_token), json=heartbeat_payload("GW001"))

    assert response.status_code == 401


def test_create_gateway_credential_helper_writes_name_not_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.database import SessionLocal

    monkeypatch.setattr(
        "scripts.create_gateway_credential.generate_token",
        lambda: ("helper01", "iotcc_gw_helper01_test-secret-value"),
    )

    with SessionLocal() as db:
        site = Site(site_id="demo-site", name="demo-site")
        db.add(site)
        db.flush()
        db.add(
            EdgeNode(
                gateway_id="GW001",
                site_id="demo-site",
                hostname="edge-demo",
                bacnet_port=47814,
                agent_version="0.1.0",
                ui_version="0.1.0",
                sqlite_db_ok=True,
                queued_upload_count=0,
                latest_status="online",
            )
        )
        db.commit()

        raw_token = create_gateway_credential(db, "GW001", name="Primary edge credential")
        credential = db.scalar(select(GatewayCredential).where(GatewayCredential.token_prefix == "helper01"))

    assert raw_token == "iotcc_gw_helper01_test-secret-value"
    assert credential is not None
    assert credential.name == "Primary edge credential"
    assert credential.scopes == DEFAULT_SCOPES
    assert credential.token_hash == hash_gateway_token(raw_token)
    assert not hasattr(credential, "label")


def test_create_claim_and_complete_job() -> None:
    raw_token = create_gateway_token("GW001")
    create_response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {"message": "hello edge"}},
    )

    assert create_response.status_code == 200
    created = create_response.json()
    assert created["status"] == "queued"
    assert created["request_json"] == {"message": "hello edge"}

    next_response = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))

    assert next_response.status_code == 200
    claimed = next_response.json()
    assert claimed["job_id"] == created["job_id"]
    assert claimed["job_type"] == "echo"
    assert claimed["request"] == {"message": "hello edge"}

    empty_response = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))
    assert empty_response.status_code == 200
    assert empty_response.json() is None

    result_response = client.post(
        f"/api/edge/jobs/{created['job_id']}/result",
        headers=auth_headers(raw_token),
        json={"status": "completed", "result": {"echo": True}, "error_message": None},
    )

    assert result_response.status_code == 200
    completed = result_response.json()
    assert completed["status"] == "completed"
    assert completed["result_json"] == {"echo": True}
    assert completed["completed_at"] is not None


def test_job_creation_rejects_payload_field() -> None:
    response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={
            "gateway_id": "GW001",
            "job_type": "bacnet_runtime_check",
            "payload": {"bacnet_port": 47814},
        },
    )

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["body", "payload"]
    assert error["type"] == "extra_forbidden"


def test_job_poll_without_token_returns_401() -> None:
    response = client.get("/api/edge/GW001/jobs/next")

    assert response.status_code == 401


def test_job_poll_with_bad_token_returns_401() -> None:
    response = client.get("/api/edge/GW001/jobs/next", headers={"Authorization": "Bearer not-a-gateway-token"})

    assert response.status_code == 401


def test_job_poll_with_other_gateway_token_returns_403() -> None:
    raw_token = create_gateway_token("GW002", token_prefix="gw00201")

    response = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))

    assert response.status_code == 403


def test_job_result_can_mark_failed() -> None:
    raw_token = create_gateway_token("GW001")
    create_response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "unknown", "request": {}},
    )
    job_id = create_response.json()["job_id"]

    result_response = client.post(
        f"/api/edge/jobs/{job_id}/result",
        headers=auth_headers(raw_token),
        json={"status": "failed", "result": None, "error_message": "Unknown job_type: unknown"},
    )
    jobs_response = client.get("/api/edge/jobs", headers=admin_headers())

    assert result_response.status_code == 200
    assert result_response.json()["status"] == "failed"
    assert result_response.json()["error_message"] == "Unknown job_type: unknown"
    assert jobs_response.status_code == 200
    assert jobs_response.json()[0]["job_id"] == job_id


def test_job_result_can_mark_deferred() -> None:
    raw_token = create_gateway_token("GW001")
    create_response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={
            "gateway_id": "GW001",
            "job_type": "bacnet_read",
            "request": {
                "device_instance": 1,
                "object_type": "analog-value",
                "object_instance": 1,
                "property": "present-value",
            },
        },
    )
    job_id = create_response.json()["job_id"]
    result_payload = {
        "job_type": "bacnet_read",
        "status": "deferred",
        "error": "bacnet_runtime_busy",
        "lock_path": "/tmp/iot-cloud-commissioning-bacnet-47814.lock",
        "lock_held": True,
    }

    result_response = client.post(
        f"/api/edge/jobs/{job_id}/result",
        headers=auth_headers(raw_token),
        json={"status": "deferred", "result": result_payload, "error_message": "bacnet_runtime_busy"},
    )

    assert result_response.status_code == 200
    assert result_response.json()["status"] == "deferred"
    assert result_response.json()["result_json"] == result_payload


def test_job_result_without_token_returns_401() -> None:
    create_response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    response = client.post(
        f"/api/edge/jobs/{create_response.json()['job_id']}/result",
        json={"status": "completed", "result": {}, "error_message": None},
    )

    assert response.status_code == 401


def test_job_result_with_bad_token_returns_401() -> None:
    create_response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    response = client.post(
        f"/api/edge/jobs/{create_response.json()['job_id']}/result",
        headers={"Authorization": "Bearer not-a-gateway-token"},
        json={"status": "completed", "result": {}, "error_message": None},
    )

    assert response.status_code == 401


def test_job_result_with_other_gateway_token_returns_403() -> None:
    raw_token = create_gateway_token("GW002", token_prefix="gw00201")
    create_response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    response = client.post(
        f"/api/edge/jobs/{create_response.json()['job_id']}/result",
        headers=auth_headers(raw_token),
        json={"status": "completed", "result": {}, "error_message": None},
    )

    assert response.status_code == 403


def test_job_creation_requires_admin_auth() -> None:
    response = client.post(
        "/api/edge/jobs",
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    assert response.status_code == 401


def test_job_creation_rejects_bad_admin_auth() -> None:
    response = client.post(
        "/api/edge/jobs",
        headers=admin_headers("bad-admin-token"),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    assert response.status_code == 401


def test_job_creation_rejects_gateway_token() -> None:
    raw_token = create_gateway_token("GW001")

    response = client.post(
        "/api/edge/jobs",
        headers=auth_headers(raw_token),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    assert response.status_code == 401


def test_job_listing_requires_admin_auth() -> None:
    response = client.get("/api/edge/jobs")

    assert response.status_code == 401


def test_job_listing_accepts_admin_auth() -> None:
    create_gateway_token("GW001")
    client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "echo", "request": {}},
    )

    response = client.get("/api/edge/jobs", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()[0]["gateway_id"] == "GW001"


def test_admin_provision_gateway_creates_identity_and_token() -> None:
    response = client.post(
        "/api/admin/gateways/provision",
        headers=admin_headers(),
        json={
            "gateway_id": "GW777",
            "site_id": "test-bench",
            "hostname": "GW777",
            "lan_ip": "192.168.1.200",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["gateway_id"] == "GW777"
    assert body["site_id"] == "test-bench"
    assert body["hostname"] == "GW777"
    assert body["lan_ip"] == "192.168.1.200"
    assert body["bacnet_port"] == 47814
    assert body["agent_version"] == "0.1.0"
    assert body["ui_version"] == "0.1.0"
    assert body["gateway_api_token"].startswith(f"iotcc_gw_{body['token_prefix']}_")
    assert "pepper" not in body
    assert "token_hash" not in body

    with SessionLocal() as db:
        edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == "GW777"))
        credential = db.scalar(select(GatewayCredential).where(GatewayCredential.token_prefix == body["token_prefix"]))

    assert edge_node is not None
    assert edge_node.site_id == "test-bench"
    assert credential is not None
    assert credential.gateway_id == "GW777"
    assert credential.token_hash == hash_gateway_token(body["gateway_api_token"])


def test_admin_provision_gateway_requires_admin_auth() -> None:
    response = client.post(
        "/api/admin/gateways/provision",
        json={"gateway_id": "GW777", "site_id": "test-bench", "hostname": "GW777"},
    )

    assert response.status_code == 401


def test_admin_provision_gateway_updates_existing_identity_and_issues_new_token() -> None:
    client.post(
        "/api/admin/gateways/provision",
        headers=admin_headers(),
        json={"gateway_id": "GW777", "site_id": "old-site", "hostname": "old-host"},
    )

    response = client.post(
        "/api/admin/gateways/provision",
        headers=admin_headers(),
        json={
            "gateway_id": "GW777",
            "site_id": "test-bench",
            "hostname": "GW777",
            "lan_ip": "192.168.1.201",
            "bacnet_port": 47814,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["site_id"] == "test-bench"
    assert body["hostname"] == "GW777"
    assert body["lan_ip"] == "192.168.1.201"

    with SessionLocal() as db:
        edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == "GW777"))
        credentials = list(
            db.scalars(select(GatewayCredential).where(GatewayCredential.gateway_id == "GW777")).all()
        )

    assert edge_node is not None
    assert edge_node.site_id == "test-bench"
    assert len(credentials) == 2
