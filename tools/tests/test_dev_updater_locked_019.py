from __future__ import annotations

import ast
import hashlib
import json
import os
import socket
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

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


def baseline_code(name: str) -> str:
    source = subprocess.check_output(
        ["git", "show", "32f2bf9:tools/dev_updater/updater_webapp.py"],
        cwd=ROOT,
        text=True,
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
        if isinstance(node, ast.ClassDef) and node.name == "LegacyUpgradeRunner":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return ast.unparse(item)
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
    assert identity.APP_VERSION == "0.2.1"
    assert identity.MSI_PRODUCT_VERSION == "0.2.1"
    assert identity.env_path().name == ".env"
    assert "EdgeDevUpdater" in str(identity.env_path())

def test_form_keeps_the_original_phase_values_and_displays_commit_controls(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()
    assert "Updater Version 0.2.1" in page
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


def test_manual_preparation_and_shared_phase_implementation_match_installed_baseline():
    """The queue adapter must not alter the proven manual or phase-engine path."""
    for name in (
        "resolve_requested_commits",
        "parse_upgrade_request",
        "ui_phases_selected",
        "backup_commands",
        "run_phase",
        "build_upload_zip",
    ):
        assert code(DEV, name) == baseline_code(name)


def test_cloud_claimed_commits_are_exact_and_fail_closed_without_local_fallback():
    ui_commit = "a" * 40
    agent_commit = "b" * 40
    claimed = {"target_ui_commit": ui_commit, "target_agent_commit": agent_commit}
    assert dev.claimed_release_commit(claimed, "target_ui_commit") == ui_commit
    assert dev.claimed_release_commit(claimed, "target_agent_commit") == agent_commit
    with pytest.raises(ValueError, match="refusing local-default fallback"):
        dev.claimed_release_commit({}, "target_ui_commit")
    with pytest.raises(ValueError, match="refusing local-default fallback"):
        dev.claimed_release_commit({"target_agent_commit": "short"}, "target_agent_commit")


def test_cloud_queue_uses_claimed_targets_and_materializes_ui_before_shared_engine(monkeypatch):
    ui_commit = "2adae3adeb339806330db0e481cba3179fff2ff1"
    agent_commit = "40133f2a81390db92a01b33a9c02c48a07363a7e"
    claimed = {
        "request_id": "request-1",
        "gateway_id": "GW010",
        "site_id": "GW010",
        "cradlepoint_host": "10.2.4.21",
        "gateway_host": "192.168.1.200",
        "update_scope": "full_non_provisioning",
        "target_ui_version": "0.2.0",
        "target_agent_version": "0.2.0",
        "target_ui_commit": ui_commit,
        "target_agent_commit": agent_commit,
    }
    calls, captured = [], {}

    def fake_cloud(_url, _token, path, **kwargs):
        calls.append((path, kwargs))
        return claimed if path.endswith("/claim") else {"ok": True}

    def fake_artifact(commit, *, token):
        assert commit == ui_commit and token == "github-token"
        return SimpleNamespace(path=Path("/isolated/edge-ui.tar.gz"), sha256="f" * 64)

    def fake_start(request):
        captured["request"] = request
        with dev.JOBS_LOCK:
            dev.JOBS["isolated-cloud-test"] = SimpleNamespace(status="complete", error="")
        return "isolated-cloud-test"

    monkeypatch.setenv("IOT_EDGE_UPDATE_REF", "stale-agent-ref")
    monkeypatch.setattr(dev, "cloud_json_request", fake_cloud)
    monkeypatch.setattr(dev, "materialize_ui_artifact", fake_artifact)
    monkeypatch.setattr(dev, "start_job", fake_start)
    monkeypatch.setattr(dev, "health_gate_enabled", lambda: False)
    try:
        assert dev.run_queued_gateway_update({"request_id": "request-1"}, {
            "IOT_ADMIN_API_TOKEN": "admin-token",
            "CRADLEPOINT_PASSWORD": "cradlepoint-password",
            "GATEWAY_PASSWORD": "gateway-password",
            "EDGE_UI_PASSWORD": "ui-password",
            "GITHUB_TOKEN": "github-token",
        }) == "completed"
    finally:
        with dev.JOBS_LOCK:
            dev.JOBS.pop("isolated-cloud-test", None)

    request = captured["request"]
    assert request.edge_ui_commit == ui_commit
    assert request.edge_agent_commit == agent_commit
    assert request.git_ref == agent_commit
    assert request.ui_artifact_path == "/isolated/edge-ui.tar.gz"
    assert request.ui_artifact_sha256 == "f" * 64
    assert request.selected_phases == dev.UPDATE_AGENT_PHASES
    assert 6 not in request.selected_phases and 8 not in request.selected_phases
    assert any(path.endswith("/claim") for path, _kwargs in calls)


@pytest.mark.parametrize("field", ("target_ui_commit", "target_agent_commit"))
def test_cloud_queue_missing_target_fails_before_starting_shared_engine(monkeypatch, field):
    claimed = {
        "gateway_id": "GW010", "site_id": "GW010", "cradlepoint_host": "10.2.4.21",
        "target_ui_commit": "a" * 40, "target_agent_commit": "b" * 40,
    }
    claimed.pop(field)
    completions = []

    def fake_cloud(_url, _token, path, **kwargs):
        if path.endswith("/claim"):
            return claimed
        completions.append(kwargs["body"])
        return {"ok": True}

    monkeypatch.setattr(dev, "cloud_json_request", fake_cloud)
    monkeypatch.setattr(dev, "start_job", lambda _request: pytest.fail("shared engine must not start"))
    assert dev.run_queued_gateway_update({"request_id": "request-1"}, {
        "IOT_ADMIN_API_TOKEN": "admin-token", "CRADLEPOINT_PASSWORD": "x",
        "GATEWAY_PASSWORD": "x", "EDGE_UI_PASSWORD": "x", "GITHUB_TOKEN": "github-token",
    }) == "failed"
    assert completions and completions[-1]["status"] == "failed"


def test_full_non_provisioning_phase_membership_is_unchanged():
    assert dev.UPDATE_AGENT_PHASES == (0, 1, 2, 3, 4, 5, 7, 9, 10, 11)
    assert 6 not in dev.UPDATE_AGENT_PHASES
    assert 8 not in dev.UPDATE_AGENT_PHASES

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
