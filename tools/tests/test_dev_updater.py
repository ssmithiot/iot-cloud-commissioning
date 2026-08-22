"""The Development Updater is a copy of the working updater, kept separate.

Two claims have to hold at once, and they pull against each other:

  * it must BE the proven updater -- same Cradlepoint connection, same nested
    SSH to the gateway behind it, same phases, checkpoints and rollback; and
  * it must never touch, read, bind or resemble Jim's installation.

So the tests fall into two halves. One half compares the copy against the
original and fails if the deployment machinery has drifted. The other compares
the two products' identities and fails if anything is shared.

Numbers refer to the eighteen checks required of this build.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tools.dev_updater import identity, runtime
from tools.dev_updater import updater_webapp as dev
from tools.dev_updater.commit_resolution import CommitResolutionError


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_MODULE = REPO_ROOT / "tools" / "legacy_edge_upgrade_webapp.py"
LEGACY_LAUNCHER = REPO_ROOT / "tools" / "start-legacy-edge-upgrade-webapp.cmd"
LEGACY_MANIFEST = REPO_ROOT / "tools" / "releases" / "manifests" / "edge-0.2.0.json"
LEGACY_README = REPO_ROOT / "tools" / "README-legacy-edge-upgrade-webapp.md"

DEV_MODULE = REPO_ROOT / "tools" / "dev_updater" / "updater_webapp.py"
DEV_MANIFEST = REPO_ROOT / "tools" / "dev_updater" / "releases" / "manifests" / "edge-0.2.0-dev.json"
BUILD_SCRIPT = REPO_ROOT / "deploy" / "dev-updater" / "build-msi.sh"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "dev-updater" / ".env.example"
LAUNCHER = REPO_ROOT / "deploy" / "dev-updater" / "IOTEdgeDevUpdater.cmd"
MSI = REPO_ROOT / "dist" / f"IOTEdgeDevUpdater-{identity.MSI_PRODUCT_VERSION}-x64.msi"

# Jim's files as they stood before any Development Updater work existed
# (commit 3979be2). Pinned rather than diffed against HEAD: a hash cannot be
# satisfied by also committing the change.
LEGACY_BASELINE_SHA256 = {
    LEGACY_MODULE: "317b9850093bafe9",
    LEGACY_LAUNCHER: "4117769e55c2b7a0",
    LEGACY_MANIFEST: "fa3b58dd646db320",
    LEGACY_README: "0e00387d7414fcd1",
}


def _sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name} not found in {path}")


def _method_source(path: Path, name: str) -> str:
    """One method body, stopping at the next definition at any nesting level."""
    found = re.search(
        rf"\n    def {name}\(.*?(?=\n    def |\nclass |\ndef |\Z)",
        path.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert found, f"{name} not found in {path}"
    return found.group(0)


# --- 1, 2: Jim's updater is untouched ---------------------------------------


@pytest.mark.parametrize("path", sorted(LEGACY_BASELINE_SHA256, key=str))
def test_1_legacy_updater_files_are_byte_for_byte_unchanged(path: Path):
    assert _sha16(path) == LEGACY_BASELINE_SHA256[path], f"{path} changed; Jim's updater must not be modified"


def test_1b_the_development_updater_never_imports_the_legacy_module():
    source = DEV_MODULE.read_text(encoding="utf-8")
    assert "import legacy_edge_upgrade_webapp" not in source
    assert "tools.legacy" not in source


def test_2_the_only_msi_this_repository_builds_is_this_product():
    """Jim's updater is a .cmd launcher run from a checkout, not an installed
    product, so there is no legacy MSI for this build to have disturbed."""
    built = sorted(p.name for p in (REPO_ROOT / "dist").glob("*.msi")) if (REPO_ROOT / "dist").exists() else []
    assert all(name.startswith("IOTEdgeDevUpdater-") for name in built), built


def test_2b_the_build_stages_no_part_of_the_legacy_updater():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "cp \"$REPO\"/tools/legacy_edge_upgrade_webapp.py" not in script
    assert "cp \"$REPO\"/tools/releases/manifests/edge-0.2.0.json" not in script


# --- 3, 4: side by side ------------------------------------------------------


def test_4_the_two_applications_use_different_ports():
    assert identity.LEGACY_PORT == 8766
    assert dev.DEFAULT_PORT == identity.DEFAULT_PORT == 8791
    assert dev.DEFAULT_PORT != identity.LEGACY_PORT


def test_3_both_ports_can_be_held_at_the_same_time():
    """The real question behind "can both run at once": two listeners, no clash."""
    legacy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    development = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        legacy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        development.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        legacy.bind(("127.0.0.1", 0))
        legacy.listen(1)
        development.bind(("127.0.0.1", 0))
        development.listen(1)
        assert legacy.getsockname()[1] != development.getsockname()[1]
    finally:
        legacy.close()
        development.close()


def test_3b_an_occupied_port_is_refused_with_an_actionable_message():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(runtime.PortUnavailable) as caught:
            runtime.require_port(port)
        message = str(caught.value)
        assert "already in use" in message
        assert "--port" in message
        assert str(identity.LEGACY_PORT) in message
    finally:
        holder.close()


def test_3c_binding_the_legacy_port_names_the_program_it_belongs_to():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        holder.bind(("127.0.0.1", identity.LEGACY_PORT))
        holder.listen(1)
    except OSError:
        pytest.skip("legacy port already held on this machine")
    try:
        with pytest.raises(runtime.PortUnavailable) as caught:
            runtime.require_port(identity.LEGACY_PORT)
        assert "Legacy Edge Upgrade Webapp" in str(caught.value)
    finally:
        holder.close()


# --- 5, 6: the copied .env ---------------------------------------------------


def test_5_a_copied_env_is_loaded_from_this_products_own_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    for name in ("CRADLEPOINT_PASSWORD", "GATEWAY_PASSWORD", "IOT_ADMIN_API_TOKEN", "EDGE_UI_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    # Exactly the shape of the existing updater's file, copied across unedited.
    (tmp_path / ".env").write_text(
        "IOT_ADMIN_API_TOKEN=token-value\n"
        "CRADLEPOINT_PASSWORD=cradle-secret\n"
        "GATEWAY_PASSWORD=gateway-secret\n"
        "EDGE_UI_PASSWORD=ui-secret\n",
        encoding="utf-8",
    )

    status = dev.env_status()
    defaults = dev.load_env_defaults()

    assert status.exists and status.ok
    assert status.missing_required == ()
    assert status.path == tmp_path / ".env"
    assert defaults["CRADLEPOINT_PASSWORD"] == "cradle-secret"
    assert defaults["GATEWAY_PASSWORD"] == "gateway-secret"


def test_5b_the_env_variable_names_match_the_existing_updater():
    """Steve copies his file without rewriting it, so the names must agree."""
    # ast.unparse normalises quoting, so match either quote character.
    pattern = r"['\"]([A-Z_]+)['\"]: os\.environ\.get"
    legacy_names = set(re.findall(pattern, _function_source(LEGACY_MODULE, "load_env_defaults")))
    dev_names = set(re.findall(pattern, _function_source(DEV_MODULE, "load_env_defaults")))
    assert legacy_names, "the original's variable names could not be read"
    assert dev_names == legacy_names
    assert set(dev.REQUIRED_ENV_VARS) <= dev_names


def test_6_a_missing_env_is_reported_with_the_path_and_the_variable_names(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    for name in dev.REQUIRED_ENV_VARS + dev.OPTIONAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    status = dev.env_status()
    message = status.message()

    assert not status.exists and not status.ok
    assert str(tmp_path / ".env") in message
    for name in dev.REQUIRED_ENV_VARS:
        assert name in message
    assert ".env.example" in message


def test_6b_a_present_but_incomplete_env_names_the_missing_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    for name in dev.REQUIRED_ENV_VARS + dev.OPTIONAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("CRADLEPOINT_PASSWORD=only-this-one\n", encoding="utf-8")

    status = dev.env_status()

    assert status.exists and not status.ok
    assert status.missing_required == ("GATEWAY_PASSWORD",)
    assert "GATEWAY_PASSWORD" in status.message()


def test_6c_the_incomplete_banner_reaches_the_page(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    for name in dev.REQUIRED_ENV_VARS + dev.OPTIONAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    assert "Configuration incomplete" in dev.form_page().decode()


def test_6d_no_value_is_ever_put_in_a_status_message(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    (tmp_path / ".env").write_text("CRADLEPOINT_PASSWORD=hunter2\n", encoding="utf-8")

    assert "hunter2" not in dev.env_status().message()


# --- 7, 8: secrets stay out of the MSI and the logs --------------------------


def test_7_the_example_file_carries_names_but_no_values():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for name in dev.REQUIRED_ENV_VARS:
        assert f"{name}=" in text
    populated = [
        line for line in text.splitlines()
        if re.match(r"^[A-Z_]*(PASSWORD|TOKEN|SECRET|PASSPHRASE)[A-Z_]*=.+", line.strip())
    ]
    assert populated == []


def test_7b_the_build_refuses_to_ship_a_populated_credential():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "Refusing to build" in script
    assert 'find "$STAGE" -name ".env"' in script


def test_7c_no_env_file_is_tracked_by_git():
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    assert [name for name in tracked if Path(name).name == ".env"] == []


def test_7d_a_dev_updater_env_would_be_ignored_by_git():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "deploy/dev-updater/.env"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, "a .env beside the launcher would be committable"


@pytest.mark.skipif(not MSI.exists(), reason="MSI not built")
def test_7e_the_built_msi_contains_no_populated_credential():
    blob = MSI.read_bytes()
    for needle in (b"CRADLEPOINT_PASSWORD=", b"GATEWAY_PASSWORD=", b"IOT_ADMIN_API_TOKEN="):
        for match in re.finditer(re.escape(needle), blob):
            trailing = blob[match.end(): match.end() + 1]
            assert trailing in (b"", b"\r", b"\n"), f"{needle!r} appears with a value in the MSI"


def test_8_redaction_covers_the_shapes_a_secret_arrives_in():
    assert "hunter2" not in runtime.redact("GATEWAY_PASSWORD=hunter2")
    assert "hunter2" not in runtime.redact("password: hunter2")
    assert "hunter2" not in runtime.redact("sudo -S -p '' hunter2")
    assert "ghp_" not in runtime.redact("token ghp_" + "a" * 24)
    assert "swordfish" not in runtime.redact("https://user:swordfish@example.com/x")


def test_8b_the_audit_log_writes_no_secret_and_lives_in_its_own_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    log = runtime.AuditLog(operator="steve")
    log.write("update", gateway_password="hunter2", detail="CRADLEPOINT_PASSWORD=hunter2")

    written = log.path.read_text(encoding="utf-8")

    assert "hunter2" not in written
    assert json.loads(written.splitlines()[0])["application"] == identity.APP_NAME
    assert json.loads(written.splitlines()[0])["updater_version"] == identity.APP_VERSION
    assert tmp_path in log.path.parents


def test_8c_the_copied_log_redactor_is_the_existing_updaters():
    """The update log itself is written by the copied code, so it inherits the
    original's redaction rather than a second implementation of it."""
    assert _method_source(DEV_MODULE, "redact") == _method_source(LEGACY_MODULE, "redact")


