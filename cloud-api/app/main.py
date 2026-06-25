from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import (
    DEFAULT_GATEWAY_SCOPES,
    AdminAuthContext,
    GatewayAuthContext,
    generate_gateway_token,
    hash_gateway_token,
    require_admin_or_admin_token_auth,
    require_gateway_auth,
    require_job_operator_auth,
    require_operator_auth,
    require_supabase_user_auth,
)
from app.config import settings
from app.database import Base, engine, get_db
from app.models import EdgeHeartbeat, EdgeJob, EdgeNode, GatewayCredential, OperatorUser, Site, utc_now
from app.schemas import (
    CurrentOperatorOut,
    EdgeJobClaimOut,
    GatewayOut,
    GatewayProvisionIn,
    GatewayProvisionOut,
    HeartbeatAccepted,
    HeartbeatIn,
    JobCreateIn,
    JobOut,
    JobResultIn,
    OperatorUserOut,
    OperatorUserUpsertIn,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="IOT Cloud Commissioning API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
def database_health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("select 1"))
    return {"status": "ok"}


@app.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
def admin_users_page() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IOT Cloud Commissioning Admin</title>
  <style>
    :root {
      color-scheme: light;
      --border: #c9d3df;
      --ink: #17202c;
      --muted: #5d6b7c;
      --panel: #f5f7fa;
      --accent: #0f766e;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      color: var(--ink);
      background: #ffffff;
    }
    header {
      border-bottom: 1px solid var(--border);
      padding: 18px 24px;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }
    h1 {
      font-size: 22px;
      margin: 0;
    }
    h2 {
      font-size: 16px;
      margin: 0 0 12px;
    }
    label {
      display: block;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      margin-bottom: 6px;
    }
    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 8px 10px;
      font: inherit;
      background: #ffffff;
    }
    button {
      min-height: 38px;
      border: 1px solid var(--accent);
      border-radius: 4px;
      padding: 8px 14px;
      font: inherit;
      font-weight: 700;
      color: #ffffff;
      background: var(--accent);
      cursor: pointer;
    }
    button.secondary {
      color: var(--accent);
      background: #ffffff;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 14px;
      align-items: end;
    }
    .span-2 { grid-column: span 2; }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-12 { grid-column: span 12; }
    section {
      border-bottom: 1px solid var(--border);
      padding: 20px 0;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      border-bottom: 1px solid var(--border);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }
    .status {
      min-height: 24px;
      margin-top: 10px;
      font-size: 14px;
      color: var(--muted);
    }
    .status.error { color: var(--danger); }
    @media (max-width: 760px) {
      main { padding: 16px; }
      .grid { grid-template-columns: 1fr; }
      .span-2, .span-3, .span-4, .span-6, .span-12 { grid-column: span 1; }
      table { display: block; overflow-x: auto; white-space: nowrap; }
    }
  </style>
