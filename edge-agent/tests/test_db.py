from pathlib import Path

from iot_cx_agent.db import initialize_database, queued_upload_count, record_heartbeat_attempt


def test_initialize_database_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "edge.db"

    initialize_database(db_path)
    record_heartbeat_attempt(db_path, "2026-06-19T00:00:00+00:00", True, status_code=200)

    assert db_path.exists()
    assert queued_upload_count(db_path) == 0

