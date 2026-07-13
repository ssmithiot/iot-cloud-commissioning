"""GW032 incident regression tests (docs/gw032-trend-backlog-incident.md).

Retired/disabled points must never remain in the edge trend workload, and the
UI tree and edge trend-config endpoint must never disagree about them.
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

from app.auth import hash_gateway_token
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EdgeNode, GatewayCredential, PointTrendConfig, Site
from scripts.create_gateway_credential import DEFAULT_SCOPES


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


client = TestClient(app)
ADMIN = {"Authorization": "Bearer test-admin-token"}


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


def create_trended_point(gateway_id: str = "GW001", device_instance: int = 9001, object_instance: int = 1) -> tuple[str, str]:
    """Returns (device_id, point_id) with an enabled 60 s trend config."""
    device = client.post(
        f"/api/ui/gateways/{gateway_id}/devices",
        headers=ADMIN,
        json={"device_instance": device_instance, "device_name": f"AHU-{device_instance}"},
    )
    assert device.status_code == 200
    device_id = device.json()["id"]
    point = client.post(
        f"/api/ui/devices/{device_id}/points",
        headers=ADMIN,
        json={"object_type": "analog-input", "object_instance": object_instance, "object_name": f"AI-{object_instance}"},
    )
    assert point.status_code == 200
    point_id = point.json()["id"]
    trend = client.put(f"/api/ui/points/{point_id}/trend", headers=ADMIN, json={"enabled": True, "interval_sec": 60})
    assert trend.status_code == 200
    return device_id, point_id


def edge_config_point_ids(raw_token: str, gateway_id: str = "GW001") -> list[str]:
    response = client.get(f"/api/edge/{gateway_id}/trend-configs", headers={"Authorization": f"Bearer {raw_token}"})
    assert response.status_code == 200
    return [config["point_id"] for config in response.json()]


def test_retiring_a_point_disables_its_trend_config_and_edge_view() -> None:
    raw_token = create_gateway()
    _, point_id = create_trended_point()
    assert edge_config_point_ids(raw_token) == [point_id]

    assert client.delete(f"/api/ui/points/{point_id}", headers=ADMIN).status_code == 200

    assert edge_config_point_ids(raw_token) == []
    with SessionLocal() as db:
        config = db.scalar(select(PointTrendConfig).where(PointTrendConfig.point_id == point_id))
        assert config is not None and config.enabled is False  # disabled in the same transaction


def test_bulk_retirement_disables_all_trend_configs() -> None:
    raw_token = create_gateway()
    _, point_a = create_trended_point(object_instance=1)
    _, point_b = create_trended_point(device_instance=9002, object_instance=2)
    assert sorted(edge_config_point_ids(raw_token)) == sorted([point_a, point_b])

    response = client.post("/api/ui/points/bulk-remove", headers=ADMIN, json={"point_ids": [point_a, point_b]})
    assert response.status_code == 200 and response.json()["removed_count"] == 2

    assert edge_config_point_ids(raw_token) == []
    with SessionLocal() as db:
        enabled = db.scalars(select(PointTrendConfig).where(PointTrendConfig.enabled.is_(True))).all()
        assert enabled == []


def test_retiring_a_device_disables_trends_on_all_its_points() -> None:
    raw_token = create_gateway()
    device_id, point_a = create_trended_point(object_instance=1)
    # Second point on the SAME device.
    point = client.post(
        f"/api/ui/devices/{device_id}/points",
        headers=ADMIN,
        json={"object_type": "analog-input", "object_instance": 2, "object_name": "AI-2"},
    )
    point_b = point.json()["id"]
    assert client.put(f"/api/ui/points/{point_b}/trend", headers=ADMIN, json={"enabled": True, "interval_sec": 60}).status_code == 200
    assert len(edge_config_point_ids(raw_token)) == 2

    assert client.delete(f"/api/ui/devices/{device_id}", headers=ADMIN).status_code == 200

    assert edge_config_point_ids(raw_token) == []
    with SessionLocal() as db:
        enabled = db.scalars(select(PointTrendConfig).where(PointTrendConfig.enabled.is_(True))).all()
        assert enabled == []


def test_legacy_enabled_config_on_retired_point_is_excluded_from_edge() -> None:
    """The exact GW032 state: point retired but its config still enabled."""
    raw_token = create_gateway()
    _, point_id = create_trended_point()
    assert client.delete(f"/api/ui/points/{point_id}", headers=ADMIN).status_code == 200

    # Recreate the legacy defect state directly (as production had it).
    with SessionLocal() as db:
        config = db.scalar(select(PointTrendConfig).where(PointTrendConfig.point_id == point_id))
        config.enabled = True
        db.commit()

    assert edge_config_point_ids(raw_token) == []  # join excludes it regardless


def test_admin_repair_is_idempotent_and_reports_counts() -> None:
    raw_token = create_gateway()
    _, point_id = create_trended_point()
    assert client.delete(f"/api/ui/points/{point_id}", headers=ADMIN).status_code == 200
    # Recreate legacy defect state.
    with SessionLocal() as db:
        config = db.scalar(select(PointTrendConfig).where(PointTrendConfig.point_id == point_id))
        config.enabled = True
        db.commit()

    first = client.post("/api/admin/maintenance/disable-retired-trend-configs?gateway_id=GW001", headers=ADMIN)
    assert first.status_code == 200
    assert first.json() == {"disabled_count": 1, "gateway_id": "GW001"}

    second = client.post("/api/admin/maintenance/disable-retired-trend-configs?gateway_id=GW001", headers=ADMIN)
    assert second.status_code == 200
    assert second.json()["disabled_count"] == 0  # safe to re-run

    assert edge_config_point_ids(raw_token) == []


def test_tree_and_edge_endpoint_cannot_disagree_about_retired_points() -> None:
    raw_token = create_gateway()
    _, point_id = create_trended_point()
    assert client.delete(f"/api/ui/points/{point_id}", headers=ADMIN).status_code == 200

    tree = client.get("/api/ui/gateways/GW001/tree", headers=ADMIN)
    assert tree.status_code == 200
    assert point_id not in tree.text  # invisible in the UI tree
    assert edge_config_point_ids(raw_token) == []  # and absent from the edge workload
