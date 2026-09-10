from __future__ import annotations

import uuid

from tools.dev_updater import updater_webapp as updater


def probe(*, ui: bool = False, config: bool = False, service: bool = False, repo: bool = False) -> str:
    return "\n".join(
        (
            f"IOT_EDGE_PROBE_UI_DIR={'yes' if ui else 'no'}",
            f"IOT_EDGE_PROBE_AGENT_CONFIG={'yes' if config else 'no'}",
            f"IOT_EDGE_PROBE_AGENT_SERVICE={'yes' if service else 'no'}",
            f"IOT_EDGE_PROBE_REPO={'yes' if repo else 'no'}",
        )
    )


def test_existing_and_supported_legacy_releases_use_existing_edge_path() -> None:
    # Version discovery is diagnostic; the filesystem/service state is authority.
    assert updater.classify_target_state(probe(ui=True, config=True, service=True, repo=True)) is updater.TargetState.EXISTING_EDGE
    assert updater.classify_target_state(probe(ui=True, config=True)) is updater.TargetState.EXISTING_EDGE


def test_020_and_older_partial_installs_are_repairable() -> None:
    assert updater.classify_target_state(probe(ui=True)) is updater.TargetState.PARTIAL_EDGE
    assert updater.classify_target_state(probe(config=True)) is updater.TargetState.PARTIAL_EDGE
    assert updater.classify_target_state(probe(repo=True)) is updater.TargetState.PARTIAL_EDGE


def test_fresh_linux_has_no_false_rollback_requirement() -> None:
    assert updater.classify_target_state(probe()) is updater.TargetState.FRESH_LINUX


def test_partial_agent_only_target_has_no_false_ui_checkpoint() -> None:
    assert updater.classify_target_state(probe(config=True)) is updater.TargetState.PARTIAL_EDGE
    assert "IOT_EDGE_PROBE_UI_DIR=yes" not in probe(config=True)


def test_full_mode_overrides_detected_existing_or_partial_state() -> None:
    assert updater.selected_install_mode("full") is updater.InstallMode.FULL
    assert updater.classify_target_state(probe(ui=True, config=True)) is updater.TargetState.EXISTING_EDGE
    assert updater.classify_target_state(probe(config=True)) is updater.TargetState.PARTIAL_EDGE
    assert updater.full_install_commands()


def test_update_mode_refuses_any_incomplete_edge_state() -> None:
    assert updater.selected_install_mode("update") is updater.InstallMode.UPDATE
    assert updater.classify_target_state(probe(config=True)) is not updater.TargetState.EXISTING_EDGE


def test_full_install_includes_firewall_router_boot_and_noninteractive_baselines() -> None:
    commands = "\n".join(command for _, command, _ in [*updater.full_install_commands(), *updater.full_router_runtime_commands()])
    for rule in ("22/tcp", "5000/tcp", "47808/udp", "47809/udp", "47814/udp"):
        assert f"ufw allow {rule}" in commands
    assert "47816/udp" not in commands and "47817/udp" not in commands and "47825" not in commands
    assert "NEEDRESTART_MODE=a" in commands
    assert "router-mstp" in commands and "visudo -cf" in commands
    assert updater.BACNET_STACK_COMMIT in commands


def test_full_agent_config_uses_selected_router_address_not_legacy_hard_code() -> None:
    request = _request()
    request = updater.replace(request, install_mode=updater.InstallMode.FULL, bacnet_lan_interface="enp1s0", bacnet_router_address="10.8.9.10")
    config = updater.agent_config_text(request)
    assert "bbmd_address: 10.8.9.10" in config
    assert "bbmd_address: 192.168.1.200" not in config
    final = "\n".join(command for _, command, _ in updater.final_commands(request))
    assert updater.BACNET_STACK_COMMIT in final and "ufw status" in final


def test_missing_nested_marker_after_shell_prompt_fails_immediately() -> None:
    class PromptShell:
        def __init__(self): self.called = False
        def recv_ready(self): return not self.called
        def recv(self, _size): self.called = True; return b"swadmin@gateway:~$ "
    try:
        updater.wait_for_shell_marker(PromptShell(), "MARKER", timeout_sec=1)
    except RuntimeError as exc:
        assert "without completion marker" in str(exc)
    else:
        raise AssertionError("shell prompt without marker must fail")


def test_bootstrap_creates_runtime_account_directories_and_only_pinned_runtime_prerequisites() -> None:
    commands = "\n".join(command for _, command, _ in updater.bootstrap_runtime_commands(_request()))
    assert "useradd --create-home" in commands
    assert "/etc/iot-cx-agent" in commands
    assert "python3-venv" in commands
    assert "NEEDRESTART_MODE=a" in commands
    assert "env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install" in commands
    assert "apt-get upgrade" not in commands


def test_all_prerequisite_install_paths_disable_needrestart_interaction() -> None:
    request = _request()
    commands = [
        *updater.bootstrap_runtime_commands(request),
        *updater.repo_commands(request),
        *updater.install_agent_commands(request),
    ]
    for label, command, _ in commands:
        if "apt-get" in command:
            assert "NEEDRESTART_MODE=a" in command, label


def test_token_write_creates_missing_directory_replaces_once_and_keeps_unrelated_env() -> None:
    command = dict((label, command) for label, command, _ in updater.auth_commands(_request()))["write edge agent adapter token"]
    assert "install -d -m 0755 -o root -g root /etc/iot-cx-agent" in command
    assert "grep -v" in command and "EDGE_AGENT_WRITE_TOKEN" in command
    assert "install -m 0600 -o root -g root" in command


