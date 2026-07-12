from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

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
        if error == BACNET_RUNTIME_BUSY:
            continue
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
