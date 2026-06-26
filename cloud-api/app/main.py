from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
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
    require_known_user_auth,
    require_operator_auth,
    require_supabase_user_auth,
)
from app.config import settings
from app.database import Base, engine, get_db
from app.models import (
    EdgeHeartbeat,
    EdgeJob,
    EdgeNode,
    GatewayCredential,
    GatewayGroup,
    OperatorUser,
    SavedBacnetDevice,
    SavedBacnetPoint,
    Site,
    utc_now,
)
from app.schemas import (
    CurrentOperatorOut,
    EdgeJobClaimOut,
    GatewayGroupIn,
    GatewayGroupOut,
    GatewayOut,
    GatewayProvisionIn,
    GatewayProvisionOut,
    GatewaySummaryOut,
    GatewayTreeOut,
    HeartbeatAccepted,
    HeartbeatIn,
    JobCreateIn,
    JobOut,
    JobResultIn,
    OperatorUserOut,
    OperatorUserUpsertIn,
    PublicAuthConfigOut,
    SavedDeviceIn,
    SavedDeviceOut,
    SavedDevicePatchIn,
    SavedPointIn,
    SavedPointOut,
    SavedPointPatchIn,
)
from app.ui import (
    admin_users_html,
    app_html,
    check_email_html,
    gateway_workspace_html,
    login_html,
    signup_html,
    unauthorized_html,
    waiting_approval_html,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="IOT Cloud Commissioning API", version="0.1.0", lifespan=lifespan)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _heartbeat_age_seconds(edge_node: EdgeNode, now: datetime | None = None) -> int | None:
    heartbeat_at = _aware_utc(edge_node.latest_heartbeat_at)
    if heartbeat_at is None:
        return None
    now = now or utc_now()
    return max(0, int((now - heartbeat_at).total_seconds()))


def _effective_status(edge_node: EdgeNode, now: datetime | None = None) -> dict[str, object]:
    age = _heartbeat_age_seconds(edge_node, now)
    if age is None or age > settings.gateway_offline_after_seconds:
        status_value = "offline"
    elif age > settings.gateway_stale_after_seconds:
        status_value = "stale"
    else:
        status_value = "online"
    return {
        "effective_status": status_value,
        "heartbeat_age_seconds": age,
        "is_online": status_value == "online",
        "is_stale": status_value == "stale",
    }


def _gateway_out(edge_node: EdgeNode, now: datetime | None = None) -> dict[str, object]:
    return {
        "gateway_id": edge_node.gateway_id,
        "site_id": edge_node.site_id,
        "hostname": edge_node.hostname,
        "lan_ip": edge_node.lan_ip,
        "bacnet_port": edge_node.bacnet_port,
        "agent_version": edge_node.agent_version,
        "ui_version": edge_node.ui_version,
        "sqlite_db_ok": edge_node.sqlite_db_ok,
        "queued_upload_count": edge_node.queued_upload_count,
        "latest_status": edge_node.latest_status,
        "latest_heartbeat_at": edge_node.latest_heartbeat_at,
        "updated_at": edge_node.updated_at,
        **_effective_status(edge_node, now),
    }


def _get_gateway_or_404(db: Session, gateway_id: str) -> EdgeNode:
    edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == gateway_id))
    if edge_node is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return edge_node


def _require_online_gateway(edge_node: EdgeNode) -> None:
    if _effective_status(edge_node)["effective_status"] != "online":
        raise HTTPException(status_code=409, detail="Gateway is not online")


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail="Record not found") from None


def _tree_id(value: str) -> str:
    return str(_uuid(value))


def _group_out(group: GatewayGroup) -> dict[str, object]:
    return {
        "id": str(group.id),
        "gateway_id": group.gateway_id,
        "name": group.name,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _device_out(device: SavedBacnetDevice) -> dict[str, object]:
    return {
        "id": str(device.id),
        "gateway_id": device.gateway_id,
        "group_id": str(device.group_id) if device.group_id else None,
        "device_instance": device.device_instance,
        "device_name": device.device_name,
        "vendor_name": device.vendor_name,
        "network_number": device.network_number,
        "mac_address": device.mac_address,
        "latest_discovered_at": device.latest_discovered_at,
        "enabled": device.enabled,
        "created_at": device.created_at,
        "updated_at": device.updated_at,
    }


def _point_out(point: SavedBacnetPoint) -> dict[str, object]:
    return {
        "id": str(point.id),
        "gateway_id": point.gateway_id,
        "saved_device_id": str(point.saved_device_id),
        "device_instance": point.device_instance,
        "object_type": point.object_type,
        "object_instance": point.object_instance,
        "object_name": point.object_name,
        "property": point.property_name,
        "present_value": point.present_value,
        "units": point.units,
        "writable": point.writable,
        "latest_read_at": point.latest_read_at,
        "enabled": point.enabled,
        "created_at": point.created_at,
        "updated_at": point.updated_at,
    }


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=307)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
def database_health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("select 1"))
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page() -> HTMLResponse:
    return HTMLResponse(login_html())


