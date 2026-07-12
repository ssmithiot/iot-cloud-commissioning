import os
import base64
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.websockets import WebSocketDisconnect
from sqlalchemy import select

EDGE_AGENT_PATH = Path(__file__).resolve().parents[2] / "edge-agent"
if str(EDGE_AGENT_PATH) not in sys.path:
    sys.path.append(str(EDGE_AGENT_PATH))

os.environ["DATABASE_URL"] = "sqlite:///./test-cloud-api.db"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["GATEWAY_AUTH_PEPPER"] = "test-pepper"
os.environ["IOT_ADMIN_API_TOKEN"] = "test-admin-token"
os.environ["SUPABASE_JWT_SECRET"] = "test-supabase-jwt-secret"

from app import main as main_module
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
        "agent_version": "0.1.1",
        "ui_version": "0.1.0",
        "sqlite_db_ok": True,
        "queued_upload_count": 0,
        "cpu_count": 4,
        "cpu_load_1m": 0.5,
        "cpu_load_pct": 12.5,
        "memory_used_pct": 42.0,
        "memory_available_mb": 2048,
        "disk_used_pct": 31.5,
        "disk_free_mb": 16384,
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
        ("/auth/confirm", "Confirm Secure Link"),
        ("/auth/reset-password", "Reset Password"),
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
    assert '<a class="button secondary home-link" href="/app">Home</a>' in response.text


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
    assert 'emailRedirectTo: "http://localhost' not in response.text


def test_protected_ui_contains_unauthenticated_redirect() -> None:
    response = client.get("/app")

    assert response.status_code == 200
    assert 'window.location.assign(statePaths.login)' in response.text
    assert "/api/auth/me" in response.text
    assert "Copyright 2026, The Internet of Team, LLC. All rights reserved." in response.text


def test_dashboard_highlights_online_gateway_status() -> None:
    response = client.get("/app")

    assert response.status_code == 200
    assert ".status-online" in response.text
    assert 'return \'<span class="status-online">ONLINE</span>\';' in response.text
    assert "return escapeHtml(statusLabel(gateway));" in response.text
    assert '<td><span class="status-text">${dashboardStatusCell(gateway)}</span></td>' in response.text
    assert 'id="gateway-heartbeat-trend"' in response.text
    assert "loadGatewayHeartbeatTrend(gateway);" in response.text
    assert "heartbeat-trend-bars" in response.text


def test_dashboard_gateway_table_supports_search_and_sort() -> None:
    response = client.get("/app")

    assert response.status_code == 200
    assert 'id="gateway-search"' in response.text
    assert "setupGatewaySearch" in response.text
    assert "gatewaySearchText" in response.text
    assert "sortedDashboardGateways" in response.text
    assert 'data-sort="gateway_id"' in response.text
    assert 'data-sort="version"' in response.text
    assert 'data-sort="status"' in response.text
    assert "edgeAppVersion(gateway)" in response.text
    assert 'id="select-all-gateway-updates"' in response.text
    assert 'id="update-selected-gateways"' in response.text
    assert 'data-select-update="${escapeHtml(gateway.gateway_id)}"' in response.text
    assert "queueGatewayUpdates" in response.text
    assert 'const edgeResourceHealthMinimumVersion = "0.1.4";' in response.text
    assert "return !versionAtLeast(gateway.agent_version, edgeResourceHealthMinimumVersion);" in response.text
    assert 'version.toLowerCase() !== "current"' in response.text
    assert 'data-sort="version">Edge App</button>' in response.text
    assert '<td>${gatewayVersionCell(gateway)}</td>' in response.text
    assert 'colspan="10"' in response.text
    assert "direction: dashboardSort.direction === \"asc\" ? \"desc\" : \"asc\"" in response.text


def test_gateway_workspace_contains_discovery_progress_ui() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert 'id="discovery-progress"' in response.text
    assert 'id="discovered-devices"' in response.text
    assert 'class="tree-shell point-workbench"' in response.text
    assert 'id="tree-details"' in response.text
    assert "renderDiscoveredDevices" in response.text
    assert "Load points" not in response.text
    assert "Saved Tree" in response.text
    assert "Imported Commissioning Model" in response.text
    assert "Last Import" in response.text
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
    assert "Remove selected" in response.text
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
    assert 'id="edge-health-cpu"' in response.text
    assert 'id="edge-health-memory"' in response.text
    assert "renderGatewayResourceHealth(gateway);" in response.text


def test_gateway_workspace_trend_chart_tracks_resized_detail_pane() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert "function renderResponsivePointTrend" in response.text
    assert '"ResizeObserver" in window' in response.text
    assert "svg.viewBox.baseVal.width" in response.text
    assert 'renderResponsivePointTrend(chart, samples, point.units || "");' in response.text
    assert '--trend-plot-fill: #fff;' in response.text
    assert 'font: 13px/1 "JetBrains Mono", Consolas, monospace;' in response.text


def test_gateway_workspace_stacks_trends_for_selected_points() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert "renderSelectedPointTrends(selected);" in response.text
    assert 'class="point-trend-list"' in response.text
    assert 'class="point-trend-card"' in response.text
    assert "loadSelectedPointTrend(card, points[index], chartRange);" in response.text
    assert "pointTrendResizeObservers" in response.text
    assert "trendCardControls(point)" in response.text
    assert 'class="trend-chart-size"' in response.text
    assert 'class="trend-chart-theme"' in response.text
    assert "trendChartThemeStorageKey" in response.text
    assert "trendChartRangeStorageKey" in response.text
    assert 'class="trend-chart-range"' in response.text
    assert 'class="trend-card-header"' in response.text
    assert 'class="trend-card-summary"' in response.text
    assert "trendSummary(samples, point.units || \"\")" in response.text
    assert "trend-edit-button" in response.text
    assert "disable-point-trend" in response.text
    assert "updatePointTrend(point, false" in response.text


def test_gateway_workspace_defaults_devices_and_object_folders_to_collapsed() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert "function addCollapsible(parent, row, children, onSelect = null, expanded = true)" in response.text
    assert "childWrap.hidden = !expanded;" in response.text
    assert 'treeRow("device", deviceLabel, device.network_number ? `network ${device.network_number}` : "", depth, false)' in response.text
    assert 'treeRow("folder", folderLabel, `${folderPoints.length}`, depth + 1, false)' in response.text
    assert 'body[data-page="gateway-workspace"] .tree-row .twisty' in response.text


