import asyncio
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
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


class TunnelManager:
    def __init__(self) -> None:
        self._tunnels: dict[str, GatewayTunnel] = {}

    def register(self, gateway_id: str, websocket: WebSocket) -> GatewayTunnel:
        existing = self._tunnels.get(gateway_id)
        if existing is not None:
            existing.fail_pending()

        tunnel = GatewayTunnel(gateway_id, websocket)
        self._tunnels[gateway_id] = tunnel
        return tunnel

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


class TunnelSessionManager:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, TunnelConsoleSession] = {}

    def create(self, *, gateway_id: str, subject: str) -> TunnelConsoleSession:
        self._expire_old()
        session = TunnelConsoleSession(
            session_id=secrets.token_urlsafe(32),
            gateway_id=gateway_id,
            subject=subject,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds),
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


tunnel_manager = TunnelManager()
tunnel_session_manager = TunnelSessionManager()
