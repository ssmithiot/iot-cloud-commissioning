from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path

import yaml

from iot_cx_agent import __version__


DEFAULT_CONFIG_PATH = Path("/etc/iot-cx-agent/agent.yaml")
DEFAULT_SQLITE_PATH = Path("/var/lib/iot-cx-agent/edge.db")
DEFAULT_UI_VERSION = "0.1.0"
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
    local_ui_write_timeout_sec: float = 120.0
    bacnet_router_profile: str = "contemporary"
    bacnet_default_port: int = DEFAULT_BACNET_PORT
    bacnet_bbmd_address: str | None = None
    bacnet_bbmd_port: int | None = None
    bacwi_path: str = "bacwi"
    bacrp_path: str = "bacrp"
    bacrpm_path: str = "bacrpm"
    bacnet_timeout_sec: int = 10
    bacnet_lock_path: Path | None = None
    bacnet_lock_timeout_sec: float = 30.0
    bacnet_lock_stale_sec: float = 120.0
    heartbeat_interval_sec: int = 30
    edge_ui_data_dir: Path | None = None
    # Local Edge trends ship enabled in 0.2.0. The Edge UI gate
    # (EDGE_TRENDS_UI_ENABLED) must be set to match; both are required.
    local_edge_trends_enabled: bool = True
    # Trend reads must never make an operator's read or write wait. A trend
    # batch gives up on the BACnet runtime lock almost immediately and retries
    # on the next agent cycle, rather than queueing behind live work for the
    # full operator lock timeout.
    trend_lock_timeout_sec: float = 2.0
    # Points read per BACnet request during trend collection. The runtime lock
    # is acquired and released per batch so live work can interleave.
    trend_read_batch_size: int = 8
    # Upper bound on trend points read in a single agent cycle, so one large
    # group cannot monopolise the BACnet runtime.
    trend_max_points_per_cycle: int = 200
    trend_local_upload_batch_size: int = 200
    trend_upload_batch_size: int = 100
    trend_queue_max_pending_samples: int = 10_000
    trend_upload_retry_base_sec: int = 30
    trend_upload_retry_max_sec: int = 900
    agent_version: str = __version__
    ui_version: str = DEFAULT_UI_VERSION
    sqlite_path: Path = DEFAULT_SQLITE_PATH
    gateway_api_token: str | None = None
    edge_agent_write_token: str | None = None

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


def _configured_ui_version(value: object | None) -> str:
    if value is None:
        return DEFAULT_UI_VERSION
    version = str(value).strip()
    return version or DEFAULT_UI_VERSION


def _parse_port(raw_port: object, source: str) -> int:
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer between 1 and 65535") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be an integer between 1 and 65535")
    return port


def _parse_ipv4_address(raw_address: object, source: str) -> str:
    value = str(raw_address).strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{source} must be an IPv4 address") from exc
    if address.version != 4:
        raise ValueError(f"{source} must be an IPv4 address")
    return str(address)


def _positive_int(raw_value: object, source: str, *, minimum: int = 1) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer greater than or equal to {minimum}") from exc
    if value < minimum:
        raise ValueError(f"{source} must be an integer greater than or equal to {minimum}")
    return value


def _positive_float(raw_value: object, source: str) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be a number greater than zero") from exc
    if value <= 0:
        raise ValueError(f"{source} must be a number greater than zero")
    return value


def _bool_flag(raw_value: object, source: str) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if raw_value in (None, ""):
        return False
    value = str(raw_value).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{source} must be true or false")


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
    raw_bbmd_address = os.getenv("BACNET_BBMD_ADDRESS") or bacnet.get("bbmd_address")
    raw_bbmd_port = os.getenv("BACNET_BBMD_PORT") or bacnet.get("bbmd_port")
    if bool(raw_bbmd_address) != bool(raw_bbmd_port):
        raise ValueError("bacnet.bbmd_address and bacnet.bbmd_port must be configured together")
    bbmd_address = _parse_ipv4_address(raw_bbmd_address, "BACNET_BBMD_ADDRESS") if raw_bbmd_address else None
    bbmd_port = _parse_port(raw_bbmd_port, "BACNET_BBMD_PORT") if raw_bbmd_port else None
    if bbmd_port is not None and bbmd_port == bacnet_port:
        raise ValueError("bacnet.default_port must differ from bacnet.bbmd_port when FDR is configured")
    return AgentConfig(
        gateway_id=str(raw["gateway_id"]),
        site_id=str(raw["site_id"]),
        cloud_url=str(raw["cloud_url"]).rstrip("/"),
        tunnel_enabled=bool(raw.get("tunnel_enabled", True)),
        local_ui_url=str(raw.get("local_ui_url", "http://127.0.0.1:5000")).rstrip("/"),
        tunnel_request_timeout_sec=float(raw.get("tunnel_request_timeout_sec", 900)),
        local_ui_write_timeout_sec=float(raw.get("local_ui_write_timeout_sec", 120)),
        bacnet_router_profile=profile,
        bacnet_default_port=bacnet_port,
        bacnet_bbmd_address=bbmd_address,
        bacnet_bbmd_port=bbmd_port,
        bacwi_path=str(bacnet.get("bacwi_path", "bacwi")),
        bacrp_path=str(bacnet.get("bacrp_path", "bacrp")),
        bacrpm_path=str(bacnet.get("bacrpm_path", "bacrpm")),
        bacnet_timeout_sec=int(bacnet.get("timeout_sec", 10)),
        bacnet_lock_path=lock_path,
        bacnet_lock_timeout_sec=float(bacnet.get("lock_timeout_sec", 30)),
        bacnet_lock_stale_sec=float(bacnet.get("lock_stale_sec", 120)),
        heartbeat_interval_sec=int(raw.get("heartbeat_interval_sec", 30)),
        edge_ui_data_dir=Path(raw["edge_ui_data_dir"]) if raw.get("edge_ui_data_dir") else None,
        local_edge_trends_enabled=_bool_flag(raw.get("local_edge_trends_enabled", True), "local_edge_trends_enabled"),
        trend_lock_timeout_sec=_positive_float(raw.get("trend_lock_timeout_sec", 2.0), "trend_lock_timeout_sec"),
        trend_read_batch_size=_positive_int(raw.get("trend_read_batch_size", 8), "trend_read_batch_size"),
        trend_max_points_per_cycle=_positive_int(raw.get("trend_max_points_per_cycle", 200), "trend_max_points_per_cycle"),
        trend_local_upload_batch_size=_positive_int(raw.get("trend_local_upload_batch_size", 200), "trend_local_upload_batch_size"),
        trend_upload_batch_size=_positive_int(raw.get("trend_upload_batch_size", 100), "trend_upload_batch_size"),
        trend_queue_max_pending_samples=_positive_int(raw.get("trend_queue_max_pending_samples", 10_000), "trend_queue_max_pending_samples"),
        trend_upload_retry_base_sec=_positive_int(raw.get("trend_upload_retry_base_sec", 30), "trend_upload_retry_base_sec"),
        trend_upload_retry_max_sec=_positive_int(raw.get("trend_upload_retry_max_sec", 900), "trend_upload_retry_max_sec"),
        agent_version=__version__,
        ui_version=_configured_ui_version(raw.get("ui_version")),
        sqlite_path=sqlite_path,
        gateway_api_token=os.getenv("GATEWAY_API_TOKEN") or raw.get("gateway_api_token"),
        edge_agent_write_token=os.getenv("EDGE_AGENT_WRITE_TOKEN") or raw.get("edge_agent_write_token"),
    )
