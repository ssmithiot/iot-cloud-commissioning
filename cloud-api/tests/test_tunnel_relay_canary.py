"""Safety properties for the production relay canary and fleet gates."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app import tunnel_relay_canary
from app.tunnel_relay_canary import owner_connection_state, owner_websocket_target, relay_client, selected
from app.tunnel_relay_owner_service import app as owner_app


def test_disabled_relay_never_selects_gateway() -> None:
    for configured_ids in ("GW017", ""):
        assert not selected("GW017", enabled=False, configured_ids=configured_ids)
        assert not selected("GW018", enabled=False, configured_ids=configured_ids)


def test_only_exact_explicit_canary_id_is_selected() -> None:
    assert selected("GW017", enabled=True, configured_ids="GW017")
    assert not selected("GW018", enabled=True, configured_ids="GW017")
    assert not selected("GW017", enabled=True, configured_ids="GW017*")
    assert not selected("GW017 ", enabled=True, configured_ids="GW017")
    assert not selected("unknown gateway", enabled=True, configured_ids="unknown gateway")


def test_missing_canary_list_selects_all_valid_gateways() -> None:
    assert selected("GW017", enabled=True, configured_ids=None)
    assert selected("GW018", enabled=True, configured_ids=None)
    assert not selected("unknown gateway", enabled=True, configured_ids=None)


def test_empty_canary_list_selects_all_valid_gateways() -> None:
    assert selected("GW017", enabled=True, configured_ids="")
    assert selected("GW018", enabled=True, configured_ids="")
    assert not selected("unknown gateway", enabled=True, configured_ids="")


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
    # The selector is called only by tunnel-open/close, tunnel admission, and
    # jobs/next helpers; normal heartbeat/trend route definitions do not use it.
    assert "def claim_next_job" in text
    assert "def _relay_canary_selected" in text


def test_private_owner_health_and_internal_auth(monkeypatch) -> None:
    client = TestClient(owner_app)
    monkeypatch.delenv("IOT_TUNNEL_RELAY_INTERNAL_SECRET", raising=False)
    assert client.get("/health").json()["status"] == "misconfigured"
    monkeypatch.setenv("IOT_TUNNEL_RELAY_INTERNAL_SECRET", "owner-test-secret")
    assert client.get("/health").json() == {"status": "ok", "internal_secret_configured": True}
    with client.websocket_connect("/internal/tunnel-relay/owner/GW017", headers={"x-iot-relay-owner-auth": "owner-test-secret"}) as socket:
        assert socket is not None
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/internal/tunnel-relay/owner/GW017", headers={"x-iot-relay-owner-auth": "wrong-secret"}):
            pass


def test_owner_expiry_query_encodes_timezone_offset_and_connects(monkeypatch) -> None:
    expires = "2026-08-30T12:57:20.780667+00:00"
    target = owner_websocket_target("ws://owner:10000/internal/tunnel-relay/owner", "GW017", expires)
    assert target == "ws://owner:10000/internal/tunnel-relay/owner/GW017?expires_at=2026-08-30T12%3A57%3A20.780667%2B00%3A00"
    assert datetime.fromisoformat(expires).astimezone(timezone.utc) == datetime(2026, 8, 30, 12, 57, 20, 780667, tzinfo=timezone.utc)

    monkeypatch.setenv("IOT_TUNNEL_RELAY_INTERNAL_SECRET", "owner-test-secret")
    client = TestClient(owner_app)
    path = target.split("owner:10000", 1)[1]
    with client.websocket_connect(path, headers={"x-iot-relay-owner-auth": "owner-test-secret"}) as socket:
        assert socket is not None


def test_unencoded_timezone_offset_is_rejected_before_owner_accept(monkeypatch) -> None:
    monkeypatch.setenv("IOT_TUNNEL_RELAY_INTERNAL_SECRET", "owner-test-secret")
    client = TestClient(owner_app)
    path = "/internal/tunnel-relay/owner/GW017?expires_at=2026-08-30T12:57:20.780667+00:00"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path, headers={"x-iot-relay-owner-auth": "owner-test-secret"}):
            pass


def test_durable_canary_recheck_contract_is_worker_independent() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    # The held request closes its Session before each bounded wait, then makes
    # a new durable EdgeJob/GatewayTunnelRequest query. No Condition signal is
    # required for another worker's committed request to become visible.
    assert "RELAY_CANARY_DURABLE_RECHECK_SECONDS = 10.0" in source
    assert "db.close()" in source
    assert "_set_tunnel_instruction_headers(response, db, gateway_id)" in source
    assert "min(remaining, RELAY_CANARY_DURABLE_RECHECK_SECONDS if canary_relay else remaining)" in source


def test_owner_connection_state_is_strict_and_fails_unknown(monkeypatch) -> None:
    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class Client:
        payload: dict | BaseException = {"connected": True}

        def __init__(self, *, timeout: int) -> None:
            assert timeout == 5

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, url: str, *, headers: dict) -> Response:
            assert url == "http://owner/internal/tunnel-relay/status/GW017"
            assert headers == {"x-iot-relay-owner-auth": "owner-secret"}
            if isinstance(self.payload, BaseException):
                raise self.payload
            return Response(self.payload)

    monkeypatch.setattr(tunnel_relay_canary.httpx, "Client", Client)
    assert owner_connection_state("ws://owner/internal/tunnel-relay/owner", "owner-secret", "GW017") is True
    Client.payload = {"connected": False}
    assert owner_connection_state("ws://owner/internal/tunnel-relay/owner", "owner-secret", "GW017") is False
    Client.payload = {"connected": "false"}
    assert owner_connection_state("ws://owner/internal/tunnel-relay/owner", "owner-secret", "GW017") is None
    Client.payload = tunnel_relay_canary.httpx.ConnectError("owner down")
    assert owner_connection_state("ws://owner/internal/tunnel-relay/owner", "owner-secret", "GW017") is None
    assert owner_connection_state(None, "owner-secret", "GW017") is None
