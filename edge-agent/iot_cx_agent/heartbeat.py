import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.network_traffic import record_http


def auth_headers(config: AgentConfig) -> dict[str, str]:
    if config.gateway_api_token is None:
        return {}
    return {"Authorization": f"Bearer {config.gateway_api_token}"}


def send_heartbeat(config: AgentConfig, payload: dict[str, object]) -> requests.Response:
    try:
        response = requests.post(
            f"{config.cloud_url}/api/edge/heartbeat",
            headers=auth_headers(config),
            json=payload,
            timeout=10,
        )
    except requests.RequestException:
        record_http(config.sqlite_path, "heartbeat", tx_body=payload, success=False)
        raise
    record_http(config.sqlite_path, "heartbeat", response, tx_body=payload)
    return response
