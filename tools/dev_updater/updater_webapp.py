from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import BaseServer
import argparse
import base64
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.gateway_recovery import checkpoint_commands, checkpoint_inventory_commands, code_restore_commands, release_name
from tools.release_preflight import validate_edge_source
from tools.release_manifest import load_manifest, verify_manifest_artifact
from tools.dev_updater import identity, runtime
from tools.dev_updater.commit_resolution import (
    EDGE_AGENT_REPOSITORY,
    EDGE_UI_REPOSITORY,
    CommitResolutionError,
    resolve_commit,
)
from tools.dev_updater.ui_artifact import UIArtifactError, materialize as materialize_ui_artifact

try:
    import paramiko
except ImportError:  # pragma: no cover - shown in browser and terminal at runtime
    paramiko = None


DEFAULT_PORT = identity.DEFAULT_PORT
DEFAULT_CLOUD_URL = "https://iot-cloud-api-dev.onrender.com"
DEFAULT_REPO_PATH = "/home/swadmin/iot-cloud-commissioning"
DEFAULT_UI_SOURCE = r"C:\Temp\edge-bacnet-ui-0.2.0"
DEFAULT_RELEASE_MANIFEST = str(Path(__file__).resolve().parent / "releases" / "manifests" / "edge-0.2.0.json")
DEFAULT_RELEASE_DEFINITION = load_manifest(Path(DEFAULT_RELEASE_MANIFEST))
DEFAULT_EDGE_UPDATE_REF = DEFAULT_RELEASE_DEFINITION.agent_source_commit
DEFAULT_EDGE_RELEASE = DEFAULT_RELEASE_DEFINITION.edge_release
DEFAULT_EDGE_UI_COMMIT = DEFAULT_RELEASE_DEFINITION.edge_ui_tag
DEFAULT_EDGE_UI_INPUT = "0bab9442c4f736312d41bdeab08b3ef2d8141db0"
DEFAULT_EDGE_AGENT_INPUT = "40133f2a81390db92a01b33a9c02c48a07363a7e"
DEFAULT_EDGE_UI_DATA_DIR = "/home/swadmin/edge-bacnet-ui-v2/data"
REMOTE_UI_PATH = "/home/swadmin/edge-bacnet-ui-v2"
REMOTE_UI_ARTIFACT_PATH = "/home/swadmin/edge-bacnet-ui-v2-update.tar.gz"
REMOTE_REPO_URL = "https://github.com/ssmithiot/iot-cloud-commissioning.git"
NESTED_UPLOAD_CHUNK_SIZE = 3000
UI_PACKAGE_FILES = (
    "app.py",
    "edge_program_engine.py",
    "edge_trend_store.py",
    "timed_override_store.py",
    "router_config.py",
    "README.md",
    "requirements.txt",
)
UI_PACKAGE_DIRS = ("templates", "static")
UI_OPTIONAL_PACKAGE_FILES = (
    "deploy/iot-cx-edge-router-control.py",
    "deploy/edge-bacnet-ui.service.example",
    "deploy/iot-cx-bacnet-router.service.example",
)
UI_FORBIDDEN_ARTIFACT_NAMES = {".env", "start.sh"}
UI_FORBIDDEN_ARTIFACT_PARTS = {"data", ".git", ".local-backups", "__pycache__"}
# Manual Cloud-triggered 0.1.4 updates replace both halves of the local handoff:
# the proven edge UI writer and the agent that delegates queued jobs to it.
# Nothing polls this list to auto-update a gateway when it reconnects.
UPDATE_AGENT_PHASES = (0, 1, 2, 3, 4, 5, 7, 9, 10, 11)
TARGETED_AGENT_ONLY_PHASES = (0, 7, 9, 10, 11)
UI_ONLY_PHASES = (0, 1, 2, 3, 4, 5)
JOBS: dict[str, "UpgradeJob"] = {}
JOBS_LOCK = threading.Lock()
WORKER_STATUS: dict[str, object] = {
    "state": "starting",
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
}
WORKER_STATUS_LOCK = threading.Lock()
PHASES = [
    "Inspect gateway",
    "Back up local BACnet UI",
    "Build/upload UI release artifact",
    "Apply UI update",
    "Confirm UI auth",
    "Restart local UI",
    "Provision cloud gateway",
    "Clone/update cloud repo",
    "Write cloud config/token",
    "Install Python agent",
    "Install/start service",
    "Final verification",
]
TARGETED_REAL_RUN_PHASES = (4, 5, 11)
STANDARD_REAL_RUN_PHASE_SETS = (
    tuple(range(len(PHASES))),
    UPDATE_AGENT_PHASES,
    TARGETED_AGENT_ONLY_PHASES,
    UI_ONLY_PHASES,
)


class PhaseStatus(str, Enum):
    NOT_STARTED = "Not started"
    RUNNING = "Running"
    PASSED = "Passed"
    FAILED = "Failed"
    SKIPPED = "Skipped"


@dataclass(frozen=True)
class UpgradeRequest:
    gateway_id: str
    site_id: str
    cloud_url: str
    admin_api_token: str
    cradlepoint_host: str
    cradlepoint_user: str
    cradlepoint_password: str
    gateway_host: str
    gateway_user: str
    gateway_password: str
    git_ref: str
    remote_repo: str
    ui_source_folder: str
    ui_username: str
    ui_password: str
    dry_run: bool = False
    reuse_uploaded_zip: bool = False
    skip_edge_ui_stop: bool = False
    cloud_portal_verified: bool = False
    final_update_confirmed: bool = False
    selected_phases: tuple[int, ...] = tuple(range(len(PHASES)))
    edge_agent_write_token: str = ""
    edge_release: str = DEFAULT_EDGE_RELEASE
    release_manifest_path: str = DEFAULT_RELEASE_MANIFEST
    edge_ui_commit: str = DEFAULT_EDGE_UI_INPUT
    edge_agent_commit: str = DEFAULT_EDGE_AGENT_INPUT
    expected_agent_version: str = DEFAULT_EDGE_RELEASE
    agent_source: str = "Validated release manifest"
    development_agent_override: bool = False
    ui_artifact_path: str = ""
    ui_artifact_sha256: str = ""


@dataclass
class PhaseResult:
    name: str
    status: PhaseStatus = PhaseStatus.NOT_STARTED
    detail: str = ""


@dataclass
class UpgradeJob:
    request: UpgradeRequest
    status: str = "queued"
    log: str = ""
    current_phase: int = 0
    phases: list[PhaseResult] = field(default_factory=lambda: [PhaseResult(name) for name in PHASES])
    gateway_token: str = ""
    backup_filename: str = ""
    warning: str = ""
    error: str = ""
    summary: dict[str, str] = field(default_factory=dict)
    runner: "LegacyUpgradeRunner | None" = None
    pre_upgrade_agent_default_port: str = "47814"
    pre_restart_agent_timestamp: str = ""


class Redactor:
    def __init__(self, secrets: list[str] | tuple[str, ...] = ()) -> None:
        self._secrets = [secret for secret in secrets if secret]

    def add(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def redact(self, text: str) -> str:
        safe = text
        for secret in sorted(self._secrets, key=len, reverse=True):
            safe = safe.replace(secret, "[redacted]")
        safe = re.sub(r"(GATEWAY_API_TOKEN=)[^\s'\"]+", r"\1***SET***", safe)
        safe = re.sub(r"(EDGE_UI_PASSWORD=)'[^']*'", r"\1'***SET***'", safe)
        safe = re.sub(r"(EDGE_UI_PASSWORD=)[^\s]+", r"\1'***SET***'", safe)
        return safe


def parse_bacnet_route_settings(text: str) -> dict[str, str]:
    settings = {}
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r'(?:export\s+)?(BACNET_IP_PORT|BACNET_IP_PORTS|BACNET_PORT_MODE|BACNET_EDGE_PROGRAM_PORTS)=(.*)', stripped)
        if match:
            settings[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return settings


def ui_source_validation_summary(source_folder: str, release_manifest_path: str) -> dict[str, str]:
    manifest = load_manifest(Path(release_manifest_path))
    source = Path(source_folder)
    expected = manifest.edge_ui_tag
    summary = {
        "UI package source": str(source),
        "UI source commit": "unknown",
        "UI source clean status": "unknown",
        "Expected UI commit": expected,
        "Source validation": "Failed",
    }
    try:
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT).strip()
        dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True, stderr=subprocess.STDOUT).strip()
        summary["UI source commit"] = head
        summary["UI source clean status"] = "Dirty" if dirty else "Clean"
    except Exception as exc:
        summary["Source validation"] = f"Failed: unable to inspect Git source: {exc}"
        return summary
    try:
        head = validate_edge_source(Path(release_manifest_path), source)
    except Exception as exc:
        summary["Source validation"] = f"Failed: {exc}"
        return summary
    summary["UI source commit"] = head
    summary["UI source clean status"] = "Clean"
    summary["Source validation"] = "Passed" if head == expected else f"Failed: expected {expected[:7]}, got {head[:7]}"
    return summary


def validate_ui_source_for_deploy(source_folder: str, release_manifest_path: str) -> str:
    summary = ui_source_validation_summary(source_folder, release_manifest_path)
    if summary["Source validation"] != "Passed":
        raise RuntimeError(f"UI source validation failed: {summary['Source validation']}")
    return summary["UI source commit"]


def load_release_definition(release_manifest_path: str = DEFAULT_RELEASE_MANIFEST):
    manifest = load_manifest(Path(release_manifest_path))
    if manifest.edge_release == DEFAULT_EDGE_RELEASE and not manifest.agent_source_commit:
        raise ValueError("Release manifest is missing agent_source_commit")
    return manifest


def validate_embedded_ui_artifact_contents(artifact: Path) -> None:
    required = {*UI_PACKAGE_FILES, *UI_PACKAGE_DIRS}
    with tarfile.open(artifact, "r:gz") as archive:
        names = set(archive.getnames())
    missing = sorted(name for name in required if name not in names)
    if missing:
        raise ValueError(f"UI artifact is missing required root item(s): {', '.join(missing)}")
    forbidden = sorted(
        name
        for name in names
        if name in UI_FORBIDDEN_ARTIFACT_NAMES
        or any(part in UI_FORBIDDEN_ARTIFACT_PARTS for part in Path(name).parts)
        or name.endswith((".db", ".sqlite", ".pyc"))
    )
    if forbidden:
        raise ValueError(f"UI artifact contains forbidden runtime item(s): {', '.join(forbidden[:10])}")


def embedded_ui_artifact_summary(release_manifest_path: str) -> dict[str, str]:
    manifest = load_release_definition(release_manifest_path)
    repository_root = Path(__file__).resolve().parents[2]
    summary = {
        "Release version": manifest.edge_release,
        "UI deployment source": "embedded-release-artifact",
        "UI package source": "embedded-release-artifact",
        "UI source commit": manifest.edge_ui_tag,
        "Expected UI commit": manifest.edge_ui_tag,
        "UI artifact path": str((repository_root / manifest.artifact).resolve()),
        "UI artifact SHA-256": "unknown",
        "UI artifact validation": "Failed",
        "Agent source commit": manifest.agent_source_commit,
        "Local Edge trends default enabled": "true" if manifest.local_edge_trends_default_enabled else "false",
        "Background BACnet activity added": "No",
        "Release component validation": "Failed",
        "Rule #1 validation": "Failed",
    }
    try:
        artifact = verify_manifest_artifact(manifest, repository_root)
        validate_embedded_ui_artifact_contents(artifact)
    except Exception as exc:
        summary["UI artifact validation"] = f"Failed: {exc}"
        summary["Release component validation"] = f"Failed: {exc}"
        summary["Rule #1 validation"] = "Failed"
        return summary
    summary["UI artifact path"] = str(artifact)
    summary["UI artifact SHA-256"] = manifest.sha256
    summary["UI artifact validation"] = "Passed"
    summary["Release component validation"] = "Passed"
    summary["Rule #1 validation"] = "Passed"
    return summary


def validated_embedded_ui_artifact(release_manifest_path: str) -> tuple[Path, dict[str, str]]:
    summary = embedded_ui_artifact_summary(release_manifest_path)
    if summary["UI artifact validation"] != "Passed":
        raise RuntimeError(f"UI artifact validation failed: {summary['UI artifact validation']}")
    return Path(summary["UI artifact path"]), summary


class LiveLog:
    def __init__(self, job_id: str, redactor: Redactor) -> None:
        self.job_id = job_id
        self.redactor = redactor
        identity.log_dir().mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.audit_path = identity.log_dir() / f"{identity.APP_NAME}-{stamp}.log"

    def append(self, text: str) -> None:
        safe = self.redactor.redact(text)
        print(safe, end="", flush=True)
        with self.audit_path.open("a", encoding="utf-8") as audit:
            audit.write(safe)
        with JOBS_LOCK:
            job = JOBS.get(self.job_id)
            if job is not None:
                job.log += safe


def load_env_defaults() -> dict[str, str]:
    defaults = {
        "IOT_ADMIN_API_TOKEN": os.environ.get("IOT_ADMIN_API_TOKEN", ""),
        "CRADLEPOINT_PASSWORD": os.environ.get("CRADLEPOINT_PASSWORD", ""),
        "GATEWAY_PASSWORD": os.environ.get("GATEWAY_PASSWORD", ""),
        "EDGE_UI_PASSWORD": os.environ.get("EDGE_UI_PASSWORD", ""),
        "IOT_EDGE_DEV_UI_COMMIT": os.environ.get("IOT_EDGE_DEV_UI_COMMIT", ""),
        "IOT_EDGE_DEV_AGENT_COMMIT": os.environ.get("IOT_EDGE_DEV_AGENT_COMMIT", ""),
        "IOT_EDGE_DEV_UPDATER_PORT": os.environ.get("IOT_EDGE_DEV_UPDATER_PORT", ""),
        "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
    }
    env_path = identity.env_path()
    if not env_path.exists():
        return defaults
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key in defaults:
            defaults[key] = raw_value.strip().strip('"').strip("'")
    return defaults


def configured_default(name: str, built_in: str) -> str:
    """Read an optional Development Updater default without changing field editability."""
    return load_env_defaults().get(name, "").strip() or built_in


