"""True multi-process tests for the isolated relay proof."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

import pytest
import websocket


CLOUD = Path(__file__).resolve().parents[1]
SECRET = "test-relay-secret"


def port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def relay():
    value = port()
    env = {**os.environ, "PYTHONPATH": str(CLOUD), "POC_INTERNAL_RELAY_SECRET": SECRET}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.tunnel_relay_poc:app", "--host", "127.0.0.1", "--port", str(value)], cwd=CLOUD, env=env)
    url = f"http://127.0.0.1:{value}"
    for _ in range(100):
        try:
            urlopen(url + "/poc/relay/health", timeout=.1).close()
            break
        except OSError:
            time.sleep(.03)
    else:
        proc.kill(); raise AssertionError("relay failed to start")
    yield url, proc
    if proc.poll() is None:
        proc.terminate(); proc.wait(timeout=5)


def owner(url, tunnel, *args):
    return subprocess.Popen([sys.executable, "-m", "app.tunnel_relay_owner_poc", "--url", url, "--tunnel", tunnel, "--secret", SECRET, *args], cwd=CLOUD, env={**os.environ, "PYTHONPATH": str(CLOUD)})


def client(url, tunnel):
    time.sleep(.12)
    return websocket.create_connection(url.replace("http", "ws", 1) + f"/poc/relay/public/{tunnel}", timeout=2)


def test_text_binary_and_agent_protocol(relay):
    url, _ = relay; process = owner(url, "frames"); ws = client(url, "frames")
    try:
        agent_frame = '{"type":"response","request_id":"x","status_code":200,"body_b64":"AAE="}'
        ws.send(agent_frame); assert ws.recv() == agent_frame
        payload = bytes(range(256)) * 4
        ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY); assert ws.recv() == payload
    finally:
        ws.close(); process.wait(timeout=5)


def test_bidirectional_close_owner_failure_and_restart(relay):
    url, _ = relay; first = owner(url, "restart", "--emit-count", "30"); ws = client(url, "restart")
    values = []
    try:
        sender = threading.Thread(target=lambda: [ws.send(f"client-{i}") for i in range(30)])
        sender.start()
        while len(values) < 60: values.append(ws.recv())
        sender.join(timeout=2)
        assert values.count("owner-push") == 30
        assert {f"client-{i}" for i in range(30)}.issubset(values)
        first.terminate(); first.wait(timeout=5)
        assert ws.recv() == ""
    finally: ws.close()
    second = owner(url, "restart"); fresh = client(url, "restart")
    try: fresh.send("fresh"); assert fresh.recv() == "fresh"
    finally: fresh.close(); second.wait(timeout=5)


def test_owner_auth_and_relay_process_failure(relay):
    url, process = relay
    bad = subprocess.run([sys.executable, "-m", "app.tunnel_relay_owner_poc", "--url", url, "--tunnel", "auth", "--secret", "wrong"], cwd=CLOUD, env={**os.environ, "PYTHONPATH": str(CLOUD)}, timeout=5)
    assert bad.returncode != 0
    owned = owner(url, "auth"); ws = client(url, "auth")
    process.terminate(); process.wait(timeout=5)
    try:
        assert ws.recv() == ""
        owned.wait(timeout=5)
    finally: ws.close()


def test_backpressure_has_no_application_queue():
    source = (CLOUD / "app" / "tunnel_relay_poc.py").read_text()
    assert "asyncio.Queue" not in source
    assert "await destination.send_" in source
