from __future__ import annotations

import base64
from dataclasses import replace
import json
import shlex
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.legacy_edge_upgrade_webapp as legacy_webapp  # noqa: E402
from tools.legacy_edge_upgrade_webapp import (  # noqa: E402
    LegacyUpgradeRunner,
    NESTED_UPLOAD_CHUNK_SIZE,
    Redactor,
    TARGETED_AGENT_ONLY_PHASES,
    UPDATE_AGENT_PHASES,
    UpgradeJob,
    UpgradeRequest,
    JOBS,
    JOBS_LOCK,
    agent_config_text,
    apply_ui_files_script,
    auth_commands,
    config_commands,
    apply_ui_commands,
    DEFAULT_EDGE_UPDATE_REF,
    DEFAULT_RELEASE_MANIFEST,
    create_update_zip,
    embedded_ui_artifact_summary,
    edge_ui_data_dir_config_command,
    edge_ui_data_dir_config_script,
    edge_ui_data_dir_validation_command,
    final_commands,
    install_agent_commands,
    load_release_definition,
    inspect_commands,
    load_env_defaults,
    parse_upgrade_request,
    repo_release_validation_command,
    repo_commands,
    restart_ui_commands,
    rollback_commands,
    backup_commands,
    service_commands,
    stop_edge_ui_command,
    sudo_systemctl_timeout,
    start_sh_update_script,
    update_start_sh_command,
    validated_embedded_ui_artifact,
    ui_source_validation_summary,
    validate_ui_source_for_deploy,
)


FINAL_AGENT_COMMIT = "d2722d395ab3b380f8858b5208863a8e49ff2cc3"
STALE_AGENT_COMMIT = "0" * 40
FINAL_UI_COMMIT = "719d4a82ed972269d44db7c0638800b26e82002d"
FINAL_UI_ARTIFACT_SHA256 = "a83fc2a6c3f17d188f8fbed13352e07a924f7c619869950a0e140890f710f683"


def test_duplicate_server_launch_does_not_start_an_orphaned_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_started = []
    monkeypatch.setattr(legacy_webapp, "gateway_update_worker", lambda: worker_started.append(True))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        with pytest.raises(OSError):
            legacy_webapp.run_server(listener.getsockname()[1])
    finally:
        listener.close()

    assert worker_started == []


def make_request() -> UpgradeRequest:
    return UpgradeRequest(
        gateway_id="GW010",
        site_id="GW010",
        cloud_url="https://iot-cloud-api-dev.onrender.com",
        admin_api_token="admin-secret-token",
        cradlepoint_host="10.0.0.10",
        cradlepoint_user="BMS_admin",
        cradlepoint_password="cp-secret",
        gateway_host="192.168.1.200",
        gateway_user="swadmin",
        gateway_password="gw-secret",
        git_ref="main",
        remote_repo="/home/swadmin/iot-cloud-commissioning",
        ui_source_folder=r"C:\Temp\edge-bacnet-ui-0.1.9",
        ui_username="admin",
        ui_password="ui-secret",
        edge_agent_write_token="gateway-local-write-secret",
    )


def test_redactor_masks_known_secrets_and_env_lines() -> None:
    redactor = Redactor(["admin-secret-token", "gateway-token", "cp-secret", "gw-secret", "ui-secret"])
    text = "\n".join(
        [
            "Authorization: Bearer admin-secret-token",
            "GATEWAY_API_TOKEN=gateway-token",
            "EDGE_UI_PASSWORD='ui-secret'",
            "passwords cp-secret gw-secret",
        ]
    )
    safe = redactor.redact(text)
    assert "admin-secret-token" not in safe
    assert "gateway-token" not in safe
    assert "cp-secret" not in safe
    assert "gw-secret" not in safe
    assert "ui-secret" not in safe
    assert "GATEWAY_API_TOKEN=***SET***" in safe
    assert "EDGE_UI_PASSWORD='***SET***'" in safe


def test_update_start_sh_command_sets_required_auth_values_without_printing_password() -> None:
    command = update_start_sh_command("admin", "ui-secret")
    assert "AUTH_ENABLED" in command
    assert "EDGE_UI_USERNAME" in command
    assert "EDGE_UI_PASSWORD" in command
    assert "ui-secret" not in Redactor(["ui-secret"]).redact(command)


def run_start_sh_update(path: Path) -> None:
    subprocess.run(
        [sys.executable, "-c", start_sh_update_script("admin", "ui-secret", str(path))],
        check=True,
    )


def write_manifest(path: Path, edge_ui_tag: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "edge_release": "0.1.9",
                "base_release": "0.1.8",
                "edge_ui_tag": edge_ui_tag,
                "artifact": "tools/releases/gw006-edge-ui-0.1.9-code.tar.gz",
                "sha256": FINAL_UI_ARTIFACT_SHA256,
                "agent_source_commit": FINAL_AGENT_COMMIT,
                "local_edge_trends_default_enabled": False,
                "preserves": ["data/", ".env", "start.sh", "gateway identity", "credentials"],
                "rollback_release": "0.1.8",
            }
        ),
        encoding="utf-8",
    )
    return path


def write_artifact_manifest(path: Path, artifact: str, sha256: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "edge_release": "0.1.9",
                "base_release": "0.1.8",
                "edge_ui_tag": FINAL_UI_COMMIT,
                "artifact": artifact,
                "sha256": sha256,
                "agent_source_commit": FINAL_AGENT_COMMIT,
                "local_edge_trends_default_enabled": False,
                "preserves": ["data/", ".env", "start.sh", "gateway identity", "credentials"],
                "rollback_release": "0.1.8",
            }
        ),
        encoding="utf-8",
    )
    return path


def make_git_source(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "app.py").write_text("print('ui')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "ui"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def run_repo_release_validation(repo: Path, expected_commit: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", repo_release_validation_command(str(repo), expected_commit)],
        capture_output=True,
        text=True,
    )


def decoded_agent_yaml(commands: list[tuple[str, str, bool]]) -> str:
    command = next(command for label, command, _sudo in commands if label == "write agent.yaml")
    encoded = command.split("printf %s ", 1)[1].split(" | base64", 1)[0].strip("'")
    return base64.b64decode(encoded).decode("utf-8")


def make_runner(job_id: str = "route-preflight-test") -> LegacyUpgradeRunner:
    request = UpgradeRequest(**{**make_request().__dict__, "dry_run": True})
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    return LegacyUpgradeRunner(job_id, request)


def existing_install_preflight_output() -> str:
    return "\n".join(
        [
            "drwxr-xr-x 10 swadmin swadmin 4096 Jul 28 /home/swadmin/edge-bacnet-ui-v2",
            "fatal: not a git repository (or any of the parent directories): .git",
            "EDGE_AGENT_VERSION=0.1.9",
            "SUDO_AVAILABLE=yes",
            "BACKUP_PATH_WRITABLE=/home/swadmin",
        ]
    )


def test_update_start_sh_preserves_existing_47809_route_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    original = (
        "#!/usr/bin/env bash\n"
        "export BACNET_IP_PORT=47809\n"
        "export BACNET_PORT_MODE=bac-rtr\n"
        "export AUTH_ENABLED=1\n"
        "export EDGE_UI_USERNAME=admin\n"
        "export EDGE_UI_PASSWORD='ui-secret'\n"
        "python3 app.py\n"
    )
    path.write_text(original, encoding="utf-8")

    run_start_sh_update(path)

    assert path.read_text(encoding="utf-8") == original


def test_update_start_sh_preserves_existing_47814_route_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    original = (
        "#!/usr/bin/env bash\n"
        "export BACNET_IP_PORT=47814\n"
        "export BACNET_PORT_MODE=basrtb\n"
        "export AUTH_ENABLED=1\n"
        "export EDGE_UI_USERNAME=admin\n"
        "export EDGE_UI_PASSWORD='ui-secret'\n"
        "python3 app.py\n"
    )
    path.write_text(original, encoding="utf-8")

    run_start_sh_update(path)

    assert path.read_text(encoding="utf-8") == original


