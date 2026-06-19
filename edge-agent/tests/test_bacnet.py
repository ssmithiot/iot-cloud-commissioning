import subprocess
from pathlib import Path

from iot_cx_agent.bacnet import parse_bacwi_output
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


def config(tmp_path: Path, bacwi_path: str = "bacwi") -> AgentConfig:
    return AgentConfig(
        gateway_id="GW001",
        site_id="demo-site",
        cloud_url="http://localhost:8000",
        bacnet_default_port=47814,
        bacwi_path=bacwi_path,
        bacnet_timeout_sec=10,
        agent_version="0.1.0",
        ui_version="0.1.0",
        sqlite_path=tmp_path / "edge.db",
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
