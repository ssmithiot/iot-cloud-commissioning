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
import tempfile
import threading
import time
import uuid
import zipfile
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse

try:
    import paramiko
except ImportError:  # pragma: no cover - shown in browser and terminal at runtime
    paramiko = None


DEFAULT_PORT = 8766
DEFAULT_CLOUD_URL = "https://iot-cloud-api-dev.onrender.com"
DEFAULT_REPO_PATH = "/home/swadmin/iot-cloud-commissioning"
DEFAULT_UI_SOURCE = r"C:\Dev\edge-bacnet-ui-v2"
REMOTE_UI_PATH = "/home/swadmin/edge-bacnet-ui-v2"
REMOTE_ZIP_PATH = "/home/swadmin/edge-bacnet-ui-v2-update.zip"
REMOTE_REPO_URL = "https://github.com/ssmithiot/iot-cloud-commissioning.git"
NESTED_UPLOAD_CHUNK_SIZE = 3000
# Manual Cloud-triggered 0.1.4 updates replace both halves of the local handoff:
# the proven edge UI writer and the agent that delegates queued jobs to it.
# Nothing polls this list to auto-update a gateway when it reconnects.
UPDATE_AGENT_PHASES = (0, 1, 2, 3, 4, 5, 7, 9, 10, 11)
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
    "Build/upload UI update ZIP",
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
    selected_phases: tuple[int, ...] = tuple(range(len(PHASES)))
    edge_agent_write_token: str = ""


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


class LiveLog:
    def __init__(self, job_id: str, redactor: Redactor) -> None:
        self.job_id = job_id
        self.redactor = redactor

    def append(self, text: str) -> None:
        safe = self.redactor.redact(text)
        print(safe, end="", flush=True)
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
    }
    env_paths = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    env_path = next((path for path in env_paths if path.exists()), None)
    if env_path is None:
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


def run_queued_gateway_update(update: dict[str, object], defaults: dict[str, str]) -> None:
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
        return

    if not isinstance(claimed, dict):
        return
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
        git_ref=os.environ.get("IOT_EDGE_UPDATE_REF", "main"),
        remote_repo=DEFAULT_REPO_PATH,
        ui_source_folder=DEFAULT_UI_SOURCE,
        ui_username=os.environ.get("EDGE_UI_USERNAME", "admin"),
        ui_password=defaults["EDGE_UI_PASSWORD"],
        cloud_portal_verified=True,
        selected_phases=UPDATE_AGENT_PHASES,
    )
    if not request.cradlepoint_host:
        cloud_json_request(
            cloud_url,
            admin_api_token,
            f"/api/admin/gateway-updates/{request_id}/complete",
            method="POST",
            body={"status": "failed", "error_message": "No Cradlepoint host is configured for this gateway."},
        )
        return

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
            return
        time.sleep(2)


