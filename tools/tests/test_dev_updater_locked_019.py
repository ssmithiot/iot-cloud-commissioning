from __future__ import annotations

import ast
import hashlib
import json
import os
import socket
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest

from tools.dev_updater import identity
from tools.dev_updater import updater_webapp as dev
from tools.dev_updater.commit_resolution import (
    CommitResolutionError,
    EDGE_AGENT_REPOSITORY,
    resolve_commit,
)
from tools.dev_updater import ui_artifact

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "tools/legacy_edge_upgrade_webapp.py"
DEV = ROOT / "tools/dev_updater/updater_webapp.py"
BASELINE = {
    "tools/legacy_edge_upgrade_webapp.py": "317b9850093bafe9253db9a5845a828f43f16d0884993836143113218084cd8c",
    "tools/start-legacy-edge-upgrade-webapp.cmd": "4117769e55c2b7a0680e1f91453dee56a123d585ae3fcebf167623a8d26ff6a7",
    "tools/gateway-update-requirements.txt": "68adc66b9c7a2d58ef8db894fd37cd5ae465d597ae7eb0f3cb14789316038dfa",
    "tools/releases/manifests/edge-0.1.9.json": "dffbb61a800917eee26a3b1711d3f6fce7c23b82c442af66bcf1214f5199efa0",
}
CONNECTIONS = (
    "connect_client", "connect_client_keyboard_interactive", "read_shell", "send_shell_command",
    "wait_for_shell_text", "wait_for_shell_marker", "ensure_cradlepoint_client", "ensure_gateway_client",
    "ensure_gateway_shell", "run_nested_command",
)

def code(path: Path, name: str) -> str:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(name)

def test_protected_legacy_files_are_unchanged():
    for relative, expected in BASELINE.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected

@pytest.mark.parametrize("name", CONNECTIONS)
def test_connection_and_nested_ssh_functions_are_literal_copies(name: str):
    assert code(DEV, name) == code(LEGACY, name)

def test_identity_isolated_and_version_mapping_is_explicit(monkeypatch):
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    assert identity.DEFAULT_PORT == 8791
    assert identity.LEGACY_PORT == 8766
    assert identity.APP_VERSION == "0.2.0-dev.2"
    assert identity.MSI_PRODUCT_VERSION == "0.2.2"
    assert identity.UPGRADE_CODE == "AECCDF45-A1D2-43A5-9142-32E6A984A66E"
    assert identity.env_path().name == ".env"
    assert "EdgeDevUpdater" in str(identity.env_path())


def test_msi_upgrade_preserves_programdata_env_and_uses_new_product_version():
    build = (ROOT / "deploy/dev-updater/build-msi.sh").read_text(encoding="utf-8")
    assert 'MSI="$OUT_DIR/$APP-0.2.0-dev.2-x64.msi"' in build
    assert 'UPGRADE_CODE=\'AECCDF45-A1D2-43A5-9142-32E6A984A66E\'' in build
    assert 'Version="$VERSION"' in build and 'RemoveExistingProducts After="InstallInitialize"' in build
    assert 'cp "$REPO"/deploy/dev-updater/requirements.txt "$REPO"/deploy/dev-updater/.env.example "$STAGE/"' in build
    assert 'find "$STAGE" -name .env -print -quit' in build

