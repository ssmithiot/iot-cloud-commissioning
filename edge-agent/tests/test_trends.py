from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3

import pytest
import requests

from iot_cx_agent.config import AgentConfig, load_config
from iot_cx_agent.db import initialize_database, pending_trend_samples, queue_trend_sample, trend_upload_attempt_count
import iot_cx_agent.main as agent_main
from iot_cx_agent.main import run_once
from iot_cx_agent.trends import sample_configured_trends, sample_local_edge_trends, upload_pending_trend_samples


def config(tmp_path: Path, **overrides: object) -> AgentConfig:
    values: dict[str, object] = {
        "gateway_id": "GW001",
        "site_id": "demo-site",
        "cloud_url": "https://cloud.example.test",
        "gateway_api_token": "iotcc_gw_prefix_secret",
        "sqlite_path": tmp_path / "edge.db",
        "trend_upload_batch_size": 2,
        "trend_upload_retry_base_sec": 30,
        "trend_upload_retry_max_sec": 120,
    }
    values.update(overrides)
    return AgentConfig(**values)


class Response:
    def __init__(self, payload: object | None = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


EDGE_TREND_SCHEMA = """
CREATE TABLE trend_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    interval_sec INTEGER NOT NULL CHECK(interval_sec >= 30),
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE trend_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    device_profile_id TEXT NOT NULL,
    device_instance INTEGER NOT NULL,
    object_type TEXT NOT NULL,
    object_instance INTEGER NOT NULL,
    object_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(group_id, device_instance, object_type, object_instance),
    FOREIGN KEY(group_id) REFERENCES trend_groups(id) ON DELETE CASCADE
);
CREATE TABLE trend_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    requested_count INTEGER NOT NULL DEFAULT 0,
    returned_count INTEGER NOT NULL DEFAULT 0,
    deferred_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    cpu_load_pct REAL,
    memory_used_pct REAL,
    network_rx_bytes INTEGER,
    network_tx_bytes INTEGER,
    error_text TEXT,
    FOREIGN KEY(group_id) REFERENCES trend_groups(id) ON DELETE CASCADE
);
CREATE TABLE trend_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trend_point_id INTEGER NOT NULL,
    sampled_at TEXT NOT NULL,
    value_text TEXT,
    status TEXT NOT NULL,
    read_source TEXT,
    error_text TEXT,
    FOREIGN KEY(trend_point_id) REFERENCES trend_points(id) ON DELETE CASCADE
);
CREATE TABLE trend_upload_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    trend_sample_id INTEGER NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'uploaded')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    uploaded_at TEXT,
    FOREIGN KEY(trend_sample_id) REFERENCES trend_samples(id) ON DELETE CASCADE
);
"""


def edge_trends_db(tmp_path: Path) -> Path:
    edge_data = tmp_path / "edge-ui-data"
    edge_data.mkdir()
    db_path = edge_data / "edge-trends.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(EDGE_TREND_SCHEMA)
    return db_path


def add_local_group(
    db_path: Path,
    *,
    enabled: bool = True,
    interval_sec: int = 60,
    name: str = "Local AHU",
    points: list[tuple[int, str, int]] | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    points = points or [(1103, "analog-value", 7)]
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO trend_groups (name, interval_sec, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, interval_sec, int(enabled), now, now),
        )
        group_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO trend_points (group_id, device_profile_id, device_instance, object_type, object_instance, object_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (group_id, f"profile-{device}", device, object_type, instance, f"{object_type} {instance}", now)
                for device, object_type, instance in points
            ],
        )
    return group_id


def fetch_rows(db_path: Path, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def test_config_defaults_local_edge_trends_to_enabled_when_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
gateway_id: GW001
site_id: demo-site
cloud_url: https://cloud.example.test
gateway_api_token: token
edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data
""",
        encoding="utf-8",
    )

    agent_config = load_config(config_path)

    assert agent_config.edge_ui_data_dir == Path("/home/swadmin/edge-bacnet-ui-v2/data")
    # 0.2.0 ships local Edge trends on; 0.1.9 shipped them off.
    assert agent_config.local_edge_trends_enabled is True


def test_config_parses_explicit_local_edge_trends_flag(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
gateway_id: GW001
site_id: demo-site
cloud_url: https://cloud.example.test
gateway_api_token: token
local_edge_trends_enabled: true
""",
        encoding="utf-8",
    )

    assert load_config(config_path).local_edge_trends_enabled is True


