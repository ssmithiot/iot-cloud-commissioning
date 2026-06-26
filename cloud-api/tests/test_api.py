import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.websockets import WebSocketDisconnect
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
from app.models import EdgeNode, GatewayCredential, OperatorUser, Site, utc_now
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


def supabase_user_token(
    email: str = "operator@example.com",
    user_id: str | None = None,
    audience: str = "authenticated",
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "aud": audience,
            "exp": now + timedelta(minutes=15),
            "iat": now,
            "sub": user_id or str(uuid4()),
            "email": email,
            "role": "authenticated",
        },
        "test-supabase-jwt-secret",
        algorithm="HS256",
    )


def supabase_rs256_user_token(
    private_key,
    email: str = "operator@example.com",
    user_id: str | None = None,
    audience: str = "authenticated",
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "aud": audience,
            "exp": now + timedelta(minutes=15),
            "iat": now,
            "sub": user_id or str(uuid4()),
            "email": email,
            "role": "authenticated",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-rs256-key"},
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


def set_gateway_heartbeat(gateway_id: str, seconds_ago: int | None) -> None:
    with SessionLocal() as db:
        edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == gateway_id))
        assert edge_node is not None
        edge_node.latest_heartbeat_at = None if seconds_ago is None else utc_now() - timedelta(seconds=seconds_ago)
        edge_node.latest_status = "online"
        db.commit()


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


def test_settings_load_supabase_jwks_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_JWKS_URL", "https://example.supabase.co/auth/v1/.well-known/jwks.json")

    assert Settings().supabase_jwks_url == "https://example.supabase.co/auth/v1/.well-known/jwks.json"


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
    assert schema["paths"]["/api/ui/gateways"]["get"]["security"] == [{"AdminBearer": []}]
    assert schema["paths"]["/api/ui/gateways/{gateway_id}/discover-devices"]["post"]["security"] == [
        {"AdminBearer": []}
    ]


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
        ("/gateways/GW001", "Gateway Workspace"),
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


def test_signup_email_confirmation_redirects_to_login_origin() -> None:
    response = client.get("/signup")

    assert response.status_code == 200
    assert "emailRedirectTo: redirectTo" in response.text
    assert "`${window.location.origin}${statePaths.login}`" in response.text
    assert "localhost" not in response.text


def test_protected_ui_contains_unauthenticated_redirect() -> None:
    response = client.get("/app")

    assert response.status_code == 200
    assert 'window.location.assign(statePaths.login)' in response.text
    assert "/api/auth/me" in response.text


def test_gateway_workspace_contains_discovery_progress_ui() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert 'id="discovery-progress"' in response.text
    assert 'id="discovered-devices"' in response.text
    assert 'class="tree-shell"' in response.text
    assert 'id="tree-details"' in response.text
    assert "renderDiscoveredDevices" in response.text
    assert "Load points" not in response.text
    assert "Saved Tree" not in response.text
    assert "Imported Commissioning Model" in response.text
    assert "Last Import" in response.text
    assert "Use the edge commissioning UI for BACnet discovery and point selection" in response.text
    assert "Site Information" in response.text
    assert 'id="site-info-form"' in response.text
    assert 'id="site-address-street"' in response.text
    assert 'id="site-address-city"' in response.text
    assert 'id="site-address-state"' in response.text
    assert 'id="site-address-postal-code"' in response.text
    assert 'id="direct-connect-link"' in response.text
    assert 'id="tunnel-status"' in response.text
    assert "Direct Connect" in response.text
    assert "GATEWAY_API_TOKEN" not in response.text
    assert "IOT_ADMIN_API_TOKEN" not in response.text
    assert 'id="import-template-form"' in response.text
    assert 'id="import-result"' in response.text
    assert 'id="template-file"' in response.text
    assert "Import template" in response.text
    assert "/commissioning-template/import" in response.text
    assert 'id="selected-points-panel"' in response.text
    assert 'id="selected-points-list"' in response.text
    assert "Remove selected points" in response.text
    assert "saved-point-select" in response.text
    assert "/api/ui/points/bulk-remove" in response.text
    assert 'id="point-candidates-panel"' in response.text
    assert 'id="point-candidates"' in response.text
    assert "Save selected points" in response.text
    assert "select-all-point-candidates" in response.text
    assert "Loaded Point Candidates" in response.text
    assert 'data-role="save-device"' in response.text
    assert "Remove device" in response.text
    assert "Input Objects" in response.text
    assert "pollDiscoveryJob" in response.text
    assert "/api/edge/jobs?limit=50" in response.text
    assert "Unexpected token" not in response.text


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