# --- 9, 10: the proven connection path is the one in use ---------------------


CONNECTION_FUNCTIONS = (
    "connect_client",
    "connect_client_keyboard_interactive",
    "read_shell",
    "send_shell_command",
    "wait_for_shell_text",
    "wait_for_shell_marker",
)


@pytest.mark.parametrize("name", CONNECTION_FUNCTIONS)
def test_9_the_connection_code_is_identical_to_the_existing_updater(name: str):
    """Not "similar to" -- identical. That is the whole basis for trusting it."""
    assert _function_source(DEV_MODULE, name) == _function_source(LEGACY_MODULE, name)


@pytest.mark.parametrize("name", ("ensure_cradlepoint_client", "ensure_gateway_client", "ensure_gateway_shell", "run_nested_command"))
def test_9b_the_gateway_reach_methods_are_identical_to_the_existing_updater(name: str):
    assert _method_source(DEV_MODULE, name) == _method_source(LEGACY_MODULE, name)


def test_9c_no_new_connection_implementation_was_introduced():
    source = DEV_MODULE.read_text(encoding="utf-8")
    legacy_source = LEGACY_MODULE.read_text(encoding="utf-8")
    for call in set(re.findall(r"paramiko\.\w+|socket\.create_connection", source)):
        assert call in legacy_source, f"{call} is not part of the proven implementation"