def test_gateway_workspace_formats_present_value_and_shows_active_priority() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert 'label: "Present Value (Property 85)"' in response.text
    assert "function formatPresentValue(value)" in response.text
    assert "return formatPresentValue(point.present_value);" in response.text
    assert "function pointTableCellHtml(point, key)" in response.text
    assert 'point.active_priority == null' in response.text
    assert 'class="point-active-priority"' in response.text
    assert '@${escapeHtml(point.active_priority)}' in response.text
    assert 'present_value: point.present_value == null ? null : String(point.present_value)' in response.text


def test_gateway_workspace_supports_hierarchical_point_selection() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert "function attachSavedBranchSelector(row, pointIds, label)" in response.text
    assert "function syncSavedTreeSelection()" in response.text
    assert "checkbox.indeterminate = selectedCount > 0 && selectedCount < pointIds.length;" in response.text
    assert "attachSavedBranchSelector(row, points.map((point) => point.id), deviceLabel);" in response.text
    assert "attachSavedBranchSelector(folderRow, folderPoints.map((point) => point.id)" in response.text
    assert "attachSavedBranchSelector(row, groupedPointIds, group.name);" in response.text
    assert 'row.setAttribute("aria-expanded", String(expanded));' in response.text
    assert '!["Enter", " "].includes(event.key)' in response.text
    assert 'window.confirm(`Remove ${selected.length} selected saved point${selected.length === 1 ? "" : "s"} from the tree?`)' in response.text
    assert 'id="select-all-saved-points"' not in response.text
    assert 'id="clear-saved-point-selection"' not in response.text


def test_gateway_workspace_applies_single_source_template_to_existing_target_devices() -> None:
    response = client.get("/gateways/GW777")

    assert response.status_code == 200
    assert 'id="template-device-preview"' in response.text
    assert 'id="template-device-tree"' in response.text
    assert 'id="template-source-summary"' in response.text
    assert 'data-role="template-target-group-select"' in response.text
    assert 'data-role="template-target-device-select"' in response.text
    assert '.template-group-row,\n    .template-device-row {\n      grid-template-columns: 18px 18px 18px minmax(0, 1fr) auto;' in response.text
    assert "const targetDevices = currentGatewayTree?.devices || [];" in response.text
    assert "selectedImportTargetDeviceIds = new Set();" in response.text
    assert 'template.devices.length !== 1' in response.text
    assert 'Template must contain exactly one source device.' in response.text
    assert 'byId("template-file").addEventListener("change", () => loadTemplateImportPreview(gatewayId));' in response.text
    assert "const selectedTargets = (currentGatewayTree?.devices || []).filter" in response.text
    assert "gateway_id: gatewayId" in response.text
    assert "groups: []" in response.text
    assert "devices: selectedTargets.map((target)" in response.text
    assert "device_instance: target.device_instance" in response.text
    assert "points: sourcePoints.map((point) => ({ ...point }))" in response.text
    assert "body: JSON.stringify(selectedTemplate)" in response.text
    assert "const sourceDeviceIds = new Set();" in response.text
    assert "sourceDeviceIds.size > 1" in response.text
    assert 'devices: [{ device_instance: 0, device_name: "CSV point template", points }]' in response.text
    assert 'currentGatewayTree?.devices?.[0]?.device_instance' not in response.text
    assert 'id="csv-device-instance"' not in response.text


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
    assert gateways.json()[0]["agent_version"] == "0.1.1"
    assert gateways.json()[0]["cpu_load_pct"] == 12.5
    assert gateways.json()[0]["memory_available_mb"] == 2048


