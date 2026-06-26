import base64
import json
import logging
import time
from urllib.parse import quote, urlparse

import requests

from iot_cx_agent.config import AgentConfig


logger = logging.getLogger("iot-cx-agent.tunnel")


def tunnel_url(config: AgentConfig) -> str:
    parsed = urlparse(config.cloud_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    gateway_id = quote(config.gateway_id, safe="")
    return f"{scheme}://{parsed.netloc}{base_path}/api/edge/tunnels/{gateway_id}"


def run_tunnel_forever(config: AgentConfig) -> None:
    while True:
        try:
            run_tunnel(config)
        except Exception as exc:
            logger.warning("Gateway tunnel disconnected: %s", exc)
        time.sleep(5)


def run_tunnel(config: AgentConfig) -> None:
    import websocket

    headers = []
    if config.gateway_api_token:
        headers.append(f"Authorization: Bearer {config.gateway_api_token}")

    url = tunnel_url(config)
    logger.info("Opening outbound gateway tunnel to %s", url)
    connection = websocket.create_connection(url, header=headers, timeout=30)
    try:
        while True:
            raw_message = connection.recv()
            if not raw_message:
                continue

            response = handle_tunnel_message(config, json.loads(raw_message))
            connection.send(json.dumps(response))
    finally:
        connection.close()


def handle_tunnel_message(config: AgentConfig, message: dict[str, object]) -> dict[str, object]:
    request_id = str(message.get("request_id", ""))
    if message.get("type") != "request":
        return {"type": "error", "request_id": request_id, "error": "Unsupported tunnel message"}

    try:
        method = str(message["method"])
        path = str(message.get("path") or "/")
        query_string = str(message.get("query_string") or "")
        headers = message.get("headers") or {}
        body_b64 = str(message.get("body_b64") or "")
        if not isinstance(headers, dict):
            headers = {}

        url = f"{config.local_ui_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        response = requests.request(
            method,
            url,
            headers={
                str(key): str(value)
                for key, value in headers.items()
                if str(key).lower() not in {"host", "content-length", "connection"}
            },
            data=base64.b64decode(body_b64),
            timeout=30,
            allow_redirects=False,
        )
        return {
            "type": "response",
            "request_id": request_id,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body_b64": base64.b64encode(response.content).decode("ascii"),
        }
    except Exception as exc:
        logger.exception("Tunnel request failed")
        return {"type": "error", "request_id": request_id, "error": str(exc)}
