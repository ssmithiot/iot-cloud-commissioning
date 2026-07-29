import asyncio
from base64 import b64decode, b64encode
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import threading
import time
from uuid import uuid4

from fastapi import WebSocket


@dataclass
class TunnelResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class TunnelUnavailable(Exception):
    pass


class TunnelRequestFailed(Exception):
    pass


@dataclass(frozen=True)
class TunnelConsoleSession:
    session_id: str
    gateway_id: str
    subject: str
    expires_at: datetime


class GatewayTunnel:
    def __init__(self, gateway_id: str, websocket: WebSocket) -> None:
        self.gateway_id = gateway_id
        self.websocket = websocket
        self.pending: dict[str, asyncio.Future[TunnelResponse]] = {}

    async def request(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_sec: float,
    ) -> TunnelResponse:
        request_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TunnelResponse] = loop.create_future()
        self.pending[request_id] = future

        await self.websocket.send_json(
            {
                "type": "request",
                "request_id": request_id,
                "method": method,
                "path": path,
                "query_string": query_string,
                "headers": dict(headers),
                "body_b64": b64encode(body).decode("ascii"),
            }
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout_sec)
        finally:
            self.pending.pop(request_id, None)

    def resolve_response(self, message: dict[str, object]) -> None:
        request_id = str(message.get("request_id", ""))
        future = self.pending.get(request_id)
        if future is None or future.done():
            return

        if message.get("type") == "error":
            future.set_exception(TunnelRequestFailed(str(message.get("error", "Tunnel request failed"))))
            return

        headers = message.get("headers")
        body_b64 = message.get("body_b64")
        status_code = message.get("status_code")
        if not isinstance(headers, dict) or not isinstance(body_b64, str) or not isinstance(status_code, int):
            future.set_exception(TunnelRequestFailed("Tunnel returned an invalid response"))
            return

        future.set_result(
            TunnelResponse(
                status_code=status_code,
                headers={str(key): str(value) for key, value in headers.items()},
                body=b64decode(body_b64),
            )
        )

    def fail_pending(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(TunnelUnavailable("Gateway tunnel disconnected"))
        self.pending.clear()

    async def close(self, code: int = 1012) -> None:
        try:
            await self.websocket.close(code=code)
        except RuntimeError:
            pass


class TunnelManager:
    def __init__(self) -> None:
        self._tunnels: dict[str, GatewayTunnel] = {}

    def register(self, gateway_id: str, websocket: WebSocket) -> tuple[GatewayTunnel, GatewayTunnel | None]:
        existing = self._tunnels.get(gateway_id)
        if existing is not None:
            existing.fail_pending()

        tunnel = GatewayTunnel(gateway_id, websocket)
        self._tunnels[gateway_id] = tunnel
        return tunnel, existing

    def unregister(self, gateway_id: str, tunnel: GatewayTunnel) -> None:
        if self._tunnels.get(gateway_id) is tunnel:
            tunnel.fail_pending()
            self._tunnels.pop(gateway_id, None)

    def get(self, gateway_id: str) -> GatewayTunnel:
        tunnel = self._tunnels.get(gateway_id)
        if tunnel is None:
            raise TunnelUnavailable("Gateway tunnel is not connected")
        return tunnel

    def is_connected(self, gateway_id: str) -> bool:
        return gateway_id in self._tunnels

    def active_count(self) -> int:
        return len(self._tunnels)


class TunnelSessionManager:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, TunnelConsoleSession] = {}

    def create(self, *, gateway_id: str, subject: str, ttl_seconds: int | None = None) -> TunnelConsoleSession:
        self._expire_old()
        session_ttl = self.ttl_seconds if ttl_seconds is None else max(300, min(3600, int(ttl_seconds)))
        session = TunnelConsoleSession(
            session_id=secrets.token_urlsafe(32),
            gateway_id=gateway_id,
            subject=subject,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=session_ttl),
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, *, gateway_id: str, session_id: str) -> TunnelConsoleSession:
        self._expire_old()
        session = self._sessions.get(session_id)
        if session is None or session.gateway_id != gateway_id:
            raise TunnelUnavailable("Tunnel console session is not valid")
        if session.expires_at <= datetime.now(timezone.utc):
            self._sessions.pop(session_id, None)
            raise TunnelUnavailable("Tunnel console session expired")
        return session

    def _expire_old(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [session_id for session_id, session in self._sessions.items() if session.expires_at <= now]
        for session_id in expired:
            self._sessions.pop(session_id, None)


class TunnelAuthGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_use = 0

    def try_acquire(self, limit: int) -> bool:
        with self._lock:
            if self._in_use >= limit:
                return False
            self._in_use += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._in_use = max(0, self._in_use - 1)

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    def reset(self) -> None:
        with self._lock:
            self._in_use = 0


class TunnelMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.auth_attempts = 0
        self.accepted = 0
        self.rejected = 0
        self.duplicate_replacements = 0
        self.auth_durations_ms: deque[float] = deque(maxlen=200)
        self.db_checkout_wait_ms: deque[float] = deque(maxlen=200)
        self.auth_attempt_monotonic: deque[float] = deque(maxlen=500)

    def record_auth_attempt(self) -> None:
        with self._lock:
            self.auth_attempts += 1
            self.auth_attempt_monotonic.append(time.monotonic())

    def record_auth_duration(self, duration_ms: float) -> None:
        with self._lock:
            self.auth_durations_ms.append(duration_ms)

    def record_db_checkout_wait(self, duration_ms: float) -> None:
        with self._lock:
            self.db_checkout_wait_ms.append(duration_ms)

    def record_accepted(self) -> None:
        with self._lock:
            self.accepted += 1

    def record_rejected(self) -> None:
        with self._lock:
            self.rejected += 1

    def record_duplicate_replacement(self) -> None:
        with self._lock:
            self.duplicate_replacements += 1

    def snapshot(self, *, active_tunnels: int, auth_gate_in_use: int, auth_gate_limit: int) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            attempts_per_second = sum(1 for attempted_at in self.auth_attempt_monotonic if now - attempted_at <= 1)
            durations = list(self.auth_durations_ms)
            checkout_waits = list(self.db_checkout_wait_ms)
            avg_duration = round(sum(durations) / len(durations), 1) if durations else 0.0
            max_duration = round(max(durations), 1) if durations else 0.0
            avg_checkout_wait = round(sum(checkout_waits) / len(checkout_waits), 1) if checkout_waits else 0.0
            max_checkout_wait = round(max(checkout_waits), 1) if checkout_waits else 0.0
            return {
                "active_tunnels": active_tunnels,
                "auth_gate_in_use": auth_gate_in_use,
                "auth_gate_limit": auth_gate_limit,
                "auth_attempts_total": self.auth_attempts,
                "auth_attempts_per_second": attempts_per_second,
                "accepted_total": self.accepted,
                "rejected_total": self.rejected,
                "duplicate_replacements_total": self.duplicate_replacements,
                "auth_duration_avg_ms": avg_duration,
                "auth_duration_max_ms": max_duration,
                "db_checkout_wait_avg_ms": avg_checkout_wait,
                "db_checkout_wait_max_ms": max_checkout_wait,
            }

    def reset(self) -> None:
        with self._lock:
            self.auth_attempts = 0
            self.accepted = 0
            self.rejected = 0
            self.duplicate_replacements = 0
            self.auth_durations_ms.clear()
            self.db_checkout_wait_ms.clear()
            self.auth_attempt_monotonic.clear()


tunnel_manager = TunnelManager()
tunnel_session_manager = TunnelSessionManager()
tunnel_auth_gate = TunnelAuthGate()
tunnel_metrics = TunnelMetrics()
