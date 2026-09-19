import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from urllib.parse import quote, urlparse

import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.network_traffic import record


logger = logging.getLogger("iot-cx-agent.tunnel")
ALLOWED_LOCAL_UI_URL = "http://127.0.0.1:5000"
STRIPPED_LOCAL_HEADERS = {"host", "content-length", "connection", "authorization"}
SENSITIVE_LOG_HEADER_NAMES = {"authorization"}


def tunnel_url(config: AgentConfig) -> str:
    parsed = urlparse(config.cloud_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    gateway_id = quote(config.gateway_id, safe="")
    return f"{scheme}://{parsed.netloc}{base_path}/api/edge/tunnels/{gateway_id}"


def run_tunnel_forever(
    config: AgentConfig,
    *,
    requested: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run only while a current Cloud lease says this gateway is wanted.

    The caller owns the lease signal.  In particular, this is deliberately
    not a service-start reconnect loop.
    """
    retry_delay = 1.0
    while requested is not None and requested():
        try:
            lease_revoked = run_tunnel(config, requested=requested)
            if lease_revoked:
                return
            retry_delay = 1.0
        except Exception as exc:
            if not requested():
                break
            logger.warning("Gateway tunnel disconnected; retrying while Cloud lease is valid: %s", exc)
            sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)


def run_tunnel(config: AgentConfig, *, requested: Callable[[], bool] | None = None) -> bool:
    import websocket

    headers = []
    if config.gateway_api_token:
        headers.append(f"Authorization: Bearer {config.gateway_api_token}")

    url = tunnel_url(config)
    logger.info("Opening outbound gateway tunnel to %s", url)
    try:
        connection = websocket.create_connection(url, header=headers, timeout=30)
    except Exception:
        record(
            config.sqlite_path,
            "tunnel_handshake",
            tx_bytes=len(url.encode()) + sum(len(header.encode()) for header in headers),
            success=False,
            tunnel_attempt=True,
        )
        raise
    record(
        config.sqlite_path,
        "tunnel_handshake",
        tx_bytes=len(url.encode()) + sum(len(header.encode()) for header in headers),
        success=True,
        tunnel_attempt=True,
    )
    timeout_errors = (TimeoutError, getattr(websocket, "WebSocketTimeoutException", TimeoutError))
    try:
        # A short receive timeout is local-only.  It lets a revoked/expired
        # lease close an otherwise idle WebSocket without a tunnel keepalive.
        if requested is not None and hasattr(connection, "settimeout"):
            connection.settimeout(1)
        while True:
            if requested is not None and not requested():
                return True
            try:
                raw_message = connection.recv()
            except timeout_errors:
                continue
            if not raw_message:
                continue
            message = json.loads(raw_message)
            if message.get("type") == "lease_revoked":
                logger.info("Cloud tunnel lease ended for %s", config.gateway_id)
                return True
            response = handle_tunnel_message(config, message)
            encoded = json.dumps(response)
            record(
                config.sqlite_path,
                "tunnel_payload",
                tx_bytes=len(encoded.encode()),
                rx_bytes=len(raw_message.encode()),
                success=True,
            )
            connection.send(encoded)
    finally:
        connection.close()


class TunnelLeaseWorker:
    """One bounded, Cloud-lease-controlled tunnel worker per edge process."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._lease_expires_at = 0.0
        self._thread: threading.Thread | None = None

    def update(self, lease_expires_at: float | None) -> None:
        with self._lock:
            self._lease_expires_at = lease_expires_at or 0.0
            active = self._is_requested_locked()
            if active and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name="iot-tunnel-lease", daemon=True)
                self._thread.start()

    def is_requested(self) -> bool:
        with self._lock:
            return self._is_requested_locked()

    def _is_requested_locked(self) -> bool:
        return self.config.is_provisioned and self.config.tunnel_enabled and time.time() < self._lease_expires_at

    def _run(self) -> None:
        run_tunnel_forever(self.config, requested=self.is_requested)


def local_ui_base_url(config: AgentConfig) -> str:
    configured = config.local_ui_url.rstrip("/")
    if configured != ALLOWED_LOCAL_UI_URL:
        raise ValueError("Gateway tunnel target is not allowlisted")
    return ALLOWED_LOCAL_UI_URL


def _parse_cookie_pairs(cookie_header: str | None) -> list[tuple[str, str]]:
    if not cookie_header:
        return []
    pairs: list[tuple[str, str]] = []
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            pairs.append((name, value))
    return pairs


def _cookie_summary(cookie_header: str | None) -> tuple[str, int]:
    pairs = _parse_cookie_pairs(cookie_header)
    if not pairs:
        return "", 0
    return ",".join(name for name, _ in pairs), len(pairs)


def _header_names(headers: dict[object, object]) -> str:
    names = sorted(
        str(key).lower()
        for key in headers
        if str(key).lower() not in SENSITIVE_LOG_HEADER_NAMES
    )
    return ",".join(names)


def _location_shape(location: str | None) -> str:
    if not location:
        return "none"
    parsed = urlparse(location.strip())
    path = parsed.path or "/"
    if parsed.scheme or parsed.netloc:
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() == "http" and host in {"127.0.0.1", "localhost"} and parsed.port == 5000:
            return f"gateway-local:{path}"
        return "external"
    return f"relative:{path}"


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

        if not path.startswith("/") or "://" in path:
            raise ValueError("Unsupported tunnel path")

        url = f"{local_ui_base_url(config)}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        forwarded_headers = {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).lower() not in STRIPPED_LOCAL_HEADERS
        }
        received_cookie_names, received_cookie_count = _cookie_summary(
            next((str(value) for key, value in headers.items() if str(key).lower() == "cookie"), None)
        )
        forwarded_cookie_names, forwarded_cookie_count = _cookie_summary(
            next((value for key, value in forwarded_headers.items() if key.lower() == "cookie"), None)
        )
        logger.warning(
            "EDGE_TUNNEL_DEBUG request gateway=%s local_method=%s local_path=%s received_header_names=%s "
            "forwarded_local_header_names=%s received_cookie_names=%s received_cookie_count=%s "
            "forwarded_cookie_names=%s forwarded_cookie_count=%s",
            config.gateway_id,
            method,
            path,
            _header_names(headers),
            _header_names(forwarded_headers),
            received_cookie_names,
            received_cookie_count,
            forwarded_cookie_names,
            forwarded_cookie_count,
        )

        response = requests.request(
            method,
            url,
            headers=forwarded_headers,
            data=base64.b64decode(body_b64),
            timeout=config.tunnel_request_timeout_sec,
            allow_redirects=False,
        )
        logger.warning(
            "EDGE_TUNNEL_DEBUG response gateway=%s local_method=%s local_path=%s local_response_status=%s "
            "local_response_location_shape=%s",
            config.gateway_id,
            method,
            path,
            response.status_code,
            _location_shape(response.headers.get("Location")),
        )
        return {
            "type": "response",
            "request_id": request_id,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body_b64": base64.b64encode(response.content).decode("ascii"),
        }
    except requests.RequestException:
        logger.exception("Tunnel request failed")
        return {"type": "error", "request_id": request_id, "error": "Local gateway UI unavailable"}
    except Exception as exc:
        logger.exception("Tunnel request failed")
        return {"type": "error", "request_id": request_id, "error": str(exc)}