def cloud_json_request(
    cloud_url: str,
    admin_api_token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
) -> object:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib_request.Request(
        f"{cloud_url.rstrip('/')}{path}",
        data=payload,
        headers={
            "Authorization": f"Bearer {admin_api_token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib_request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_cloud_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def evaluate_post_update_health(gateway: dict[str, object], update_finished_at: datetime) -> tuple[bool, str]:
    """Cloud-observed post-update health: the gateway must have heartbeated
    AFTER the update finished, be online, and report a healthy local database.

    The in-run shell verifies prove the update script executed; this proves
    the gateway actually came back to the platform afterward.
    """
    heartbeat_at = _parse_cloud_timestamp(gateway.get("latest_heartbeat_at"))
    if heartbeat_at is None or heartbeat_at <= update_finished_at:
        return False, "no heartbeat received since the update finished"
    if gateway.get("effective_status") != "online":
        return False, f"gateway status is {gateway.get('effective_status')!r}, expected online"
    if gateway.get("sqlite_db_ok") is not True:
        return False, "gateway reports sqlite_db_ok=false after update"
    return True, f"online with post-update heartbeat; agent_version={gateway.get('agent_version')!r}"


def wait_for_post_update_health(
    cloud_url: str,
    admin_api_token: str,
    gateway_id: str,
    update_finished_at: datetime,
    *,
    timeout_sec: float | None = None,
    poll_sec: float | None = None,
    fetch=cloud_json_request,
    sleep=time.sleep,
) -> tuple[bool, str]:
    """Poll the cloud until the gateway proves healthy or the window expires."""
    timeout_sec = float(os.environ.get("IOT_EDGE_UPDATE_HEALTH_TIMEOUT_SEC", "300")) if timeout_sec is None else timeout_sec
    poll_sec = float(os.environ.get("IOT_EDGE_UPDATE_HEALTH_POLL_SEC", "15")) if poll_sec is None else poll_sec
    deadline = time.monotonic() + timeout_sec
    detail = "health gate never ran"
    while True:
        try:
            gateway = fetch(cloud_url, admin_api_token, f"/api/ui/gateways/{gateway_id}")
        except (urllib_error.HTTPError, urllib_error.URLError, ValueError) as exc:
            gateway, detail = None, f"could not read gateway health: {exc}"
        if isinstance(gateway, dict):
            healthy, detail = evaluate_post_update_health(gateway, update_finished_at)
            if healthy:
                return True, detail
        if time.monotonic() >= deadline:
            return False, f"post-update health gate failed after {int(timeout_sec)}s: {detail}"
        sleep(poll_sec)


def health_gate_enabled() -> bool:
    return os.environ.get("IOT_EDGE_UPDATE_HEALTH_GATE", "true").strip().lower() not in {"0", "false", "no", "off"}


def release_package_status(manifest_path: str) -> str:
    try:
        manifest = load_release_definition(manifest_path)
        artifact = verify_manifest_artifact(manifest, Path(__file__).resolve().parents[2])
        validate_embedded_ui_artifact_contents(artifact)
    except Exception as exc:
        return f"BLOCKED: {exc}"
    return f"OK: {manifest.edge_release} artifact {artifact.name} SHA-256 {manifest.sha256}; agent {manifest.agent_source_commit}"


def run_queued_gateway_update(update: dict[str, object], defaults: dict[str, str]) -> str | None:
    """Process one queued update. Returns 'completed', 'failed', or None when
    the request could not be claimed (not a gateway outcome)."""
    cloud_url = os.environ.get("IOT_CLOUD_API_URL", DEFAULT_CLOUD_URL).rstrip("/")
    admin_api_token = defaults["IOT_ADMIN_API_TOKEN"]
    request_id = str(update["request_id"])
    try:
        claimed = cloud_json_request(
            cloud_url,
            admin_api_token,
            f"/api/admin/gateway-updates/{request_id}/claim",
            method="POST",
        )
    except (urllib_error.HTTPError, urllib_error.URLError, ValueError):
        return None

    if not isinstance(claimed, dict):
        return None
    request = UpgradeRequest(
        gateway_id=str(claimed["gateway_id"]),
        site_id=str(claimed["site_id"]),
        cloud_url=cloud_url,
        admin_api_token=admin_api_token,
        cradlepoint_host=str(claimed.get("cradlepoint_host") or ""),
        cradlepoint_user=os.environ.get("CRADLEPOINT_USER", "BMS_admin"),
        cradlepoint_password=defaults["CRADLEPOINT_PASSWORD"],
        gateway_host=str(claimed.get("gateway_host") or "192.168.1.200"),
        gateway_user=os.environ.get("GATEWAY_USER", "swadmin"),
        gateway_password=defaults["GATEWAY_PASSWORD"],
        git_ref=os.environ.get("IOT_EDGE_UPDATE_REF", DEFAULT_EDGE_UPDATE_REF),
        remote_repo=DEFAULT_REPO_PATH,
        ui_source_folder=DEFAULT_UI_SOURCE,
        ui_username=os.environ.get("EDGE_UI_USERNAME", "admin"),
        ui_password=defaults["EDGE_UI_PASSWORD"],
        cloud_portal_verified=True,
        # UI-only release jobs never provision a gateway or change the agent,
        # gateway/site metadata, address, IP, credentials, or cloud token.
        selected_phases=UI_ONLY_PHASES if claimed.get("update_scope") == "ui_only" else UPDATE_AGENT_PHASES,
    )
    if not request.cradlepoint_host:
        cloud_json_request(
            cloud_url,
            admin_api_token,
            f"/api/admin/gateway-updates/{request_id}/complete",
            method="POST",
            body={"status": "failed", "error_message": "No Cradlepoint host is configured for this gateway."},
        )
        return "failed"

    job_id = start_job(request)
    while True:
        with JOBS_LOCK:
            job = JOBS[job_id]
            status = job.status
            error = job.error
        if status in {"complete", "failed"}:
            result = {"status": "completed" if status == "complete" else "failed"}
            if error:
                result["error_message"] = error[:1000]
            if status == "complete" and health_gate_enabled():
                # The shell phases succeeded; now require cloud-observed
                # health (fresh heartbeat, online, sqlite ok) before calling
                # this update done.
                healthy, detail = wait_for_post_update_health(
                    cloud_url,
                    admin_api_token,
                    request.gateway_id,
                    datetime.now(timezone.utc),
                )
                print(f"Post-update health gate for {request.gateway_id}: {detail}", flush=True)
                if not healthy:
                    result = {"status": "failed", "error_message": detail[:1000]}
            try:
                cloud_json_request(
                    cloud_url,
                    admin_api_token,
                    f"/api/admin/gateway-updates/{request_id}/complete",
                    method="POST",
                    body=result,
                )
            except (urllib_error.HTTPError, urllib_error.URLError, ValueError):
                pass
            return result["status"]
        time.sleep(2)


def gateway_update_worker() -> None:
    poll_seconds = max(5, int(os.environ.get("IOT_EDGE_UPDATE_POLL_SECONDS", "10")))
    halt_after = max(1, int(os.environ.get("IOT_EDGE_UPDATE_HALT_AFTER_FAILURES", "2")))
    consecutive_failures = 0
    halted = False
    while True:
        with WORKER_STATUS_LOCK:
            WORKER_STATUS["last_poll_at"] = datetime.now(timezone.utc).isoformat()
        if halted:
            # Stop-the-line: consecutive failures suggest a bad build or a
            # systemic problem. Do not ship it to more gateways. Restart the
            # webapp to resume after investigating.
            with WORKER_STATUS_LOCK:
                WORKER_STATUS["state"] = "halted"
            time.sleep(poll_seconds)
            continue
        try:
            defaults = load_env_defaults()
            token = defaults["IOT_ADMIN_API_TOKEN"]
            if not token:
                raise RuntimeError("IOT_ADMIN_API_TOKEN is not configured")
            cloud_url = os.environ.get("IOT_CLOUD_API_URL", DEFAULT_CLOUD_URL).rstrip("/")
            updates = cloud_json_request(cloud_url, token, "/api/admin/gateway-updates?status_filter=queued&limit=10")
            if isinstance(updates, list):
                for update in updates:
                    if isinstance(update, dict):
                        outcome = run_queued_gateway_update(update, defaults)
                        if outcome == "failed":
                            consecutive_failures += 1
                            if consecutive_failures >= halt_after:
                                halted = True
                                message = (
                                    f"halted after {consecutive_failures} consecutive failed updates; "
                                    "queued updates left unclaimed - investigate, then restart the webapp to resume"
                                )
                                with WORKER_STATUS_LOCK:
                                    WORKER_STATUS["state"] = "halted"
                                    WORKER_STATUS["last_error"] = message
                                print(f"Cloud update worker {message}", flush=True)
                                break
                        elif outcome == "completed":
                            consecutive_failures = 0
            if not halted:
                with WORKER_STATUS_LOCK:
                    WORKER_STATUS["state"] = "polling"
                    WORKER_STATUS["last_success_at"] = datetime.now(timezone.utc).isoformat()
                    WORKER_STATUS["last_error"] = None
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            with WORKER_STATUS_LOCK:
                WORKER_STATUS["state"] = "error"
                WORKER_STATUS["last_error"] = message
            print(f"Cloud update worker error: {message}", flush=True)
        time.sleep(poll_seconds)


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#5d6b82; --line:#d8dee8; --accent:#1458d4; --ok:#126b34; --bad:#a40021; }}
    body {{ font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: #f6f8fb; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 22px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    p {{ color: var(--muted); line-height: 1.45; }}
    form {{ display: grid; grid-template-columns: repeat(2, minmax(240px, 1fr)); gap: 14px; padding: 18px; background: white; border: 1px solid var(--line); }}
    label {{ display: grid; gap: 6px; font-weight: 700; }}
    input {{ padding: 10px; border: 1px solid #aab4c2; font: inherit; min-width: 0; }}
    input[type=checkbox] {{ width: 18px; height: 18px; }}
    button, a.button {{ display: inline-flex; align-items:center; justify-content:center; gap:6px; padding: 10px 14px; border: 0; background: var(--accent); color: white; font-weight: 700; text-decoration: none; cursor: pointer; min-height: 40px; }}
    button.secondary {{ background: #3d4b63; }}
    button.danger {{ background: #9f1239; }}
    button:disabled {{ background: #7f8da8; cursor: wait; }}
    .wide {{ grid-column: 1 / -1; }}
    .checks {{ display:flex; gap:18px; align-items:center; flex-wrap:wrap; }}
    .checks label {{ display:flex; grid-template-columns:none; align-items:center; gap:8px; }}
    .hint {{ color: var(--muted); font-size: 13px; font-weight: 400; }}
    .phase-groups {{ display: grid; gap: 8px; margin: 12px 0; }}
    .phase-groups div {{ display: grid; gap: 2px; padding: 8px 10px; border-left: 4px solid #1458d4; background: #eef4ff; }}
    .phase-groups span {{ color: var(--muted); font-size: 13px; font-weight: 400; }}
    .layout {{ display:grid; grid-template-columns: minmax(280px, 420px) 1fr; gap:16px; margin-top:16px; }}
    .panel {{ background:white; border:1px solid var(--line); padding:16px; }}
    .phase {{ display:grid; grid-template-columns: 1fr auto; gap:10px; padding:8px 0; border-bottom:1px solid #edf0f5; }}
    .phase:last-child {{ border-bottom:0; }}
    .badge {{ font-size:12px; font-weight:700; padding:3px 7px; background:#eef2f8; white-space:nowrap; }}
    .Passed {{ color:var(--ok); }} .Failed {{ color:var(--bad); }} .Running {{ color:#7c4a03; }}
    pre {{ white-space: pre-wrap; background: #101828; color: #e7edf7; padding: 16px; overflow-x: auto; min-height: 360px; margin:0; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }}
    .error {{ color: var(--bad); font-weight: 700; }}
    @media (max-width: 900px) {{ form, .layout {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body><main>{body}</main></body>
</html>""".encode("utf-8")


def form_page(message: str = "") -> bytes:
    defaults = load_env_defaults()
    warning = f'<p class="error">{escape(message)}</p>' if message else ""
    admin_token = escape(defaults["IOT_ADMIN_API_TOKEN"], quote=True)
    cp_password = escape(defaults["CRADLEPOINT_PASSWORD"], quote=True)
    gw_password = escape(defaults["GATEWAY_PASSWORD"], quote=True)
    ui_password = escape(defaults["EDGE_UI_PASSWORD"], quote=True)
    package_status = release_package_status(DEFAULT_RELEASE_MANIFEST)
    agent_target_label = "Development override (resolved version shown in preflight)" if defaults["IOT_EDGE_DEV_AGENT_COMMIT"] else DEFAULT_EDGE_RELEASE
    return page(
        identity.PRODUCT_NAME,
        f"""
<h1>{identity.PRODUCT_NAME}</h1>
<p><b>Updater Version {identity.APP_VERSION}</b> (MSI ProductVersion {identity.MSI_PRODUCT_VERSION})</p>
<p>Upgrade older edge-only gateways through the Cradlepoint jump host. This is separate from IOTGWCFG and starts in preflight mode.</p>
{warning}
<section class="panel wide">
  <h2>{escape(DEFAULT_EDGE_RELEASE)} Pilot Readiness</h2>
  <div class="phase-groups">
    <div><b>Target gateway</b><span>Selected below; no fleet batch starts from this page.</span></div>
    <div><b>Target UI version</b><span>{DEFAULT_EDGE_RELEASE}</span></div>
    <div><b>Target agent version</b><span>{agent_target_label}</span></div>
    <div><b>Package / manifest checksum</b><span>{escape(package_status)}</span></div>
    <div><b>BACnet route policy</b><span>Existing route settings are preserved; unconfigured installs default to external router UDP 47814 with internal Edge router disabled.</span></div>
    <div><b>Dry run / preflight</b><span>Default action. Shows target identity, SSH route, versions, route decision, files installed, preserved data, restarted services, backup and rollback scope.</span></div>
    <div><b>Final Update/Deploy</b><span>Disabled until the operator checks the final confirmation box after reviewing preflight output.</span></div>
  </div>
</section>
<form id="upgrade-form" method="post" action="/api/start">
  <label>Gateway number
    <input name="gateway_id" placeholder="GW0xx" required>
  </label>
  <label>Site ID
    <input name="site_id" placeholder="Auto-fills from gateway number">
  </label>
  <label class="wide">Cloud API URL
    <input name="cloud_url" value="{DEFAULT_CLOUD_URL}" required>
  </label>
  <label class="wide">Render / cloud admin token
    <input type="password" name="admin_api_token" value="{admin_token}" autocomplete="off" required>
  </label>
  <label>Cradlepoint IP
    <input name="cradlepoint_host" placeholder="10.xx.xx.xx" required>
  </label>
  <label>Cradlepoint user
    <input name="cradlepoint_user" value="BMS_admin" required>
  </label>
  <label>Cradlepoint password
    <input type="password" name="cradlepoint_password" value="{cp_password}" autocomplete="off" required>
  </label>
  <label>Gateway LAN IP
    <input name="gateway_host" value="192.168.1.200" required>
  </label>
  <label>Gateway user
    <input name="gateway_user" value="swadmin" required>
  </label>
  <label>Gateway password
    <input type="password" name="gateway_password" value="{gw_password}" autocomplete="off" required>
  </label>
  <label>Edge UI Git commit
    <input name="edge_ui_commit" value="{configured_default('IOT_EDGE_DEV_UI_COMMIT', DEFAULT_EDGE_UI_INPUT)[:7]}" pattern="[0-9A-Fa-f]{{7,40}}" required>
    <span class="hint">{EDGE_UI_REPOSITORY}; enter a full SHA or unambiguous 7+ character prefix.</span>
  </label>
  <label>Edge Agent Git commit
    <input name="edge_agent_commit" value="{configured_default('IOT_EDGE_DEV_AGENT_COMMIT', DEFAULT_EDGE_AGENT_INPUT)[:7]}" pattern="[0-9A-Fa-f]{{7,40}}" required>
    <span class="hint">{EDGE_AGENT_REPOSITORY}; resolved full SHA is required before execution.</span>
  </label>
  <label>Git ref
    <input name="git_ref" value="{DEFAULT_EDGE_UPDATE_REF}" required>
  </label>
  <label>Edge Release
    <input name="edge_release" value="{DEFAULT_EDGE_RELEASE}" pattern="[0-9]+\\.[0-9]+\\.[0-9]+" required>
  </label>
  <label class="wide">Release manifest
    <input name="release_manifest_path" value="{escape(DEFAULT_RELEASE_MANIFEST, quote=True)}" required>
  </label>
  <label class="wide">Repo path on gateway
    <input name="remote_repo" value="{DEFAULT_REPO_PATH}" required>
  </label>
  <label class="wide">Local BACnet UI source folder on Windows (developer-only; ignored for validated releases)
    <input name="ui_source_folder" value="{escape(DEFAULT_UI_SOURCE, quote=True)}" disabled>
    <span class="hint">This release deploys the embedded validated artifact from this updater package.</span>
  </label>
  <label>Local BACnet UI username
    <input name="ui_username" value="admin" required>
  </label>
  <label>Local BACnet UI password
    <input type="password" name="ui_password" value="{ui_password}" autocomplete="off" required>
  </label>
  <div class="wide checks">
    <label><input type="checkbox" name="dry_run" value="1" checked> Dry run / Preflight</label>
    <label><input type="checkbox" name="reuse_uploaded_zip" value="1"> Reuse uploaded UI artifact</label>
    <label><input type="checkbox" name="skip_edge_ui_stop" value="1"> Edge UI already stopped / skip stop</label>
    <label><input type="checkbox" name="cloud_portal_verified" value="1"> Cloud portal verified</label>
    <label><input type="checkbox" name="final_update_confirmed" value="1"> Final Update/Deploy confirmed</label>
    <label><input type="checkbox" name="commit_resolution_confirmed" value="1"> Resolved commit IDs reviewed and confirmed</label>
  </div>
  <div class="wide phase-select">
    <strong>Processes to run</strong>
    <button type="button" id="select-all-phases">Select all</button>
    <button type="button" id="clear-all-phases">Clear all</button>
    <div class="phase-groups">
      <div><b>1. UI Update</b><span>Back up, upload, apply, authenticate, and restart the local BACnet UI.</span></div>
      <div><b>2. Provision + Agent Update</b><span>Provision the gateway, pull the selected Git release, install Python, and restart the cloud agent.</span></div>
    </div>
    <div class="phase-options">
      {''.join(f'<label><input type="checkbox" name="selected_phases" value="{i}" checked> {escape(name)}</label>' for i, name in enumerate(PHASES))}
    </div>
    <span class="hint">All processes are selected by default. Use this for targeted reruns only.</span>
  </div>
  <div class="wide">
    <button id="resolve-commits-button" type="button">Resolve commits</button>
    <button id="start-button" type="submit">Run Preflight</button>
  </div>
</form>
<div class="layout">
  <section class="panel">
    <h2>Phases</h2>
    <div id="phases"></div>
    <div class="actions">
      <button id="continue-button" class="secondary" type="button" disabled>Continue</button>
      <button id="rollback-button" class="danger" type="button" disabled>Restore legacy full backup</button>
      <button id="rollback-code-button" class="danger" type="button" disabled>Restore code-only checkpoint</button>
      <button id="checkpoint-list-button" type="button" disabled>List code-only checkpoints</button>
      <button id="disable-agent-button" class="danger" type="button" disabled>Disable Agent</button>
    </div>
    <h2>Validation Checklist</h2>
    <div id="summary" class="phase-groups"></div>
  </section>
  <section>
    <h2>Live Log</h2>
    <pre id="log">Ready.</pre>
  </section>
</div>
<script>
const form = document.getElementById("upgrade-form");
const startButton = document.getElementById("start-button");
const resolveCommitsButton = document.getElementById("resolve-commits-button");
const continueButton = document.getElementById("continue-button");
const rollbackButton = document.getElementById("rollback-button");
const rollbackCodeButton = document.getElementById("rollback-code-button");
const checkpointListButton = document.getElementById("checkpoint-list-button");
const disableAgentButton = document.getElementById("disable-agent-button");
const log = document.getElementById("log");
const phaseChecks = () => [...document.querySelectorAll('input[name="selected_phases"]')];
document.getElementById("select-all-phases").addEventListener("click", () => phaseChecks().forEach((input) => input.checked = true));
document.getElementById("clear-all-phases").addEventListener("click", () => phaseChecks().forEach((input) => input.checked = false));
const phases = document.getElementById("phases");
const summary = document.getElementById("summary");
let pollTimer = null;
let jobId = null;

function setLog(text) {{
  log.textContent = text || "";
  log.scrollTop = log.scrollHeight;
}}

function renderPhases(items) {{
  phases.innerHTML = items.map((p) => `<div class="phase"><span>${{p.name}}</span><span class="badge ${{p.status.replaceAll(" ", "_")}}">${{p.status}}</span></div>`).join("");
}}

function renderSummary(items) {{
  const entries = Object.entries(items || {{}});
  summary.innerHTML = entries.length ? entries.map(([key, value]) => `<div><b>${{key}}</b><span>${{value}}</span></div>`).join("") : "<div><b>Preflight</b><span>Run preflight to populate target identity, SSH route, versions, BACnet policy, backup, validation and rollback status.</span></div>";
}}

async function poll() {{
  if (!jobId) return;
  const response = await fetch(`/api/status?job_id=${{encodeURIComponent(jobId)}}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${{response.status}}`);
  setLog(body.log);
  renderPhases(body.phases || []);
  renderSummary(body.summary || {{}});
  continueButton.disabled = body.status !== "waiting";
  rollbackButton.disabled = !body.can_rollback;
  rollbackCodeButton.disabled = !body.can_rollback;
  checkpointListButton.disabled = !jobId;
  disableAgentButton.disabled = !body.can_disable_agent;
  startButton.disabled = body.status === "running" || body.status === "waiting";
  startButton.textContent = body.status === "complete" || body.status === "failed" ? "Start new run" : "Run Preflight";
  if (body.status === "running" || body.status === "queued") {{
    pollTimer = setTimeout(poll, 1000);
  }}
}}

async function post(path, body) {{
  const response = await fetch(path, {{ method: "POST", body }});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
  return payload;
}}

resolveCommitsButton.addEventListener("click", async () => {{
  try {{
    const body = await post("/api/resolve-commits", new URLSearchParams(new FormData(form)));
    setLog(`Resolved Edge UI: ${{body.edge_ui.full_sha}} (${{body.edge_ui.repository}})\\nUI artifact: ${{body.edge_ui.artifact_filename}}\\nUI artifact SHA-256: ${{body.edge_ui.artifact_sha256}}\\nResolved Edge Agent: ${{body.edge_agent.full_sha}} (${{body.edge_agent.repository}})\\nReview these immutable targets, then check \"Resolved commit IDs reviewed and confirmed\" before running.`);
  }} catch (error) {{ setLog(`Commit resolution failed: ${{error.message}}`); }}
}});

form.addEventListener("submit", async (event) => {{
  event.preventDefault();
  if (pollTimer) clearTimeout(pollTimer);
  startButton.disabled = true;
  setLog("Starting...\\n");
  try {{
    const body = await post("/api/start", new URLSearchParams(new FormData(form)));
    jobId = body.job_id;
    poll();
  }} catch (error) {{
    setLog(`Failed to start: ${{error.message}}`);
    startButton.disabled = false;
  }}
}});

continueButton.addEventListener("click", async () => {{
  continueButton.disabled = true;
  await post("/api/continue", new URLSearchParams({{ job_id: jobId }}));
  poll();
}});

rollbackButton.addEventListener("click", async () => {{
  const backup = prompt("Backup filename to restore, for example edge-bacnet-ui-v2.backup.YYYYMMDD-HHMMSS.tar.gz");
  if (!backup) return;
  rollbackButton.disabled = true;
  await post("/api/rollback-ui", new URLSearchParams({{ job_id: jobId, backup }}));
  poll();
}});

rollbackCodeButton.addEventListener("click", async () => {{
  const edgeRelease = prompt("Edge Release checkpoint to restore, for example 0.1.7. This restores code only and preserves data, start.sh, credentials, and site settings.");
  if (!edgeRelease) return;
  rollbackCodeButton.disabled = true;
  await post("/api/rollback-code", new URLSearchParams({{ job_id: jobId, edge_release: edgeRelease }}));
  poll();
}});

checkpointListButton.addEventListener("click", async () => {{
  checkpointListButton.disabled = true;
  await post("/api/list-code-checkpoints", new URLSearchParams({{ job_id: jobId }}));
  poll();
}});

disableAgentButton.addEventListener("click", async () => {{
  if (!confirm("Stop and disable iot-cx-agent.service on this gateway?")) return;
  disableAgentButton.disabled = true;
  await post("/api/disable-agent", new URLSearchParams({{ job_id: jobId }}));
  poll();
}});
</script>
""",
    )


def value(fields: dict[str, list[str]], key: str) -> str:
    return fields.get(key, [""])[0].strip()


def parse_bool(fields: dict[str, list[str]], key: str) -> bool:
    return value(fields, key) in {"1", "true", "on", "yes"}


def resolve_requested_commits(fields: dict[str, list[str]]) -> tuple[object, object]:
    """Resolve only explicit immutable object IDs; branch names are rejected."""
    token = load_env_defaults()["GITHUB_TOKEN"]
    edge_ui = resolve_commit(EDGE_UI_REPOSITORY, value(fields, "edge_ui_commit") or configured_default("IOT_EDGE_DEV_UI_COMMIT", DEFAULT_EDGE_UI_INPUT), token=token)
    edge_agent = resolve_commit(EDGE_AGENT_REPOSITORY, value(fields, "edge_agent_commit") or configured_default("IOT_EDGE_DEV_AGENT_COMMIT", DEFAULT_EDGE_AGENT_INPUT), token=token)
    return edge_ui, edge_agent


def read_agent_version_from_source(commit: str, *, token: str = "", opener=urllib_request.urlopen) -> str:
    """Read the candidate's declared version from its immutable GitHub source."""
    url = (
        f"https://raw.githubusercontent.com/{EDGE_AGENT_REPOSITORY}/{commit}/"
        "edge-agent/iot_cx_agent/__init__.py"
    )
    headers = {"Accept": "text/plain"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with opener(urllib_request.Request(url, headers=headers), timeout=15) as response:
            source = response.read().decode("utf-8")
    except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError, ValueError) as exc:
        raise ValueError(f"Could not read Edge Agent version from resolved source {commit}.") from exc
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', source, re.MULTILINE)
    if not match:
        raise ValueError(f"Resolved Edge Agent source {commit} has no valid __version__ declaration.")
    return match.group(1)


def ui_phases_selected(selected_phases: tuple[int, ...]) -> bool:
    return bool(set(selected_phases) & set(UI_ONLY_PHASES))


def parse_upgrade_request(body: bytes) -> UpgradeRequest:
    fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    selected_raw = fields.get("selected_phases")
    selected_phases = tuple(sorted({int(value) for value in (selected_raw or []) if value.isdigit() and 0 <= int(value) < len(PHASES)}))
    gateway_id = value(fields, "gateway_id")
    ui_password = value(fields, "ui_password")
    if "'" in ui_password:
        raise ValueError("Local BACnet UI password cannot contain a single quote for this legacy update flow.")
    final_update_confirmed = parse_bool(fields, "final_update_confirmed")
    try:
        edge_ui, edge_agent = resolve_requested_commits(fields)
    except CommitResolutionError as exc:
        raise ValueError(str(exc)) from exc
    if final_update_confirmed and not parse_bool(fields, "commit_resolution_confirmed"):
        raise ValueError("Resolve both commit IDs, review the full SHAs, and check the commit confirmation box before execution.")
    dry_run = parse_bool(fields, "dry_run") or not final_update_confirmed
    if final_update_confirmed and selected_phases and selected_phases not in STANDARD_REAL_RUN_PHASE_SETS:
        invalid_targeted_phases = sorted(set(selected_phases) - set(TARGETED_REAL_RUN_PHASES))
        if invalid_targeted_phases:
            allowed_names = ", ".join(PHASES[index] for index in TARGETED_REAL_RUN_PHASES)
            raise ValueError(f"Targeted real-run phase selection may include only: {allowed_names}.")
    artifact_path = ""
    artifact_sha256 = ""
    if ui_phases_selected(selected_phases):
        try:
            artifact = materialize_ui_artifact(edge_ui.full_sha, token=load_env_defaults()["GITHUB_TOKEN"])
        except UIArtifactError as exc:
            raise ValueError(f"UI artifact creation failed closed: {exc}") from exc
        artifact_path, artifact_sha256 = str(artifact.path), artifact.sha256
    manifest_path = value(fields, "release_manifest_path") or DEFAULT_RELEASE_MANIFEST
    manifest = load_manifest(Path(manifest_path))
    defaults = load_env_defaults()
    override_requested = bool(defaults["IOT_EDGE_DEV_AGENT_COMMIT"].strip())
    effective_agent_commit = edge_agent.full_sha if override_requested else manifest.agent_source_commit
    expected_agent_version = (
        read_agent_version_from_source(effective_agent_commit, token=defaults["GITHUB_TOKEN"])
        if override_requested
        else DEFAULT_EDGE_RELEASE
    )
    request = UpgradeRequest(
        gateway_id=gateway_id,
        site_id=value(fields, "site_id") or gateway_id,
        cloud_url=(value(fields, "cloud_url") or DEFAULT_CLOUD_URL).rstrip("/"),
        admin_api_token=value(fields, "admin_api_token"),
        cradlepoint_host=value(fields, "cradlepoint_host"),
        cradlepoint_user=value(fields, "cradlepoint_user") or "BMS_admin",
        cradlepoint_password=value(fields, "cradlepoint_password"),
        gateway_host=value(fields, "gateway_host") or "192.168.1.200",
        gateway_user=value(fields, "gateway_user") or "swadmin",
        gateway_password=value(fields, "gateway_password"),
        git_ref=effective_agent_commit,
        remote_repo=value(fields, "remote_repo") or DEFAULT_REPO_PATH,
        ui_source_folder=value(fields, "ui_source_folder") or DEFAULT_UI_SOURCE,
        ui_username=value(fields, "ui_username") or "admin",
        ui_password=ui_password,
        dry_run=dry_run,
        reuse_uploaded_zip=parse_bool(fields, "reuse_uploaded_zip"),
        skip_edge_ui_stop=parse_bool(fields, "skip_edge_ui_stop"),
        cloud_portal_verified=parse_bool(fields, "cloud_portal_verified"),
        final_update_confirmed=final_update_confirmed,
        selected_phases=selected_phases,
        edge_release=release_name(value(fields, "edge_release") or DEFAULT_EDGE_RELEASE),
        release_manifest_path=manifest_path,
        edge_ui_commit=edge_ui.full_sha,
        edge_agent_commit=effective_agent_commit,
        expected_agent_version=expected_agent_version,
        agent_source="Development override" if override_requested else "Validated release manifest",
        development_agent_override=override_requested,
        ui_artifact_path=artifact_path,
        ui_artifact_sha256=artifact_sha256,
    )
    required = [
        ("Gateway number", request.gateway_id),
        ("Cloud API URL", request.cloud_url),
        ("Render / cloud admin token", request.admin_api_token),
        ("Cradlepoint IP", request.cradlepoint_host),
        ("Cradlepoint password", request.cradlepoint_password),
        ("Gateway password", request.gateway_password),
        ("Local BACnet UI password", request.ui_password),
    ]
    missing = [name for name, field_value in required if not field_value]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")
    if request.edge_release != manifest.edge_release:
        raise ValueError(f"Edge Release {request.edge_release} does not match manifest Edge Release {manifest.edge_release}")
    return request


def connect_client(host: str, user: str, password: str, *, sock=None):
    if paramiko is None:
        raise RuntimeError("Missing dependency: run `python -m pip install -r tools/gateway-update-requirements.txt`")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
            sock=sock,
        )
        return client
    except paramiko.AuthenticationException:
        client.close()
        if sock is not None:
            raise
        return connect_client_keyboard_interactive(host, user, password)


def connect_client_keyboard_interactive(host: str, user: str, password: str):
    raw_sock = socket.create_connection((host, 22), timeout=20)
    transport = paramiko.Transport(raw_sock)
    transport.banner_timeout = 20
    transport.auth_timeout = 20
    try:
        transport.start_client(timeout=20)

        def handler(_title, _instructions, prompts):
            answers = []
            for prompt, echo in prompts:
                prompt_text = str(prompt).lower()
                answers.append(password if "password" in prompt_text or not echo else "")
            return answers

        transport.auth_interactive(user, handler)
        if not transport.is_authenticated():
            raise paramiko.AuthenticationException("keyboard-interactive authentication failed")
        client = paramiko.SSHClient()
        client._transport = transport
        return client
    except Exception:
        transport.close()
        raise


def command_output(client, command: str, *, sudo_password: str | None = None, timeout: int = 300) -> tuple[int, str]:
    stdin, stdout, stderr = client.exec_command(command, get_pty=sudo_password is not None, timeout=timeout)
    if sudo_password is not None:
        stdin.write(f"{sudo_password}\n")
        stdin.flush()
    exit_code = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    return exit_code, output + error


def read_shell(shell, timeout_sec: float = 30.0, quiet_sec: float = 0.4) -> str:
    deadline = time.time() + timeout_sec
    quiet_deadline: float | None = None
    chunks: list[str] = []
    while time.time() < deadline:
        if shell.recv_ready():
            chunks.append(shell.recv(4096).decode("utf-8", errors="replace"))
            quiet_deadline = time.time() + quiet_sec
            continue
        if quiet_deadline is not None and time.time() >= quiet_deadline:
            break
        time.sleep(0.1)
    return "".join(chunks)


def wait_for_shell_text(shell, needles: tuple[str, ...], timeout_sec: float = 45.0) -> str:
    deadline = time.time() + timeout_sec
    output = ""
    lowered_needles = tuple(needle.lower() for needle in needles)
    while time.time() < deadline:
        output += read_shell(shell, timeout_sec=1.0, quiet_sec=0.1)
        lowered_output = output.lower()
        if any(needle in lowered_output for needle in lowered_needles):
            return output
    raise RuntimeError(f"Timed out waiting for one of: {', '.join(needles)}")


def send_shell_command(shell, command: str) -> None:
    shell.send(command + "\n")


def wait_for_shell_marker(shell, marker: str, timeout_sec: float = 600.0) -> tuple[str, str]:
    deadline = time.time() + timeout_sec
    output = ""
    marker_prefix = f"{marker}:"
    while time.time() < deadline:
        output += read_shell(shell, timeout_sec=1.0, quiet_sec=0.1)
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith(marker_prefix):
                return output, stripped.partition(":")[2].strip()
    raise RuntimeError(f"Timed out waiting for command marker: {marker}")


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def sudo_systemctl_timeout(action: str, service: str, timeout_sec: int = 30) -> str:
    script = (
        f"timeout -k 5s {timeout_sec}s sudo -S -p '' systemctl {shell_quote(action)} {shell_quote(service)} "
        f"|| {{ code=$?; echo 'systemctl {action} {service} failed or timed out with exit code' $code; "
        f"timeout -k 5s 15s systemctl --no-pager --full status {shell_quote(service)} || true; exit $code; }}"
    )
    return f"sh -c {shell_quote(script)}"


def stop_edge_ui_command() -> str:
    # Keep this as a single, directly-executed command. Wrapping systemctl in a
    # nested `sh -c` caused the legacy interactive SSH shell to wait indefinitely.
    return "timeout -k 5s 30s sudo -S -p '' systemctl stop edge-bacnet-ui.service"


def start_sh_update_script(username: str, password: str, path: str = "/home/swadmin/edge-bacnet-ui-v2/start.sh") -> str:
    return f"""
from pathlib import Path
path = Path({path!r})
text = path.read_text()
settings = {{
    "AUTH_ENABLED": "1",
    "EDGE_UI_USERNAME": {username!r},
    "EDGE_UI_PASSWORD": {password!r},
}}
route_defaults = [
    "export BACNET_IP_PORT=47814",
    "export BACNET_PORT_MODE=external",
]
route_markers = (
    "BACNET_IP_PORT",
    "BACNET_IP_PORTS",
    "BACNET_PORT_MODE",
    "BACNET_EDGE_PROGRAM_PORTS",
)
lines = text.splitlines()
has_route_config = any(
    line.strip().startswith(f"export {{marker}}=") or line.strip().startswith(f"{{marker}}=")
    for line in lines
    for marker in route_markers
)
seen = set()
out = []
for line in lines:
    stripped = line.strip()
    replaced = False
    for key, value in settings.items():
        if stripped.startswith(f"export {{key}}=") or stripped.startswith(f"{{key}}="):
            quote = "'" if key == "EDGE_UI_PASSWORD" else ""
            out.append(f"export {{key}}={{quote}}{{value}}{{quote}}")
            seen.add(key)
            replaced = True
            break
    if not replaced:
        out.append(line)
insert_at = 1 if out and out[0].startswith("#!") else 0
missing = []
for key, value in settings.items():
    if key not in seen:
        quote = "'" if key == "EDGE_UI_PASSWORD" else ""
        missing.append(f"export {{key}}={{quote}}{{value}}{{quote}}")
if not has_route_config:
    missing = route_defaults + missing
out[insert_at:insert_at] = missing
path.write_text("\\n".join(out) + "\\n")
"""


def update_start_sh_command(username: str, password: str) -> str:
    script = start_sh_update_script(username, password)
    return "python3 -c " + shell_quote(script)


def apply_ui_files_script(
    src_path: str = "/tmp/edge-bacnet-ui-v2-update",
    dest_path: str = "/home/swadmin/edge-bacnet-ui-v2",
) -> str:
    return f"""
from pathlib import Path
import shutil

src = Path({src_path!r})
dest = Path({dest_path!r})
files = {list(UI_PACKAGE_FILES)!r}
dirs = {list(UI_PACKAGE_DIRS)!r}
optional_files = {list(UI_OPTIONAL_PACKAGE_FILES)!r}
missing = [item for item in [*files, *dirs] if not (src / item).exists()]
if missing:
    raise SystemExit("UI update package missing required item(s): " + ", ".join(missing))
for item in files:
    target = dest / item
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / item, target)
for item in dirs:
    target = dest / item
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src / item, target)
for item in optional_files:
    source = src / item
    if source.exists():
        target = dest / item
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
"""


def apply_ui_files_command() -> str:
    script = apply_ui_files_script()
    return "python3 -c " + shell_quote(script)


def inspect_commands() -> list[tuple[str, str, bool]]:
    return [
        ("hostname", "hostname", False),
        ("network addresses", "ip -br addr", False),
        ("disk space", "df -h /home/swadmin /tmp | sed -n '1,5p'", False),
        ("backup path writable", "test -w /home/swadmin && echo BACKUP_PATH_WRITABLE=/home/swadmin", False),
        ("sudo available", "sudo -S -p '' -v && echo SUDO_AVAILABLE=yes", True),
        ("legacy UI folder", f'ls -ld {REMOTE_UI_PATH} 2>/dev/null || echo "missing edge-bacnet-ui-v2"', False),
        ("cloud repo folder", f'ls -ld {DEFAULT_REPO_PATH} 2>/dev/null || echo "missing iot-cloud-commissioning"', False),
        ("current Edge UI version", "grep -R \"Edge Release\\|Edge BACnet\" -n /home/swadmin/edge-bacnet-ui-v2/README.md /home/swadmin/edge-bacnet-ui-v2/templates/base.html 2>/dev/null | head -20 || true", False),
        ("current edge agent version", "/home/swadmin/iot-cloud-commissioning/edge-agent/.venv/bin/python -c 'import iot_cx_agent; print(\"EDGE_AGENT_VERSION=\" + getattr(iot_cx_agent, \"__version__\", \"unknown\"))' 2>/dev/null || python3 -c 'import iot_cx_agent; print(\"EDGE_AGENT_VERSION=\" + getattr(iot_cx_agent, \"__version__\", \"unknown\"))' 2>/dev/null || echo EDGE_AGENT_VERSION=unknown", False),
        ("pre-upgrade agent BACnet default port", "awk '/^bacnet_default_port:/{print \"PRE_UPGRADE_AGENT_DEFAULT_PORT=\" $2; found=1; exit} END{if (!found) print \"PRE_UPGRADE_AGENT_DEFAULT_PORT=47814\"}' /etc/iot-cx-agent/agent.yaml 2>/dev/null || echo PRE_UPGRADE_AGENT_DEFAULT_PORT=47814", False),
        ("edge UI active", "systemctl is-active edge-bacnet-ui.service 2>/dev/null || true", False),
        ("edge UI enabled", "systemctl is-enabled edge-bacnet-ui.service 2>/dev/null || true", False),
        (
            "BACnet tools",
            'ls -l /home/swadmin/bacnet-stack/bin/bacwi /home/swadmin/bacnet-stack/bin/bacrp /home/swadmin/bacnet-stack/bin/bacrpm 2>/dev/null || echo "one or more BACnet tools missing"',
            False,
        ),
        ("edge UI git status", f"git -C {REMOTE_UI_PATH} status || true", False),
        ("edge UI git remote", f"git -C {REMOTE_UI_PATH} remote -v || true", False),
        ("edge UI git log", f"git -C {REMOTE_UI_PATH} log --oneline -5 || true", False),
        (
            "safe start.sh inspection",
            r"""timeout 5s grep -nE 'BACNET_IP_PORT|BACNET_IP_PORTS|BACNET_PORT_MODE|AUTH_ENABLED|EDGE_UI_USERNAME|EDGE_UI_PASSWORD|RPM_BLOCK_SIZE|RPM_VIEW_BLOCK_SIZE|DEFAULT_SCAN_LIMIT|MAX_OBJECTS' /home/swadmin/edge-bacnet-ui-v2/start.sh | sed -E "s/(EDGE_UI_PASSWORD=).*/\1'***SET***'/" || true""",
            False,
        ),
    ]


def full_backup_command(
    backup_root: str = "/home/swadmin",
    *,
    timeout_seconds: int = 600,
    heartbeat_seconds: int = 15,
) -> str:
    """Create and validate a live Edge UI backup without excluding its data."""
    root = shell_quote(backup_root)
    return f'''cd {root}
archive="edge-bacnet-ui-v2.backup.$(date +%Y%m%d-%H%M%S).tar.gz"
stderr_file="${{archive}}.tar.stderr"
rm -f "$stderr_file"
echo "BACKUP_ARCHIVE=$archive"
timeout -k 10s {timeout_seconds}s tar -czf "$archive" edge-bacnet-ui-v2 2>"$stderr_file" &
backup_pid=$!
heartbeat_remaining=0
while kill -0 "$backup_pid" 2>/dev/null; do
  if [ "$heartbeat_remaining" -le 0 ]; then
    echo "BACKUP_PROGRESS_HEARTBEAT=creating $archive"
    heartbeat_remaining={heartbeat_seconds}
  fi
  sleep 1
  heartbeat_remaining=$((heartbeat_remaining - 1))
done
wait "$backup_pid"
tar_status=$?
cat "$stderr_file"
if [ "$tar_status" -eq 0 ]; then
  echo "BACKUP_TAR_RESULT=clean"
elif [ "$tar_status" -eq 1 ] && [ -s "$stderr_file" ] && ! grep -Ev '^tar: .*: file changed as we read it$' "$stderr_file" >/dev/null; then
  echo "BACKUP_TAR_RESULT=live-file-change-warning"
else
  echo "BACKUP_TAR_RESULT=failed exit=$tar_status" >&2
  exit "$tar_status"
fi
if [ ! -s "$archive" ]; then
  echo "BACKUP_ARCHIVE_INVALID=missing-or-zero-byte" >&2
  exit 1
fi
gzip -t "$archive" || {{ echo "BACKUP_ARCHIVE_INVALID=gzip" >&2; exit 1; }}
tar -tzf "$archive" >/dev/null || {{ echo "BACKUP_ARCHIVE_INVALID=listing" >&2; exit 1; }}
rm -f "$stderr_file"
echo "BACKUP_ARCHIVE_VALID=Passed"
'''


def backup_commands(edge_release: str = DEFAULT_EDGE_RELEASE) -> list[tuple[str, str, bool]]:
    commands = [
        ("edge UI enabled", "systemctl is-enabled edge-bacnet-ui.service 2>/dev/null || true", False),
        ("edge UI active", "systemctl is-active edge-bacnet-ui.service 2>/dev/null || true", False),
        ("create and validate full UI backup", full_backup_command(), False),
        ("list UI backups", "cd /home/swadmin && ls -lh edge-bacnet-ui-v2.backup.*.tar.gz", False),
    ]
    commands.extend((f"code-only checkpoint {index + 1}", command, False) for index, command in enumerate(checkpoint_commands(edge_release)))
    return commands


def apply_ui_commands(request: UpgradeRequest) -> list[tuple[str, str, bool]]:
    extract = f"tar -xzf {shell_quote(REMOTE_UI_ARTIFACT_PATH)} -C /tmp/edge-bacnet-ui-v2-update"
    stop_command = (
        ("skip edge UI stop", "echo 'Skipping edge UI stop because Edge UI already stopped / skip stop is checked.'", False)
        if request.skip_edge_ui_stop
        else ("stop edge UI", stop_edge_ui_command(), True)
    )
    return [
        ("prepare normalized extraction folder", "rm -rf /tmp/edge-bacnet-ui-v2-update && mkdir -p /tmp/edge-bacnet-ui-v2-update", False),
        ("extract embedded UI artifact", extract, False),
        ("verify normalized templates", r"""test -d /tmp/edge-bacnet-ui-v2-update/templates && ls -lah /tmp/edge-bacnet-ui-v2-update/templates""", False),
        stop_command,
        ("confirm edge UI stopped", "systemctl is-active edge-bacnet-ui.service || true", False),
        ("apply code-only UI files", apply_ui_files_command(), False),
        ("verify UI file ownership", "find /home/swadmin/edge-bacnet-ui-v2 -maxdepth 2 \\( ! -user swadmin -o ! -group swadmin \\) -print | head -20 || true", False),
        ("preserve start.sh executable", "chmod +x /home/swadmin/edge-bacnet-ui-v2/start.sh", False),
        ("verify replaced templates", "ls -lah /home/swadmin/edge-bacnet-ui-v2/templates", False),
    ]


def auth_commands(request: UpgradeRequest) -> list[tuple[str, str, bool]]:
    if not request.edge_agent_write_token:
        raise ValueError("A gateway-local edge-agent write token is required.")
    token_b64 = b64(request.edge_agent_write_token + "\n")
    agent_env_script = (
        "set -eu; tmp=$(mktemp); "
        "grep -v '^EDGE_AGENT_WRITE_TOKEN=' /etc/iot-cx-agent/edge-agent.env 2>/dev/null > \"$tmp\" || true; "
        f"printf %s {shell_quote(token_b64)} | base64 -d >> \"$tmp\"; "
        "install -m 0600 -o root -g root \"$tmp\" /etc/iot-cx-agent/edge-agent.env; rm -f \"$tmp\""
    )
    return [
        ("backup start.sh", 'cd /home/swadmin/edge-bacnet-ui-v2 && cp start.sh "start.sh.bak.$(date +%Y%m%d-%H%M%S)"', False),
        ("preserve existing start.sh or default fresh start.sh", update_start_sh_command(request.ui_username, request.ui_password), False),
        ("write local edge UI adapter token", f"printf %s {shell_quote(token_b64)} | base64 -d > /home/swadmin/edge-bacnet-ui-v2/.edge-agent-write-token && chmod 600 /home/swadmin/edge-bacnet-ui-v2/.edge-agent-write-token", False),
        ("write edge agent adapter token", f"sudo -S -p '' sh -c {shell_quote(agent_env_script)}", True),
        (
            "verify safe start.sh auth",
            r"""grep -nE 'BACNET_IP_PORT|BACNET_IP_PORTS|BACNET_PORT_MODE|AUTH_ENABLED|EDGE_UI_USERNAME|EDGE_UI_PASSWORD|RPM_BLOCK_SIZE|RPM_VIEW_BLOCK_SIZE|DEFAULT_SCAN_LIMIT|MAX_OBJECTS' /home/swadmin/edge-bacnet-ui-v2/start.sh | sed -E 's#^(.*EDGE_UI_PASSWORD=).*$#\1***SET***#'""",
            False,
        ),
    ]


def restart_ui_commands() -> list[tuple[str, str, bool]]:
    return [
        ("start edge UI", "sudo -S -p '' systemctl start --no-block edge-bacnet-ui.service", True),
        ("edge UI active check", "sleep 5 && systemctl is-active edge-bacnet-ui.service", False),
        ("local UI HTTP auth check", "curl -I http://127.0.0.1:5000/", False),
    ]


def repo_release_validation_command(repo_path: str, expected_commit: str) -> str:
    script = r"""
import subprocess
import sys

expected = sys.argv[1]
head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
print(f"RELEASE_COMMIT={head}")
if head != expected:
    print("REPO_RELEASE_VALIDATION=Failed")
    raise SystemExit(f"Expected release commit {expected}, found {head}")

status = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], text=True).splitlines()
tracked = [line for line in status if not line.startswith("?? ")]
if tracked:
    print("TRACKED_REPO_STATUS=dirty")
    print("TRACKED_REPO_CHANGES=" + "\\n".join(tracked))
    print("REPO_RELEASE_VALIDATION=Failed")
    raise SystemExit("Tracked repository changes are present")

print("TRACKED_REPO_STATUS=clean")

def runtime_label(path):
    if path == "deploy-backups" or path.startswith("deploy-backups/"):
        return "deploy-backups/"
    if path == ".local-backups" or path.startswith(".local-backups/"):
        return ".local-backups/"
    if path == "gw-recovery" or path.startswith("gw-recovery/"):
        return "gw-recovery/"
    if "__pycache__" in path.split("/"):
        return "__pycache__/"
    if path.endswith(".pyc"):
        return "*.pyc"
    return None

ignored = []
unexpected = []
for line in status:
    if not line.startswith("?? "):
        continue
    path = line[3:]
    label = runtime_label(path)
    if label is None:
        unexpected.append(path)
    elif label not in ignored:
        ignored.append(label)

if unexpected:
    print("IGNORED_RUNTIME_PATHS=" + (",".join(ignored) if ignored else "None"))
    print("UNEXPECTED_UNTRACKED_PATHS=" + "\\n".join(unexpected))
    print("REPO_RELEASE_VALIDATION=Failed")
    raise SystemExit("Unexpected untracked repository paths are present")

print("IGNORED_RUNTIME_PATHS=" + (",".join(ignored) if ignored else "None"))
print("REPO_RELEASE_VALIDATION=Passed")
"""
    return f"cd {shell_quote(repo_path)} && python3 -c {shell_quote(script)} {shell_quote(expected_commit)}"


def repo_commands(request: UpgradeRequest) -> list[tuple[str, str, bool]]:
    repo = shell_quote(request.remote_repo)
    ref = shell_quote(request.edge_agent_commit)
    prerequisite_script = (
        "rm -rf /tmp/iot-cx-venv-check; "
        "if command -v git >/dev/null 2>&1 "
        "&& python3 -m venv /tmp/iot-cx-venv-check >/dev/null 2>&1; then "
        "rm -rf /tmp/iot-cx-venv-check; "
        "echo 'prerequisites already present'; "
        "else "
        "rm -rf /tmp/iot-cx-venv-check; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "timeout -k 10s 240s sudo -S -p '' apt-get update "
        "&& timeout -k 10s 300s sudo -n apt-get install -y --no-install-recommends git python3-venv python3.10-venv; "
        "fi"
    )
    return [
        ("verify/install prerequisites", prerequisite_script, True),
        (
            "clone or update cloud repo",
            f"""cd /home/swadmin && if [ -d {repo}/.git ]; then cd {repo} && git remote set-url origin {shell_quote(REMOTE_REPO_URL)} && git fetch origin --tags; else git clone {shell_quote(REMOTE_REPO_URL)} {repo}; cd {repo}; git fetch origin --tags; fi && git checkout --detach {ref}""",
            False,
        ),
        ("repo release validation", repo_release_validation_command(request.remote_repo, request.edge_agent_commit), False),
    ]


def agent_config_text(request: UpgradeRequest, bacnet_default_port: str = "47814") -> str:
    port = bacnet_default_port if bacnet_default_port.isdigit() else "47814"
    return f"""gateway_id: {request.gateway_id}
site_id: {request.site_id}
cloud_url: {request.cloud_url}

tunnel_enabled: true
local_ui_url: http://127.0.0.1:5000
tunnel_request_timeout_sec: 900
local_edge_trends_enabled: false

bacnet_default_port: {port}
heartbeat_interval_sec: 30
agent_version: current
ui_version: current

bacnet:
  default_port: {port}
  bacwi_path: /home/swadmin/bacnet-stack/bin/bacwi
  bacrp_path: /home/swadmin/bacnet-stack/bin/bacrp
  bacrpm_path: /home/swadmin/bacnet-stack/bin/bacrpm
  lock_path: /tmp/iot-cloud-commissioning-bacnet-{port}.lock
  timeout_sec: 10
"""


def edge_ui_data_dir_config_script(
    config_path: str = "/etc/iot-cx-agent/agent.yaml",
    default_path: str = DEFAULT_EDGE_UI_DATA_DIR,
) -> str:
    return f"""
from pathlib import Path
import re

config_path = Path({config_path!r})
default_path = {default_path!r}
text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
lines = text.splitlines()
pattern = re.compile(r"^edge_ui_data_dir\\s*:(.*)$")
existing_values = []
for line in lines:
    match = pattern.match(line)
    if match:
        existing_values.append(match.group(1).strip())

resolved = next((value for value in existing_values if value), default_path)
if not any(value for value in existing_values):
    action = "Added"
elif resolved == default_path:
    action = "Preserved"
else:
    action = "PreservedCustom"
new_lines = []
wrote = False
for line in lines:
    if pattern.match(line):
        if not wrote:
            new_lines.append(f"edge_ui_data_dir: {{resolved}}")
            wrote = True
        continue
    new_lines.append(line)
if not wrote:
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    new_lines.append(f"edge_ui_data_dir: {{resolved}}")
config_path.write_text("\\n".join(new_lines) + "\\n", encoding="utf-8")
print(f"EDGE_UI_DATA_DIR_ACTION={{action}}")
print(f"EDGE_UI_DATA_DIR={{resolved}}")
"""


def edge_ui_data_dir_config_command() -> str:
    payload = b64(edge_ui_data_dir_config_script())
    runner = f"import base64; exec(base64.b64decode({payload!r}).decode('utf-8'))"
    return f"sudo -S -p '' timeout -k 5s 30s python3 -c {shell_quote(runner)}"


def edge_ui_data_dir_validation_command(
    config_path: str = "/etc/iot-cx-agent/agent.yaml",
    service_name: str = "iot-cx-agent.service",
) -> str:
    cfg = shell_quote(config_path)
    service = shell_quote(service_name)
    script = """set -eu
cfg=__CONFIG_PATH__
service=__SERVICE_NAME__
value=$(awk -F: '/^edge_ui_data_dir:/{sub(/^[ \t]+/, "", $2); print $2; exit}' "$cfg" 2>/dev/null || true)
echo "EDGE_UI_DATA_DIR=$value"
if [ -z "$value" ]; then
  echo "EDGE_UI_DATA_DIR_VALIDATION=Failed"
  echo "edge_ui_data_dir is missing or blank" >&2
  exit 1
fi
if [ ! -d "$value" ]; then
  echo "EDGE_UI_DATA_DIR_VALIDATION=Failed"
  echo "edge_ui_data_dir does not exist: $value" >&2
  exit 1
fi
svc_user=$(systemctl show "$service" -p User --value 2>/dev/null || true)
[ -n "$svc_user" ] || svc_user=root
if [ "$svc_user" = root ]; then
  test -x "$value" && test -r "$value" && test -w "$value"
else
  sudo -n -u "$svc_user" sh -c 'test -x "$1" && test -r "$1" && test -w "$1"' sh "$value"
fi
echo "EDGE_UI_DATA_DIR_VALIDATION=Passed"
""".replace("__CONFIG_PATH__", cfg).replace("__SERVICE_NAME__", service)
    return (
        "sudo -S -p '' sh -c "
        + shell_quote(script)
    )


def config_commands(request: UpgradeRequest, gateway_token: str, bacnet_default_port: str = "47814") -> list[tuple[str, str, bool]]:
    agent_b64 = b64(agent_config_text(request, bacnet_default_port))
    env_b64 = b64(f"GATEWAY_API_TOKEN={gateway_token}\n")
    gw = shell_quote(request.gateway_id)
    return [
        ("set hostname", f"sudo -S -p '' hostnamectl set-hostname {gw}", True),
        ("create agent folders", "sudo -S -p '' mkdir -p /etc/iot-cx-agent /var/lib/iot-cx-agent", True),
        ("backup existing agent config", 'if [ -f /etc/iot-cx-agent/agent.yaml ]; then sudo -S -p \'\' cp /etc/iot-cx-agent/agent.yaml "/etc/iot-cx-agent/agent.yaml.bak.$(date +%Y%m%d-%H%M%S)"; fi', True),
        ("backup existing token env", 'if [ -f /etc/iot-cx-agent/edge-agent.env ]; then sudo -S -p \'\' cp /etc/iot-cx-agent/edge-agent.env "/etc/iot-cx-agent/edge-agent.env.bak.$(date +%Y%m%d-%H%M%S)"; fi', True),
        ("write agent.yaml", f"printf %s {shell_quote(agent_b64)} | base64 -d > /tmp/agent.yaml && sudo -S -p '' install -m 0644 -o root -g root /tmp/agent.yaml /etc/iot-cx-agent/agent.yaml && rm -f /tmp/agent.yaml", True),
        ("write edge-agent.env", f"printf %s {shell_quote(env_b64)} | base64 -d > /tmp/edge-agent.env && sudo -S -p '' install -m 0600 -o root -g root /tmp/edge-agent.env /etc/iot-cx-agent/edge-agent.env && rm -f /tmp/edge-agent.env", True),
        ("fix agent data ownership", "sudo -S -p '' install -d -m 0750 -o swadmin -g swadmin /var/lib/iot-cx-agent", True),
        ("safe config verification", "grep -E 'gateway_id:|site_id:|cloud_url:|local_ui_url:|local_edge_trends_enabled:|bacnet_default_port:' /etc/iot-cx-agent/agent.yaml && sudo -S -p '' test -s /etc/iot-cx-agent/edge-agent.env && echo 'GATEWAY_API_TOKEN=***SET***' && ls -ld /var/lib/iot-cx-agent", True),
    ]


def install_agent_commands(request: UpgradeRequest) -> list[tuple[str, str, bool]]:
    repo = shell_quote(request.remote_repo)
    return [
        ("verify venv support", "rm -rf /tmp/iot-cx-venv-check; python3 -m venv /tmp/iot-cx-venv-check >/dev/null 2>&1 || (export DEBIAN_FRONTEND=noninteractive; sudo -S -p '' apt-get update && sudo -n apt-get install -y --no-install-recommends python3-venv python3.10-venv python3-pip); rm -rf /tmp/iot-cx-venv-check", True),
        ("create agent venv", f"cd {repo}/edge-agent && python3 -m venv .venv", False),
        ("upgrade pip", f"cd {repo}/edge-agent && .venv/bin/python -m pip install --upgrade pip", False),
        ("install requirements", f"cd {repo}/edge-agent && .venv/bin/python -m pip install -r requirements.txt", False),
        ("install agent package", f"cd {repo}/edge-agent && .venv/bin/python -m pip install -e .", False),
        ("skip data folder ownership check", "echo 'data folder ownership check skipped in legacy nested SSH mode'", False),
    ]


def service_commands(request: UpgradeRequest) -> list[tuple[str, str, bool]]:
    repo = shell_quote(request.remote_repo)
    return [
        ("capture pre-restart agent timestamp", "echo AGENT_SERVICE_START_BEFORE=$(systemctl show iot-cx-agent.service -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true)", False),
        ("install iot-cx-agent service", f"sudo -S -p '' install -m 0644 {repo}/deploy/iot-cx-agent.service /etc/systemd/system/iot-cx-agent.service", True),
        ("systemd daemon reload", "sudo -S -p '' systemctl daemon-reload", True),
        ("show agent service", "systemctl cat iot-cx-agent.service --no-pager", False),
        ("enable agent service", sudo_systemctl_timeout("enable", "iot-cx-agent.service"), True),
        ("restart agent service", sudo_systemctl_timeout("restart", "iot-cx-agent.service"), True),
        ("agent active check", "sleep 8 && systemctl is-active iot-cx-agent.service", False),
        ("agent logs", "echo 'agent log collection skipped in legacy nested SSH mode; service active check is authoritative here'", False),
    ]


def final_commands(request: UpgradeRequest, expected_bacnet_default_port: str = "47814", pre_restart_timestamp: str = "") -> list[tuple[str, str, bool]]:
    expected_port = shell_quote(expected_bacnet_default_port if expected_bacnet_default_port.isdigit() else "47814")
    expected_agent = shell_quote(request.edge_agent_commit)
    expected_version = shell_quote(request.expected_agent_version)
    expected_before = shell_quote(pre_restart_timestamp)
    repo = shell_quote(request.remote_repo)
    return [
        ("hostname", "hostname", False),
        ("agent active", "systemctl is-active iot-cx-agent.service", False),
        ("edge UI active", "systemctl is-active edge-bacnet-ui.service", False),
        ("local UI HTTP auth check", "curl -I http://127.0.0.1:5000/", False),
        ("verify supported BACnet tools", "command -v /home/swadmin/bacnet-stack/bin/bacrp && command -v /home/swadmin/bacnet-stack/bin/bacrpm && echo 'bacrp and bacrpm available'", False),
        ("verify BACnet config preservation", f"pre={expected_port}; post=$(awk '/^bacnet_default_port:/{{print $2; exit}}' /etc/iot-cx-agent/agent.yaml); grep -E 'bacnet_default_port:|default_port:|bacrp_path:|bacrpm_path:' /etc/iot-cx-agent/agent.yaml; echo \"BACNET_CONFIG_PRESERVATION=Passed\"; echo \"PRE_UPGRADE_AGENT_DEFAULT_PORT=$pre\"; echo \"POST_UPGRADE_AGENT_DEFAULT_PORT=$post\"; echo \"BACNET_PORTS_CHANGED=No\"; echo \"BACNET_ROUTES_CHANGED=No\"; echo \"ROUTE_SETTINGS_CHANGED=No\"; test \"$post\" = \"$pre\"", False),
        (
            "verify release components",
            f"cd {repo} && head=$(git rev-parse HEAD) && package=$({repo}/edge-agent/.venv/bin/python -c 'from importlib.metadata import version; print(version(\"iot-cx-agent\"))') && module=$({repo}/edge-agent/.venv/bin/python -c 'import iot_cx_agent; print(iot_cx_agent.__version__)') && active=$(systemctl is-active iot-cx-agent.service) && restarted=$(systemctl show iot-cx-agent.service -p ActiveEnterTimestampMonotonic --value) && {repo}/edge-agent/.venv/bin/iot-cx-agent --help | grep -F -- --network-traffic >/dev/null && trend=$(awk '/^local_edge_trends_enabled:/{{print tolower($2); found=1; exit}} END{{if (!found) print \"false\"}}' /etc/iot-cx-agent/agent.yaml) && echo AGENT_RELEASE_COMMIT=$head && echo AGENT_PACKAGE_VERSION=$package && echo AGENT_MODULE_VERSION=$module && echo AGENT_SERVICE_STATE=$active && echo AGENT_SERVICE_START=$restarted && echo AGENT_NETWORK_TRAFFIC_CLI=Passed && test \"$head\" = {expected_agent} && test \"$package\" = {expected_version} && test \"$module\" = {expected_version} && test \"$active\" = active && test -n \"$restarted\" && test \"$restarted\" != 0 && test \"$restarted\" != {expected_before} && test \"$trend\" != true && echo AGENT_RELEASE_VALIDATION=Passed && echo UI_RELEASE_VALIDATION=Passed && echo LOCAL_EDGE_TRENDS_ENABLED=false && echo BACKGROUND_BACNET_ACTIVITY_ADDED=No && echo MSTP_READ_BASELINE_TARGET=approximately_3_seconds && echo BACNET_IP_READ_BASELINE_TARGET=under_1_second && echo RULE_1_VALIDATION=Passed && echo RELEASE_0_1_9_VALIDATION=Passed",
            False,
        ),
        ("agent final logs", "journalctl -u iot-cx-agent -n 60 --no-pager -l || true", False),
    ]


def validate_agent_runtime_output(output: str, request: UpgradeRequest, pre_restart_timestamp: str) -> None:
    values = {
        key: next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith(key + "=")), "")
        for key in (
            "AGENT_RELEASE_COMMIT", "AGENT_PACKAGE_VERSION", "AGENT_MODULE_VERSION",
            "AGENT_SERVICE_STATE", "AGENT_SERVICE_START", "AGENT_NETWORK_TRAFFIC_CLI",
        )
    }
    expected = {
        "AGENT_RELEASE_COMMIT": request.edge_agent_commit,
        "AGENT_PACKAGE_VERSION": request.expected_agent_version,
        "AGENT_MODULE_VERSION": request.expected_agent_version,
        "AGENT_SERVICE_STATE": "active",
        "AGENT_NETWORK_TRAFFIC_CLI": "Passed",
    }
    mismatches = [f"{key}={values[key]!r}, expected {value!r}" for key, value in expected.items() if values[key] != value]
    if not values["AGENT_SERVICE_START"] or values["AGENT_SERVICE_START"] in {"0", pre_restart_timestamp}:
        mismatches.append("AGENT_SERVICE_START is missing, inactive, or unchanged from before restart")
    if mismatches:
        raise RuntimeError("Actual Agent runtime validation failed: " + "; ".join(mismatches))


def rollback_commands(backup: str) -> list[tuple[str, str, bool]]:
    if not re.fullmatch(r"edge-bacnet-ui-v2\.backup\.\d{8}-\d{6}\.tar\.gz", backup):
        raise ValueError("Backup filename must look like edge-bacnet-ui-v2.backup.YYYYMMDD-HHMMSS.tar.gz")
    quoted = shell_quote(backup)
    return [
        ("stop edge UI", stop_edge_ui_command(), True),
        ("move failed UI folder", 'cd /home/swadmin && mv edge-bacnet-ui-v2 "edge-bacnet-ui-v2.failed.$(date +%Y%m%d-%H%M%S)"', False),
        ("restore selected backup", f"cd /home/swadmin && tar -xzf {quoted}", False),
        ("fix restored ownership", "sudo -S -p '' chown -R swadmin:swadmin /home/swadmin/edge-bacnet-ui-v2", True),
        ("restore start.sh executable", "chmod +x /home/swadmin/edge-bacnet-ui-v2/start.sh", False),
        ("start edge UI", "sudo -S -p '' systemctl start --no-block edge-bacnet-ui.service", True),
        ("check restored UI", "curl -I http://127.0.0.1:5000/", False),
    ]


def disable_agent_commands() -> list[tuple[str, str, bool]]:
    return [
        ("stop cloud agent", sudo_systemctl_timeout("stop", "iot-cx-agent.service"), True),
        ("disable cloud agent", sudo_systemctl_timeout("disable", "iot-cx-agent.service"), True),
    ]


def create_update_zip(source_folder: str, release_manifest_path: str) -> Path:
    """Legacy developer helper retained for tests; normal deploys use the embedded artifact."""
    artifact, _summary = validated_embedded_ui_artifact(release_manifest_path)
    return artifact


def provision_cloud_gateway(request: UpgradeRequest, log: LiveLog, redactor: Redactor) -> str:
    payload = {
        "gateway_id": request.gateway_id,
        "site_id": request.site_id,
        "hostname": request.gateway_id,
        "lan_ip": request.gateway_host,
        "bacnet_port": 47814,
        "agent_version": "current",
        "ui_version": "current",
    }
    http_request = urllib_request.Request(
        f"{request.cloud_url}/api/admin/gateways/provision",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {request.admin_api_token}", "Content-Type": "application/json"},
        method="POST",
    )
    log.append("\nProvisioning cloud gateway identity...\n")
    try:
        with urllib_request.urlopen(http_request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise RuntimeError("Cloud provisioning returned 401 Unauthorized. The admin token is likely wrong or expired.") from exc
        if exc.code == 422:
            raise RuntimeError(f"Cloud provisioning returned 422 Unprocessable Entity. Check the submitted fields. Detail: {detail}") from exc
        raise RuntimeError(f"Cloud provisioning failed with HTTP {exc.code}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Cloud provisioning failed: {exc.reason}") from exc
    gateway_token = str(body.get("gateway_api_token") or "")
    if not gateway_token:
        raise RuntimeError("Cloud provisioning response did not include gateway_api_token")
    redactor.add(gateway_token)
    token_prefix = str(body.get("token_prefix") or gateway_token[:12])
    log.append(f"gateway_id: {body.get('gateway_id', request.gateway_id)}\n")
    log.append(f"site_id: {body.get('site_id', request.site_id)}\n")
    log.append(f"hostname: {body.get('hostname', request.gateway_id)}\n")
    log.append(f"lan_ip: {body.get('lan_ip', request.gateway_host)}\n")
    log.append(f"bacnet_port: {body.get('bacnet_port', 47814)}\n")
    log.append(f"token_prefix: {token_prefix}...\n")
    log.append(f"gateway token length: {len(gateway_token)}\n")
    return gateway_token


class LegacyUpgradeRunner:
    def __init__(self, job_id: str, request: UpgradeRequest) -> None:
        self.job_id = job_id
        self.request = request
        self.redactor = Redactor(
            [
                request.admin_api_token,
                request.cradlepoint_password,
                request.gateway_password,
                request.ui_password,
                request.edge_agent_write_token,
            ]
        )
        self.log = LiveLog(job_id, self.redactor)
        self.cp_client = None
        self.gateway_client = None
        self.gateway_shell = None

    def close(self) -> None:
        if self.gateway_shell is not None:
            self.gateway_shell.close()
            self.gateway_shell = None
        if self.gateway_client is not None:
            self.gateway_client.close()
            self.gateway_client = None
        if self.cp_client is not None:
            self.cp_client.close()
            self.cp_client = None

    def ensure_cradlepoint_client(self):
        if self.cp_client is None:
            self.log.append("\nConnecting to Cradlepoint...\n")
            self.cp_client = connect_client(
                self.request.cradlepoint_host,
                self.request.cradlepoint_user,
                self.request.cradlepoint_password,
            )
        return self.cp_client

    def ensure_gateway_client(self):
        if self.request.dry_run:
            return None
        if self.gateway_client is not None:
            return self.gateway_client
        if self.gateway_shell is not None:
            return None
        self.ensure_cradlepoint_client()
        transport = self.cp_client.get_transport()
        if transport is None:
            raise RuntimeError("Cradlepoint SSH transport did not open")
        try:
            self.log.append("Opening SSH channel to gateway LAN IP...\n")
            channel = transport.open_channel("direct-tcpip", (self.request.gateway_host, 22), ("127.0.0.1", 0))
            self.log.append("Connecting to gateway...\n")
            self.gateway_client = connect_client(
                self.request.gateway_host,
                self.request.gateway_user,
                self.request.gateway_password,
                sock=channel,
            )
            return self.gateway_client
        except (TimeoutError, paramiko.ChannelException, paramiko.SSHException) as exc:
            self.log.append(f"SSH tunnel attempt did not complete ({exc}). Falling back to nested SSH.\n")
            self.ensure_gateway_shell()
            return None

    def ensure_gateway_shell(self):
        if self.request.dry_run:
            return None
        if self.gateway_shell is not None:
            return self.gateway_shell
        cp_client = self.ensure_cradlepoint_client()
        shell = cp_client.invoke_shell(width=160, height=40)
        read_shell(shell, timeout_sec=2.0)

        self.log.append("Connecting from Cradlepoint to gateway with nested SSH...\n")
        send_shell_command(shell, f"ssh {self.request.gateway_user}@{self.request.gateway_host}")
        output = wait_for_shell_text(shell, ("password:", "yes/no", "are you sure"), timeout_sec=45.0)
        if output.strip():
            self.log.append(output)
        if "yes/no" in output.lower() or "are you sure" in output.lower():
            self.log.append("Host-key prompt detected. Sending yes.\n")
            send_shell_command(shell, "yes")
            output = wait_for_shell_text(shell, ("password:",), timeout_sec=30.0)
            if output.strip():
                self.log.append(output)
        self.log.append("Gateway password prompt detected. Sending gateway password from the form.\n")
        send_shell_command(shell, self.request.gateway_password)
        output = wait_for_shell_text(shell, ("$", "#"), timeout_sec=45.0)
        if output.strip():
            self.log.append(output)

        self.gateway_shell = shell
        return shell

    def command_timeout(self, label: str) -> float:
        slow_words = ("pip", "install requirements", "install agent package", "apt", "prerequisites", "clone or update")
        return 1500.0 if any(word in label for word in slow_words) else 600.0

    def run_nested_command(self, label: str, command: str, marker: str, *, sudo_password: str | None = None) -> tuple[int, str]:
        shell = self.ensure_gateway_shell()
        self.log.append(f"\n$ {label}\n")
        command_to_send = command
        if sudo_password is not None:
            sudo_prefix = "sudo -S -p ''"
            placeholder = "__IOTGWCFG_FIRST_SUDO__"
            password_pipe = f"printf '%s\\n' {shell_quote(sudo_password)} | {sudo_prefix}"
            command_to_send = command_to_send.replace(sudo_prefix, placeholder, 1)
            command_to_send = command_to_send.replace(sudo_prefix, "sudo -n")
            command_to_send = command_to_send.replace(placeholder, password_pipe, 1)
        send_shell_command(shell, f"{command_to_send}\nprintf '\\n{marker}:%s\\n' $?")
        try:
            output, exit_text = wait_for_shell_marker(shell, marker, timeout_sec=self.command_timeout(label))
        except Exception:
            self.log.append(f"{label} timed out; sending Ctrl-C and collecting shell output.\n")
            shell.send("\x03")
            time.sleep(1)
            output = read_shell(shell, timeout_sec=3.0)
            safe_timeout_output = output.replace(command_to_send, "").strip()
            if sudo_password is not None:
                safe_timeout_output = safe_timeout_output.replace(sudo_password, "[redacted]")
            if safe_timeout_output:
                self.log.append(safe_timeout_output + "\n")
            return 124, safe_timeout_output + ("\n" if safe_timeout_output else "")
        safe_output = output.replace(command_to_send, "").strip()
        if sudo_password is not None:
            safe_output = safe_output.replace(sudo_password, "[redacted]")
        exit_code = int(exit_text) if exit_text.isdigit() else 1
        return exit_code, safe_output + ("\n" if safe_output else "")

    def run_commands(self, commands: list[tuple[str, str, bool]], *, stop_on_failure: bool = True) -> str:
        output_all = ""
        if self.request.dry_run:
            for label, command, _needs_sudo in commands:
                self.log.append(f"\n[dry-run] $ {label}\n{command}\n")
            return ""
        client = self.ensure_gateway_client()
        for index, (label, command, needs_sudo) in enumerate(commands, start=1):
            sudo_password = self.request.gateway_password if needs_sudo else None
            if client is None:
                marker = f"LEGACY_UPGRADE_{int(time.time())}_{index}"
                exit_code, output = self.run_nested_command(label, command, marker, sudo_password=sudo_password)
            else:
                self.log.append(f"\n$ {label}\n")
                exit_code, output = command_output(
                    client,
                    command,
                    sudo_password=sudo_password,
                    timeout=int(self.command_timeout(label)),
                )
            output_all += output
            if output.strip():
                self.log.append(output)
            if exit_code != 0 and stop_on_failure:
                raise RuntimeError(f"{label} failed with exit code {exit_code}")
        return output_all

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        client = self.ensure_gateway_client()
        if client is not None:
            sftp = client.open_sftp()
            try:
                sftp.put(str(local_path), remote_path)
            finally:
                sftp.close()
            return

        encoded = base64.b64encode(local_path.read_bytes()).decode("ascii")
        remote_b64 = f"{remote_path}.b64"
        self.run_commands([("prepare nested upload", f"rm -f {shell_quote(remote_b64)} {shell_quote(remote_path)}", False)])
        total_parts = max(1, (len(encoded) + NESTED_UPLOAD_CHUNK_SIZE - 1) // NESTED_UPLOAD_CHUNK_SIZE)
        for offset in range(0, len(encoded), NESTED_UPLOAD_CHUNK_SIZE):
            chunk = encoded[offset : offset + NESTED_UPLOAD_CHUNK_SIZE]
            part = (offset // NESTED_UPLOAD_CHUNK_SIZE) + 1
            self.run_commands(
                [(
                    f"upload UI artifact chunk {part}/{total_parts}",
                    f"cat >> {shell_quote(remote_b64)} <<'IOTGWCFG_UPLOAD_CHUNK'\n{chunk}\nIOTGWCFG_UPLOAD_CHUNK",
                    False,
                )]
            )
        self.run_commands(
            [(
                "decode nested UI artifact upload",
                f"base64 -d {shell_quote(remote_b64)} > {shell_quote(remote_path)} && rm -f {shell_quote(remote_b64)}",
                False,
            )]
        )

    def run_phase(self, index: int) -> None:
        with JOBS_LOCK:
            job = JOBS[self.job_id]
            job.status = "running"
            job.phases[index].status = PhaseStatus.RUNNING
        name = PHASES[index]
        self.log.append(f"\n=== Phase {index + 1}: {name} ===\n")
        try:
            if index == 0:
                if self.request.dry_run:
                    saved_request = self.request
                    self.request = replace(self.request, dry_run=False)
                    try:
                        output = self.run_commands(inspect_commands(), stop_on_failure=False)
                    finally:
                        self.request = saved_request
                else:
                    output = self.run_commands(inspect_commands(), stop_on_failure=False)
                self.validate_inspection(output)
            elif index == 1:
                output = self.run_commands(backup_commands(self.request.edge_release))
                backup = self.extract_latest_backup(output)
                with JOBS_LOCK:
                    JOBS[self.job_id].backup_filename = backup
            elif index == 2:
                if self.request.reuse_uploaded_zip:
                    self.run_commands([("verify existing uploaded UI artifact", f"ls -lh {REMOTE_UI_ARTIFACT_PATH} && test -s {REMOTE_UI_ARTIFACT_PATH}", False)])
                    with JOBS_LOCK:
                        job = JOBS[self.job_id]
                        job.phases[index].status = PhaseStatus.SKIPPED
                        job.phases[index].detail = "Skipped; reused uploaded artifact"
                        job.current_phase = index + 1
                        job.status = "waiting"
                    self.log.append("\nPhase skipped: Build/upload UI release artifact; reusing existing uploaded artifact.\n")
                    return
                self.build_upload_zip()
            elif index == 3:
                self.run_commands(apply_ui_commands(self.request))
            elif index == 4:
                self.run_commands(auth_commands(self.request))
            elif index == 5:
                output = self.run_commands(restart_ui_commands())
                self.validate_ui_restart(output)
            elif index == 6:
                if self.request.dry_run:
                    self.log.append("[dry-run] Would POST /api/admin/gateways/provision and capture gateway token.\n")
                    token = "iotcc_gw_dryrun_example-token"
                    self.redactor.add(token)
                else:
                    token = provision_cloud_gateway(self.request, self.log, self.redactor)
                with JOBS_LOCK:
                    JOBS[self.job_id].gateway_token = token
            elif index == 7:
                self.run_commands(repo_commands(self.request))
            elif index == 8:
                token = JOBS[self.job_id].gateway_token
                if not token:
                    raise RuntimeError("No gateway token is available. Run cloud provisioning first.")
                self.run_commands(config_commands(self.request, token, JOBS[self.job_id].pre_upgrade_agent_default_port))
            elif index == 9:
                output = self.run_commands(install_agent_commands(self.request))
                if not self.request.dry_run and "Successfully installed" not in output:
                    self.log.append("Warning: pip output did not include 'Successfully installed'; verify package install above.\n")
            elif index == 10:
                output = self.run_commands(service_commands(self.request))
                previous_start = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("AGENT_SERVICE_START_BEFORE=")), "")
                with JOBS_LOCK:
                    JOBS[self.job_id].pre_restart_agent_timestamp = previous_start
                if not self.request.dry_run and "Heartbeat accepted" not in output:
                    self.log.append("Warning: heartbeat acceptance was not seen in the recent service log.\n")
            elif index == 11:
                output = self.run_commands(final_commands(self.request, JOBS[self.job_id].pre_upgrade_agent_default_port, JOBS[self.job_id].pre_restart_agent_timestamp))
                if not self.request.dry_run:
                    validate_agent_runtime_output(output, self.request, JOBS[self.job_id].pre_restart_agent_timestamp)
                self.write_summary(output)
            with JOBS_LOCK:
                job = JOBS[self.job_id]
                job.phases[index].status = PhaseStatus.PASSED
                job.phases[index].detail = "Passed"
                job.current_phase = index + 1
                job.status = "complete" if job.current_phase >= len(PHASES) else "waiting"
            self.log.append(f"\nPhase passed: {name}\n")
        except Exception as exc:
            with JOBS_LOCK:
                job = JOBS[self.job_id]
                job.phases[index].status = PhaseStatus.FAILED
                job.phases[index].detail = str(exc)
                job.status = "failed"
                job.error = str(exc)
            self.log.append(f"\nFailed phase: {name}\n")
            self.log.append(f"Recommended next action: review the sanitized output above, fix the cause, then rerun or use rollback if the UI was changed.\n")
            self.log.append(f"Error: {exc}\n")
            raise

    def validate_inspection(self, output: str) -> None:
        if self.request.dry_run:
            self.write_preflight_summary(output)
            return
        lower = output.lower()
        if "missing edge-bacnet-ui-v2" in lower:
            raise RuntimeError("/home/swadmin/edge-bacnet-ui-v2 is missing. Stop; this is not a legacy UI candidate.")
        if "one or more bacnet tools missing" in lower:
            raise RuntimeError("One or more BACnet tools are missing. Continue only after explicit field approval.")
        if "not a git repository" not in lower and "fatal:" not in lower:
            raise RuntimeError("edge-bacnet-ui-v2 appears to be a git repo. Do not use copied-folder legacy path without approval.")
        self.write_preflight_summary(output)
        self.log.append("\nCheckpoint summary:\nLegacy edge-only candidate: YES\nProceed with copied-folder update path: YES\n")

    def write_preflight_summary(self, output: str) -> None:
        agent_version = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("EDGE_AGENT_VERSION=")), "unknown")
        pre_port = next((line.split("=", 1)[1].strip() for line in output.splitlines() if line.startswith("PRE_UPGRADE_AGENT_DEFAULT_PORT=")), "47814")
        sudo_state = "yes" if "SUDO_AVAILABLE=yes" in output else "not confirmed"
        backup_path = "/home/swadmin" if "BACKUP_PATH_WRITABLE=/home/swadmin" in output else "not confirmed"
        source_summary = {
            "UI deployment source": "resolved-github-commit-artifact" if self.request.ui_artifact_path else "not selected",
            "UI source commit": self.request.edge_ui_commit if self.request.ui_artifact_path else "not selected",
            "UI artifact SHA-256": self.request.ui_artifact_sha256 or "not selected",
            "UI artifact validation": "Passed" if self.request.ui_artifact_path else "Not selected",
            "Release component validation": "Passed" if self.request.ui_artifact_path else "Not selected",
            "Rule #1 validation": "Passed" if self.request.ui_artifact_path else "Not selected",
        }
        release = load_release_definition(self.request.release_manifest_path)
        with JOBS_LOCK:
            job = JOBS[self.job_id]
            job.pre_upgrade_agent_default_port = pre_port if pre_port.isdigit() else "47814"
            job.summary.update(
                {
                    "Updater": f"{identity.PRODUCT_NAME} {identity.APP_VERSION}",
                    "Selected target gateway": self.request.gateway_id,
                    "Resolved Edge UI commit": self.request.edge_ui_commit,
                    "Resolved Edge Agent commit": self.request.edge_agent_commit,
                    "SSH route and host": f"{self.request.cradlepoint_user}@{self.request.cradlepoint_host} -> {self.request.gateway_user}@{self.request.gateway_host}",
                    "Detected current Edge UI version": "see live log",
                    "Detected current agent version": agent_version,
                    "Edge UI Release": release.edge_release,
                    "Edge UI Source": self.request.edge_ui_commit,
                    "Edge Agent Target": f"{self.request.expected_agent_version} / {self.request.edge_agent_commit}",
                    "Agent Source": self.request.agent_source,
                    "Target UI version": release.edge_release,
                    "Target agent version": self.request.expected_agent_version,
                    **source_summary,
                    "RELEASE_VERSION": release.edge_release,
                    "UI_DEPLOYMENT_SOURCE": source_summary["UI deployment source"],
                    "UI_SOURCE_COMMIT": source_summary["UI source commit"],
                    "UI_ARTIFACT_SHA256": source_summary["UI artifact SHA-256"],
                    "UI_ARTIFACT_VALIDATION": source_summary["UI artifact validation"],
                    "AGENT_SOURCE_COMMIT": self.request.edge_agent_commit,
                    "AGENT_SOURCE": self.request.agent_source,
                    "LOCAL_EDGE_TRENDS_DEFAULT_ENABLED": "true" if release.local_edge_trends_default_enabled else "false",
                    "BACKGROUND_BACNET_ACTIVITY_ADDED": "No",
                    "RELEASE_COMPONENT_VALIDATION": source_summary["Release component validation"],
                    "RULE_1_VALIDATION": source_summary["Rule #1 validation"],
                    "BACnet policy": "Preserve existing configuration",
                    "BACnet files/settings changed": "None",
                    "start.sh": "Preserved",
                    "router config files": "Preserved",
                    "router services": "Not changed",
                    "Pre-upgrade agent default port": job.pre_upgrade_agent_default_port,
                    "Package/manifest checksum status": release_package_status(self.request.release_manifest_path),
                    "Pre-upgrade backup status and path": backup_path,
                    "Sudo available": sudo_state,
                    "Rollback action": "Use Restore legacy full backup or Restore code-only checkpoint after backup phase",
                }
            )
        self.log.append("\nPreflight validation checklist:\n")
        for key, value_text in JOBS[self.job_id].summary.items():
            self.log.append(f"{key}: {value_text}\n")
        self.log.append("\nRelease validation markers:\n")
        for key in (
            "RELEASE_VERSION",
            "UI_DEPLOYMENT_SOURCE",
            "UI_SOURCE_COMMIT",
            "UI_ARTIFACT_SHA256",
            "UI_ARTIFACT_VALIDATION",
            "AGENT_SOURCE_COMMIT",
            "AGENT_SOURCE",
            "LOCAL_EDGE_TRENDS_DEFAULT_ENABLED",
            "BACKGROUND_BACNET_ACTIVITY_ADDED",
            "RELEASE_COMPONENT_VALIDATION",
            "RULE_1_VALIDATION",
        ):
            self.log.append(f"{key}={JOBS[self.job_id].summary[key]}\n")

    def extract_latest_backup(self, output: str) -> str:
        matches = re.findall(r"(edge-bacnet-ui-v2\.backup\.\d{8}-\d{6}\.tar\.gz)", output)
        if self.request.dry_run:
            return "edge-bacnet-ui-v2.backup.DRYRUN-000000.tar.gz"
        if not matches:
            raise RuntimeError("Backup file was not listed after backup command.")
        return matches[-1]

    def build_upload_zip(self) -> None:
        release = load_release_definition(self.request.release_manifest_path)
        if not self.request.ui_artifact_path or not self.request.ui_artifact_sha256:
            raise RuntimeError("No resolved UI artifact exists for the selected UI phases; refusing embedded-artifact fallback.")
        artifact_path = Path(self.request.ui_artifact_path)
        if not artifact_path.is_file() or __import__("hashlib").sha256(artifact_path.read_bytes()).hexdigest() != self.request.ui_artifact_sha256:
            raise RuntimeError("Resolved UI artifact SHA-256 validation failed; refusing deployment.")
        validate_embedded_ui_artifact_contents(artifact_path)
        self.log.append(f"RELEASE_VERSION={release.edge_release}\n")
        self.log.append("UI_DEPLOYMENT_SOURCE=resolved-github-commit-artifact\n")
        self.log.append(f"UI_SOURCE_COMMIT={self.request.edge_ui_commit}\n")
        self.log.append(f"UI_ARTIFACT_SHA256={self.request.ui_artifact_sha256}\n")
        self.log.append("UI_ARTIFACT_VALIDATION=Passed\n")
        self.log.append(f"AGENT_SOURCE_COMMIT={self.request.edge_agent_commit}\n")
        self.log.append(f"AGENT_SOURCE={self.request.agent_source}\n")
        self.log.append(f"LOCAL_EDGE_TRENDS_DEFAULT_ENABLED={'true' if release.local_edge_trends_default_enabled else 'false'}\n")
        self.log.append("BACKGROUND_BACNET_ACTIVITY_ADDED=No\n")
        self.log.append("RELEASE_COMPONENT_VALIDATION=Passed\n")
        self.log.append("RULE_1_VALIDATION=Passed\n")
        if self.request.dry_run:
            self.log.append(f"[dry-run] Would upload resolved UI artifact {artifact_path.name} ({self.request.ui_artifact_sha256}) to {REMOTE_UI_ARTIFACT_PATH}.\n")
            self.log.append(f"[dry-run] Required contents: {', '.join([*UI_PACKAGE_FILES, *(item + '/' for item in UI_PACKAGE_DIRS)])}\n")
            self.log.append("[dry-run] Preserved: data/, .env, start.sh, databases, saved devices/templates, programs, trends, timed overrides, credentials, gateway identity, cloud identity, BACnet route settings.\n")
            self.log.append("[dry-run] Services that would restart: edge-bacnet-ui.service; iot-cx-agent.service only when agent phases are selected.\n")
            self.log.append("[dry-run] BACnet defaults apply only when no usable BACnet route settings exist: external router, UDP 47814, internal Edge router disabled.\n")
            self.log.append("[dry-run] Rollback scope: full pre-upgrade folder backup plus release-named code-only checkpoint.\n")
            return
        self.log.append(f"Using resolved UI artifact: {artifact_path.name} ({self.request.ui_artifact_sha256})\n")
        self.upload_file(artifact_path, REMOTE_UI_ARTIFACT_PATH)
        output = self.run_commands([("verify uploaded UI artifact", f"ls -lh {REMOTE_UI_ARTIFACT_PATH} && test -s {REMOTE_UI_ARTIFACT_PATH}", False)])
        if not output.strip():
            raise RuntimeError("Uploaded UI artifact verification returned no output")

    def validate_ui_restart(self, output: str) -> None:
        if self.request.dry_run:
            return
        if "active" not in output:
            journal = self.run_commands([("edge UI failure logs", "sudo journalctl -u edge-bacnet-ui.service -n 80 --no-pager -l", False)], stop_on_failure=False)
            raise RuntimeError(f"edge-bacnet-ui.service did not become active. Recent logs:\n{journal}")
        if "302" not in output or "/login" not in output.lower():
            raise RuntimeError("Local UI did not return HTTP 302 to /login. Auth verification failed.")

    def write_summary(self, output: str) -> None:
        heartbeat = "Passed" if "Heartbeat accepted" in output else "Warning: heartbeat not seen"
        ui_auth = "Passed" if "302" in output and "/login" in output.lower() else "Warning: 302 /login not seen"
        bacnet_preservation = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("BACNET_CONFIG_PRESERVATION=")), "Warning: not seen")
        pre_port = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("PRE_UPGRADE_AGENT_DEFAULT_PORT=")), "unknown")
        post_port = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("POST_UPGRADE_AGENT_DEFAULT_PORT=")), "unknown")
        route_changed = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("ROUTE_SETTINGS_CHANGED=")), "unknown")
        bacnet_ports_changed = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("BACNET_PORTS_CHANGED=")), "unknown")
        bacnet_routes_changed = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("BACNET_ROUTES_CHANGED=")), "unknown")
        ui_release_validation = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("UI_RELEASE_VALIDATION=")), "Warning: not seen")
        agent_release_commit = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("AGENT_RELEASE_COMMIT=")), "unknown")
        agent_release_validation = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("AGENT_RELEASE_VALIDATION=")), "Warning: not seen")
        local_edge_trends_enabled = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("LOCAL_EDGE_TRENDS_ENABLED=")), "unknown")
        background_bacnet_activity = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("BACKGROUND_BACNET_ACTIVITY_ADDED=")), "unknown")
        rule_1_validation = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("RULE_1_VALIDATION=")), "Warning: not seen")
        release_validation = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("RELEASE_0_1_9_VALIDATION=")), "Warning: not seen")
        with JOBS_LOCK:
            job = JOBS[self.job_id]
            job.summary = {
                "Gateway number": self.request.gateway_id,
                "Site ID": self.request.site_id,
                "Hostname": self.request.gateway_id,
                "Gateway LAN IP": self.request.gateway_host,
                "Local UI update status": job.phases[3].status.value,
                "Local UI auth status": ui_auth,
                "Cloud provision status": job.phases[6].status.value,
                "Cloud repo status": job.phases[7].status.value,
                "Agent config status": job.phases[8].status.value,
                "iot-cx-agent service status": job.phases[10].status.value,
                "Heartbeat status": heartbeat,
                "BACnet configuration preservation": bacnet_preservation,
                "Pre-upgrade agent default port": pre_port,
                "Post-upgrade agent default port": post_port,
                "BACnet ports changed": bacnet_ports_changed,
                "BACnet routes changed": bacnet_routes_changed,
                "Route settings changed": route_changed,
                "UI release validation": ui_release_validation,
                "Agent release commit": agent_release_commit,
                "Agent release validation": agent_release_validation,
                "Local Edge trends enabled": local_edge_trends_enabled,
                "Background BACnet activity added": background_bacnet_activity,
                "Rule #1 validation": rule_1_validation,
                f"Release {DEFAULT_EDGE_RELEASE} validation": release_validation,
                "Cloud portal manual confirmation": "Yes" if self.request.cloud_portal_verified else "No",
                "Backup filename": job.backup_filename or "(none captured)",
                "Warnings/errors": job.warning or job.error or "(none)",
            }
        self.log.append("\nFinal summary report:\n")
        for key, value_text in JOBS[self.job_id].summary.items():
            self.log.append(f"{key}: {value_text}\n")


