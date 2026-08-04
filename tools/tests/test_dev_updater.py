"""The Development Updater must be provably separate from Jim's Legacy Updater.

Every test here answers one of the eighteen questions asked of this build. They
are grouped by what they protect: the Legacy Updater's continued operation, this
application's own identity, the immutability of what it deploys, the manual-only
safety model, and the MSI's side-by-side behaviour.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tools.dev_updater import identity
from tools.dev_updater.plan import (
    AGENT_SERVICE,
    Component,
    PlanError,
    build_plan,
    preflight_steps,
)
from tools.dev_updater.release_source import (
    ReleaseSourceError,
    approved_manifests,
    assert_immutable_ref,
    resolve_target,
)
from tools.dev_updater.runtime import AuditLog, PortUnavailable, port_status, redact, require_port


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = REPO_ROOT / "tools" / "releases" / "manifests"
MANIFEST_0_2_0 = MANIFEST_DIR / "edge-0.2.0.json"
LEGACY_MODULE = REPO_ROOT / "tools" / "legacy_edge_upgrade_webapp.py"
LEGACY_LAUNCHER = REPO_ROOT / "tools" / "start-legacy-edge-upgrade-webapp.cmd"
MSI_BUILDER = REPO_ROOT / "deploy" / "dev-updater" / "build-msi.sh"


@pytest.fixture()
def target():
    return resolve_target(MANIFEST_0_2_0, repo_root=REPO_ROOT)


def _armed(component: Component, target):
    return build_plan(component, target, confirmed=True, agent_confirmed=True)


# --- 1, 2, 18: the Legacy Updater is untouched -------------------------------


def test_1_legacy_updater_files_are_unchanged_by_this_work():
    """Nothing in this change may edit the program Jim is using.

    Compared against the committed blob rather than a pinned hash, so the test
    keeps working when Jim's updater is intentionally changed by someone else,
    and fails the moment *this* working tree modifies it.
    """
    for path in (LEGACY_MODULE, LEGACY_LAUNCHER):
        assert path.is_file(), path
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "diff", "HEAD", "--", str(path.relative_to(REPO_ROOT))],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            pytest.skip("git is unavailable in this environment")
        assert result.stdout == "", f"{path.name} has uncommitted modifications:\n{result.stdout}"


def test_2_legacy_updater_port_is_unchanged():
    source = LEGACY_MODULE.read_text(encoding="utf-8")

    assert "DEFAULT_PORT = 8766" in source
    assert identity.LEGACY_PORT == 8766
    # And this application records it only to stay away from it.
    assert str(identity.DEFAULT_PORT) not in re.findall(r"DEFAULT_PORT = (\d+)", source)


def test_18_the_legacy_updater_still_imports_and_keeps_its_own_identity():
    """The existing tests keep passing; this one proves the module still loads."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import tools.legacy_edge_upgrade_webapp as legacy;"
         "print(legacy.DEFAULT_PORT)"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "8766"


# --- 3, 4, 17: ports ----------------------------------------------------------


def test_3_development_updater_uses_a_different_port():
    assert identity.DEFAULT_PORT != identity.LEGACY_PORT
    assert identity.DEFAULT_PORT == 8791


def test_4_both_applications_can_hold_their_ports_at_the_same_time():
    """Two sockets, two ports, no contention: they can run together."""
    legacy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    development = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        legacy.bind((identity.DEFAULT_HOST, identity.LEGACY_PORT))
        legacy.listen(1)
        # The Development Updater starts while the Legacy Updater holds 8766.
        development.bind((identity.DEFAULT_HOST, identity.DEFAULT_PORT))
        development.listen(1)
        assert legacy.getsockname()[1] == identity.LEGACY_PORT
        assert development.getsockname()[1] == identity.DEFAULT_PORT
    except OSError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"a required port is busy on this host: {error}")
    finally:
        legacy.close()
        development.close()


