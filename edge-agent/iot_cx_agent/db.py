import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    status_code INTEGER,
    error TEXT,
    response_body TEXT
);

CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edge_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_key TEXT NOT NULL UNIQUE,
    display_name TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL,
    sample_value TEXT,
    sample_time_utc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(point_id) REFERENCES edge_points(id)
);

CREATE TABLE IF NOT EXISTS trend_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_sec INTEGER NOT NULL DEFAULT 300,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(point_id) REFERENCES edge_points(id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT,
    payload_json TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_network_traffic (
    bucket_start_utc TEXT PRIMARY KEY,
    heartbeat_tx INTEGER NOT NULL DEFAULT 0, heartbeat_rx INTEGER NOT NULL DEFAULT 0, heartbeat_requests INTEGER NOT NULL DEFAULT 0, heartbeat_successes INTEGER NOT NULL DEFAULT 0, heartbeat_failures INTEGER NOT NULL DEFAULT 0,
    jobs_poll_tx INTEGER NOT NULL DEFAULT 0, jobs_poll_rx INTEGER NOT NULL DEFAULT 0, jobs_poll_requests INTEGER NOT NULL DEFAULT 0, jobs_poll_successes INTEGER NOT NULL DEFAULT 0, jobs_poll_failures INTEGER NOT NULL DEFAULT 0,
    trend_poll_tx INTEGER NOT NULL DEFAULT 0, trend_poll_rx INTEGER NOT NULL DEFAULT 0, trend_poll_requests INTEGER NOT NULL DEFAULT 0, trend_poll_successes INTEGER NOT NULL DEFAULT 0, trend_poll_failures INTEGER NOT NULL DEFAULT 0,
    trend_upload_tx INTEGER NOT NULL DEFAULT 0, trend_upload_rx INTEGER NOT NULL DEFAULT 0, trend_upload_requests INTEGER NOT NULL DEFAULT 0, trend_upload_successes INTEGER NOT NULL DEFAULT 0, trend_upload_failures INTEGER NOT NULL DEFAULT 0,
    job_result_tx INTEGER NOT NULL DEFAULT 0, job_result_rx INTEGER NOT NULL DEFAULT 0, job_result_requests INTEGER NOT NULL DEFAULT 0, job_result_successes INTEGER NOT NULL DEFAULT 0, job_result_failures INTEGER NOT NULL DEFAULT 0,
    tunnel_lease_tx INTEGER NOT NULL DEFAULT 0, tunnel_lease_rx INTEGER NOT NULL DEFAULT 0, tunnel_lease_requests INTEGER NOT NULL DEFAULT 0, tunnel_lease_successes INTEGER NOT NULL DEFAULT 0, tunnel_lease_failures INTEGER NOT NULL DEFAULT 0,
    tunnel_handshake_tx INTEGER NOT NULL DEFAULT 0, tunnel_handshake_rx INTEGER NOT NULL DEFAULT 0, tunnel_handshake_requests INTEGER NOT NULL DEFAULT 0, tunnel_handshake_successes INTEGER NOT NULL DEFAULT 0, tunnel_handshake_failures INTEGER NOT NULL DEFAULT 0,
    tunnel_payload_tx INTEGER NOT NULL DEFAULT 0, tunnel_payload_rx INTEGER NOT NULL DEFAULT 0, tunnel_payload_requests INTEGER NOT NULL DEFAULT 0, tunnel_payload_successes INTEGER NOT NULL DEFAULT 0, tunnel_payload_failures INTEGER NOT NULL DEFAULT 0,
    other_agent_tx INTEGER NOT NULL DEFAULT 0, other_agent_rx INTEGER NOT NULL DEFAULT 0, other_agent_requests INTEGER NOT NULL DEFAULT 0, other_agent_successes INTEGER NOT NULL DEFAULT 0, other_agent_failures INTEGER NOT NULL DEFAULT 0,
    tunnel_attempt_count INTEGER NOT NULL DEFAULT 0
);
"""

JOB_COLUMNS = {
    "job_id": "TEXT",
    "request_json": "TEXT",
    "error_message": "TEXT",
    "claimed_at": "TEXT",
    "completed_at": "TEXT",
}

SYNC_QUEUE_COLUMNS = {
    "last_error": "TEXT",
    "next_attempt_at": "TEXT",
}


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        ensure_jobs_columns(conn)
        ensure_sync_queue_columns(conn)
        conn.commit()


def ensure_jobs_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for column_name, column_def in JOB_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {column_def}")


def ensure_sync_queue_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sync_queue)").fetchall()}
    for column_name, column_def in SYNC_QUEUE_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE sync_queue ADD COLUMN {column_name} {column_def}")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def record_network_traffic(path: Path, category: str, *, tx_bytes: int, rx_bytes: int, success: bool | None, tunnel_attempt: bool, now: object | None = None) -> None:
    if category not in {"heartbeat", "jobs_poll", "trend_poll", "trend_upload", "job_result", "tunnel_lease", "tunnel_handshake", "tunnel_payload", "other_agent"}:
        raise ValueError("unknown network category")
    from datetime import datetime, timedelta, timezone
    current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc).replace(second=0, microsecond=0)
    bucket = current - timedelta(minutes=current.minute % 5)
    prefix = category
    with connect(path) as conn:
        conn.execute("INSERT OR IGNORE INTO agent_network_traffic (bucket_start_utc) VALUES (?)", (bucket.isoformat(),))
        conn.execute(f"UPDATE agent_network_traffic SET {prefix}_tx={prefix}_tx+?, {prefix}_rx={prefix}_rx+?, {prefix}_requests={prefix}_requests+1, {prefix}_successes={prefix}_successes+?, {prefix}_failures={prefix}_failures+?, tunnel_attempt_count=tunnel_attempt_count+? WHERE bucket_start_utc=?", (tx_bytes, rx_bytes, int(success is True), int(success is False), int(tunnel_attempt), bucket.isoformat()))
        cutoff = (current - timedelta(days=30)).isoformat()
        conn.execute("DELETE FROM agent_network_traffic WHERE bucket_start_utc < ?", (cutoff,))
        conn.commit()


def network_traffic_report(path: Path, *, now: object | None = None) -> dict[str, object]:
    from datetime import datetime, timedelta, timezone
    current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM agent_network_traffic ORDER BY bucket_start_utc DESC").fetchall()
    periods = {"current_hour": current.replace(minute=0, second=0, microsecond=0), "current_day": current.replace(hour=0, minute=0, second=0, microsecond=0), "rolling_24h": current-timedelta(hours=24), "rolling_7d": current-timedelta(days=7), "rolling_30d": current-timedelta(days=30)}
    def totals(since):
        selected = [r for r in rows if str(r["bucket_start_utc"]) >= since.isoformat()]
        return {c: {"tx_bytes": sum(r[f"{c}_tx"] for r in selected), "rx_bytes": sum(r[f"{c}_rx"] for r in selected), "request_count": sum(r[f"{c}_requests"] for r in selected), "success_count": sum(r[f"{c}_successes"] for r in selected), "failure_count": sum(r[f"{c}_failures"] for r in selected)} for c in ("heartbeat","jobs_poll","trend_poll","trend_upload","job_result","tunnel_lease","tunnel_handshake","tunnel_payload","other_agent")}
    return {"application_bytes_not_wire_tls_bytes": True, "periods": {name: totals(since) for name, since in periods.items()}, "recent_buckets": [dict(r) for r in rows[:24]]}


def queued_upload_count(path: Path) -> int:
    with connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM sync_queue WHERE status = 'pending'").fetchone()
        return int(row["count"])


def trend_queue_status(path: Path, *, now: str | None = None) -> dict[str, int | str | None]:
    """Return small, non-sensitive trend backlog diagnostics for the heartbeat."""
    current_time = now or "9999-12-31T23:59:59+00:00"
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS pending_count,
                SUM(CASE WHEN next_attempt_at IS NOT NULL AND next_attempt_at > ? THEN 1 ELSE 0 END) AS deferred_count,
                MIN(created_at) AS oldest_pending_at,
                MAX(attempt_count) AS max_attempt_count
            FROM sync_queue
            WHERE status = 'pending' AND item_type = 'trend_sample'
            """,
            (current_time,),
        ).fetchone()
    return {
        "pending_count": int(row["pending_count"]),
        "deferred_count": int(row["deferred_count"] or 0),
        "oldest_pending_at": None if row["oldest_pending_at"] is None else str(row["oldest_pending_at"]),
        "max_attempt_count": int(row["max_attempt_count"] or 0),
    }


