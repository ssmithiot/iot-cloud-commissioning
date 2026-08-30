"""Exact-ID, fail-closed Cloud-to-private-owner relay canary."""
from __future__ import annotations

import asyncio
from base64 import b64decode, b64encode
import re
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from fastapi import WebSocket, WebSocketDisconnect
import httpx

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def selected(gateway_id: str, *, enabled: bool, configured_ids: str) -> bool:
    if not enabled or not _ID.fullmatch(gateway_id):
        return False
    return gateway_id in {item for item in configured_ids.split(",") if _ID.fullmatch(item)}


async def _close(ws: WebSocket, code: int = 1013) -> None:
    try:
        await ws.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _public_to_owner(public: WebSocket, owner: object) -> None:
    while True:
        frame = await public.receive()
        if frame["type"] == "websocket.disconnect": return
        if frame.get("text") is not None: await owner.send(frame["text"])
        elif frame.get("bytes") is not None: await owner.send(frame["bytes"])


async def _owner_to_public(owner: object, public: WebSocket) -> None:
    async for frame in owner:
        if isinstance(frame, bytes): await public.send_bytes(frame)
        else: await public.send_text(frame)


def owner_websocket_target(owner_url: str, gateway_id: str, expires_at: str | None) -> str:
    target = f"{owner_url.rstrip('/')}/{quote(gateway_id, safe='')}"
    if expires_at:
        target = f"{target}?{urlencode({'expires_at': expires_at})}"
    return target


async def relay_client(gateway_id: str, public: WebSocket, *, owner_url: str | None, owner_secret: str | None, expires_at: str | None = None) -> None:
    """Relay a selected Agent connection, with no legacy-manager fallback."""
    if not owner_url or not owner_secret:
        await _close(public)
        return
    try:
        import websockets
        target = owner_websocket_target(owner_url, gateway_id, expires_at)
        async with websockets.connect(target, additional_headers={"x-iot-relay-owner-auth": owner_secret}, open_timeout=10) as owner:
            tasks = [asyncio.create_task(_public_to_owner(public, owner)), asyncio.create_task(_owner_to_public(owner, public))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass
    await _close(public)


def owner_api_base(owner_url: str) -> str:
    parsed = urlsplit(owner_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    marker = "/owner"
    if marker not in parsed.path:
        raise ValueError("owner URL must end in the private owner path")
    return urlunsplit((scheme, parsed.netloc, parsed.path.rsplit(marker, 1)[0], "", ""))


async def owner_api(owner_url: str | None, owner_secret: str | None, method: str, path: str, payload: dict | None = None) -> dict:
    if not owner_url or not owner_secret:
        raise TunnelOwnerUnavailable("Tunnel owner is not configured")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(method, owner_api_base(owner_url) + path, headers={"x-iot-relay-owner-auth": owner_secret}, json=payload)
            response.raise_for_status()
            return dict(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise TunnelOwnerUnavailable("Tunnel owner is unavailable") from exc


class TunnelOwnerUnavailable(Exception):
    pass