def test_full_install_does_not_imply_cloud_provisioning_and_preserves_existing_token() -> None:
    request = updater.replace(_request(), install_mode=updater.InstallMode.FULL, dry_run=True)
    assert not request.provision_new_cloud_gateway
    commands = "\n".join(command for _, command, _ in updater.config_commands(request, ""))
    assert "preserve validated gateway token" in "\n".join(label for label, _, _ in updater.config_commands(request, ""))
    assert "write edge-agent.env" not in commands
    validation = "\n".join(command for _, command, _ in updater.validate_preserved_gateway_token_commands(request))
    assert "/trend-configs" in validation
    assert "/heartbeat" not in validation


def test_form_exposes_separate_existing_token_and_new_identity_controls() -> None:
    page = updater.form_page().decode()
    assert 'name="gateway_api_token"' in page
    assert 'name="provision_new_cloud_gateway"' in page
    assert "Full installation / rebuild gateway runtime" in page
    assert "Provision new Cloud gateway identity" in page
    assert "Full Install always runs the complete phase set" in page
    assert "syncFullInstallPhases" in page
    assert updater.PHASES[6] == "Cloud gateway identity"


def test_cloud_provision_phase_is_skipped_for_existing_identity_and_only_opted_in_when_selected(monkeypatch) -> None:
    job_id = uuid.uuid4().hex
    existing = updater.replace(_request(), dry_run=True, provision_new_cloud_gateway=False)
    with updater.JOBS_LOCK:
        updater.JOBS[job_id] = updater.UpgradeJob(request=existing)
    try:
        runner = updater.LegacyUpgradeRunner(job_id, existing)
        runner.run_phase(6)
        with updater.JOBS_LOCK:
            assert updater.JOBS[job_id].phases[6].status is updater.PhaseStatus.SKIPPED
            assert "existing Cloud gateway identity retained" in updater.JOBS[job_id].phases[6].detail
    finally:
        runner.close()
        with updater.JOBS_LOCK:
            updater.JOBS.pop(job_id, None)

    provisioned = updater.replace(_request(), dry_run=False, provision_new_cloud_gateway=True)
    calls = []
    monkeypatch.setattr(updater, "provision_cloud_gateway", lambda request, log, redactor: calls.append(request.gateway_id) or "new-token")
    with updater.JOBS_LOCK:
        updater.JOBS[job_id] = updater.UpgradeJob(request=provisioned)
    try:
        runner = updater.LegacyUpgradeRunner(job_id, provisioned)
        runner.run_phase(6)
        assert calls == ["GW001"]
    finally:
        runner.close()
        with updater.JOBS_LOCK:
            updater.JOBS.pop(job_id, None)


def test_preflight_reports_existing_cloud_identity_token_status_without_heartbeat() -> None:
    job_id = uuid.uuid4().hex
    request = updater.replace(_request(), install_mode=updater.InstallMode.FULL, dry_run=True)
    with updater.JOBS_LOCK:
        updater.JOBS[job_id] = updater.UpgradeJob(request=request, target_state=updater.TargetState.FRESH_LINUX)
    try:
        runner = updater.LegacyUpgradeRunner(job_id, request)
        runner.write_preflight_summary("")
        with updater.JOBS_LOCK:
            summary = updater.JOBS[job_id].summary
        assert summary["Cloud gateway identity"] == "EXISTING"
        assert summary["Provision new gateway"] == "NO"
        assert summary["Existing token"] == "PRESERVE / VALIDATE"
        assert summary["Gateway token"] .startswith("MISSING")
        assert summary["Gateway token identity match"] == "NOT CHECKED (preflight)"
    finally:
        runner.close()
        with updater.JOBS_LOCK:
            updater.JOBS.pop(job_id, None)


def test_missing_ui_agent_services_are_installed_idempotently() -> None:
    ui = "\n".join(command for _, command, _ in updater.restart_ui_commands())
    agent = "\n".join(command for _, command, _ in updater.service_commands(_request()))
    assert "systemctl enable edge-bacnet-ui.service" in ui
    assert "install -m 0644" in agent and "iot-cx-agent.service" in agent


def test_fresh_ui_start_script_is_materialized_when_no_prior_install_exists() -> None:
    script = updater.start_sh_update_script("admin", "secret")
    assert "path.read_text() if path.exists()" in script
    assert "exec .venv/bin/python app.py" in script


def test_ui_and_agent_authorities_are_exactly_verified() -> None:
    final = "\n".join(command for _, command, _ in updater.final_commands(_request()))
    assert ".iot-edge-ui-commit" in final
    assert "AGENT_RELEASE_COMMIT" in final
    assert "AGENT_RUNTIME_FILES=Passed" in final


def test_partial_rerun_keeps_runtime_data_outside_the_release_artifact() -> None:
    script = updater.apply_ui_files_script()
    assert "data" not in updater.UI_PACKAGE_DIRS
    assert "shutil.copytree(src / item, target)" in script


def _request() -> updater.UpgradeRequest:
    return updater.UpgradeRequest(
        gateway_id="GW001", site_id="GW001", cloud_url="https://cloud.example.test", admin_api_token="admin",
        cradlepoint_host="10.0.0.1", cradlepoint_user="operator", cradlepoint_password="cp",
        gateway_host="192.168.1.200", gateway_user="bootstrap", gateway_password="gateway",
        git_ref="a" * 40, remote_repo="/home/swadmin/iot-cloud-commissioning", ui_source_folder="unused",
        ui_username="admin", ui_password="ui", edge_agent_write_token="replacement-token",
        edge_ui_commit="b" * 40, edge_agent_commit="a" * 40, expected_agent_version="0.2.2",
    )
