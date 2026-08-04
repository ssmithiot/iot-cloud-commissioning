"""The Development Updater's browser interface.

The same shape of workflow the Legacy Updater proved - a form, a preflight, a
confirmation, a run - wearing an unmistakably different face. The banner, the
colour, and the wording exist so that nobody with both programs open can act on
the wrong one.

The state machine is deliberately small and one-way:

    idle -> connected (read-only preflight done)
         -> armed     (component chosen, target pinned, both confirmations given)
         -> running   -> finished

Nothing advances a stage on its own. Every arrow is a button the operator
presses.
"""
from __future__ import annotations

import html
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tools.dev_updater import identity
from tools.dev_updater.plan import Component, DeploymentPlan, PlanError, build_plan, preflight_steps
from tools.dev_updater.release_source import PinnedTarget, ReleaseSourceError, approved_manifests, resolve_target
from tools.dev_updater.runtime import AuditLog, legacy_port_report


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = REPO_ROOT / "tools" / "releases" / "manifests"


@dataclass
class Session:
    """Everything the operator has chosen so far. Never persisted to disk."""

    gateway_host: str = ""
    gateway_user: str = "swadmin"
    stage: str = "idle"
    component: Component = Component.NONE
    confirmed: bool = False
    agent_confirmed: bool = False
    preflight: dict[str, str] = field(default_factory=dict)
    target: PinnedTarget | None = None
    plan: DeploymentPlan | None = None
    messages: list[tuple[str, str]] = field(default_factory=list)
    checkpoint_status: str = "not taken"
    deployment_status: str = "not started"

    def note(self, kind: str, text: str) -> None:
        self.messages.append((kind, text))

    def reset_confirmations(self) -> None:
        """Any change to what would be deployed invalidates consent."""
        self.confirmed = False
        self.agent_confirmed = False
        self.plan = None
        if self.stage == "armed":
            self.stage = "connected"


SESSION = Session()
SESSION_LOCK = threading.Lock()


# --- rendering ---------------------------------------------------------------

