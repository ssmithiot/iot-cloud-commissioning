import subprocess
from pathlib import Path

from iot_cx_agent.bacnet import parse_bacnet_object_list, parse_bacwi_output, run_bacnet_runtime_check
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.jobs import execute_job


SAMPLE_BACWI_OUTPUT = """
;Device   MAC (hex)            SNET  SADR (hex)           APDU
;-------- -------------------- ----- -------------------- ----
20001   C0:A8:01:66:BA:C6    1     C0:A8:01:66:BA:C0    1476
46      C0:A8:01:66:BA:C6    1     C0:A8:01:67:BA:C0    1476
1       C0:A8:01:66:BA:C6    2001  01                   480
50      C0:A8:01:66:BA:C6    2001  03                   480
"""

SAMPLE_OBJECT_LIST_OUTPUT = """
object-list: (device, 1)
object-list: (binary-input, 3)
object-list: (binary-output, 1)
object-list: (analog-value, 2)
object-list: (binary-input, 3)
"""


def config(
    tmp_path: Path,
    bacwi_path: str = "bacwi",
    bacrp_path: str = "bacrp",
    bacrpm_path: str = "bacrpm",
    bacnet_default_port: int = 47814,
) -> AgentConfig:
    return AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        bacnet_default_port=bacnet_default_port,
        bacwi_path=bacwi_path,
        bacrp_path=bacrp_path,
        bacrpm_path=bacrpm_path,
        bacnet_timeout_sec=10,
        agent_version="0.1.0",
        ui_version="0.1.0",
        sqlite_path=tmp_path / "edge.db",
        bacnet_lock_path=tmp_path / "bacnet.lock",
    )


def test_parse_bacwi_output() -> None:
    devices = parse_bacwi_output(SAMPLE_BACWI_OUTPUT)

    assert devices == [
        {
            "device_id": 20001,
            "mac": "C0:A8:01:66:BA:C6",
            "network": 1,
            "sadr": "C0:A8:01:66:BA:C0",
            "apdu": 1476,
        },
        {
            "device_id": 46,
            "mac": "C0:A8:01:66:BA:C6",
            "network": 1,
            "sadr": "C0:A8:01:67:BA:C0",
            "apdu": 1476,
        },
        {
            "device_id": 1,
            "mac": "C0:A8:01:66:BA:C6",
            "network": 2001,
            "sadr": "01",
            "apdu": 480,
        },
        {
            "device_id": 50,
            "mac": "C0:A8:01:66:BA:C6",
            "network": 2001,
            "sadr": "03",
            "apdu": 480,
        },
    ]


def test_parse_bacnet_object_list_skips_device_and_duplicates() -> None:
    points = parse_bacnet_object_list(SAMPLE_OBJECT_LIST_OUTPUT)

    assert points == [
        {
            "object_type": "binary-input",
            "object_instance": 3,
            "property_name": "present-value",
            "object_name": None,
            "present_value": None,
            "units": None,
            "writable": None,
        },
        {
            "object_type": "binary-output",
            "object_instance": 1,
            "property_name": "present-value",
            "object_name": None,
            "present_value": None,
            "units": None,
            "writable": None,
        },
        {
            "object_type": "analog-value",
            "object_instance": 2,
            "property_name": "present-value",
            "object_name": None,
            "present_value": None,
            "units": None,
            "writable": None,
        },
    ]


