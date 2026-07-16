from pathlib import Path

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import initialize_database, pending_trend_samples, queue_trend_sample, record_trend_upload_failure
from iot_cx_agent.status import collect_status


def test_collect_status_includes_physical_identity_fingerprint(monkeypatch, tmp_path: Path) -> None:
    config = AgentConfig(gateway_id="GW082", site_id="site-1", cloud_url="https://cloud.example", sqlite_path=tmp_path / "agent.db")
    monkeypatch.setattr("iot_cx_agent.status.detect_machine_id", lambda: "machine-gw082")
    monkeypatch.setattr("iot_cx_agent.status.detect_primary_mac", lambda: "02:00:00:00:00:82")

    status = collect_status(config, sqlite_db_ok=False)

    assert status["machine_id"] == "machine-gw082"
    assert status["primary_mac"] == "02:00:00:00:00:82"


def test_collect_status_includes_lightweight_resource_metrics(tmp_path: Path) -> None:
    config = AgentConfig(gateway_id="GW001", site_id="site-1", cloud_url="https://cloud.example", sqlite_path=tmp_path / "agent.db")

    status = collect_status(config, sqlite_db_ok=False)

    assert status["cpu_count"] >= 1
    assert status["disk_free_mb"] is not None
    assert "cpu_load_1m" in status
    assert "memory_used_pct" in status


def test_collect_status_reports_trend_backlog_health(tmp_path: Path) -> None:
    config = AgentConfig(gateway_id="GW001", site_id="site-1", cloud_url="https://cloud.example", sqlite_path=tmp_path / "agent.db")
    initialize_database(config.sqlite_path)
    queue_trend_sample(config.sqlite_path, {"point_id": "point-1", "value": "72"}, "2026-07-12T00:00:00+00:00")
    item_id = pending_trend_samples(config.sqlite_path)[0][0]
    record_trend_upload_failure(
        config.sqlite_path,
        [item_id],
        error="cloud offline",
        retry_at="9999-12-31T23:59:59.999999+00:00",
        updated_at="2026-07-12T00:01:00+00:00",
    )

    status = collect_status(config)

    assert status["trend_pending_upload_count"] == 1
    assert status["trend_deferred_upload_count"] == 1
    assert status["trend_max_upload_attempt_count"] == 1
    assert status["trend_oldest_pending_at"] == "2026-07-12T00:00:00+00:00"
