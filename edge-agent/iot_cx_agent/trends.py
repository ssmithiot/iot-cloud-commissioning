from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4

import requests

from iot_cx_agent.bacnet import BACNET_RUNTIME_BUSY, bacnet_runtime_lock_held, run_bacnet_read_bulk
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import (
    get_agent_state,
    mark_trend_samples_uploaded,
    pending_trend_samples,
    queue_trend_sample,
    record_trend_upload_failure,
    record_trend_transport_event,
    trend_upload_attempt_count,
    trend_last_sample_at,
    set_agent_state,
)
from iot_cx_agent.heartbeat import auth_headers


logger = logging.getLogger("iot-cx-agent")
EDGE_TRENDS_DB_NAME = "edge-trends.db"
LOCAL_SYNC_STATE_KEY = "edge-local-trend-next-sync-at"
TREND_CLOUD_UPLOAD_ENABLED = False
TREND_CLOUD_TRANSPORT_SUSPENDED = "trend Cloud transport is suspended"


def trend_cloud_upload_enabled() -> bool:
    """Single release authority for all trend sample transport to Cloud.

    This corrective Agent deliberately has no configuration or heartbeat path
    capable of changing the value. Local sampling and all pending data remain
    active and preserved while transport is suspended.
    """
    return TREND_CLOUD_UPLOAD_ENABLED


def _safe_error(error: object) -> str:
    return str(error or "").replace("\n", " ")[:1000]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _due(config: AgentConfig, trend: dict[str, Any], now: datetime) -> bool:
    previous = trend_last_sample_at(config.sqlite_path, str(trend["point_id"]))
    if previous is None:
        return True
    try:
        last = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - last).total_seconds() >= int(trend["interval_sec"])


def _local_due(last_started_at: object, interval_sec: object, now: datetime) -> bool:
    if not last_started_at:
        return True
    try:
        last = datetime.fromisoformat(str(last_started_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now >= last + timedelta(seconds=int(interval_sec))


def _edge_trends_db(config: AgentConfig) -> Path | None:
    if config.edge_ui_data_dir is None:
        return None
    return config.edge_ui_data_dir / EDGE_TRENDS_DB_NAME


def _connect_edge_trends(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_local_quarantine(conn: sqlite3.Connection) -> None:
    """Keep permanently rejected mirror rows out of the retry queue.

    This side table avoids changing the Edge UI-owned outbox schema (some
    deployed UI databases constrain its state to pending/uploaded), while the
    original sample and outbox row remain permanently available for diagnosis
    or a future manual repair.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_upload_quarantine (
            outbox_id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            http_status INTEGER,
            error_text TEXT NOT NULL,
            quarantined_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL
        )
        """
    )


def _batch_fingerprint(rows: list[sqlite3.Row]) -> str:
    material = ",".join(str(row["event_id"]) for row in rows).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _payload_bytes(payload: object) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))


def _record_transport(
    config: AgentConfig, transport: str, rows: list[sqlite3.Row], payload: object, *, success: bool,
    status: int | None, response: object | None = None,
) -> None:
    try:
        record_trend_transport_event(
            config.sqlite_path, recorded_at=_now().isoformat(), transport=transport, success=success,
            http_status=status, sample_count=len(rows), tx_bytes=_payload_bytes(payload),
            rx_bytes=len(getattr(response, "content", b"") or b""),
            attempt_min=min((int(row["attempt_count"] or 0) for row in rows), default=0),
            attempt_max=max((int(row["attempt_count"] or 0) for row in rows), default=0),
            batch_fingerprint=_batch_fingerprint(rows),
        )
    except Exception:
        logger.exception("Unable to record safe %s telemetry", transport)