def record_heartbeat_attempt(
    path: Path,
    attempted_at: str,
    success: bool,
    status_code: int | None = None,
    error: str | None = None,
    response_body: str | None = None,
) -> None:
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO heartbeat_log (attempted_at, success, status_code, error, response_body)
            VALUES (?, ?, ?, ?, ?)
            """,
            (attempted_at, int(success), status_code, error, response_body),
        )
        conn.execute(
            """
            INSERT INTO agent_state (key, value, updated_at)
            VALUES ('last_heartbeat_success', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("true" if success else "false", attempted_at),
        )
        conn.commit()


def trend_last_sample_at(path: Path, point_id: str) -> str | None:
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM agent_state WHERE key = ?", (f"trend-last:{point_id}",)).fetchone()
        return None if row is None else str(row["value"])


def queue_trend_sample(path: Path, sample: dict[str, object], sampled_at: str, *, max_pending: int = 10_000) -> bool:
    """Persist a trend sample unless the bounded local backlog is full."""
    if max_pending < 1:
        raise ValueError("max_pending must be positive")
    payload = json.dumps(sample, sort_keys=True)
    with connect(path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS count FROM sync_queue WHERE status = 'pending' AND item_type = 'trend_sample'"
        ).fetchone()
        if int(pending["count"]) >= max_pending:
            return False
        conn.execute(
            "INSERT INTO sync_queue (item_type, payload_json, status, created_at, updated_at) VALUES ('trend_sample', ?, 'pending', ?, ?)",
            (payload, sampled_at, sampled_at),
        )
        conn.execute(
            "INSERT INTO agent_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (f"trend-last:{sample['point_id']}", sampled_at, sampled_at),
        )
        conn.commit()
    return True


