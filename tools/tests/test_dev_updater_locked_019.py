from __future__ import annotations

import ast
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import tarfile
import textwrap
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
from tools.dev_updater import trend_config_backup

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
    assert identity.APP_VERSION == "0.2.0-dev.3"
    assert identity.MSI_PRODUCT_VERSION == "0.2.3"
    assert identity.UPGRADE_CODE == "AECCDF45-A1D2-43A5-9142-32E6A984A66E"
    assert identity.env_path().name == ".env"
    assert "EdgeDevUpdater" in str(identity.env_path())


def test_msi_upgrade_preserves_programdata_env_and_uses_new_product_version():
    build = (ROOT / "deploy/dev-updater/build-msi.sh").read_text(encoding="utf-8")
    assert 'MSI="$OUT_DIR/$APP-$DISPLAY_VERSION-x64.msi"' in build
    assert 'UPGRADE_CODE=\'AECCDF45-A1D2-43A5-9142-32E6A984A66E\'' in build
    assert "DISPLAY_VERSION" in build and "MSI_PRODUCT_VERSION" in build
    assert "IOT Edge Development Updater $DISPLAY_VERSION" in build
    assert '<Directory Id="INSTALLDIR" Name="$PRODUCT"/>' in build
    assert 'Version="$VERSION"' in build and 'RemoveExistingProducts After="InstallInitialize"' in build
    assert 'cp "$REPO"/deploy/dev-updater/requirements.txt "$REPO"/deploy/dev-updater/.env.example "$STAGE/"' in build
    assert 'find "$STAGE" -name .env -print -quit' in build

def test_form_keeps_the_original_phase_values_and_displays_commit_controls(tmp_path, monkeypatch):
    monkeypatch.setenv(identity.DATA_DIR_ENV_VAR, str(tmp_path))
    page = dev.form_page().decode()
    assert "Updater Version 0.2.0-dev.3" in page
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


def cloud_queue_defaults() -> dict[str, str]:
    return {
        "IOT_ADMIN_API_TOKEN": "admin-token",
        "CRADLEPOINT_PASSWORD": "cradlepoint-password",
        "GATEWAY_PASSWORD": "gateway-password",
        "EDGE_UI_PASSWORD": "ui-password",
        "GITHUB_TOKEN": "github-token",
    }


def claimed_cloud_update(scope: str = "full_non_provisioning") -> dict[str, object]:
    return {
        "request_id": "request-1",
        "gateway_id": "GW004",
        "site_id": "GW004",
        "cradlepoint_host": "10.1.8.131",
        "gateway_host": "192.168.1.200",
        "update_scope": scope,
        "target_ui_version": "0.2.3",
        "target_agent_version": "0.2.3",
        "target_ui_commit": "2adae3adeb339806330db0e481cba3179fff2ff1",
        "target_agent_commit": "f77c42b88c5307009c35a2d94e5afbbdb3e4db98",
    }


def test_cloud_full_rollout_materializes_exact_ui_before_starting_shared_engine(monkeypatch, tmp_path):
    claimed = claimed_cloud_update()
    artifact = SimpleNamespace(path=tmp_path / "edge-ui-2adae3a.tar.gz", sha256="c" * 64)
    captured: dict[str, object] = {}

    def fake_cloud(_url, _token, path, **_kwargs):
        return claimed if path.endswith("/claim") else {"ok": True}

    def fake_materialize(commit, *, token):
        assert commit == claimed["target_ui_commit"]
        assert token == "github-token"
        return artifact

    def fake_start(request):
        captured["request"] = request
        with dev.JOBS_LOCK:
            dev.JOBS["cloud-full-test"] = SimpleNamespace(status="complete", error="")
        return "cloud-full-test"

    monkeypatch.setattr(dev, "cloud_json_request", fake_cloud)
    monkeypatch.setattr(dev, "materialize_ui_artifact", fake_materialize)
    monkeypatch.setattr(dev, "start_job", fake_start)
    monkeypatch.setattr(dev, "health_gate_enabled", lambda: False)
    try:
        assert dev.run_queued_gateway_update({"request_id": "request-1"}, cloud_queue_defaults()) == "completed"
    finally:
        with dev.JOBS_LOCK:
            dev.JOBS.pop("cloud-full-test", None)

    request = captured["request"]
    assert request.selected_phases == dev.UPDATE_AGENT_PHASES
    assert request.edge_ui_commit == claimed["target_ui_commit"]
    assert request.ui_artifact_path == str(artifact.path)
    assert request.ui_artifact_sha256 == artifact.sha256
    assert request.edge_agent_commit == request.git_ref == claimed["target_agent_commit"]
    assert request.expected_agent_version == "0.2.3"


