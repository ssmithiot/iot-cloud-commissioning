import os
from pathlib import Path

from iot_cx_agent.config import AgentConfig, load_config, resolve_bacnet_port
from iot_cx_agent.heartbeat import auth_headers
from iot_cx_agent.jobs import execute_job
from iot_cx_agent.main import run_once


def config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        agent_version="0.1.0",
        ui_version="0.1.0",
        sqlite_path=tmp_path / "edge.db",
        bacnet_lock_path=tmp_path / "bacnet.lock",
        bacnet_lock_timeout_sec=0,
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


def test_bacnet_read_job_dispatches_to_handler(tmp_path: Path, monkeypatch) -> None:
    def fake_run_bacnet_read(agent_config, request):
        assert agent_config.gateway_id == "GW001"
        assert request == {
            "device_instance": 1,
            "object_type": "analog-value",
            "object_instance": 1,
            "property": "present-value",
        }
        return {"job_type": "bacnet_read", "status": "ok", "value": 72.4}, None

    monkeypatch.setattr("iot_cx_agent.jobs.run_bacnet_read", fake_run_bacnet_read)

    status, result, error = execute_job(
        config(tmp_path),
        {
            "job_id": "job-2",
            "job_type": "bacnet_read",
            "request": {
                "device_instance": 1,
                "object_type": "analog-value",
                "object_instance": 1,
                "property": "present-value",
            },
        },
    )

    assert status == "completed"
    assert error is None
    assert result == {"job_type": "bacnet_read", "status": "ok", "value": 72.4}


def test_bacnet_bulk_read_job_dispatches_to_handler(tmp_path: Path, monkeypatch) -> None:
    def fake_run_bacnet_read_bulk(agent_config, request):
        assert agent_config.gateway_id == "GW001"
        assert request == {
            "device_instance": 1,
            "points": [{"object_type": "analog-value", "object_instance": 1}],
        }
        return {"job_type": "bacnet_read_bulk", "status": "ok", "value_count": 1}, None

    monkeypatch.setattr("iot_cx_agent.jobs.run_bacnet_read_bulk", fake_run_bacnet_read_bulk)

    status, result, error = execute_job(
        config(tmp_path),
        {
            "job_id": "job-bulk-1",
            "job_type": "bacnet_read_bulk",
            "request": {
                "device_instance": 1,
                "points": [{"object_type": "analog-value", "object_instance": 1}],
            },
        },
    )

    assert status == "completed"
    assert error is None
    assert result == {"job_type": "bacnet_read_bulk", "status": "ok", "value_count": 1}


def test_bacnet_load_points_job_dispatches_to_handler(tmp_path: Path, monkeypatch) -> None:
    def fake_run_bacnet_load_points(agent_config, request):
        assert agent_config.gateway_id == "GW001"
        assert request == {"device_instance": 1, "limit": 10}
        return {"job_type": "bacnet_load_points", "status": "ok", "point_count": 2}, None

    monkeypatch.setattr("iot_cx_agent.jobs.run_bacnet_load_points", fake_run_bacnet_load_points)

    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-load-1", "job_type": "bacnet_load_points", "request": {"device_instance": 1, "limit": 10}},
    )

    assert status == "completed"
    assert error is None
    assert result == {"job_type": "bacnet_load_points", "status": "ok", "point_count": 2}


def test_bacnet_read_deferred_when_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    agent_config.bacnet_lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called while BACnet runtime lock is held")

    monkeypatch.setattr("iot_cx_agent.bacnet.subprocess.run", fail_run)

    status, result, error = execute_job(
        agent_config,
        {
            "job_id": "job-4",
            "job_type": "bacnet_read",
            "request": {
                "device_instance": 1,
                "object_type": "analog-value",
                "object_instance": 1,
                "property": "present-value",
            },
        },
    )

    assert status == "deferred"
    assert error == "bacnet_runtime_busy"
    assert result is not None
    assert result["status"] == "deferred"
    assert result["error"] == "bacnet_runtime_busy"
    assert result["message"] == "BACnet runtime is busy. Another local BACnet command is already using UDP 47814."


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


