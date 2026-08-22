"""0.2.0 local Edge trend behaviour: non-interference and durable upload.

The 0.1.9 release disabled local trends because trend collection had to be
proven unable to slow live BACnet work. These tests pin the two properties that
made enabling them acceptable:

* trend reads yield the BACnet runtime to operator reads and writes, and are
  bounded, and
* samples collected locally reach the cloud exactly once, surviving a cloud
  outage and an agent restart.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import initialize_database
from iot_cx_agent.trends import (
    local_trend_sync_due,
    schedule_next_local_trend_sync,
    _trend_read_config,
    sample_local_edge_trends,
    upload_pending_local_trend_samples,
)

from tests.test_trends import Response, add_local_group, config, edge_trends_db, fetch_rows


def enabled_config(tmp_path: Path, db_path: Path, **overrides: object) -> AgentConfig:
    values: dict[str, object] = {"edge_ui_data_dir": db_path.parent, "local_edge_trends_enabled": True}
    values.update(overrides)
    return config(tmp_path, **values)


def ok_bulk(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
    return (
        {
            "values": [
                {"saved_point_id": str(point["saved_point_id"]), "status": "ok", "raw_value": "71.0", "read_source": "rpm-bulk"}
                for point in request["points"]
            ]
        },
        None,
    )


# --- non-interference with live BACnet work ---------------------------------


def test_trend_reads_give_up_the_runtime_lock_quickly(tmp_path: Path) -> None:
    db_path = edge_trends_db(tmp_path)
    agent_config = enabled_config(tmp_path, db_path, trend_lock_timeout_sec=2.0)

    read_config = _trend_read_config(agent_config)

    # Operator work keeps the full lock timeout; trend work does not.
    assert agent_config.bacnet_lock_timeout_sec == 30.0
    assert read_config.bacnet_lock_timeout_sec == 2.0
    assert read_config.bacnet_default_port == agent_config.bacnet_default_port
    assert read_config.bacnet_router_profile == agent_config.bacnet_router_profile


def test_no_bacnet_command_runs_while_an_operator_holds_the_runtime(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = enabled_config(tmp_path, db_path)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: True)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda *args, **kwargs: pytest.fail("BACnet command ran"))

    assert sample_local_edge_trends(agent_config) == 0
    # No run row: the group is still due and is retried on the next cycle,
    # rather than losing a whole interval to a busy moment.
    assert fetch_rows(db_path, "trend_runs") == []
    assert fetch_rows(db_path, "trend_samples") == []


def test_group_stays_due_after_deferring_to_live_work(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = enabled_config(tmp_path, db_path)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: True)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda *args, **kwargs: pytest.fail("BACnet command ran"))
    sample_local_edge_trends(agent_config)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", ok_bulk)

    assert sample_local_edge_trends(agent_config) == 1


def test_trend_reads_are_split_into_bounded_batches(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", index) for index in range(20)])
    agent_config = enabled_config(tmp_path, db_path, trend_read_batch_size=5)
    batch_sizes: list[int] = []

    def recording_bulk(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
        batch_sizes.append(len(request["points"]))
        return ok_bulk(agent_config, request)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", recording_bulk)

    assert sample_local_edge_trends(agent_config) == 20
    assert batch_sizes == [5, 5, 5, 5]


def test_trend_reads_yield_between_batches_when_live_work_appears(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", index) for index in range(20)])
    agent_config = enabled_config(tmp_path, db_path, trend_read_batch_size=5)
    calls: list[int] = []

    # Idle for the group's pre-flight check, then busy: the first batch runs
    # and the between-batch check hands the runtime back.
    busy_states = iter([False, True])

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: next(busy_states, True))

    def recording_bulk(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
        calls.append(len(request["points"]))
        return ok_bulk(agent_config, request)

    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", recording_bulk)

    stored = sample_local_edge_trends(agent_config)

    assert calls == [5]
    assert stored == 5
    run = fetch_rows(db_path, "trend_runs")[0]
    assert run["deferred_count"] == 15
    # Deferral is not a reading: no sample rows were invented for the 15 points.
    assert len(fetch_rows(db_path, "trend_samples")) == 5


def test_busy_runtime_mid_run_defers_without_writing_samples(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", index) for index in range(10)])
    agent_config = enabled_config(tmp_path, db_path, trend_read_batch_size=5)
    attempts = {"count": 0}

    def busy_after_first(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return ok_bulk(agent_config, request)
        return {"status": "deferred"}, "bacnet_runtime_busy"

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", busy_after_first)

    assert sample_local_edge_trends(agent_config) == 5
    run = fetch_rows(db_path, "trend_runs")[0]
    assert run["deferred_count"] == 5
    assert len(fetch_rows(db_path, "trend_samples")) == 5


def test_per_cycle_point_budget_is_enforced(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", index) for index in range(20)])
    agent_config = enabled_config(tmp_path, db_path, trend_read_batch_size=5, trend_max_points_per_cycle=10)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", ok_bulk)

    assert sample_local_edge_trends(agent_config) == 10
    assert fetch_rows(db_path, "trend_runs")[0]["deferred_count"] == 10


def test_one_failing_group_does_not_stop_the_others(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, name="Broken", points=[(1103, "analog-value", 1)])
    add_local_group(db_path, enabled=True, name="Healthy", points=[(1104, "analog-value", 2)])
    agent_config = enabled_config(tmp_path, db_path)

    def explode_for_first_device(agent_config: AgentConfig, request: dict[str, object]) -> tuple[dict[str, object], str | None]:
        if request["device_instance"] == 1103:
            raise RuntimeError("controller exploded")
        return ok_bulk(agent_config, request)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", explode_for_first_device)

    stored = sample_local_edge_trends(agent_config)

    assert stored >= 1
    samples = fetch_rows(db_path, "trend_samples")
    # The healthy group still collected, and the failure is recorded rather
    # than swallowed as a good reading.
    assert any(row["status"] == "ok" for row in samples)
    assert any(row["status"] == "error" for row in samples)


def test_disabled_group_issues_no_bacnet_command(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=False)
    agent_config = enabled_config(tmp_path, db_path)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda *args, **kwargs: pytest.fail("BACnet command ran"))

    assert sample_local_edge_trends(agent_config) == 0
    assert fetch_rows(db_path, "trend_runs") == []


def test_group_not_yet_due_issues_no_bacnet_command(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    group_id = add_local_group(db_path, enabled=True, interval_sec=3600)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO trend_runs (group_id, started_at, requested_count) VALUES (?, ?, 1)",
            (group_id, datetime.now(timezone.utc).isoformat()),
        )
    agent_config = enabled_config(tmp_path, db_path)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", lambda *args, **kwargs: pytest.fail("BACnet command ran"))

    assert sample_local_edge_trends(agent_config) == 0


# --- cloud mirror upload ----------------------------------------------------


def collect_one(tmp_path: Path, db_path: Path, monkeypatch, **overrides: object) -> AgentConfig:
    agent_config = enabled_config(tmp_path, db_path, **overrides)
    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", ok_bulk)
    sample_local_edge_trends(agent_config)
    return agent_config


def test_collected_samples_are_uploaded_and_marked(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)
    sent: list[dict] = []

    def capture_post(url, *args, **kwargs):
        sent.append({"url": str(url), "json": kwargs.get("json")})
        return Response()

    monkeypatch.setattr(requests, "post", capture_post)

    assert upload_pending_local_trend_samples(agent_config) == 1
    assert sent[0]["url"].endswith("/api/edge/GW001/local-trend-samples")
    body = sent[0]["json"][0]
    assert body["group_name"] == "Local AHU"
    assert body["device_instance"] == 1103
    assert body["value_text"] == "71.0"
    assert body["status"] == "ok"
    assert body["event_id"]
    outbox = fetch_rows(db_path, "trend_upload_outbox")[0]
    assert outbox["state"] == "uploaded"
    assert outbox["uploaded_at"]


def test_nothing_is_uploaded_twice(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)
    posts: list[object] = []
    monkeypatch.setattr(requests, "post", lambda url, *args, **kwargs: (posts.append(kwargs.get("json")), Response())[1])

    assert upload_pending_local_trend_samples(agent_config) == 1
    assert upload_pending_local_trend_samples(agent_config) == 0
    assert len(posts) == 1


def test_a_retry_resends_the_same_event_ids(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)
    attempts: list[list[str]] = []

    def failing_post(url, *args, **kwargs):
        attempts.append([sample["event_id"] for sample in kwargs.get("json", [])])
        raise requests.ConnectionError("cloud offline")

    monkeypatch.setattr(requests, "post", failing_post)
    with pytest.raises(requests.ConnectionError):
        upload_pending_local_trend_samples(agent_config)

    # Clear the backoff so the retry is eligible in this test.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trend_upload_outbox SET next_attempt_at = NULL")

    monkeypatch.setattr(requests, "post", lambda url, *args, **kwargs: (attempts.append([s["event_id"] for s in kwargs.get("json", [])]), Response())[1])
    assert upload_pending_local_trend_samples(agent_config) == 1

    assert attempts[0] == attempts[1]


def test_cloud_outage_keeps_the_sample_and_schedules_a_retry(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("cloud offline")))
    with pytest.raises(requests.ConnectionError):
        upload_pending_local_trend_samples(agent_config)

    outbox = fetch_rows(db_path, "trend_upload_outbox")[0]
    assert outbox["state"] == "pending"
    assert outbox["attempt_count"] == 1
    assert outbox["next_attempt_at"]
    assert "cloud offline" in outbox["last_error"]
    assert len(fetch_rows(db_path, "trend_samples")) == 1


def test_validation_failure_isolates_bad_row_and_keeps_good_rows_draining(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", 7), (1103, "analog-value", 8)])
    agent_config = collect_one(tmp_path, db_path, monkeypatch)

    def reject_only_second(url, *args, **kwargs):
        payload = kwargs["json"]
        if len(payload) > 1 or payload[0]["object_instance"] == 8:
            return Response(status_code=422)
        return Response()

    monkeypatch.setattr(requests, "post", reject_only_second)
    assert upload_pending_local_trend_samples(agent_config) == 1
    with sqlite3.connect(db_path) as conn:
        states = conn.execute("SELECT state FROM trend_upload_outbox ORDER BY id").fetchall()
        quarantined = conn.execute("SELECT http_status FROM trend_upload_quarantine").fetchall()
    assert states[0][0] == "uploaded"
    assert states[1][0] == "pending"
    assert quarantined == [(422,)]


def test_sync_schedule_is_stable_and_not_due_each_control_cycle(tmp_path: Path) -> None:
    db_path = edge_trends_db(tmp_path)
    agent_config = enabled_config(tmp_path, db_path)
    initialize_database(agent_config.sqlite_path)
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    assert local_trend_sync_due(agent_config, 7_200, now=now) is False
    assert local_trend_sync_due(agent_config, 7_200, now=now + timedelta(seconds=30)) is False
    schedule_next_local_trend_sync(agent_config, 7_200, now=now)


def test_collection_continues_while_the_cloud_is_unreachable(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, interval_sec=30)
    agent_config = enabled_config(tmp_path, db_path)

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("cloud offline")))
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("cloud offline")))
    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", ok_bulk)

    assert sample_local_edge_trends(agent_config) == 1
    _age_last_run(db_path)
    assert sample_local_edge_trends(agent_config) == 1
    assert len(fetch_rows(db_path, "trend_samples")) == 2


def test_backlog_is_uploaded_when_the_cloud_returns(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, interval_sec=30)
    agent_config = enabled_config(tmp_path, db_path, trend_local_upload_batch_size=100)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", ok_bulk)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("cloud offline")))

    for _ in range(3):
        sample_local_edge_trends(agent_config)
        with pytest.raises(requests.ConnectionError):
            upload_pending_local_trend_samples(agent_config)
        _age_last_run(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trend_upload_outbox SET next_attempt_at = NULL")

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    assert upload_pending_local_trend_samples(agent_config) == 3
    assert all(row["state"] == "uploaded" for row in fetch_rows(db_path, "trend_upload_outbox"))


def test_backlog_survives_an_agent_restart(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    collect_one(tmp_path, db_path, monkeypatch)

    # A restart is a brand new config object reading the same on-disk outbox.
    restarted = enabled_config(tmp_path, db_path)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    assert upload_pending_local_trend_samples(restarted) == 1


def test_upload_batch_is_bounded(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", index) for index in range(10)])
    agent_config = collect_one(tmp_path, db_path, monkeypatch, trend_local_upload_batch_size=4)
    sizes: list[int] = []
    monkeypatch.setattr(requests, "post", lambda url, *args, **kwargs: (sizes.append(len(kwargs.get("json", []))), Response())[1])

    assert upload_pending_local_trend_samples(agent_config) == 4
    assert sizes == [4]


def test_upload_is_skipped_when_local_trends_are_disabled(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    collect_one(tmp_path, db_path, monkeypatch)
    disabled = enabled_config(tmp_path, db_path, local_edge_trends_enabled=False)

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("upload ran while disabled"))

    assert upload_pending_local_trend_samples(disabled) == 0


def _age_last_run(db_path: Path) -> None:
    """Backdate the newest run so the group is due again."""
    older = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trend_runs SET started_at = ? WHERE id = (SELECT MAX(id) FROM trend_runs)", (older,))
