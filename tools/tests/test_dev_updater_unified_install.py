from __future__ import annotations

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


def test_bootstrap_creates_runtime_account_directories_and_only_pinned_runtime_prerequisites() -> None:
    commands = "\n".join(command for _, command, _ in updater.bootstrap_runtime_commands(_request()))
    assert "useradd --create-home" in commands
    assert "/etc/iot-cx-agent" in commands
    assert "python3-venv" in commands
    assert "apt-get upgrade" not in commands


def test_token_write_creates_missing_directory_replaces_once_and_keeps_unrelated_env() -> None:
    command = dict((label, command) for label, command, _ in updater.auth_commands(_request()))["write edge agent adapter token"]
    assert "install -d -m 0755 -o root -g root /etc/iot-cx-agent" in command
    assert "grep -v" in command and "EDGE_AGENT_WRITE_TOKEN" in command
    assert "install -m 0600 -o root -g root" in command


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
