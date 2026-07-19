import json
import sqlite3
from pathlib import Path

from iot_cx_agent.inventory import _snapshot


def test_snapshot_reads_only_valid_edge_ui_saved_devices(tmp_path: Path) -> None:
    devices = tmp_path / "devices"
    devices.mkdir()
    (devices / "rtu.json").write_text(json.dumps({
        "device_id": 1001,
        "device_name": "RTU-1",
        "points": [{"object_type": "analog-value", "instance": 1, "object_name": "Setpoint"}],
    }), encoding="utf-8")
    (devices / "invalid.json").write_text("not json", encoding="utf-8")

    assert _snapshot(tmp_path) == {"devices": [{
        "device_instance": 1001,
        "device_name": "RTU-1",
        "points": [{"object_type": "analog-value", "object_instance": 1, "object_name": "Setpoint"}],
    }], "trend_snapshot_complete": False, "trend_points": []}


def test_snapshot_includes_enabled_edge_trends(tmp_path: Path) -> None:
    trends = tmp_path / "edge-trends.db"
    with sqlite3.connect(trends) as conn:
        conn.executescript("""
            CREATE TABLE trend_groups (id INTEGER PRIMARY KEY, interval_sec INTEGER, enabled INTEGER);
            CREATE TABLE trend_points (group_id INTEGER, device_instance INTEGER, object_type TEXT, object_instance INTEGER);
            INSERT INTO trend_groups VALUES (1, 300, 1), (2, 60, 1), (3, 120, 0);
            INSERT INTO trend_points VALUES (1, 1001, 'analog-input', 1), (2, 1001, 'analog-input', 1), (3, 1001, 'analog-input', 2);
        """)
    snapshot = _snapshot(tmp_path, trends)
    assert snapshot["trend_snapshot_complete"] is True
    assert snapshot["trend_points"] == [{"device_instance": 1001, "object_type": "analog-input", "object_instance": 1, "interval_sec": 60}]
