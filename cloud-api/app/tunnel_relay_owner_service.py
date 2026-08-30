"""Standalone Render Private Service for the controlled relay canary."""
from __future__ import annotations

import asyncio
import os
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.tunnel import TunnelRequestFailed, TunnelUnavailable, tunnel_manager, tunnel_session_manager

SECRET_ENV = "IOT_TUNNEL_RELAY_INTERNAL_SECRET"
app = FastAPI(title="IOT tunnel relay owner", docs_url=None, redoc_url=None)


def configured_secret() -> str | None:
    return os.environ.get(SECRET_ENV)


def require_internal(secret: str | None) -> None:
    if not configured_secret() or secret != configured_secret():
        raise HTTPException(status_code=403, detail="internal relay authentication failed")


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok" if configured_secret() else "misconfigured", "internal_secret_configured": bool(configured_secret())}


@app.websocket("/internal/tunnel-relay/owner/{gateway_id}")
async def owner(gateway_id: str, websocket: WebSocket, expires_at: datetime | None = Query(default=None)) -> None:
    if not configured_secret() or websocket.headers.get("x-iot-relay-owner-auth") != configured_secret():
        await websocket.close(code=1008)
        return
    await websocket.accept()
    tunnel, _ = tunnel_manager.register(gateway_id, websocket)
    expiry_task: asyncio.Task[None] | None = None
    if expires_at is not None:
        async def expire() -> None:
            delay = max(0, (expires_at - datetime.now(timezone.utc)).total_seconds())
            await asyncio.sleep(delay)
            tunnel_session_manager.revoke_gateway(gateway_id)
            await tunnel_manager.close_gateway(gateway_id, code=1000)
        expiry_task = asyncio.create_task(expire())
    try:
        while True:
            tunnel.resolve_response(await websocket.receive_json())
    except WebSocketDisconnect:
        pass
    finally:
        if expiry_task is not None:
            expiry_task.cancel()
        tunnel_manager.unregister(gateway_id, tunnel)


@app.get("/internal/tunnel-relay/status/{gateway_id}")
async def status(gateway_id: str, x_iot_relay_owner_auth: str | None = Header(default=None)) -> dict[str, bool]:
    require_internal(x_iot_relay_owner_auth)
    return {"connected": tunnel_manager.is_connected(gateway_id)}


@app.post("/internal/tunnel-relay/session/{gateway_id}")
async def create_session(gateway_id: str, payload: dict[str, Any], x_iot_relay_owner_auth: str | None = Header(default=None)) -> dict[str, str]:
    require_internal(x_iot_relay_owner_auth)
    if not tunnel_manager.is_connected(gateway_id):
        raise HTTPException(status_code=503, detail="Gateway tunnel is not connected")
    session = tunnel_session_manager.create(gateway_id=gateway_id, subject=str(payload.get("subject", "internal")), ttl_seconds=payload.get("ttl_seconds"))
    return {"session_id": session.session_id}


@app.post("/internal/tunnel-relay/session/{gateway_id}/{session_id}/validate")
async def validate_session(gateway_id: str, session_id: str, x_iot_relay_owner_auth: str | None = Header(default=None)) -> dict[str, bool]:
    require_internal(x_iot_relay_owner_auth)
    try:
        tunnel_session_manager.get(gateway_id=gateway_id, session_id=session_id)
    except TunnelUnavailable:
        return {"valid": False}
    return {"valid": True}


@app.post("/internal/tunnel-relay/request/{gateway_id}")
async def request(gateway_id: str, payload: dict[str, Any], x_iot_relay_owner_auth: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal(x_iot_relay_owner_auth)
    try:
        response = await tunnel_manager.get(gateway_id).request(
            method=str(payload["method"]), path=str(payload["path"]), query_string=str(payload.get("query_string", "")),
            headers={str(k): str(v) for k, v in dict(payload.get("headers", {})).items()},
            body=b64decode(str(payload.get("body_b64", ""))), timeout_sec=float(payload.get("timeout_sec", 900)),
        )
    except (TunnelUnavailable, TunnelRequestFailed) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status_code": response.status_code, "headers": response.headers, "body_b64": b64encode(response.body).decode("ascii")}


@app.post("/internal/tunnel-relay/close/{gateway_id}")
async def close(gateway_id: str, x_iot_relay_owner_auth: str | None = Header(default=None)) -> dict[str, bool]:
    require_internal(x_iot_relay_owner_auth)
    tunnel_session_manager.revoke_gateway(gateway_id)
    return {"closed": await tunnel_manager.close_gateway(gateway_id)}