def test_config_string_false_keeps_local_edge_trends_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
gateway_id: GW001
site_id: demo-site
cloud_url: https://cloud.example.test
gateway_api_token: token
local_edge_trends_enabled: "false"
""",
        encoding="utf-8",
    )

    assert load_config(config_path).local_edge_trends_enabled is False


def latest_started_at(db_path: Path, group_id: int) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT MAX(started_at) FROM trend_runs WHERE group_id=?", (group_id,)).fetchone()
    return str(row[0])


def test_upload_pending_trend_samples_uses_bounded_batch_and_marks_success(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    initialize_database(agent_config.sqlite_path)
    for index in range(3):
        queue_trend_sample(
            agent_config.sqlite_path,
            {"point_id": f"point-{index}", "sampled_at": f"2026-07-12T12:00:0{index}+00:00", "value": str(index)},
            f"2026-07-12T12:00:0{index}+00:00",
        )
    sent: list[object] = []

    def fake_post(url: str, **kwargs: object) -> Response:
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)

    assert upload_pending_trend_samples(agent_config) == 2
    assert len(sent) == 1
    assert len(sent[0]) == 2
    assert len(pending_trend_samples(agent_config.sqlite_path)) == 1


def test_failed_trend_upload_records_attempt_and_defers_retry(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    initialize_database(agent_config.sqlite_path)
    queue_trend_sample(
        agent_config.sqlite_path,
        {"point_id": "point-1", "sampled_at": "2026-07-12T12:00:00+00:00", "value": "72.5"},
        "2026-07-12T12:00:00+00:00",
    )

    def failing_post(*args: object, **kwargs: object) -> Response:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", failing_post)

    with pytest.raises(requests.ConnectionError, match="offline"):
        upload_pending_trend_samples(agent_config)

    queued = pending_trend_samples(agent_config.sqlite_path, now="2026-07-12T00:00:00+00:00")
    assert queued == []
    all_rows = pending_trend_samples(agent_config.sqlite_path)
    assert len(all_rows) == 1
    assert trend_upload_attempt_count(agent_config.sqlite_path, [all_rows[0][0]]) == 1


def test_sampling_queues_only_successful_due_points_within_backlog_limit(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path, trend_queue_max_pending_samples=1)
    initialize_database(agent_config.sqlite_path)
    trend_configs = [
        {"point_id": "point-1", "device_instance": 1001, "object_type": "analog-value", "object_instance": 1, "interval_sec": 60},
        {"point_id": "point-2", "device_instance": 1001, "object_type": "analog-value", "object_instance": 2, "interval_sec": 60},
    ]

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(trend_configs))
    monkeypatch.setattr(
        "iot_cx_agent.trends.run_bacnet_read_bulk",
        lambda *args, **kwargs: (
            {
                "values": [
                    {"saved_point_id": "point-1", "status": "ok", "value": "71.0"},
                    {"saved_point_id": "point-2", "status": "ok", "value": "72.0"},
                ]
            },
            None,
        ),
    )

    assert sample_configured_trends(agent_config) == 1
    queued = pending_trend_samples(agent_config.sqlite_path)
    assert len(queued) == 1
    assert queued[0][1]["point_id"] == "point-1"


def test_sampling_logs_safe_route_diagnostics(tmp_path: Path, monkeypatch, caplog) -> None:
    agent_config = config(tmp_path)
    initialize_database(agent_config.sqlite_path)
    trend_configs = [
        {"point_id": "point-1", "device_instance": 1103, "object_type": "analog-value", "object_instance": 7, "interval_sec": 60},
    ]

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(trend_configs))
    monkeypatch.setattr(
        "iot_cx_agent.trends.run_bacnet_read_bulk",
        lambda *args, **kwargs: (
            {
                "route_diagnostics": {
                    "device_instance": "1103",
                    "route_classification": "routed-mstp",
                    "router_profile": "basrtb",
                    "local_bacnet_source_port": 47814,
                    "dnet": "202",
                    "dadr": "01",
                    "route_args": ["--mac", "192.168.1.200:47814", "--dnet", "202", "--dadr", "01"],
                    "batches": [{"command_type": "bulk-rpm", "elapsed_time_sec": 0.125, "sanitized_error": ""}],
                },
                "values": [{"saved_point_id": "point-1", "status": "ok", "value": "68.5"}],
            },
            None,
        ),
    )

    with caplog.at_level(logging.INFO, logger="iot-cx-agent"):
        assert sample_configured_trends(agent_config) == 1

    assert "Trend BACnet route diagnostics" in caplog.text
    assert "routed-mstp" in caplog.text
    assert "202" in caplog.text
    assert "iotcc_gw_prefix_secret" not in caplog.text


def test_enabled_local_group_is_discovered_and_first_run_due(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=True)

    calls: list[dict[str, object]] = []

    def fake_bulk(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
        calls.append(request)
        return {
            "values": [
                {"saved_point_id": str(request["points"][0]["saved_point_id"]), "status": "ok", "value": "71.5", "read_source": "rpm-bulk"}
            ]
        }, None

    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", fake_bulk)

    assert sample_local_edge_trends(agent_config) == 1
    assert calls[0]["device_instance"] == 1103
    assert len(fetch_rows(db_path, "trend_runs")) == 1
    assert fetch_rows(db_path, "trend_samples")[0]["status"] == "ok"


def test_disabled_local_group_is_skipped(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=False)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=True)

    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda *args, **kwargs: pytest.fail("disabled group ran"))

    assert sample_local_edge_trends(agent_config) == 0
    assert fetch_rows(db_path, "trend_runs") == []


def test_sixty_second_group_is_not_rerun_early_and_runs_after_interval(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    group_id = add_local_group(db_path, interval_sec=60)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=True)
    calls = 0

    def fake_bulk(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
        nonlocal calls
        calls += 1
        return {"values": [{"saved_point_id": str(request["points"][0]["saved_point_id"]), "status": "ok", "value": calls}]}, None

    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", fake_bulk)

    assert sample_local_edge_trends(agent_config) == 1
    assert sample_local_edge_trends(agent_config) == 0
    first_started = latest_started_at(db_path, group_id)
    old_started = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trend_runs SET started_at=? WHERE group_id=?", (old_started, group_id))
    assert sample_local_edge_trends(agent_config) == 1
    assert calls == 2
    assert latest_started_at(db_path, group_id) != first_started


def test_group_added_after_startup_is_discovered_on_next_cycle(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=True)
    monkeypatch.setattr(
        "iot_cx_agent.trends.run_bacnet_read_bulk",
        lambda agent_config, request: (
            {"values": [{"saved_point_id": str(request["points"][0]["saved_point_id"]), "status": "ok", "value": "1"}]},
            None,
        ),
    )

    assert sample_local_edge_trends(agent_config) == 0
    add_local_group(db_path, name="Created later")
    assert sample_local_edge_trends(agent_config) == 1


def test_local_sampler_persists_good_missing_and_error_samples_and_outbox(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(
        db_path,
        points=[
            (1103, "analog-value", 7),
            (1103, "analog-value", 8),
            (1104, "analog-value", 9),
        ],
    )
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=True)

    def fake_bulk(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
        point_ids = [str(point["saved_point_id"]) for point in request["points"]]
        if request["device_instance"] == 1104:
            return {"values": []}, "device timeout"
        return {
            "values": [
                {"saved_point_id": point_ids[0], "status": "ok", "raw_value": "71.0", "read_source": "rpm-bulk"},
                {"saved_point_id": point_ids[1], "status": "missing", "error": "present-value absent", "read_source": "single-fallback"},
            ]
        }, None

    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", fake_bulk)

    assert sample_local_edge_trends(agent_config) == 3
    samples = fetch_rows(db_path, "trend_samples")
    assert [row["status"] for row in samples] == ["ok", "missing", "error"]
    assert samples[0]["value_text"] == "71.0"
    assert samples[1]["error_text"] == "present-value absent"
    assert samples[2]["error_text"] == "device timeout"
    assert len(fetch_rows(db_path, "trend_upload_outbox")) == 3
    run = fetch_rows(db_path, "trend_runs")[0]
    assert run["completed_at"]
    assert run["requested_count"] == 3
    assert run["returned_count"] == 1
    # From 0.2.0 deferred_count counts points that were not read because the
    # BACnet runtime was busy. A missing or failed read is a recorded sample
    # with a quality status, not a deferral.
    assert run["deferred_count"] == 0
    assert "device 1104: device timeout" in run["error_text"]


def test_trend_viewer_query_reads_written_local_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    group_id = add_local_group(db_path)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=True)
    monkeypatch.setattr(
        "iot_cx_agent.trends.run_bacnet_read_bulk",
        lambda agent_config, request: (
            {"values": [{"saved_point_id": str(request["points"][0]["saved_point_id"]), "status": "ok", "raw_value": "68.5", "read_source": "rpm-bulk"}]},
            None,
        ),
    )

    assert sample_local_edge_trends(agent_config) == 1

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT s.*, p.group_id, p.device_instance, p.object_type, p.object_instance, p.object_name
            FROM trend_samples s JOIN trend_points p ON p.id = s.trend_point_id
            WHERE p.group_id = ?
            ORDER BY s.sampled_at DESC, s.id DESC LIMIT 2000
            """,
            (group_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["device_instance"] == 1103
    assert rows[0]["value_text"] == "68.5"


def test_local_sampling_does_not_depend_on_cloud_trend_configs(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, gateway_api_token="token", local_edge_trends_enabled=True)
    initialize_database(agent_config.sqlite_path)

    monkeypatch.setattr("iot_cx_agent.main.send_heartbeat", lambda *args, **kwargs: Response())
    monkeypatch.setattr("iot_cx_agent.main.process_next_job", lambda *args, **kwargs: None)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda agent_config, request: ({"values": [{"saved_point_id": str(request["points"][0]["saved_point_id"]), "status": "ok", "value": "1"}]}, None))
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("cloud offline")))
    posted: list[str] = []

    def record_post(url, *args, **kwargs):
        posted.append(str(url))
        return Response()

    monkeypatch.setattr(requests, "post", record_post)

    assert run_once(agent_config) is True
    assert len(fetch_rows(db_path, "trend_samples")) == 1
    # Sampling used only the gateway's own trend database; the only cloud call
    # is the upward mirror of what was already collected locally.
    assert all(url.endswith("/local-trend-samples") for url in posted)


