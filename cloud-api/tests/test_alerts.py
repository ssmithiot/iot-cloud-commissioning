"""Fleet alerting groundwork tests: transition-based offline + trend-backlog
alerts, eligibility gating, groundwork (no-webhook) mode, and delivery retry."""

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

from app import main as main_module
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EdgeNode, GatewayAlertState, OperatorUser, Site, utc_now


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


client = TestClient(app)


def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-token"}


def create_gateway(
    gateway_id: str,
    *,
    heartbeat_seconds_ago: int | None,
    trend_oldest_pending_hours_ago: float | None = None,
    trend_pending: int = 0,
) -> None:
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
                latest_heartbeat_at=None if heartbeat_seconds_ago is None else utc_now() - timedelta(seconds=heartbeat_seconds_ago),
                trend_oldest_pending_at=None if trend_oldest_pending_hours_ago is None else utc_now() - timedelta(hours=trend_oldest_pending_hours_ago),
                trend_pending_upload_count=trend_pending,
            )
        )
        db.commit()


def set_heartbeat(gateway_id: str, seconds_ago: int) -> None:
    with SessionLocal() as db:
        edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == gateway_id))
        edge_node.latest_heartbeat_at = utc_now() - timedelta(seconds=seconds_ago)
        db.commit()


def evaluate() -> dict:
    response = client.post("/api/admin/alerts/evaluate", headers=admin_headers())
    assert response.status_code == 200
    return response.json()


def test_never_heartbeated_gateways_are_ineligible() -> None:
    create_gateway("GW-PREPROV", heartbeat_seconds_ago=None)
    result = evaluate()
    assert result["evaluated_gateways"] == 0
    assert result["events"] == []
    with SessionLocal() as db:
        assert db.scalar(select(GatewayAlertState)) is None  # no state rows at all


def test_offline_alert_fires_once_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(main_module.settings, "alert_webhook_url", "https://hooks.example.test/x")
    monkeypatch.setattr(main_module, "_deliver_alert_webhook", lambda url, payload: sent.append(payload) or True)

    create_gateway("GW001", heartbeat_seconds_ago=7200)  # far beyond offline threshold (1800 s)

    first = evaluate()
    assert [e["type"] for e in first["events"]] == ["gateway_offline"]
    assert first["events"][0]["delivered"] is True
    assert len(sent) == 1
    assert sent[0]["type"] == "gateway_offline"
    assert "OFFLINE" in sent[0]["text"]  # Slack-compatible summary line
    assert sent[0]["gateway_id"] == "GW001"

    # Second evaluation: still offline, no re-notification.
    second = evaluate()
    assert second["events"] == []
    assert len(sent) == 1

    # Heartbeats resume: exactly one recovery event.
    set_heartbeat("GW001", seconds_ago=10)
    third = evaluate()
    assert [e["type"] for e in third["events"]] == ["gateway_offline_recovered"]
    assert len(sent) == 2
    assert evaluate()["events"] == []  # and then silence


def test_trend_backlog_alert_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(main_module.settings, "alert_webhook_url", "https://hooks.example.test/x")
    monkeypatch.setattr(main_module, "_deliver_alert_webhook", lambda url, payload: sent.append(payload) or True)

    create_gateway("GW002", heartbeat_seconds_ago=10, trend_oldest_pending_hours_ago=12, trend_pending=400)

    first = evaluate()
    assert [e["type"] for e in first["events"]] == ["trend_backlog"]
    assert "backlog" in sent[0]["text"]
    assert evaluate()["events"] == []  # deduplicated

    # Backlog drains.
    with SessionLocal() as db:
        edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == "GW002"))
        edge_node.trend_oldest_pending_at = None
        edge_node.trend_pending_upload_count = 0
        db.commit()
    recovery = evaluate()
    assert [e["type"] for e in recovery["events"]] == ["trend_backlog_recovered"]


def test_offline_gateway_does_not_also_backlog_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "alert_webhook_url", "https://hooks.example.test/x")
    monkeypatch.setattr(main_module, "_deliver_alert_webhook", lambda url, payload: True)
    create_gateway("GW003", heartbeat_seconds_ago=7200, trend_oldest_pending_hours_ago=48, trend_pending=900)

    result = evaluate()
    assert [e["type"] for e in result["events"]] == ["gateway_offline"]  # backlog suppressed while offline


def test_groundwork_mode_reports_without_delivering() -> None:
    # No webhook configured (default): events appear in the response once,
    # marked undelivered, and are acknowledged so a later webhook rollout
    # does not replay history.
    create_gateway("GW004", heartbeat_seconds_ago=7200)
    first = evaluate()
    assert first["webhook_configured"] is False
    assert [e["type"] for e in first["events"]] == ["gateway_offline"]
    assert first["events"][0]["delivered"] is False
    assert first["delivery_failures"] == 0
    assert evaluate()["events"] == []  # acknowledged, not replayed


def test_failed_delivery_is_retried_next_run(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = iter([False, True])
    sent_attempts: list[str] = []

    def flaky(url: str, payload: dict) -> bool:
        sent_attempts.append(payload["type"])
        return next(outcomes)

    monkeypatch.setattr(main_module.settings, "alert_webhook_url", "https://hooks.example.test/x")
    monkeypatch.setattr(main_module, "_deliver_alert_webhook", flaky)
    create_gateway("GW005", heartbeat_seconds_ago=7200)

    first = evaluate()
    assert first["delivery_failures"] == 1
    assert first["events"][0]["delivered"] is False

    second = evaluate()  # same pending alert retried, now succeeds
    assert second["delivery_failures"] == 0
    assert [e["type"] for e in second["events"]] == ["gateway_offline"]
    assert second["events"][0]["delivered"] is True
    assert sent_attempts == ["gateway_offline", "gateway_offline"]
    assert evaluate()["events"] == []


def test_evaluate_requires_admin_role() -> None:
    with SessionLocal() as db:
        db.add(OperatorUser(supabase_user_id=str(uuid4()), email="op@example.com", role="operator", status="active"))
        db.commit()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"aud": "authenticated", "exp": now + timedelta(minutes=15), "iat": now, "sub": str(uuid4()), "email": "op@example.com", "role": "authenticated"},
        "test-supabase-jwt-secret",
        algorithm="HS256",
    )
    response = client.post("/api/admin/alerts/evaluate", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