def test_update_start_sh_preserves_edge_router_enabled_fixture_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    original = (
        "#!/usr/bin/env bash\n"
        "export BACNET_IP_PORT=47814\n"
        "export BACNET_IP_PORTS=47809,47814\n"
        "export BACNET_PORT_MODE=edge-router-fdr\n"
        "export BACNET_EDGE_PROGRAM_PORTS=47816\n"
        "export AUTH_ENABLED=1\n"
        "export EDGE_UI_USERNAME=admin\n"
        "export EDGE_UI_PASSWORD='ui-secret'\n"
        "python3 app.py\n"
    )
    path.write_text(original, encoding="utf-8")

    run_start_sh_update(path)

    assert path.read_text(encoding="utf-8") == original


def test_existing_install_skips_route_detection_entirely() -> None:
    commands = inspect_commands()
    labels = [label for label, _command, _sudo in commands]
    joined_commands = "\n".join(command for _label, command, _sudo in commands)

    assert "BACnet route detection" not in labels
    assert "ROUTE_DECISION" not in joined_commands
    assert "bacnet_route_probe" not in joined_commands
    assert "python3 - <<" not in joined_commands
    assert "ss -H" not in joined_commands
    assert "pgrep" not in joined_commands
    assert "iot-cx-bacnet-router.service" not in joined_commands
    assert "iot-cx-mstp-router.service" not in joined_commands


def test_existing_install_preflight_reports_preservation_policy() -> None:
    runner = make_runner("existing-policy-preflight-test")
    try:
        runner.validate_inspection(existing_install_preflight_output())
        with JOBS_LOCK:
            summary = JOBS["existing-policy-preflight-test"].summary
        assert summary["BACnet policy"] == "Preserve existing configuration"
        assert summary["BACnet files/settings changed"] == "None"
        assert summary["start.sh"] == "Preserved"
        assert summary["router config files"] == "Preserved"
        assert summary["router services"] == "Not changed"
        assert "BACnet decision" not in summary
        assert "Detected external router port" not in summary
    finally:
        with JOBS_LOCK:
            JOBS.pop("existing-policy-preflight-test", None)


