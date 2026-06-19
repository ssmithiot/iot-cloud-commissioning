from pathlib import Path

from iot_cx_agent.db import (
    initialize_database,
    local_job,
    queued_upload_count,
    record_claimed_job,
    record_heartbeat_attempt,
    record_job_result,
)


def test_initialize_database_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "edge.db"

    initialize_database(db_path)
    record_heartbeat_attempt(db_path, "2026-06-19T00:00:00+00:00", True, status_code=200)

    assert db_path.exists()
    assert queued_upload_count(db_path) == 0


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