def test_configure_gateway_redirects_to_cloud_tunnel() -> None:
    create_gateway_token("GW001")

    response = client.get("/gateways/GW001/configure", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/gateways/GW001/tunnel/"


def test_admin_can_update_site_metadata_and_direct_connect() -> None:
    response = client.patch(
        "/api/ui/sites/demo-site",
        headers=admin_headers(),
        json={
            "name": "Demo Store",
            "cradlepoint_ip": "10.20.30.40",
            "direct_connect_host": "10.20.30.40",
            "direct_connect_port": 5002,
            "gateway_ui_port": 5000,
            "address": "123 Main St, Springfield, IL",
            "address_street": "123 Main St",
            "address_city": "Springfield",
            "address_state": "IL",
            "address_postal_code": "62701",
            "store_hours_monday_friday": "8:00 AM - 6:00 PM",
            "store_hours_saturday": "9:00 AM - 5:00 PM",
            "store_hours_sunday": "10:00 AM - 4:00 PM",
            "network_status_notes": "The rest of the boxes on these two networks are online as well.",
        },
    )
    sites = client.get("/api/ui/sites", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["site_id"] == "demo-site"
    assert response.json()["name"] == "Demo Store"
    assert response.json()["direct_connect_host"] == "10.20.30.40"
    assert response.json()["direct_connect_port"] == 5002
    assert response.json()["gateway_ui_port"] == 5000
    assert response.json()["address"] == "123 Main St, Springfield, IL"
    assert response.json()["address_street"] == "123 Main St"
    assert response.json()["address_city"] == "Springfield"
    assert response.json()["address_state"] == "IL"
    assert response.json()["address_postal_code"] == "62701"
    assert response.json()["store_hours_monday_friday"] == "8:00 AM - 6:00 PM"
    assert response.json()["store_hours_saturday"] == "9:00 AM - 5:00 PM"
    assert response.json()["store_hours_sunday"] == "10:00 AM - 4:00 PM"
    assert response.json()["network_status_notes"] == "The rest of the boxes on these two networks are online as well."
    assert sites.status_code == 200
    assert sites.json()[0]["site_id"] == "demo-site"


def test_operator_and_viewer_cannot_update_site_metadata() -> None:
    operator_id = create_operator_user("operator@example.com", role="operator", status="active")
    viewer_id = create_operator_user("viewer@example.com", role="viewer", status="active")

    operator_response = client.patch(
        "/api/ui/sites/demo-site",
        headers=user_headers("operator@example.com", operator_id),
        json={"name": "Operator Edit"},
    )
    viewer_response = client.patch(
        "/api/ui/sites/demo-site",
        headers=user_headers("viewer@example.com", viewer_id),
        json={"name": "Viewer Edit"},
    )

    assert operator_response.status_code == 403
    assert viewer_response.status_code == 403


def test_operator_can_read_site_metadata() -> None:
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    client.patch("/api/ui/sites/demo-site", headers=admin_headers(), json={"name": "Demo Store"})

    response = client.get("/api/ui/sites/demo-site", headers=user_headers("operator@example.com", user_id))

    assert response.status_code == 200
    assert response.json()["name"] == "Demo Store"


def test_gateway_list_includes_site_info_and_direct_connect_availability() -> None:
    create_gateway_token("GW001")
    client.patch(
        "/api/ui/sites/demo-site",
        headers=admin_headers(),
        json={
            "name": "Demo Store",
            "address_street": "123 Main St",
            "address_city": "Springfield",
            "address_state": "IL",
            "address_postal_code": "62701",
            "direct_connect_host": "10.20.30.40",
            "network_status_notes": "The rest of the boxes on these two networks are online as well.",
        },
    )

    response = client.get("/api/ui/gateways", headers=admin_headers())
    gateway = response.json()[0]

    assert response.status_code == 200
    assert gateway["site_name"] == "Demo Store"
    assert gateway["site_address_street"] == "123 Main St"
    assert gateway["site_address_city"] == "Springfield"
    assert gateway["site_address_state"] == "IL"
    assert gateway["site_address_postal_code"] == "62701"
    assert gateway["site_compact_address"] == "123 Main St, Springfield, IL 62701"
    assert gateway["direct_connect_available"] is True
    assert gateway["direct_connect_host"] == "10.20.30.40"
    assert gateway["direct_connect_port"] == 5002
    assert gateway["network_status_notes"] == "The rest of the boxes on these two networks are online as well."


def test_gateway_detail_site_endpoint_includes_site_info() -> None:
    create_gateway_token("GW001")
    client.patch(
        "/api/ui/gateways/GW001/site",
        headers=admin_headers(),
        json={
            "name": "Demo Store",
            "address": "Legacy full address",
            "address_street": "123 Main St",
            "address_city": "Springfield",
            "address_state": "IL",
            "address_postal_code": "62701",
        },
    )

    response = client.get("/api/ui/gateways/GW001/site", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["name"] == "Demo Store"
    assert response.json()["address"] == "Legacy full address"
    assert response.json()["address_street"] == "123 Main St"
    assert response.json()["address_city"] == "Springfield"
    assert response.json()["address_state"] == "IL"
    assert response.json()["address_postal_code"] == "62701"


def test_direct_connect_hidden_when_not_configured() -> None:
    create_gateway_token("GW001")

    response = client.get("/api/ui/gateways/GW001/direct-connect", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["url"] is None
    assert response.json()["reason"] == "Direct connect is not configured for this site or gateway."


def test_direct_connect_url_generated_when_configured() -> None:
    create_gateway_token("GW001")
    client.patch(
        "/api/ui/sites/demo-site",
        headers=admin_headers(),
        json={"direct_connect_host": "10.20.30.40", "direct_connect_port": 5002},
    )

    response = client.get("/api/ui/gateways/GW001/direct-connect", headers=admin_headers())

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "url": "http://10.20.30.40:5002",
        "host": "10.20.30.40",
        "port": 5002,
        "label": "Direct Connect",
        "reason": None,
    }


def test_direct_connect_rejects_unsafe_host_and_port() -> None:
    unsafe_host = client.patch(
        "/api/ui/sites/demo-site",
        headers=admin_headers(),
        json={"direct_connect_host": "javascript:alert(1)"},
    )
    unsafe_path = client.patch(
        "/api/ui/sites/demo-site",
        headers=admin_headers(),
        json={"direct_connect_host": "10.20.30.40/path"},
    )
    unsafe_port = client.patch(
        "/api/ui/sites/demo-site",
        headers=admin_headers(),
        json={"direct_connect_port": 70000},
    )

    assert unsafe_host.status_code == 422
    assert unsafe_path.status_code == 422
    assert unsafe_port.status_code == 422


def test_tunnel_status_remains_friendly_when_disconnected() -> None:
    create_gateway_token("GW001")

    response = client.get("/api/ui/gateways/GW001/tunnel-status", headers=admin_headers())

    assert response.status_code == 200
    assert response.json() == {"connected": False, "status": "not_connected"}


def test_gateway_tunnel_registration_requires_matching_gateway_token() -> None:
    raw_token = create_gateway_token("GW002", token_prefix="gw00201")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/edge/tunnels/GW001", headers=auth_headers(raw_token)):
            pass


def test_gateway_tunnel_registration_updates_status() -> None:
    raw_token = create_gateway_token("GW001")

    with client.websocket_connect("/api/edge/tunnels/GW001", headers=auth_headers(raw_token)):
        connected = client.get("/api/ui/gateways/GW001/tunnel-status", headers=admin_headers())
        assert connected.status_code == 200
        assert connected.json() == {"connected": True, "status": "connected"}

    disconnected = client.get("/api/ui/gateways/GW001/tunnel-status", headers=admin_headers())
    assert disconnected.status_code == 200
    assert disconnected.json() == {"connected": False, "status": "not_connected"}


def test_tunnel_proxy_relays_for_operator_without_forwarding_browser_auth() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager

    create_gateway_token("GW001")
    operator_id = create_operator_user("operator@example.com", role="operator", status="active")

    class FakeTunnel:
        async def request(self, **kwargs):
            assert kwargs["method"] == "GET"
            assert kwargs["path"] == "/status"
            assert kwargs["query_string"] == "tab=network"
            assert "authorization" not in {key.lower() for key in kwargs["headers"]}
            assert "cookie" not in {key.lower() for key in kwargs["headers"]}
            return TunnelResponse(status_code=200, headers={"content-type": "text/html"}, body=b"<html>gateway ui</html>")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        response = client.get(
            "/gateways/GW001/tunnel/proxy/status?tab=network",
            headers=user_headers("operator@example.com", operator_id),
        )
    finally:
        tunnel_manager._tunnels.pop("GW001", None)

    assert response.status_code == 200
    assert response.text == "<html>gateway ui</html>"
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    ("upstream_location", "rewritten_location"),
    [
        ("http://127.0.0.1:5000/login?next=%2F", "/gateways/GW001/tunnel/proxy/login?next=%2F"),
        ("http://localhost:5000/login?next=%2F", "/gateways/GW001/tunnel/proxy/login?next=%2F"),
        ("/login?next=%2F", "/gateways/GW001/tunnel/proxy/login?next=%2F"),
    ],
)
def test_tunnel_proxy_rewrites_safe_gateway_local_redirects(upstream_location: str, rewritten_location: str) -> None:
    from app.tunnel import TunnelResponse, tunnel_manager

    create_gateway_token("GW001")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=302, headers={"Location": upstream_location}, body=b"")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        response = client.get("/gateways/GW001/tunnel/proxy/", headers=admin_headers(), follow_redirects=False)
    finally:
        tunnel_manager._tunnels.pop("GW001", None)

    assert response.status_code == 302
    assert response.headers["location"] == rewritten_location


def test_tunnel_proxy_rejects_external_redirect_location() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager

    create_gateway_token("GW001")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=302, headers={"Location": "https://example.com/login"}, body=b"")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        response = client.get("/gateways/GW001/tunnel/proxy/", headers=admin_headers(), follow_redirects=False)
    finally:
        tunnel_manager._tunnels.pop("GW001", None)

    assert response.status_code == 502
    assert response.json() == {"detail": "Gateway tunnel redirect target is not allowlisted"}


