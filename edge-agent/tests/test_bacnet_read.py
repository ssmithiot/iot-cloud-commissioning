import subprocess
import sys
from pathlib import Path

from iot_cx_agent.bacnet import parse_bacnet_read_value, parse_bacnet_rpm_present_values, prepare_bacnet_command, resolve_bacnet_route
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


def edge_router_authority_config(tmp_path: Path, bacrp_path: str = "bacrp", bacrpm_path: str = "bacrpm") -> AgentConfig:
    edge_dir = tmp_path / "edge-ui-data"
    edge_dir.mkdir()
    (edge_dir / "router-config.json").write_text(
        """
        {
          "enabled": true,
          "bip_interfaces": [
            {
              "enabled": true,
              "bind_address": "192.168.1.200",
              "udp_port": 47809
            }
          ],
          "mstp_trunks": [
            {
              "enabled": true,
              "id": "mstp-1",
              "network_number": 202
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (edge_dir / "router-mstp-status.json").write_text(
        """
        {
          "route_table": ["device 1 via network 202 address 01"],
          "trunks": {
            "mstp-1": {
              "macs": {
                "17": {"device_instance": "1569", "online": true}
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )
    devices_dir = edge_dir / "devices"
    devices_dir.mkdir()
    (devices_dir / "46-plant-chillers.json").write_text(
        """
        {
          "device_id": "46",
          "device_name": "Plant Chillers",
          "meta": {
            "device": "46",
            "mac": "C0:A8:01:66:BA:C6",
            "snet": "1",
            "sadr": "C0:A8:01:67:BA:C0",
            "bacnet_port": "47814"
          },
          "points": [{"object_type": "analog-value", "instance": 1}]
        }
        """,
        encoding="utf-8",
    )
    base = config(tmp_path, bacrp_path=bacrp_path, bacrpm_path=bacrpm_path)
    return AgentConfig(
        **{
            **base.__dict__,
            "edge_ui_data_dir": edge_dir,
            "bacnet_bbmd_address": "192.168.1.200",
            "bacnet_bbmd_port": 47809,
        }
    )


def legacy_basrtb_route_config(tmp_path: Path, *, port: int = 47814, bacrp_path: str = "bacrp", bacrpm_path: str = "bacrpm") -> AgentConfig:
    edge_dir = tmp_path / "edge-ui-data"
    (edge_dir / "cache").mkdir(parents=True)
    (edge_dir / "cache" / "last_discovery.json").write_text(
        """
        {
          "devices": [
            {
              "device": "1103",
              "mac": "C0:A8:01:C8:BA:C6",
              "snet": "202",
              "sadr": "01",
              "bacnet_port": "47814"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    base = config(tmp_path, bacrp_path=bacrp_path, bacrpm_path=bacrpm_path)
    return AgentConfig(**{**base.__dict__, "edge_ui_data_dir": edge_dir, "bacnet_default_port": port})


def incomplete_routed_route_config(tmp_path: Path, bacrp_path: str = "bacrp", bacrpm_path: str = "bacrpm") -> AgentConfig:
    edge_dir = tmp_path / "edge-ui-data"
    (edge_dir / "cache").mkdir(parents=True)
    (edge_dir / "cache" / "last_discovery.json").write_text(
        '{"devices":[{"device":"1103","snet":"202","sadr":"01"}]}',
        encoding="utf-8",
    )
    base = config(tmp_path, bacrp_path=bacrp_path, bacrpm_path=bacrpm_path)
    return AgentConfig(**{**base.__dict__, "edge_ui_data_dir": edge_dir})


def executable_stub(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


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


def test_router_authority_routes_cloud_read_through_edge_router(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        assert args[0] == [
            "bacrp",
            "1",
            "analog-value",
            "1",
            "85",
            "--mac",
            "192.168.1.200:47809",
            "--dnet",
            "202",
            "--dadr",
            "01",
        ]
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        assert kwargs["env"]["BACNET_BBMD_ADDRESS"] == "192.168.1.200"
        assert kwargs["env"]["BACNET_BBMD_PORT"] == "47809"
        return subprocess.CompletedProcess(args[0], 0, stdout="present-value: Real: 72.4\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(edge_router_authority_config(tmp_path), bacnet_read_job(valid_read_request()))

    assert status == "completed"
    assert error is None
    assert result["status"] == "ok"


def test_router_authority_replaces_stale_cloud_route_args(tmp_path: Path) -> None:
    prepared = prepare_bacnet_command(
        edge_router_authority_config(tmp_path),
        [
            "/home/swadmin/bacnet-stack/bin/bacrpm",
            "1569",
            "analog-value",
            "1",
            "85",
            "--mac",
            "192.168.1.102:47808",
            "--dnet",
            "2001",
            "--dadr",
            "11",
        ],
    )

    assert prepared == [
        "/home/swadmin/bacnet-stack/bin/bacrpm",
        "1569",
        "analog-value",
        "1",
        "85",
        "--mac",
        "192.168.1.200:47809",
        "--dnet",
        "202",
        "--dadr",
        "11",
    ]


def test_cloud_native_bip_uses_saved_endpoint_without_mstp_route(tmp_path: Path) -> None:
    prepared = prepare_bacnet_command(
        edge_router_authority_config(tmp_path),
        ["/home/swadmin/bacnet-stack/bin/bacrpm", "46", "analog-value", "1", "85"],
    )

    assert prepared == [
        "/home/swadmin/bacnet-stack/bin/bacrpm",
        "46",
        "analog-value",
        "1",
        "85",
        "--mac",
        "192.168.1.103:47808",
    ]
    assert "--dnet" not in prepared
    assert "202" not in prepared


def test_legacy_basrtb_routed_mstp_trend_read_includes_route_args(tmp_path: Path) -> None:
    agent_config = legacy_basrtb_route_config(tmp_path)

    prepared = prepare_bacnet_command(agent_config, ["bacrpm", "1103", "analog-value", "7", "85"])
    route = resolve_bacnet_route(agent_config, "1103")

    assert prepared == [
        "bacrpm",
        "1103",
        "analog-value",
        "7",
        "85",
        "--mac",
        "192.168.1.200:47814",
        "--dnet",
        "202",
        "--dadr",
        "01",
    ]
    assert route["classification"] == "routed-mstp"
    assert route["source"] == "last-discovery-cache"


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
    assert result["route_diagnostics"]["route_classification"] == "default"
    assert result["route_diagnostics"]["local_bacnet_source_port"] == 47814
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


def test_trend_bulk_rpm_uses_route_aware_preparation(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    bacrpm_path = executable_stub(tmp_path, "bacrpm")

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        assert kwargs["env"]["BACNET_IP_PORT"] == "47814"
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="analog-value, 7\n  present-value: Real: 68.5\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        legacy_basrtb_route_config(tmp_path, bacrpm_path=bacrpm_path),
        bacnet_read_bulk_job(
            {
                "device_instance": 1103,
                "points": [{"saved_point_id": "point-1103-av7", "object_type": "analog-value", "object_instance": 7}],
            }
        ),
    )

    assert status == "completed"
    assert error is None
    assert calls == [
        [
            bacrpm_path,
            "1103",
            "analog-value",
            "7",
            "85",
            "--mac",
            "192.168.1.200:47814",
            "--dnet",
            "202",
            "--dadr",
            "01",
        ]
    ]
    assert result is not None
    assert result["route_diagnostics"]["route_classification"] == "routed-mstp"
    assert result["route_diagnostics"]["dnet"] == "202"
    assert result["route_diagnostics"]["dadr"] == "01"


def test_existing_47809_site_remains_on_47809_for_bulk_reads(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        assert kwargs["env"]["BACNET_IP_PORT"] == "47809"
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="analog-value, 7\n  present-value: Real: 68.5\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    agent_config = config(tmp_path, bacrpm_path=sys.executable)
    status, result, error = execute_job(
        AgentConfig(**{**agent_config.__dict__, "bacnet_default_port": 47809}),
        bacnet_read_bulk_job(
            {
                "device_instance": 1103,
                "points": [{"saved_point_id": "point-1103-av7", "object_type": "analog-value", "object_instance": 7}],
            }
        ),
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert result["route_diagnostics"]["local_bacnet_source_port"] == 47809


def test_bacnet_bulk_read_uses_edge_priority_array_read_before_property_87(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        if args[0] == [sys.executable, "1", "analog-value", "39", "85"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="analog-value, 39\n  present-value: Real: 69\n", stderr="")
        if args[0] == [sys.executable, "1", "analog-value", "39", "priority-array"]:
            return subprocess.CompletedProcess(args[0], 0, stdout="priority-array: (NULL, NULL, NULL, NULL, NULL, NULL, NULL, Real: 69)\n", stderr="")
        raise AssertionError(f"unexpected command: {args[0]}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        config(tmp_path, bacrp_path=sys.executable, bacrpm_path=sys.executable),
        bacnet_read_bulk_job({
            "device_instance": 1,
            "points": [{"saved_point_id": "point-39", "object_type": "analog-value", "object_instance": 39, "read_priority": True}],
        }),
    )

    assert status == "completed"
    assert error is None
    assert result is not None
    assert calls == [
        [sys.executable, "1", "analog-value", "39", "85"],
        [sys.executable, "1", "analog-value", "39", "priority-array"],
    ]
    assert result["values"][0]["active_priority"] == 8


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
    assert result["route_diagnostics"]["batches"][0]["command_type"] == "bulk-rpm"
    assert result["route_diagnostics"]["batches"][1]["command_type"] == "single-fallback"
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


def test_trend_single_read_fallback_uses_same_route_aware_preparation(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    bacrpm_path = executable_stub(tmp_path, "bacrpm")

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        if args[0][0] == bacrpm_path:
            return subprocess.CompletedProcess(args[0], 0, stdout="no parseable present values here\n", stderr="")
        return subprocess.CompletedProcess(args[0], 0, stdout="present-value: Real: 68.5\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        legacy_basrtb_route_config(tmp_path, bacrpm_path=bacrpm_path),
        bacnet_read_bulk_job(
            {
                "device_instance": 1103,
                "points": [{"saved_point_id": "point-1103-av7", "object_type": "analog-value", "object_instance": 7}],
            }
        ),
    )

    expected_route_args = ["--mac", "192.168.1.200:47814", "--dnet", "202", "--dadr", "01"]
    assert status == "completed"
    assert error is None
    assert calls == [
        [bacrpm_path, "1103", "analog-value", "7", "85", *expected_route_args],
        ["bacrp", "1103", "analog-value", "7", "85", *expected_route_args],
    ]
    assert result is not None
    assert result["single_read_fallback_count"] == 1
    assert result["route_diagnostics"]["batches"][1]["command_type"] == "single-fallback"


def test_known_routed_device_with_incomplete_metadata_fails_before_plain_read(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        raise AssertionError("plain bacrp/bacrpm must not be executed for incomplete routed metadata")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, result, error = execute_job(
        incomplete_routed_route_config(tmp_path, bacrpm_path=sys.executable),
        bacnet_read_bulk_job(
            {
                "device_instance": 1103,
                "points": [{"saved_point_id": "point-1103-av7", "object_type": "analog-value", "object_instance": 7}],
            }
        ),
    )

    assert status == "failed"
    assert calls == []
    assert result is not None
    assert result["status"] == "error"
    assert result["route_diagnostics"]["route_classification"] == "metadata-error"
    assert "metadata incomplete for routed device 1103" in str(error)


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