def test_10_the_gateway_is_only_ever_reached_through_the_cradlepoint():
    source = DEV_MODULE.read_text(encoding="utf-8")
    assert 'transport.open_channel("direct-tcpip", (self.request.gateway_host, 22)' in source
    assert "sock=channel" in source
    assert "ssh {self.request.gateway_user}@{self.request.gateway_host}" in source


def test_10b_the_gateway_address_is_only_an_operator_supplied_default():
    """192.168.1.200 is a value in a form field the operator can change, and the
    same default the existing updater offers -- not a hardcoded destination."""
    legacy_source = LEGACY_MODULE.read_text(encoding="utf-8")
    occurrences = re.findall(r".*192\.168\.1\.200.*", DEV_MODULE.read_text(encoding="utf-8"))
    assert occurrences, "the familiar default should still be offered"
    for line in occurrences:
        assert line in legacy_source, "the address appears somewhere the original does not have it"


# --- 11, 14, 15: components -------------------------------------------------


def test_11_the_phase_list_and_component_sets_match_the_existing_updater():
    legacy_source = LEGACY_MODULE.read_text(encoding="utf-8")
    dev_source = DEV_MODULE.read_text(encoding="utf-8")
    for constant in ("PHASES", "UI_ONLY_PHASES", "UPDATE_AGENT_PHASES", "TARGETED_AGENT_ONLY_PHASES"):
        block = re.search(rf"^{constant} = .*?(?=\n[A-Z_]+ =|\nclass |\ndef )", legacy_source, re.DOTALL | re.MULTILINE)
        assert block, constant
        assert block.group(0) in dev_source, constant