def test_operator_can_create_tunnel_session_when_connected() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    operator_id = create_operator_user("operator@example.com", role="operator", status="active")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=200, headers={"content-type": "text/html"}, body=b"gateway")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        response = client.post(
            "/api/ui/gateways/GW001/tunnel-session",
            headers=user_headers("operator@example.com", operator_id),
        )
    finally:
        tunnel_manager._tunnels.pop("GW001", None)

    assert response.status_code == 200
    body = response.json()
    assert body["url"].startswith("/gateways/GW001/tunnel/session/")
    session_id = body["url"].rstrip("/").split("/")[-1]
    tunnel_session_manager._sessions.pop(session_id, None)


def test_tunnel_session_creation_requires_connected_tunnel() -> None:
    create_gateway_token("GW001")

    response = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "Gateway tunnel is not connected"}


def test_viewer_cannot_create_tunnel_session() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager

    create_gateway_token("GW001")
    viewer_id = create_operator_user("viewer@example.com", role="viewer", status="active")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=200, headers={}, body=b"")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        response = client.post("/api/ui/gateways/GW001/tunnel-session", headers=user_headers("viewer@example.com", viewer_id))
    finally:
        tunnel_manager._tunnels.pop("GW001", None)

    assert response.status_code == 403
    assert response.json()["detail"] == "Operator role required"


