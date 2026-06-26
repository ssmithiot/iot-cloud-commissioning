from dataclasses import dataclass
import os
from pathlib import Path

import yaml

from iot_cx_agent import __version__


DEFAULT_CONFIG_PATH = Path("/etc/iot-cx-agent/agent.yaml")
DEFAULT_SQLITE_PATH = Path("/var/lib/iot-cx-agent/edge.db")
DEFAULT_BACNET_LOCK_PATH = Path("/tmp/iot-cloud-commissioning-bacnet-47814.lock")
UNPROVISIONED_VALUE = "UNPROVISIONED"


@dataclass(frozen=True)
class AgentConfig:
    gateway_id: str
    site_id: str
    cloud_url: str
    bacnet_default_port: int = 47814
    bacwi_path: str = "bacwi"
    bacrp_path: str = "bacrp"
    bacrpm_path: str = "bacrpm"
    bacnet_timeout_sec: int = 10
    bacnet_lock_path: Path = DEFAULT_BACNET_LOCK_PATH
    heartbeat_interval_sec: int = 30
    agent_version: str = "0.1.0"
    ui_version: str = "0.1.0"
    sqlite_path: Path = DEFAULT_SQLITE_PATH
    gateway_api_token: str | None = None

    @property
    def is_provisioned(self) -> bool:
        return (
            self.gateway_id.strip().upper() != UNPROVISIONED_VALUE
            and self.site_id.strip().upper() != UNPROVISIONED_VALUE
            and bool(self.gateway_api_token)
        )


def _configured_version(value: object | None) -> str:
    if value is None:
        return __version__
    version = str(value).strip()
    return version or __version__


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
        bacrpm_path=str(bacnet.get("bacrpm_path", "bacrpm")),
        bacnet_timeout_sec=int(bacnet.get("timeout_sec", 10)),
        bacnet_lock_path=Path(bacnet.get("lock_path", DEFAULT_BACNET_LOCK_PATH)),
        heartbeat_interval_sec=int(raw.get("heartbeat_interval_sec", 30)),
        agent_version=_configured_version(raw.get("agent_version")),
        ui_version=_configured_version(raw.get("ui_version")),
        sqlite_path=sqlite_path,
        gateway_api_token=os.getenv("GATEWAY_API_TOKEN") or raw.get("gateway_api_token"),
    )
