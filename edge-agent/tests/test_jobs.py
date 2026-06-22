from pathlib import Path

from iot_cx_agent.config import AgentConfig
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


def test_unknown_job_type_fails_gracefully(tmp_path: Path) -> None:
    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-2", "job_type": "not-real", "request": {}},
    )

    assert status == "failed"
    assert result is None
    assert error == "Unknown job_type: not-real"