def test_tunnel_session_url_expires() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    original_ttl = tunnel_session_manager.ttl_seconds

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=200, headers={}, body=b"")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    tunnel_session_manager.ttl_seconds = -1
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        response = client.get(created.json()["url"], follow_redirects=False)
    finally:
        tunnel_session_manager.ttl_seconds = original_ttl
        tunnel_manager._tunnels.pop("GW001", None)

    assert response.status_code == 403
    assert response.json() == {"detail": "Tunnel console session is not valid"}


def test_tunnel_session_url_is_gateway_scoped() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    create_gateway_token("GW002", token_prefix="gw00202")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=200, headers={}, body=b"")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    tunnel_manager._tunnels["GW002"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_id = created.json()["url"].rstrip("/").split("/")[-1]
        response = client.get(f"/gateways/GW002/tunnel/session/{session_id}/", follow_redirects=False)
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_manager._tunnels.pop("GW002", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    assert response.status_code == 403
    assert response.json() == {"detail": "Tunnel console session is not valid"}


def test_tunnel_session_rewrites_redirects_into_session_path() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=302, headers={"Location": "http://127.0.0.1:5000/login?next=%2F"}, body=b"")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.get(session_url, follow_redirects=False)
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    assert response.status_code == 302
    assert response.headers["location"] == f"/gateways/GW001/tunnel/session/{session_id}/login?next=%2F"


def test_tunnel_session_proxies_post_and_scopes_cookie_path() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    captured: dict[str, object] = {}

    class FakeTunnel:
        async def request(self, **kwargs):
            captured.update(kwargs)
            return TunnelResponse(status_code=200, headers={"Set-Cookie": "sid=abc; Path=/; HttpOnly"}, body=b"ok")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.post(f"{session_url}login", content=b"u=demo", headers={"Cookie": "sid=abc"})
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    assert response.status_code == 200
    assert captured["method"] == "POST"
    assert captured["path"] == "/login"
    assert captured["body"] == b"u=demo"
    assert captured["headers"]["cookie"] == "sid=abc"
    assert response.headers["set-cookie"] == f"sid=abc; Path=/gateways/GW001/tunnel/session/{session_id}/; HttpOnly"


def test_tunnel_console_direct_navigation_renders_friendly_shell() -> None:
    create_gateway_token("GW001")

    response = client.get("/gateways/GW001/tunnel/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Remote Console" in response.text
    assert "Gateway tunnel is not connected" in response.text
    assert "Direct Connect" in response.text
    assert "Heartbeat and job polling" in response.text
    assert "Missing admin credentials" not in response.text
    assert "initTunnelConsole" in response.text
    assert "/tunnel-session" in response.text
    assert "window.open(session.url" in response.text
    assert 'id="tunnel-frame"' not in response.text


def test_tunnel_proxy_requires_operator_auth() -> None:
    create_gateway_token("GW001")

    response = client.get("/gateways/GW001/tunnel/proxy/")

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing admin credentials"


def test_tunnel_proxy_blocks_viewer_role() -> None:
    create_gateway_token("GW001")
    viewer_id = create_operator_user("viewer@example.com", role="viewer", status="active")

    response = client.get("/gateways/GW001/tunnel/proxy/", headers=user_headers("viewer@example.com", viewer_id))

    assert response.status_code == 403
    assert response.json()["detail"] == "Operator role required"


def test_tunnel_proxy_returns_friendly_disconnected_for_adminbearer() -> None:
    create_gateway_token("GW001")

    response = client.get("/gateways/GW001/tunnel/proxy/", headers=admin_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "Gateway tunnel is not connected"}


def test_tunnel_proxy_returns_friendly_disconnected_for_operator_user() -> None:
    create_gateway_token("GW001")
    operator_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.get("/gateways/GW001/tunnel/proxy/", headers=user_headers("operator@example.com", operator_id))

    assert response.status_code == 503
    assert response.json() == {"detail": "Gateway tunnel is not connected"}


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


def test_register_operator_profile_rejects_invalid_jwt() -> None:
    response = client.post("/api/auth/register", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid admin credentials"


def test_register_operator_profile_rejects_wrong_audience() -> None:
    token = supabase_user_token("operator@example.com", audience="wrong-audience")

    response = client.post("/api/auth/register", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid admin credentials"


def test_register_operator_profile_creates_pending_user() -> None:
    user_id = str(uuid4())
    response = client.post("/api/auth/register", headers=user_headers("NewUser@Example.com", user_id))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "newuser@example.com"
    assert body["role"] == "pending"
    assert body["status"] == "pending"
    assert body["supabase_user_id"] == user_id


def test_register_operator_profile_accepts_rs256_jwks_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.auth as auth_module

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    expected_jwks_url = "https://project-ref.supabase.co/auth/v1/.well-known/jwks.json"

    class FakeSigningKey:
        key = public_key

    class FakeJWKClient:
        def __init__(self, url: str) -> None:
            assert url == expected_jwks_url

        def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
            header = jwt.get_unverified_header(token)
            assert header["alg"] == "RS256"
            assert header["kid"] == "test-rs256-key"
            return FakeSigningKey()

    monkeypatch.setattr(auth_module.settings, "supabase_url", "https://project-ref.supabase.co")
    monkeypatch.setattr(auth_module.settings, "supabase_jwks_url", None)
    monkeypatch.setattr(auth_module, "PyJWKClient", FakeJWKClient)

    user_id = str(uuid4())
    token = supabase_rs256_user_token(private_key, email="rs256@example.com", user_id=user_id)

    response = client.post("/api/auth/register", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "rs256@example.com"
    assert response.json()["supabase_user_id"] == user_id


def test_operator_route_rejects_valid_jwt_without_role_lookup() -> None:
    response = client.get("/api/edge/gateways", headers=user_headers("unknown@example.com"))

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid admin credentials"


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


def test_ui_gateway_status_marks_recent_heartbeat_online() -> None:
    create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=30)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.get("/api/ui/gateways", headers=user_headers("operator@example.com", user_id))

    assert response.status_code == 200
    gateway = response.json()[0]
    assert gateway["gateway_id"] == "GW001"
    assert gateway["effective_status"] == "online"
    assert gateway["is_online"] is True
    assert gateway["bacnet_port"] == 47814


def test_ui_gateway_status_marks_old_heartbeat_stale() -> None:
    create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=600)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.get("/api/ui/gateways", headers=user_headers("operator@example.com", user_id))

    assert response.status_code == 200
    assert response.json()[0]["effective_status"] == "stale"


def test_ui_gateway_status_marks_missing_or_expired_heartbeat_offline() -> None:
    create_gateway_token("GW001")
    create_gateway_token("GW002", token_prefix="gw00202")
    set_gateway_heartbeat("GW001", seconds_ago=None)
    set_gateway_heartbeat("GW002", seconds_ago=3600)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.get("/api/ui/gateways", headers=user_headers("operator@example.com", user_id))

    assert response.status_code == 200
    statuses = {gateway["gateway_id"]: gateway["effective_status"] for gateway in response.json()}
    assert statuses == {"GW001": "offline", "GW002": "offline"}


def test_ui_gateway_summary_counts_online_stale_offline() -> None:
    create_gateway_token("GW001")
    create_gateway_token("GW002", token_prefix="gw00202")
    create_gateway_token("GW003", token_prefix="gw00303")
    set_gateway_heartbeat("GW001", seconds_ago=20)
    set_gateway_heartbeat("GW002", seconds_ago=600)
    set_gateway_heartbeat("GW003", seconds_ago=3600)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.get("/api/ui/gateways/summary", headers=user_headers("operator@example.com", user_id))

    assert response.status_code == 200
    assert response.json() == {"total": 3, "online": 1, "stale": 1, "offline": 1}


def test_ui_gateway_read_routes_allow_viewer() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("viewer@example.com", role="viewer", status="active")

    response = client.get("/api/ui/gateways", headers=user_headers("viewer@example.com", user_id))

    assert response.status_code == 200
    assert response.json()[0]["gateway_id"] == "GW001"


def test_ui_gateway_routes_block_pending_and_disabled_users() -> None:
    create_gateway_token("GW001")
    pending_id = create_operator_user("pending@example.com", role="pending", status="pending")
    disabled_id = create_operator_user("disabled@example.com", role="operator", status="disabled")

    pending_response = client.get("/api/ui/gateways", headers=user_headers("pending@example.com", pending_id))
    disabled_response = client.get("/api/ui/gateways", headers=user_headers("disabled@example.com", disabled_id))

    assert pending_response.status_code == 401
    assert disabled_response.status_code == 401


def test_ui_tree_write_routes_reject_viewer() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("viewer@example.com", role="viewer", status="active")

    group_response = client.post(
        "/api/ui/gateways/GW001/groups",
        headers=user_headers("viewer@example.com", user_id),
        json={"name": "Plant Floor"},
    )
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=user_headers("viewer@example.com", user_id),
        json={"device_instance": 1001},
    )
    import_response = client.post(
        "/api/ui/gateways/GW001/commissioning-template/import",
        headers=user_headers("viewer@example.com", user_id),
        json={"devices": [{"device_id": "1001", "points": []}]},
    )

    assert group_response.status_code == 403
    assert device_response.status_code == 403
    assert import_response.status_code == 403


def test_ui_gateway_tree_can_store_group_device_and_point() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)

    group_response = client.post("/api/ui/gateways/GW001/groups", headers=headers, json={"name": "Plant Floor"})
    assert group_response.status_code == 200
    group_id = group_response.json()["id"]

    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={
            "group_id": group_id,
            "device_instance": 1001,
            "device_name": "AHU-1",
            "vendor_name": "Test Vendor",
        },
    )
    assert device_response.status_code == 200
    device_id = device_response.json()["id"]

    point_response = client.post(
        f"/api/ui/devices/{device_id}/points",
        headers=headers,
        json={
            "object_type": "analog-input",
            "object_instance": 1,
            "object_name": "Space Temp",
            "property": "present-value",
            "present_value": "72.0",
            "units": "degF",
        },
    )
    tree_response = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert point_response.status_code == 200
    assert tree_response.status_code == 200
    tree = tree_response.json()
    assert tree["groups"][0]["name"] == "Plant Floor"
    assert tree["devices"][0]["device_instance"] == 1001
    assert tree["points"][0]["object_type"] == "analog-input"
    assert tree["points"][0]["property"] == "present-value"