def test_load_config_reads_tunnel_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW001\n"
        "site_id: demo-site\n"
        "cloud_url: http://localhost:8000\n"
        "tunnel_enabled: false\n"
        "local_ui_url: http://127.0.0.1:5100\n",
        encoding="utf-8",
    )

    agent_config = load_config(config_path)

    assert agent_config.tunnel_enabled is False
    assert agent_config.local_ui_url == "http://127.0.0.1:5100"


def test_load_config_uses_installed_edge_app_version(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW001\n"
        "site_id: demo-site\n"
        "cloud_url: http://localhost:8000\n"
        "agent_version: current\n",
        encoding="utf-8",
    )

    agent_config = load_config(config_path)

    assert agent_config.agent_version == "0.1.6"
    assert agent_config.ui_version == "0.1.0"


def test_unprovisioned_agent_skips_cloud_calls(tmp_path: Path, monkeypatch) -> None:
    agent_config = AgentConfig(
        gateway_id="UNPROVISIONED",
        site_id="UNPROVISIONED",
        cloud_url="http://localhost:8000",
        sqlite_path=tmp_path / "edge.db",
    )

    def fail_send_heartbeat(*args, **kwargs):
        raise AssertionError("unprovisioned gateway should not send a heartbeat")

    def fail_process_next_job(*args, **kwargs):
        raise AssertionError("unprovisioned gateway should not poll for jobs")

    monkeypatch.setattr("iot_cx_agent.main.send_heartbeat", fail_send_heartbeat)
    monkeypatch.setattr("iot_cx_agent.main.process_next_job", fail_process_next_job)

    assert run_once(agent_config) is False


def test_load_config_reads_bacrp_path(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW001\n"
        "site_id: demo-site\n"
        "cloud_url: http://localhost:8000\n"
        "bacnet:\n"
        "  bacrp_path: /opt/bacnet-stack/bin/bacrp\n",
        encoding="utf-8",
    )

    agent_config = load_config(config_path)

    assert agent_config.bacrp_path == "/opt/bacnet-stack/bin/bacrp"


def test_bacnet_port_resolution_defaults_to_contemporary_47814() -> None:
    profile, port = resolve_bacnet_port()

    assert profile == "contemporary"
    assert port == 47814


def test_bacnet_port_resolution_supports_bac_rtr_47809() -> None:
    profile, port = resolve_bacnet_port(profile="bac-rtr")

    assert profile == "bac-rtr"
    assert port == 47809


def test_bacnet_port_resolution_supports_contemporary_aliases() -> None:
    assert resolve_bacnet_port(profile="contemporary") == ("contemporary", 47814)
    assert resolve_bacnet_port(profile="basrtb") == ("basrtb", 47814)


def test_bacnet_ip_port_overrides_router_profile() -> None:
    profile, port = resolve_bacnet_port(profile="bac-rtr", explicit_port="47814")

    assert profile == "bac-rtr"
    assert port == 47814


def test_load_config_reads_bac_rtr_profile_from_env(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW001\nsite_id: demo-site\ncloud_url: http://localhost:8000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BACNET_ROUTER_PROFILE", "bac-rtr")

    agent_config = load_config(config_path)

    assert agent_config.bacnet_router_profile == "bac-rtr"
    assert agent_config.bacnet_default_port == 47809
    assert agent_config.bacnet_lock_path_for_port() == Path("/tmp/iot-edge-bacnet-47809.lock")


def test_load_config_bacnet_ip_port_overrides_profile_default(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW001\nsite_id: demo-site\ncloud_url: http://localhost:8000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BACNET_ROUTER_PROFILE", "contemporary")
    monkeypatch.setenv("BACNET_IP_PORT", "47809")

    agent_config = load_config(config_path)

    assert agent_config.bacnet_router_profile == "contemporary"
    assert agent_config.bacnet_default_port == 47809


def test_unknown_job_type_fails_gracefully(tmp_path: Path) -> None:
    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-3", "job_type": "not-real", "request": {}},
    )

    assert status == "failed"
    assert result is None
    assert error == "Unknown job_type: not-real"


def test_edge_agent_does_not_import_or_reference_supabase_or_postgres_clients() -> None:
    package_root = Path(__file__).resolve().parents[1] / "iot_cx_agent"
    source_text = "\n".join(path.read_text(encoding="utf-8").lower() for path in package_root.rglob("*.py"))

    for forbidden in ("supabase", "psycopg", "sqlalchemy", "postgres"):
        assert forbidden not in source_text