def test_bacnet_discover_success_with_mocked_command(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        assert args[0] == ["bacwi"]
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        return subprocess.CompletedProcess(args[0], 0, stdout=SAMPLE_BACWI_OUTPUT, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-1", "job_type": "bacnet_discover", "request": {"port": 47814, "timeout_sec": 10}},
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert result["bacnet_discover"] is True
    assert result["port"] == 47814
    assert result["device_count"] == 4
    assert result["devices"][2]["device_id"] == 1


def test_bacnet_load_points_success_with_mocked_command(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        if args[0] == ["bacrp", "1", "device", "1", "76"]:
            return subprocess.CompletedProcess(args[0], 0, stdout=SAMPLE_OBJECT_LIST_OUTPUT, stderr="")
        if args[0] == ["bacrp", "1", "device", "1", "76", "0"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: 4\n", stderr="")
        if args[0] == ["bacrp", "1", "device", "1", "76", "1"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: (device, 1)\n", stderr="")
        if args[0] == ["bacrp", "1", "device", "1", "76", "2"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: (binary-input, 3)\n", stderr="")
        if args[0] == ["bacrp", "1", "device", "1", "76", "3"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: (binary-output, 1)\n", stderr="")
        if args[0] == ["bacrp", "1", "device", "1", "76", "4"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: (analog-value, 2)\n", stderr="")
        object_type = args[0][2]
        object_instance = args[0][3]
        return subprocess.CompletedProcess(args[0], 0, stdout=f"object-name: {object_type} {object_instance}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-load-1", "job_type": "bacnet_load_points", "request": {"device_instance": 1, "limit": 10}},
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert result["job_type"] == "bacnet_load_points"
    assert result["bacnet_port"] == 47814
    assert result["object_count"] == 3
    assert result["read_mode"] == "full"
    assert result["point_count"] == 3
    assert result["points"][0]["object_name"] == "binary-input 3"
    assert ["bacrp", "1", "device", "1", "76"] in calls
    assert ["bacrp", "1", "device", "1", "76", "0"] not in calls
    assert ["bacrp", "1", "device", "1", "76", "-2"] not in calls
    assert ["bacrp", "1", "binary-input", "3", "77"] in calls


def test_bacnet_load_points_falls_back_to_indexed_reads(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        if args[0] == ["bacrp", "1", "device", "1", "76"]:
            return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="APDU timeout")
        if args[0] == ["bacrp", "1", "device", "1", "76", "0"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: 2\n", stderr="")
        if args[0] == ["bacrp", "1", "device", "1", "76", "1"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: (device, 1)\n", stderr="")
        if args[0] == ["bacrp", "1", "device", "1", "76", "2"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="object-list: (binary-input, 3)\n", stderr="")
        object_type = args[0][2]
        object_instance = args[0][3]
        return subprocess.CompletedProcess(args[0], 0, stdout=f"object-name: {object_type} {object_instance}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-load-3", "job_type": "bacnet_load_points", "request": {"device_instance": 1, "limit": 10}},
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert result["read_mode"] == "indexed"
    assert result["object_count"] == 2
    assert result["point_count"] == 1
    assert ["bacrp", "1", "device", "1", "76", "0"] in calls
    assert ["bacrp", "1", "device", "1", "76", "-2"] not in calls


def test_bacnet_load_points_uses_rpm_batch_when_available(tmp_path: Path, monkeypatch) -> None:
    calls = []
    bacrpm_path = tmp_path / "bacrpm"
    bacrpm_path.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        if args[0] == ["bacrp", "1", "device", "1", "76"]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout="object-list: (analog-input, 1)\nobject-list: (analog-input, 2)\n",
                stderr="",
            )
        if args[0] == [str(bacrpm_path), "1", "analog-input", "1", "77,85", "analog-input", "2", "77,85"]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=(
                    "object-identifier: analog-input, 1\n"
                    "object-name: SPACE_SENSOR\n"
                    "present-value: Real: 72.5\n"
                    "object-identifier: analog-input, 2\n"
                    "object-name: REMOTE_SENSOR\n"
                    "present-value: Real: 70.0\n"
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {args[0]}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path, bacrpm_path=str(bacrpm_path)),
        {"job_id": "job-load-4", "job_type": "bacnet_load_points", "request": {"device_instance": 1, "limit": 10}},
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert result["enrichment_mode"] == "rpm"
    assert result["batch_size"] == 40
    assert result["points"][0]["object_name"] == "SPACE_SENSOR"
    assert result["points"][0]["present_value"] == 72.5
    assert result["points"][1]["object_name"] == "REMOTE_SENSOR"
    assert result["points"][1]["present_value"] == 70.0
    assert [str(bacrpm_path), "1", "analog-input", "1", "77,85", "analog-input", "2", "77,85"] in calls


def test_bacnet_load_points_deferred_when_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    agent_config.bacnet_lock_path.write_text("ui-active", encoding="utf-8")

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called while BACnet runtime lock is held")

    monkeypatch.setattr(subprocess, "run", fail_run)

    status, result, error = execute_job(
        agent_config,
        {"job_id": "job-load-2", "job_type": "bacnet_load_points", "request": {"device_instance": 1}},
    )

    assert status == "deferred"
    assert error == "bacnet_runtime_busy"
    assert result is not None
    assert result["status"] == "deferred"
    assert result["error"] == "bacnet_runtime_busy"
    assert result["bacnet_port"] == 47814


def test_bacnet_discover_missing_command_fails_gracefully(tmp_path: Path) -> None:
    status, result, error = execute_job(
        config(tmp_path, bacwi_path="not-a-real-bacwi-command"),
        {"job_id": "job-2", "job_type": "bacnet_discover", "request": {}},
    )

    assert status == "failed"
    assert result is None
    assert error == "BACnet discovery command not found: not-a-real-bacwi-command"


def test_bacnet_discover_timeout_fails_gracefully(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path),
        {"job_id": "job-3", "job_type": "bacnet_discover", "request": {"timeout_sec": 3}},
    )

    assert status == "failed"
    assert result is None
    assert error == "BACnet discovery command timed out after 3 seconds"


def test_bacnet_discover_deferred_when_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    agent_config = config(tmp_path)
    agent_config.bacnet_lock_path.write_text("ui-active", encoding="utf-8")

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called while BACnet runtime lock is held")

    monkeypatch.setattr(subprocess, "run", fail_run)

    status, result, error = execute_job(
        agent_config,
        {"job_id": "job-4", "job_type": "bacnet_discover", "request": {}},
    )

    assert status == "deferred"
    assert error == "bacnet_runtime_busy"
    assert result is not None
    assert result["status"] == "deferred"
    assert result["error"] == "bacnet_runtime_busy"
    assert result["message"] == "Local commissioning UI is using BACnet port 47814. Cloud BACnet job yielded."
    assert result["port"] == 47814


def test_bacnet_runtime_check_success(tmp_path: Path) -> None:
    bacwi_path = tmp_path / "bacwi"
    bacrp_path = tmp_path / "bacrp"
    bacwi_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bacrp_path.write_text("#!/bin/sh\n", encoding="utf-8")

    agent_config = config(tmp_path, bacwi_path=str(bacwi_path), bacrp_path=str(bacrp_path))
    result, error = run_bacnet_runtime_check(agent_config, {})

    assert error is None
    assert result["status"] == "ok"
    assert result["bacnet_port"] == 47814
    assert result["timeout_sec"] == 10
    assert result["lock_path"] == str(agent_config.bacnet_lock_path)
    assert result["lock_held"] is False
    assert result["bacwi_exists"] is True
    assert result["bacrp_exists"] is True


def test_bacnet_runtime_check_accepts_bacnet_port_request_field(tmp_path: Path) -> None:
    bacwi_path = tmp_path / "bacwi"
    bacrp_path = tmp_path / "bacrp"
    bacwi_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bacrp_path.write_text("#!/bin/sh\n", encoding="utf-8")

    agent_config = config(tmp_path, bacwi_path=str(bacwi_path), bacrp_path=str(bacrp_path))
    result, error = run_bacnet_runtime_check(agent_config, {"bacnet_port": 47814})

    assert error is None
    assert result["status"] == "ok"
    assert result["bacnet_port"] == 47814


def test_bacnet_runtime_check_failure_when_port_is_47808(tmp_path: Path) -> None:
    result, error = run_bacnet_runtime_check(config(tmp_path, bacnet_default_port=47808), {})

    assert error == "Cloud BACnet jobs must use UDP 47814"
    assert result["status"] == "error"
    assert result["bacnet_port"] == 47808
