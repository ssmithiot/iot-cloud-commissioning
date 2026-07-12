from pathlib import Path

import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.local_write import WRITE_BATCH_PATH, dispatch_bacnet_write_batch, local_write_base_url


def config(tmp_path: Path, **overrides: object) -> AgentConfig:
    values: dict[str, object] = {
        "gateway_id": "GW001",
        "site_id": "demo-site",
        "cloud_url": "https://cloud.example",
        "local_ui_url": "http://127.0.0.1:5000",
        "sqlite_path": tmp_path / "edge.db",
        "edge_agent_write_token": "local-secret",
    }
    values.update(overrides)
    return AgentConfig(**values)


def test_local_write_target_must_be_loopback(tmp_path: Path) -> None:
    unsafe = config(tmp_path, local_ui_url="http://192.168.1.20:5000")

    try:
        local_write_base_url(unsafe)
    except ValueError as exc:
        assert "loopback-only" in str(exc)
    else:
        raise AssertionError("Expected a non-loopback URL to be rejected")


def test_dispatch_write_batch_posts_only_to_local_ui(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def post(url: str, **kwargs: object) -> requests.Response:
        captured["url"] = url
        captured.update(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"job_type":"bacnet_write_batch","status":"ok","results":[]}'
        return response

    monkeypatch.setattr(requests, "post", post)
    result, error = dispatch_bacnet_write_batch(
        config(tmp_path),
        {"job_id": "job-1", "request": {"device_instance": 1001, "writes": []}},
    )

    assert error is None
    assert result == {"job_type": "bacnet_write_batch", "status": "ok", "results": []}
    assert captured["url"] == f"http://127.0.0.1:5000{WRITE_BATCH_PATH}"
    assert captured["headers"] == {"Authorization": "Bearer local-secret"}
    assert captured["json"] == {"job_id": "job-1", "device_instance": 1001, "writes": []}


def test_dispatch_write_batch_requires_local_token(tmp_path: Path) -> None:
    result, error = dispatch_bacnet_write_batch(
        config(tmp_path, edge_agent_write_token=None),
        {"job_id": "job-1", "request": {"device_instance": 1001, "writes": []}},
    )

    assert result is None
    assert error == "EDGE_AGENT_WRITE_TOKEN is not configured"