def test_ui_gateway_heartbeat_trend_returns_ordered_history() -> None:
    raw_token = create_gateway_token("GW001")
    first = heartbeat_payload("GW001")
    first["timestamp_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    first["queued_upload_count"] = 3
    second = heartbeat_payload("GW001")
    second["timestamp_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    second["sqlite_db_ok"] = False

    assert client.post("/api/edge/heartbeat", headers=auth_headers(raw_token), json=first).status_code == 200
    assert client.post("/api/edge/heartbeat", headers=auth_headers(raw_token), json=second).status_code == 200
    response = client.get("/api/ui/gateways/GW001/heartbeat-trend?limit=1", headers=admin_headers())

    assert response.status_code == 200
    assert response.json() == [
        {
            "timestamp_utc": second["timestamp_utc"].replace("+00:00", ""),
            "received_at": response.json()[0]["received_at"],
            "status": "degraded",
            "sqlite_db_ok": False,
            "queued_upload_count": 0,
            "cpu_load_pct": 12.5,
            "memory_used_pct": 42.0,
            "disk_used_pct": 31.5,
            "agent_version": "0.1.1",
            "ui_version": "0.1.0",
        }
    ]


def test_gateway_update_request_queue_claim_and_completion() -> None:
    create_gateway_token("GW001")

    queued = client.post(
        "/api/ui/gateway-updates",
        headers=admin_headers(),
        json={"gateway_ids": ["GW001"]},
    )

    assert queued.status_code == 200
    request = queued.json()[0]
    assert request["gateway_id"] == "GW001"
    assert request["status"] == "queued"

    listed = client.get("/api/admin/gateway-updates", headers=admin_headers())
    assert listed.status_code == 200
    assert listed.json()[0]["request_id"] == request["request_id"]

    claimed = client.post(f"/api/admin/gateway-updates/{request['request_id']}/claim", headers=admin_headers())
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "running"

    completed = client.post(
        f"/api/admin/gateway-updates/{request['request_id']}/complete",
        headers=admin_headers(),
        json={"status": "completed"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"


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
            "latitude": 39.7817,
            "longitude": -89.6501,
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
    assert response.json()["latitude"] == 39.7817
    assert response.json()["longitude"] == -89.6501
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
            "latitude": 39.7817,
            "longitude": -89.6501,
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
    assert gateway["site_latitude"] == 39.7817
    assert gateway["site_longitude"] == -89.6501
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
            "latitude": 39.7817,
            "longitude": -89.6501,
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
    assert response.json()["latitude"] == 39.7817
    assert response.json()["longitude"] == -89.6501


def test_gateway_weather_fetches_and_caches_open_meteo(monkeypatch: pytest.MonkeyPatch) -> None:
    create_gateway_token("GW001")
    client.patch(
        "/api/ui/gateways/GW001/site",
        headers=admin_headers(),
        json={"latitude": 39.7817, "longitude": -89.6501},
    )
    observed: list[tuple[float, float]] = []

    def fake_weather(latitude: float, longitude: float) -> dict[str, object]:
        observed.append((latitude, longitude))
        return {
            "timezone": "America/Chicago",
            "timezone_abbreviation": "CDT",
            "utc_offset_seconds": -18000,
            "current": {
                "time": "2026-07-09T14:00",
                "temperature_2m": 82.4,
                "apparent_temperature": 85.1,
                "relative_humidity_2m": 58,
                "precipitation": 0.0,
                "weather_code": 2,
                "wind_speed_10m": 7.6,
            },
            "daily": {
                "time": ["2026-07-09"],
                "sunrise": ["2026-07-09T05:42"],
                "sunset": ["2026-07-09T20:28"],
            },
        }

    monkeypatch.setattr(main_module, "_fetch_open_meteo_weather", fake_weather)

    first = client.get("/api/ui/gateways/GW001/weather", headers=admin_headers())
    second = client.get("/api/ui/gateways/GW001/weather", headers=admin_headers())

    assert first.status_code == 200
    assert first.json()["available"] is True
    assert first.json()["provider"] == "open-meteo"
    assert first.json()["temperature_f"] == 82.4
    assert first.json()["condition"] == "Partly cloudy"
    assert first.json()["timezone_abbreviation"] == "CDT"
    assert first.json()["sunrise_at"].startswith("2026-07-09T10:42:00")
    assert first.json()["sunset_at"].startswith("2026-07-10T01:28:00")
    assert first.json()["solar_noon_at"].startswith("2026-07-09T18:05:00")
    assert second.status_code == 200
    assert len(observed) == 1


def test_gateway_weather_requires_site_coordinates() -> None:
    create_gateway_token("GW001")

    response = client.get("/api/ui/gateways/GW001/weather", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "latitude and longitude" in response.json()["reason"]


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


def test_tunnel_session_login_post_preserves_form_cookie_and_rewrites_next_redirect(caplog) -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    captured: dict[str, object] = {}

    class FakeTunnel:
        async def request(self, **kwargs):
            captured.update(kwargs)
            return TunnelResponse(
                status_code=302,
                headers={
                    "Location": "/devices",
                    "Set-Cookie": "session=gateway-session; Path=/; HttpOnly; SameSite=Lax",
                },
                body=b"",
            )

    caplog.set_level(logging.INFO, logger="iot-cloud-api.tunnel")
    body = b"username=local-admin&password=secret-password"
    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.post(
            f"{session_url}login?next=%2Fdevices",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "session=old-gateway-session",
            },
            follow_redirects=False,
        )
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    assert response.status_code == 302
    assert captured["method"] == "POST"
    assert captured["path"] == "/login"
    assert captured["query_string"] == "next=%2Fdevices"
    assert captured["body"] == body
    assert captured["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert captured["headers"]["cookie"] == "session=old-gateway-session"
    assert response.headers["location"] == f"/gateways/GW001/tunnel/session/{session_id}/devices"
    assert response.headers["set-cookie"] == (
        f"session=gateway-session; Path=/gateways/GW001/tunnel/session/{session_id}/; HttpOnly; SameSite=Lax"
    )
    log_text = caplog.text
    assert "method=POST" in log_text
    assert "path=/login" in log_text
    assert "query_keys=next" in log_text
    assert f"body_bytes={len(body)}" in log_text
    assert "inbound_cookie_names=session" in log_text
    assert "inbound_cookie_count=1" in log_text
    assert "forwarded_cookie_names=session" in log_text
    assert "forwarded_cookie_count=1" in log_text
    assert "set_cookie_received=True" in log_text
    assert "set_cookie_forwarded=True" in log_text
    assert "secret-password" not in log_text
    assert "old-gateway-session" not in log_text
    assert "gateway-session" not in log_text


def test_tunnel_session_redirected_get_forwards_gateway_cookie_and_devices_path(caplog) -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    captured: dict[str, object] = {}

    class FakeTunnel:
        async def request(self, **kwargs):
            captured.update(kwargs)
            return TunnelResponse(status_code=200, headers={"content-type": "text/html"}, body=b"devices")

    caplog.set_level(logging.INFO, logger="iot-cloud-api.tunnel")
    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.get(
            f"{session_url}devices?tab=network",
            headers={"Cookie": "session=gateway-ui-session"},
        )
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    assert response.status_code == 200
    assert response.text == "devices"
    assert captured["method"] == "GET"
    assert captured["path"] == "/devices"
    assert captured["query_string"] == "tab=network"
    assert captured["headers"]["cookie"] == "session=gateway-ui-session"
    log_text = caplog.text
    assert f"inbound_path=/gateways/GW001/tunnel/session/{session_id}/devices" in log_text
    assert "upstream_path=/devices" in log_text
    assert "query_keys=tab" in log_text
    assert "inbound_cookie_names=session" in log_text
    assert "forwarded_cookie_names=session" in log_text
    assert "gateway-ui-session" not in log_text


def test_tunnel_session_deduplicates_cookie_names_before_upstream_forwarding() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    captured: dict[str, object] = {}

    class FakeTunnel:
        async def request(self, **kwargs):
            captured.update(kwargs)
            return TunnelResponse(status_code=200, headers={"content-type": "text/html"}, body=b"devices")

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.get(
            f"{session_url}devices",
            headers={"Cookie": "session=gateway-ui-session; session=cloud-root-session; theme=dark"},
        )
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    assert response.status_code == 200
    assert captured["headers"]["cookie"] == "session=gateway-ui-session; theme=dark"


def test_tunnel_session_login_redirect_rewrite_keeps_session_slash(caplog) -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=302, headers={"Location": "/login?next=%2Fdevices"}, body=b"")

    caplog.set_level(logging.INFO, logger="iot-cloud-api.tunnel")
    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.get(f"{session_url}devices", follow_redirects=False)
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    assert response.status_code == 302
    assert response.headers["location"] == f"/gateways/GW001/tunnel/session/{session_id}/login?next=%2Fdevices"
    assert "upstream_location_shape=relative:/login" in caplog.text
    assert "response_location_session_slash=True" in caplog.text


def test_tunnel_session_login_chain_through_edge_agent_handler(monkeypatch, caplog) -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager
    import requests
    from iot_cx_agent.config import AgentConfig
    from iot_cx_agent.tunnel import handle_tunnel_message

    create_gateway_token("GW777", token_prefix="gw77701")
    valid_gateway_session = "flask-session-value"
    observed_requests: list[dict[str, object]] = []

    def fake_gateway_ui_request(*args: object, **kwargs: object) -> requests.Response:
        method = str(args[0])
        url = str(args[1])
        headers = {str(key).lower(): str(value) for key, value in (kwargs.get("headers") or {}).items()}
        body = kwargs.get("data") or b""
        observed_requests.append({"method": method, "url": url, "headers": headers, "body": body})
        response = requests.Response()
        response.url = url

        if method == "GET" and url == "http://127.0.0.1:5000/devices":
            if headers.get("cookie") == f"session={valid_gateway_session}":
                response.status_code = 200
                response._content = b"devices ok"
                response.headers["content-type"] = "text/html"
            else:
                response.status_code = 302
                response._content = b""
                response.headers["Location"] = "/login?next=%2Fdevices"
            return response

        if method == "POST" and url == "http://127.0.0.1:5000/login?next=%2Fdevices":
            assert body == b"username=admin&password=correct"
            response.status_code = 302
            response._content = b""
            response.headers["Location"] = "/devices"
            response.headers["Set-Cookie"] = f"session={valid_gateway_session}; Path=/; SameSite=Lax; HttpOnly"
            return response

        response.status_code = 404
        response._content = b"not found"
        return response

    monkeypatch.setattr(requests, "request", fake_gateway_ui_request)
    edge_config = AgentConfig(
        gateway_id="GW777",
        site_id="test-site",
        cloud_url="http://testserver",
        local_ui_url="http://127.0.0.1:5000",
        sqlite_path=Path("edge.db"),
    )

    class FakeEdgeTunnel:
        async def request(self, **kwargs):
            message = {
                "type": "request",
                "request_id": uuid4().hex,
                "method": kwargs["method"],
                "path": kwargs["path"],
                "query_string": kwargs["query_string"],
                "headers": kwargs["headers"],
                "body_b64": base64.b64encode(kwargs["body"]).decode("ascii"),
            }
            edge_response = handle_tunnel_message(edge_config, message)
            assert edge_response["type"] == "response"
            return TunnelResponse(
                status_code=int(edge_response["status_code"]),
                headers={str(key): str(value) for key, value in dict(edge_response["headers"]).items()},
                body=base64.b64decode(str(edge_response["body_b64"])),
            )

    caplog.set_level(logging.WARNING, logger="iot-cloud-api.tunnel")
    tunnel_manager._tunnels["GW777"] = FakeEdgeTunnel()
    session_id = ""
    try:
        created = client.post("/api/ui/gateways/GW777/tunnel-session", headers=admin_headers())
        assert created.status_code == 200
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]

        first_devices = client.get(f"{session_url}devices", follow_redirects=False)
        assert first_devices.status_code == 302
        assert first_devices.headers["location"] == f"/gateways/GW777/tunnel/session/{session_id}/login?next=%2Fdevices"

        login = client.post(
            f"{session_url}login?next=%2Fdevices",
            content=b"username=admin&password=correct",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert login.status_code == 302
        assert login.headers["location"] == f"/gateways/GW777/tunnel/session/{session_id}/devices"
        assert login.headers["set-cookie"] == (
            f"session={valid_gateway_session}; Path=/gateways/GW777/tunnel/session/{session_id}/; "
            "SameSite=Lax; HttpOnly"
        )

        redirected_devices = client.get(
            login.headers["location"],
            headers={"Cookie": f"session={valid_gateway_session}"},
            follow_redirects=False,
        )
        assert redirected_devices.status_code == 200
        assert redirected_devices.text == "devices ok"
    finally:
        tunnel_manager._tunnels.pop("GW777", None)
        if session_id:
            tunnel_session_manager._sessions.pop(session_id, None)

    assert observed_requests[0]["method"] == "GET"
    assert observed_requests[0]["url"] == "http://127.0.0.1:5000/devices"
    assert observed_requests[1]["method"] == "POST"
    assert observed_requests[1]["url"] == "http://127.0.0.1:5000/login?next=%2Fdevices"
    assert observed_requests[2]["method"] == "GET"
    assert observed_requests[2]["url"] == "http://127.0.0.1:5000/devices"
    assert observed_requests[2]["headers"]["cookie"] == f"session={valid_gateway_session}"

    log_text = caplog.text
    assert "TUNNEL_PROXY_DEBUG request" in log_text
    assert "TUNNEL_PROXY_DEBUG response" in log_text
    assert "inbound_path=/gateways/GW777/tunnel/session/" in log_text
    assert "upstream_path=/devices" in log_text
    assert "upstream_path=/login" in log_text
    assert "forwarded_cookie_names=session" in log_text
    assert "flask-session-value" not in log_text
    assert "password=correct" not in log_text


def test_tunnel_session_rewrites_html_root_relative_and_gateway_local_urls() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW001")
    html = (
        '<a href="/login?next=%2F">login</a>'
        '<img src="/static/app.css">'
        '<form action="/login" method="post"></form>'
        '<button formaction="/submit">save</button>'
        '<a href="http://127.0.0.1:5000/settings?tab=users">settings</a>'
        '<script src="http://localhost:5000/static/app.js"></script>'
        '<a href="https://example.com/help">external</a>'
        '<a href="#local">anchor</a>'
    )

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=200, headers={"content-type": "text/html; charset=utf-8"}, body=html.encode())

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        created = client.post("/api/ui/gateways/GW001/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.get(session_url)
    finally:
        tunnel_manager._tunnels.pop("GW001", None)
        tunnel_session_manager._sessions.pop(session_id, None)

    prefix = f"/gateways/GW001/tunnel/session/{session_id}"
    assert response.status_code == 200
    assert f'href="{prefix}/login?next=%2F"' in response.text
    assert f'src="{prefix}/static/app.css"' in response.text
    assert f'action="{prefix}/login"' in response.text
    assert f'formaction="{prefix}/submit"' in response.text
    assert f'href="{prefix}/settings?tab=users"' in response.text
    assert f'src="{prefix}/static/app.js"' in response.text
    assert 'href="https://example.com/help"' in response.text
    assert 'href="#local"' in response.text
    assert "window.fetch = function" in response.text
    assert "window.XMLHttpRequest.prototype.open = function" in response.text
    assert f"var tunnelPrefix = \"{prefix}\"" in response.text


def test_tunnel_session_helper_rewrites_root_relative_fetch_url() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"

    rewritten = main_module._rewrite_tunnel_session_root_relative_url("/device-ping/status/abc", prefix)

    assert rewritten == "/gateways/GW777/tunnel/session/session-1/device-ping/status/abc"


def test_tunnel_session_helper_rewrites_root_relative_xhr_url() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"

    rewritten = main_module._rewrite_tunnel_session_root_relative_url("/device-ping/status/abc?poll=1", prefix)

    assert rewritten == "/gateways/GW777/tunnel/session/session-1/device-ping/status/abc?poll=1"


def test_tunnel_session_helper_does_not_double_prefix_urls() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"
    url = "/gateways/GW777/tunnel/session/session-1/device-ping/status/abc"

    assert main_module._rewrite_tunnel_session_root_relative_url(url, prefix) == url


def test_tunnel_session_helper_does_not_rewrite_external_urls() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"

    assert main_module._rewrite_tunnel_session_root_relative_url("https://example.com/api", prefix) == "https://example.com/api"
    assert main_module._rewrite_tunnel_session_root_relative_url("//example.com/api", prefix) == "//example.com/api"


def test_tunnel_session_rewrites_json_device_ping_results_url() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW777", token_prefix="gw77701")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(
                status_code=200,
                headers={"content-type": "application/json", "content-length": "42"},
                body=b'{"results_url":"/device-ping/results/abc"}',
            )

    tunnel_manager._tunnels["GW777"] = FakeTunnel()
    session_id = ""
    try:
        created = client.post("/api/ui/gateways/GW777/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.get(f"{session_url}device-ping/status/abc")
    finally:
        tunnel_manager._tunnels.pop("GW777", None)
        if session_id:
            tunnel_session_manager._sessions.pop(session_id, None)

    prefix = f"/gateways/GW777/tunnel/session/{session_id}"
    assert response.status_code == 200
    assert response.json()["results_url"] == f"{prefix}/device-ping/results/abc"
    assert response.headers["content-length"] == str(len(response.content))


def test_tunnel_session_rewrites_json_device_ping_status_url() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"

    body = main_module._rewrite_tunnel_json_body(b'{"status_url":"/device-ping/status/abc?poll=1"}', prefix)

    assert json.loads(body)["status_url"] == f"{prefix}/device-ping/status/abc?poll=1"


def test_tunnel_session_rewrites_nested_json_and_arrays() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"
    payload = {
        "next": {"url": "/route-check/status/abc"},
        "links": ["/route-check/results/abc", "/device-ping/results/def?download=1"],
    }

    body = main_module._rewrite_tunnel_json_body(json.dumps(payload).encode(), prefix)

    rewritten = json.loads(body)
    assert rewritten["next"]["url"] == f"{prefix}/route-check/status/abc"
    assert rewritten["links"] == [
        f"{prefix}/route-check/results/abc",
        f"{prefix}/device-ping/results/def?download=1",
    ]


def test_tunnel_session_json_rewrite_preserves_prefixed_external_and_non_url_strings() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"
    payload = {
        "already": f"{prefix}/device-ping/results/abc",
        "external": "https://example.com/device-ping/results/abc",
        "protocol_relative": "//example.com/device-ping/results/abc",
        "ordinary": "device-ping/results/abc",
        "other_root": "/api/cloud-route",
    }

    body = main_module._rewrite_tunnel_json_body(json.dumps(payload).encode(), prefix)

    assert json.loads(body) == payload


def test_tunnel_session_rewrites_inline_javascript_result_navigation() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"
    html = b"""
    <html><head></head><body>
    <script>
    window.location.href = "/device-ping/results/" + jobId;
    location.assign('/route-check/results/' + routeId);
    const external = "https://example.com/device-ping/results/abc";
    const ordinary = "device-ping/results/abc";
    const prefixed = "/gateways/GW777/tunnel/session/session-1/device-ping/results/abc";
    </script>
    </body></html>
    """

    rewritten = main_module._rewrite_tunnel_html_body(html, prefix).decode()

    assert f'window.location.href = "{prefix}/device-ping/results/" + jobId' in rewritten
    assert f"location.assign('{prefix}/route-check/results/' + routeId)" in rewritten
    assert 'const external = "https://example.com/device-ping/results/abc"' in rewritten
    assert 'const ordinary = "device-ping/results/abc"' in rewritten
    assert f'const prefixed = "{prefix}/device-ping/results/abc"' in rewritten


def test_tunnel_session_rewrites_external_javascript_asset_result_navigation() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW777", token_prefix="gw77701")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(
                status_code=200,
                headers={"content-type": "application/javascript"},
                body=b'location.assign("/device-ping/results/" + jobId);',
            )

    tunnel_manager._tunnels["GW777"] = FakeTunnel()
    session_id = ""
    try:
        created = client.post("/api/ui/gateways/GW777/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        response = client.get(f"{session_url}static/app.js")
    finally:
        tunnel_manager._tunnels.pop("GW777", None)
        if session_id:
            tunnel_session_manager._sessions.pop(session_id, None)

    prefix = f"/gateways/GW777/tunnel/session/{session_id}"
    assert response.status_code == 200
    assert response.text == f'location.assign("{prefix}/device-ping/results/" + jobId);'


def test_tunnel_session_rewrites_discover_start_status_and_results_paths() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW777", token_prefix="gw77701")
    captured: list[dict[str, object]] = []

    class FakeTunnel:
        async def request(self, **kwargs):
            captured.append(kwargs)
            if kwargs["path"] == "/discover/start":
                return TunnelResponse(
                    status_code=200,
                    headers={"content-type": "text/html"},
                    body=b"""
                    <html><head></head><body>
                    <script>
                    const statusUrl = "/discover/status/abc";
                    const resultsUrl = "/discover/results/abc";
                    fetch(statusUrl).then(function () { window.location.href = resultsUrl; });
                    </script>
                    </body></html>
                    """,
                )
            if kwargs["path"] == "/discover/status/abc":
                return TunnelResponse(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body=b'{"ok":true,"done":true}',
                )
            if kwargs["path"] == "/discover/results/abc":
                return TunnelResponse(
                    status_code=200,
                    headers={"content-type": "text/html"},
                    body=b"<html><head></head><body>discovered</body></html>",
                )
            return TunnelResponse(status_code=404, headers={"content-type": "text/plain"}, body=b"not found")

    tunnel_manager._tunnels["GW777"] = FakeTunnel()
    session_id = ""
    try:
        created = client.post("/api/ui/gateways/GW777/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        start = client.post(f"{session_url}discover/start")
        prefix = f"/gateways/GW777/tunnel/session/{session_id}"
        status = client.get(f"{prefix}/discover/status/abc")
        results = client.get(f"{prefix}/discover/results/abc")
        escaped_results = client.get("/discover/results/abc")
    finally:
        tunnel_manager._tunnels.pop("GW777", None)
        if session_id:
            tunnel_session_manager._sessions.pop(session_id, None)

    prefix = f"/gateways/GW777/tunnel/session/{session_id}"
    assert start.status_code == 200
    assert f'const statusUrl = "{prefix}/discover/status/abc"' in start.text
    assert f'const resultsUrl = "{prefix}/discover/results/abc"' in start.text
    assert status.status_code == 200
    assert results.status_code == 200
    assert escaped_results.status_code == 404
    assert captured[0]["path"] == "/discover/start"
    assert captured[1]["path"] == "/discover/status/abc"
    assert captured[2]["path"] == "/discover/results/abc"


def test_tunnel_session_rewrites_template_and_live_point_refresh_urls() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"
    html = b"""
    <html><head></head><body>
    <script>
    const response = await fetch(`/template/scan/status/${jobId}`, {cache: "no-store"});
    const start = await fetch("/template/scan/start", {method: "POST"});
    const refreshUrl = "/devices/live/profile-1/refresh";
    const resp = await fetch(refreshUrl + "?read_method=" + encodeURIComponent(method), {method: "POST"});
    document.getElementById("pvFullPageLink").href = "/write-pv?device=" + encodeURIComponent(device);
    </script>
    </body></html>
    """

    rewritten = main_module._rewrite_tunnel_html_body(html, prefix).decode()

    assert f"fetch(`{prefix}/template/scan/status/${{jobId}}`" in rewritten
    assert f'fetch("{prefix}/template/scan/start"' in rewritten
    assert f'const refreshUrl = "{prefix}/devices/live/profile-1/refresh"' in rewritten
    assert f'pvFullPageLink").href = "{prefix}/write-pv?device="' in rewritten


def test_tunnel_session_rewrites_packet_capture_urls() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"
    html = b"""
    <html><head></head><body>
    <form method="post" action="/captures/start"></form>
    <form method="post" action="/captures/stop/job-1"></form>
    <a href="/captures/download/capture.pcap">Download</a>
    <script>
    fetch('/captures/jobs').then(function (r) { return r.json(); });
    </script>
    </body></html>
    """

    rewritten = main_module._rewrite_tunnel_html_body(html, prefix).decode()

    assert f'action="{prefix}/captures/start"' in rewritten
    assert f'action="{prefix}/captures/stop/job-1"' in rewritten
    assert f'href="{prefix}/captures/download/capture.pcap"' in rewritten
    assert f"fetch('{prefix}/captures/jobs')" in rewritten


def test_tunnel_session_json_rewrites_gateway_local_allowlist_urls() -> None:
    prefix = "/gateways/GW777/tunnel/session/session-1"
    payload = {
        "discover": "/discover/results/abc",
        "template": "/template/scan/status/abc",
        "live": "/devices/live/profile-1/refresh?read_method=rpm",
        "capture": "/captures/jobs",
        "write": "/write-pv/apply",
        "external": "https://example.com/discover/results/abc",
        "cloud": "/api/ui/gateways",
        "ordinary": "discover/results/abc",
    }

    rewritten = json.loads(main_module._rewrite_tunnel_json_body(json.dumps(payload).encode(), prefix))

    assert rewritten["discover"] == f"{prefix}/discover/results/abc"
    assert rewritten["template"] == f"{prefix}/template/scan/status/abc"
    assert rewritten["live"] == f"{prefix}/devices/live/profile-1/refresh?read_method=rpm"
    assert rewritten["capture"] == f"{prefix}/captures/jobs"
    assert rewritten["write"] == f"{prefix}/write-pv/apply"
    assert rewritten["external"] == payload["external"]
    assert rewritten["cloud"] == payload["cloud"]
    assert rewritten["ordinary"] == payload["ordinary"]


def test_tunnel_session_device_ping_start_status_and_results_proxy_through_session() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager, tunnel_session_manager

    create_gateway_token("GW777", token_prefix="gw77701")
    captured: list[dict[str, object]] = []

    class FakeTunnel:
        async def request(self, **kwargs):
            captured.append(kwargs)
            if kwargs["path"] == "/device-ping/start":
                return TunnelResponse(
                    status_code=200,
                    headers={"content-type": "text/html"},
                    body=b"""
                    <html><head></head><body>
                    <script>
                    window.location.href = "/device-ping/results/" + jobId;
                    </script>
                    started
                    </body></html>
                    """,
                )
            if kwargs["path"] == "/device-ping/status/abc":
                return TunnelResponse(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body=b'{"status":"done","job_id":"abc"}',
                )
            if kwargs["path"] == "/device-ping/results/abc":
                return TunnelResponse(
                    status_code=200,
                    headers={"content-type": "text/html"},
                    body=b"<html><head></head><body>results</body></html>",
                )
            return TunnelResponse(status_code=404, headers={"content-type": "text/plain"}, body=b"not found")

    tunnel_manager._tunnels["GW777"] = FakeTunnel()
    session_id = ""
    try:
        created = client.post("/api/ui/gateways/GW777/tunnel-session", headers=admin_headers())
        session_url = created.json()["url"]
        session_id = session_url.rstrip("/").split("/")[-1]
        start = client.get(f"{session_url}device-ping/start?run=1&read_metadata=no")
        status = client.get(f"{session_url}device-ping/status/abc")
        prefix = f"/gateways/GW777/tunnel/session/{session_id}"
        result_url = f"{prefix}/device-ping/results/{status.json()['job_id']}"
        results = client.get(result_url)
        escaped_status = client.get("/device-ping/status/abc")
        escaped_results = client.get("/device-ping/results/abc")
    finally:
        tunnel_manager._tunnels.pop("GW777", None)
        if session_id:
            tunnel_session_manager._sessions.pop(session_id, None)

    prefix = f"/gateways/GW777/tunnel/session/{session_id}"
    assert start.status_code == 200
    assert f'window.location.href = "{prefix}/device-ping/results/" + jobId' in start.text
    assert status.status_code == 200
    assert results.status_code == 200
    assert escaped_status.status_code == 404
    assert escaped_results.status_code == 404
    assert captured[0]["path"] == "/device-ping/start"
    assert captured[0]["query_string"] == "run=1&read_metadata=no"
    assert captured[1]["path"] == "/device-ping/status/abc"
    assert captured[2]["path"] == "/device-ping/results/abc"


def test_tunnel_proxy_does_not_rewrite_html_body() -> None:
    from app.tunnel import TunnelResponse, tunnel_manager

    create_gateway_token("GW001")

    class FakeTunnel:
        async def request(self, **kwargs):
            return TunnelResponse(status_code=200, headers={"content-type": "text/html"}, body=b'<a href="/login">login</a>')

    tunnel_manager._tunnels["GW001"] = FakeTunnel()
    try:
        response = client.get("/gateways/GW001/tunnel/proxy/", headers=admin_headers())
    finally:
        tunnel_manager._tunnels.pop("GW001", None)

    assert response.status_code == 200
    assert response.text == '<a href="/login">login</a>'


def test_cloud_login_route_is_not_hijacked_by_tunnel_session() -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert "IOT Cloud Commissioning" in response.text


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
    assert 'window.open("about:blank", "_blank")' in response.text
    assert "tunnelWindow.opener = null" in response.text
    assert "tunnelWindow.location.assign(session.url)" in response.text
    assert "tunnelWindow.close()" in response.text
    assert "Popup blocked? Open tunnel manually" in response.text
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


def test_ui_can_queue_saved_point_reads_and_store_result_value() -> None:
    raw_token = create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=15)
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    group_response = client.post("/api/ui/gateways/GW001/groups", headers=headers, json={"name": "HVAC"})
    device_response = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={
            "group_id": group_response.json()["id"],
            "device_instance": 1001,
            "device_name": "AHU-1",
        },
    )
    point_response = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/points",
        headers=headers,
        json={
            "object_type": "analog-value",
            "object_instance": 1,
            "object_name": "Space Temp",
            "property": "present-value",
        },
    )
    second_point_response = client.post(
        f"/api/ui/devices/{device_response.json()['id']}/points",
        headers=headers,
        json={
            "object_type": "binary-value",
            "object_instance": 5,
            "object_name": "Fan Status",
            "property": "present-value",
        },
    )

    read_response = client.post(
        "/api/ui/gateways/GW001/points/read",
        headers=headers,
        json={"point_ids": [point_response.json()["id"], second_point_response.json()["id"]]},
    )
    job_id = read_response.json()["job_ids"][0]
    next_response = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))
    result_response = client.post(
        f"/api/edge/jobs/{job_id}/result",
        headers=auth_headers(raw_token),
        json={
            "status": "completed",
            "result": {
                "job_type": "bacnet_read_bulk",
                "device_instance": 1001,
                "property": "present-value",
                "status": "ok",
                "values": [
                    {
                        "saved_point_id": point_response.json()["id"],
                        "object_type": "analog-value",
                        "object_instance": 1,
                        "value": 72.4,
                        "raw_value": "72.4",
                        "active_priority": 8,
                        "priority_array": "(NULL, NULL, NULL, NULL, NULL, NULL, NULL, Real: 72.4)",
                        "status": "ok",
                    },
                    {
                        "saved_point_id": second_point_response.json()["id"],
                        "object_type": "binary-value",
                        "object_instance": 5,
                        "value": "active",
                        "raw_value": "active",
                        "status": "ok",
                    },
                ],
            },
            "error_message": None,
        },
    )
    tree_response = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert read_response.status_code == 200
    assert read_response.json()["queued_count"] == 1
    assert next_response.status_code == 200
    assert next_response.json()["job_type"] == "bacnet_read_bulk"
    assert len(next_response.json()["request"]["points"]) == 2
    assert next_response.json()["request"]["points"][0]["saved_point_id"] == point_response.json()["id"]
    assert next_response.json()["request"]["points"][0]["read_priority"] is True
    assert result_response.status_code == 200
    assert tree_response.json()["points"][0]["present_value"] == "72.4"
    assert tree_response.json()["points"][0]["active_priority"] == 8
    assert tree_response.json()["points"][1]["present_value"] == "active"


