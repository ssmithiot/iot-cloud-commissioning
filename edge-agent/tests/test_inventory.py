import json
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
    }]}