def test_ui_operator_can_import_edge_commissioning_template() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    template = {
        "schema_version": "iot-cx-commissioning-template/v1",
        "source": "edge-bacnet-ui-v2",
        "gateway_id": "GW001",
        "groups": [{"name": "HVAC"}],
        "devices": [
            {
                "device_id": "1001",
                "device_name": "AHU-1",
                "vendor": "Test Vendor",
                "network_number": 2001,
                "mac": "C0:A8:01:66:BA:C6 sadr 01",
                "group_name": "HVAC",
                "points": [
                    {"object_type": "analog-input", "instance": 1, "object_name": "Space Temp"},
                    {"object_type": "binary-output", "instance": 7, "object_name": "Fan Command"},
                ],
            }
        ],
    }

    import_response = client.post("/api/ui/gateways/GW001/commissioning-template/import", headers=headers, json=template)
    second_import_response = client.post("/api/ui/gateways/GW001/commissioning-template/import", headers=headers, json=template)
    tree_response = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert import_response.status_code == 200
    assert import_response.json()["created_groups"] == 1
    assert import_response.json()["created_devices"] == 1
    assert import_response.json()["created_points"] == 2
    assert second_import_response.status_code == 200
    assert second_import_response.json()["created_devices"] == 0
    assert second_import_response.json()["updated_devices"] == 1
    assert second_import_response.json()["created_points"] == 0
    assert second_import_response.json()["updated_points"] == 2
    tree = tree_response.json()
    assert tree["groups"][0]["name"] == "HVAC"
    assert tree["devices"][0]["device_instance"] == 1001
    assert tree["devices"][0]["device_name"] == "AHU-1"
    assert tree["devices"][0]["vendor_name"] == "Test Vendor"
    assert tree["points"][0]["object_type"] == "analog-input"
    assert tree["points"][0]["object_instance"] == 1
    assert tree["points"][1]["object_type"] == "binary-output"


