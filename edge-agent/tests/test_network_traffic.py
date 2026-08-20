from datetime import datetime, timedelta, timezone
from pathlib import Path

from iot_cx_agent.db import initialize_database
from iot_cx_agent.network_traffic import CATEGORIES, record, report


def test_counts_rollup_persist_and_report_all_categories(tmp_path: Path) -> None:
    path = tmp_path / "edge.db"
    initialize_database(path)
    now = datetime(2026, 8, 19, 12, 4, tzinfo=timezone.utc)
    record(path, "heartbeat", tx_bytes=12, rx_bytes=34, success=True, now=now)
    record(path, "jobs_poll", tx_bytes=5, rx_bytes=6, success=False, now=now)
    record(path, "tunnel_handshake", tx_bytes=7, rx_bytes=0, success=False, tunnel_attempt=True, now=now)
    result = report(path, now=now)
    hour = result["periods"]["current_hour"]
    assert set(hour) == set(CATEGORIES)
    assert hour["heartbeat"] == {"tx_bytes": 12, "rx_bytes": 34, "request_count": 1, "success_count": 1, "failure_count": 0}
    assert hour["jobs_poll"]["failure_count"] == 1
    assert result["recent_buckets"][0]["tunnel_attempt_count"] == 1
    assert "Authorization" not in str(result)


def test_bucket_rollover_and_30_day_pruning(tmp_path: Path) -> None:
    path = tmp_path / "edge.db"; initialize_database(path)
    now = datetime(2026, 8, 19, 12, 5, tzinfo=timezone.utc)
    record(path, "heartbeat", tx_bytes=1, now=now - timedelta(minutes=1))
    record(path, "heartbeat", tx_bytes=2, now=now)
    record(path, "heartbeat", tx_bytes=3, now=now - timedelta(days=31))
    record(path, "heartbeat", tx_bytes=4, now=now)
    result = report(path, now=now)
    assert len(result["recent_buckets"]) == 2
    assert result["periods"]["rolling_30d"]["heartbeat"]["tx_bytes"] == 7


def test_accounting_failure_is_fail_open(tmp_path: Path, monkeypatch) -> None:
    from iot_cx_agent import network_traffic
    path = tmp_path / "edge.db"; initialize_database(path)
    monkeypatch.setattr(network_traffic, "record_network_traffic", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    network_traffic.record(path, "heartbeat", tx_bytes=1, success=True)
