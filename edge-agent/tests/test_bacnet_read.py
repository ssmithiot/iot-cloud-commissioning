import subprocess
from pathlib import Path

from iot_cx_agent.bacnet import parse_bacnet_read_value
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.jobs import execute_job


def config(tmp_path: Path, bacrp_path: str = "bacrp") -> AgentConfig:
    return AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        bacnet_default_port=47814,
        bacrp_path=bacrp_path,
        bacnet_timeout_sec=10,
        agent_version="0.1.0",
        ui_version="0.1.0",
        sqlite_path=tmp_path / "edge.db",
    )


def bacnet_read_job(request: dict[str, object]) -> dict[str, object]:
    return {"job_id": "job-read-1", "job_type": "bacnet_read", "request": request}


def valid_read_request(object_type: str = "analog-value") -> dict[str, object]:
    return {
        "device_instance": 1,
        "object_type": object_type,
        "object_instance": 1,
        "property": "present-value",
    }


def test_parse_bacnet_read_numeric_analog_value() -> None:
    value, raw_value = parse_bacnet_read_value("present-value: Real: 72.4\n")

    assert value == 72.4
    assert raw_value == "72.4"


def test_parse_bacnet_read_binary_value() -> None:
    value, raw_value = parse_bacnet_read_value("present-value: active\n")

    assert value == "active"
    assert raw_value == "active"


def test_parse_bacnet_read_multi_state_numeric_value() -> None:
    value, raw_value = parse_bacnet_read_value("value = 3\n")

    assert value == 3
    assert raw_value == "3"


def test_bacnet_read_success_with_mocked_command_args(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        assert args[0] == ["bacrp", "1", "analog-value", "1", "85"]
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        assert kwargs["timeout"] == 10
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(args[0], 0, stdout="present-value: Real: 72.4\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(config(tmp_path), bacnet_read_job(valid_read_request()))

    assert status == "completed"
    assert error is None
    assert result == {
        "job_type": "bacnet_read",
        "device_instance": 1,
        "object_type": "analog-value",
        "object_instance": 1,
        "property": "present-value",
        "property_id": 85,
        "value": 72.4,
        "raw_value": "72.4",
        "status": "ok",
    }


def test_bacnet_read_invalid_object_type_fails_cleanly(tmp_path: Path) -> None:
    request = valid_read_request("calendar")

    status, result, error = execute_job(config(tmp_path), bacnet_read_job(request))

    assert status == "failed"
    assert result is not None
    assert result["status"] == "error"
    assert "object_type must be one of" in str(error)


def test_bacnet_read_missing_required_field_fails_cleanly(tmp_path: Path) -> None:
    request = {"device_instance": 1, "object_type": "analog-value"}

    status, result, error = execute_job(config(tmp_path), bacnet_read_job(request))

    assert status == "failed"
    assert result is not None
    assert result["status"] == "error"
    assert error == "object_instance must be an integer"


def test_bacnet_read_timeout_returns_error_result(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"], output="partial output")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(config(tmp_path), bacnet_read_job(valid_read_request()))

    assert status == "failed"
    assert result is not None
    assert result["status"] == "error"
    assert result["raw_output"] == "partial output"
    assert error == "BACnet read command timed out after 10 seconds"


def test_bacnet_read_nonzero_cli_exit_returns_error_result(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 2, stdout="", stderr="APDU timeout")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(config(tmp_path), bacnet_read_job(valid_read_request()))

    assert status == "failed"
    assert result is not None
    assert result["status"] == "error"
    assert result["raw_output"] == "APDU timeout"
    assert error == "BACnet read command failed: APDU timeout"


def test_bacnet_read_unparseable_success_returns_error_result(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="ReadProperty ACK received\nNo value here\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(config(tmp_path), bacnet_read_job(valid_read_request()))

    assert status == "failed"
    assert result is not None
    assert result["status"] == "error"
    assert "did not contain a readable present-value" in str(error)
