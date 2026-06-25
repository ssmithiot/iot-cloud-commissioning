from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_systemd_service_matches_gateway_runtime() -> None:
    service = (REPO_ROOT / "deploy" / "iot-cx-agent.service").read_text(encoding="utf-8")

    assert "User=swadmin" in service
    assert "Group=swadmin" in service
    assert "EnvironmentFile=-/etc/iot-cx-agent/edge-agent.env" in service
    assert "--config /etc/iot-cx-agent/agent.yaml" in service
    assert "User=root" not in service


def test_example_config_is_clone_safe() -> None:
    config = (REPO_ROOT / "edge-agent" / "config.example.yaml").read_text(encoding="utf-8")

    assert "gateway_id: UNPROVISIONED" in config
    assert "site_id: UNPROVISIONED" in config
    assert "gateway_api_token:" not in "\n".join(
        line for line in config.splitlines() if not line.lstrip().startswith("#")
    )
    assert "47814" in config
    assert "47808" not in config
