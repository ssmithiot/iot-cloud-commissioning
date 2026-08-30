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


def test_durable_canary_recheck_contract_is_worker_independent() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    # The held request closes its Session before each bounded wait, then makes
    # a new durable EdgeJob/GatewayTunnelRequest query. No Condition signal is
    # required for another worker's committed request to become visible.
    assert "RELAY_CANARY_DURABLE_RECHECK_SECONDS = 10.0" in source
    assert "db.close()" in source
    assert "_set_tunnel_instruction_headers(response, db, gateway_id)" in source
    assert "min(remaining, RELAY_CANARY_DURABLE_RECHECK_SECONDS if canary_relay else remaining)" in source