def _record_legacy_transport(
    config: AgentConfig, ids: list[int], payload: list[dict[str, object]], prior_attempts: int,
    *, success: bool, status: int | None, response: object | None = None,
) -> None:
    try:
        record_trend_transport_event(
            config.sqlite_path, recorded_at=_now().isoformat(), transport="legacy_trend_upload", success=success,
            http_status=status, sample_count=len(payload), tx_bytes=_payload_bytes(payload),
            rx_bytes=len(getattr(response, "content", b"") or b""), attempt_min=prior_attempts,
            attempt_max=prior_attempts,
            batch_fingerprint=hashlib.sha256(",".join(map(str, ids)).encode("utf-8")).hexdigest()[:16],
        )
    except Exception:
        logger.exception("Unable to record safe legacy_trend_upload telemetry")


def _http_status(exc: requests.RequestException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _retryable_upload_error(exc: requests.RequestException) -> bool:
    status = _http_status(exc)
    return status is None or status == 408 or status == 429 or status >= 500


def _record_local_upload_failure(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], config: AgentConfig, now: datetime, error: str
) -> None:
    prior_attempts = max((int(row["attempt_count"] or 0) for row in rows), default=0)
    retry_seconds = min(
        config.trend_upload_retry_max_sec,
        config.trend_upload_retry_base_sec * (2 ** min(6, max(0, prior_attempts))),
    )
    timestamp = now.isoformat()
    retry_at = (now + timedelta(seconds=retry_seconds)).isoformat()
    conn.executemany(
        """
        UPDATE trend_upload_outbox
        SET attempt_count = attempt_count + 1, next_attempt_at = ?, last_error = ?, updated_at = ?
        WHERE id = ?
        """,
        [(retry_at, _safe_error(error), timestamp, int(row["outbox_id"])) for row in rows],
    )


def _quarantine_local_row(conn: sqlite3.Connection, row: sqlite3.Row, status: int | None, error: str, timestamp: str) -> None:
    _ensure_local_quarantine(conn)
    conn.execute(
        """
        INSERT INTO trend_upload_quarantine
            (outbox_id, event_id, http_status, error_text, quarantined_at, attempt_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(outbox_id) DO UPDATE SET
            http_status=excluded.http_status, error_text=excluded.error_text,
            quarantined_at=excluded.quarantined_at, attempt_count=excluded.attempt_count
        """,
        (int(row["outbox_id"]), str(row["event_id"]), status, _safe_error(error), timestamp, int(row["attempt_count"]) + 1),
    )
    conn.execute(
        "UPDATE trend_upload_outbox SET attempt_count=attempt_count+1, last_error=?, updated_at=? WHERE id=?",
        (_safe_error(error), timestamp, int(row["outbox_id"])),
    )


def _local_upload_payload(rows: list[sqlite3.Row]) -> list[dict[str, object | None]]:
    return [
        {
            "event_id": str(row["event_id"]), "group_name": str(row["group_name"]),
            "device_instance": int(row["device_instance"]), "object_type": str(row["object_type"]),
            "object_instance": int(row["object_instance"]), "object_name": str(row["object_name"] or ""),
            "sampled_at": str(row["sampled_at"]), "value_text": row["value_text"],
            "status": str(row["status"]), "read_source": row["read_source"], "error_text": row["error_text"],
        }
        for row in rows
    ]


def _load_enabled_local_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    groups = conn.execute(
        """
        SELECT g.*,
          (SELECT MAX(started_at) FROM trend_runs r WHERE r.group_id = g.id) AS last_started_at
        FROM trend_groups g
        WHERE g.enabled = 1
        ORDER BY g.id
        """
    ).fetchall()
    loaded: list[dict[str, Any]] = []
    for group in groups:
        points = conn.execute(
            """
            SELECT * FROM trend_points
            WHERE group_id = ?
            ORDER BY device_instance, object_type, object_instance
            """,
            (group["id"],),
        ).fetchall()
        if points:
            loaded.append({"group": dict(group), "points": [dict(point) for point in points]})
    return loaded