def pending_trend_samples(path: Path, limit: int = 100, *, now: str | None = None) -> list[tuple[int, dict[str, object]]]:
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json FROM sync_queue
            WHERE status = 'pending' AND item_type = 'trend_sample'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id LIMIT ?
            """,
            (now or "9999-12-31T23:59:59+00:00", limit),
        ).fetchall()
    return [(int(row["id"]), json.loads(str(row["payload_json"]))) for row in rows]


def mark_trend_samples_uploaded(path: Path, ids: list[int], updated_at: str) -> None:
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    with connect(path) as conn:
        conn.execute(
            f"UPDATE sync_queue SET status = 'uploaded', updated_at = ?, last_error = NULL, next_attempt_at = NULL WHERE id IN ({marks})",
            [updated_at, *ids],
        )
        conn.commit()


def trend_upload_attempt_count(path: Path, ids: list[int]) -> int:
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with connect(path) as conn:
        row = conn.execute(
            f"SELECT COALESCE(MAX(attempt_count), 0) AS count FROM sync_queue WHERE id IN ({marks})",
            ids,
        ).fetchone()
    return int(row["count"])


def record_trend_upload_failure(path: Path, ids: list[int], *, error: str, retry_at: str, updated_at: str) -> None:
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    with connect(path) as conn:
        conn.execute(
            f"""
            UPDATE sync_queue
            SET attempt_count = attempt_count + 1, last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE id IN ({marks}) AND status = 'pending'
            """,
            [error[:1000], retry_at, updated_at, *ids],
        )
        conn.commit()


def record_claimed_job(path: Path, job: dict[str, object], claimed_at: str) -> None:
    request_json = json.dumps(job.get("request", {}), sort_keys=True)
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, status, request_json, payload_json, created_at, claimed_at, updated_at
            )
            VALUES (?, ?, 'claimed', ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                job_type = excluded.job_type,
                status = excluded.status,
                request_json = excluded.request_json,
                payload_json = excluded.payload_json,
                claimed_at = excluded.claimed_at,
                updated_at = excluded.updated_at
            """,
            (
                str(job["job_id"]),
                str(job["job_type"]),
                request_json,
                request_json,
                claimed_at,
                claimed_at,
                claimed_at,
            ),
        )
        conn.commit()


def record_job_result(
    path: Path,
    job_id: str,
    status: str,
    completed_at: str,
    result: dict[str, object] | None = None,
    error_message: str | None = None,
) -> None:
    result_json = json.dumps(result, sort_keys=True) if result is not None else None
    with connect(path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, result_json = ?, error_message = ?, completed_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (status, result_json, error_message, completed_at, completed_at, job_id),
        )
        conn.commit()


def local_job(path: Path, job_id: str) -> sqlite3.Row | None:
    with connect(path) as conn:
        return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
