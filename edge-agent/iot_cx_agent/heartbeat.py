import requests

from iot_cx_agent.config import AgentConfig


def auth_headers(config: AgentConfig) -> dict[str, str]:
    if config.gateway_api_token is None:
        return {}
    return {"Authorization": f"Bearer {config.gateway_api_token}"}


def send_heartbeat(config: AgentConfig, payload: dict[str, object]) -> requests.Response:
    return requests.post(
        f"{config.cloud_url}/api/edge/heartbeat",
        headers=auth_headers(config),
        json=payload,
        timeout=10,
    )