def test_ui_import_template_rejects_gateway_mismatch() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)

    response = client.post(
        "/api/ui/gateways/GW001/commissioning-template/import",
        headers=headers,
        json={"gateway_id": "GW002", "devices": [{"device_id": "1001", "points": []}]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Template gateway_id does not match target gateway"


def test_ui_duplicate_group_returns_clean_json_error() -> None:
    create_gateway_token("GW777")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)

    first_response = client.post("/api/ui/gateways/GW777/groups", headers=headers, json={"name": "HVAC"})
    duplicate_response = client.post("/api/ui/gateways/GW777/groups", headers=headers, json={"name": "HVAC"})

    assert first_response.status_code == 200
    assert duplicate_response.status_code == 409
    assert duplicate_response.headers["content-type"].startswith("application/json")
    assert duplicate_response.json()["detail"] == "Group already exists for this gateway"
    assert "Internal Server Error" not in duplicate_response.text


def test_ui_duplicate_device_returns_clean_json_error() -> None:
    create_gateway_token("GW777")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    device_payload = {
        "device_instance": 1,
        "device_name": "Device 1",
        "network_number": 2001,
        "mac_address": "C0:A8:01:66:BA:C6 sadr 01",
    }

    first_response = client.post("/api/ui/gateways/GW777/devices", headers=headers, json=device_payload)
    duplicate_response = client.post("/api/ui/gateways/GW777/devices", headers=headers, json=device_payload)

    assert first_response.status_code == 200
    assert duplicate_response.status_code == 409
    assert duplicate_response.headers["content-type"].startswith("application/json")
    assert duplicate_response.json()["detail"] == "Device already exists for this gateway"
    assert "Internal Server Error" not in duplicate_response.text


def test_ui_operator_can_soft_remove_point_from_tree() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={"device_instance": 1001, "device_name": "AHU-1"},
    )
    point_response = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/points",
        headers=headers,
        json={"object_type": "binary-input", "object_instance": 3, "object_name": "Bypass Open"},
    )

    remove_response = client.delete(f"/api/ui/points/{point_response.json()['id']}", headers=headers)
    tree_response = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert remove_response.status_code == 200
    assert remove_response.json()["enabled"] is False
    assert tree_response.status_code == 200
    assert tree_response.json()["points"] == []