def test_local_sampler_runs_when_flag_is_absent_from_config(tmp_path: Path, monkeypatch) -> None:
    """0.2.0 release identity: an agent.yaml with no trend flag collects trends."""
    config_path = tmp_path / "agent.yaml"
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    config_path.write_text(
        f"""
gateway_id: GW001
site_id: demo-site
cloud_url: https://cloud.example.test
gateway_api_token: token
edge_ui_data_dir: {db_path.parent}
sqlite_path: {tmp_path / 'edge.db'}
""",
        encoding="utf-8",
    )
    agent_config = load_config(config_path)

    monkeypatch.setattr(
        "iot_cx_agent.trends.run_bacnet_read_bulk",
        lambda agent_config, request: (
            {"values": [{"saved_point_id": str(point["saved_point_id"]), "status": "ok", "value": "1"} for point in request["points"]]},
            None,
        ),
    )

    assert agent_config.local_edge_trends_enabled is True
    assert sample_local_edge_trends(agent_config) > 0


def test_local_sampler_not_invoked_when_flag_is_false(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=False)
    initialize_database(agent_config.sqlite_path)

    monkeypatch.setattr("iot_cx_agent.main.send_heartbeat", lambda *args, **kwargs: Response())
    monkeypatch.setattr("iot_cx_agent.main.sample_local_edge_trends", lambda *args, **kwargs: pytest.fail("local sampler invoked"))
    monkeypatch.setattr("iot_cx_agent.main.sample_configured_trends", lambda *args, **kwargs: 0)
    monkeypatch.setattr("iot_cx_agent.main.upload_pending_trend_samples", lambda *args, **kwargs: 0)
    monkeypatch.setattr("iot_cx_agent.main.process_next_job", lambda *args, **kwargs: None)

    assert run_once(agent_config) is True
    assert fetch_rows(db_path, "trend_runs") == []
    assert fetch_rows(db_path, "trend_samples") == []


