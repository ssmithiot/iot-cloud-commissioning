"""Standalone Render Private Service for the controlled relay canary."""
from __future__ import annotations

import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

SECRET_ENV = "IOT_TUNNEL_RELAY_INTERNAL_SECRET"
app = FastAPI(title="IOT tunnel relay owner", docs_url=None, redoc_url=None)


def configured_secret() -> str | None:
    return os.environ.get(SECRET_ENV)


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok" if configured_secret() else "misconfigured", "internal_secret_configured": bool(configured_secret())}


@app.websocket("/internal/tunnel-relay/owner/{gateway_id}")
async def owner(gateway_id: str, websocket: WebSocket) -> None:
    if not configured_secret() or websocket.headers.get("x-iot-relay-owner-auth") != configured_secret():
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect": return
            if frame.get("text") is not None: await websocket.send_text(frame["text"])
            elif frame.get("bytes") is not None: await websocket.send_bytes(frame["bytes"])
    except WebSocketDisconnect:
        return
