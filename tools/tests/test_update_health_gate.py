"""Post-update health gate and stop-the-line tests for the update worker."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.legacy_edge_upgrade_webapp import (  # noqa: E402
    evaluate_post_update_health,
    health_gate_enabled,
    wait_for_post_update_health,
)


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
FINISHED_AT = NOW - timedelta(minutes=2)


def healthy_gateway(**overrides: object) -> dict[str, object]:
    gateway: dict[str, object] = {
        "gateway_id": "GW001",
        "latest_heartbeat_at": (NOW - timedelta(seconds=30)).isoformat(),
        "effective_status": "online",
        "sqlite_db_ok": True,
        "agent_version": "0.1.6",
    }
    gateway.update(overrides)
    return gateway


def test_healthy_gateway_passes_and_reports_version() -> None:
    healthy, detail = evaluate_post_update_health(healthy_gateway(), FINISHED_AT)
    assert healthy is True
    assert "0.1.6" in detail


def test_heartbeat_from_before_update_fails() -> None:
    stale = healthy_gateway(latest_heartbeat_at=(FINISHED_AT - timedelta(minutes=5)).isoformat())
    healthy, detail = evaluate_post_update_health(stale, FINISHED_AT)
    assert healthy is False
    assert "no heartbeat received since the update finished" in detail


def test_missing_or_garbage_heartbeat_fails() -> None:
    for raw in (None, "", "not-a-date"):
        healthy, _ = evaluate_post_update_health(healthy_gateway(latest_heartbeat_at=raw), FINISHED_AT)
        assert healthy is False


def test_non_online_status_fails() -> None:
    healthy, detail = evaluate_post_update_health(healthy_gateway(effective_status="stale"), FINISHED_AT)
    assert healthy is False and "'stale'" in detail


def test_bad_sqlite_fails() -> None:
    healthy, detail = evaluate_post_update_health(healthy_gateway(sqlite_db_ok=False), FINISHED_AT)
    assert healthy is False and "sqlite_db_ok" in detail


def test_wait_polls_until_healthy() -> None:
    responses = iter(
        [
            healthy_gateway(latest_heartbeat_at=(FINISHED_AT - timedelta(minutes=1)).isoformat()),  # pre-update heartbeat
            healthy_gateway(),  # fresh heartbeat arrives
        ]
    )
    sleeps: list[float] = []
    healthy, detail = wait_for_post_update_health(
        "https://cloud.example.test",
        "token",
        "GW001",
        FINISHED_AT,
        timeout_sec=60,
        poll_sec=5,
        fetch=lambda *args, **kwargs: next(responses),
        sleep=sleeps.append,
    )
    assert healthy is True
    assert sleeps == [5]  # one wait between the two polls


def test_wait_times_out_with_last_failure_detail() -> None:
    healthy, detail = wait_for_post_update_health(
        "https://cloud.example.test",
        "token",
        "GW001",
        FINISHED_AT,
        timeout_sec=0,  # immediate deadline: single poll
        poll_sec=1,
        fetch=lambda *args, **kwargs: healthy_gateway(effective_status="offline"),
        sleep=lambda _: None,
    )
    assert healthy is False
    assert "post-update health gate failed" in detail
    assert "'offline'" in detail


def test_health_gate_enabled_env_switch(monkeypatch) -> None:
    monkeypatch.delenv("IOT_EDGE_UPDATE_HEALTH_GATE", raising=False)
    assert health_gate_enabled() is True
    for off in ("false", "0", "no", "OFF"):
        monkeypatch.setenv("IOT_EDGE_UPDATE_HEALTH_GATE", off)
        assert health_gate_enabled() is False
    monkeypatch.setenv("IOT_EDGE_UPDATE_HEALTH_GATE", "true")
    assert health_gate_enabled() is True