def test_existing_route_start_sh_updates_auth_without_changing_route_lines(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    original = (
        "#!/usr/bin/env bash\n"
        "export BACNET_IP_PORT=47814\n"
        "export BACNET_IP_PORTS=47809,47814\n"
        "export BACNET_PORT_MODE=dual-47809-first\n"
        "export AUTH_ENABLED=0\n"
        "export EDGE_UI_USERNAME=old-admin\n"
        "export EDGE_UI_PASSWORD='old-secret'\n"
        "python3 app.py\n"
    )
    path.write_text(original, encoding="utf-8")

    subprocess.run(
        [sys.executable, "-c", start_sh_update_script("new-admin", "new-secret", str(path))],
        check=True,
    )

    updated = path.read_text(encoding="utf-8")
    assert "export BACNET_IP_PORT=47814\n" in updated
    assert "export BACNET_IP_PORTS=47809,47814\n" in updated
    assert "export BACNET_PORT_MODE=dual-47809-first\n" in updated
    assert "export AUTH_ENABLED=1\n" in updated
    assert "export EDGE_UI_USERNAME=new-admin\n" in updated
    assert "export EDGE_UI_PASSWORD='new-secret'\n" in updated
    assert "old-secret" not in updated


def test_existing_47814_route_missing_auth_gets_auth_inserted(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    original_route = "export BACNET_IP_PORT=47814\n"
    path.write_text("#!/usr/bin/env bash\n" + original_route + "python3 app.py\n", encoding="utf-8")

    run_start_sh_update(path)

    updated = path.read_text(encoding="utf-8")
    assert original_route in updated
    assert "export AUTH_ENABLED=1\n" in updated
    assert "export EDGE_UI_USERNAME=admin\n" in updated
    assert "export EDGE_UI_PASSWORD='ui-secret'\n" in updated


@pytest.mark.parametrize(
    "route_lines",
    [
        ["export BACNET_IP_PORT=47809", "export BACNET_PORT_MODE=bac-rtr"],
        ["export BACNET_IP_PORT=47814", "export BACNET_IP_PORTS=47809,47814", "export BACNET_PORT_MODE=dual-47809-first"],
    ],
)
def test_existing_route_lines_remain_unchanged_when_auth_is_inserted(tmp_path: Path, route_lines: list[str]) -> None:
    path = tmp_path / "start.sh"
    original = "#!/usr/bin/env bash\n" + "\n".join(route_lines) + "\npython3 app.py\n"
    path.write_text(original, encoding="utf-8")

    run_start_sh_update(path)

    updated_lines = path.read_text(encoding="utf-8").splitlines()
    for route_line in route_lines:
        assert route_line in updated_lines


def test_router_config_files_remain_untouched_by_ui_apply() -> None:
    apply_command = next(command for label, command, _sudo in apply_ui_commands(make_request()) if label == "apply code-only UI files")

    assert "router-config.json" not in apply_command
    assert "edge_bacnet_router.json" not in apply_command
    assert "/data" not in apply_command


def test_router_services_are_not_queried_or_changed_by_preflight() -> None:
    commands = inspect_commands()
    joined_commands = "\n".join(command for _label, command, _sudo in commands)

    assert "iot-cx-bacnet-router.service" not in joined_commands
    assert "iot-cx-mstp-router.service" not in joined_commands
    assert "systemctl list-unit-files" not in joined_commands


def test_fresh_install_fixture_receives_external_47814_defaults(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    path.write_text("#!/usr/bin/env bash\npython3 app.py\n", encoding="utf-8")

    run_start_sh_update(path)

    updated = path.read_text(encoding="utf-8")
    assert "export BACNET_IP_PORT=47814\n" in updated
    assert "export BACNET_PORT_MODE=external\n" in updated
    assert "EDGE_BACNET_ROUTER_ENABLED=1" not in updated


@pytest.mark.parametrize("port", ["47809", "47814"])
def test_existing_47809_and_47814_fixtures_pass_without_route_inspection(port: str) -> None:
    runner = make_runner(f"existing-{port}-preflight-test")
    try:
        runner.validate_inspection(existing_install_preflight_output() + f"\n1:export BACNET_IP_PORT={port}")
        with JOBS_LOCK:
            summary = JOBS[f"existing-{port}-preflight-test"].summary
        assert summary["BACnet policy"] == "Preserve existing configuration"
        assert summary["BACnet files/settings changed"] == "None"
    finally:
        with JOBS_LOCK:
            JOBS.pop(f"existing-{port}-preflight-test", None)


def test_ambiguous_route_text_no_longer_blocks_existing_upgrade() -> None:
    runner = make_runner("ambiguous-text-is-ignored-test")
    try:
        runner.validate_inspection(existing_install_preflight_output() + "\nROUTE_DECISION=ambiguous legacy text")
        with JOBS_LOCK:
            summary = JOBS["ambiguous-text-is-ignored-test"].summary
        assert summary["BACnet policy"] == "Preserve existing configuration"
    finally:
        with JOBS_LOCK:
            JOBS.pop("ambiguous-text-is-ignored-test", None)


def test_final_deployment_blocks_ui_source_not_expected_commit(tmp_path: Path) -> None:
    source = tmp_path / "ui"
    head = make_git_source(source)
    manifest = write_manifest(tmp_path / "manifest.json", FINAL_UI_COMMIT)

    assert head != FINAL_UI_COMMIT
    with pytest.raises(RuntimeError, match="UI source validation failed"):
        validate_ui_source_for_deploy(str(source), str(manifest))


def test_dirty_ui_source_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "ui"
    head = make_git_source(source)
    manifest = write_manifest(tmp_path / "manifest.json", head)
    (source / "app.py").write_text("dirty\n", encoding="utf-8")

    summary = ui_source_validation_summary(str(source), str(manifest))

    assert summary["UI source commit"] == head
    assert summary["UI source clean status"] == "Dirty"
    assert summary["Expected UI commit"] == head
    assert summary["Source validation"].startswith("Failed")
    assert "dirty" in summary["Source validation"]
    with pytest.raises(RuntimeError, match="dirty"):
        validate_ui_source_for_deploy(str(source), str(manifest))


def test_valid_clean_expected_ui_source_is_accepted(tmp_path: Path) -> None:
    source = tmp_path / "ui"
    head = make_git_source(source)
    manifest = write_manifest(tmp_path / "manifest.json", head)

    summary = ui_source_validation_summary(str(source), str(manifest))

    assert validate_ui_source_for_deploy(str(source), str(manifest)) == head
    assert summary["UI package source"] == str(source)
    assert summary["UI source commit"] == head
    assert summary["UI source clean status"] == "Clean"
    assert summary["Expected UI commit"] == head
    assert summary["Source validation"] == "Passed"


def test_existing_47809_agent_default_remains_47809() -> None:
    config = agent_config_text(make_request(), "47809")
    final = "\n".join(command for _label, command, _sudo in final_commands("47809"))

    assert "bacnet_default_port: 47809" in config
    assert "edge_ui_data_dir:" not in config
    assert "local_edge_trends_enabled: false" in config
    assert "default_port: 47809" in config
    assert "bacnet-47809.lock" in config
    assert "pre=47809" in final
    assert "= 47814" not in final


def test_existing_47814_agent_default_remains_47814() -> None:
    config = agent_config_text(make_request(), "47814")
    final = "\n".join(command for _label, command, _sudo in final_commands("47814"))

    assert "bacnet_default_port: 47814" in config
    assert "edge_ui_data_dir:" not in config
    assert "local_edge_trends_enabled: false" in config
    assert "default_port: 47814" in config
    assert "pre=47814" in final


def test_fresh_agent_install_defaults_to_47814() -> None:
    config = agent_config_text(make_request())
    agent_yaml = decoded_agent_yaml(config_commands(make_request(), "token"))

    assert "bacnet_default_port: 47814" in config
    assert "default_port: 47814" in config
    assert "bacnet-47814.lock" in config
    assert "bacnet_default_port: 47814" in agent_yaml
    assert "edge_ui_data_dir:" not in agent_yaml
    assert "local_edge_trends_enabled: false" in agent_yaml


def test_existing_gateway_is_not_converted_to_47814() -> None:
    agent_yaml = decoded_agent_yaml(config_commands(make_request(), "token", "47809"))

    assert "47814" not in agent_yaml
    assert "bacnet_default_port: 47809" in agent_yaml
    assert "edge_ui_data_dir:" not in agent_yaml
    assert "local_edge_trends_enabled: false" in agent_yaml
    assert "default_port: 47809" in agent_yaml


def run_edge_ui_data_dir_config(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", edge_ui_data_dir_config_script(str(path))],
        text=True,
        capture_output=True,
        check=True,
    )


def test_edge_ui_data_dir_missing_key_is_added_with_default_path(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW062\nsite_id: GW062\ncloud_url: https://cloud.example\nGATEWAY_API_TOKEN: should-stay\n",
        encoding="utf-8",
    )

    result = run_edge_ui_data_dir_config(config_path)

    updated = config_path.read_text(encoding="utf-8")
    assert "EDGE_UI_DATA_DIR_ACTION=Added" in result.stdout
    assert "edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data\n" in updated
    assert "gateway_id: GW062" in updated
    assert "GATEWAY_API_TOKEN: should-stay" in updated


def test_edge_ui_data_dir_correct_existing_value_is_preserved_without_duplication(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW062\nedge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data\nbacnet_default_port: 47814\n",
        encoding="utf-8",
    )

    result = run_edge_ui_data_dir_config(config_path)

    updated = config_path.read_text(encoding="utf-8")
    assert "EDGE_UI_DATA_DIR_ACTION=Preserved" in result.stdout
    assert updated.count("edge_ui_data_dir:") == 1
    assert "bacnet_default_port: 47814" in updated


def test_edge_ui_data_dir_custom_non_empty_value_is_preserved(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text("edge_ui_data_dir: /custom/edge/data\nheartbeat_interval_sec: 30\n", encoding="utf-8")

    result = run_edge_ui_data_dir_config(config_path)

    updated = config_path.read_text(encoding="utf-8")
    assert "EDGE_UI_DATA_DIR_ACTION=PreservedCustom" in result.stdout
    assert "edge_ui_data_dir: /custom/edge/data\n" in updated
    assert "/home/swadmin/edge-bacnet-ui-v2/data" not in updated


def test_edge_ui_data_dir_blank_value_is_replaced_without_duplicate_key(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text("edge_ui_data_dir:\nbacnet:\n  default_port: 47809\n", encoding="utf-8")

    run_edge_ui_data_dir_config(config_path)

    updated = config_path.read_text(encoding="utf-8")
    assert updated.count("edge_ui_data_dir:") == 1
    assert "edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data\n" in updated
    assert "default_port: 47809" in updated


def test_edge_ui_data_dir_duplicate_keys_are_collapsed_preserving_custom_value(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW062\nedge_ui_data_dir:\nedge_ui_data_dir: /custom/data\nedge_ui_data_dir: /other/data\n",
        encoding="utf-8",
    )

    run_edge_ui_data_dir_config(config_path)

    updated = config_path.read_text(encoding="utf-8")
    assert updated.count("edge_ui_data_dir:") == 1
    assert "edge_ui_data_dir: /custom/data\n" in updated


def test_agent_only_phase_selection_does_not_require_edge_ui_data_dir_config_fix() -> None:
    commands = install_agent_commands(make_request())
    labels = [label for label, _command, _sudo in commands]

    assert 9 in UPDATE_AGENT_PHASES
    assert 6 not in UPDATE_AGENT_PHASES
    assert 8 not in UPDATE_AGENT_PHASES
    assert "ensure Edge UI data dir config (add if missing, preserve existing)" not in labels
    assert "GATEWAY_API_TOKEN" not in "\n".join(command for _label, command, _sudo in commands)
    assert "local_edge_trends_enabled" not in "\n".join(command for _label, command, _sudo in commands)


def test_edge_ui_data_dir_config_command_is_nested_ssh_safe_and_finite() -> None:
    command = edge_ui_data_dir_config_command()

    assert command.startswith("sudo -S -p '' timeout -k 5s 30s python3 -c ")
    assert "<<" not in command
    assert "cat >" not in command
    assert "/tmp/iot-cx-ensure-edge-ui-data-dir.py" not in command
    assert "base64" in command
    assert "EDGE_UI_DATA_DIR_ACTION" not in command
    assert "\n" not in command
    assert command.count("sudo -S -p ''") == 1
    assert "sudo -n" not in command


def test_edge_ui_data_dir_config_does_not_change_identity_tokens_bacnet_or_route_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    original = """gateway_id: GW062
site_id: SITE062
cloud_url: https://cloud.example
gateway_api_token: local-token
bacnet_default_port: 47809
tunnel_enabled: true
heartbeat_interval_sec: 30
bacnet:
  default_port: 47809
  bacrp_path: /custom/bacrp
  bacrpm_path: /custom/bacrpm
route_metadata:
  profile: keep-me
"""
    config_path.write_text(original, encoding="utf-8")

    run_edge_ui_data_dir_config(config_path)

    updated = config_path.read_text(encoding="utf-8")
    for line in original.splitlines():
        assert line in updated
    assert "edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data" in updated


def test_dry_run_displays_edge_ui_data_dir_plan_without_modifying_config(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text("gateway_id: GW062\n", encoding="utf-8")
    request = replace(make_request(), dry_run=True)
    job_id = "dry-run-edge-ui-data-dir"
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    runner = LegacyUpgradeRunner(job_id, request)
    try:
        runner.run_commands([("ensure Edge UI data dir config (add if missing, preserve existing)", f"{sys.executable} -c {shlex.quote(edge_ui_data_dir_config_script(str(config_path)))}", False)])
        with JOBS_LOCK:
            log_text = JOBS[job_id].log
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)

    assert config_path.read_text(encoding="utf-8") == "gateway_id: GW062\n"
    assert "ensure Edge UI data dir config" in log_text


def test_phase_10_install_agent_skips_edge_ui_data_dir_config_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    request = replace(make_request(), dry_run=False)
    job_id = "edge-ui-data-dir-timeout"
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    runner = LegacyUpgradeRunner(job_id, request)
    labels: list[str] = []

    def fake_run_commands(command_list, *, stop_on_failure=True):
        labels.extend(label for label, _command, _sudo in command_list)
        return "ok\n"

    monkeypatch.setattr(runner, "run_commands", fake_run_commands)
    runner.run_phase(9)

    assert "ensure Edge UI data dir config (add if missing, preserve existing)" not in labels
    assert "restart agent service" not in labels
    with JOBS_LOCK:
        job = JOBS.pop(job_id)
    assert job.status == "waiting"
    assert job.phases[9].status == legacy_webapp.PhaseStatus.PASSED
    assert job.phases[10].status == legacy_webapp.PhaseStatus.NOT_STARTED


def make_fake_runtime_bin(tmp_path: Path, *, service_user: str = "root", fail_inner_sudo: bool = False) -> Path:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    sudo_inner = 'exit 1' if fail_inner_sudo else 'shift; if [ "$1" = "-u" ]; then shift 2; fi; exec "$@"'
    (fakebin / "systemctl").write_text(
        f"#!/bin/sh\nif [ \"$1\" = show ]; then printf '%s\\n' {shlex.quote(service_user)}; exit 0; fi\nexit 1\n",
        encoding="utf-8",
    )
    (fakebin / "sudo").write_text(
        f"""#!/bin/sh
if [ "$1" = "-n" ]; then
  {sudo_inner}
fi
while [ "$1" = "-S" ] || [ "$1" = "-p" ] || [ "$1" = "" ]; do
  if [ "$1" = "-p" ]; then shift 2; else shift; fi
done
exec "$@"
""",
        encoding="utf-8",
    )
    (fakebin / "systemctl").chmod(0o755)
    (fakebin / "sudo").chmod(0o755)
    return fakebin


def run_edge_ui_data_dir_validation(config_path: Path, tmp_path: Path, *, service_user: str = "root", fail_inner_sudo: bool = False) -> subprocess.CompletedProcess[str]:
    fakebin = make_fake_runtime_bin(tmp_path, service_user=service_user, fail_inner_sudo=fail_inner_sudo)
    env = {"PATH": f"{fakebin}:{Path('/usr/bin')}:{Path('/bin')}"}
    return subprocess.run(
        edge_ui_data_dir_validation_command(str(config_path)),
        shell=True,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_final_verification_no_longer_requires_edge_ui_data_dir() -> None:
    labels = [label for label, _command, _sudo in final_commands("47814")]
    commands = "\n".join(command for _label, command, _sudo in final_commands("47814"))

    assert "verify Edge UI data dir config" not in labels
    assert "edge_ui_data_dir" not in commands


def test_final_verification_reports_release_and_rule_1_markers() -> None:
    commands = "\n".join(command for _label, command, _sudo in final_commands("47814"))

    assert f"test \"$head\" = {FINAL_AGENT_COMMIT}" in commands
    assert "AGENT_RELEASE_COMMIT=$head" in commands
    assert "AGENT_RELEASE_VALIDATION=Passed" in commands
    assert "LOCAL_EDGE_TRENDS_ENABLED=false" in commands
    assert "BACKGROUND_BACNET_ACTIVITY_ADDED=No" in commands
    assert "BACNET_CONFIG_PRESERVATION=Passed" in commands
    assert "BACNET_PORTS_CHANGED=No" in commands
    assert "BACNET_ROUTES_CHANGED=No" in commands
    assert "ROUTE_SETTINGS_CHANGED=No" in commands
    assert "MSTP_READ_BASELINE_TARGET=approximately_3_seconds" in commands
    assert "BACNET_IP_READ_BASELINE_TARGET=under_1_second" in commands
    assert "RULE_1_VALIDATION=Passed" in commands
    assert "RELEASE_0_1_9_VALIDATION=Passed" in commands
    assert STALE_AGENT_COMMIT not in commands


@pytest.mark.parametrize(
    ("config_text", "expected_error"),
    [
        ("gateway_id: GW062\n", "missing or blank"),
        ("edge_ui_data_dir:\n", "missing or blank"),
        ("edge_ui_data_dir: /does/not/exist\n", "does not exist"),
    ],
)
def test_legacy_edge_ui_data_dir_validation_helper_still_reports_invalid_paths(tmp_path: Path, config_text: str, expected_error: str) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(config_text, encoding="utf-8")

    result = run_edge_ui_data_dir_validation(config_path, tmp_path)

    assert result.returncode != 0
    assert "EDGE_UI_DATA_DIR_VALIDATION=Failed" in result.stdout
    assert expected_error in result.stderr


def test_final_verification_fails_for_inaccessible_service_user_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f"edge_ui_data_dir: {data_dir}\n", encoding="utf-8")

    result = run_edge_ui_data_dir_validation(config_path, tmp_path, service_user="swadmin", fail_inner_sudo=True)

    assert result.returncode != 0
    assert f"EDGE_UI_DATA_DIR={data_dir}" in result.stdout


def test_repo_release_validation_allows_deploy_backups_runtime_folder(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = make_git_source(repo)
    (repo / "deploy-backups").mkdir()
    (repo / "deploy-backups" / "backup.tar.gz").write_text("runtime backup\n", encoding="utf-8")

    result = run_repo_release_validation(repo, head)

    assert result.returncode == 0
    assert f"RELEASE_COMMIT={head}" in result.stdout
    assert "TRACKED_REPO_STATUS=clean" in result.stdout
    assert "IGNORED_RUNTIME_PATHS=deploy-backups/" in result.stdout
    assert "REPO_RELEASE_VALIDATION=Passed" in result.stdout


def test_repo_release_validation_allows_local_backups_runtime_folder(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = make_git_source(repo)
    (repo / ".local-backups").mkdir()
    (repo / ".local-backups" / "app.py").write_text("runtime backup\n", encoding="utf-8")

    result = run_repo_release_validation(repo, head)

    assert result.returncode == 0
    assert "TRACKED_REPO_STATUS=clean" in result.stdout
    assert "IGNORED_RUNTIME_PATHS=.local-backups/" in result.stdout
    assert "REPO_RELEASE_VALIDATION=Passed" in result.stdout


def test_repo_release_validation_blocks_modified_tracked_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = make_git_source(repo)
    (repo / "app.py").write_text("modified\n", encoding="utf-8")

    result = run_repo_release_validation(repo, head)

    assert result.returncode != 0
    assert "TRACKED_REPO_STATUS=dirty" in result.stdout
    assert "TRACKED_REPO_CHANGES= M app.py" in result.stdout
    assert "REPO_RELEASE_VALIDATION=Failed" in result.stdout


@pytest.mark.parametrize("path", ["unexpected.py", "config.yaml"])
def test_repo_release_validation_blocks_unexpected_untracked_source_or_config(tmp_path: Path, path: str) -> None:
    repo = tmp_path / "repo"
    head = make_git_source(repo)
    (repo / path).write_text("unexpected\n", encoding="utf-8")

    result = run_repo_release_validation(repo, head)

    assert result.returncode != 0
    assert "TRACKED_REPO_STATUS=clean" in result.stdout
    assert f"UNEXPECTED_UNTRACKED_PATHS={path}" in result.stdout
    assert "REPO_RELEASE_VALIDATION=Failed" in result.stdout


def test_repo_release_validation_blocks_wrong_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = make_git_source(repo)
    wrong_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    result = run_repo_release_validation(repo, wrong_head)

    assert head != wrong_head
    assert result.returncode != 0
    assert f"RELEASE_COMMIT={head}" in result.stdout
    assert "REPO_RELEASE_VALIDATION=Failed" in result.stdout
    assert f"Expected release commit {wrong_head}" in result.stderr


def test_update_start_sh_defaults_unconfigured_install_to_external_47814(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    path.write_text("#!/usr/bin/env bash\npython3 app.py\n", encoding="utf-8")

    run_start_sh_update(path)

    updated = path.read_text(encoding="utf-8")
    assert "export BACNET_IP_PORT=47814\n" in updated
    assert "export BACNET_PORT_MODE=external\n" in updated
    assert "EDGE_BACNET_ROUTER_ENABLED=1" not in updated


def safe_auth_verify_command() -> str:
    commands = auth_commands(make_request())
    return next(command for label, command, _sudo in commands if label == "verify safe start.sh auth")


def test_auth_commands_include_safe_verification() -> None:
    commands = auth_commands(make_request())
    labels = [label for label, _command, _sudo in commands]
    assert "backup start.sh" in labels
    assert "write local edge UI adapter token" in labels
    assert "write edge agent adapter token" in labels
    assert "verify safe start.sh auth" in labels
    verify_command = safe_auth_verify_command()
    assert "sed -E" in verify_command
    assert "***SET***" in verify_command
    assert "BACNET_IP_PORTS" in verify_command
    assert "BACNET_PORT_MODE" in verify_command


def test_verify_safe_start_sh_auth_command_has_balanced_shell_quotes() -> None:
    verify_command = safe_auth_verify_command()

    shlex.split(verify_command)
    subprocess.run(["bash", "-n", "-c", verify_command], check=True)
    assert "<<" not in verify_command
    assert verify_command.count("'") % 2 == 0


def test_verify_safe_start_sh_auth_command_redacts_password(tmp_path: Path) -> None:
    start_sh = tmp_path / "start.sh"
    start_sh.write_text(
        "\n".join(
            [
                "export BACNET_IP_PORT=47814",
                "export BACNET_IP_PORTS=47809,47814",
                "export BACNET_PORT_MODE=dual-47809-first",
                "export EDGE_UI_PASSWORD='super-secret'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    command = safe_auth_verify_command().replace("/home/swadmin/edge-bacnet-ui-v2/start.sh", shlex.quote(str(start_sh)))

    completed = subprocess.run(["bash", "-c", command], check=True, capture_output=True, text=True)

    assert "super-secret" not in completed.stdout
    assert "EDGE_UI_PASSWORD=***SET***" in completed.stdout


def test_targeted_dry_run_phase_5_confirm_ui_auth_completes() -> None:
    request = UpgradeRequest(**{**make_request().__dict__, "dry_run": True, "selected_phases": (4,)})
    job_id = "targeted-phase-5-auth-dry-run"
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    runner = LegacyUpgradeRunner(job_id, request)
    try:
        runner.run_phase(4)

        with JOBS_LOCK:
            job = JOBS[job_id]
            assert job.phases[4].status == legacy_webapp.PhaseStatus.PASSED
            assert job.status == "waiting"
            assert "verify safe start.sh auth" in job.log
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)


def test_apply_ui_commands_preserves_start_script_and_installs_engine() -> None:
    commands = apply_ui_commands(make_request())
    apply_command = next(command for label, command, _sudo in commands if label == "apply code-only UI files")
    assert "edge_program_engine.py" in apply_command
    assert "edge_trend_store.py" in apply_command
    assert "timed_override_store.py" in apply_command
    assert "router_config.py" in apply_command
    assert "static" in apply_command
    assert "start.sh" not in apply_command


def test_apply_ui_files_script_copies_019_inventory_and_preserves_site_state(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    for name in [
        "app.py",
        "edge_program_engine.py",
        "edge_trend_store.py",
        "timed_override_store.py",
        "router_config.py",
        "README.md",
        "requirements.txt",
    ]:
        (src / name).write_text(f"new {name}", encoding="utf-8")
        (dest / name).write_text(f"old {name}", encoding="utf-8")
    for directory in ["templates", "static"]:
        (src / directory).mkdir()
        (src / directory / "item.txt").write_text(f"new {directory}", encoding="utf-8")
        (dest / directory).mkdir()
        (dest / directory / "old.txt").write_text(f"old {directory}", encoding="utf-8")
    (dest / "start.sh").write_text("site startup", encoding="utf-8")
    (dest / "data").mkdir()
    (dest / "data" / "timed-overrides.db").write_text("site data", encoding="utf-8")

    subprocess.run(
        [sys.executable, "-c", apply_ui_files_script(str(src), str(dest))],
        check=True,
    )

    assert (dest / "edge_trend_store.py").read_text(encoding="utf-8") == "new edge_trend_store.py"
    assert (dest / "timed_override_store.py").read_text(encoding="utf-8") == "new timed_override_store.py"
    assert (dest / "router_config.py").read_text(encoding="utf-8") == "new router_config.py"
    assert (dest / "templates" / "item.txt").read_text(encoding="utf-8") == "new templates"
    assert not (dest / "templates" / "old.txt").exists()
    assert (dest / "static" / "item.txt").read_text(encoding="utf-8") == "new static"
    assert (dest / "start.sh").read_text(encoding="utf-8") == "site startup"
    assert (dest / "data" / "timed-overrides.db").read_text(encoding="utf-8") == "site data"


def test_config_commands_write_root_owned_token_env_with_600_mode() -> None:
    request = make_request()
    commands = config_commands(request, "iotcc_gw_prefix_full-secret-token")
    joined = "\n".join(command for _label, command, _sudo in commands)
    assert "install -m 0600 -o root -g root" in joined
    assert "/etc/iot-cx-agent/edge-agent.env" in joined
    assert "install -d -m 0750 -o swadmin -g swadmin /var/lib/iot-cx-agent" in joined
    assert "GATEWAY_API_TOKEN=iotcc_gw_prefix_full-secret-token" not in joined

    write_env_command = next(command for label, command, _sudo in commands if label == "write edge-agent.env")
    encoded = write_env_command.split("printf %s ", 1)[1].split(" | base64", 1)[0].strip("'")
    decoded = base64.b64decode(encoded).decode("utf-8")
    assert decoded == "GATEWAY_API_TOKEN=iotcc_gw_prefix_full-secret-token\n"


def test_agent_config_uses_47814_and_expected_paths() -> None:
    config = agent_config_text(make_request())
    assert "bacnet_default_port: 47814" in config
    assert "default_port: 47814" in config
    assert "bacwi_path: /home/swadmin/bacnet-stack/bin/bacwi" in config
    assert "local_ui_url: http://127.0.0.1:5000" in config


def test_rollback_rejects_unexpected_backup_filename() -> None:
    try:
        rollback_commands("../../bad.tar.gz")
    except ValueError as exc:
        assert "Backup filename" in str(exc)
    else:
        raise AssertionError("rollback_commands accepted an unsafe filename")


def test_rollback_commands_restore_selected_backup() -> None:
    commands = rollback_commands("edge-bacnet-ui-v2.backup.20260706-141500.tar.gz")
    joined = "\n".join(command for _label, command, _sudo in commands)
    assert "systemctl stop edge-bacnet-ui.service" in joined
    assert "tar -xzf edge-bacnet-ui-v2.backup.20260706-141500.tar.gz" in joined
    assert "systemctl start --no-block edge-bacnet-ui.service" in joined


def test_load_env_defaults_reads_passwords_and_keys_from_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "IOT_ADMIN_API_TOKEN=admin-from-env",
                "CRADLEPOINT_PASSWORD=cp-from-env",
                "GATEWAY_PASSWORD=gw-from-env",
                "EDGE_UI_PASSWORD=ui-from-env",
            ]
        ),
        encoding="utf-8",
    )
    defaults = load_env_defaults()
    assert defaults["IOT_ADMIN_API_TOKEN"] == "admin-from-env"
    assert defaults["CRADLEPOINT_PASSWORD"] == "cp-from-env"
    assert defaults["GATEWAY_PASSWORD"] == "gw-from-env"
    assert defaults["EDGE_UI_PASSWORD"] == "ui-from-env"


def test_parse_upgrade_request_accepts_reuse_uploaded_zip_checkbox() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret&reuse_uploaded_zip=1&skip_edge_ui_stop=1"
    ).encode("utf-8")
    request = parse_upgrade_request(body)
    assert request.reuse_uploaded_zip is True
    assert request.skip_edge_ui_stop is True
    assert request.dry_run is True
    assert request.final_update_confirmed is False


def test_parse_upgrade_request_allows_final_update_only_when_confirmed() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret&final_update_confirmed=1"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.dry_run is False
    assert request.final_update_confirmed is True


def test_parse_upgrade_request_allows_targeted_real_run_auth_restart_final_only() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret&final_update_confirmed=1"
        "&selected_phases=4&selected_phases=5&selected_phases=11"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.dry_run is False
    assert request.final_update_confirmed is True
    assert request.selected_phases == (4, 5, 11)


def test_parse_upgrade_request_allows_standard_update_agent_real_run_phases() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret&final_update_confirmed=1"
        "&selected_phases=0&selected_phases=1&selected_phases=2&selected_phases=3"
        "&selected_phases=4&selected_phases=5&selected_phases=7"
        "&selected_phases=9&selected_phases=10&selected_phases=11"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.dry_run is False
    assert request.selected_phases == (0, 1, 2, 3, 4, 5, 7, 9, 10, 11)
    assert 6 not in request.selected_phases
    assert 8 not in request.selected_phases


def test_full_non_provisioning_phase_selection_installs_complete_release_without_cloud_provisioning() -> None:
    selected = UPDATE_AGENT_PHASES
    phase_names = [legacy_webapp.PHASES[index] for index in selected]

    assert phase_names == [
        "Inspect gateway",
        "Back up local BACnet UI",
        "Build/upload UI release artifact",
        "Apply UI update",
        "Confirm UI auth",
        "Restart local UI",
        "Clone/update cloud repo",
        "Install Python agent",
        "Install/start service",
        "Final verification",
    ]
    assert "Provision cloud gateway" not in phase_names
    assert "Write cloud config/token" not in phase_names


def test_full_non_provisioning_update_commands_do_not_create_or_sample_local_trends() -> None:
    request = replace(make_request(), selected_phases=UPDATE_AGENT_PHASES)
    command_groups = [
        backup_commands("0.1.9"),
        repo_commands(request),
        install_agent_commands(request),
        service_commands(request),
        final_commands("47814"),
    ]
    joined = "\n".join(command for commands in command_groups for _label, command, _sudo in commands)

    assert "edge-trends.db" not in joined
    assert "trend_runs" not in joined
    assert "trend_samples" not in joined
    assert "sample_local_edge_trends" not in joined
    read_surface = joined.replace("command -v /home/swadmin/bacnet-stack/bin/bacrp", "")
    read_surface = read_surface.replace("command -v /home/swadmin/bacnet-stack/bin/bacrpm", "")
    assert "/bacrp " not in read_surface
    assert "/bacrpm " not in read_surface
    assert "local_edge_trends_enabled: true" not in joined


def test_parse_upgrade_request_allows_targeted_agent_only_real_run_phases() -> None:
    body = (
        "gateway_id=GW062&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.2.0.55"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret&final_update_confirmed=1"
        "&selected_phases=0&selected_phases=7&selected_phases=9"
        "&selected_phases=10&selected_phases=11"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.dry_run is False
    assert request.selected_phases == TARGETED_AGENT_ONLY_PHASES == (0, 7, 9, 10, 11)


def test_parse_upgrade_request_rejects_other_targeted_real_run_phases() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret&final_update_confirmed=1"
        "&selected_phases=4&selected_phases=6"
    ).encode("utf-8")

    with pytest.raises(ValueError, match="Confirm UI auth, Restart local UI, Final verification"):
        parse_upgrade_request(body)


def test_form_page_shows_019_pilot_preflight_requirements() -> None:
    html = legacy_webapp.form_page().decode("utf-8")
    assert "0.1.9 Pilot Readiness" in html
    assert "Target UI version" in html
    assert "Target agent version" in html
    assert "Package / manifest checksum" in html
    assert "Dry run / Preflight" in html
    assert "Final Update/Deploy confirmed" in html
    assert "value=\"0.1.9\"" in html
    assert "Run Preflight" in html


def test_form_page_marks_ui_source_as_developer_only_and_ignored() -> None:
    html = legacy_webapp.form_page().decode("utf-8")

    assert 'name="ui_source_folder" value="C:\\Temp\\edge-bacnet-ui-0.1.9"' in html
    assert 'name="ui_source_folder" value="C:\\Temp\\edge-bacnet-ui-0.1.9" disabled' in html
    assert "developer-only; ignored for 0.1.9" in html
    assert "embedded validated artifact" in html
    assert "C:\\Dev\\edge-bacnet-ui-v2" not in html


def test_submitted_ui_source_path_persists_for_job() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret"
        "&ui_source_folder=C%3A%5CBuilds%5Cedge-ui-pilot"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.ui_source_folder == r"C:\Builds\edge-ui-pilot"


def test_form_refresh_shows_configured_default_ui_source() -> None:
    refreshed = legacy_webapp.form_page().decode("utf-8")

    assert 'value="C:\\Temp\\edge-bacnet-ui-0.1.9"' in refreshed
    assert "embedded validated artifact" in refreshed


def test_parse_upgrade_request_defaults_git_ref_to_release_commit() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.git_ref == DEFAULT_EDGE_UPDATE_REF == FINAL_AGENT_COMMIT
    assert request.edge_release == "0.1.9"
    assert request.release_manifest_path.endswith("edge-0.1.9.json")


def test_repo_commands_checkout_exact_019_agent_release_pointer() -> None:
    request = replace(make_request(), git_ref=DEFAULT_EDGE_UPDATE_REF)
    commands = {label: command for label, command, _sudo in repo_commands(request)}

    assert f"git checkout {shlex.quote(FINAL_AGENT_COMMIT)}" in commands["clone or update cloud repo"]
    assert FINAL_AGENT_COMMIT in commands["repo release validation"]
    assert STALE_AGENT_COMMIT not in "\n".join(commands.values())
    assert "844d93d013359d837619e991da8f4da7a5000472" not in commands["clone or update cloud repo"]


def test_repo_commands_ignore_stale_submitted_git_ref_and_use_release_definition() -> None:
    request = replace(make_request(), git_ref=STALE_AGENT_COMMIT)
    joined = "\n".join(command for _label, command, _sudo in repo_commands(request))

    assert f"git checkout {shlex.quote(FINAL_AGENT_COMMIT)}" in joined
    assert STALE_AGENT_COMMIT not in joined


def test_stale_default_constant_cannot_override_authoritative_release_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy_webapp, "DEFAULT_EDGE_UPDATE_REF", STALE_AGENT_COMMIT)
    request = replace(make_request(), git_ref=legacy_webapp.DEFAULT_EDGE_UPDATE_REF)

    joined = "\n".join(command for _label, command, _sudo in repo_commands(request))

    assert f"git checkout {shlex.quote(FINAL_AGENT_COMMIT)}" in joined
    assert STALE_AGENT_COMMIT not in joined


def test_authoritative_release_definition_supplies_ui_agent_and_trend_policy() -> None:
    release = load_release_definition(DEFAULT_RELEASE_MANIFEST)
    summary = embedded_ui_artifact_summary(DEFAULT_RELEASE_MANIFEST)

    assert release.edge_release == "0.1.9"
    assert release.edge_ui_tag == FINAL_UI_COMMIT
    assert release.artifact == "tools/releases/gw006-edge-ui-0.1.9-code.tar.gz"
    assert release.sha256 == FINAL_UI_ARTIFACT_SHA256
    assert release.agent_source_commit == FINAL_AGENT_COMMIT
    assert release.local_edge_trends_default_enabled is False
    assert summary["UI source commit"] == release.edge_ui_tag
    assert summary["UI artifact SHA-256"] == release.sha256
    assert summary["Agent source commit"] == release.agent_source_commit
    assert summary["Local Edge trends default enabled"] == "false"
    assert summary["Release component validation"] == "Passed"
    assert summary["Rule #1 validation"] == "Passed"


def test_backup_commands_create_named_code_only_checkpoint() -> None:
    joined = "\n".join(command for _label, command, _sudo in backup_commands("0.1.8"))
    assert "/home/swadmin/gw-recovery/0.1.8/pre-update-code.tar.gz" in joined
    assert "-T /home/swadmin/gw-recovery/0.1.8/included.txt" in joined
    assert "preserves=data/.env/start.sh/site-data" in joined


def test_full_backup_behavior_remains_unchanged() -> None:
    commands = {label: command for label, command, _sudo in backup_commands("0.1.9")}

    assert commands["create UI backup"] == 'cd /home/swadmin && tar -czf "edge-bacnet-ui-v2.backup.$(date +%Y%m%d-%H%M%S).tar.gz" edge-bacnet-ui-v2'


def test_queued_gateway_update_defaults_git_ref_to_release_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    claimed = {
        "request_id": "request-1",
        "gateway_id": "GW010",
        "site_id": "GW010",
        "cradlepoint_host": "10.0.0.10",
        "gateway_host": "192.168.1.200",
        "update_scope": "full_non_provisioning",
        "target_agent_version": "0.1.9",
        "target_ui_version": "0.1.9",
    }
    captured = {}

    def fake_cloud_json_request(_cloud_url, _token, path, *, method="GET", body=None):
        if path.endswith("/claim"):
            return claimed
        if path.endswith("/complete"):
            return {"ok": True}
        return {}

    def fake_start_job(request):
        captured["request"] = request
        job_id = "queued-default-ref-test"
        with JOBS_LOCK:
            JOBS[job_id] = UpgradeJob(request=request, status="complete")
        return job_id

    monkeypatch.delenv("IOT_EDGE_UPDATE_REF", raising=False)
    monkeypatch.setattr(legacy_webapp, "cloud_json_request", fake_cloud_json_request)
    monkeypatch.setattr(legacy_webapp, "start_job", fake_start_job)
    monkeypatch.setattr(legacy_webapp, "health_gate_enabled", lambda: False)

    outcome = legacy_webapp.run_queued_gateway_update(
        {"request_id": "request-1"},
        {
            "IOT_ADMIN_API_TOKEN": "admin-secret-token",
            "CRADLEPOINT_PASSWORD": "cp-secret",
            "GATEWAY_PASSWORD": "gw-secret",
            "EDGE_UI_PASSWORD": "ui-secret",
        },
    )

    assert outcome == "completed"
    assert captured["request"].git_ref == DEFAULT_EDGE_UPDATE_REF == FINAL_AGENT_COMMIT
    assert captured["request"].selected_phases == UPDATE_AGENT_PHASES
    assert captured["request"].release_manifest_path.endswith("edge-0.1.9.json")
    with JOBS_LOCK:
        JOBS.pop("queued-default-ref-test", None)


def embedded_artifact_names() -> set[str]:
    artifact, _summary = validated_embedded_ui_artifact(DEFAULT_RELEASE_MANIFEST)
    with tarfile.open(artifact, "r:gz") as archive:
        return set(archive.getnames())


def test_embedded_artifact_summary_validates_release_checksum() -> None:
    summary = embedded_ui_artifact_summary(DEFAULT_RELEASE_MANIFEST)

    assert summary["UI deployment source"] == "embedded-release-artifact"
    assert summary["UI package source"] == "embedded-release-artifact"
    assert summary["UI source commit"] == FINAL_UI_COMMIT
    assert summary["UI artifact SHA-256"] == FINAL_UI_ARTIFACT_SHA256
    assert summary["UI artifact validation"] == "Passed"
    assert summary["UI artifact path"].endswith("tools/releases/gw006-edge-ui-0.1.9-code.tar.gz")


def test_preflight_summary_reports_embedded_artifact_release_fields() -> None:
    runner = make_runner("embedded-artifact-preflight-test")
    try:
        runner.validate_inspection(existing_install_preflight_output())
        with JOBS_LOCK:
            summary = JOBS["embedded-artifact-preflight-test"].summary
            log = JOBS["embedded-artifact-preflight-test"].log
    finally:
        with JOBS_LOCK:
            JOBS.pop("embedded-artifact-preflight-test", None)

    assert summary["UI_DEPLOYMENT_SOURCE"] == "embedded-release-artifact"
    assert summary["UI_SOURCE_COMMIT"] == FINAL_UI_COMMIT
    assert summary["UI_ARTIFACT_SHA256"] == FINAL_UI_ARTIFACT_SHA256
    assert summary["UI_ARTIFACT_VALIDATION"] == "Passed"
    assert summary["RELEASE_VERSION"] == "0.1.9"
    assert summary["AGENT_SOURCE_COMMIT"] == FINAL_AGENT_COMMIT
    assert summary["LOCAL_EDGE_TRENDS_DEFAULT_ENABLED"] == "false"
    assert summary["BACKGROUND_BACNET_ACTIVITY_ADDED"] == "No"
    assert summary["RELEASE_COMPONENT_VALIDATION"] == "Passed"
    assert summary["RULE_1_VALIDATION"] == "Passed"
    assert "UI_DEPLOYMENT_SOURCE: embedded-release-artifact" in log
    assert "UI_ARTIFACT_VALIDATION: Passed" in log
    assert "RELEASE_VERSION=0.1.9" in log
    assert f"AGENT_SOURCE_COMMIT={FINAL_AGENT_COMMIT}" in log
    assert "LOCAL_EDGE_TRENDS_DEFAULT_ENABLED=false" in log
    assert "BACKGROUND_BACNET_ACTIVITY_ADDED=No" in log
    assert "RELEASE_COMPONENT_VALIDATION=Passed" in log
    assert "RULE_1_VALIDATION=Passed" in log


def test_missing_or_modified_embedded_artifact_fails_closed(tmp_path: Path) -> None:
    missing_manifest = write_artifact_manifest(
        tmp_path / "missing.json",
        "tools/releases/does-not-exist.tar.gz",
        FINAL_UI_ARTIFACT_SHA256,
    )
    modified = tmp_path / "modified.tar.gz"
    modified.write_bytes(b"not the release artifact")
    modified_manifest = write_artifact_manifest(
        tmp_path / "modified.json",
        str(modified),
        FINAL_UI_ARTIFACT_SHA256,
    )

    assert embedded_ui_artifact_summary(str(missing_manifest))["UI artifact validation"].startswith("Failed")
    assert embedded_ui_artifact_summary(str(modified_manifest))["UI artifact validation"].startswith("Failed")
    with pytest.raises(RuntimeError, match="UI artifact validation failed"):
        validated_embedded_ui_artifact(str(missing_manifest))
    with pytest.raises(RuntimeError, match="UI artifact validation failed"):
        validated_embedded_ui_artifact(str(modified_manifest))


def test_external_ui_source_folder_is_not_consulted_for_normal_release_artifact(tmp_path: Path) -> None:
    stale_source = tmp_path / "stale-ui"
    stale_source.mkdir()
    (stale_source / "app.py").write_text("stale checkout must not be packaged\n", encoding="utf-8")
    missing_source = tmp_path / "missing-ui"

    artifact_from_stale = create_update_zip(str(stale_source), DEFAULT_RELEASE_MANIFEST)
    artifact_from_missing = create_update_zip(str(missing_source), DEFAULT_RELEASE_MANIFEST)

    assert artifact_from_stale == artifact_from_missing
    assert artifact_from_stale.name == "gw006-edge-ui-0.1.9-code.tar.gz"
    with tarfile.open(artifact_from_stale, "r:gz") as archive:
        app_text = archive.extractfile("app.py").read().decode("utf-8")  # type: ignore[union-attr]
    assert "stale checkout must not be packaged" not in app_text


def test_embedded_artifact_includes_019_runtime_inventory_and_excludes_site_state() -> None:
    names = embedded_artifact_names()

    assert "app.py" in names
    assert "edge_program_engine.py" in names
    assert "edge_trend_store.py" in names
    assert "timed_override_store.py" in names
    assert "router_config.py" in names
    assert "templates/edge_trends_disabled.html" in names
    assert "templates/timed_overrides.html" in names
    assert "templates/edge_programs.html" in names
    assert "static/css/sidebar_nav.css" in names
    assert "deploy/iot-cx-edge-router-control.py" in names
    assert "data/" not in names
    assert ".env" not in names
    assert "start.sh" not in names
    assert not any(name.startswith("data/") for name in names)
    assert not any(name.endswith(".db") for name in names)
    assert not any(".local-backups/" in name for name in names)
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_embedded_artifact_extracts_with_expected_root_layout(tmp_path: Path) -> None:
    artifact, _summary = validated_embedded_ui_artifact(DEFAULT_RELEASE_MANIFEST)

    with tarfile.open(artifact, "r:gz") as archive:
        archive.extractall(tmp_path, filter="data")

    assert (tmp_path / "app.py").is_file()
    assert (tmp_path / "templates" / "edge_trends_disabled.html").is_file()
    assert (tmp_path / "static" / "css" / "sidebar_nav.css").is_file()
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "start.sh").exists()


def test_apply_ui_commands_extract_embedded_artifact_tarball() -> None:
    commands = {label: command for label, command, _sudo in apply_ui_commands(make_request())}

    assert commands["extract embedded UI artifact"] == (
        "tar -xzf /home/swadmin/edge-bacnet-ui-v2-update.tar.gz -C /tmp/edge-bacnet-ui-v2-update"
    )
    assert "zip" not in commands["extract embedded UI artifact"]


def test_build_upload_zip_dry_run_reports_embedded_artifact_without_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    request = replace(make_request(), dry_run=True, ui_source_folder=r"C:\Stale\edge-ui")
    job_id = "embedded-dry-run-test"
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    runner = LegacyUpgradeRunner(job_id, request)
    monkeypatch.setattr(runner, "upload_file", lambda *_args, **_kwargs: pytest.fail("dry run uploaded artifact"))
    monkeypatch.setattr(runner, "run_commands", lambda *_args, **_kwargs: pytest.fail("dry run ran remote commands"))

    try:
        runner.build_upload_zip()
        with JOBS_LOCK:
            log = JOBS[job_id].log
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)

    assert "UI_DEPLOYMENT_SOURCE=embedded-release-artifact" in log
    assert "RELEASE_VERSION=0.1.9" in log
    assert f"UI_SOURCE_COMMIT={FINAL_UI_COMMIT}" in log
    assert f"UI_ARTIFACT_SHA256={FINAL_UI_ARTIFACT_SHA256}" in log
    assert "UI_ARTIFACT_VALIDATION=Passed" in log
    assert f"AGENT_SOURCE_COMMIT={FINAL_AGENT_COMMIT}" in log
    assert "LOCAL_EDGE_TRENDS_DEFAULT_ENABLED=false" in log
    assert "BACKGROUND_BACNET_ACTIVITY_ADDED=No" in log
    assert "RELEASE_COMPONENT_VALIDATION=Passed" in log
    assert "RULE_1_VALIDATION=Passed" in log
    assert "Would upload embedded validated UI artifact" in log
    assert r"C:\Stale\edge-ui" not in log


def test_build_upload_zip_uploads_embedded_artifact_only(monkeypatch: pytest.MonkeyPatch) -> None:
    request = replace(make_request(), dry_run=False, ui_source_folder=r"C:\Missing\edge-ui")
    job_id = "embedded-upload-test"
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    runner = LegacyUpgradeRunner(job_id, request)
    uploads: list[tuple[Path, str]] = []
    command_labels: list[str] = []

    monkeypatch.setattr(runner, "upload_file", lambda local, remote: uploads.append((local, remote)))

    def fake_run_commands(command_list, *, stop_on_failure=True):
        command_labels.extend(label for label, _command, _sudo in command_list)
        return "-rw-r--r-- 1 swadmin swadmin 123 edge-bacnet-ui-v2-update.tar.gz\n"

    monkeypatch.setattr(runner, "run_commands", fake_run_commands)
    try:
        runner.build_upload_zip()
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)

    artifact, _summary = validated_embedded_ui_artifact(DEFAULT_RELEASE_MANIFEST)
    assert uploads == [(artifact, "/home/swadmin/edge-bacnet-ui-v2-update.tar.gz")]
    assert command_labels == ["verify uploaded UI artifact"]


def test_runner_uses_nested_shell_when_direct_gateway_client_is_unavailable(monkeypatch) -> None:
    request = make_request()
    job_id = "nested-fallback-test"
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    runner = LegacyUpgradeRunner(job_id, request)
    calls = []

    monkeypatch.setattr(runner, "ensure_gateway_client", lambda: None)

    def fake_nested(label, command, marker, *, sudo_password=None):
        calls.append((label, command, marker, sudo_password))
        return 0, "nested output\n"

    monkeypatch.setattr(runner, "run_nested_command", fake_nested)
    output = runner.run_commands([("hostname", "hostname", False)])

    assert output == "nested output\n"
    assert calls[0][0] == "hostname"
    assert calls[0][1] == "hostname"
    with JOBS_LOCK:
        JOBS.pop(job_id, None)


def test_service_control_commands_use_timeout_wrapper() -> None:
    command = sudo_systemctl_timeout("stop", "edge-bacnet-ui.service")
    assert command.startswith("sh -c ")
    assert "timeout -k 5s 30s sudo -S -p" in command
    assert "systemctl stop edge-bacnet-ui.service" in command
    assert "timeout -k 5s 15s systemctl --no-pager --full status edge-bacnet-ui.service" in command
    assert "failed or timed out" in command

    apply_stop = next(command for label, command, _sudo in apply_ui_commands(make_request()) if label == "stop edge UI")
    restart_start = next(command for label, command, _sudo in restart_ui_commands() if label == "start edge UI")
    agent_restart = next(command for label, command, _sudo in service_commands(make_request()) if label == "restart agent service")

    assert apply_stop.startswith("timeout -k 5s 30s sudo -S -p")
    assert "systemctl start --no-block edge-bacnet-ui.service" in restart_start
    assert agent_restart.startswith("sh -c ")


def test_stop_edge_ui_command_is_direct_and_bounded() -> None:
    command = stop_edge_ui_command()
    assert "systemctl stop edge-bacnet-ui.service" in command
    assert command.startswith("timeout -k 5s 30s sudo -S -p")
    assert "sh -c" not in command


def test_apply_ui_commands_can_skip_edge_ui_stop() -> None:
    request = UpgradeRequest(**{**make_request().__dict__, "skip_edge_ui_stop": True})
    commands = apply_ui_commands(request)
    labels = [label for label, _command, _sudo in commands]
    assert "skip edge UI stop" in labels
    assert "stop edge UI" not in labels


def test_nested_upload_uses_small_heredoc_chunks(tmp_path, monkeypatch) -> None:
    request = make_request()
    job_id = "nested-upload-test"
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    runner = LegacyUpgradeRunner(job_id, request)
    local_file = tmp_path / "upload.tar.gz"
    local_file.write_bytes(b"x" * (NESTED_UPLOAD_CHUNK_SIZE * 2))
    commands = []

    monkeypatch.setattr(runner, "ensure_gateway_client", lambda: None)

    def fake_run_commands(command_list, *, stop_on_failure=True):
        commands.extend(command_list)
        return "ok\n"

    monkeypatch.setattr(runner, "run_commands", fake_run_commands)
    runner.upload_file(local_file, "/tmp/upload.tar.gz")

    upload_commands = [command for label, command, _sudo in commands if label.startswith("upload UI artifact chunk")]
    assert len(upload_commands) > 1
    assert all("cat >> /tmp/upload.tar.gz.b64 <<'IOTGWCFG_UPLOAD_CHUNK'" in command for command in upload_commands)
    assert all("printf %s" not in command for command in upload_commands)
    assert max(len(command) for command in upload_commands) < 5000
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