def test_11b_the_form_keeps_the_same_fields_and_checkbox_mechanism(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()

    for field in ("gateway_id", "site_id", "cradlepoint_host", "cradlepoint_user", "gateway_host", "gateway_user", "dry_run"):
        assert f'name="{field}"' in page
    assert page.count('type="checkbox" name="selected_phases"') == len(dev.PHASES)
    assert "Run Preflight" in page
    for preset in ("Select all", "Clear all", "Edge UI only", "Edge Agent only"):
        assert f">{preset}</button>" in page


def test_14_edge_ui_only_selects_no_agent_phase():
    agent_phases = set(dev.UPDATE_AGENT_PHASES) - set(dev.UI_ONLY_PHASES)
    assert agent_phases, "the two component sets must actually differ"
    assert not set(dev.UI_ONLY_PHASES) & agent_phases
    for index in dev.UI_ONLY_PHASES:
        assert "agent" not in dev.PHASES[index].lower()


def test_15_edge_agent_only_selects_no_ui_phase():
    ui_work = {index for index in dev.UI_ONLY_PHASES if index != 0}  # phase 0 is shared inspection
    assert not set(dev.TARGETED_AGENT_ONLY_PHASES) & ui_work


def test_14b_the_page_presets_tick_exactly_those_sets(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()
    assert f"const UI_ONLY_PHASES = {list(dev.UI_ONLY_PHASES)};" in page
    assert f"const AGENT_ONLY_PHASES = {list(dev.TARGETED_AGENT_ONLY_PHASES)};" in page


# --- 12, 13: pinned commits --------------------------------------------------


def test_13_the_full_forty_character_shas_are_what_is_used_internally():
    assert re.fullmatch(r"[0-9a-f]{40}", dev.DEFAULT_EDGE_UI_COMMIT)
    assert re.fullmatch(r"[0-9a-f]{40}", dev.DEFAULT_EDGE_UPDATE_REF)


def test_12_the_seven_character_ids_are_what_the_operator_sees(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()

    assert len(dev.DEFAULT_EDGE_UI_COMMIT_SHORT) == 7
    assert len(dev.DEFAULT_EDGE_UPDATE_REF_SHORT) == 7
    assert dev.DEFAULT_EDGE_UI_COMMIT_SHORT == dev.DEFAULT_EDGE_UI_COMMIT[:7]
    assert dev.DEFAULT_EDGE_UPDATE_REF_SHORT == dev.DEFAULT_EDGE_UPDATE_REF[:7]
    assert f"<code>{dev.DEFAULT_EDGE_UI_COMMIT_SHORT}</code>" in page
    assert f"<code>{dev.DEFAULT_EDGE_UPDATE_REF_SHORT}</code>" in page
    assert "release/edge-ui-0.2.0" in page and "release/edge-agent-0.2.0" in page


def test_13b_the_targets_are_pinned_commits_not_a_moving_branch():
    manifest = json.loads(DEV_MANIFEST.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["edge_ui_tag"])
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["agent_source_commit"])
    source = DEV_MODULE.read_text(encoding="utf-8")
    assert "origin/main" not in source


def test_13c_the_development_manifest_is_a_separate_file_from_jims():
    assert DEV_MANIFEST.exists()
    assert DEV_MANIFEST != LEGACY_MANIFEST
    assert "dev_updater" in str(DEV_MANIFEST)
    assert dev.DEFAULT_RELEASE_MANIFEST.endswith("edge-0.2.0-dev.json")
    assert "releases/manifests/edge-0.2.0.json" not in DEV_MODULE.read_text(encoding="utf-8")


def test_13d_the_pinned_artifact_hash_validates():
    status = dev.release_package_status(dev.DEFAULT_RELEASE_MANIFEST)
    assert status.startswith("OK:"), status
    assert json.loads(DEV_MANIFEST.read_text(encoding="utf-8"))["sha256"] in status


def test_13e_the_approved_targets_are_the_ones_pinned():
    """The Edge UI target moved to 0bab944; the Agent target did not move."""
    assert dev.DEFAULT_EDGE_UI_COMMIT == "0bab9442c4f736312d41bdeab08b3ef2d8141db0"
    assert dev.DEFAULT_EDGE_UI_COMMIT_SHORT == "0bab944"
    assert dev.DEFAULT_EDGE_UPDATE_REF == "40133f2a81390db92a01b33a9c02c48a07363a7e"
    assert dev.DEFAULT_EDGE_UPDATE_REF_SHORT == "40133f2"


def test_13e2_no_superseded_edge_ui_target_survives_anywhere():
    """Each retarget leaves the previous SHA behind in a manifest, a document or
    a staged copy, where it reads as still current. None of them may remain."""
    superseded = ("cd4c0a5", "3246bff", "760bde8", "02e9746")
    # Everything an operator or the build reads. Not this file: it has to spell
    # the retired SHAs out to look for them.
    searched = (
        DEV_MANIFEST,
        DEV_MODULE,
        REPO_ROOT / "deploy" / "dev-updater" / "README.md",
        REPO_ROOT / "docs" / "dev-updater-operator-guide.md",
        REPO_ROOT / "deploy" / "dev-updater" / "build-msi.sh",
    )
    for path in searched:
        text = path.read_text(encoding="utf-8")
        for old in superseded:
            assert old not in text, f"{path.name} still names {old}"


def test_13f_this_product_ships_its_own_artifact_not_jims():
    """Both manifests once named the same tarball. Retargeting the Edge UI
    commit means rebuilding it -- which would have broken the checksum in Jim's
    manifest -- so this product now builds and ships its own."""
    dev_manifest = json.loads(DEV_MANIFEST.read_text(encoding="utf-8"))
    legacy_manifest = json.loads(LEGACY_MANIFEST.read_text(encoding="utf-8"))

    assert dev_manifest["artifact"] != legacy_manifest["artifact"]
    assert dev_manifest["sha256"] != legacy_manifest["sha256"]
    assert dev_manifest["artifact"].startswith("tools/dev_updater/releases/")
    assert (REPO_ROOT / dev_manifest["artifact"]).is_file()
    # Jim's artifact still matches the checksum his manifest declares.
    legacy_artifact = REPO_ROOT / legacy_manifest["artifact"]
    assert hashlib.sha256(legacy_artifact.read_bytes()).hexdigest() == legacy_manifest["sha256"]


# --- 16, 17: the MSI ---------------------------------------------------------


def test_16_the_product_has_its_own_identity():
    assert identity.PRODUCT_NAME == "IOT Edge Development Updater"
    assert identity.APP_VERSION == "0.2.0-dev.3"
    assert identity.MSI_PRODUCT_VERSION == "0.2.3"
    assert identity.UPGRADE_CODE == "AECCDF45-A1D2-43A5-9142-32E6A984A66E"
    assert identity.WINDOWS_INSTALL_DIR == r"C:\Program Files\IOT Edge Development Updater"
    assert identity.WINDOWS_DATA_DIR.endswith(r"IOT\EdgeDevUpdater")
    assert identity.pid_path().name == "IOTEdgeDevUpdater.pid"


def test_16b_the_title_states_what_this_is(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()
    assert "<h1>IOT Edge Development Updater</h1>" in page
    assert f"Version {identity.APP_VERSION}" in page
    assert "<h1>Legacy Edge Upgrade</h1>" not in page


def test_16bb_preflight_summary_names_this_updater_and_version():
    source = DEV_MODULE.read_text(encoding="utf-8")
    assert '"Updater": f"{identity.PRODUCT_NAME} {identity.APP_VERSION}"' in source


def _agent_only_request_body(agent_commit: str = "") -> bytes:
    suffix = f"&edge_agent_commit={agent_commit}".encode()
    return (
        b"gateway_id=GW006&cloud_url=https%3A%2F%2Fexample.test&admin_api_token=x"
        b"&cradlepoint_host=10.0.0.1&cradlepoint_password=x&gateway_password=x&ui_password=x"
        b"&selected_phases=7" + suffix
    )


def test_agent_commit_blank_retains_manifest_authority():
    request = dev.parse_upgrade_request(_agent_only_request_body())
    manifest_commit = dev.load_release_definition().agent_source_commit
    assert request.effective_agent_commit == manifest_commit
    assert request.git_ref == manifest_commit
    assert request.agent_source == "Validated release manifest"


def test_explicit_agent_commit_is_the_single_authority(monkeypatch):
    pilot = "218c0f63c818f1406582a7db0e3ce5fb9995743a"
    resolved = type("Resolved", (), {"full_sha": pilot})()
    monkeypatch.setattr(dev, "resolve_commit", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(dev, "read_agent_version_from_source", lambda *_args, **_kwargs: "0.2.2")

    request = dev.parse_upgrade_request(_agent_only_request_body(pilot))
    commands = "\n".join(command for _label, command, _sudo in dev.repo_commands(request))
    final = "\n".join(command for _label, command, _sudo in dev.final_commands(request, pre_restart_timestamp="100"))

    assert request.effective_agent_commit == pilot
    assert request.expected_agent_version == "0.2.2"
    assert request.agent_source == "Explicit operator override"
    assert pilot in commands and pilot in final
    assert dev.load_release_definition().agent_source_commit not in commands


def test_invalid_agent_commit_stops_before_any_gateway_work(monkeypatch):
    monkeypatch.setattr(dev, "resolve_commit", lambda *_args, **_kwargs: (_ for _ in ()).throw(CommitResolutionError("bad commit")))

    with pytest.raises(ValueError, match="bad commit"):
        dev.parse_upgrade_request(_agent_only_request_body("not-a-commit"))


def test_actual_agent_runtime_validation_requires_commit_version_and_restart():
    request = dev.UpgradeRequest(
        gateway_id="GW006", site_id="GW006", cloud_url="https://example.test", admin_api_token="x",
        cradlepoint_host="x", cradlepoint_user="x", cradlepoint_password="x", gateway_host="x",
        gateway_user="x", gateway_password="x", git_ref="a" * 40, remote_repo="/repo",
        ui_source_folder="x", ui_username="x", ui_password="x", effective_agent_commit="a" * 40,
        expected_agent_version="0.2.2",
    )
    valid = "\n".join((
        f"AGENT_RELEASE_COMMIT={request.effective_agent_commit}", "AGENT_PACKAGE_VERSION=0.2.2",
        "AGENT_MODULE_VERSION=0.2.2", "AGENT_SERVICE_STATE=active", "AGENT_SERVICE_START=200",
    ))
    dev.validate_agent_runtime_output(valid, request, "100")
    with pytest.raises(RuntimeError, match="Actual Agent runtime validation failed"):
        dev.validate_agent_runtime_output(valid.replace("AGENT_MODULE_VERSION=0.2.2", "AGENT_MODULE_VERSION=0.2.0"), request, "100")


@pytest.mark.skipif(not MSI.exists(), reason="MSI not built")
def test_16c_the_msi_installs_under_its_own_name_and_codes():
    blob = MSI.read_bytes()
    assert identity.UPGRADE_CODE.encode() in blob or identity.UPGRADE_CODE.lower().encode() in blob
    assert b"IOT Edge Development Updater" in blob


@pytest.mark.skipif(not MSI.exists(), reason="MSI not built")
def test_16d_the_msi_ships_the_copied_updater_and_its_own_manifest():
    blob = MSI.read_bytes()
    assert b"updater_webapp.py" in blob
    assert b"edge-0.2.0-dev.json" in blob
    assert b"legacy_edge_upgrade_webapp.py" not in blob


def test_17_uninstall_removes_only_this_products_own_folders():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    removals = re.findall(r'<RemoveFolder Id="([^"]+)"', script)
    assert removals == ["AppMenuFolder"], removals
    # The data directory carries no RemoveFolder, so a copied .env and the logs
    # survive both upgrade and uninstall.
    assert "DEVUPDATERDATA" in script
    assert '<RemoveFolder Id="DEVUPDATERDATA"' not in script
    assert '<RemoveFolder Id="DEVUPDATERLOGS"' not in script


def test_17b_the_upgrade_code_is_this_products_alone():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert identity.UPGRADE_CODE in script
    for path in (LEGACY_MODULE, LEGACY_LAUNCHER):
        assert identity.UPGRADE_CODE not in path.read_text(encoding="utf-8", errors="ignore")


def test_17c_the_launcher_keeps_its_environment_out_of_the_legacy_checkout():
    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert r"%ProgramData%\IOT\EdgeDevUpdater" in launcher
    assert "tools.dev_updater" in launcher
    # The virtual environment is created inside this product's own data
    # directory, not in the legacy checkout. (The legacy venv is named in a
    # comment for contrast, so match the assignment rather than the word.)
    venv = re.search(r'^set "VENV=(.+)"', launcher, re.MULTILINE)
    assert venv, "the launcher must pin its own venv location"
    assert venv.group(1).startswith("%DATADIR%")
    assert f"set \"VENV=%DATADIR%\\{identity.LEGACY_VENV_DIR_NAME}\"" not in launcher


# --- the manual-only gate ----------------------------------------------------


def test_cloud_job_claiming_is_off_unless_explicitly_requested(monkeypatch):
    """Two updaters polling the same queue would race for the same job."""
    monkeypatch.delenv(dev.CLAIM_ENV_VAR, raising=False)
    assert dev.cloud_claiming_enabled() is False
    monkeypatch.setenv(dev.CLAIM_ENV_VAR, "1")
    assert dev.cloud_claiming_enabled() is True


# --- 18: the existing updater is unaffected ----------------------------------


def test_18_the_legacy_updater_still_imports_and_keeps_its_own_settings():
    result = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, sys;"
         f"sys.path.insert(0, {str(REPO_ROOT)!r});"
         f"spec = importlib.util.spec_from_file_location('legacy', {str(LEGACY_MODULE)!r});"
         "m = importlib.util.module_from_spec(spec);"
         # Dataclasses resolve their module from sys.modules during creation.
         "sys.modules['legacy'] = m; spec.loader.exec_module(m);"
         "print(m.DEFAULT_PORT); print(m.DEFAULT_RELEASE_MANIFEST)"],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )
    assert result.returncode == 0, result.stderr
    port, manifest = result.stdout.split()
    assert port == "8766"
    assert manifest.endswith("tools/releases/manifests/edge-0.2.0.json")
