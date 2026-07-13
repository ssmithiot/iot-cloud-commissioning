from pathlib import Path

import iot_cx_agent.db as edge_db
from iot_cx_agent.db import (
    initialize_database,
    local_job,
    queued_upload_count,
    record_claimed_job,
    record_heartbeat_attempt,
    record_job_result,
    queue_trend_sample,
    pending_trend_samples,
    mark_trend_samples_uploaded,
    record_trend_upload_failure,
    trend_queue_status,
    trend_upload_attempt_count,
)


def test_initialize_database_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "edge.db"

    initialize_database(db_path)
    record_heartbeat_attempt(db_path, "2026-06-19T00:00:00+00:00", True, status_code=200)

    assert db_path.exists()
    assert queued_upload_count(db_path) == 0


def test_edge_database_uses_sqlite_only() -> None:
    assert edge_db.sqlite3.__name__ == "sqlite3"
    assert not hasattr(edge_db, "create_engine")


def test_job_history_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "edge.db"
    job = {"job_id": "job-1", "job_type": "echo", "request": {"message": "hello edge"}}

    initialize_database(db_path)
    record_claimed_job(db_path, job, "2026-06-19T00:00:00+00:00")
    record_job_result(
        db_path,
        "job-1",
        "completed",
        "2026-06-19T00:00:01+00:00",
        result={"echo": True},
    )

    stored = local_job(db_path, "job-1")
    assert stored is not None
    assert stored["job_id"] == "job-1"
    assert stored["status"] == "completed"
    assert stored["result_json"] == '{"echo": true}'


def test_trend_sample_queue_is_durable_until_uploaded(tmp_path: Path) -> None:
    db_path = tmp_path / "edge.db"
    initialize_database(db_path)
    queue_trend_sample(db_path, {"point_id": "point-1", "sampled_at": "2026-07-11T12:00:00+00:00", "value": "72.5"}, "2026-07-11T12:00:00+00:00")

    pending = pending_trend_samples(db_path)

    assert queued_upload_count(db_path) == 1
    assert pending[0][1]["value"] == "72.5"
    mark_trend_samples_uploaded(db_path, [pending[0][0]], "2026-07-11T12:01:00+00:00")
    assert queued_upload_count(db_path) == 0


def test_trend_queue_is_bounded_and_retries_after_backoff(tmp_path: Path) -> None:
    db_path = tmp_path / "edge.db"
    initialize_database(db_path)
    first = {"point_id": "point-1", "sampled_at": "2026-07-11T12:00:00+00:00", "value": "72.5"}
    second = {"point_id": "point-2", "sampled_at": "2026-07-11T12:00:00+00:00", "value": "73.5"}

    assert queue_trend_sample(db_path, first, first["sampled_at"], max_pending=1) is True
    assert queue_trend_sample(db_path, second, second["sampled_at"], max_pending=1) is False
    pending = pending_trend_samples(db_path, now="2026-07-11T12:00:00+00:00")
    row_id = pending[0][0]
    record_trend_upload_failure(
        db_path,
        [row_id],
        error="network unavailable",
        retry_at="2026-07-11T12:01:00+00:00",
        updated_at="2026-07-11T12:00:00+00:00",
    )

    assert trend_upload_attempt_count(db_path, [row_id]) == 1
    assert pending_trend_samples(db_path, now="2026-07-11T12:00:30+00:00") == []
    assert pending_trend_samples(db_path, now="2026-07-11T12:01:00+00:00")[0][0] == row_id


def test_trend_queue_status_separates_deferred_samples(tmp_path: Path) -> None:
    db_path = tmp_path / "edge.db"
    initialize_database(db_path)
    queue_trend_sample(db_path, {"point_id": "point-1", "value": "72"}, "2026-07-12T00:00:00+00:00")
    queue_trend_sample(db_path, {"point_id": "point-2", "value": "73"}, "2026-07-12T00:01:00+00:00")
    ids = [item_id for item_id, _ in pending_trend_samples(db_path)]
    record_trend_upload_failure(
        db_path,
        [ids[1]],
        error="cloud offline",
        retry_at="2026-07-12T01:00:00+00:00",
        updated_at="2026-07-12T00:02:00+00:00",
    )

    assert trend_queue_status(db_path, now="2026-07-12T00:30:00+00:00") == {
        "pending_count": 2,
        "deferred_count": 1,
        "oldest_pending_at": "2026-07-12T00:00:00+00:00",
        "max_attempt_count": 1,
    }
