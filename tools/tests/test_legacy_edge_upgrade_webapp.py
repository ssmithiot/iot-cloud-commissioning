from __future__ import annotations

import base64
import os
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
    UpgradeJob,
    UpgradeRequest,
    JOBS,
    JOBS_LOCK,
    agent_config_text,
    apply_ui_files_script,
    auth_commands,
    bacnet_route_probe_command,
    bacnet_route_probe_script,
    classify_bacnet_preflight,
    config_commands,
    apply_ui_commands,
    DEFAULT_EDGE_UPDATE_REF,
    create_update_zip,
    load_env_defaults,
    parse_upgrade_request,
    parse_bacnet_route_settings,
    restart_ui_commands,
    rollback_commands,
    backup_commands,
    service_commands,
    stop_edge_ui_command,
    sudo_systemctl_timeout,
    start_sh_update_script,
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


def make_runner(job_id: str = "route-preflight-test") -> LegacyUpgradeRunner:
    request = UpgradeRequest(**{**make_request().__dict__, "dry_run": True})
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request)
    return LegacyUpgradeRunner(job_id, request)


def preflight_output(route_state: dict[str, str]) -> str:
    return "\n".join(
        [
            f"ROUTE_DECISION={route_state['route_decision']}",
            f"DETECTED_BACNET_IP_PORT={route_state['primary_bacnet_port']}",
            f"DETECTED_BACNET_PORT_MODE={route_state['mode']}",
            f"CONFIGURED_BACNET_PORTS={route_state['configured_bacnet_ports']}",
            f"PREFERRED_ROUTE={route_state['preferred_route']}",
            f"INTERNAL_EDGE_ROUTER_SERVICES={route_state['internal_edge_router_services']}",
            f"ROUTE_FILES={route_state['route_files']}",
            f"UPDATE_ALLOWED={route_state['update_allowed']}",
            f"BACNET_CONFLICT={route_state['conflict']}",
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


def test_bacnet_preflight_classifies_gw006_dual_port_preserve_existing() -> None:
    start_sh = (
        "export BACNET_IP_PORT=47814\n"
        "export BACNET_IP_PORTS=47809,47814\n"
        "export BACNET_PORT_MODE=dual-47809-first\n"
    )

    state = classify_bacnet_preflight(
        parse_bacnet_route_settings(start_sh),
        router_config_present=True,
        legacy_router_config_present=True,
        service_states={
            "iot-cx-bacnet-router.service": "inactive/disabled",
            "iot-cx-mstp-router.service": "inactive/disabled",
            "edge-bacnet-ui.service": "active/enabled",
            "iot-cx-agent.service": "active/enabled",
        },
    )

    assert state == {
        "configured": "true",
        "confidence": "confirmed",
        "route_decision": "preserve_existing",
        "mode": "external_dual_port",
        "primary_bacnet_port": "47814",
        "configured_bacnet_ports": "47809,47814",
        "preferred_route": "47809-first",
        "internal_edge_router_services": "disabled",
        "route_files": "preserve_unchanged",
        "update_allowed": "true",
        "conflict": "None",
    }

    runner = make_runner("gw006-route-preflight-test")
    try:
        runner.validate_inspection(preflight_output(state))
        with JOBS_LOCK:
            summary = JOBS["gw006-route-preflight-test"].summary
        assert summary["BACnet decision"] == "Preserve existing"
        assert summary["Configured ports"] == "47809, 47814"
        assert summary["Primary/source setting"] == "47814"
        assert summary["Preferred route order"] == "47809 first"
        assert summary["Existing router config files"] == "Preserved unchanged"
        assert summary["Conflict"] == "None"
        assert summary["Update allowed"] == "Yes"
        assert "BACNET_IP_PORT is not necessarily" in summary["BACNET_IP_PORT note"]
    finally:
        with JOBS_LOCK:
            JOBS.pop("gw006-route-preflight-test", None)


def test_bacnet_preflight_preserves_both_route_files_without_active_conflict() -> None:
    state = classify_bacnet_preflight(
        parse_bacnet_route_settings("export BACNET_IP_PORT=47814\n"),
        router_config_present=True,
        legacy_router_config_present=True,
        service_states={
            "iot-cx-bacnet-router.service": "inactive/disabled",
            "iot-cx-mstp-router.service": "inactive/disabled",
        },
    )

    assert state["route_decision"] == "preserve_existing"
    assert state["route_files"] == "preserve_unchanged"
    assert state["update_allowed"] == "true"
    assert state["conflict"] == "None"


def test_bacnet_preflight_blocks_duplicate_listener_conflict() -> None:
    state = classify_bacnet_preflight(
        parse_bacnet_route_settings("export BACNET_IP_PORT=47814\n"),
        listeners=[(47814, "edge-ui"), (47814, "router-mstp")],
    )

    assert state["route_decision"] == "ambiguous"
    assert state["update_allowed"] == "false"
    assert "duplicate listener on UDP 47814" in state["conflict"]

    runner = make_runner("duplicate-listener-route-preflight-test")
    try:
        with pytest.raises(RuntimeError, match="Ambiguous BACnet route state"):
            runner.validate_inspection(preflight_output(state))
    finally:
        with JOBS_LOCK:
            JOBS.pop("duplicate-listener-route-preflight-test", None)


def test_bacnet_preflight_blocks_invalid_primary_port() -> None:
    state = classify_bacnet_preflight(parse_bacnet_route_settings("export BACNET_IP_PORT=not-a-port\n"))

    assert state["route_decision"] == "ambiguous"
    assert state["update_allowed"] == "false"
    assert "invalid BACNET_IP_PORT=not-a-port" in state["conflict"]


def test_bacnet_route_detection_command_contains_no_interactive_heredoc() -> None:
    command = bacnet_route_probe_command()

    assert "\n" not in command
    assert "<<" not in command
    assert "python3 - <<" not in command
    assert "base64 -d" in command
    assert "timeout 30s" in command


def test_bacnet_route_detection_inspections_are_bounded() -> None:
    script = bacnet_route_probe_script()

    assert "INSPECTION_TIMEOUT_SEC = 5" in script
    assert "DETECTOR_TIMEOUT_SEC = 30" in script
    assert "['timeout', f'{INSPECTION_TIMEOUT_SEC}s', *command]" in script
    assert "['systemctl', 'is-active', service]" in script
    assert "['systemctl', 'is-enabled', service]" in script
    assert "ss -H -lunp" in script
    assert "['pgrep', '-af'," in script
    assert "sudo" not in script


def test_bacnet_route_detection_command_executes_and_emits_parseable_output() -> None:
    command = bacnet_route_probe_command()
    completed = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=10)

    assert completed.returncode == 0
    output = completed.stdout
    assert "detection_status=" in output
    assert "ROUTE_DECISION=" in output
    assert "UPDATE_ALLOWED=" in output


def test_bacnet_route_detection_timed_out_inspection_blocks_safely(tmp_path: Path) -> None:
    fake_timeout = tmp_path / "timeout"
    fake_timeout.write_text(
        "#!/usr/bin/env sh\n"
        "case \"$*\" in\n"
        "  *systemctl*|*ss\\ -*|*pgrep*) exit 124 ;;\n"
        "  *) shift; exec \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_timeout.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    completed = subprocess.run(
        [sys.executable, "-c", bacnet_route_probe_script()],
        text=True,
        capture_output=True,
        timeout=10,
        env=env,
    )

    assert completed.returncode == 0
    output = completed.stdout
    assert "detection_status=failed" in output
    assert "reason=iot-cx-bacnet-router.service active state timeout after 5s" in output
    assert "ROUTE_DECISION=ambiguous" in output
    assert "UPDATE_ALLOWED=false" in output


def test_update_start_sh_defaults_unconfigured_install_to_external_47814(tmp_path: Path) -> None:
    path = tmp_path / "start.sh"
    path.write_text("#!/usr/bin/env bash\npython3 app.py\n", encoding="utf-8")

    run_start_sh_update(path)

    updated = path.read_text(encoding="utf-8")
    assert "export BACNET_IP_PORT=47814\n" in updated
    assert "export BACNET_PORT_MODE=external\n" in updated
    assert "EDGE_BACNET_ROUTER_ENABLED=1" not in updated


def test_auth_commands_include_safe_verification() -> None:
    commands = auth_commands(make_request())
    labels = [label for label, _command, _sudo in commands]
    assert "backup start.sh" in labels
    assert "write local edge UI adapter token" in labels
    assert "write edge agent adapter token" in labels
    assert "verify safe start.sh auth" in labels
    verify_command = commands[-1][1]
    assert "sed -E" in verify_command
    assert "***SET***" in verify_command


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


def test_parse_upgrade_request_defaults_git_ref_to_release_commit() -> None:
    body = (
        "gateway_id=GW010&cloud_url=https%3A%2F%2Fiot-cloud-api-dev.onrender.com"
        "&admin_api_token=admin-secret-token&cradlepoint_host=10.0.0.10"
        "&cradlepoint_password=cp-secret&gateway_password=gw-secret"
        "&ui_password=ui-secret"
    ).encode("utf-8")

    request = parse_upgrade_request(body)

    assert request.git_ref == DEFAULT_EDGE_UPDATE_REF == "844d93d013359d837619e991da8f4da7a5000472"
    assert request.edge_release == "0.1.9"
    assert request.release_manifest_path.endswith("edge-0.1.9.json")


def test_backup_commands_create_named_code_only_checkpoint() -> None:
    joined = "\n".join(command for _label, command, _sudo in backup_commands("0.1.8"))
    assert "/home/swadmin/gw-recovery/0.1.8/pre-update-code.tar.gz" in joined
    assert "preserves=data/.env/start.sh/site-data" in joined


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
    assert captured["request"].git_ref == DEFAULT_EDGE_UPDATE_REF == "844d93d013359d837619e991da8f4da7a5000472"
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
    monkeypatch.setattr(legacy_webapp, "validate_edge_source", lambda _manifest, _source: "b4dc654")

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
