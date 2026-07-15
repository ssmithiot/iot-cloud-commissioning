"""HTTP-level Customer 2 tenancy isolation tests.

Complements the pure-function coverage in test_site_access.py by hitting
real routes through TestClient: two organizations, each with their own
gateway/device/point/trend config, and verifying a scoped Organization A
operator cannot reach Organization B's resources by guessed valid IDs --
and that the shared admin token and platform-admin operators still can.
"""

import os
import sys
from pathlib import Path

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

from app import access as access_module
from app.auth import AdminAuthContext, require_job_operator_auth, require_operator_auth
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Organization, OperatorUser, OrganizationMembership, Site

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()
    engine.dispose()


def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-token"}


def provision_tenant(label: str) -> dict[str, str]:
    """Provision one organization with a gateway, one device, one point,
    and an enabled trend config, all via the real admin/operator HTTP
    routes (not raw DB inserts) so this exercises the actual request path.
    Returns the ids needed to address each resource."""
    gateway_id = f"GW-{label}"
    site_id = f"site-{label.lower()}"

    org_response = client.post("/api/admin/organizations", headers=admin_headers(), json={"name": f"Customer {label}"})
    assert org_response.status_code == 200, org_response.text
    organization_id = org_response.json()["id"]

    provision_response = client.post(
        "/api/admin/gateways/provision",
        headers=admin_headers(),
        json={"gateway_id": gateway_id, "site_id": site_id, "hostname": gateway_id.lower()},
    )
    assert provision_response.status_code == 200, provision_response.text

    site_response = client.put(
        f"/api/admin/sites/{site_id}/organization/{organization_id}",
        headers=admin_headers(),
    )
    assert site_response.status_code == 200, site_response.text

    device_response = client.post(
        f"/api/ui/gateways/{gateway_id}/devices",
        headers=admin_headers(),
        json={"device_instance": 1, "device_name": f"{label} device"},
    )
    assert device_response.status_code == 200, device_response.text
    device_id = device_response.json()["id"]

    point_response = client.post(
        f"/api/ui/devices/{device_id}/points",
        headers=admin_headers(),
        json={"object_type": "analog-input", "object_instance": 1, "object_name": f"{label} point"},
    )
    assert point_response.status_code == 200, point_response.text
    point_id = point_response.json()["id"]

    trend_response = client.put(
        f"/api/ui/points/{point_id}/trend",
        headers=admin_headers(),
        json={"enabled": True, "interval_sec": 300},
    )
    assert trend_response.status_code == 200, trend_response.text

    return {
        "organization_id": organization_id,
        "gateway_id": gateway_id,
        "device_id": device_id,
        "point_id": point_id,
    }


def scoped_auth_for(email: str, organization_id: str) -> AdminAuthContext:
    user_id = None
    with SessionLocal() as db:
        operator = OperatorUser(email=email, role="operator", status="active")
        db.add(operator)
        db.flush()
        db.add(OrganizationMembership(organization_id=organization_id, operator_user_id=operator.id, role="operator"))
        db.commit()
        user_id = str(operator.id)
    return AdminAuthContext(auth_type="supabase_user", role="operator", operator_user_id=user_id)


def resource_routes(tenant: dict[str, str]) -> dict[str, tuple[str, str, dict | None]]:
    """Map a friendly resource-type name to (method, path, json body)."""
    return {
        "gateway": ("GET", f"/api/ui/gateways/{tenant['gateway_id']}", None),
        "tree": ("GET", f"/api/ui/gateways/{tenant['gateway_id']}/tree", None),
        "device": ("PATCH", f"/api/ui/devices/{tenant['device_id']}", {"device_name": "probe"}),
        "point": ("PATCH", f"/api/ui/points/{tenant['point_id']}", {"object_name": "probe"}),
        "trend": ("GET", f"/api/ui/points/{tenant['point_id']}/trend", None),
    }


def call(method: str, path: str, json_body: dict | None, headers: dict[str, str] | None = None):
    if method == "GET":
        return client.get(path, headers=headers or {})
    return client.patch(path, json=json_body, headers=headers or {})


def test_organization_a_operator_cannot_reach_organization_b_resources_by_guessed_id() -> None:
    tenant_a = provision_tenant("A")
    tenant_b = provision_tenant("B")
    auth_a = scoped_auth_for("operator-a@example.com", tenant_a["organization_id"])

    app.dependency_overrides[require_operator_auth] = lambda: auth_a
    app.dependency_overrides[require_job_operator_auth] = lambda: auth_a
    try:
        for resource_type, (method, path, body) in resource_routes(tenant_a).items():
            response = call(method, path, body)
            assert response.status_code == 200, f"Org A operator should reach their own {resource_type}: {response.text}"

        for resource_type, (method, path, body) in resource_routes(tenant_b).items():
            response = call(method, path, body)
            assert response.status_code == 404, (
                f"Org A operator reached Org B's {resource_type} via guessed id ({path}): "
                f"status={response.status_code} body={response.text}"
            )
    finally:
        app.dependency_overrides.clear()


def test_platform_admin_and_admin_token_retain_global_access_across_organizations() -> None:
    tenant_a = provision_tenant("A")
    tenant_b = provision_tenant("B")

    # Shared admin token, no dependency override needed -- real header auth.
    for tenant in (tenant_a, tenant_b):
        for resource_type, (method, path, body) in resource_routes(tenant).items():
            response = call(method, path, body, headers=admin_headers())
            assert response.status_code == 200, f"admin token should reach {resource_type} in any org: {response.text}"

    # An operator whose global role is "admin" (not the shared token).
    with SessionLocal() as db:
        admin_operator = OperatorUser(email="platform-admin@example.com", role="admin", status="active")
        db.add(admin_operator)
        db.commit()
        admin_operator_id = str(admin_operator.id)
    admin_auth = AdminAuthContext(auth_type="supabase_user", role="admin", operator_user_id=admin_operator_id)
    app.dependency_overrides[require_operator_auth] = lambda: admin_auth
    app.dependency_overrides[require_job_operator_auth] = lambda: admin_auth
    try:
        for tenant in (tenant_a, tenant_b):
            for resource_type, (method, path, body) in resource_routes(tenant).items():
                response = call(method, path, body)
                assert response.status_code == 200, f"role=admin operator should reach {resource_type} in any org: {response.text}"
    finally:
        app.dependency_overrides.clear()


def test_zero_membership_operator_http_behavior_matches_flag_state(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_a = provision_tenant("A")
    with SessionLocal() as db:
        zero_membership_operator = OperatorUser(email="zero-membership@example.com", role="operator", status="active")
        db.add(zero_membership_operator)
        db.commit()
        operator_id = str(zero_membership_operator.id)
    zero_auth = AdminAuthContext(auth_type="supabase_user", role="operator", operator_user_id=operator_id)

    app.dependency_overrides[require_operator_auth] = lambda: zero_auth
    app.dependency_overrides[require_job_operator_auth] = lambda: zero_auth
    try:
        # Flag off (default): legacy fallback, sees the gateway tree.
        assert access_module.settings.require_explicit_membership is False
        response = client.get(f"/api/ui/gateways/{tenant_a['gateway_id']}/tree")
        assert response.status_code == 200

        # Flag on: fail closed, same zero-membership operator now sees nothing.
        monkeypatch.setattr(access_module.settings, "require_explicit_membership", True)
        response = client.get(f"/api/ui/gateways/{tenant_a['gateway_id']}/tree")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
