from dataclasses import dataclass
import os
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = Path("/etc/iot-cx-agent/agent.yaml")
DEFAULT_SQLITE_PATH = Path("/var/lib/iot-cx-agent/edge.db")
DEFAULT_BACNET_LOCK_PATH = Path("/tmp/iot-cloud-commissioning-bacnet-47814.lock")


@dataclass(frozen=True)
class AgentConfig:
    gateway_id: str
    site_id: str
    cloud_url: str
    bacnet_default_port: int = 47814
    bacwi_path: str = "bacwi"
    bacrp_path: str = "bacrp"
    bacnet_timeout_sec: int = 10
    bacnet_lock_path: Path = DEFAULT_BACNET_LOCK_PATH
    heartbeat_interval_sec: int = 30
    agent_version: str = "0.1.0"
    ui_version: str = "0.1.0"
    sqlite_path: Path = DEFAULT_SQLITE_PATH
    gateway_api_token: str | None = None


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AgentConfig:
    with path.open("r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file) or {}

    bacnet = raw.get("bacnet") or {}
    sqlite_path = Path(raw.get("sqlite_path", DEFAULT_SQLITE_PATH))
    return AgentConfig(
        gateway_id=str(raw["gateway_id"]),
        site_id=str(raw["site_id"]),
        cloud_url=str(raw["cloud_url"]).rstrip("/"),
        bacnet_default_port=int(raw.get("bacnet_default_port", bacnet.get("default_port", 47814))),
        bacwi_path=str(bacnet.get("bacwi_path", "bacwi")),
        bacrp_path=str(bacnet.get("bacrp_path", "bacrp")),
        bacnet_timeout_sec=int(bacnet.get("timeout_sec", 10)),
        bacnet_lock_path=Path(bacnet.get("lock_path", DEFAULT_BACNET_LOCK_PATH)),
        heartbeat_interval_sec=int(raw.get("heartbeat_interval_sec", 30)),
        agent_version=str(raw.get("agent_version", "0.1.0")),
        ui_version=str(raw.get("ui_version", "0.1.0")),
        sqlite_path=sqlite_path,
        gateway_api_token=os.getenv("GATEWAY_API_TOKEN") or raw.get("gateway_api_token"),
    )
