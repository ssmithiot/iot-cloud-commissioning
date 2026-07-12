import subprocess
import sys
from pathlib import Path

from iot_cx_agent.bacnet import parse_bacnet_read_value, parse_bacnet_rpm_present_values
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.jobs import execute_job


def config(tmp_path: Path, bacrp_path: str = "bacrp", bacrpm_path: str = "bacrpm") -> AgentConfig:
    return AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        bacnet_default_port=47814,
        bacrp_path=bacrp_path,
        bacrpm_path=bacrpm_path,
        bacnet_timeout_sec=10,
        agent_version="0.1.0",
        ui_version="0.1.0",
        sqlite_path=tmp_path / "edge.db",
        bacnet_lock_path=tmp_path / "bacnet.lock",
        bacnet_lock_timeout_sec=0,
    )


def bacnet_read_job(request: dict[str, object]) -> dict[str, object]:
    return {"job_id": "job-read-1", "job_type": "bacnet_read", "request": request}


def bacnet_read_bulk_job(request: dict[str, object]) -> dict[str, object]:
    return {"job_id": "job-read-bulk-1", "job_type": "bacnet_read_bulk", "request": request}


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


def test_parse_bacnet_rpm_present_values() -> None:
    values = parse_bacnet_rpm_present_values(
        """
        analog-value, 1
            present-value: Real: 72.4
        binary-value, 5
            85: active
        """
    )

    assert values[("analog-value", 1)] == (72.4, "72.4")
    assert values[("binary-value", 5)] == ("active", "active")


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
        "bacnet_port": 47814,
        "bacnet_router_profile": "contemporary",
        "value": 72.4,
        "raw_value": "72.4",
        "status": "ok",
    }


def test_bacnet_bulk_read_uses_one_rpm_command_for_multiple_points(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        assert args[0] == [sys.executable, "1", "analog-value", "1", "85", "binary-value", "5", "85"]
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="""
            analog-value, 1
                present-value: Real: 72.4
            binary-value, 5
                present-value: active
            """,
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path, bacrpm_path=sys.executable),
        bacnet_read_bulk_job(
            {
                "device_instance": 1,
                "points": [
                    {
                        "saved_point_id": "point-1",
                        "object_type": "analog-value",
                        "object_instance": 1,
                        "object_name": "Space Temp",
                    },
                    {
                        "saved_point_id": "point-2",
                        "object_type": "binary-value",
                        "object_instance": 5,
                        "object_name": "Fan Status",
                    },
                ],
            }
        ),
    )

    assert status == "completed"
    assert error is None
    assert calls == [[sys.executable, "1", "analog-value", "1", "85", "binary-value", "5", "85"]]
    assert result is not None
    assert result["read_mode"] == "rpm-bulk"
    assert result["requested_count"] == 2
    assert result["value_count"] == 2
    assert result["single_read_fallback_count"] == 0
    assert result["values"] == [
        {
            "saved_point_id": "point-1",
            "object_type": "analog-value",
            "object_instance": 1,
            "value": 72.4,
            "raw_value": "72.4",
            "status": "ok",
            "read_source": "rpm-bulk",
        },
        {
            "saved_point_id": "point-2",
            "object_type": "binary-value",
            "object_instance": 5,
            "value": "active",
            "raw_value": "active",
            "status": "ok",
            "read_source": "rpm-bulk",
        },
    ]


def test_bacnet_bulk_read_uses_edge_priority_array_read_before_property_87(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        if args[0] == [sys.executable, "1", "analog-value", "39", "85"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="analog-value, 39\n  present-value: Real: 69\n", stderr="")
        if args[0] == [sys.executable, "1", "analog-value", "39", "priority-array"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="priority-array: (NULL, NULL, NULL, NULL, NULL, NULL, NULL, Real: 69)\n", stderr="")
        if args[0] == [sys.executable, "1", "analog-value", "39", "relinquish-default"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="relinquish-default: Real: 72\n", stderr="")
        raise AssertionError(f"unexpected command: {args[0]}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path, bacrp_path=sys.executable, bacrpm_path=sys.executable),
        bacnet_read_bulk_job({
            "device_instance": 1,
            "points": [{"saved_point_id": "point-39", "object_type": "analog-value", "object_instance": 39, "read_priority": True, "read_relinquish_default": True}],
        }),
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert calls == [
        [sys.executable, "1", "analog-value", "39", "85"],
        [sys.executable, "1", "analog-value", "39", "priority-array"],
        [sys.executable, "1", "analog-value", "39", "relinquish-default"],
    ]
    assert result["values"][0]["active_priority"] == 8
    assert result["values"][0]["relinquish_default"] == "relinquish-default: Real: 72"


def test_bacnet_bulk_read_falls_back_to_single_reads_when_rpm_returns_no_values(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        if args[0] == [sys.executable, "1", "analog-value", "1", "85", "binary-value", "5", "85"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="no parseable present values here\n", stderr="")
        if args[0] == ["bacrp", "1", "analog-value", "1", "85"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="present-value: Real: 72.4\n", stderr="")
        if args[0] == ["bacrp", "1", "binary-value", "5", "85"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="present-value: inactive\n", stderr="")
        raise AssertionError(f"unexpected command: {args[0]}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path, bacrpm_path=sys.executable),
        bacnet_read_bulk_job(
            {
                "device_instance": 1,
                "points": [
                    {"saved_point_id": "point-1", "object_type": "analog-value", "object_instance": 1},
                    {"saved_point_id": "point-2", "object_type": "binary-value", "object_instance": 5},
                ],
            }
        ),
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert result["value_count"] == 2
    assert result["single_read_fallback_count"] == 2
    assert calls == [
        [sys.executable, "1", "analog-value", "1", "85", "binary-value", "5", "85"],
        ["bacrp", "1", "analog-value", "1", "85"],
        ["bacrp", "1", "binary-value", "5", "85"],
    ]
    assert result["values"] == [
        {
            "saved_point_id": "point-1",
            "object_type": "analog-value",
            "object_instance": 1,
            "value": 72.4,
            "raw_value": "72.4",
            "status": "ok",
            "read_source": "single-fallback",
        },
        {
            "saved_point_id": "point-2",
            "object_type": "binary-value",
            "object_instance": 5,
            "value": "inactive",
            "raw_value": "inactive",
            "status": "ok",
            "read_source": "single-fallback",
        },
    ]


def test_bacnet_read_invalid_object_type_fails_cleanly(tmp_path: Path) -> None:
    request = valid_read_request("calendar")

    status, result, error = execute_job(config(tmp_path), bacnet_read_job(request))

    assert status == "failed"
    assert result is not None
    assert result["status"] == "error"
    assert "object_type received 'calendar'" in str(error)
    assert "must be one of" in str(error)


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
