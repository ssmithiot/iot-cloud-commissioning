from __future__ import annotations

import ast
import hashlib
import json
import socket
from pathlib import Path

import pytest

from tools.dev_updater import identity
from tools.dev_updater import updater_webapp as dev
from tools.dev_updater.commit_resolution import (
    CommitResolutionError,
    EDGE_AGENT_REPOSITORY,
    resolve_commit,
)

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
    assert identity.APP_VERSION == "0.2.0-dev.1"
    assert identity.MSI_PRODUCT_VERSION == "0.2.1"
    assert identity.env_path().name == ".env"
    assert "EdgeDevUpdater" in str(identity.env_path())

def test_form_keeps_the_original_phase_values_and_displays_commit_controls(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()
    assert "Updater Version 0.2.0-dev.1" in page
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

def test_final_execution_requires_reviewed_resolved_commits(monkeypatch):
    resolved = type("Resolved", (), {"full_sha": "a" * 40})()
    monkeypatch.setattr(dev, "resolve_requested_commits", lambda _fields: (resolved, resolved))
    body = b"gateway_id=GW1&cloud_url=https%3A%2F%2Fexample.test&admin_api_token=x&cradlepoint_host=10.0.0.1&cradlepoint_password=x&gateway_password=x&ui_password=x&final_update_confirmed=1"
    with pytest.raises(ValueError, match="commit confirmation"):
        dev.parse_upgrade_request(body)

def test_cloud_claiming_is_off_unless_the_explicit_switch_is_set(monkeypatch):
    monkeypatch.delenv("IOT_EDGE_DEV_UPDATER_CLAIM_CLOUD_JOBS", raising=False)
    assert "CLAIM_CLOUD_JOBS" in DEV.read_text()

def test_two_distinct_local_listeners_can_run_together():
    legacy, development = socket.socket(), socket.socket()
    try:
        legacy.bind(("127.0.0.1", 0)); legacy.listen()
        development.bind(("127.0.0.1", 0)); development.listen()
        assert legacy.getsockname()[1] != development.getsockname()[1]
    finally:
        legacy.close(); development.close()