def test_admin_stages_then_approves_bacnet_write_batch() -> None:
    raw_token = create_gateway_token("GW001")
    set_gateway_heartbeat("GW001", seconds_ago=15)
    headers = admin_headers()
    group = client.post("/api/ui/gateways/GW001/groups", headers=headers, json={"name": "HVAC"}).json()
    device = client.post(
        "/api/ui/gateways/GW001/devices",
        headers=headers,
        json={"group_id": group["id"], "device_instance": 1001, "device_name": "AHU-1"},
    ).json()
    point = client.post(
        f"/api/ui/devices/{device['id']}/points",
        headers=headers,
        json={
            "object_type": "analog-value",
            "object_instance": 5,
            "object_name": "Setpoint",
            "property": "present-value",
            "writable": True,
        },
    ).json()

    staged = client.post(
        "/api/ui/gateways/GW001/points/write",
        headers=headers,
        json={"writes": [{"point_id": point["id"], "value": "72.5", "priority": 8}]},
    )
    before_approval = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))
    approved = client.post(
        f"/api/ui/gateways/GW001/points/write/{staged.json()['batch_id']}/approve",
        headers=headers,
    )
    next_job = client.get("/api/edge/GW001/jobs/next", headers=auth_headers(raw_token))

    assert staged.status_code == 200
    assert staged.json()["status"] == "pending_approval"
    assert staged.json()["approved_by"] is None
    assert staged.json()["job_ids"] == []
    assert before_approval.status_code in {200, 204}
    if before_approval.status_code == 200:
        assert before_approval.json() is None
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"
    assert approved.json()["approved_by"] == "admin_api_token"
    assert len(approved.json()["job_ids"]) == 1
    assert next_job.json()["job_type"] == "bacnet_write_batch"
    assert next_job.json()["request"]["writes"][0]["priority"] == 8


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


