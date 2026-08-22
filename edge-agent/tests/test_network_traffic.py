from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from iot_cx_agent.db import initialize_database, record_trend_transport_event
from iot_cx_agent.main import main
from iot_cx_agent.network_traffic import CATEGORIES, record, report


def test_proven_network_history_remains_readable(tmp_path: Path) -> None:
    path = tmp_path / "edge.db"
    initialize_database(path)
    now = datetime(2026, 8, 22, 12, 4, tzinfo=timezone.utc)
    record(path, "heartbeat", tx_bytes=12, rx_bytes=34, success=True, now=now)
    record(path, "jobs_poll", tx_bytes=5, rx_bytes=6, success=False, now=now)

    result = report(path, now=now)

    hour = result["periods"]["current_hour"]
    assert set(hour) == set(CATEGORIES)
    assert hour["heartbeat"] == {"tx_bytes": 12, "rx_bytes": 34, "request_count": 1, "success_count": 1, "failure_count": 0}
    assert hour["jobs_poll"]["failure_count"] == 1


def test_022_trend_transport_history_remains_reportable(tmp_path: Path) -> None:
    path = tmp_path / "edge.db"
    initialize_database(path)
    for transport, success in (("local_trend_upload", False), ("legacy_trend_upload", True)):
        record_trend_transport_event(
            path, recorded_at="2026-08-22T12:00:00+00:00", transport=transport,
            success=success, http_status=200 if success else 404, sample_count=3,
            tx_bytes=100, rx_bytes=10, attempt_min=0, attempt_max=1,
            batch_fingerprint=transport,
        )

    transports = report(path)["trend_transports"]
    assert transports["local_trend_upload"]["failure_count"] == 1
    assert transports["legacy_trend_upload"]["success_count"] == 1
    assert transports["local_trend_upload"]["sample_count"] == 3


def test_network_traffic_cli_prints_valid_json_and_exits(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "edge.db"
    initialize_database(path)
    record(path, "heartbeat", tx_bytes=7, rx_bytes=11, success=True)
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f"gateway_id: GW001\nsite_id: demo-site\ncloud_url: https://cloud.example\nsqlite_path: {path}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["iot-cx-agent", "--config", str(config_path), "--network-traffic"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["application_bytes_not_wire_tls_bytes"] is True
    assert payload["periods"]["rolling_30d"]["heartbeat"]["tx_bytes"] == 7