def test_ui_operator_can_bulk_remove_points_from_tree() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={"device_instance": 1001, "device_name": "AHU-1"},
    )
    first_point = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/points",
        headers=headers,
        json={"object_type": "binary-input", "object_instance": 3, "object_name": "Bypass Open"},
    )
    second_point = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/points",
        headers=headers,
        json={"object_type": "analog-value", "object_instance": 10, "object_name": "Setpoint"},
    )

    remove_response = client.post(
        "/api/ui/points/bulk-remove",
        headers=headers,
        json={"point_ids": [first_point.json()["id"], second_point.json()["id"]]},
    )
    tree_response = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert remove_response.status_code == 200
    assert remove_response.json() == {"requested_count": 2, "removed_count": 2, "missing_ids": []}
    assert tree_response.status_code == 200
    assert tree_response.json()["points"] == []


def test_ui_operator_can_soft_remove_device_from_tree() -> None:
    create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={"device_instance": 1001, "device_name": "AHU-1"},
    )
    point_response = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/points",
        headers=headers,
        json={"object_type": "binary-input", "object_instance": 3, "object_name": "Bypass Open"},
    )

    remove_response = client.delete(f"/api/ui/devices/{device_response.json()['id']}", headers=headers)
    tree_response = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert remove_response.status_code == 200
    assert remove_response.json()["enabled"] is False
    assert tree_response.status_code == 200
    assert tree_response.json()["devices"] == []
    assert tree_response.json()["points"] == []