def test_point_trend_config_and_edge_sample_upload() -> None:
    raw_token = create_gateway_token("GW001")
    user_id = create_operator_user("operator@example.com", role="operator", status="active")
    headers = user_headers("operator@example.com", user_id)
    device = client.post("/api/ui/gateways/GW001/devices", headers=headers, json={"device_instance": 1001, "device_name": "AHU-1"}).json()
    point = client.post(
        f"/api/ui/devices/{device['id']}/points",
        headers=headers,
        json={"object_type": "analog-value", "object_instance": 10, "object_name": "Setpoint"},
    ).json()

    configured = client.put(f"/api/ui/points/{point['id']}/trend", headers=headers, json={"enabled": True, "interval_sec": 60})
    edge_configs = client.get("/api/edge/GW001/trend-configs", headers=auth_headers(raw_token))
    uploaded = client.post(
        "/api/edge/GW001/trend-samples",
        headers=auth_headers(raw_token),
        json=[{"point_id": point["id"], "sampled_at": "2026-07-11T12:00:00Z", "value": "72.5"}],
    )
    history = client.get(f"/api/ui/points/{point['id']}/trend", headers=headers)
    recent_history = client.get(f"/api/ui/points/{point['id']}/trend?since=2026-07-11T12:00:01Z", headers=headers)
    tree = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert configured.status_code == 200
    assert edge_configs.status_code == 200
    assert edge_configs.json()[0]["device_instance"] == 1001
    assert uploaded.status_code == 200
    assert history.json()[0]["value"] == "72.5"
    assert recent_history.json() == []
    assert tree.json()["points"][0]["trend_enabled"] is True
    assert tree.json()["points"][0]["trend_interval_sec"] == 60

    disabled = client.put(f"/api/ui/points/{point['id']}/trend", headers=headers, json={"enabled": False, "interval_sec": 60})
    disabled_tree = client.get("/api/ui/gateways/GW001/tree", headers=headers)

    assert disabled.status_code == 200
    assert disabled_tree.json()["points"][0]["trend_enabled"] is False


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


