from __future__ import annotations

import base64
from dataclasses import replace
import json
import shlex
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.legacy_edge_upgrade_webapp as legacy_webapp  # noqa: E402
from tools.legacy_edge_upgrade_webapp import (  # noqa: E402
    LegacyUpgradeRunner,
    NESTED_UPLOAD_CHUNK_SIZE,
    Redactor,
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
    create_update_zip,
    edge_ui_data_dir_config_script,
    edge_ui_data_dir_validation_command,
    final_commands,
    install_agent_commands,
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
    ui_source_validation_summary,
    validate_ui_source_for_deploy,
)


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
                "sha256": "f7acfbaad0d83a63c5b6fac2db80cae296ae07fe332d2d660dc92480dbe1a475",
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
    manifest = write_manifest(tmp_path / "manifest.json", "b4dc654793af17a2a440baa5142b8eee07e08880")

    assert head != "b4dc654793af17a2a440baa5142b8eee07e08880"
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
    assert "edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data" in config
    assert "default_port: 47809" in config
    assert "bacnet-47809.lock" in config
    assert "pre=47809" in final
    assert "= 47814" not in final


def test_existing_47814_agent_default_remains_47814() -> None:
    config = agent_config_text(make_request(), "47814")
    final = "\n".join(command for _label, command, _sudo in final_commands("47814"))

    assert "bacnet_default_port: 47814" in config
    assert "edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data" in config
    assert "default_port: 47814" in config
    assert "pre=47814" in final


def test_fresh_agent_install_defaults_to_47814() -> None:
    config = agent_config_text(make_request())
    agent_yaml = decoded_agent_yaml(config_commands(make_request(), "token"))

    assert "bacnet_default_port: 47814" in config
    assert "default_port: 47814" in config
    assert "bacnet-47814.lock" in config
    assert "bacnet_default_port: 47814" in agent_yaml
    assert "edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data" in agent_yaml


def test_existing_gateway_is_not_converted_to_47814() -> None:
    agent_yaml = decoded_agent_yaml(config_commands(make_request(), "token", "47809"))

    assert "47814" not in agent_yaml
    assert "bacnet_default_port: 47809" in agent_yaml
    assert "edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data" in agent_yaml
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
    assert "EDGE_UI_DATA_DIR_ACTION=add" in result.stdout
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
    assert "EDGE_UI_DATA_DIR_ACTION=preserve" in result.stdout
    assert updated.count("edge_ui_data_dir:") == 1
    assert "bacnet_default_port: 47814" in updated