</head>
<body>
  <header><h1>IOT Cloud Commissioning Admin</h1></header>
  <main>
    <section>
      <h2>Authorization</h2>
      <div class="grid">
        <div class="span-6">
          <label for="token">Bearer token</label>
          <input id="token" type="password" autocomplete="off">
        </div>
        <div class="span-2">
          <button id="load" type="button">Load users</button>
        </div>
      </div>
      <div id="status" class="status"></div>
    </section>
    <section>
      <h2>Assign User</h2>
      <form id="user-form" class="grid">
        <div class="span-3">
          <label for="email">Email</label>
          <input id="email" type="email" required>
        </div>
        <div class="span-3">
          <label for="display-name">Display name</label>
          <input id="display-name" type="text">
        </div>
        <div class="span-2">
          <label for="role">Role</label>
          <select id="role">
            <option value="operator">operator</option>
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
            <option value="pending">pending</option>
          </select>
        </div>
        <div class="span-2">
          <label for="user-status">Status</label>
          <select id="user-status">
            <option value="active">active</option>
            <option value="pending">pending</option>
            <option value="disabled">disabled</option>
          </select>
        </div>
        <div class="span-2">
          <button type="submit">Save user</button>
        </div>
      </form>
    </section>
    <section>
      <h2>Users</h2>
      <table>
        <thead>
          <tr>
            <th>Email</th>
            <th>Name</th>
            <th>Role</th>
            <th>Status</th>
            <th>Last login</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="users"></tbody>
      </table>
    </section>
  </main>
  <script>
    const tokenInput = document.querySelector("#token");
    const statusEl = document.querySelector("#status");
    const usersEl = document.querySelector("#users");
    const form = document.querySelector("#user-form");

    function setStatus(message, isError = false) {
      statusEl.textContent = message;
      statusEl.className = isError ? "status error" : "status";
    }

    function headers() {
      return {
        "Authorization": `Bearer ${tokenInput.value.trim()}`,
        "Content-Type": "application/json"
      };
    }

    async function api(path, options = {}) {
      const response = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
      const text = await response.text();
      const body = text ? JSON.parse(text) : null;
      if (!response.ok) {
        throw new Error(body?.detail || `HTTP ${response.status}`);
      }
      return body;
    }

    function renderUsers(users) {
      usersEl.textContent = "";
      for (const user of users) {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td>${user.email}</td>
          <td>${user.display_name || ""}</td>
          <td>${user.role}</td>
          <td>${user.status}</td>
          <td>${user.last_login_at || ""}</td>
          <td><button class="secondary" type="button">Edit</button></td>
        `;
        row.querySelector("button").addEventListener("click", () => {
          document.querySelector("#email").value = user.email;
          document.querySelector("#display-name").value = user.display_name || "";
          document.querySelector("#role").value = user.role;
          document.querySelector("#user-status").value = user.status;
        });
        usersEl.appendChild(row);
      }
    }

    async function loadUsers() {
      if (!tokenInput.value.trim()) {
        setStatus("Bearer token required.", true);
        return;
      }
      try {
        const users = await api("/api/admin/users");
        renderUsers(users);
        setStatus(`Loaded ${users.length} user(s).`);
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    document.querySelector("#load").addEventListener("click", loadUsers);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const email = document.querySelector("#email").value.trim().toLowerCase();
      const payload = {
        email,
        display_name: document.querySelector("#display-name").value.trim() || null,
        role: document.querySelector("#role").value,
        status: document.querySelector("#user-status").value
      };
      try {
        await api(`/api/admin/users/${encodeURIComponent(email)}`, {
          method: "PUT",
          body: JSON.stringify(payload)
        });
        setStatus(`Saved ${email}.`);
        await loadUsers();
      } catch (error) {
        setStatus(error.message, true);
      }
    });
  </script>
</body>
</html>"""
    )


@app.post("/api/auth/register", response_model=OperatorUserOut)
def register_operator_profile(
    auth=Depends(require_supabase_user_auth),
    db: Session = Depends(get_db),
) -> OperatorUser:
    operator = db.scalar(select(OperatorUser).where(OperatorUser.email == auth.email))
    now = utc_now()
    if operator is None:
        operator = OperatorUser(
            supabase_user_id=auth.supabase_user_id,
            email=auth.email,
            role="pending",
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(operator)
    else:
        operator.supabase_user_id = operator.supabase_user_id or auth.supabase_user_id
        operator.updated_at = now
    db.commit()
    db.refresh(operator)
    return operator


@app.get("/api/auth/me", response_model=CurrentOperatorOut)
def current_operator(auth: AdminAuthContext = Depends(require_operator_auth)) -> CurrentOperatorOut:
    return CurrentOperatorOut(email=auth.email, role=auth.role, status=auth.status, auth_type=auth.auth_type)


@app.get("/api/admin/users", response_model=list[OperatorUserOut])
def list_operator_users(
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> list[OperatorUser]:
    return list(db.scalars(select(OperatorUser).order_by(OperatorUser.email)).all())


@app.put("/api/admin/users/{email}", response_model=OperatorUserOut)
def upsert_operator_user(
    email: str,
    payload: OperatorUserUpsertIn,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> OperatorUser:
    path_email = email.strip().lower()
    body_email = payload.email.strip().lower()
    if path_email != body_email:
        raise HTTPException(status_code=400, detail="Path email must match body email")

    operator = db.scalar(select(OperatorUser).where(OperatorUser.email == body_email))
    now = utc_now()
    if operator is None:
        operator = OperatorUser(email=body_email, created_at=now)
        db.add(operator)

    operator.display_name = payload.display_name
    operator.role = payload.role
    operator.status = payload.status
    operator.supabase_user_id = payload.supabase_user_id
    operator.updated_at = now
    db.commit()
    db.refresh(operator)
    return operator


@app.post("/api/edge/heartbeat", response_model=HeartbeatAccepted)
def receive_heartbeat(
    payload: HeartbeatIn,
    auth: GatewayAuthContext = Depends(require_gateway_auth),
    db: Session = Depends(get_db),
) -> HeartbeatAccepted:
    if auth.gateway_id != payload.gateway_id:
        raise HTTPException(status_code=403, detail="Gateway credential does not match heartbeat gateway_id")

    site = db.scalar(select(Site).where(Site.site_id == payload.site_id))
    if site is None:
        site = Site(site_id=payload.site_id, name=payload.site_id)
        db.add(site)
        db.flush()

    edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == payload.gateway_id))
    now = utc_now()
    status = "online" if payload.sqlite_db_ok else "degraded"

    if edge_node is None:
        edge_node = EdgeNode(gateway_id=payload.gateway_id, site_id=payload.site_id, hostname=payload.hostname)
        db.add(edge_node)

    edge_node.site_id = payload.site_id
    edge_node.hostname = payload.hostname
    edge_node.lan_ip = payload.lan_ip
    edge_node.bacnet_port = payload.bacnet_port
    edge_node.agent_version = payload.agent_version
    edge_node.ui_version = payload.ui_version
    edge_node.sqlite_db_ok = payload.sqlite_db_ok
    edge_node.queued_upload_count = payload.queued_upload_count
    edge_node.latest_status = status
    edge_node.latest_heartbeat_at = payload.timestamp_utc
    edge_node.updated_at = now

    db.flush()
    db.add(
        EdgeHeartbeat(
            edge_node_id=edge_node.id,
            gateway_id=payload.gateway_id,
            site_id=payload.site_id,
            hostname=payload.hostname,
            lan_ip=payload.lan_ip,
            bacnet_port=payload.bacnet_port,
            agent_version=payload.agent_version,
            ui_version=payload.ui_version,
            sqlite_db_ok=payload.sqlite_db_ok,
            queued_upload_count=payload.queued_upload_count,
            timestamp_utc=payload.timestamp_utc,
        )
    )
    db.commit()

    return HeartbeatAccepted(
        gateway_id=edge_node.gateway_id,
        status=edge_node.latest_status,
        latest_heartbeat_at=edge_node.latest_heartbeat_at,
    )


@app.get("/api/edge/gateways", response_model=list[GatewayOut])
def list_gateways(
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> list[EdgeNode]:
    return list(db.scalars(select(EdgeNode).order_by(EdgeNode.gateway_id)).all())


@app.post("/api/edge/jobs", response_model=JobOut)
def create_job(
    payload: JobCreateIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> EdgeJob:
    job = EdgeJob(
        job_id=f"job-{uuid4().hex}",
        gateway_id=payload.gateway_id,
        job_type=payload.job_type,
        status="queued",
        request_json=payload.request,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@app.post("/api/admin/gateways/provision", response_model=GatewayProvisionOut)
def provision_gateway(
    payload: GatewayProvisionIn,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> GatewayProvisionOut:
    site = db.scalar(select(Site).where(Site.site_id == payload.site_id))
    if site is None:
        site = Site(site_id=payload.site_id, name=payload.site_id)
        db.add(site)
        db.flush()

    edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == payload.gateway_id))
    now = utc_now()
    if edge_node is None:
        edge_node = EdgeNode(
            gateway_id=payload.gateway_id,
            site_id=payload.site_id,
            hostname=payload.hostname,
            lan_ip=payload.lan_ip,
            bacnet_port=payload.bacnet_port,
            agent_version=payload.agent_version,
            ui_version=payload.ui_version,
            sqlite_db_ok=False,
            queued_upload_count=0,
            latest_status="preprovisioned",
            updated_at=now,
        )
        db.add(edge_node)
    else:
        edge_node.site_id = payload.site_id
        edge_node.hostname = payload.hostname
        edge_node.lan_ip = payload.lan_ip
        edge_node.bacnet_port = payload.bacnet_port
        edge_node.agent_version = payload.agent_version
        edge_node.ui_version = payload.ui_version
        edge_node.updated_at = now

    token_prefix, raw_token = generate_gateway_token()
    db.add(
        GatewayCredential(
            gateway_id=payload.gateway_id,
            token_prefix=token_prefix,
            token_hash=hash_gateway_token(raw_token),
            name=f"{payload.gateway_id} office provisioning token",
            scopes=DEFAULT_GATEWAY_SCOPES,
        )
    )
    db.commit()

    return GatewayProvisionOut(
        gateway_id=payload.gateway_id,
        site_id=payload.site_id,
        hostname=payload.hostname,
        lan_ip=payload.lan_ip,
        bacnet_port=payload.bacnet_port,
        agent_version=payload.agent_version,
        ui_version=payload.ui_version,
        gateway_api_token=raw_token,
        token_prefix=token_prefix,
    )


@app.get("/api/edge/{gateway_id}/jobs/next", response_model=EdgeJobClaimOut | None)
def claim_next_job(
    gateway_id: str,
    auth: GatewayAuthContext = Depends(require_gateway_auth),
    db: Session = Depends(get_db),
) -> EdgeJobClaimOut | None:
    if auth.gateway_id != gateway_id:
        raise HTTPException(status_code=403, detail="Gateway credential does not match requested gateway_id")

    job = db.scalar(
        select(EdgeJob)
        .where(EdgeJob.gateway_id == gateway_id, EdgeJob.status == "queued")
        .order_by(EdgeJob.created_at, EdgeJob.id)
        .limit(1)
    )
    if job is None:
        return None

    job.status = "claimed"
    job.claimed_at = utc_now()
    db.commit()
    db.refresh(job)
    return EdgeJobClaimOut(
        job_id=job.job_id,
        gateway_id=job.gateway_id,
        job_type=job.job_type,
        request=job.request_json,
    )


@app.post("/api/edge/jobs/{job_id}/result", response_model=JobOut)
def receive_job_result(
    job_id: str,
    payload: JobResultIn,
    auth: GatewayAuthContext = Depends(require_gateway_auth),
    db: Session = Depends(get_db),
) -> EdgeJob:
    job = db.scalar(select(EdgeJob).where(EdgeJob.job_id == job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.gateway_id != auth.gateway_id:
        raise HTTPException(status_code=403, detail="Gateway credential does not own this job")

    job.status = payload.status
    job.result_json = payload.result
    job.error_message = payload.error_message
    job.completed_at = utc_now()
    db.commit()
    db.refresh(job)
    return job


@app.get("/api/edge/jobs", response_model=list[JobOut])
def list_jobs(
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
    limit: int = 50,
) -> list[EdgeJob]:
    limit = max(1, min(limit, 200))
    return list(db.scalars(select(EdgeJob).order_by(EdgeJob.created_at.desc(), EdgeJob.id.desc()).limit(limit)).all())