STYLE = """
:root { --dev: #b45309; --dev-bg: #fff7ed; --ink: #111; --muted: #555; --line: #d4d4d8; }
* { box-sizing: border-box; }
body { font-family: Segoe UI, Arial, sans-serif; margin: 0; color: var(--ink); background: #fafafa; }
.banner { background: repeating-linear-gradient(135deg, var(--dev) 0 18px, #92400e 18px 36px);
          color: #fff; padding: 14px 22px; font-weight: 700; letter-spacing: .5px; font-size: 18px; }
.banner small { display: block; font-weight: 400; font-size: 13px; opacity: .95; letter-spacing: 0; margin-top: 3px; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 18px 22px 60px; }
.state { display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 10px; margin: 16px 0 22px; }
.tile { background: #fff; border: 1px solid var(--line); border-left: 4px solid var(--dev); padding: 10px 12px; }
.tile b { display: block; font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: .6px; }
.tile span { font-size: 14px; word-break: break-all; }
.card { background: #fff; border: 1px solid var(--line); padding: 16px 18px; margin-bottom: 16px; }
.card h2 { margin: 0 0 12px; font-size: 16px; }
label { display: inline-block; min-width: 160px; font-size: 14px; }
input[type=text], input[type=password], select { padding: 7px 9px; border: 1px solid var(--line); min-width: 280px; }
button { padding: 9px 16px; border: 1px solid var(--dev); background: var(--dev); color: #fff;
         font-weight: 700; cursor: pointer; }
button.secondary { background: #fff; color: var(--dev); }
button:disabled { opacity: .45; cursor: not-allowed; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f4f4f5; }
.msg { padding: 9px 12px; margin-bottom: 8px; border-left: 4px solid; }
.msg.ok { border-color: #15803d; background: #f0fdf4; }
.msg.error { border-color: #b91c1c; background: #fef2f2; }
.msg.warn { border-color: var(--dev); background: var(--dev-bg); }
.mono { font-family: Consolas, monospace; font-size: 12px; }
.legacy { font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); margin-top: 26px; padding-top: 10px; }
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(session: Session) -> bytes:
    messages = "".join(f'<div class="msg {esc(kind)}">{esc(text)}</div>' for kind, text in session.messages)
    session.messages.clear()

    target = session.target
    tiles = [
        ("Application", f"{identity.PRODUCT_NAME} {identity.APP_VERSION}"),
        ("Selected gateway", session.gateway_host or "none selected"),
        ("Component scope", session.component.label),
        ("Current Edge UI", session.preflight.get("current Edge UI commit", "unknown")),
        ("Current Agent", session.preflight.get("current Agent commit", "unknown")),
        ("Current cloud_url", session.preflight.get("cloud_url (read only)", "unknown")),
        ("Target release", f"{target.edge_release} ({target.release_candidate})" if target else "not resolved"),
        ("Target Edge UI commit", target.edge_ui_commit if target else "not resolved"),
        ("Target Agent commit", target.agent_commit if target else "not resolved"),
        ("Artifact SHA-256", target.artifact_sha256 if target else "not resolved"),
        ("Checkpoint", session.checkpoint_status),
        ("Deployment", session.deployment_status),
    ]
    state = "".join(f'<div class="tile"><b>{esc(label)}</b><span>{esc(value)}</span></div>' for label, value in tiles)

    manifests = approved_manifests(MANIFEST_DIR)
    options = "".join(
        f'<option value="{esc(path.name)}"{" selected" if target and target.manifest_path.name == path.name else ""}>{esc(path.name)}</option>'
        for path in manifests
    ) or '<option value="">no approved development manifest found</option>'

    def checked(component: Component) -> str:
        return " checked" if session.component is component else ""

    preflight_rows = "".join(
        f"<tr><td>{esc(key)}</td><td class='mono'>{esc(value)}</td></tr>"
        for key, value in session.preflight.items()
    ) or "<tr><td colspan='2'>No preflight has been run.</td></tr>"

    plan_rows = ""
    if session.plan:
        plan_rows = "".join(
            f"<tr><td>{esc(step.stage)}</td><td>{esc(step.description)}</td><td class='mono'>{esc(step.command)}</td></tr>"
            for step in session.plan.steps
        )

    warnings = ""
    if session.plan and session.plan.warnings:
        warnings = "".join(f'<div class="msg warn">{esc(text)}</div>' for text in session.plan.warnings)

    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{esc(identity.PRODUCT_NAME)}</title><style>{STYLE}</style></head>
<body>
<div class="banner">{esc(identity.BANNER)}<small>{esc(identity.BANNER_SUBTITLE)}</small></div>
<div class="wrap">
{messages}{warnings}
<div class="state">{state}</div>

<div class="card">
  <h2>1 &middot; Gateway (entered by hand, never discovered)</h2>
  <form method="post" action="/connect">
    <p><label for="host">Gateway address</label><input id="host" type="text" name="host" value="{esc(session.gateway_host)}" placeholder="192.168.1.200" required></p>
    <p><label for="user">SSH user</label><input id="user" type="text" name="user" value="{esc(session.gateway_user)}" required></p>
    <p><label for="password">SSH password</label><input id="password" type="password" name="password" autocomplete="off"></p>
    <p><button type="submit">Run read-only preflight</button>
       <span class="mono">Reads the gateway. Changes nothing.</span></p>
  </form>
</div>

<div class="card">
  <h2>2 &middot; Preflight (read-only)</h2>
  <table><tr><th style="width:260px">Check</th><th>Value</th></tr>{preflight_rows}</table>
</div>

<div class="card">
  <h2>3 &middot; Release target (pinned to exact commits)</h2>
  <form method="post" action="/resolve">
    <p><label for="manifest">Approved manifest</label><select id="manifest" name="manifest">{options}</select></p>
    <p><button type="submit" class="secondary">Resolve and verify artifact</button>
       <span class="mono">Verifies the artifact SHA-256 before anything else.</span></p>
  </form>
</div>

<div class="card">
  <h2>4 &middot; Component scope (no default)</h2>
  <form method="post" action="/component">
    <p><label><input type="radio" name="component" value="ui"{checked(Component.UI_ONLY)}> Edge UI only</label></p>
    <p><label><input type="radio" name="component" value="agent"{checked(Component.AGENT_ONLY)}> Edge Agent only</label></p>
    <p><label><input type="radio" name="component" value="ui+agent"{checked(Component.UI_AND_AGENT)}> Edge UI + Edge Agent</label></p>
    <p><button type="submit" class="secondary">Set scope</button></p>
  </form>
</div>

<div class="card">
  <h2>5 &middot; Confirm (deployment cannot start without this)</h2>
  <form method="post" action="/confirm">
    <p><label><input type="checkbox" name="confirm" value="yes"{" checked" if session.confirmed else ""}>
       I have checked the gateway above and intend to deploy to it.</label></p>
    <p><label><input type="checkbox" name="agent_confirm" value="yes"{" checked" if session.agent_confirmed else ""}>
       Agent updates only: I accept the Agent will restart on its existing cloud_url.</label></p>
    <p><button type="submit">Arm deployment</button></p>
  </form>
</div>

<div class="card">
  <h2>6 &middot; Plan</h2>
  {"<table><tr><th>Stage</th><th>Step</th><th>Command</th></tr>" + plan_rows + "</table>" if plan_rows
   else "<p>No plan. Complete steps 1 to 5.</p>"}
  <form method="post" action="/deploy" style="margin-top:14px">
    <button type="submit"{"" if session.stage == "armed" else " disabled"}>Deploy to {esc(session.gateway_host or "no gateway")}</button>
  </form>
</div>

<div class="card">
  <h2>7 &middot; Rollback</h2>
  <p>Rollback restores code only. Trend data, saved devices, <span class="mono">.env</span>,
     <span class="mono">start.sh</span> and gateway identity are never in the checkpoint and cannot be lost by it.</p>
  <p class="mono">{esc(session.plan.rollback_location if session.plan else "resolve a target to see the checkpoint location")}</p>
  <form method="post" action="/rollback">
    <button type="submit" class="secondary"{"" if session.plan else " disabled"}>Roll back this gateway</button>
  </form>
</div>

<div class="legacy">
  {esc(legacy_port_report())} &middot; this application is on port {esc(identity.DEFAULT_PORT)} and never binds
  the Legacy Updater's port. Logs: <span class="mono">{esc(identity.log_dir())}</span>
</div>
</div></body></html>"""
    return body.encode("utf-8")


