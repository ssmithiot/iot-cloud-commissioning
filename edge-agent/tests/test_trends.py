from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import initialize_database, pending_trend_samples, queue_trend_sample, trend_upload_attempt_count
from iot_cx_agent.trends import sample_configured_trends, upload_pending_trend_samples


def config(tmp_path: Path, **overrides: object) -> AgentConfig:
    values: dict[str, object] = {
        "gateway_id": "GW001",
        "site_id": "demo-site",
        "cloud_url": "https://cloud.example.test",
        "gateway_api_token": "iotcc_gw_prefix_secret",
        "sqlite_path": tmp_path / "edge.db",
        "trend_upload_batch_size": 2,
        "trend_upload_retry_base_sec": 30,
        "trend_upload_retry_max_sec": 120,
    }
    values.update(overrides)
    return AgentConfig(**values)


class Response:
    def __init__(self, payload: object | None = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def test_upload_pending_trend_samples_uses_bounded_batch_and_marks_success(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    initialize_database(agent_config.sqlite_path)
    for index in range(3):
        queue_trend_sample(
            agent_config.sqlite_path,
            {"point_id": f"point-{index}", "sampled_at": f"2026-07-12T12:00:0{index}+00:00", "value": str(index)},
            f"2026-07-12T12:00:0{index}+00:00",
        )
    sent: list[object] = []

    def fake_post(url: str, **kwargs: object) -> Response:
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)

    assert upload_pending_trend_samples(agent_config) == 2
    assert len(sent) == 1
    assert len(sent[0]) == 2
    assert len(pending_trend_samples(agent_config.sqlite_path)) == 1


def test_failed_trend_upload_records_attempt_and_defers_retry(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    initialize_database(agent_config.sqlite_path)
    queue_trend_sample(
        agent_config.sqlite_path,
        {"point_id": "point-1", "sampled_at": "2026-07-12T12:00:00+00:00", "value": "72.5"},
        "2026-07-12T12:00:00+00:00",
    )

    def failing_post(*args: object, **kwargs: object) -> Response:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", failing_post)

    with pytest.raises(requests.ConnectionError, match="offline"):
        upload_pending_trend_samples(agent_config)

    queued = pending_trend_samples(agent_config.sqlite_path, now="2026-07-12T00:00:00+00:00")
    assert queued == []
    all_rows = pending_trend_samples(agent_config.sqlite_path)
    assert len(all_rows) == 1
    assert trend_upload_attempt_count(agent_config.sqlite_path, [all_rows[0][0]]) == 1


def test_sampling_queues_only_successful_due_points_within_backlog_limit(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path, trend_queue_max_pending_samples=1)
    initialize_database(agent_config.sqlite_path)
    trend_configs = [
        {"point_id": "point-1", "device_instance": 1001, "object_type": "analog-value", "object_instance": 1, "interval_sec": 60},
        {"point_id": "point-2", "device_instance": 1001, "object_type": "analog-value", "object_instance": 2, "interval_sec": 60},
    ]

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(trend_configs))
    monkeypatch.setattr(
        "iot_cx_agent.trends.run_bacnet_read_bulk",
        lambda *args, **kwargs: (
            {
                "values": [
                    {"saved_point_id": "point-1", "status": "ok", "value": "71.0"},
                    {"saved_point_id": "point-2", "status": "ok", "value": "72.0"},
                ]
            },
            None,
        ),
    )

    assert sample_configured_trends(agent_config) == 1
    queued = pending_trend_samples(agent_config.sqlite_path)
    assert len(queued) == 1
    assert queued[0][1]["point_id"] == "point-1"
