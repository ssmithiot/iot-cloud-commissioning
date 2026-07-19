from __future__ import annotations

import sqlite3
from pathlib import Path

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.local_trends import sample_local_edge_trends


def _config(tmp_path: Path, trends_db: Path) -> AgentConfig:
    return AgentConfig(
        gateway_id="GW006", site_id="GW006", cloud_url="https://cloud.example.test",
        sqlite_path=tmp_path / "edge.db", local_edge_trends_enabled=True,
        edge_trends_db_path=trends_db,
    )


def _create_trends_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE trend_groups (id INTEGER PRIMARY KEY, name TEXT, interval_sec INTEGER, enabled INTEGER);
        CREATE TABLE trend_points (id INTEGER PRIMARY KEY, group_id INTEGER, device_profile_id TEXT, device_instance INTEGER, object_type TEXT, object_instance INTEGER, object_name TEXT);
        CREATE TABLE trend_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, started_at TEXT, completed_at TEXT, requested_count INTEGER, returned_count INTEGER, deferred_count INTEGER, duration_ms INTEGER, cpu_load_pct REAL, memory_used_pct REAL, network_rx_bytes INTEGER, network_tx_bytes INTEGER, error_text TEXT);
        CREATE TABLE trend_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, trend_point_id INTEGER, sampled_at TEXT, value_text TEXT, status TEXT, read_source TEXT, error_text TEXT);
        INSERT INTO trend_groups VALUES (1, 'Pilot', 60, 1);
        INSERT INTO trend_points VALUES (11, 1, '1-demo', 1001, 'analog-value', 1, 'Temperature');
        INSERT INTO trend_points VALUES (12, 1, '1-demo', 1001, 'analog-value', 2, 'Setpoint');
        """)


def test_local_trends_stores_samples_without_cloud_upload(tmp_path: Path, monkeypatch) -> None:
    trends_db = tmp_path / "edge-trends.db"
    _create_trends_db(trends_db)
    config = _config(tmp_path, trends_db)
    calls: list[dict] = []
    monkeypatch.setattr(
        "iot_cx_agent.local_trends.run_bacnet_read_bulk",
        lambda _config, request: (calls.append(request) or {"values": [
            {"saved_point_id": "11", "status": "ok", "value": "71.2", "read_source": "rpm-bulk"},
            {"saved_point_id": "12", "status": "ok", "value": "72.2", "read_source": "rpm-bulk"},
        ]}, None),
    )
    assert sample_local_edge_trends(config) == 1
    assert len(calls) == 1
    assert len(calls[0]["points"]) == 2
    with sqlite3.connect(trends_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trend_samples").fetchone()[0] == 2
        assert conn.execute("SELECT returned_count FROM trend_runs").fetchone()[0] == 2


def test_local_trends_skips_when_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "missing.db")
    assert sample_local_edge_trends(config) == 0