def test_form_keeps_the_original_phase_values_and_displays_commit_controls(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()
    assert "Updater Version 0.2.0-dev.2" in page
    assert page.count('type="checkbox" name="selected_phases"') == len(dev.PHASES)
    for index, phase in enumerate(dev.PHASES):
        assert f'value="{index}" checked> {phase}' in page
    assert 'name="edge_ui_commit"' in page
    assert 'name="edge_agent_commit"' in page
    assert 'name="commit_resolution_confirmed"' in page

class Response:
    def __init__(self, payload): self.payload = payload
    def read(self): return json.dumps(self.payload).encode()
    def __enter__(self): return self
    def __exit__(self, *_): return False

def test_full_and_short_sha_resolution_is_explicit_and_full_length():
    full = "a" * 40
    result = resolve_commit(EDGE_AGENT_REPOSITORY, "a" * 7, opener=lambda *_args, **_kwargs: Response({"sha": full}))
    assert result.full_sha == full
    assert result.repository == EDGE_AGENT_REPOSITORY
    with pytest.raises(CommitResolutionError):
        resolve_commit(EDGE_AGENT_REPOSITORY, "main", opener=lambda *_args, **_kwargs: Response({"sha": full}))
    with pytest.raises(CommitResolutionError):
        resolve_commit(EDGE_AGENT_REPOSITORY, "b" * 7, opener=lambda *_args, **_kwargs: Response({"sha": full}))

def test_github_token_is_sent_only_as_an_authorization_header():
    seen = []
    def opener(request, **_kwargs):
        seen.append(request.headers)
        return Response({"sha": "a" * 40})
    resolve_commit(EDGE_AGENT_REPOSITORY, "a" * 7, token="token-for-test", opener=opener)
    assert seen[0]["Authorization"] == "Bearer token-for-test"

def test_git_askpass_uses_pat_basic_credentials_without_token_leakage(tmp_path):
    token = "private-token-for-test"
    environment = ui_artifact.git_askpass_environment(token, tmp_path)
    helper = Path(environment["GIT_ASKPASS"])
    assert helper.exists() and environment["GIT_TERMINAL_PROMPT"] == "0"
    assert token not in helper.read_text()
    assert token not in " ".join(["git", "clone", "--no-checkout", ui_artifact.REPOSITORY_URL])
    assert subprocess.check_output([str(helper), "Username for 'https://github.com':"], text=True, env=environment).strip() == "x-access-token"
    assert subprocess.check_output([str(helper), "Password for 'https://x-access-token@github.com':"], text=True, env=environment).strip() == token
    assert ui_artifact._safe_git_error(RuntimeError(f"clone failed {token}"), token) == "clone failed [redacted]"

@pytest.mark.skipif(not os.environ.get("GITHUB_TOKEN"), reason="private GitHub integration token not available on this build host")
def test_private_edge_ui_commit_clones_and_checks_out_with_pat(tmp_path):
    artifact = ui_artifact.materialize("0bab9442c4f736312d41bdeab08b3ef2d8141db0", root=tmp_path, token=os.environ["GITHUB_TOKEN"])
    assert artifact.commit == "0bab9442c4f736312d41bdeab08b3ef2d8141db0"

def test_final_execution_requires_reviewed_resolved_commits(monkeypatch):
    resolved = type("Resolved", (), {"full_sha": "a" * 40})()
    monkeypatch.setattr(dev, "resolve_requested_commits", lambda _fields: (resolved, resolved))
    body = b"gateway_id=GW1&cloud_url=https%3A%2F%2Fexample.test&admin_api_token=x&cradlepoint_host=10.0.0.1&cradlepoint_password=x&gateway_password=x&ui_password=x&final_update_confirmed=1"
    with pytest.raises(ValueError, match="commit confirmation"):
        dev.parse_upgrade_request(body)

def test_cloud_claiming_is_off_unless_the_explicit_switch_is_set(monkeypatch):
    monkeypatch.delenv("IOT_EDGE_DEV_UPDATER_CLAIM_CLOUD_JOBS", raising=False)
    assert "CLAIM_CLOUD_JOBS" in DEV.read_text()

def test_development_audit_log_is_separate_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    log = dev.LiveLog("test", dev.Redactor(["secret-value"]))
    log.append("UI_SOURCE_COMMIT=abc\npassword=secret-value\n")
    written = log.audit_path.read_text()
    assert log.audit_path.parent == tmp_path / "logs"
    assert "secret-value" not in written and "UI_SOURCE_COMMIT=abc" in written

def test_development_env_commit_and_port_defaults_are_loaded_and_shown_short(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    ui_sha, agent_sha = "a" * 40, "b" * 40
    (tmp_path / ".env").write_text(
        f"IOT_EDGE_DEV_UI_COMMIT={ui_sha}\nIOT_EDGE_DEV_AGENT_COMMIT={agent_sha}\nIOT_EDGE_DEV_UPDATER_PORT=8791\n"
    )
    page = dev.form_page().decode()
    assert f'name="edge_ui_commit" value="{ui_sha[:7]}"' in page
    assert f'name="edge_agent_commit" value="{agent_sha[:7]}"' in page
    assert dev.configured_default("IOT_EDGE_DEV_UPDATER_PORT", "8791") == "8791"

def test_two_distinct_local_listeners_can_run_together():
    legacy, development = socket.socket(), socket.socket()
    try:
        legacy.bind(("127.0.0.1", 0)); legacy.listen()
        development.bind(("127.0.0.1", 0)); development.listen()
        assert legacy.getsockname()[1] != development.getsockname()[1]
    finally:
        legacy.close(); development.close()


def run_full_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tar_script: str, *, timeout_seconds: int = 5) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "swadmin"
    (root / "edge-bacnet-ui-v2" / "data").mkdir(parents=True)
    (root / "edge-bacnet-ui-v2" / "data" / "history.sqlite").write_bytes(b"live data")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tar = fake_bin / "tar"
    tar.write_text("#!/bin/sh\n" + textwrap.dedent(tar_script), encoding="utf-8")
    tar.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    return subprocess.run(
        ["/bin/sh", "-c", dev.full_backup_command(str(root), timeout_seconds=timeout_seconds, heartbeat_seconds=1)],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )


REAL_TAR = 'exec /usr/bin/tar "$@"\n'


def test_full_backup_accepts_a_clean_tar_and_valid_archive(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, REAL_TAR)
    assert result.returncode == 0
    assert "BACKUP_TAR_RESULT=clean" in result.stdout
    assert "BACKUP_ARCHIVE_VALID=Passed" in result.stdout


def test_full_backup_accepts_only_live_file_change_warning_after_validation(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, '''
        if [ "$1" = "-czf" ]; then
          /usr/bin/tar "$@" || exit $?
          echo "tar: edge-bacnet-ui-v2/data/history.sqlite: file changed as we read it" >&2
          exit 1
        fi
        exec /usr/bin/tar "$@"
    ''')
    assert result.returncode == 0
    assert "file changed as we read it" in result.stdout
    assert "BACKUP_TAR_RESULT=live-file-change-warning" in result.stdout
    assert "BACKUP_ARCHIVE_VALID=Passed" in result.stdout


def test_full_backup_rejects_live_warning_when_archive_is_invalid(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, '''
        if [ "$1" = "-czf" ]; then
          printf invalid > "$2"
          echo "tar: edge-bacnet-ui-v2/data/history.sqlite: file changed as we read it" >&2
          exit 1
        fi
        exec /usr/bin/tar "$@"
    ''')
    assert result.returncode != 0
    assert "BACKUP_ARCHIVE_INVALID=gzip" in result.stderr


def test_full_backup_rejects_non_warning_tar_failure(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, '''
        if [ "$1" = "-czf" ]; then echo "tar: fatal error" >&2; exit 2; fi
        exec /usr/bin/tar "$@"
    ''')
    assert result.returncode == 2
    assert "BACKUP_TAR_RESULT=failed exit=2" in result.stderr


@pytest.mark.parametrize("mode", ["zero", "missing"])
def test_full_backup_rejects_missing_or_zero_byte_archive(tmp_path, monkeypatch, mode):
    action = ': > "$2"' if mode == "zero" else 'rm -f "$2"'
    result = run_full_backup(tmp_path, monkeypatch, f'''
        if [ "$1" = "-czf" ]; then {action}; exit 0; fi
        exec /usr/bin/tar "$@"
    ''')
    assert result.returncode != 0
    assert "BACKUP_ARCHIVE_INVALID=missing-or-zero-byte" in result.stderr


def test_full_backup_rejects_gzip_integrity_failure(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, '''
        if [ "$1" = "-czf" ]; then printf invalid > "$2"; exit 0; fi
        exec /usr/bin/tar "$@"
    ''')
    assert result.returncode != 0
    assert "BACKUP_ARCHIVE_INVALID=gzip" in result.stderr


def test_full_backup_rejects_tar_listing_failure(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, '''
        if [ "$1" = "-tzf" ]; then echo "listing failed" >&2; exit 1; fi
        exec /usr/bin/tar "$@"
    ''')
    assert result.returncode != 0
    assert "BACKUP_ARCHIVE_INVALID=listing" in result.stderr


def test_full_backup_timeout_is_reported_clearly(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, '''
        if [ "$1" = "-czf" ]; then sleep 3; fi
        exec /usr/bin/tar "$@"
    ''', timeout_seconds=1)
    assert result.returncode == 124
    assert "BACKUP_TAR_RESULT=failed exit=124" in result.stderr
    assert "BACKUP_PROGRESS_HEARTBEAT=" in result.stdout

def ui_checkout(root: Path, *, include_required: bool = True) -> tuple[Path, str]:
    root.mkdir(); subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    names = ui_artifact.REQUIRED_FILES if include_required else ui_artifact.REQUIRED_FILES[:-1]
    for name in names:
        (root / name).write_text(name)
    for directory in ui_artifact.REQUIRED_DIRS:
        (root / directory).mkdir(); (root / directory / "asset.txt").write_text(directory)
    (root / ".env").write_text("SECRET=value")
    (root / "tests").mkdir(); (root / "tests" / "bad.py").write_text("bad")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return root, subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

def test_ui_artifact_is_built_from_the_exact_clean_commit_and_is_allowlisted(tmp_path):
    source, commit = ui_checkout(tmp_path / "source")
    built = ui_artifact.build_from_checkout(source, commit, tmp_path / "artifact.tar.gz")
    assert built.commit == commit and built.sha256 == ui_artifact.sha256(built.path)
    ui_artifact.verify_contents(built.path)
    dev.validate_embedded_ui_artifact_contents(built.path)
    with tarfile.open(built.path, "r:gz") as archive:
        names = archive.getnames()
    assert ".env" not in names and not any(name.startswith("tests/") for name in names)

def test_ui_artifact_rejects_missing_runtime_file_and_hash_tampering(tmp_path):
    source, commit = ui_checkout(tmp_path / "source", include_required=False)
    with pytest.raises(ui_artifact.UIArtifactError, match="missing"):
        ui_artifact.build_from_checkout(source, commit, tmp_path / "bad.tar.gz")
    source, commit = ui_checkout(tmp_path / "source2")
    built = ui_artifact.build_from_checkout(source, commit, tmp_path / "good.tar.gz")
    built.path.write_bytes(b"not a tarball")
    with pytest.raises((ui_artifact.UIArtifactError, tarfile.ReadError)):
        ui_artifact.verify_contents(built.path)

def test_two_ui_commits_produce_commit_bound_artifacts(tmp_path):
    source, first = ui_checkout(tmp_path / "source")
    one = ui_artifact.build_from_checkout(source, first, tmp_path / "one.tar.gz")
    (source / "app.py").write_text("second commit")
    subprocess.run(["git", "-C", str(source), "commit", "-am", "second", "-q"], check=True)
    second = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    two = ui_artifact.build_from_checkout(source, second, tmp_path / "two.tar.gz")
    assert one.commit != two.commit and one.sha256 != two.sha256

def test_ui_only_materializes_the_resolved_ui_artifact_and_agent_only_does_not(monkeypatch, tmp_path):
    resolved_ui = type("Resolved", (), {"full_sha": "a" * 40})()
    resolved_agent = type("Resolved", (), {"full_sha": "b" * 40})()
    artifact = type("Artifact", (), {"path": tmp_path / "ui.tar.gz", "sha256": "c" * 64})()
    calls = []
    monkeypatch.setattr(dev, "resolve_requested_commits", lambda _fields: (resolved_ui, resolved_agent))
    monkeypatch.setattr(dev, "materialize_ui_artifact", lambda sha, **_kwargs: calls.append(sha) or artifact)
    base = b"gateway_id=GW1&cloud_url=https%3A%2F%2Fexample.test&admin_api_token=x&cradlepoint_host=10.0.0.1&cradlepoint_password=x&gateway_password=x&ui_password=x"
    ui_request = dev.parse_upgrade_request(base + b"&selected_phases=1")
    assert calls == ["a" * 40] and ui_request.ui_artifact_sha256 == "c" * 64
    calls.clear()
    agent_request = dev.parse_upgrade_request(base + b"&selected_phases=7")
    assert calls == [] and agent_request.ui_artifact_path == ""


def test_manual_update_selected_phase_selection_is_unchanged(monkeypatch):
    resolved = type("Resolved", (), {"full_sha": "a" * 40})()
    artifact = type("Artifact", (), {"path": Path("/tmp/ui.tar.gz"), "sha256": "b" * 64})()
    monkeypatch.setattr(dev, "resolve_requested_commits", lambda _fields: (resolved, resolved))
    monkeypatch.setattr(dev, "materialize_ui_artifact", lambda *_args, **_kwargs: artifact)
    body = (
        b"gateway_id=GW006&cloud_url=https%3A%2F%2Fexample.test&admin_api_token=x"
        b"&cradlepoint_host=10.0.0.1&cradlepoint_password=x&gateway_password=x&ui_password=x"
        b"&selected_phases=1"
    )
    request = dev.parse_upgrade_request(body)
    assert request.gateway_id == "GW006"
    assert request.selected_phases == (1,)


def agent_only_request_body() -> bytes:
    return (
        b"gateway_id=GW006&cloud_url=https%3A%2F%2Fexample.test&admin_api_token=x"
        b"&cradlepoint_host=10.0.0.1&cradlepoint_password=x&gateway_password=x&ui_password=x"
        b"&selected_phases=7"
    )


def test_no_development_agent_override_retains_manifest_authority(monkeypatch):
    manifest_agent = dev.load_release_definition().agent_source_commit
    resolved_ui = type("Resolved", (), {"full_sha": "a" * 40})()
    resolved_agent = type("Resolved", (), {"full_sha": "b" * 40})()
    monkeypatch.delenv("IOT_EDGE_DEV_AGENT_COMMIT", raising=False)
    monkeypatch.setattr(dev, "resolve_requested_commits", lambda _fields: (resolved_ui, resolved_agent))
    request = dev.parse_upgrade_request(agent_only_request_body())
    assert request.edge_agent_commit == manifest_agent
    assert request.git_ref == manifest_agent
    assert request.agent_source == "Validated release manifest"
    assert manifest_agent in "\n".join(command for _label, command, _sudo in dev.repo_commands(request))


def test_development_agent_override_is_the_only_agent_authority(monkeypatch):
    pilot = "8fecbe6b1d6834626dbc5d3cecdd2401b10aec58"
    resolved_ui = type("Resolved", (), {"full_sha": "2adae3adeb339806330db0e481cba3179fff2ff1"})()
    resolved_agent = type("Resolved", (), {"full_sha": pilot})()
    monkeypatch.setenv("IOT_EDGE_DEV_AGENT_COMMIT", pilot)
    monkeypatch.setattr(dev, "resolve_requested_commits", lambda _fields: (resolved_ui, resolved_agent))
    monkeypatch.setattr(dev, "read_agent_version_from_source", lambda commit, **_kwargs: "0.2.1" if commit == pilot else "wrong")
    request = dev.parse_upgrade_request(agent_only_request_body())
    assert request.edge_agent_commit == pilot
    assert request.git_ref == pilot
    assert request.expected_agent_version == "0.2.1"
    assert request.agent_source == "Development override"
    repo_text = "\n".join(command for _label, command, _sudo in dev.repo_commands(request))
    final_text = "\n".join(command for _label, command, _sudo in dev.final_commands(request, pre_restart_timestamp="100"))
    assert pilot in repo_text and pilot in final_text
    assert dev.load_release_definition().agent_source_commit not in repo_text
    assert "--network-traffic" in final_text
    assert "tunnel_request_timeout_sec" not in final_text


def test_candidate_version_is_read_from_immutable_source():
    class SourceResponse:
        def read(self): return b'__version__ = "0.2.1"\n'
        def __enter__(self): return self
        def __exit__(self, *_args): return False
    assert dev.read_agent_version_from_source("8fecbe6b1d6834626dbc5d3cecdd2401b10aec58", opener=lambda *_args, **_kwargs: SourceResponse()) == "0.2.1"


def test_actual_agent_runtime_validation_rejects_each_mismatch():
    request = dev.UpgradeRequest(
        gateway_id="GW006", site_id="GW006", cloud_url="https://example.test", admin_api_token="x",
        cradlepoint_host="x", cradlepoint_user="x", cradlepoint_password="x", gateway_host="x",
        gateway_user="x", gateway_password="x", git_ref="8" * 40, remote_repo="/repo",
        ui_source_folder="x", ui_username="x", ui_password="x", edge_agent_commit="8" * 40,
        expected_agent_version="0.2.1",
    )
    good = "\n".join((
        f"AGENT_RELEASE_COMMIT={request.edge_agent_commit}", "AGENT_PACKAGE_VERSION=0.2.1",
        "AGENT_MODULE_VERSION=0.2.1", "AGENT_SERVICE_STATE=active", "AGENT_SERVICE_START=200",
        "AGENT_NETWORK_TRAFFIC_CLI=Passed",
    ))
    dev.validate_agent_runtime_output(good, request, "100")
    for bad in (
        good.replace(request.edge_agent_commit, "4" * 40),
        good.replace("AGENT_PACKAGE_VERSION=0.2.1", "AGENT_PACKAGE_VERSION=0.2.0"),
        good.replace("AGENT_MODULE_VERSION=0.2.1", "AGENT_MODULE_VERSION=0.2.0"),
        good.replace("AGENT_SERVICE_STATE=active", "AGENT_SERVICE_STATE=inactive"),
        good.replace("AGENT_SERVICE_START=200", "AGENT_SERVICE_START=100"),
        good.replace("AGENT_NETWORK_TRAFFIC_CLI=Passed", "AGENT_NETWORK_TRAFFIC_CLI="),
    ):
        with pytest.raises(RuntimeError, match="Actual Agent runtime validation failed"):
            dev.validate_agent_runtime_output(bad, request, "100")