def _record_local_sample(
    conn: sqlite3.Connection,
    *,
    trend_point_id: int,
    sampled_at: str,
    value_text: object | None,
    status: str,
    read_source: object | None,
    error_text: object | None,
    timestamp: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO trend_samples (trend_point_id, sampled_at, value_text, status, read_source, error_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            trend_point_id,
            sampled_at,
            None if value_text is None else str(value_text),
            status,
            None if read_source is None else str(read_source),
            _safe_error(error_text) or None,
        ),
    )
    sample_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO trend_upload_outbox (event_id, trend_sample_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (str(uuid4()), sample_id, timestamp, timestamp),
    )
    return sample_id


def _log_route_diagnostics(result: dict[str, object], prefix: str = "Trend BACnet route diagnostics") -> None:
    if not isinstance(result, dict) or not result.get("route_diagnostics"):
        return
    diagnostic = dict(result["route_diagnostics"])  # type: ignore[arg-type]
    diagnostic.pop("route_args", None)
    logger.info("%s: %s", prefix, diagnostic)


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), max(1, size))]


def _trend_read_config(config: AgentConfig) -> AgentConfig:
    """A read configuration that yields the BACnet runtime quickly.

    Operator reads and writes keep the full `bacnet.lock_timeout_sec`. A trend
    batch waits only `trend_lock_timeout_sec` and then defers to the next agent
    cycle, so trend collection can never be the reason a live read is slow.
    """
    return replace(config, bacnet_lock_timeout_sec=config.trend_lock_timeout_sec)