def test_ui_viewer_cannot_remove_tree_items() -> None:
    create_gateway_token("GW001")
    operator_id = create_operator_user("operator@example.com", role="operator", status="active")
    viewer_id = create_operator_user("viewer@example.com", role="viewer", status="active")
    operator_headers = user_headers("operator@example.com", operator_id)
    viewer_headers = user_headers("viewer@example.com", viewer_id)
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=operator_headers,
        json={"device_instance": 1001, "device_name": "AHU-1"},
    )
    point_response = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/points",
        headers=operator_headers,
        json={"object_type": "binary-input", "object_instance": 3, "object_name": "Bypass Open"},
    )

    device_remove = client.delete(f"/api/ui/devices/{device_response.json()['id']}", headers=viewer_headers)
    point_remove = client.delete(f"/api/ui/points/{point_response.json()['id']}", headers=viewer_headers)
    bulk_remove = client.post(
        "/api/ui/points/bulk-remove",
        headers=viewer_headers,
        json={"point_ids": [point_response.json()["id"]]},
    )

    assert device_remove.status_code == 403
    assert point_remove.status_code == 403
    assert bulk_remove.status_code == 403


def test_ui_device_group_must_belong_to_same_gateway() -> None:
    create_gateway_token("GW001")
    create_gateway_token("GW002", token_prefix="gw00202")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)

    group_response = client.post("/api/ui/gateways/GW001/groups", headers=headers, json={"name": "Plant Floor"})
    response = client.post(
        "/api/ui/gateways/GW002/devices",
        headers=headers,
        json={"group_id": group_response.json()["id"], "device_instance": 1001},
    )

    assert response.status_code == 404


def test_ui_discover_devices_queues_safe_47814_job_for_online_gateway() -> None:
    create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=15)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.post(
        "/api/ui/gateways/GW001/discover-devices",
        headers=user_headers("operator@example.com", user_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["gateway_id"] == "GW001"
    assert body["job_type"] == "bacnet_discover"
    assert body["status"] == "queued"
    assert body["request_json"] == {"bacnet_port": 47814}
    assert "47808" not in response.text


def test_ui_discover_devices_rejects_offline_gateway() -> None:
    create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=3600)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")

    response = client.post(
        "/api/ui/gateways/GW001/discover-devices",
        headers=user_headers("operator@example.com", user_id),
    )

    assert response.status_code == 409


def test_ui_operator_can_queue_point_load_for_saved_device() -> None:
    create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=15)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={"device_instance": 1001, "device_name": "AHU-1"},
    )

    response = client.post(f"/api/ui/devices/{device_response.json()['id']}/load-points", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["gateway_id"] == "GW001"
    assert body["job_type"] == "bacnet_load_points"
    assert body["status"] == "queued"
    assert body["request_json"]["device_instance"] == 1001
    assert body["request_json"]["bacnet_port"] == 47814
    assert body["request_json"]["limit"] == 80
    assert body["request_json"]["name_limit"] == 40
    assert body["request_json"]["include_object_names"] is True
    assert "47808" not in response.text


def test_ui_point_load_rejects_offline_gateway() -> None:
    create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=3600)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={"device_instance": 1001, "device_name": "AHU-1"},
    )

    response = client.post(f"/api/ui/devices/{device_response.json()['id']}/load-points", headers=headers)

    assert response.status_code == 409


def test_ui_viewer_cannot_queue_point_load() -> None:
    create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=15)
    operator_id = create_operator_user("operator@example.com", role="operator", status="active")
    viewer_id = create_operator_user("viewer@example.com", role="viewer", status="active")
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=user_headers("operator@example.com", operator_id),
        json={"device_instance": 1001, "device_name": "AHU-1"},
    )

    response = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/load-points",
        headers=user_headers("viewer@example.com", viewer_id),
    )

    assert response.status_code == 403


def test_job_creation_normalizes_bacnet_load_points_to_47814() -> None:
    response = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "bacnet_load_points", "request": {"device_instance": 1001}},
    )

    assert response.status_code == 200
    assert response.json()["request_json"] == {
        "device_instance": 1001,
        "bacnet_port": 47814,
        "limit": 250,
        "name_limit": 40,
        "include_object_names": True,
    }


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
