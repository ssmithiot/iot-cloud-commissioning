from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.tunnel import handle_tunnel_message, local_ui_base_url, run_tunnel, tunnel_url


def test_tunnel_url_uses_gateway_id() -> None:
    config = AgentConfig(
        gateway_id="GW 001",
        site_id="demo-site",
        cloud_url="https://cloud.example.com",
        sqlite_path=Path("edge.db"),
    )

    assert tunnel_url(config) == "wss://cloud.example.com/api/edge/tunnels/GW%20001"


def test_handle_tunnel_message_proxies_to_local_ui(monkeypatch) -> None:
    config = AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        local_ui_url="http://127.0.0.1:5000",
        sqlite_path=Path("edge.db"),
    )

    def fake_request(*args: object, **kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = b"gateway ui"
        response.headers["content-type"] = "text/plain"
        assert args[0] == "GET"
        assert args[1] == "http://127.0.0.1:5000/status?tab=network"
        forwarded_headers = kwargs["headers"]
        assert "Authorization" not in forwarded_headers
        assert forwarded_headers["Cookie"] == "session=secret"
        assert forwarded_headers["Content-Type"] == "application/x-www-form-urlencoded"
        return response

    monkeypatch.setattr(requests, "request", fake_request)

    response = handle_tunnel_message(
        config,
        {
            "type": "request",
            "request_id": "req-1",
            "method": "GET",
            "path": "/status",
            "query_string": "tab=network",
            "headers": {
                "Authorization": "Bearer browser-token",
                "Cookie": "session=secret",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            "body_b64": "",
        },
    )

    assert response["type"] == "response"
    assert response["request_id"] == "req-1"
    assert response["status_code"] == 200
    assert response["body_b64"] == "Z2F0ZXdheSB1aQ=="


def test_local_ui_base_url_requires_allowlisted_target(tmp_path: Path) -> None:
    config = AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        local_ui_url="http://192.168.1.50:5000",
        sqlite_path=tmp_path / "edge.db",
    )

    with pytest.raises(ValueError, match="not allowlisted"):
        local_ui_base_url(config)


def test_handle_tunnel_message_rejects_unallowlisted_local_ui(monkeypatch, tmp_path: Path) -> None:
    config = AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        local_ui_url="http://127.0.0.1:5100",
        sqlite_path=tmp_path / "edge.db",
    )

    def fail_request(*args: object, **kwargs: object) -> requests.Response:
        raise AssertionError("unallowlisted tunnel target should not be requested")

    monkeypatch.setattr(requests, "request", fail_request)

    response = handle_tunnel_message(
        config,
        {
            "type": "request",
            "request_id": "req-2",
            "method": "GET",
            "path": "/",
            "query_string": "",
            "headers": {},
            "body_b64": "",
        },
    )

    assert response == {
        "type": "error",
        "request_id": "req-2",
        "error": "Gateway tunnel target is not allowlisted",
    }


def test_handle_tunnel_message_reports_local_ui_unavailable(monkeypatch, tmp_path: Path) -> None:
    config = AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        sqlite_path=tmp_path / "edge.db",
    )

    def unavailable(*args: object, **kwargs: object) -> requests.Response:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "request", unavailable)

    response = handle_tunnel_message(
        config,
        {
            "type": "request",
            "request_id": "req-3",
            "method": "GET",
            "path": "/",
            "query_string": "",
            "headers": {},
            "body_b64": "",
        },
    )

    assert response == {"type": "error", "request_id": "req-3", "error": "Local gateway UI unavailable"}


def test_run_tunnel_sends_gateway_auth_header(monkeypatch, tmp_path: Path) -> None:
    config = AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="https://cloud.example.com",
        sqlite_path=tmp_path / "edge.db",
        gateway_api_token="iotcc_gw_prefix_secret",
    )
    captured = {}

    class FakeConnection:
        def recv(self) -> str:
            raise RuntimeError("stop")

        def close(self) -> None:
            captured["closed"] = True

    def fake_create_connection(url: str, header: list[str], timeout: int) -> FakeConnection:
        captured["url"] = url
        captured["header"] = header
        captured["timeout"] = timeout
        return FakeConnection()

    monkeypatch.setitem(sys.modules, "websocket", SimpleNamespace(create_connection=fake_create_connection))

    with pytest.raises(RuntimeError, match="stop"):
        run_tunnel(config)

    assert captured["url"] == "wss://cloud.example.com/api/edge/tunnels/GW001"
    assert captured["header"] == ["Authorization: Bearer iotcc_gw_prefix_secret"]
    assert captured["timeout"] == 30
    assert captured["closed"] is True