def test_disabled_flag_beats_a_configured_edge_ui_data_dir(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=False)

    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda *args, **kwargs: pytest.fail("BACnet command ran"))

    assert sample_local_edge_trends(agent_config) == 0
    assert fetch_rows(db_path, "trend_runs") == []
    assert fetch_rows(db_path, "trend_samples") == []


def test_enabled_local_ui_group_does_not_issue_bacnet_while_disabled(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = config(tmp_path, edge_ui_data_dir=db_path.parent, local_edge_trends_enabled=False)
    initialize_database(agent_config.sqlite_path)

    monkeypatch.setattr("iot_cx_agent.main.send_heartbeat", lambda *args, **kwargs: Response())
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda *args, **kwargs: pytest.fail("BACnet command ran"))
    monkeypatch.setattr("iot_cx_agent.main.sample_configured_trends", lambda *args, **kwargs: 0)
    monkeypatch.setattr("iot_cx_agent.main.upload_pending_trend_samples", lambda *args, **kwargs: 0)
    monkeypatch.setattr("iot_cx_agent.main.process_next_job", lambda *args, **kwargs: None)

    assert run_once(agent_config) is True
    assert fetch_rows(db_path, "trend_runs") == []
    assert fetch_rows(db_path, "trend_samples") == []


def test_cloud_sampler_remains_unchanged(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    initialize_database(agent_config.sqlite_path)
    trend_configs = [
        {"point_id": "cloud-point", "device_instance": 1001, "object_type": "analog-value", "object_instance": 1, "interval_sec": 60},
    ]
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(trend_configs))
    monkeypatch.setattr(
        "iot_cx_agent.trends.run_bacnet_read_bulk",
        lambda *args, **kwargs: ({"values": [{"saved_point_id": "cloud-point", "status": "ok", "value": "72.0"}]}, None),
    )

    assert sample_configured_trends(agent_config) == 1
    assert pending_trend_samples(agent_config.sqlite_path)[0][1]["point_id"] == "cloud-point"