def test_edge_discovery_and_point_load_results_reconcile_inventory_lifecycle() -> None:
    from app.models import EdgeJob, SavedBacnetDevice, SavedBacnetPoint

    raw_token = create_gateway_token("GW001")
    discovery = client.post(
        "/api/edge/jobs",
        headers=admin_headers(),
        json={"gateway_id": "GW001", "job_type": "bacnet_discover", "request": {}},
    ).json()
    response = client.post(
        f"/api/edge/jobs/{discovery['job_id']}/result",
        headers=auth_headers(raw_token),
        json={"status": "completed", "result": {"devices": [{"device_id": 1001, "network": 1, "mac": "0A"}]}, "error_message": None},
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        device = db.scalar(select(SavedBacnetDevice).where(SavedBacnetDevice.gateway_id == "GW001"))
        assert device is not None
        assert device.device_instance == 1001
        assert device.lifecycle_state == "active"
        assert device.first_seen_at is not None
        assert device.last_seen_at is not None
        device_id = str(device.id)

    with SessionLocal() as db:
        point_load = EdgeJob(
            job_id=f"job-{uuid4().hex}",
            gateway_id="GW001",
            job_type="bacnet_load_points",
            status="claimed",
            request_json={"saved_device_id": device_id},
        )
        db.add(point_load)
        db.commit()
        point_load_job_id = point_load.job_id
    response = client.post(
        f"/api/edge/jobs/{point_load_job_id}/result",
        headers=auth_headers(raw_token),
        json={"status": "completed", "result": {"points": [{"object_type": "analog-value", "object_instance": 39, "object_name": "Occupied Heat Setpoint"}]}, "error_message": None},
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        point = db.scalar(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device_id))
        assert point is not None
        assert point.lifecycle_state == "active"
        assert point.first_seen_at is not None
        assert point.last_seen_at is not None


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


def test_workspace_routes_enforce_site_scope_and_admin_access_overview_lists_memberships() -> None:
    from app.auth import AdminAuthContext, require_job_operator_auth, require_operator_auth
    from app.models import SiteMembership

    create_gateway_token("GW001")
    user_id = create_operator_user("scoped@example.com", role="operator", status="active")
    with SessionLocal() as db:
        visible_site = db.scalar(select(Site).where(Site.site_id == "demo-site"))
        assert visible_site is not None
        hidden_site = Site(site_id="hidden-site", name="Hidden site")
        db.add(hidden_site)
        db.flush()
        db.add(
            EdgeNode(
                gateway_id="GW002",
                site_id=hidden_site.site_id,
                hostname="GW002",
                bacnet_port=47814,
                agent_version="0.1.0",
                ui_version="0.1.0",
                sqlite_db_ok=True,
                queued_upload_count=0,
                latest_status="online",
            )
        )
        operator = db.scalar(select(OperatorUser).where(OperatorUser.email == "scoped@example.com"))
        assert operator is not None
        db.add(SiteMembership(site_uuid=visible_site.id, operator_user_id=operator.id, role="operator"))
        db.commit()

    scoped_auth = AdminAuthContext(auth_type="supabase_user", role="operator", operator_user_id=str(operator.id))
    app.dependency_overrides[require_operator_auth] = lambda: scoped_auth
    app.dependency_overrides[require_job_operator_auth] = lambda: scoped_auth
    try:
        assert client.get("/api/ui/gateways/GW001/tree").status_code == 200
        assert client.get("/api/ui/gateways/GW002/tree").status_code == 404
        assert client.post("/api/ui/gateways/GW002/groups", json={"name": "Hidden"}).status_code == 404
    finally:
        app.dependency_overrides.clear()

    overview = client.get("/api/admin/access-overview", headers=admin_headers())
    assert overview.status_code == 200
    assert overview.json()["memberships"] == [{"email": "scoped@example.com", "role": "operator", "scope_kind": "site", "scope_id": "demo-site", "scope_name": "demo-site"}]