def _sample_local_group(
    conn: sqlite3.Connection,
    config: AgentConfig,
    read_config: AgentConfig,
    item: dict[str, Any],
    *,
    timestamp: str,
    point_budget: int,
) -> tuple[int, int]:
    """Collect one due trend group. Returns (samples written, points attempted)."""
    group = item["group"]
    points: list[dict[str, Any]] = item["points"]

    # Never start a group while an operator read or write holds the runtime.
    # The group stays due and is retried on the next agent cycle.
    if bacnet_runtime_lock_held(config):
        logger.info(
            "Local Edge trend group %s (%s) deferred: BACnet runtime is busy with live work",
            group["id"],
            group["name"],
        )
        return 0, 0

    run_started = time.monotonic()
    run_cursor = conn.execute(
        """
        INSERT INTO trend_runs (group_id, started_at, requested_count)
        VALUES (?, ?, ?)
        """,
        (group["id"], timestamp, len(points)),
    )
    run_id = int(run_cursor.lastrowid)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for point in points:
        grouped.setdefault(int(point["device_instance"]), []).append(point)

    stored = 0
    attempted = 0
    returned_count = 0
    missing_count = 0
    error_count = 0
    deferred_count = 0
    errors: list[str] = []
    yielded = False

    for device_instance, device_points in grouped.items():
        if yielded:
            deferred_count += len(device_points)
            continue
        logger.info(
            "Local Edge trend route-aware read: group_id=%s device_instance=%s point_count=%s batch_size=%s",
            group["id"],
            device_instance,
            len(device_points),
            config.trend_read_batch_size,
        )
        batches = _chunked(device_points, config.trend_read_batch_size)
        for batch_index, batch in enumerate(batches):
            remaining = [point for chunk in batches[batch_index:] for point in chunk]
            if attempted + len(batch) > point_budget:
                logger.info(
                    "Local Edge trend group %s stopped at the per-cycle point budget (%s); %s point(s) deferred",
                    group["id"],
                    point_budget,
                    len(remaining),
                )
                deferred_count += len(remaining)
                yielded = True
                break
            # Between batches, hand the runtime back if live work is waiting.
            if batch_index and bacnet_runtime_lock_held(config):
                logger.info(
                    "Local Edge trend group %s yielded to live BACnet work; %s point(s) deferred",
                    group["id"],
                    len(remaining),
                )
                deferred_count += len(remaining)
                yielded = True
                break

            request = {
                "device_instance": device_instance,
                "points": [
                    {
                        "saved_point_id": str(point["id"]),
                        "object_type": point["object_type"],
                        "object_instance": int(point["object_instance"]),
                    }
                    for point in batch
                ],
            }
            try:
                result, error = run_bacnet_read_bulk(read_config, request)
            except Exception as exc:  # one controller must not end the group
                errors.append(f"device {device_instance}: {_safe_error(exc)}")
                logger.exception(
                    "Local Edge trend read raised for group %s device %s", group["id"], device_instance
                )
                result, error = {}, str(exc)

            if error == BACNET_RUNTIME_BUSY:
                # Deferred work is not a failed read: record no sample, leave the
                # group due, and try again on the next cycle.
                logger.info(
                    "Local Edge trend group %s deferred mid-run: BACnet runtime busy; %s point(s) deferred",
                    group["id"],
                    len(remaining),
                )
                deferred_count += len(remaining)
                yielded = True
                break

            attempted += len(batch)
            if isinstance(result, dict):
                _log_route_diagnostics(result, "Local Edge trend BACnet route diagnostics")
            result_values = result.get("values", []) if isinstance(result, dict) else []
            values_by_point = {
                str(value.get("saved_point_id")): value
                for value in result_values
                if isinstance(value, dict) and value.get("saved_point_id") is not None
            }
            device_error = _safe_error(error)
            if device_error:
                errors.append(f"device {device_instance}: {device_error}")
                logger.warning(
                    "Local Edge trend read failed for group %s device %s: %s",
                    group["id"],
                    device_instance,
                    device_error,
                )

            for point in batch:
                value = values_by_point.get(str(point["id"]))
                if value is None:
                    status = "error" if device_error else "missing"
                    error_text = device_error or "BACnet bulk read returned no result for point"
                    _record_local_sample(
                        conn,
                        trend_point_id=int(point["id"]),
                        sampled_at=timestamp,
                        value_text=None,
                        status=status,
                        read_source="bulk-missing",
                        error_text=error_text,
                        timestamp=timestamp,
                    )
                    if status == "error":
                        error_count += 1
                    else:
                        missing_count += 1
                    stored += 1
                    continue

                status = str(value.get("status") or "missing")
                if status == "ok":
                    returned_count += 1
                elif status == "missing":
                    missing_count += 1
                else:
                    error_count += 1
                _record_local_sample(
                    conn,
                    trend_point_id=int(point["id"]),
                    sampled_at=timestamp,
                    value_text=value.get("raw_value", value.get("value")),
                    status=status,
                    read_source=value.get("read_source"),
                    error_text=value.get("error"),
                    timestamp=timestamp,
                )
                stored += 1

    elapsed_ms = int((time.monotonic() - run_started) * 1000)
    run_error = "; ".join(errors)[:1000] if errors else None
    conn.execute(
        """
        UPDATE trend_runs
        SET completed_at = ?, returned_count = ?, deferred_count = ?, duration_ms = ?, error_text = ?
        WHERE id = ?
        """,
        (timestamp, returned_count, deferred_count, elapsed_ms, run_error, run_id),
    )
    logger.info(
        "Local Edge trend group %s complete: samples_written=%s good=%s missing=%s error=%s deferred=%s elapsed_ms=%s error=%s",
        group["id"],
        stored,
        returned_count,
        missing_count,
        error_count,
        deferred_count,
        elapsed_ms,
        run_error or "",
    )
    return stored, attempted


