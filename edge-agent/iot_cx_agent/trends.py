from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4

import requests

from iot_cx_agent.bacnet import BACNET_RUNTIME_BUSY, run_bacnet_read_bulk
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import (
    mark_trend_samples_uploaded,
    pending_trend_samples,
    queue_trend_sample,
    record_trend_upload_failure,
    trend_upload_attempt_count,
    trend_last_sample_at,
)
from iot_cx_agent.heartbeat import auth_headers


logger = logging.getLogger("iot-cx-agent")
EDGE_TRENDS_DB_NAME = "edge-trends.db"


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


def sample_local_edge_trends(config: AgentConfig) -> int:
    """Sample enabled Edge UI local trend groups into the UI-owned trend DB."""
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

            returned_count = 0
            missing_count = 0
            error_count = 0
            errors: list[str] = []

            for device_instance, device_points in grouped.items():
                logger.info(
                    "Local Edge trend route-aware read: group_id=%s device_instance=%s point_count=%s",
                    group["id"],
                    device_instance,
                    len(device_points),
                )
                request = {
                    "device_instance": device_instance,
                    "points": [
                        {
                            "saved_point_id": str(point["id"]),
                            "object_type": point["object_type"],
                            "object_instance": int(point["object_instance"]),
                        }
                        for point in device_points
                    ],
                }
                result, error = run_bacnet_read_bulk(config, request)
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

                for point in device_points:
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
                (timestamp, returned_count, missing_count + error_count, elapsed_ms, run_error, run_id),
            )
            logger.info(
                "Local Edge trend group %s complete: samples_written=%s good=%s missing=%s error=%s elapsed_ms=%s error=%s",
                group["id"],
                returned_count + missing_count + error_count,
                returned_count,
                missing_count,
                error_count,
                elapsed_ms,
                run_error or "",
            )
        conn.commit()
    return stored


def upload_pending_trend_samples(config: AgentConfig) -> int:
    now = _now()
    queued = pending_trend_samples(config.sqlite_path, limit=config.trend_upload_batch_size, now=now.isoformat())
    if not queued:
        return 0
    ids = [row_id for row_id, _ in queued]
    prior_attempts = trend_upload_attempt_count(config.sqlite_path, ids)
    try:
        response = requests.post(
            f"{config.cloud_url}/api/edge/{config.gateway_id}/trend-samples",
            headers=auth_headers(config),
            json=[sample for _, sample in queued],
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
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