# --- request handling --------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"{identity.APP_NAME}/{identity.APP_VERSION}"
    audit: AuditLog

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 - stdlib name
        return  # request logging goes to the audit log, redacted

    def _send(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        route = urlparse(self.path).path
        if route == "/health":
            payload = json.dumps({
                "application": identity.APP_NAME,
                "version": identity.APP_VERSION,
                "port": self.server.server_address[1],
                "legacy_port": identity.LEGACY_PORT,
                "stage": SESSION.stage,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if route != "/":
            self._send(b"Not found", 404)
            return
        with SESSION_LOCK:
            self._send(page(SESSION))

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        fields = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}

        def one(key: str, default: str = "") -> str:
            return (fields.get(key) or [default])[0].strip()

        with SESSION_LOCK:
            try:
                if route == "/connect":
                    self._connect(SESSION, one("host"), one("user"), one("password"))
                elif route == "/resolve":
                    self._resolve(SESSION, one("manifest"))
                elif route == "/component":
                    self._component(SESSION, one("component"))
                elif route == "/confirm":
                    self._confirm(SESSION, bool(one("confirm")), bool(one("agent_confirm")))
                elif route == "/deploy":
                    self._deploy(SESSION)
                elif route == "/rollback":
                    self._rollback(SESSION)
                else:
                    self._send(b"Not found", 404)
                    return
            except (PlanError, ReleaseSourceError) as error:
                SESSION.note("error", str(error))
        self._redirect()

    # -- actions --

    def _connect(self, session: Session, host: str, user: str, password: str) -> None:
        if not host:
            session.note("error", "Enter the gateway address. This application never discovers gateways.")
            return
        if host != session.gateway_host:
            session.reset_confirmations()
            session.preflight.clear()
        session.gateway_host = host
        session.gateway_user = user or "swadmin"
        # The password is used for this connection and never stored or logged.
        from tools.dev_updater.executor import run_preflight  # imported late: paramiko is optional

        try:
            session.preflight = run_preflight(host, session.gateway_user, password, preflight_steps())
            session.stage = "connected"
            session.note("ok", f"Read-only preflight complete for {host}. Nothing on the gateway was changed.")
        except Exception as error:  # surfaced to the operator rather than a traceback in a terminal
            session.note("error", f"Preflight failed: {error}")
        self.audit.write(
            "preflight",
            gateway=host,
            result="ok" if session.preflight else "failed",
            current_edge_ui=session.preflight.get("current Edge UI commit", "unknown"),
            current_agent=session.preflight.get("current Agent commit", "unknown"),
            cloud_url=session.preflight.get("cloud_url (read only)", "unknown"),
        )

    def _resolve(self, session: Session, manifest_name: str) -> None:
        if not manifest_name:
            session.note("error", "Choose an approved development manifest.")
            return
        session.reset_confirmations()
        session.target = resolve_target(MANIFEST_DIR / manifest_name, repo_root=REPO_ROOT)
        session.note("ok", f"Target pinned. Artifact SHA-256 verified: {session.target.artifact_sha256}")
        self.audit.write(
            "target_resolved",
            manifest=manifest_name,
            edge_release=session.target.edge_release,
            edge_ui_commit=session.target.edge_ui_commit,
            agent_commit=session.target.agent_commit,
            artifact=session.target.artifact_name,
            artifact_sha256=session.target.artifact_sha256,
        )

    def _component(self, session: Session, value: str) -> None:
        try:
            component = Component(value)
        except ValueError:
            session.note("error", "Choose Edge UI only, Edge Agent only, or both.")
            return
        session.reset_confirmations()
        session.component = component
        session.note("ok", f"Component scope set to: {component.label}. Confirm below to arm.")

    def _confirm(self, session: Session, confirmed: bool, agent_confirmed: bool) -> None:
        if session.target is None:
            session.note("error", "Resolve a release target before confirming.")
            return
        session.confirmed = confirmed
        session.agent_confirmed = agent_confirmed
        session.plan = build_plan(
            session.component, session.target,
            confirmed=session.confirmed, agent_confirmed=session.agent_confirmed,
        )
        session.stage = "armed"
        session.note("ok", "Deployment armed. Review the plan, then press Deploy.")
        self.audit.write(
            "armed", gateway=session.gateway_host, component=session.component.value,
            steps=len(session.plan.steps),
        )

    def _deploy(self, session: Session) -> None:
        if session.stage != "armed" or session.plan is None:
            session.note("error", "Nothing is armed. Deployment refused.")
            return
        session.note("warn", "Deployment is performed by the operator-approved run; see the log for the result.")
        self.audit.write(
            "deploy_requested", gateway=session.gateway_host, component=session.component.value,
            edge_ui_commit=session.target.edge_ui_commit if session.target else "",
            agent_commit=session.target.agent_commit if session.target else "",
            rollback_location=session.plan.rollback_location,
        )
        session.deployment_status = "requested"

    def _rollback(self, session: Session) -> None:
        if session.plan is None:
            session.note("error", "No checkpoint is known for this session.")
            return
        session.note("warn", f"Rollback restores code only from {session.plan.rollback_location}.")
        self.audit.write("rollback_requested", gateway=session.gateway_host,
                         rollback_location=session.plan.rollback_location)


def make_server(port: int, host: str = identity.DEFAULT_HOST, *, operator: str = "unknown") -> ThreadingHTTPServer:
    Handler.audit = AuditLog(operator=operator)
    return ThreadingHTTPServer((host, port), Handler)
