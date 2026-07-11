from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from iot_cx_agent.bacnet import BACNET_RUNTIME_BUSY, run_bacnet_read_bulk
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import mark_trend_samples_uploaded, pending_trend_samples, queue_trend_sample, trend_last_sample_at
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
    queued = pending_trend_samples(config.sqlite_path)
    if not queued:
        return 0
    response = requests.post(
        f"{config.cloud_url}/api/edge/{config.gateway_id}/trend-samples",
        headers=auth_headers(config),
        json=[sample for _, sample in queued],
        timeout=20,
    )
    response.raise_for_status()
    mark_trend_samples_uploaded(config.sqlite_path, [row_id for row_id, _ in queued], _now().isoformat())
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
                queue_trend_sample(config.sqlite_path, sample, now.isoformat())
                stored += 1
    return stored