@pytest.mark.parametrize("failure", ("missing-commit", "materialization-failed"))
def test_cloud_full_rollout_fails_safely_without_valid_ui_artifact(monkeypatch, failure):
    claimed = claimed_cloud_update()
    completions: list[dict[str, object]] = []
    if failure == "missing-commit":
        claimed.pop("target_ui_commit")

    def fake_cloud(_url, _token, path, **kwargs):
        if path.endswith("/claim"):
            return claimed
        completions.append(kwargs["body"])
        return {"ok": True}

    def fake_materialize(*_args, **_kwargs):
        if failure == "missing-commit":
            pytest.fail("missing commit must fail before materialization")
        raise dev.UIArtifactError("artifact checkout failed")

    monkeypatch.setattr(dev, "cloud_json_request", fake_cloud)
    monkeypatch.setattr(dev, "materialize_ui_artifact", fake_materialize)
    monkeypatch.setattr(dev, "start_job", lambda _request: pytest.fail("gateway execution must not start"))

    assert dev.run_queued_gateway_update({"request_id": "request-1"}, cloud_queue_defaults()) == "failed"
    assert completions[-1]["status"] == "failed"
    assert "fallback" in str(completions[-1]["error_message"]) or "artifact checkout failed" in str(completions[-1]["error_message"])


def test_cloud_agent_only_rollout_does_not_require_or_materialize_ui(monkeypatch):
    claimed = claimed_cloud_update("agent")
    claimed.pop("target_ui_commit")
    claimed.pop("target_ui_version")
    captured: dict[str, object] = {}

    def fake_cloud(_url, _token, path, **_kwargs):
        return claimed if path.endswith("/claim") else {"ok": True}

    def fake_start(request):
        captured["request"] = request
        with dev.JOBS_LOCK:
            dev.JOBS["cloud-agent-test"] = SimpleNamespace(status="complete", error="")
        return "cloud-agent-test"

    monkeypatch.setattr(dev, "cloud_json_request", fake_cloud)
    monkeypatch.setattr(dev, "materialize_ui_artifact", lambda *_args, **_kwargs: pytest.fail("agent-only must not materialize UI"))
    monkeypatch.setattr(dev, "start_job", fake_start)
    monkeypatch.setattr(dev, "health_gate_enabled", lambda: False)
    try:
        assert dev.run_queued_gateway_update({"request_id": "request-1"}, cloud_queue_defaults()) == "completed"
    finally:
        with dev.JOBS_LOCK:
            dev.JOBS.pop("cloud-agent-test", None)

    request = captured["request"]
    assert request.selected_phases == dev.TARGETED_AGENT_ONLY_PHASES
    assert request.ui_artifact_path == ""
    assert request.ui_artifact_sha256 == ""
    assert request.edge_agent_commit == claimed["target_agent_commit"]
    assert request.expected_agent_version == "0.2.3"


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


