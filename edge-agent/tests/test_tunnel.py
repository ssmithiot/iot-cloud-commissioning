from pathlib import Path

import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.tunnel import handle_tunnel_message, tunnel_url


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
            "headers": {},
            "body_b64": "",
        },
    )

    assert response["type"] == "response"
    assert response["request_id"] == "req-1"
    assert response["status_code"] == 200
    assert response["body_b64"] == "Z2F0ZXdheSB1aQ=="