def test_edge_ui_data_dir_custom_non_empty_value_is_preserved(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text("edge_ui_data_dir: /custom/edge/data\nheartbeat_interval_sec: 30\n", encoding="utf-8")

    run_edge_ui_data_dir_config(config_path)

    updated = config_path.read_text(encoding="utf-8")
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


def test_agent_only_phase_selection_applies_config_fix_without_cloud_or_token_phases() -> None:
    commands = install_agent_commands(make_request())
    labels = [label for label, _command, _sudo in commands]
    ensure_command = next(item for item in commands if item[0] == "ensure Edge UI data dir config (add if missing, preserve existing)")

    assert 9 in UPDATE_AGENT_PHASES
    assert 6 not in UPDATE_AGENT_PHASES
    assert 8 not in UPDATE_AGENT_PHASES
    assert "ensure Edge UI data dir config (add if missing, preserve existing)" in labels
    assert ensure_command[2] is True
    assert "edge_ui_data_dir" in edge_ui_data_dir_config_script()
    assert "GATEWAY_API_TOKEN" not in "\n".join(command for _label, command, _sudo in commands)


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


def make_fake_runtime_bin(tmp_path: Path, *, service_user: str = "root", fail_inner_sudo: bool = False) -> Path:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text(
        f"#!/bin/sh\nif [ \"$1\" = show ]; then printf '%s\\n' {shlex.quote(service_user)}; exit 0; fi\nexit 1\n",
        encoding="utf-8",
    )
    (fakebin / "sudo").write_text(
        f"""#!/bin/sh
if [ "$1" = "-n" ]; then
  {"exit 1" if fail_inner_sudo else "shift; if [ \"$1\" = \"-u\" ]; then shift 2; fi; exec \"$@\""}
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


def test_final_verification_passes_for_valid_accessible_edge_ui_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f"edge_ui_data_dir: {data_dir}\n", encoding="utf-8")

    result = run_edge_ui_data_dir_validation(config_path, tmp_path)

    assert result.returncode == 0
    assert f"EDGE_UI_DATA_DIR={data_dir}" in result.stdout
    assert "EDGE_UI_DATA_DIR_VALIDATION=Passed" in result.stdout


@pytest.mark.parametrize(
    ("config_text", "expected_error"),
    [
        ("gateway_id: GW062\n", "missing or blank"),
        ("edge_ui_data_dir:\n", "missing or blank"),
        ("edge_ui_data_dir: /does/not/exist\n", "does not exist"),
    ],
)
def test_final_verification_fails_for_missing_blank_or_nonexistent_edge_ui_data_dir(tmp_path: Path, config_text: str, expected_error: str) -> None:
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


def test_form_page_defaults_to_temp_019_ui_source() -> None:
    html = legacy_webapp.form_page().decode("utf-8")

    assert 'name="ui_source_folder" value="C:\\Temp\\edge-bacnet-ui-0.1.9"' in html
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


def test_parse_upgrade_request_defaults_git_ref_to_release_commit() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.git_ref == DEFAULT_EDGE_UPDATE_REF == "63b18c961d095fdbac2bcbd645ee4a6d164fcf87"
    assert request.edge_release == "0.1.9"
    assert request.release_manifest_path.endswith("edge-0.1.9.json")


def test_repo_commands_checkout_exact_019_agent_release_pointer() -> None:
    request = replace(make_request(), git_ref=DEFAULT_EDGE_UPDATE_REF)
    commands = {label: command for label, command, _sudo in repo_commands(request)}

    assert f"git checkout {shlex.quote('63b18c961d095fdbac2bcbd645ee4a6d164fcf87')}" in commands["clone or update cloud repo"]
    assert "844d93d013359d837619e991da8f4da7a5000472" not in commands["clone or update cloud repo"]


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
    assert captured["request"].git_ref == DEFAULT_EDGE_UPDATE_REF == "63b18c961d095fdbac2bcbd645ee4a6d164fcf87"
    with JOBS_LOCK:
        JOBS.pop("queued-default-ref-test", None)


def test_create_update_zip_includes_019_runtime_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in [
        "app.py",
        "edge_program_engine.py",
        "edge_trend_store.py",
        "timed_override_store.py",
        "router_config.py",
        "README.md",
        "requirements.txt",
    ]:
        (source / name).write_text(f"{name}\n", encoding="utf-8")
    for directory in ["templates", "static"]:
        (source / directory).mkdir()
        (source / directory / "item.txt").write_text(directory, encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "timed-overrides.db").write_text("site data", encoding="utf-8")
    (source / ".env").write_text("secret=true", encoding="utf-8")
    (source / "start.sh").write_text("site startup", encoding="utf-8")
    (source / ".local-backups").mkdir()
    (source / ".local-backups" / "app.py").write_text("backup", encoding="utf-8")
    monkeypatch.setattr(legacy_webapp, "validate_ui_source_for_deploy", lambda _source, _manifest: "b4dc654")

    zip_path = create_update_zip(str(source), "manifest.json")

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "app.py" in names
    assert "edge_trend_store.py" in names
    assert "timed_override_store.py" in names
    assert "router_config.py" in names
    assert "templates/item.txt" in names
    assert "static/item.txt" in names
    assert "data/timed-overrides.db" not in names
    assert ".env" not in names
    assert "start.sh" not in names
    assert ".local-backups/app.py" not in names


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
    local_file = tmp_path / "upload.zip"
    local_file.write_bytes(b"x" * (NESTED_UPLOAD_CHUNK_SIZE * 2))
    commands = []

    monkeypatch.setattr(runner, "ensure_gateway_client", lambda: None)

    def fake_run_commands(command_list, *, stop_on_failure=True):
        commands.extend(command_list)
        return "ok\n"

    monkeypatch.setattr(runner, "run_commands", fake_run_commands)
    runner.upload_file(local_file, "/tmp/upload.zip")

    upload_commands = [command for label, command, _sudo in commands if label.startswith("upload UI zip chunk")]
    assert len(upload_commands) > 1
    assert all("cat >> /tmp/upload.zip.b64 <<'IOTGWCFG_UPLOAD_CHUNK'" in command for command in upload_commands)
    assert all("printf %s" not in command for command in upload_commands)
    assert max(len(command) for command in upload_commands) < 5000
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