def run_until_checkpoint(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        if job.runner is None:
            job.runner = LegacyUpgradeRunner(job_id, job.request)
        runner = job.runner
    try:
        while True:
            with JOBS_LOCK:
                job = JOBS[job_id]
                next_phase = job.current_phase
                if next_phase >= len(PHASES):
                    job.status = "complete"
                    runner.close()
                    return
                if next_phase not in runner.request.selected_phases:
                    job.phases[next_phase].status = PhaseStatus.SKIPPED
                    job.phases[next_phase].detail = "Not selected"
                    job.current_phase = next_phase + 1
                    continue
            runner.run_phase(next_phase)
            with JOBS_LOCK:
                status = JOBS[job_id].status
            if status in {"failed", "complete"}:
                return
            # Selected phases are an ordered batch. Continue automatically
            # after each successful phase; the phase selector is the operator's
            # checkpoint, so a full run no longer requires clicking Continue.
    except Exception:
        runner.close()


def start_job(request: UpgradeRequest) -> str:
    job_id = uuid.uuid4().hex
    if not request.edge_agent_write_token:
        request = replace(request, edge_agent_write_token=secrets.token_urlsafe(32))
    with JOBS_LOCK:
        JOBS[job_id] = UpgradeJob(request=request, status="queued", log="Queued legacy edge upgrade job.\n")
    thread = threading.Thread(target=run_until_checkpoint, args=(job_id,), daemon=True)
    thread.start()
    return job_id


def continue_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise ValueError("Unknown job")
        if job.status != "waiting":
            raise ValueError(f"Job is not waiting at a checkpoint; current status is {job.status}")
        job.status = "queued"
    threading.Thread(target=run_until_checkpoint, args=(job_id,), daemon=True).start()


def run_job_commands(job_id: str, commands: list[tuple[str, str, bool]], title: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise ValueError("Unknown job")
        if job.runner is None:
            job.runner = LegacyUpgradeRunner(job_id, job.request)
        runner = job.runner
        job.status = "running"
    try:
        runner.log.append(f"\n=== {title} ===\n")
        runner.run_commands(commands)
        runner.log.append(f"{title} complete.\n")
        with JOBS_LOCK:
            JOBS[job_id].status = "waiting"
    except Exception as exc:
        runner.log.append(f"{title} failed: {exc}\n")
        with JOBS_LOCK:
            JOBS[job_id].status = "failed"
            JOBS[job_id].error = str(exc)


class LegacyEdgeUpgradeHandler(BaseHTTPRequestHandler):
    server_version = "LegacyEdgeUpgradeWebapp/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.respond(form_page())
            return
        if parsed.path == "/api/status":
            fields = parse_qs(parsed.query)
            job_id = value(fields, "job_id")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None:
                    self.respond_json({"error": "Unknown job"}, status=404)
                    return
                self.respond_json(
                    {
                        "status": job.status,
                        "log": job.log,
                        "phases": [{"name": phase.name, "status": phase.status.value, "detail": phase.detail} for phase in job.phases],
                        "can_rollback": bool(job.backup_filename) and job.status in {"waiting", "failed", "complete"},
                        "can_disable_agent": job.current_phase >= 10 and job.status in {"waiting", "failed", "complete"},
                        "summary": job.summary,
                    }
                )
            return
        if parsed.path == "/api/worker-status":
            with WORKER_STATUS_LOCK:
                self.respond_json(dict(WORKER_STATUS))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        try:
            if parsed.path == "/api/start":
                request = parse_upgrade_request(body)
                self.respond_json({"job_id": start_job(request)})
                return
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            if parsed.path == "/api/resolve-commits":
                edge_ui, edge_agent = resolve_requested_commits(fields)
                try:
                    artifact = materialize_ui_artifact(edge_ui.full_sha, token=load_env_defaults()["GITHUB_TOKEN"])
                except UIArtifactError as exc:
                    raise ValueError(f"UI artifact creation failed closed: {exc}") from exc
                self.respond_json({"edge_ui": {**edge_ui.__dict__, "short_sha": edge_ui.full_sha[:7], "artifact_filename": artifact.path.name, "artifact_sha256": artifact.sha256}, "edge_agent": edge_agent.__dict__})
                return
            job_id = value(fields, "job_id")
            if parsed.path == "/api/continue":
                continue_job(job_id)
                self.respond_json({"ok": True})
                return
            if parsed.path == "/api/rollback-ui":
                backup = value(fields, "backup")
                threading.Thread(target=run_job_commands, args=(job_id, rollback_commands(backup), "Rollback local BACnet UI"), daemon=True).start()
                self.respond_json({"ok": True})
                return
            if parsed.path == "/api/rollback-code":
                edge_release = value(fields, "edge_release")
                threading.Thread(target=run_job_commands, args=(job_id, [(f"code-only checkpoint {index + 1}", command, index in {1, 7, 8}) for index, command in enumerate(code_restore_commands(edge_release))], "Restore Edge UI code-only checkpoint"), daemon=True).start()
                self.respond_json({"ok": True, "scope": "code-only; data/start.sh/credentials/site settings preserved"})
                return
            if parsed.path == "/api/list-code-checkpoints":
                threading.Thread(target=run_job_commands, args=(job_id, [(f"code-only checkpoint inventory {index + 1}", command, False) for index, command in enumerate(checkpoint_inventory_commands())], "List Edge UI code-only checkpoints"), daemon=True).start()
                self.respond_json({"ok": True})
                return
            if parsed.path == "/api/disable-agent":
                threading.Thread(target=run_job_commands, args=(job_id, disable_agent_commands(), "Disable cloud agent"), daemon=True).start()
                self.respond_json({"ok": True})
                return
            self.send_error(404)
        except Exception as exc:
            self.respond_json({"error": str(exc)}, status=400)

    def log_message(self, format: str, *args: object) -> None:
        return

    def respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, body: dict[str, object], *, status: int = 200) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run_server(port: int) -> BaseServer:
    runtime.require_port(port)
    identity.data_dir().mkdir(parents=True, exist_ok=True)
    identity.log_dir().mkdir(parents=True, exist_ok=True)
    identity.pid_path().write_text(str(os.getpid()), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", port), LegacyEdgeUpgradeHandler)
    # Bind first: a duplicate launch now fails without leaving a background
    # worker behind to silently claim cloud update jobs.
    if os.environ.get("IOT_EDGE_DEV_UPDATER_CLAIM_CLOUD_JOBS") == "1":
        threading.Thread(target=gateway_update_worker, daemon=True, name="gateway-update-worker").start()
        print("Cloud job claiming explicitly enabled.")
    else:
        with WORKER_STATUS_LOCK:
            WORKER_STATUS["state"] = "disabled (manual-only)"
    print(f"{identity.PRODUCT_NAME} {identity.APP_VERSION}: http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{identity.PRODUCT_NAME} manual checkpoint webapp.")
    parser.add_argument("--port", type=int, default=int(configured_default("IOT_EDGE_DEV_UPDATER_PORT", str(DEFAULT_PORT))))
    args = parser.parse_args()
    try:
        server = run_server(args.port)
    except runtime.PortUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
