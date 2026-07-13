"""Environment identity and staging safety-guard tests."""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EDGE_AGENT_PATH = Path(__file__).resolve().parents[2] / "edge-agent"
if str(EDGE_AGENT_PATH) not in sys.path:
    sys.path.append(str(EDGE_AGENT_PATH))

os.environ["DATABASE_URL"] = "sqlite:///./test-cloud-api.db"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["GATEWAY_AUTH_PEPPER"] = "test-pepper"
os.environ["IOT_ADMIN_API_TOKEN"] = "test-admin-token"
os.environ["SUPABASE_JWT_SECRET"] = "test-supabase-jwt-secret"

from app.config import Settings, production_resource_conflicts
from app.database import connect_args_for
from app.main import app

client = TestClient(app)


def test_health_reports_environment_and_version_without_secrets() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "development"  # default when ENVIRONMENT unset
    assert body["version"] == app.version
    # Exactly these keys: no URLs, tokens, or credentials may ever appear here.
    assert set(body) == {"status", "environment", "version"}
    text = response.text.lower()
    for forbidden in ("postgres", "supabase.co", "token", "pepper", "secret"):
        assert forbidden not in text


def test_environment_setting_accepts_known_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert Settings().environment == "staging"
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert Settings().environment == "production"


def test_environment_setting_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod-ish")
    with pytest.raises(Exception):
        Settings()


def test_staging_guard_flags_known_production_fingerprints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("SUPABASE_URL", "https://iot-cloud-api-dev.onrender.com")
    conflicts = production_resource_conflicts(Settings())
    assert conflicts == ["SUPABASE_URL"]


def test_staging_guard_supports_custom_fingerprints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("PRODUCTION_RESOURCE_FINGERPRINTS", "prod-project-ref, iot-cloud-api-dev.onrender.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.PROD-PROJECT-REF.supabase.co:5432/postgres")
    conflicts = production_resource_conflicts(Settings())
    assert conflicts == ["DATABASE_URL"]


def test_staging_guard_reports_names_not_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("SUPABASE_URL", "https://iot-cloud-api-dev.onrender.com")
    for conflict in production_resource_conflicts(Settings()):
        assert "onrender" not in conflict
        assert conflict.isupper()


def test_staging_guard_inactive_outside_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://iot-cloud-api-dev.onrender.com")
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert production_resource_conflicts(Settings()) == []
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert production_resource_conflicts(Settings()) == []


def test_staging_guard_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("SUPABASE_URL", "https://iot-cloud-api-dev.onrender.com")
    monkeypatch.setenv("ALLOW_PRODUCTION_RESOURCES", "true")
    assert production_resource_conflicts(Settings()) == []


def test_connect_args_disable_prepared_statements_on_postgres() -> None:
    # Required for transaction-mode poolers (Supabase port 6543 / PgBouncer):
    # psycopg auto-prepared statements do not survive backend multiplexing.
    assert connect_args_for("postgresql+psycopg://u:p@host:5432/db") == {"prepare_threshold": None}
    assert connect_args_for("sqlite:///./x.db") == {"check_same_thread": False}


def test_staging_guard_clean_staging_config_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.staging-ref.supabase.co:5432/postgres")
    monkeypatch.setenv("SUPABASE_URL", "https://staging-ref.supabase.co")
    assert production_resource_conflicts(Settings()) == []