def sample_local_edge_trends(config: AgentConfig) -> int:
    """Sample enabled Edge UI local trend groups into the UI-owned trend DB.

    Trend configuration is read from the gateway's own trend database on every
    cycle, so a group created in the Edge UI is picked up without restarting the
    agent, and collection continues whether or not the cloud is reachable.
    """
    if not config.local_edge_trends_enabled:
        return 0
    db_path = _edge_trends_db(config)
    if db_path is None:
        logger.info("Local Edge trend sampling skipped: edge_ui_data_dir is not configured")
        return 0
    if not db_path.exists():
        logger.info("Local Edge trend sampling skipped: %s does not exist", db_path)
        return 0

    now = _now()
    timestamp = now.isoformat()
    read_config = _trend_read_config(config)
    remaining_budget = config.trend_max_points_per_cycle
    stored = 0
    with _connect_edge_trends(db_path) as conn:
        for item in _load_enabled_local_groups(conn):
            group = item["group"]
            points = item["points"]
            due = _local_due(group.get("last_started_at"), group["interval_sec"], now)
            device_count = len({int(point["device_instance"]) for point in points})
            logger.info(
                "Local Edge trend group %s (%s): interval=%s due=%s point_count=%s device_count=%s",
                group["id"],
                group["name"],
                group["interval_sec"],
                due,
                len(points),
                device_count,
            )
            if not due:
                continue
            if remaining_budget <= 0:
                logger.info(
                    "Local Edge trend group %s deferred: per-cycle point budget exhausted", group["id"]
                )
                continue

            try:
                group_stored, attempted = _sample_local_group(
                    conn,
                    config,
                    read_config,
                    item,
                    timestamp=timestamp,
                    point_budget=remaining_budget,
                )
            except Exception:
                # One malformed or failing group must never stop the others, and
                # must never end the agent loop.
                logger.exception("Local Edge trend group %s failed", group["id"])
                conn.commit()
                continue
            stored += group_stored
            remaining_budget -= attempted
        conn.commit()
    return stored


