from dataclasses import dataclass
import os
from pathlib import Path

import yaml

from iot_cx_agent import __version__


DEFAULT_CONFIG_PATH = Path("/etc/iot-cx-agent/agent.yaml")
DEFAULT_SQLITE_PATH = Path("/var/lib/iot-cx-agent/edge.db")
DEFAULT_BACNET_PORT = 47814
BAC_RTR_BACNET_PORT = 47809
DEFAULT_BACNET_LOCK_DIR = Path("/tmp")
DEFAULT_BACNET_LOCK_PREFIX = "iot-edge-bacnet"
UNPROVISIONED_VALUE = "UNPROVISIONED"
BACNET_ROUTER_PROFILE_PORTS = {
    "contemporary": DEFAULT_BACNET_PORT,
    "basrtb": DEFAULT_BACNET_PORT,
    "bac-rtr": BAC_RTR_BACNET_PORT,
}


@dataclass(frozen=True)
class AgentConfig:
    gateway_id: str
    site_id: str
    cloud_url: str
    tunnel_enabled: bool = True
    local_ui_url: str = "http://127.0.0.1:5000"
    # Maximum time allowed for a relayed request to the gateway-local UI.
    # This is intentionally independent from the WebSocket connect timeout.
    tunnel_request_timeout_sec: float = 900.0
    bacnet_router_profile: str = "contemporary"
    bacnet_default_port: int = DEFAULT_BACNET_PORT
    bacwi_path: str = "bacwi"
    bacrp_path: str = "bacrp"
    bacrpm_path: str = "bacrpm"
    bacnet_timeout_sec: int = 10
    bacnet_lock_path: Path | None = None
    bacnet_lock_timeout_sec: float = 30.0
    bacnet_lock_stale_sec: float = 120.0
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

    def bacnet_lock_path_for_port(self, port: int | None = None) -> Path:
        resolved_port = self.bacnet_default_port if port is None else port
        if self.bacnet_lock_path is None:
            return DEFAULT_BACNET_LOCK_DIR / f"{DEFAULT_BACNET_LOCK_PREFIX}-{resolved_port}.lock"
        if str(self.bacnet_lock_path).endswith(".lock"):
            return self.bacnet_lock_path
        return self.bacnet_lock_path / f"{DEFAULT_BACNET_LOCK_PREFIX}-{resolved_port}.lock"


def _configured_version(value: object | None) -> str:
    if value is None:
        return __version__
    version = str(value).strip()
    return version or __version__


def _parse_port(raw_port: object, source: str) -> int:
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer between 1 and 65535") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be an integer between 1 and 65535")
    return port


def normalize_bacnet_router_profile(raw_profile: object | None) -> str:
    profile = str(raw_profile or "contemporary").strip().lower()
    profile = profile.replace("_", "-")
    if profile == "basrt-b":
        profile = "basrtb"
    if profile not in {*BACNET_ROUTER_PROFILE_PORTS, "custom"}:
        raise ValueError(
            "BACNET_ROUTER_PROFILE must be one of: contemporary, basrtb, bac-rtr, custom"
        )
    return profile


def resolve_bacnet_port(
    *,
    profile: object | None = None,
    explicit_port: object | None = None,
    fallback_port: object | None = DEFAULT_BACNET_PORT,
) -> tuple[str, int]:
    normalized_profile = normalize_bacnet_router_profile(profile)
    if explicit_port not in (None, ""):
        return normalized_profile, _parse_port(explicit_port, "BACNET_IP_PORT")
    if normalized_profile == "custom":
        return normalized_profile, _parse_port(fallback_port, "bacnet.default_port")
    if normalized_profile in BACNET_ROUTER_PROFILE_PORTS:
        return normalized_profile, BACNET_ROUTER_PROFILE_PORTS[normalized_profile]
    return normalized_profile, _parse_port(fallback_port, "bacnet.default_port")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AgentConfig:
    with path.open("r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file) or {}

    bacnet = raw.get("bacnet") or {}
    sqlite_path = Path(raw.get("sqlite_path", DEFAULT_SQLITE_PATH))
    configured_port = raw.get("bacnet_default_port", bacnet.get("default_port", DEFAULT_BACNET_PORT))
    profile, bacnet_port = resolve_bacnet_port(
        profile=os.getenv("BACNET_ROUTER_PROFILE") or bacnet.get("router_profile"),
        explicit_port=os.getenv("BACNET_IP_PORT"),
        fallback_port=configured_port,
    )
    raw_lock_path = bacnet.get("lock_path")
    lock_path = Path(raw_lock_path) if raw_lock_path else None
    return AgentConfig(
        gateway_id=str(raw["gateway_id"]),
        site_id=str(raw["site_id"]),
        cloud_url=str(raw["cloud_url"]).rstrip("/"),
        tunnel_enabled=bool(raw.get("tunnel_enabled", True)),
        local_ui_url=str(raw.get("local_ui_url", "http://127.0.0.1:5000")).rstrip("/"),
        tunnel_request_timeout_sec=float(raw.get("tunnel_request_timeout_sec", 900)),
        bacnet_router_profile=profile,
        bacnet_default_port=bacnet_port,
        bacwi_path=str(bacnet.get("bacwi_path", "bacwi")),
        bacrp_path=str(bacnet.get("bacrp_path", "bacrp")),
        bacrpm_path=str(bacnet.get("bacrpm_path", "bacrpm")),
        bacnet_timeout_sec=int(bacnet.get("timeout_sec", 10)),
        bacnet_lock_path=lock_path,
        bacnet_lock_timeout_sec=float(bacnet.get("lock_timeout_sec", 30)),
        bacnet_lock_stale_sec=float(bacnet.get("lock_stale_sec", 120)),
        heartbeat_interval_sec=int(raw.get("heartbeat_interval_sec", 30)),
        agent_version=_configured_version(raw.get("agent_version")),
        ui_version=_configured_version(raw.get("ui_version")),
        sqlite_path=sqlite_path,
        gateway_api_token=os.getenv("GATEWAY_API_TOKEN") or raw.get("gateway_api_token"),
    )
