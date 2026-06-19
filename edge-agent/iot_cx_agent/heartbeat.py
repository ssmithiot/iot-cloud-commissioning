import requests

from iot_cx_agent.config import AgentConfig


def send_heartbeat(config: AgentConfig, payload: dict[str, object]) -> requests.Response:
    return requests.post(
        f"{config.cloud_url}/api/edge/heartbeat",
        json=payload,
        timeout=10,
    )