@app.get("/signup", response_class=HTMLResponse, include_in_schema=False)
def signup_page() -> HTMLResponse:
    return HTMLResponse(signup_html())


@app.get("/auth/check-email", response_class=HTMLResponse, include_in_schema=False)
def check_email_page() -> HTMLResponse:
    return HTMLResponse(check_email_html())


@app.get("/auth/waiting-approval", response_class=HTMLResponse, include_in_schema=False)
def waiting_approval_page() -> HTMLResponse:
    return HTMLResponse(waiting_approval_html())


@app.get("/auth/unauthorized", response_class=HTMLResponse, include_in_schema=False)
def unauthorized_page() -> HTMLResponse:
    return HTMLResponse(unauthorized_html())


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def app_page() -> HTMLResponse:
    return HTMLResponse(app_html())


@app.get("/gateways/{gateway_id}", response_class=HTMLResponse, include_in_schema=False)
def gateway_workspace_page(gateway_id: str) -> HTMLResponse:
    return HTMLResponse(gateway_workspace_html(gateway_id))


@app.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
def admin_users_page() -> HTMLResponse:
    return HTMLResponse(admin_users_html())


@app.get("/api/auth/public-config", response_model=PublicAuthConfigOut)
def public_auth_config() -> PublicAuthConfigOut:
    supabase_url = (settings.supabase_url or "").strip() or None
    supabase_anon_key = (settings.supabase_anon_key or "").strip() or None
    return PublicAuthConfigOut(
        supabase_url=supabase_url,
        supabase_anon_key=supabase_anon_key,
        configured=bool(supabase_url and supabase_anon_key),
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
def current_operator(auth: AdminAuthContext = Depends(require_known_user_auth)) -> CurrentOperatorOut:
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


@app.get("/api/ui/gateways", response_model=list[GatewayOut])
def ui_list_gateways(
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
    status_filter: str = "all",
) -> list[dict[str, object]]:
    now = utc_now()
    gateways = [_gateway_out(edge_node, now) for edge_node in db.scalars(select(EdgeNode).order_by(EdgeNode.gateway_id)).all()]
    if status_filter != "all":
        gateways = [gateway for gateway in gateways if gateway["effective_status"] == status_filter]
    return gateways


@app.get("/api/ui/gateways/summary", response_model=GatewaySummaryOut)
def ui_gateway_summary(
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> GatewaySummaryOut:
    counts = {"total": 0, "online": 0, "stale": 0, "offline": 0}
    now = utc_now()
    for edge_node in db.scalars(select(EdgeNode)).all():
        counts["total"] += 1
        counts[str(_effective_status(edge_node, now)["effective_status"])] += 1
    return GatewaySummaryOut(**counts)


@app.get("/api/ui/gateways/{gateway_id}", response_model=GatewayOut)
def ui_get_gateway(
    gateway_id: str,
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return _gateway_out(_get_gateway_or_404(db, gateway_id))


@app.get("/api/ui/gateways/{gateway_id}/tree", response_model=GatewayTreeOut)
def ui_get_gateway_tree(
    gateway_id: str,
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> GatewayTreeOut:
    gateway = _get_gateway_or_404(db, gateway_id)
    groups = list(db.scalars(select(GatewayGroup).where(GatewayGroup.gateway_id == gateway_id).order_by(GatewayGroup.name)).all())
    devices = list(
        db.scalars(select(SavedBacnetDevice).where(SavedBacnetDevice.gateway_id == gateway_id).order_by(SavedBacnetDevice.device_instance)).all()
    )
    points = list(
        db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.gateway_id == gateway_id).order_by(SavedBacnetPoint.device_instance, SavedBacnetPoint.object_type, SavedBacnetPoint.object_instance)).all()
    )
    return GatewayTreeOut(
        gateway=GatewayOut(**_gateway_out(gateway)),
        groups=[GatewayGroupOut(**_group_out(group)) for group in groups],
        devices=[SavedDeviceOut(**_device_out(device)) for device in devices],
        points=[SavedPointOut(**_point_out(point)) for point in points],
    )


@app.post("/api/ui/gateways/{gateway_id}/groups", response_model=GatewayGroupOut)
def ui_create_group(
    gateway_id: str,
    payload: GatewayGroupIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _get_gateway_or_404(db, gateway_id)
    group = GatewayGroup(gateway_id=gateway_id, name=payload.name.strip())
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Group already exists for this gateway") from None
    db.refresh(group)
    return _group_out(group)


@app.patch("/api/ui/groups/{group_id}", response_model=GatewayGroupOut)
def ui_rename_group(
    group_id: str,
    payload: GatewayGroupIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    group = db.get(GatewayGroup, _tree_id(group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    group.name = payload.name.strip()
    group.updated_at = utc_now()
    db.commit()
    db.refresh(group)
    return _group_out(group)


@app.delete("/api/ui/groups/{group_id}", status_code=204)
def ui_delete_group(
    group_id: str,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> None:
    group = db.get(GatewayGroup, _tree_id(group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    has_devices = db.scalar(select(SavedBacnetDevice).where(SavedBacnetDevice.group_id == group.id).limit(1))
    if has_devices is not None:
        raise HTTPException(status_code=409, detail="Group is not empty")
    db.delete(group)
    db.commit()


@app.post("/api/ui/gateways/{gateway_id}/devices", response_model=SavedDeviceOut)
def ui_save_device(
    gateway_id: str,
    payload: SavedDeviceIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _get_gateway_or_404(db, gateway_id)
    group_id = _tree_id(payload.group_id) if payload.group_id else None
    if group_id is not None:
        group = db.get(GatewayGroup, group_id)
        if group is None or group.gateway_id != gateway_id:
            raise HTTPException(status_code=404, detail="Group not found")
    device = SavedBacnetDevice(
        gateway_id=gateway_id,
        group_id=group_id,
        device_instance=payload.device_instance,
        device_name=payload.device_name,
        vendor_name=payload.vendor_name,
        network_number=payload.network_number,
        mac_address=payload.mac_address,
        latest_discovered_at=utc_now(),
        enabled=payload.enabled,
    )
    db.add(device)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Device already exists for this gateway") from None
    db.refresh(device)
    return _device_out(device)


@app.patch("/api/ui/devices/{device_id}", response_model=SavedDeviceOut)
def ui_patch_device(
    device_id: str,
    payload: SavedDevicePatchIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    device = db.get(SavedBacnetDevice, _tree_id(device_id))
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if payload.group_id is not None:
        group_id = _tree_id(payload.group_id)
        group = db.get(GatewayGroup, group_id)
        if group is None or group.gateway_id != device.gateway_id:
            raise HTTPException(status_code=404, detail="Group not found")
        device.group_id = group_id
    if payload.device_name is not None:
        device.device_name = payload.device_name
    if payload.vendor_name is not None:
        device.vendor_name = payload.vendor_name
    if payload.enabled is not None:
        device.enabled = payload.enabled
    device.updated_at = utc_now()
    db.commit()
    db.refresh(device)
    return _device_out(device)


@app.post("/api/ui/devices/{device_id}/points", response_model=SavedPointOut)
def ui_save_point(
    device_id: str,
    payload: SavedPointIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    device = db.get(SavedBacnetDevice, _tree_id(device_id))
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    point = SavedBacnetPoint(
        gateway_id=device.gateway_id,
        saved_device_id=device.id,
        device_instance=device.device_instance,
        object_type=payload.object_type,
        object_instance=payload.object_instance,
        object_name=payload.object_name,
        property_name=payload.property,
        present_value=payload.present_value,
        units=payload.units,
        writable=payload.writable,
        latest_read_at=utc_now() if payload.present_value is not None else None,
        enabled=payload.enabled,
    )
    db.add(point)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Point already exists for this device") from None
    db.refresh(point)
    return _point_out(point)


@app.patch("/api/ui/points/{point_id}", response_model=SavedPointOut)
def ui_patch_point(
    point_id: str,
    payload: SavedPointPatchIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    point = db.get(SavedBacnetPoint, _tree_id(point_id))
    if point is None:
        raise HTTPException(status_code=404, detail="Point not found")
    if payload.object_name is not None:
        point.object_name = payload.object_name
    if payload.present_value is not None:
        point.present_value = payload.present_value
        point.latest_read_at = utc_now()
    if payload.units is not None:
        point.units = payload.units
    if payload.writable is not None:
        point.writable = payload.writable
    if payload.enabled is not None:
        point.enabled = payload.enabled
    point.updated_at = utc_now()
    db.commit()
    db.refresh(point)
    return _point_out(point)


@app.post("/api/ui/gateways/{gateway_id}/discover-devices", response_model=JobOut)
def ui_discover_devices(
    gateway_id: str,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> EdgeJob:
    edge_node = _get_gateway_or_404(db, gateway_id)
    _require_online_gateway(edge_node)
    job = EdgeJob(
        job_id=f"job-{uuid4().hex}",
        gateway_id=gateway_id,
        job_type="bacnet_discover",
        status="queued",
        request_json={"bacnet_port": 47814},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


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
) -> list[dict[str, object]]:
    now = utc_now()
    return [_gateway_out(edge_node, now) for edge_node in db.scalars(select(EdgeNode).order_by(EdgeNode.gateway_id)).all()]


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
