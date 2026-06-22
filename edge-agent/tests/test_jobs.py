from pathlib import Path

from iot_cx_agent.config import AgentConfig, load_config
from iot_cx_agent.heartbeat import auth_headers
from iot_cx_agent.jobs import execute_job


def config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        agent_version="0.1.0",
        ui_version="0.1.0",
        sqlite_path=tmp_path / "edge.db",
    )


def test_echo_job_returns_expected_payload(tmp_path: Path) -> None:
    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-1", "job_type": "echo", "request": {"message": "hello edge"}},
    )

    assert status == "completed"
    assert error is None
    assert result == {
        "echo": True,
        "request": {"message": "hello edge"},
        "gateway_id": "GW001",
        "agent_version": "0.1.0",
    }


def test_agent_config_keeps_jobs_on_cloud_api_and_local_sqlite(tmp_path: Path) -> None:
    agent_config = config(tmp_path)

    assert agent_config.cloud_url == "http://localhost:8000"
    assert agent_config.sqlite_path == tmp_path / "edge.db"
    assert not hasattr(agent_config, "database_url")


def test_auth_headers_use_gateway_api_token(tmp_path: Path) -> None:
    agent_config = AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        sqlite_path=tmp_path / "edge.db",
        gateway_api_token="iotcc_gw_prefix_secret",
    )

    assert auth_headers(agent_config) == {"Authorization": "Bearer iotcc_gw_prefix_secret"}


def test_load_config_reads_gateway_api_token_from_env(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW001\nsite_id: demo-site\ncloud_url: http://localhost:8000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_API_TOKEN", "iotcc_gw_prefix_secret")

    agent_config = load_config(config_path)

    assert agent_config.gateway_api_token == "iotcc_gw_prefix_secret"


def test_unknown_job_type_fails_gracefully(tmp_path: Path) -> None:
    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-2", "job_type": "not-real", "request": {}},
    )

    assert status == "failed"
    assert result is None
    assert error == "Unknown job_type: not-real"
