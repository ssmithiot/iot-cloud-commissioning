from __future__ import annotations

import base64
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.legacy_edge_upgrade_webapp as legacy_webapp  # noqa: E402
from tools.legacy_edge_upgrade_webapp import (  # noqa: E402
    LegacyUpgradeRunner,
    NESTED_UPLOAD_CHUNK_SIZE,
    Redactor,
    UpgradeJob,
    UpgradeRequest,
    JOBS,
    JOBS_LOCK,
    agent_config_text,
    auth_commands,
    config_commands,
    apply_ui_commands,
    load_env_defaults,
    parse_upgrade_request,
    restart_ui_commands,
    rollback_commands,
    service_commands,
    stop_edge_ui_command,
    sudo_systemctl_timeout,
    update_start_sh_command,
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
        ui_source_folder=r"C:\Dev\edge-bacnet-ui-v2",
        ui_username="admin",
        ui_password="ui-secret",
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
    assert "BACNET_IP_PORT" in command
    assert "AUTH_ENABLED" in command
    assert "EDGE_UI_USERNAME" in command
    assert "EDGE_UI_PASSWORD" in command
    assert "ui-secret" not in Redactor(["ui-secret"]).redact(command)


def test_auth_commands_include_safe_verification() -> None:
    commands = auth_commands(make_request())
    labels = [label for label, _command, _sudo in commands]
    assert "backup start.sh" in labels
    assert "verify safe start.sh auth" in labels
    verify_command = commands[-1][1]
    assert "sed -E" in verify_command
    assert "***SET***" in verify_command


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