def test_17_a_port_collision_produces_a_clear_startup_error():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind((identity.DEFAULT_HOST, 0))
        holder.listen(1)
        busy = holder.getsockname()[1]

        assert port_status(busy).available is False
        with pytest.raises(PortUnavailable) as error:
            require_port(busy)
    finally:
        holder.close()

    message = str(error.value)
    assert "cannot start" in message
    assert str(busy) in message
    assert "--port" in message and identity.PORT_ENV_VAR in message
    # It must also say it will not solve the problem by stealing 8766.
    assert str(identity.LEGACY_PORT) in message


def test_17b_the_legacy_port_is_refused_outright():
    from tools.dev_updater.__main__ import main

    assert main(["--port", str(identity.LEGACY_PORT), "--no-browser"]) == 2


# --- 5: identity --------------------------------------------------------------


def test_5_development_updater_has_a_wholly_distinct_identity():
    assert identity.PRODUCT_NAME == "IOT Edge Development Updater"
    assert identity.APP_NAME == "IOTEdgeDevUpdater"
    assert identity.APP_VERSION == "0.1.0"

    # Nothing it owns may be named after, or live inside, the Legacy Updater.
    for value in (identity.PRODUCT_NAME, identity.APP_NAME, identity.WINDOWS_INSTALL_DIR,
                  identity.WINDOWS_DATA_DIR, identity.WINDOWS_LOG_DIR):
        assert "legacy" not in value.lower()
        assert identity.LEGACY_VENV_DIR_NAME not in value

    assert "EdgeDevUpdater" in str(identity.WINDOWS_DATA_DIR)
    assert identity.pid_path().name == "IOTEdgeDevUpdater.pid"
    assert re.fullmatch(r"[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}", identity.UPGRADE_CODE)
    # The three component GUIDs must be distinct from each other.
    guids = {identity.UPGRADE_CODE, identity.DATA_DIR_COMPONENT_GUID, identity.SHORTCUT_COMPONENT_GUID}
    assert len(guids) == 3


def test_5b_the_banner_is_unmistakable():
    assert identity.BANNER == "DEVELOPMENT UPDATER — MANUAL TEST GATEWAYS ONLY"

    from tools.dev_updater import webapp

    rendered = webapp.page(webapp.Session()).decode("utf-8")
    assert identity.BANNER in rendered
    for required in ("Selected gateway", "Component scope", "Target release",
                     "Artifact SHA-256", "Checkpoint", "Deployment", "cloud_url"):
        assert required in rendered, required


# --- 6, 7: the MSI ------------------------------------------------------------


def test_6_msi_installs_side_by_side():
    builder = MSI_BUILDER.read_text(encoding="utf-8")

    assert identity.UPGRADE_CODE in builder
    # perMachine into its own Program Files folder, with no reference to the
    # Legacy Updater's checkout.
    assert "ProgramFiles64Folder" in builder
    assert "IOT Edge Development Updater" in builder
    assert identity.LEGACY_VENV_DIR_NAME not in builder
    assert "legacy_edge_upgrade_webapp" not in builder


def test_7_msi_uninstall_removes_only_this_application():
    builder = MSI_BUILDER.read_text(encoding="utf-8")

    # Structural, not a comment: the data and logs directories must carry no
    # RemoveFolder, so logs and checkpoint records survive an uninstall.
    removals = re.findall(r'<RemoveFolder[^>]*Id="([^"]+)"', builder)
    assert removals == ["AppMenuFolder"], removals
    assert "logs are not removed on uninstall" in builder
    assert "gateway-update-venv" not in builder


MSI = REPO_ROOT / "dist" / "IOTEdgeDevUpdater-0.1.0-x64.msi"
msi_built = pytest.mark.skipif(not MSI.is_file(), reason="MSI not built; run deploy/dev-updater/build-msi.sh")