def upload_pending_local_trend_samples(config: AgentConfig, *, force: bool = False, retry_only: bool = False) -> int:
    """Cloud mirror implementation retained behind the corrective hard gate.

    The Edge remains authoritative: this only copies samples upward. Samples
    stay in the gateway's outbox until the cloud acknowledges them, so a cloud
    outage delays replication without losing or duplicating a reading.
    """
    if not trend_cloud_upload_enabled() or not config.local_edge_trends_enabled:
        return 0
    db_path = _edge_trends_db(config)
    if db_path is None or not db_path.exists():
        return 0

    now = _now()
    timestamp = now.isoformat()
    with _connect_edge_trends(db_path) as conn:
        _ensure_local_quarantine(conn)
        rows = conn.execute(
            """
            SELECT o.id AS outbox_id, o.event_id, o.attempt_count,
                   s.sampled_at, s.value_text, s.status, s.read_source, s.error_text,
                   p.device_instance, p.object_type, p.object_instance, p.object_name,
                   g.name AS group_name
            FROM trend_upload_outbox o
            JOIN trend_samples s ON s.id = o.trend_sample_id
            JOIN trend_points p ON p.id = s.trend_point_id
            JOIN trend_groups g ON g.id = p.group_id
            WHERE o.state = 'pending'
              AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?)
              AND (? = 0 OR o.attempt_count > 0)
              AND NOT EXISTS (SELECT 1 FROM trend_upload_quarantine q WHERE q.outbox_id = o.id)
            ORDER BY o.id
            LIMIT ?
            """,
            (timestamp if not force else "9999-12-31T23:59:59+00:00", int(retry_only), config.trend_local_upload_batch_size),
        ).fetchall()
        if not rows:
            return 0

        uploaded = 0

        def deliver(batch: list[sqlite3.Row]) -> None:
            nonlocal uploaded
            payload = _local_upload_payload(batch)
            fingerprint = _batch_fingerprint(batch)
            try:
                response = requests.post(
                    f"{config.cloud_url}/api/edge/{config.gateway_id}/local-trend-samples",
                    headers=auth_headers(config), json=payload, timeout=20,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                status = _http_status(exc)
                _record_transport(config, "local_trend_upload", batch, payload, success=False, status=status)
                if _retryable_upload_error(exc):
                    _record_local_upload_failure(conn, batch, config, now, str(exc))
                    logger.warning(
                        "local_trend_upload retryable failure status=%s samples=%s attempts=%s-%s batch=%s",
                        status, len(batch), min(int(row["attempt_count"]) for row in batch),
                        max(int(row["attempt_count"]) for row in batch), fingerprint,
                    )
                    raise
                if len(batch) == 1:
                    _quarantine_local_row(conn, batch[0], status, str(exc), timestamp)
                    logger.warning("local_trend_upload quarantined status=%s samples=1 batch=%s", status, fingerprint)
                    return
                midpoint = len(batch) // 2
                deliver(batch[:midpoint])
                deliver(batch[midpoint:])
                return
            conn.executemany(
                """
                UPDATE trend_upload_outbox
                SET state = 'uploaded', uploaded_at = ?, updated_at = ?, last_error = NULL, next_attempt_at = NULL
                WHERE id = ?
                """,
                [(timestamp, timestamp, int(row["outbox_id"])) for row in batch],
            )
            uploaded += len(batch)
            _record_transport(config, "local_trend_upload", batch, payload, success=True, status=response.status_code, response=response)
            logger.info("local_trend_upload success status=%s samples=%s batch=%s", response.status_code, len(batch), fingerprint)

        try:
            deliver(list(rows))
        finally:
            conn.commit()
    return uploaded


def _gateway_sync_offset_seconds(gateway_id: str, interval_sec: int) -> int:
    digest = hashlib.sha256(gateway_id.strip().upper().encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % interval_sec


def local_trend_sync_due(config: AgentConfig, interval_sec: int, *, now: datetime | None = None) -> bool:
    """Return whether the normal Edge-local cloud mirror window is due.

    The next window is persisted in agent SQLite and aligned with a stable
    gateway-specific offset, preventing a fleet restart from causing a burst.
    Retry scheduling remains in the outbox and is deliberately separate.
    """
    if not trend_cloud_upload_enabled():
        return False
    current = now or _now()
    stored = get_agent_state(config.sqlite_path, LOCAL_SYNC_STATE_KEY)
    if stored:
        try:
            return current >= datetime.fromisoformat(stored.replace("Z", "+00:00"))
        except ValueError:
            pass
    offset = _gateway_sync_offset_seconds(config.gateway_id, interval_sec)
    epoch = int(current.timestamp())
    next_epoch = (epoch // interval_sec) * interval_sec + offset
    if next_epoch <= epoch:
        next_epoch += interval_sec
    set_agent_state(config.sqlite_path, LOCAL_SYNC_STATE_KEY, datetime.fromtimestamp(next_epoch, timezone.utc).isoformat(), current.isoformat())
    return False


def schedule_next_local_trend_sync(config: AgentConfig, interval_sec: int, *, now: datetime | None = None) -> None:
    if not trend_cloud_upload_enabled():
        return
    current = now or _now()
    offset = _gateway_sync_offset_seconds(config.gateway_id, interval_sec)
    epoch = int(current.timestamp())
    next_epoch = (epoch // interval_sec) * interval_sec + offset
    if next_epoch <= epoch:
        next_epoch += interval_sec
    set_agent_state(config.sqlite_path, LOCAL_SYNC_STATE_KEY, datetime.fromtimestamp(next_epoch, timezone.utc).isoformat(), current.isoformat())


def local_trend_retry_due(config: AgentConfig, *, now: datetime | None = None) -> bool:
    """Retries may run before the next normal sync, but new rows may not."""
    if not trend_cloud_upload_enabled():
        return False
    db_path = _edge_trends_db(config)
    if db_path is None or not db_path.exists():
        return False
    timestamp = (now or _now()).isoformat()
    with _connect_edge_trends(db_path) as conn:
        _ensure_local_quarantine(conn)
        row = conn.execute(
            """
            SELECT 1 FROM trend_upload_outbox o
            WHERE o.state = 'pending' AND o.attempt_count > 0
              AND o.next_attempt_at IS NOT NULL AND o.next_attempt_at <= ?
              AND NOT EXISTS (SELECT 1 FROM trend_upload_quarantine q WHERE q.outbox_id = o.id)
            LIMIT 1
            """,
            (timestamp,),
        ).fetchone()
        conn.commit()
    return row is not None


def queue_local_trend_backfill(config: AgentConfig, since: str, until: str, *, limit: int = 500) -> int:
    """Re-offer a bounded historical range using its original event IDs.

    Existing Cloud rows are acknowledged as duplicates; rows pruned by Cloud
    retention are inserted again. Quarantined rows are deliberately excluded.
    """
    if not trend_cloud_upload_enabled():
        return 0
    db_path = _edge_trends_db(config)
    if db_path is None or not db_path.exists():
        return 0
    timestamp = _now().isoformat()
    with _connect_edge_trends(db_path) as conn:
        _ensure_local_quarantine(conn)
        rows = conn.execute(
            """
            SELECT o.id FROM trend_upload_outbox o
            JOIN trend_samples s ON s.id = o.trend_sample_id
            WHERE o.state = 'uploaded' AND s.sampled_at >= ? AND s.sampled_at <= ?
              AND NOT EXISTS (SELECT 1 FROM trend_upload_quarantine q WHERE q.outbox_id = o.id)
            ORDER BY s.sampled_at, o.id LIMIT ?
            """,
            (since, until, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            return 0
        marks = ",".join("?" for _ in rows)
        conn.execute(
            f"UPDATE trend_upload_outbox SET state='pending', next_attempt_at=NULL, updated_at=? WHERE id IN ({marks})",
            [timestamp, *(int(row["id"]) for row in rows)],
        )
        conn.commit()
    return len(rows)


def upload_pending_trend_samples(config: AgentConfig) -> int:
    if not trend_cloud_upload_enabled():
        return 0
    now = _now()
    queued = pending_trend_samples(config.sqlite_path, limit=config.trend_upload_batch_size, now=now.isoformat())
    if not queued:
        return 0
    ids = [row_id for row_id, _ in queued]
    prior_attempts = trend_upload_attempt_count(config.sqlite_path, ids)
    payload = [sample for _, sample in queued]
    try:
        response = requests.post(
            f"{config.cloud_url}/api/edge/{config.gateway_id}/trend-samples",
            headers=auth_headers(config),
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _record_legacy_transport(config, ids, payload, prior_attempts, success=False, status=_http_status(exc))
        retry_seconds = min(
            config.trend_upload_retry_max_sec,
            config.trend_upload_retry_base_sec * (2 ** min(6, max(0, prior_attempts))),
        )
        record_trend_upload_failure(
            config.sqlite_path,
            ids,
            error=str(exc),
            retry_at=(now + timedelta(seconds=retry_seconds)).isoformat(),
            updated_at=now.isoformat(),
        )
        raise
    _record_legacy_transport(config, ids, payload, prior_attempts, success=True, status=response.status_code, response=response)
    mark_trend_samples_uploaded(config.sqlite_path, ids, now.isoformat())
    return len(queued)
def sample_configured_trends(config: AgentConfig) -> int:
    response = requests.get(f"{config.cloud_url}/api/edge/{config.gateway_id}/trend-configs", headers=auth_headers(config), timeout=20)
    response.raise_for_status()
    now = _now()
    due = [trend for trend in response.json() if isinstance(trend, dict) and _due(config, trend, now)]
    grouped: dict[int, list[dict[str, Any]]] = {}
    for trend in due:
        grouped.setdefault(int(trend["device_instance"]), []).append(trend)
    stored = 0
    for device_instance, trends in grouped.items():
        result, error = run_bacnet_read_bulk(config, {"device_instance": device_instance, "points": [{"saved_point_id": trend["point_id"], "object_type": trend["object_type"], "object_instance": trend["object_instance"]} for trend in trends]})
        if isinstance(result, dict):
            _log_route_diagnostics(result)
        if error == BACNET_RUNTIME_BUSY:
            continue
        if error:
            logger.warning("Trend BACnet read failed for device %s: %s", device_instance, error)
        for value in result.get("values", []) if isinstance(result, dict) else []:
            if value.get("status") == "ok" and value.get("saved_point_id"):
                sample = {"point_id": str(value["saved_point_id"]), "sampled_at": now.isoformat(), "value": str(value.get("value", ""))}
                if queue_trend_sample(
                    config.sqlite_path,
                    sample,
                    now.isoformat(),
                    max_pending=config.trend_queue_max_pending_samples,
                ):
                    stored += 1
    return stored
