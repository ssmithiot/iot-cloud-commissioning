"""Safety properties for the exact-ID production relay canary gate."""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from app.tunnel_relay_canary import relay_client, selected
from app.tunnel_relay_owner_service import app as owner_app


def test_disabled_relay_never_selects_gateway() -> None:
    assert not selected("GW017", enabled=False, configured_ids="GW017")


def test_only_exact_explicit_canary_id_is_selected() -> None:
    assert selected("GW017", enabled=True, configured_ids="GW017")
    assert not selected("GW018", enabled=True, configured_ids="GW017")
    assert not selected("GW017", enabled=True, configured_ids="")
    assert not selected("GW017", enabled=True, configured_ids="GW017*")
    assert not selected("GW017 ", enabled=True, configured_ids="GW017")
    assert not selected("unknown gateway", enabled=True, configured_ids="unknown gateway")


def test_missing_owner_fails_only_selected_gateway_and_never_selects_other() -> None:
    class Client:
        def __init__(self) -> None: self.closed: list[int] = []
        async def close(self, code: int) -> None: self.closed.append(code)

    client = Client()
    asyncio.run(relay_client("GW017", client, owner_url=None, owner_secret=None))
    assert client.closed == [1013]
    assert not selected("GW018", enabled=True, configured_ids="GW017")


def test_normal_routes_are_not_canary_gated() -> None:
    text = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert text.count("relay_canary_selected(") == 1


def test_private_owner_health_and_internal_auth(monkeypatch) -> None:
    client = TestClient(owner_app)
    monkeypatch.delenv("IOT_TUNNEL_RELAY_INTERNAL_SECRET", raising=False)
    assert client.get("/health").json()["status"] == "misconfigured"
    monkeypatch.setenv("IOT_TUNNEL_RELAY_INTERNAL_SECRET", "owner-test-secret")
    assert client.get("/health").json() == {"status": "ok", "internal_secret_configured": True}
    with client.websocket_connect("/internal/tunnel-relay/owner/GW017", headers={"x-iot-relay-owner-auth": "owner-test-secret"}) as socket:
        socket.send_text("owner-authenticated")
        assert socket.receive_text() == "owner-authenticated"
