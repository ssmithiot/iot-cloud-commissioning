"""Separate owner process used only by relay POC tests."""
import argparse
import asyncio
import base64
import os

import websockets


async def run(args: argparse.Namespace) -> None:
    headers = {"x-iot-relay-owner-auth": args.secret}
    private_url = args.url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    async with websockets.connect(f"{private_url}/poc/relay/owner/{args.tunnel}", additional_headers=headers) as ws:
        for _ in range(args.emit_count):
            await ws.send(args.emit)
        async for frame in ws:
            await ws.send(frame)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--tunnel", required=True)
    parser.add_argument("--secret", default=os.environ.get("POC_INTERNAL_RELAY_SECRET", "poc-only-change-me"))
    parser.add_argument("--emit", default="owner-push")
    parser.add_argument("--emit-count", type=int, default=0)
    asyncio.run(run(parser.parse_args()))
