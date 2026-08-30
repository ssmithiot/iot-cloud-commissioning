"""Isolated ASGI relay proof. It is never imported by app.main."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

AUTH_HEADER = "x-iot-relay-owner-auth"


@dataclass
class Pair:
    owner: WebSocket | None = None
    client: WebSocket | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


pairs: dict[str, Pair] = {}
lock = asyncio.Lock()
app = FastAPI(docs_url=None, redoc_url=None)


def secret() -> str:
    return os.environ.get("POC_INTERNAL_RELAY_SECRET", "poc-only-change-me")


@app.get("/poc/relay/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def close(ws: WebSocket | None) -> None:
    if ws:
        try:
            await ws.close(code=1011)
        except (RuntimeError, WebSocketDisconnect):
            pass


async def copy(source: WebSocket, destination: WebSocket) -> None:
    """No queue: each receive is blocked until the peer send completes."""
    while True:
        message = await source.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("text") is not None:
            await destination.send_text(message["text"])
        elif message.get("bytes") is not None:
            await destination.send_bytes(message["bytes"])


async def relay(tunnel_id: str, pair: Pair) -> None:
    assert pair.owner and pair.client
    tasks = [asyncio.create_task(copy(pair.client, pair.owner)), asyncio.create_task(copy(pair.owner, pair.client))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close(pair.client)
        await close(pair.owner)
        pair.done.set()
        async with lock:
            if pairs.get(tunnel_id) is pair:
                pairs.pop(tunnel_id, None)


@app.websocket("/poc/relay/owner/{tunnel_id}")
async def owner(tunnel_id: str, ws: WebSocket) -> None:
    if ws.headers.get(AUTH_HEADER) != secret():
        await ws.close(code=1008)
        return
    await ws.accept()
    async with lock:
        pair = pairs.setdefault(tunnel_id, Pair())
        pair.owner = ws
        if pair.client:
            pair.ready.set()
    try:
        await pair.ready.wait()
        await pair.done.wait()
    finally:
        if pair.client is None:
            async with lock:
                if pairs.get(tunnel_id) is pair:
                    pairs.pop(tunnel_id, None)


@app.websocket("/poc/relay/public/{tunnel_id}")
async def public(tunnel_id: str, ws: WebSocket) -> None:
    await ws.accept()
    async with lock:
        pair = pairs.get(tunnel_id)
        if pair and pair.owner:
            pair.client = ws
            pair.ready.set()
    if pair is None or pair.owner is None:
        await close(ws)
        return
    await relay(tunnel_id, pair)
