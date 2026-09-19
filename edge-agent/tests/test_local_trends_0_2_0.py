"""Local Edge trend behaviour: non-interference and durable collection.

The 0.1.9 release disabled local trends because trend collection had to be
proven unable to slow live BACnet work. These tests pin the two properties that
made enabling them acceptable:

* trend reads yield the BACnet runtime to operator reads and writes, and are
  bounded, and
* samples collected locally remain durable while Cloud transport is suspended,
  surviving an Agent restart without upload or mutation.
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


def test_collected_samples_remain_pending_while_cloud_transport_is_suspended(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("local trend upload request ran"))

    assert upload_pending_local_trend_samples(agent_config) == 0
    outbox = fetch_rows(db_path, "trend_upload_outbox")[0]
    assert outbox["state"] == "pending"
    assert outbox["uploaded_at"] is None
    assert outbox["attempt_count"] == 0
    assert len(fetch_rows(db_path, "trend_samples")) == 1


def test_repeated_direct_upload_calls_make_zero_requests(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("local trend upload request ran"))

    assert upload_pending_local_trend_samples(agent_config) == 0
    assert upload_pending_local_trend_samples(agent_config, force=True) == 0
    assert fetch_rows(db_path, "trend_upload_outbox")[0]["state"] == "pending"


def test_retry_only_cannot_upload_or_mutate_pending_row(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trend_upload_outbox SET attempt_count=1287, next_attempt_at='2000-01-01T00:00:00+00:00', last_error='existing failure'")
        conn.commit()
    before = dict(fetch_rows(db_path, "trend_upload_outbox")[0])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("local trend retry request ran"))

    assert upload_pending_local_trend_samples(agent_config, retry_only=True) == 0
    assert dict(fetch_rows(db_path, "trend_upload_outbox")[0]) == before


def test_suspension_does_not_schedule_a_new_retry_or_delete_history(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    agent_config = collect_one(tmp_path, db_path, monkeypatch)

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("local trend upload request ran"))
    assert upload_pending_local_trend_samples(agent_config) == 0

    outbox = fetch_rows(db_path, "trend_upload_outbox")[0]
    assert outbox["state"] == "pending"
    assert outbox["attempt_count"] == 0
    assert outbox["next_attempt_at"] is None
    assert outbox["last_error"] is None
    assert len(fetch_rows(db_path, "trend_samples")) == 1


def test_suspension_does_not_split_or_quarantine_pending_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", 7), (1103, "analog-value", 8)])
    agent_config = collect_one(tmp_path, db_path, monkeypatch)

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("recursive split request ran"))
    assert upload_pending_local_trend_samples(agent_config) == 0
    assert [row["state"] for row in fetch_rows(db_path, "trend_upload_outbox")] == ["pending", "pending"]


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


def test_backlog_stays_pending_even_when_cloud_would_accept_it(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, interval_sec=30)
    agent_config = enabled_config(tmp_path, db_path, trend_local_upload_batch_size=100)

    monkeypatch.setattr("iot_cx_agent.trends.bacnet_runtime_lock_held", lambda *args, **kwargs: False)
    monkeypatch.setattr("iot_cx_agent.trends.run_bacnet_read_bulk", ok_bulk)
    for _ in range(3):
        sample_local_edge_trends(agent_config)
        _age_last_run(db_path)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("backlog upload request ran"))

    assert upload_pending_local_trend_samples(agent_config) == 0
    assert all(row["state"] == "pending" for row in fetch_rows(db_path, "trend_upload_outbox"))


def test_backlog_survives_an_agent_restart(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True)
    collect_one(tmp_path, db_path, monkeypatch)

    # A restart is a brand new config object reading the same on-disk outbox.
    restarted = enabled_config(tmp_path, db_path)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("restart triggered upload"))

    assert upload_pending_local_trend_samples(restarted) == 0
    assert fetch_rows(db_path, "trend_upload_outbox")[0]["state"] == "pending"


def test_suspension_ignores_batch_size_and_preserves_every_row(tmp_path: Path, monkeypatch) -> None:
    db_path = edge_trends_db(tmp_path)
    add_local_group(db_path, enabled=True, points=[(1103, "analog-value", index) for index in range(10)])
    agent_config = collect_one(tmp_path, db_path, monkeypatch, trend_local_upload_batch_size=4)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("batch upload request ran"))

    assert upload_pending_local_trend_samples(agent_config) == 0
    assert len(fetch_rows(db_path, "trend_upload_outbox")) == 10


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