TREND_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE trend_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    interval_sec INTEGER NOT NULL, enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE trend_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
    device_profile_id TEXT NOT NULL, device_instance INTEGER NOT NULL,
    object_type TEXT NOT NULL, object_instance INTEGER NOT NULL,
    object_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    UNIQUE(group_id, device_instance, object_type, object_instance),
    FOREIGN KEY(group_id) REFERENCES trend_groups(id) ON DELETE CASCADE
);
CREATE TABLE trend_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
    started_at TEXT NOT NULL, completed_at TEXT, requested_count INTEGER NOT NULL DEFAULT 0,
    returned_count INTEGER NOT NULL DEFAULT 0, deferred_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER, cpu_load_pct REAL, memory_used_pct REAL,
    network_rx_bytes INTEGER, network_tx_bytes INTEGER, error_text TEXT,
    FOREIGN KEY(group_id) REFERENCES trend_groups(id) ON DELETE CASCADE
);
CREATE TABLE trend_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trend_point_id INTEGER NOT NULL,
    sampled_at TEXT NOT NULL, value_text TEXT, status TEXT NOT NULL,
    read_source TEXT, error_text TEXT,
    FOREIGN KEY(trend_point_id) REFERENCES trend_points(id) ON DELETE CASCADE
);
CREATE TABLE trend_upload_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    trend_sample_id INTEGER NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, uploaded_at TEXT,
    FOREIGN KEY(trend_sample_id) REFERENCES trend_samples(id) ON DELETE CASCADE
);
CREATE TABLE trend_sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE trend_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
    name TEXT NOT NULL, settings_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(group_id, name),
    FOREIGN KEY(group_id) REFERENCES trend_groups(id) ON DELETE CASCADE
);
"""


def create_trend_database(
    path: Path,
    *,
    group_name: str = "Current trend",
    interval: int = 60,
    enabled: int = 1,
    sample_value: str = "72.5",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(TREND_SCHEMA)
        connection.execute("INSERT INTO trend_groups VALUES (10, ?, ?, ?, 'created', 'updated')", (group_name, interval, enabled))
        connection.execute("INSERT INTO trend_points VALUES (20, 10, 'profile-1', 1234, 'analog-input', 7, 'Space Temp', 'created')")
        connection.execute("INSERT INTO trend_views VALUES (30, 10, 'Primary', '{\"range\":\"24h\"}', 'created', 'updated')")
        connection.execute("INSERT INTO trend_runs VALUES (40, 10, 'run-start', 'run-end', 1, 1, 0, 20, 2.5, 30.0, 100, 50, NULL)")
        connection.execute("INSERT INTO trend_samples VALUES (50, 20, 'sample-time', ?, 'ok', 'rpm-bulk', NULL)", (sample_value,))
        connection.execute("INSERT INTO trend_upload_outbox VALUES (60, 'event-1', 50, 'pending', 2, NULL, 'retry', 'created', 'updated', NULL)")
        connection.execute("INSERT INTO trend_sync_state VALUES ('cursor', 'current-cursor', 'updated')")
        connection.execute("PRAGMA user_version=2")


def table_rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return list(connection.execute(f'SELECT * FROM "{table}" ORDER BY 1'))


def run_full_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tar_script: str, *, timeout_seconds: int = 5) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "swadmin"
    (root / "edge-bacnet-ui-v2" / "data").mkdir(parents=True)
    ui = root / "edge-bacnet-ui-v2"
    (ui / ".env").write_text("GATEWAY_ID=GW006\n", encoding="utf-8")
    (ui / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (ui / "data" / "devices").mkdir()
    (ui / "data" / "devices" / "1234.json").write_text('{"points":[7]}', encoding="utf-8")
    trend_db = ui / "data" / "edge-trends.db"
    create_trend_database(trend_db)
    (ui / "data" / "edge-trends.db.pre-2-history.bak").write_bytes(b"old history backup")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tar = fake_bin / "tar"
    tar.write_text("#!/bin/sh\n" + textwrap.dedent(tar_script), encoding="utf-8")
    tar.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    live_connection = sqlite3.connect(trend_db)
    try:
        live_connection.execute("PRAGMA journal_mode=WAL")
        live_connection.execute("PRAGMA wal_autocheckpoint=0")
        live_connection.execute("UPDATE trend_sync_state SET updated_at='backup-running' WHERE key='cursor'")
        live_connection.commit()
        assert trend_db.with_name("edge-trends.db-wal").exists()
        assert trend_db.with_name("edge-trends.db-shm").exists()
        return subprocess.run(
            ["/bin/sh", "-c", dev.full_backup_command(str(root), timeout_seconds=timeout_seconds, heartbeat_seconds=1)],
            text=True,
            capture_output=True,
            env=os.environ.copy(),
        )
    finally:
        live_connection.close()


REAL_TAR = 'exec /usr/bin/tar "$@"\n'


def test_full_backup_accepts_a_clean_tar_and_valid_archive(tmp_path, monkeypatch):
    result = run_full_backup(tmp_path, monkeypatch, REAL_TAR)
    assert result.returncode == 0
    assert "BACKUP_TAR_RESULT=clean" in result.stdout
    assert "BACKUP_ARCHIVE_VALID=Passed" in result.stdout
    assert "TREND_HISTORY_BACKUP=excluded_by_policy" in result.stdout
    assert "TREND_CONFIG_GROUPS=1" in result.stdout
    assert "TREND_CONFIG_POINTS=1" in result.stdout
    assert "TREND_CONFIG_VIEWS=1" in result.stdout

    archive_path = next((tmp_path / "swadmin").glob("edge-bacnet-ui-v2.backup.*.tar.gz"))
    with tarfile.open(archive_path, "r:gz") as archive:
        names = set(archive.getnames())
        assert "edge-bacnet-ui-v2/.env" in names
        assert "edge-bacnet-ui-v2/start.sh" in names
        assert "edge-bacnet-ui-v2/data/devices/1234.json" in names
        assert f"edge-bacnet-ui-v2/data/{trend_config_backup.SNAPSHOT_FILENAME}" in names
        assert not any("edge-trends.db" in name for name in names)
        archive.extract(
            f"edge-bacnet-ui-v2/data/{trend_config_backup.SNAPSHOT_FILENAME}",
            path=tmp_path / "inspect",
            filter="data",
        )
    snapshot = tmp_path / "inspect" / "edge-bacnet-ui-v2" / "data" / trend_config_backup.SNAPSHOT_FILENAME
    with sqlite3.connect(snapshot) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == set(trend_config_backup.CONFIG_TABLES)
        assert connection.execute("SELECT id,name,interval_sec,enabled FROM trend_groups").fetchone() == (10, "Current trend", 60, 1)
        assert connection.execute("SELECT id,group_id,device_instance,object_instance FROM trend_points").fetchone() == (20, 10, 1234, 7)
        assert connection.execute("SELECT id,group_id,name FROM trend_views").fetchone() == (30, 10, "Primary")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not (tmp_path / "swadmin" / "edge-bacnet-ui-v2" / "data" / trend_config_backup.SNAPSHOT_FILENAME).exists()
    assert "systemctl" not in dev.full_backup_command(str(tmp_path / "swadmin"))


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


def history_state(path: Path) -> dict[str, list[tuple]]:
    return {table: table_rows(path, table) for table in trend_config_backup.HISTORY_TABLES}


def test_new_backup_restore_replaces_only_configuration_and_preserves_newer_history(tmp_path, monkeypatch):
    backup_result = run_full_backup(tmp_path, monkeypatch, REAL_TAR)
    assert backup_result.returncode == 0
    root = tmp_path / "swadmin"
    archive = next(root.glob("edge-bacnet-ui-v2.backup.*.tar.gz"))
    live_db = root / "edge-bacnet-ui-v2" / "data" / "edge-trends.db"
    with sqlite3.connect(live_db) as connection:
        connection.execute("UPDATE trend_groups SET name='Newer config', interval_sec=900, enabled=0 WHERE id=10")
        connection.execute("UPDATE trend_samples SET value_text='newer-history' WHERE id=50")
        connection.execute("UPDATE trend_runs SET error_text='newer-run' WHERE id=40")
        connection.execute("UPDATE trend_upload_outbox SET last_error='newer-outbox' WHERE id=60")
        connection.execute("UPDATE trend_sync_state SET value='newer-cursor' WHERE key='cursor'")
    history_before = history_state(live_db)

    result = subprocess.run(
        ["/bin/sh", "-c", dev.full_restore_command(archive.name, str(root), str(tmp_path / "restore"))],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    assert "TREND_CONFIG_RESTORE_SOURCE=configuration_snapshot" in result.stdout
    assert "TREND_HISTORY_RESTORE=preserved_current" in result.stdout
    assert table_rows(live_db, "trend_groups")[0][1:4] == ("Current trend", 60, 1)
    assert table_rows(live_db, "trend_points")[0][0:2] == (20, 10)
    assert table_rows(live_db, "trend_views")[0][0:3] == (30, 10, "Primary")
    assert history_state(live_db) == history_before
    assert not (tmp_path / "restore").exists()


def test_legacy_backup_database_supplies_only_config_and_never_replaces_history(tmp_path):
    root = tmp_path / "swadmin"
    live_ui = root / "edge-bacnet-ui-v2"
    live_db = live_ui / "data" / "edge-trends.db"
    create_trend_database(live_db, group_name="Current config", interval=60, enabled=1, sample_value="current-history")
    (live_ui / ".env").write_text("CURRENT=yes\n", encoding="utf-8")
    history_before = history_state(live_db)

    archived_ui = tmp_path / "archived" / "edge-bacnet-ui-v2"
    legacy_db = archived_ui / "data" / "edge-trends.db"
    create_trend_database(legacy_db, group_name="Backup config", interval=300, enabled=0, sample_value="old-history")
    (archived_ui / ".env").write_text("BACKUP=yes\n", encoding="utf-8")
    legacy_connection = sqlite3.connect(legacy_db)
    try:
        legacy_connection.execute("PRAGMA journal_mode=WAL")
        legacy_connection.execute("PRAGMA wal_autocheckpoint=0")
        legacy_connection.execute("UPDATE trend_sync_state SET updated_at='archived-wal' WHERE key='cursor'")
        legacy_connection.commit()
        assert legacy_db.with_name("edge-trends.db-wal").exists()
        archive = root / "edge-bacnet-ui-v2.backup.20260830-010203.tar.gz"
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz") as output:
            output.add(archived_ui, arcname="edge-bacnet-ui-v2")
    finally:
        legacy_connection.close()

    result = subprocess.run(
        ["/bin/sh", "-c", dev.full_restore_command(archive.name, str(root), str(tmp_path / "legacy-restore"))],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    assert "TREND_CONFIG_RESTORE_SOURCE=legacy_database_configuration_only" in result.stdout
    assert "LEGACY_TREND_HISTORY_RESTORE=ignored_by_policy" in result.stdout
    assert table_rows(live_db, "trend_groups")[0][1:4] == ("Backup config", 300, 0)
    assert history_state(live_db) == history_before
    assert (live_ui / ".env").read_text(encoding="utf-8") == "BACKUP=yes\n"
    assert not (live_ui / "data" / trend_config_backup.SNAPSHOT_FILENAME).exists()


def test_incompatible_config_restore_rolls_back_without_history_or_config_mutation(tmp_path):
    live_db = tmp_path / "live" / "edge-trends.db"
    backup_db = tmp_path / "backup" / "edge-trends.db"
    create_trend_database(live_db, group_name="Current", sample_value="current-history")
    create_trend_database(backup_db, group_name="Incomplete", sample_value="old-history")
    with sqlite3.connect(backup_db) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM trend_upload_outbox")
        connection.execute("DELETE FROM trend_samples")
        connection.execute("DELETE FROM trend_points")
    before = {table: table_rows(live_db, table) for table in (*trend_config_backup.CONFIG_TABLES, *trend_config_backup.HISTORY_TABLES)}

    with pytest.raises(RuntimeError, match="omits trend point IDs"):
        trend_config_backup.restore_trend_config(backup_db, live_db)

    after = {table: table_rows(live_db, table) for table in (*trend_config_backup.CONFIG_TABLES, *trend_config_backup.HISTORY_TABLES)}
    assert after == before


def test_schema_mismatch_restore_fails_without_partial_mutation(tmp_path):
    live_db = tmp_path / "live" / "edge-trends.db"
    backup_db = tmp_path / "backup" / "edge-trends.db"
    create_trend_database(live_db, group_name="Current", sample_value="current-history")
    create_trend_database(backup_db, group_name="Backup", sample_value="old-history")
    with sqlite3.connect(backup_db) as connection:
        connection.execute("ALTER TABLE trend_views ADD COLUMN incompatible TEXT")
    before = {table: table_rows(live_db, table) for table in (*trend_config_backup.CONFIG_TABLES, *trend_config_backup.HISTORY_TABLES)}

    with pytest.raises(RuntimeError, match="schema mismatch"):
        trend_config_backup.restore_trend_config(backup_db, live_db)

    after = {table: table_rows(live_db, table) for table in (*trend_config_backup.CONFIG_TABLES, *trend_config_backup.HISTORY_TABLES)}
    assert after == before


def test_rollback_commands_use_safe_overlay_and_never_move_the_live_ui_directory():
    commands = dev.rollback_commands("edge-bacnet-ui-v2.backup.20260830-010203.tar.gz")
    text = "\n".join(command for _label, command, _sudo in commands)
    assert "move failed UI folder" not in {label for label, _command, _sudo in commands}
    assert " mv edge-bacnet-ui-v2 " not in text
    assert "TREND_CONFIG_RESTORE_SOURCE" in text
    assert "find \"$restore_root/edge-bacnet-ui-v2/data\" -maxdepth 1 -name 'edge-trends.db*' -delete" in text
    assert "cp -a" in text

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
    pilot = "f77c42b88c5307009c35a2d94e5afbbdb3e4db98"
    resolved_ui = type("Resolved", (), {"full_sha": "2adae3adeb339806330db0e481cba3179fff2ff1"})()
    resolved_agent = type("Resolved", (), {"full_sha": pilot})()
    monkeypatch.setenv("IOT_EDGE_DEV_AGENT_COMMIT", pilot)
    monkeypatch.setattr(dev, "resolve_requested_commits", lambda _fields: (resolved_ui, resolved_agent))
    monkeypatch.setattr(dev, "read_agent_version_from_source", lambda commit, **_kwargs: "0.2.3" if commit == pilot else "wrong")
    request = dev.parse_upgrade_request(agent_only_request_body())
    assert request.edge_agent_commit == pilot
    assert request.git_ref == pilot
    assert request.expected_agent_version == "0.2.3"
    assert request.agent_source == "Development override"
    repo_text = "\n".join(command for _label, command, _sudo in dev.repo_commands(request))
    final_text = "\n".join(command for _label, command, _sudo in dev.final_commands(request, pre_restart_timestamp="100"))
    assert pilot in repo_text and pilot in final_text
    manifest_agent = dev.load_release_definition().agent_source_commit
    assert manifest_agent != pilot
    assert manifest_agent not in repo_text
    assert manifest_agent not in final_text
    assert "--network-traffic" in final_text
    assert "tunnel_request_timeout_sec" not in final_text
    assert 'test "$trend" != true' not in final_text
    assert "echo LOCAL_EDGE_TRENDS_ENABLED=$trend" in final_text


def test_same_version_agent_commit_update_always_checks_out_the_exact_target():
    previous = "f77c42b88c5307009c35a2d94e5afbbdb3e4db98"
    target = "d9232758b93fc9954a64235be918724df08238de"
    request = dev.UpgradeRequest(
        gateway_id="GW017", site_id="GW017", cloud_url="https://example.test", admin_api_token="x",
        cradlepoint_host="x", cradlepoint_user="x", cradlepoint_password="x", gateway_host="x",
        gateway_user="x", gateway_password="x", git_ref=target, remote_repo="/repo",
        ui_source_folder="x", ui_username="x", ui_password="x", edge_agent_commit=target,
        expected_agent_version="0.2.3", agent_source="Development override", development_agent_override=True,
    )

    commands = "\n".join(command for _label, command, _sudo in dev.repo_commands(request))
    assert f"git checkout --detach {target}" in commands
    assert previous not in commands
    assert "expected_agent_version" not in commands


def test_agent_cadence_migration_is_narrow_and_preserves_gateway_config(tmp_path):
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "gateway_id: GW017\nsite_id: pilot\ncloud_url: https://cloud.example\n"
        "heartbeat_interval_sec: 30\nlocal_edge_trends_enabled: true\n"
        "custom_gateway_setting: retain-me\nbacnet:\n  default_port: 47809\n",
        encoding="utf-8",
    )

    namespace: dict[str, object] = {}
    exec(dev.agent_cadence_migration_script(str(config_path)), namespace)
    migrated = config_path.read_text(encoding="utf-8")

    assert "heartbeat_interval_sec: 7200" in migrated
    assert "command_wait_timeout_sec: 600" in migrated
    assert "command_failure_backoff_initial_sec: 5" in migrated
    assert "command_failure_backoff_max_sec: 300" in migrated
    assert "gateway_id: GW017" in migrated
    assert "site_id: pilot" in migrated
    assert "cloud_url: https://cloud.example" in migrated
    assert "local_edge_trends_enabled: true" in migrated
    assert "custom_gateway_setting: retain-me" in migrated
    assert "  default_port: 47809" in migrated


def test_agent_cadence_migration_preserves_nonlegacy_custom_values(tmp_path):
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        "heartbeat_interval_sec: 1800\ncommand_wait_timeout_sec: 420\n"
        "command_failure_backoff_initial_sec: 9\ncommand_failure_backoff_max_sec: 480\n",
        encoding="utf-8",
    )

    exec(dev.agent_cadence_migration_script(str(config_path)), {})

    assert config_path.read_text(encoding="utf-8") == (
        "heartbeat_interval_sec: 1800\ncommand_wait_timeout_sec: 420\n"
        "command_failure_backoff_initial_sec: 9\ncommand_failure_backoff_max_sec: 480\n"
    )


def test_fresh_agent_config_uses_the_new_platform_cadence_defaults():
    request = gw006_override_request()
    config = dev.agent_config_text(request)
    assert "heartbeat_interval_sec: 7200" in config
    assert "command_wait_timeout_sec: 600" in config
    assert "command_failure_backoff_initial_sec: 5" in config
    assert "command_failure_backoff_max_sec: 300" in config


def test_candidate_version_is_read_from_immutable_source():
    class SourceResponse:
        def read(self): return b'__version__ = "0.2.1"\n'
        def __enter__(self): return self
        def __exit__(self, *_args): return False
    assert dev.read_agent_version_from_source("8fecbe6b1d6834626dbc5d3cecdd2401b10aec58", opener=lambda *_args, **_kwargs: SourceResponse()) == "0.2.1"


def gw006_override_request() -> dev.UpgradeRequest:
    target = "f77c42b88c5307009c35a2d94e5afbbdb3e4db98"
    return dev.UpgradeRequest(
        gateway_id="GW006", site_id="GW006", cloud_url="https://example.test", admin_api_token="x",
        cradlepoint_host="x", cradlepoint_user="x", cradlepoint_password="x", gateway_host="x",
        gateway_user="x", gateway_password="x", git_ref=target, remote_repo="/repo",
        ui_source_folder="x", ui_username="x", ui_password="x", edge_agent_commit=target,
        expected_agent_version="0.2.3", agent_source="Development override", development_agent_override=True,
    )


def gw006_runtime_output(request: dev.UpgradeRequest) -> str:
    return "\n".join((
        f"AGENT_RELEASE_COMMIT={request.edge_agent_commit}", "AGENT_PACKAGE_VERSION=0.2.3",
        "AGENT_MODULE_VERSION=0.2.3", "AGENT_SERVICE_STATE=active", "AGENT_SERVICE_START=200",
        "AGENT_NETWORK_TRAFFIC_CLI=Passed", "LOCAL_EDGE_TRENDS_ENABLED=true",
    ))


def test_final_verification_passes_for_the_resolved_gw006_development_override():
    request = gw006_override_request()
    assert dev.load_release_definition().agent_source_commit != request.edge_agent_commit
    dev.validate_agent_runtime_output(gw006_runtime_output(request), request, "100")


@pytest.mark.parametrize(
    "bad_output",
    (
        lambda good, request: good.replace(request.edge_agent_commit, "4" * 40),
        lambda good, _request: good.replace("AGENT_PACKAGE_VERSION=0.2.3", "AGENT_PACKAGE_VERSION=0.2.2"),
        lambda good, _request: good.replace("AGENT_MODULE_VERSION=0.2.3", "AGENT_MODULE_VERSION=0.2.2"),
        lambda good, _request: good.replace("AGENT_SERVICE_STATE=active", "AGENT_SERVICE_STATE=inactive"),
        lambda good, _request: good.replace("AGENT_SERVICE_START=200", "AGENT_SERVICE_START=100"),
        lambda good, _request: good.replace("AGENT_NETWORK_TRAFFIC_CLI=Passed", "AGENT_NETWORK_TRAFFIC_CLI="),
    ),
    ids=("wrong-head", "wrong-package-version", "wrong-module-version", "inactive-service", "service-not-restarted", "missing-network-traffic-cli"),
)
def test_actual_agent_runtime_validation_rejects_wrong_install(bad_output):
    request = gw006_override_request()
    good = gw006_runtime_output(request)
    with pytest.raises(RuntimeError, match="Actual Agent runtime validation failed"):
        dev.validate_agent_runtime_output(bad_output(good, request), request, "100")


def test_final_verification_retains_bacnet_and_ui_validation():
    commands = {label: command for label, command, _sudo in dev.final_commands(gw006_override_request())}
    bacnet = commands["verify BACnet config preservation"]
    assert 'test "$post" = "$pre"' in bacnet
    assert "BACNET_CONFIG_PRESERVATION=Passed" in bacnet
    assert commands["edge UI active"] == "systemctl is-active edge-bacnet-ui.service"
    assert commands["local UI HTTP auth check"] == "curl -I http://127.0.0.1:5000/"


def test_ui_restart_validation_still_rejects_failed_or_unauthenticated_ui():
    request = dev.UpgradeRequest(
        gateway_id="GW006", site_id="GW006", cloud_url="https://example.test", admin_api_token="x",
        cradlepoint_host="x", cradlepoint_user="x", cradlepoint_password="x", gateway_host="x",
        gateway_user="x", gateway_password="x", git_ref="8" * 40, remote_repo="/repo", dry_run=False,
        ui_source_folder="x", ui_username="x", ui_password="x",
    )
    runner = type("Runner", (), {"request": request, "run_commands": lambda *_args, **_kwargs: "journal"})()
    dev.LegacyUpgradeRunner.validate_ui_restart(runner, "active\nHTTP/1.1 302 Found\nLocation: /login")
    with pytest.raises(RuntimeError, match="did not become active"):
        dev.LegacyUpgradeRunner.validate_ui_restart(runner, "failed\nHTTP/1.1 302 Found\nLocation: /login")
    with pytest.raises(RuntimeError, match="Auth verification failed"):
        dev.LegacyUpgradeRunner.validate_ui_restart(runner, "active\nHTTP/1.1 200 OK")