def _msi_table(name: str) -> str:
    result = subprocess.run(["msiinfo", "export", str(MSI), name],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("msiinfo (msitools) is not on PATH")
    return result.stdout


@msi_built
def test_6b_the_built_msi_carries_this_products_own_identity():
    properties = _msi_table("Property")

    assert "IOT Edge Development Updater" in properties
    assert identity.UPGRADE_CODE in properties.upper()
    # A ProductCode distinct from the UpgradeCode, minted per build.
    product_code = re.search(r"ProductCode\t\{([0-9A-F-]+)\}", properties)
    assert product_code, properties
    assert product_code.group(1) != identity.UPGRADE_CODE

    directories = _msi_table("Directory")
    # Its own Program Files folder and its own ProgramData tree.
    assert "INSTALLDIR\tProgramFiles64Folder\tIOT Edge Development Updater" in directories
    assert "DEVUPDATERDATA\tIOTDataFolder\tEdgeDevUpdater" in directories
    assert "DEVUPDATERLOGS\tDEVUPDATERDATA\tlogs" in directories


@msi_built
def test_7b_the_built_msi_removes_nothing_but_its_own_start_menu_folder():
    removals = [line.split("\t")[0] for line in _msi_table("RemoveFile").splitlines()[3:] if line.strip()]

    # Exactly one removal, and it is this product's own Start Menu folder.
    assert removals == ["AppMenuFolder"], removals

    shortcuts = _msi_table("Shortcut")
    assert "StartMenuLink" in shortcuts and "DesktopLink" in shortcuts


@msi_built
def test_7c_the_built_msi_ships_no_part_of_the_legacy_updater():
    result = subprocess.run(["msiinfo", "export", str(MSI), "File"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("msiinfo (msitools) is not on PATH")

    names = result.stdout
    assert "legacy_edge_upgrade_webapp" not in names
    assert "start-legacy" not in names
    assert identity.LEGACY_VENV_DIR_NAME not in names
    # What it does ship: its own package, the shared helpers, the approved release.
    for expected in ("identity.py", "plan.py", "release_source.py", "gateway_recovery.py",
                     "release_manifest.py", "edge-0.2.0.json"):
        assert expected in names, expected


# --- 8, 9, 10: the release source is immutable --------------------------------


def test_8_it_reads_the_0_2_0_development_manifest(target):
    assert target.edge_release == "0.2.0"
    assert target.edge_ui_commit == "cd4c0a5468c6d6d8937de62b6ddcc1119bd17d6e"
    assert target.agent_commit == "40133f2a81390db92a01b33a9c02c48a07363a7e"
    assert target.artifact_name == "gw006-edge-ui-0.2.0-code.tar.gz"
    assert target.artifact_sha256 == "78fac819890a53ae30050fbfdc40fafe470ed2d04460e4d1485416a4c57083f9"
    assert target.rollback_release == "0.1.9"

    # Only 0.2.0 is offered; 0.1.9 and 0.1.7 belong to the Legacy Updater.
    offered = {path.name for path in approved_manifests(MANIFEST_DIR)}
    assert offered == {"edge-0.2.0.json"}


def test_9_it_refuses_an_artifact_whose_hash_does_not_match(tmp_path):
    manifest = json.loads(MANIFEST_0_2_0.read_text())
    artifact = tmp_path / "tools" / "releases" / "tampered.tar.gz"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"not the approved artifact")
    manifest["artifact"] = "tools/releases/tampered.tar.gz"
    path = tmp_path / "edge-0.2.0.json"
    path.write_text(json.dumps(manifest))

    with pytest.raises(ReleaseSourceError) as error:
        resolve_target(path, repo_root=tmp_path)

    assert "SHA-256 does not match" in str(error.value)
    assert "refused" in str(error.value).lower()


def test_10_it_refuses_an_unapproved_or_mutable_target(tmp_path):
    # A moving reference is refused whatever shape it arrives in.
    for moving in ("main", "origin/main", "HEAD", "refs/heads/release/edge-agent-0.2.0",
                   "latest", "cd4c0a5", "v0.2.0"):
        with pytest.raises(ReleaseSourceError):
            assert_immutable_ref(moving, field="Edge UI commit")

    assert assert_immutable_ref("CD4C0A5468C6D6D8937DE62B6DDCC1119BD17D6E", field="x") == \
        "cd4c0a5468c6d6d8937de62b6ddcc1119bd17d6e"

    # An unapproved release is refused even with a perfectly valid manifest.
    manifest = json.loads(MANIFEST_0_2_0.read_text())
    manifest["edge_release"] = "0.1.9"
    path = tmp_path / "edge-0.1.9.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ReleaseSourceError) as error:
        resolve_target(path, repo_root=REPO_ROOT)
    assert "not an approved development release" in str(error.value)
    assert "Legacy Updater" in str(error.value)


# --- 11 to 15: the manual-only safety model -----------------------------------


def test_11_no_deployment_is_built_without_explicit_confirmation(target):
    with pytest.raises(PlanError) as error:
        build_plan(Component.UI_ONLY, target, confirmed=False)
    assert "not been confirmed" in str(error.value)

    # No component selected is also refused - there is no default.
    with pytest.raises(PlanError):
        build_plan(Component.NONE, target, confirmed=True)

    # An Agent update needs a second, separate confirmation.
    with pytest.raises(PlanError) as error:
        build_plan(Component.AGENT_ONLY, target, confirmed=True, agent_confirmed=False)
    assert "second explicit confirmation" in str(error.value)


def test_12_ui_only_leaves_the_agent_untouched(target):
    plan = build_plan(Component.UI_ONLY, target, confirmed=True)

    for step in plan.steps:
        if step.read_only:
            continue
        assert AGENT_SERVICE not in step.command, step.description
        assert "/etc/iot-cx-agent" not in step.command, step.description
        assert "iot-cloud-commissioning" not in step.command, step.description
    assert "edge-agent" not in plan.stages


def test_13_agent_only_leaves_edge_ui_data_and_files_untouched(target):
    plan = build_plan(Component.AGENT_ONLY, target, confirmed=True, agent_confirmed=True)

    assert "edge-ui" not in plan.stages
    for step in plan.steps:
        if step.read_only:
            continue
        assert "edge-bacnet-ui-v2-update.tar.gz" not in step.command, step.description
        assert "systemctl stop edge-bacnet-ui" not in step.command, step.description
        # Site data is never written by any step, in any mode.
        assert not re.search(r"rm\s+-rf\s+\S*edge-bacnet-ui-v2/data", step.command)


@pytest.mark.parametrize("component", [Component.UI_ONLY, Component.AGENT_ONLY, Component.UI_AND_AGENT])
def test_14_cloud_url_is_never_changed_automatically(target, component):
    plan = _armed(component, target)

    for step in plan.steps:
        assert not re.search(r"cloud_url\s*=", step.command), step.description
        assert not re.search(r"sed .*cloud_url", step.command), step.description
        if "cloud_url" in step.command:
            # It may only ever be read.
            assert step.read_only or step.command.lstrip().startswith("grep"), step.description

    # And it is reported both before and after, so a change would be visible.
    descriptions = [step.description for step in plan.steps]
    assert "cloud_url (read only)" in descriptions
    assert "cloud_url after update" in descriptions


def test_15_the_checkpoint_is_taken_and_verified_before_any_change(target):
    plan = _armed(Component.UI_AND_AGENT, target)
    stages = plan.stages

    assert stages.index("checkpoint") < stages.index("edge-ui")
    assert stages.index("checkpoint") < stages.index("edge-agent")

    checkpoint = [step for step in plan.steps if step.stage == "checkpoint"]
    verifications = " ".join(step.command for step in checkpoint)
    assert "sha256sum -c" in verifications, "the checkpoint must be checksum-verified"
    assert "tar -tzf" in verifications, "the checkpoint must be proven readable"

    # And rollback is code-only: nothing in it deletes gateway data.
    assert plan.rollback_steps
    for step in plan.rollback_steps:
        assert "edge-bacnet-ui-v2/data" not in step.command
    assert plan.rollback_location.endswith("pre-update-code.tar.gz")


def test_15b_preflight_is_read_only(target):
    for step in preflight_steps():
        assert step.read_only, step.description
        assert not re.search(r"\b(systemctl (start|stop|restart)|rm |cp |mv |tar -xz|sed -i|tee )", step.command)


# --- 16: logs carry no secrets ------------------------------------------------


def test_16_logs_contain_no_secrets(tmp_path):
    log = AuditLog(tmp_path, operator="steve")
    log.write(
        "deploy_requested",
        gateway="192.168.1.200",
        password="hunter2",
        ssh_password="hunter2",
        github_token="ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        command="sudo -S -p '' systemctl restart iot-cx-agent.service",
        note="password: hunter2 and token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        url="https://user:hunter2@example.invalid/repo.git",
        key="-----BEGIN OPENSSH PRIVATE KEY-----\nc2VjcmV0\n-----END OPENSSH PRIVATE KEY-----",
    )

    written = log.path.read_text(encoding="utf-8")
    assert "hunter2" not in written
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in written
    assert "c2VjcmV0" not in written
    assert "REDACTED" in written

    # The useful parts survive.
    record = json.loads(written.splitlines()[0])
    assert record["gateway"] == "192.168.1.200"
    assert record["operator"] == "steve"
    assert record["application"] == identity.APP_NAME
    assert record["event"] == "deploy_requested"
    assert "timestamp" in record


def test_16b_redaction_covers_the_shapes_a_secret_arrives_in():
    assert "hunter2" not in redact("PASSWORD=hunter2")
    assert "hunter2" not in redact("passphrase: hunter2")
    assert "hunter2" not in redact("https://steve:hunter2@github.com/x.git")
    assert "ghp_" not in redact("ghp_0123456789ABCDEFGHIJKLMNOPQRSTUVWX")
    # Ordinary text is left alone.
    assert redact("deployed edge-ui to 192.168.1.200") == "deployed edge-ui to 192.168.1.200"


def test_16c_the_log_directory_belongs_to_this_application_alone(monkeypatch):
    # On Windows this is %ProgramData%\IOT\EdgeDevUpdater\logs.
    monkeypatch.delenv(identity.DATA_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    assert identity.log_dir().as_posix().endswith("IOT/EdgeDevUpdater/logs")
    assert "EdgeDevUpdater" in identity.WINDOWS_LOG_DIR

    # Wherever it lands, it is inside this application's own data directory and
    # nowhere near the Legacy Updater, which keeps its state in its checkout.
    assert identity.log_dir().is_relative_to(identity.data_dir())
    assert not identity.data_dir().is_relative_to(Path(REPO_ROOT))


def test_17c_a_restart_is_not_blocked_by_the_previous_run(monkeypatch):
    """The probe must ask the same question the server will ask.

    Closing the application leaves its accepted connections in TIME_WAIT. The
    server sets SO_REUSEADDR and would bind straight through that; a probe
    without it would refuse to restart for the whole TIME_WAIT window and
    report a conflict that does not exist.
    """
    import socket as socket_module

    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    listener.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    listener.bind((identity.DEFAULT_HOST, 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    # A live listener is a real conflict and must be detected.
    assert port_status(port).available is False

    # Connect, then close both ends, leaving the port in TIME_WAIT.
    client = socket_module.create_connection((identity.DEFAULT_HOST, port))
    accepted, _ = listener.accept()
    client.close()
    accepted.close()
    listener.close()

    # The port is now restartable, and the probe must say so.
    assert port_status(port).available is True, "TIME_WAIT must not block a restart"