def gateway_update_worker() -> None:
    poll_seconds = max(5, int(os.environ.get("IOT_EDGE_UPDATE_POLL_SECONDS", "10")))
    while True:
        with WORKER_STATUS_LOCK:
            WORKER_STATUS["last_poll_at"] = datetime.now(timezone.utc).isoformat()
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
                        run_queued_gateway_update(update, defaults)
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
    return page(
        "Legacy Edge Upgrade",
        f"""
<h1>Legacy Edge Upgrade</h1>
<p>Upgrade older edge-only gateways through the Cradlepoint jump host. This is separate from IOTGWCFG and defaults to checkpoint mode.</p>
{warning}
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
  <label>Git ref
    <input name="git_ref" value="main" required>
  </label>
  <label class="wide">Repo path on gateway
    <input name="remote_repo" value="{DEFAULT_REPO_PATH}" required>
  </label>
  <label class="wide">Local BACnet UI source folder on Windows
    <input name="ui_source_folder" value="{escape(DEFAULT_UI_SOURCE, quote=True)}" required>
  </label>
  <label>Local BACnet UI username
    <input name="ui_username" value="admin" required>
  </label>
  <label>Local BACnet UI password
    <input type="password" name="ui_password" value="{ui_password}" autocomplete="off" required>
  </label>
  <div class="wide checks">
    <label><input type="checkbox" name="dry_run" value="1"> Dry run</label>
    <label><input type="checkbox" name="reuse_uploaded_zip" value="1"> Reuse uploaded UI ZIP</label>
    <label><input type="checkbox" name="skip_edge_ui_stop" value="1"> Edge UI already stopped / skip stop</label>
    <label><input type="checkbox" name="cloud_portal_verified" value="1"> Cloud portal verified</label>
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
    <button id="start-button" type="submit">Start inspect</button>
  </div>
</form>
<div class="layout">
  <section class="panel">
    <h2>Phases</h2>
    <div id="phases"></div>
    <div class="actions">
      <button id="continue-button" class="secondary" type="button" disabled>Continue</button>
      <button id="rollback-button" class="danger" type="button" disabled>Rollback UI</button>
      <button id="disable-agent-button" class="danger" type="button" disabled>Disable Agent</button>
    </div>
  </section>
  <section>
    <h2>Live Log</h2>
    <pre id="log">Ready.</pre>
  </section>
</div>
<script>
const form = document.getElementById("upgrade-form");
const startButton = document.getElementById("start-button");
const continueButton = document.getElementById("continue-button");
const rollbackButton = document.getElementById("rollback-button");
const disableAgentButton = document.getElementById("disable-agent-button");
const log = document.getElementById("log");
const phaseChecks = () => [...document.querySelectorAll('input[name="selected_phases"]')];
document.getElementById("select-all-phases").addEventListener("click", () => phaseChecks().forEach((input) => input.checked = true));
document.getElementById("clear-all-phases").addEventListener("click", () => phaseChecks().forEach((input) => input.checked = false));
const phases = document.getElementById("phases");
let pollTimer = null;
let jobId = null;

function setLog(text) {{
  log.textContent = text || "";
  log.scrollTop = log.scrollHeight;
}}

function renderPhases(items) {{
  phases.innerHTML = items.map((p) => `<div class="phase"><span>${{p.name}}</span><span class="badge ${{p.status.replaceAll(" ", "_")}}">${{p.status}}</span></div>`).join("");
}}

async function poll() {{
  if (!jobId) return;
  const response = await fetch(`/api/status?job_id=${{encodeURIComponent(jobId)}}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${{response.status}}`);
  setLog(body.log);
  renderPhases(body.phases || []);
  continueButton.disabled = body.status !== "waiting";
  rollbackButton.disabled = !body.can_rollback;
  disableAgentButton.disabled = !body.can_disable_agent;
  startButton.disabled = body.status === "running" || body.status === "waiting";
  startButton.textContent = body.status === "complete" || body.status === "failed" ? "Start new run" : "Start inspect";
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


def parse_upgrade_request(body: bytes) -> UpgradeRequest:
    fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    selected_raw = fields.get("selected_phases")
    selected_phases = tuple(sorted({int(value) for value in (selected_raw or []) if value.isdigit() and 0 <= int(value) < len(PHASES)}))
    gateway_id = value(fields, "gateway_id")
    ui_password = value(fields, "ui_password")
    if "'" in ui_password:
        raise ValueError("Local BACnet UI password cannot contain a single quote for this legacy update flow.")
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
        git_ref=value(fields, "git_ref") or "main",
        remote_repo=value(fields, "remote_repo") or DEFAULT_REPO_PATH,
        ui_source_folder=value(fields, "ui_source_folder") or DEFAULT_UI_SOURCE,
        ui_username=value(fields, "ui_username") or "admin",
        ui_password=ui_password,
        dry_run=parse_bool(fields, "dry_run"),
        reuse_uploaded_zip=parse_bool(fields, "reuse_uploaded_zip"),
        skip_edge_ui_stop=parse_bool(fields, "skip_edge_ui_stop"),
        cloud_portal_verified=parse_bool(fields, "cloud_portal_verified"),
        selected_phases=selected_phases,
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


def update_start_sh_command(username: str, password: str) -> str:
    script = f"""
from pathlib import Path
path = Path("/home/swadmin/edge-bacnet-ui-v2/start.sh")
text = path.read_text()
settings = {{
    "BACNET_IP_PORT": "47814",
    "AUTH_ENABLED": "1",
    "EDGE_UI_USERNAME": {username!r},
    "EDGE_UI_PASSWORD": {password!r},
}}
lines = text.splitlines()
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
out[insert_at:insert_at] = missing
path.write_text("\\n".join(out) + "\\n")
"""
    return "python3 -c " + shell_quote(script)


def inspect_commands() -> list[tuple[str, str, bool]]:
    return [
        ("hostname", "hostname", False),
        ("network addresses", "ip -br addr", False),
        ("legacy UI folder", f'ls -ld {REMOTE_UI_PATH} 2>/dev/null || echo "missing edge-bacnet-ui-v2"', False),
        ("cloud repo folder", f'ls -ld {DEFAULT_REPO_PATH} 2>/dev/null || echo "missing iot-cloud-commissioning"', False),
        ("unit files", "systemctl list-unit-files | grep -Ei 'iot|cx|bacnet|edge' || true", False),
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
            r"""grep -nE 'BACNET_IP_PORT|AUTH_ENABLED|EDGE_UI_USERNAME|EDGE_UI_PASSWORD|RPM_BLOCK_SIZE|RPM_VIEW_BLOCK_SIZE|DEFAULT_SCAN_LIMIT|MAX_OBJECTS' /home/swadmin/edge-bacnet-ui-v2/start.sh | sed -E "s/(EDGE_UI_PASSWORD=).*/\1'***SET***'/" || true""",
            False,
        ),
    ]


def backup_commands() -> list[tuple[str, str, bool]]:
    return [
        ("edge UI enabled", "systemctl is-enabled edge-bacnet-ui.service 2>/dev/null || true", False),
        ("edge UI active", "systemctl is-active edge-bacnet-ui.service 2>/dev/null || true", False),
        ("create UI backup", 'cd /home/swadmin && tar -czf "edge-bacnet-ui-v2.backup.$(date +%Y%m%d-%H%M%S).tar.gz" edge-bacnet-ui-v2', False),
        ("list UI backups", "cd /home/swadmin && ls -lh edge-bacnet-ui-v2.backup.*.tar.gz", False),
    ]


def apply_ui_commands(request: UpgradeRequest) -> list[tuple[str, str, bool]]:
    normalize = r"""python3 -c 'import zipfile,pathlib; src=pathlib.Path("/home/swadmin/edge-bacnet-ui-v2-update.zip"); dest=pathlib.Path("/tmp/edge-bacnet-ui-v2-update"); z=zipfile.ZipFile(src); [((dest / pathlib.PurePosixPath(i.filename.replace("\\","/"))).parent.mkdir(parents=True, exist_ok=True), (dest / pathlib.PurePosixPath(i.filename.replace("\\","/"))).write_bytes(z.read(i))) for i in z.infolist() if i.filename and not i.filename.replace("\\","/").endswith("/")]; z.close()'"""
    stop_command = (
        ("skip edge UI stop", "echo 'Skipping edge UI stop because Edge UI already stopped / skip stop is checked.'", False)
        if request.skip_edge_ui_stop
        else ("stop edge UI", stop_edge_ui_command(), True)
    )
    return [
        ("prepare normalized extraction folder", "rm -rf /tmp/edge-bacnet-ui-v2-update && mkdir -p /tmp/edge-bacnet-ui-v2-update", False),
        ("extract UI update zip with normalized paths", normalize, False),
        ("verify normalized templates", r"""test -d /tmp/edge-bacnet-ui-v2-update/templates && ! find /tmp/edge-bacnet-ui-v2-update -maxdepth 1 -name 'templates\*' | grep -q . && ls -lah /tmp/edge-bacnet-ui-v2-update/templates""", False),
        stop_command,
        ("confirm edge UI stopped", "systemctl is-active edge-bacnet-ui.service || true", False),
        ("apply UI files", "cp /tmp/edge-bacnet-ui-v2-update/app.py /home/swadmin/edge-bacnet-ui-v2/app.py && cp /tmp/edge-bacnet-ui-v2-update/README.md /home/swadmin/edge-bacnet-ui-v2/README.md && cp /tmp/edge-bacnet-ui-v2-update/requirements.txt /home/swadmin/edge-bacnet-ui-v2/requirements.txt && cp /tmp/edge-bacnet-ui-v2-update/start.sh /home/swadmin/edge-bacnet-ui-v2/start.sh && rm -rf /home/swadmin/edge-bacnet-ui-v2/templates && cp -a /tmp/edge-bacnet-ui-v2-update/templates /home/swadmin/edge-bacnet-ui-v2/templates", False),
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
        ("update start.sh auth and BACnet port", update_start_sh_command(request.ui_username, request.ui_password), False),
        ("write local edge UI adapter token", f"printf %s {shell_quote(token_b64)} | base64 -d > /home/swadmin/edge-bacnet-ui-v2/.edge-agent-write-token && chmod 600 /home/swadmin/edge-bacnet-ui-v2/.edge-agent-write-token", False),
        ("write edge agent adapter token", f"sudo -S -p '' sh -c {shell_quote(agent_env_script)}", True),
        (
            "verify safe start.sh auth",
            r"""grep -nE 'BACNET_IP_PORT|AUTH_ENABLED|EDGE_UI_USERNAME|EDGE_UI_PASSWORD|RPM_BLOCK_SIZE|RPM_VIEW_BLOCK_SIZE|DEFAULT_SCAN_LIMIT|MAX_OBJECTS' /home/swadmin/edge-bacnet-ui-v2/start.sh | sed -E "s/(EDGE_UI_PASSWORD=).*/\1'***SET***'/" """,
            False,
        ),
    ]


def restart_ui_commands() -> list[tuple[str, str, bool]]:
    return [
        ("start edge UI", "sudo -S -p '' systemctl start --no-block edge-bacnet-ui.service", True),
        ("edge UI active check", "sleep 5 && systemctl is-active edge-bacnet-ui.service", False),
        ("local UI HTTP auth check", "curl -I http://127.0.0.1:5000/", False),
    ]


def repo_commands(request: UpgradeRequest) -> list[tuple[str, str, bool]]:
    repo = shell_quote(request.remote_repo)
    ref = shell_quote(request.git_ref)
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
            f"""cd /home/swadmin && if [ -d {repo}/.git ]; then cd {repo} && git remote set-url origin {shell_quote(REMOTE_REPO_URL)} && git fetch origin --tags; else git clone {shell_quote(REMOTE_REPO_URL)} {repo}; cd {repo}; git fetch origin --tags; fi && git checkout {ref} && (git pull --ff-only origin {ref} 2>/dev/null || true)""",
            False,
        ),
        ("repo release commit", f"cd {repo} && git log --oneline -1 && printf 'RELEASE_COMMIT=' && git rev-parse HEAD", False),
        ("repo status clean", f"cd {repo} && git status --porcelain=v1 && test -z \"$(git status --porcelain=v1)\"", False),
    ]


def agent_config_text(request: UpgradeRequest) -> str:
    return f"""gateway_id: {request.gateway_id}
site_id: {request.site_id}
cloud_url: {request.cloud_url}

tunnel_enabled: true
local_ui_url: http://127.0.0.1:5000
tunnel_request_timeout_sec: 900

bacnet_default_port: 47814
heartbeat_interval_sec: 30
agent_version: current
ui_version: current

bacnet:
  default_port: 47814
  bacwi_path: /home/swadmin/bacnet-stack/bin/bacwi
  bacrp_path: /home/swadmin/bacnet-stack/bin/bacrp
  bacrpm_path: /home/swadmin/bacnet-stack/bin/bacrpm
  lock_path: /tmp/iot-cloud-commissioning-bacnet-47814.lock
  timeout_sec: 10
"""


def config_commands(request: UpgradeRequest, gateway_token: str) -> list[tuple[str, str, bool]]:
    agent_b64 = b64(agent_config_text(request))
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
        ("safe config verification", "grep -E 'gateway_id:|site_id:|cloud_url:|local_ui_url:|bacnet_default_port:' /etc/iot-cx-agent/agent.yaml && sudo -S -p '' test -s /etc/iot-cx-agent/edge-agent.env && echo 'GATEWAY_API_TOKEN=***SET***' && ls -ld /var/lib/iot-cx-agent", True),
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
        ("install iot-cx-agent service", f"sudo -S -p '' install -m 0644 {repo}/deploy/iot-cx-agent.service /etc/systemd/system/iot-cx-agent.service", True),
        ("systemd daemon reload", "sudo -S -p '' systemctl daemon-reload", True),
        ("show agent service", "systemctl cat iot-cx-agent.service --no-pager", False),
        ("enable agent service", sudo_systemctl_timeout("enable", "iot-cx-agent.service"), True),
        ("restart agent service", sudo_systemctl_timeout("restart", "iot-cx-agent.service"), True),
        ("agent active check", "sleep 8 && systemctl is-active iot-cx-agent.service", False),
        ("agent logs", "echo 'agent log collection skipped in legacy nested SSH mode; service active check is authoritative here'", False),
    ]


def final_commands() -> list[tuple[str, str, bool]]:
    return [
        ("hostname", "hostname", False),
        ("agent active", "systemctl is-active iot-cx-agent.service", False),
        ("edge UI active", "systemctl is-active edge-bacnet-ui.service", False),
        ("local UI HTTP auth check", "curl -I http://127.0.0.1:5000/", False),
        ("verify supported BACnet tools", "command -v /home/swadmin/bacnet-stack/bin/bacrp && command -v /home/swadmin/bacnet-stack/bin/bacrpm && echo 'bacrp and bacrpm available'", False),
        ("verify MSTP/BACnet profile", "grep -E 'bacnet_default_port:|default_port:|bacrp_path:|bacrpm_path:' /etc/iot-cx-agent/agent.yaml; test \"$(awk '/^bacnet_default_port:/{print $2; exit}' /etc/iot-cx-agent/agent.yaml)\" = 47814", False),
        ("verify tunnel relay timeout", "grep -E '^tunnel_request_timeout_sec:' /etc/iot-cx-agent/agent.yaml; test \"$(awk '/^tunnel_request_timeout_sec:/{print $2; exit}' /etc/iot-cx-agent/agent.yaml)\" = 900", False),
        ("list protected listeners", "timeout -k 5s 15s sh -c \"(sudo -n ss -lntup || ss -lntup) | grep -E ':5000|:47808|:47809|:47814'\" || true", False),
        ("agent final logs", "journalctl -u iot-cx-agent -n 60 --no-pager -l || true", False),
    ]


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


def create_update_zip(source_folder: str) -> Path:
    source = Path(source_folder)
    required = ["app.py", "templates", "README.md", "requirements.txt", "start.sh"]
    missing = [item for item in required if not (source / item).exists()]
    if missing:
        raise RuntimeError(f"Local BACnet UI source folder is missing: {', '.join(missing)}")
    temp_dir = Path(tempfile.mkdtemp(prefix="legacy-edge-upgrade-"))
    zip_path = temp_dir / "edge-bacnet-ui-v2-update.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_name in ["app.py", "README.md", "requirements.txt", "start.sh"]:
            archive.write(source / file_name, arcname=file_name)
        for path in (source / "templates").rglob("*"):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(source).as_posix())
    if zip_path.stat().st_size <= 0:
        raise RuntimeError("Built UI update ZIP is empty")
    return zip_path


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
                    f"upload UI zip chunk {part}/{total_parts}",
                    f"cat >> {shell_quote(remote_b64)} <<'IOTGWCFG_UPLOAD_CHUNK'\n{chunk}\nIOTGWCFG_UPLOAD_CHUNK",
                    False,
                )]
            )
        self.run_commands(
            [(
                "decode nested UI zip upload",
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
                output = self.run_commands(inspect_commands(), stop_on_failure=False)
                self.validate_inspection(output)
            elif index == 1:
                output = self.run_commands(backup_commands())
                backup = self.extract_latest_backup(output)
                with JOBS_LOCK:
                    JOBS[self.job_id].backup_filename = backup
            elif index == 2:
                if self.request.reuse_uploaded_zip:
                    self.run_commands([("verify existing uploaded UI zip", f"ls -lh {REMOTE_ZIP_PATH} && test -s {REMOTE_ZIP_PATH}", False)])
                    with JOBS_LOCK:
                        job = JOBS[self.job_id]
                        job.phases[index].status = PhaseStatus.SKIPPED
                        job.phases[index].detail = "Skipped; reused uploaded ZIP"
                        job.current_phase = index + 1
                        job.status = "waiting"
                    self.log.append("\nPhase skipped: Build/upload UI update ZIP; reusing existing uploaded ZIP.\n")
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
                self.run_commands(config_commands(self.request, token))
            elif index == 9:
                output = self.run_commands(install_agent_commands(self.request))
                if not self.request.dry_run and "Successfully installed" not in output:
                    self.log.append("Warning: pip output did not include 'Successfully installed'; verify package install above.\n")
            elif index == 10:
                output = self.run_commands(service_commands(self.request))
                if not self.request.dry_run and "Heartbeat accepted" not in output:
                    self.log.append("Warning: heartbeat acceptance was not seen in the recent service log.\n")
            elif index == 11:
                output = self.run_commands(final_commands(), stop_on_failure=False)
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
            return
        lower = output.lower()
        if "missing edge-bacnet-ui-v2" in lower:
            raise RuntimeError("/home/swadmin/edge-bacnet-ui-v2 is missing. Stop; this is not a legacy UI candidate.")
        if "one or more bacnet tools missing" in lower:
            raise RuntimeError("One or more BACnet tools are missing. Continue only after explicit field approval.")
        if "not a git repository" not in lower and "fatal:" not in lower:
            raise RuntimeError("edge-bacnet-ui-v2 appears to be a git repo. Do not use copied-folder legacy path without approval.")
        self.log.append("\nCheckpoint summary:\nLegacy edge-only candidate: YES\nProceed with copied-folder update path: YES\n")

    def extract_latest_backup(self, output: str) -> str:
        matches = re.findall(r"(edge-bacnet-ui-v2\.backup\.\d{8}-\d{6}\.tar\.gz)", output)
        if self.request.dry_run:
            return "edge-bacnet-ui-v2.backup.DRYRUN-000000.tar.gz"
        if not matches:
            raise RuntimeError("Backup file was not listed after backup command.")
        return matches[-1]

    def build_upload_zip(self) -> None:
        if self.request.dry_run:
            zip_path = Path(tempfile.gettempdir()) / "edge-bacnet-ui-v2-update.dry-run.zip"
            self.log.append(f"[dry-run] Would build ZIP from {self.request.ui_source_folder} and upload to {REMOTE_ZIP_PATH}.\n")
            self.log.append(f"[dry-run] Required contents: app.py, templates/, README.md, requirements.txt, start.sh\n")
            return
        zip_path = create_update_zip(self.request.ui_source_folder)
        self.log.append(f"Built local UI update ZIP: {zip_path} ({zip_path.stat().st_size} bytes)\n")
        self.upload_file(zip_path, REMOTE_ZIP_PATH)
        output = self.run_commands([("verify uploaded UI zip", f"ls -lh {REMOTE_ZIP_PATH} && test -s {REMOTE_ZIP_PATH}", False)])
        if not output.strip():
            raise RuntimeError("Uploaded ZIP verification returned no output")

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
    server = ThreadingHTTPServer(("127.0.0.1", port), LegacyEdgeUpgradeHandler)
    # Bind first: a duplicate launch now fails without leaving a background
    # worker behind to silently claim cloud update jobs.
    threading.Thread(target=gateway_update_worker, daemon=True, name="gateway-update-worker").start()
    print(f"Legacy Edge Upgrade webapp: http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Local checkpoint webapp for upgrading legacy edge-only gateways.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = run_server(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
