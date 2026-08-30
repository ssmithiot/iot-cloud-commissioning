import asyncio
import contextlib
import csv
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import logging
import re
import threading
import time
from urllib.parse import parse_qsl, quote, urlsplit
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import UUID
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import String, and_, cast, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

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
from app.access import is_platform_admin, require_site_access, visible_site_ids
from app.config import Settings, production_resource_conflicts, settings
from app.database import Base, SessionLocal, engine, get_db, reset_pool_request_context, set_pool_request_context
from app.models import (
    BacnetWriteBatch,
    BacnetWriteCommand,
    EdgeHeartbeat,
    EdgeJob,
    EdgeNode,
    GatewayAlertState,
    GatewayCredential,
    GatewayGroup,
    GatewayTunnelRequest,
    GatewayUpdateRequest,
    MappingTemplate,
    MappingTemplateRule,
    PointTrendConfig,
    PointTrendSample,
    OperatorUser,
    Organization,
    OrganizationMembership,
    SavedBacnetDevice,
    SavedBacnetPoint,
    Site,
    SiteMembership,
    SiteWeather,
    utc_now,
)
from app.template_registry import TEMPLATES, default_display_label, template_for
from app.schema import require_current_schema, schema_revision_status
from app.schemas import (
    AccessMembershipRecordOut,
    AccessMembershipOut,
    AccessMembershipUpsertIn,
    AccessOverviewOut,
    AlertEvaluationOut,
    AlertEventOut,
    BACNET_WRITE_OBJECT_TYPES,
    BacnetWriteBatchOut,
    CommissioningTemplateImportOut,
    CommissioningTemplateIn,
    CurrentOperatorOut,
    DirectConnectOut,
    DeviceConfigurationIn,
    DeviceConfigurationOut,
    EdgeJobClaimOut,
    EdgeInventorySnapshotIn,
    EdgeInventorySyncOut,
    EdgeTrendConfigOut,
    GatewayCredentialOut,
    GatewayGroupIn,
    GatewayGroupOut,
    GatewayHeartbeatTrendOut,
    GatewayOut,
    GatewayProvisionIn,
    GatewayProvisionOut,
    GatewaySummaryOut,
    GatewayTreeOut,
    GatewayUpdateCompleteIn,
    GatewayUpdateRequestIn,
    GatewayUpdateRequestOut,
    HeartbeatAccepted,
    HeartbeatIn,
    JobCreateIn,
    JobOut,
    MappingApplyOut,
    MappingTemplateFromDeviceIn,
    MappingTemplateIn,
    MappingTemplateOut,
    JobResultIn,
    OperatorUserOut,
    OperatorInviteIn,
    OperatorUserUpsertIn,
    OrganizationCreateIn,
    OrganizationOut,
    PublicAuthConfigOut,
    PointTrendConfigIn,
    PointTrendConfigBulkIn,
    PointTrendConfigOut,
    PointTrendSampleIn,
    PointTrendSampleOut,
    SavedDeviceIn,
    SavedDeviceOut,
    SavedDevicePatchIn,
    SavedPointIn,
    SavedPointOut,
    SavedPointPatchIn,
    SavedPointsReadIn,
    SavedPointsReadOut,
    SavedPointsWriteIn,
    SavedPointsBulkRemoveIn,
    SavedPointsBulkRemoveOut,
    SiteOut,
    SiteUpdate,
    SiteWeatherOut,
    TrendConfigRepairOut,
    TunnelSessionCreateIn,
    TunnelSessionOut,
    TunnelOpenIn,
    TunnelStatusOut,
)
from app.tunnel import (
    TunnelRequestFailed,
    TunnelResponse,
    TunnelUnavailable,
    tunnel_auth_gate,
    tunnel_allowlist,
    tunnel_manager,
    tunnel_metrics,
    tunnel_session_manager,
)
from app.tunnel_relay_canary import relay_client, selected as relay_canary_selected
from app.ui import (
    admin_users_html,
    app_html,
    auth_confirm_html,
    check_email_html,
    gateway_workspace_html,
    gateway_points_html,
    gateway_bms_shell_html,
    login_html,
    reset_password_html,
    signup_html,
    tunnel_console_html,
    tunnel_connecting_html,
    unauthorized_html,
    waiting_approval_html,
)


RETENTION_CLEANUP_INTERVAL_SECONDS = 3600
RETENTION_CLEANUP_BATCH_SIZE = 1000


def _run_bounded_retention_cleanup() -> None:
    """Prune at most one bounded batch per history table outside ingestion."""
    now = utc_now()
    with SessionLocal() as db:
        for model, timestamp, days in (
            (PointTrendSample, PointTrendSample.sampled_at, settings.trend_retention_days),
            (EdgeHeartbeat, EdgeHeartbeat.timestamp_utc, settings.heartbeat_retention_days),
        ):
            ids = db.scalars(select(model.id).where(timestamp < now - timedelta(days=days)).limit(RETENTION_CLEANUP_BATCH_SIZE)).all()
            if ids:
                db.execute(delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False))
        db.commit()


async def _retention_cleanup_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_run_bounded_retention_cleanup)
        except Exception:
            logging.getLogger("iot-cloud-api.retention").exception("bounded retention cleanup failed")
        await asyncio.sleep(RETENTION_CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Staging safety guard: refuse to start a staging instance that points at
    # known production resources. Reports setting NAMES only, never values.
    conflicts = production_resource_conflicts(settings)
    if conflicts:
        raise RuntimeError(
            "Staging environment is configured with known production resources: "
            f"{', '.join(conflicts)}. Use staging-specific values or set "
            "ALLOW_PRODUCTION_RESOURCES=true only if this is intentional."
        )
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    else:
        require_current_schema(engine)
    cleanup_task = asyncio.create_task(_retention_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="IOT Cloud Commissioning API", version="0.1.0", lifespan=lifespan)
logger = logging.getLogger("iot-cloud-api.tunnel")
_tunnel_expiry_tasks: dict[str, asyncio.Task[None]] = {}
request_logger = logging.getLogger("iot-cloud-api.requests")
app_started_monotonic = time.monotonic()


def _ensure_visible_logging(target: logging.Logger) -> None:
    """Attach a stdout handler at INFO when nothing else configures logging.

    Uvicorn configures its own loggers only; app loggers otherwise propagate
    to an unconfigured root at WARNING, which made request-timing logs
    invisible in production during the 2026-07-13 incident.
    """
    if not target.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        target.addHandler(handler)
        target.setLevel(logging.INFO)
        # Keep propagate=True: the unconfigured root has no handler in
        # production (no double logging), and pytest's caplog relies on
        # propagation to capture records.


_ensure_visible_logging(request_logger)

_REQUEST_LOG_EXCLUDED_PATHS = {"/health", "/health/db", "/health/schema"}
# In-process wake-up for the Agent's bounded command wait. The database remains
# authoritative; a missed notification merely lets the bounded wait expire.
job_wait_condition = threading.Condition()
RELAY_CANARY_DURABLE_RECHECK_SECONDS = 10.0


def _relay_canary_selected(gateway_id: str) -> bool:
    return relay_canary_selected(gateway_id, enabled=settings.iot_tunnel_relay_enabled, configured_ids=settings.iot_tunnel_relay_canary_gateways)


def _active_durable_tunnel_request(db: Session, gateway_id: str) -> GatewayTunnelRequest | None:
    now = utc_now()
    return db.scalar(select(GatewayTunnelRequest).where(
        GatewayTunnelRequest.gateway_id == gateway_id,
        GatewayTunnelRequest.state == "requested",
        GatewayTunnelRequest.expires_at.is_not(None),
        GatewayTunnelRequest.expires_at > now,
    ))


def _set_tunnel_instruction_headers(response: Response, db: Session, gateway_id: str) -> None:
    """Use durable intent only for the explicitly selected relay canary."""
    if _relay_canary_selected(gateway_id):
        request = _active_durable_tunnel_request(db, gateway_id)
        expires_at = request.expires_at if request else None
    else:
        expires_at = tunnel_allowlist.expires_at(gateway_id)
    if expires_at is None:
        response.headers["X-IOT-Tunnel-Lease"] = "none"
        return
    response.headers["X-IOT-Tunnel-Lease"] = "active"
    response.headers["X-IOT-Tunnel-Lease-Expires-At"] = expires_at.isoformat()
    response.headers["X-IOT-Tunnel-Requested"] = "true"
    response.headers["X-IOT-Tunnel-Expires-At"] = expires_at.isoformat()


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Structured per-request observability: method, route, status, duration.

    Uses the route template (not the raw path) to keep log cardinality low at
    fleet scale. Additive only; never blocks or alters the response.
    """
    start = time.perf_counter()
    token = set_pool_request_context(uuid4().hex, request.method, request.url.path)
    try:
        response = await call_next(request)
    finally:
        reset_pool_request_context(token)
    route = request.scope.get("route")
    path_template = getattr(route, "path", request.url.path)
    if path_template not in _REQUEST_LOG_EXCLUDED_PATHS:
        request_logger.info(
            "request method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            path_template,
            response.status_code,
            (time.perf_counter() - start) * 1000,
        )
    return response


DIRECT_CONNECT_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
WEATHER_CACHE_TTL = timedelta(minutes=30)
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TUNNEL_HTML_ATTR_PATTERN = re.compile(r"""(?P<attr>\b(?:href|src|action|formaction)=)(?P<quote>["'])(?P<url>[^"']+)(?P=quote)""", re.IGNORECASE)
TUNNEL_GATEWAY_LOCAL_ROUTE_PREFIXES = (
    "/captures",
    "/device-ping",
    "/devices",
    "/discover",
    "/exports",
    "/health",
    "/login",
    "/logout",
    "/packet",
    "/points",
    "/programs",
    "/route-check",
    "/schedules",
    "/static",
    "/template",
    "/templates",
    "/timed-overrides",
    "/view",
    "/write-pv",
)
TUNNEL_JS_ROOT_RELATIVE_PATH_PATTERN = re.compile(
    r"(?P<quote>[\"'`])(?P<url>/(?!/)[^\"'`]*)(?P=quote)"
)


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


def _duplicate_identity_status(db: Session, gateway_id: str, now: datetime | None = None) -> dict[str, object]:
    now = now or utc_now()
    cutoff = now - timedelta(seconds=max(settings.gateway_offline_after_seconds, 1800))
    recent = list(
        db.scalars(
            select(EdgeHeartbeat)
            .where(EdgeHeartbeat.gateway_id == gateway_id, EdgeHeartbeat.timestamp_utc >= cutoff)
            .order_by(EdgeHeartbeat.timestamp_utc.desc())
            .limit(200)
        )
    )
    if not recent:
        return {"duplicate_identity_suspected": False, "duplicate_identity_detail": None}

    machine_ids = {heartbeat.machine_id for heartbeat in recent if heartbeat.machine_id}
    primary_macs = {heartbeat.primary_mac for heartbeat in recent if heartbeat.primary_mac}
    hostnames = {heartbeat.hostname for heartbeat in recent if heartbeat.hostname}
    lan_ips = {heartbeat.lan_ip for heartbeat in recent if heartbeat.lan_ip}
    reasons: list[str] = []
    if len(machine_ids) > 1:
        reasons.append(f"{len(machine_ids)} machine IDs")
    if len(primary_macs) > 1:
        reasons.append(f"{len(primary_macs)} primary MACs")
    if not reasons and len(hostnames) > 1 and len(lan_ips) > 1:
        reasons.append(f"{len(hostnames)} hostnames and {len(lan_ips)} LAN IPs")
    if not reasons:
        return {"duplicate_identity_suspected": False, "duplicate_identity_detail": None}
    return {
        "duplicate_identity_suspected": True,
        "duplicate_identity_detail": (
            f"Duplicate gateway identity suspected in recent heartbeats: {', '.join(reasons)}. "
            f"hostnames={sorted(hostnames)} lan_ips={sorted(lan_ips)}"
        ),
    }


def _duplicate_identity_statuses(db: Session, gateway_ids: list[str], now: datetime) -> dict[str, dict[str, object]]:
    if not gateway_ids:
        return {}
    cutoff = now - timedelta(seconds=max(settings.gateway_offline_after_seconds, 1800))
    ranked = select(
        EdgeHeartbeat.gateway_id,
        EdgeHeartbeat.machine_id,
        EdgeHeartbeat.primary_mac,
        EdgeHeartbeat.hostname,
        EdgeHeartbeat.lan_ip,
        func.row_number().over(partition_by=EdgeHeartbeat.gateway_id, order_by=EdgeHeartbeat.timestamp_utc.desc()).label("rank"),
    ).where(EdgeHeartbeat.gateway_id.in_(gateway_ids), EdgeHeartbeat.timestamp_utc >= cutoff).subquery()
    grouped: dict[str, list[object]] = {}
    for row in db.execute(select(ranked).where(ranked.c.rank <= 200)):
        grouped.setdefault(row.gateway_id, []).append(row)
    statuses: dict[str, dict[str, object]] = {}
    for gateway_id, rows in grouped.items():
        machine_ids = {row.machine_id for row in rows if row.machine_id}; primary_macs = {row.primary_mac for row in rows if row.primary_mac}; hostnames = {row.hostname for row in rows if row.hostname}; lan_ips = {row.lan_ip for row in rows if row.lan_ip}
        reasons = []
        if len(machine_ids) > 1: reasons.append(f"{len(machine_ids)} machine IDs")
        if len(primary_macs) > 1: reasons.append(f"{len(primary_macs)} primary MACs")
        if not reasons and len(hostnames) > 1 and len(lan_ips) > 1: reasons.append(f"{len(hostnames)} hostnames and {len(lan_ips)} LAN IPs")
        statuses[gateway_id] = {"duplicate_identity_suspected": bool(reasons), "duplicate_identity_detail": f"Duplicate gateway identity suspected in recent heartbeats: {', '.join(reasons)}. hostnames={sorted(hostnames)} lan_ips={sorted(lan_ips)}" if reasons else None}
    return statuses


def _gateway_out(edge_node: EdgeNode, now: datetime | None = None, db: Session | None = None, duplicate_identity: dict[str, object] | None = None) -> dict[str, object]:
    site = edge_node.site
    store_hours_mf = (site.store_hours_monday_friday or site.store_hours_mf) if site else None
    store_hours_sat = (site.store_hours_saturday or site.store_hours_sat) if site else None
    store_hours_sun = (site.store_hours_sunday or site.store_hours_sun) if site else None
    direct_connect = _direct_connect_for_site(site) if site else DirectConnectOut(available=False)
    duplicate_identity = duplicate_identity or (
        _duplicate_identity_status(db, edge_node.gateway_id, now)
        if db is not None
        else {"duplicate_identity_suspected": False, "duplicate_identity_detail": None}
    )
    return {
        "gateway_id": edge_node.gateway_id,
        "site_id": edge_node.site_id,
        "hostname": edge_node.hostname,
        "lan_ip": edge_node.lan_ip,
        "machine_id": edge_node.machine_id,
        "primary_mac": edge_node.primary_mac,
        **duplicate_identity,
        "bacnet_port": edge_node.bacnet_port,
        "agent_version": edge_node.agent_version,
        "ui_version": edge_node.ui_version,
        "sqlite_db_ok": edge_node.sqlite_db_ok,
        "queued_upload_count": edge_node.queued_upload_count,
        "trend_pending_upload_count": edge_node.trend_pending_upload_count,
        "trend_deferred_upload_count": edge_node.trend_deferred_upload_count,
        "trend_oldest_pending_at": edge_node.trend_oldest_pending_at,
        "trend_max_upload_attempt_count": edge_node.trend_max_upload_attempt_count,
        "cpu_count": edge_node.cpu_count,
        "cpu_load_1m": edge_node.cpu_load_1m,
        "cpu_load_pct": edge_node.cpu_load_pct,
        "memory_used_pct": edge_node.memory_used_pct,
        "memory_available_mb": edge_node.memory_available_mb,
        "disk_used_pct": edge_node.disk_used_pct,
        "disk_free_mb": edge_node.disk_free_mb,
        "latest_status": edge_node.latest_status,
        "latest_heartbeat_at": edge_node.latest_heartbeat_at,
        "updated_at": edge_node.updated_at,
        "site_name": site.name if site else None,
        "site_address": site.address if site else None,
        "site_address_street": site.address_street if site else None,
        "site_address_city": site.address_city if site else None,
        "site_address_state": site.address_state if site else None,
        "site_address_postal_code": site.address_postal_code if site else None,
        "site_latitude": site.latitude if site else None,
        "site_longitude": site.longitude if site else None,
        "site_compact_address": _site_compact_address(site),
        "store_hours_monday_friday": store_hours_mf,
        "store_hours_saturday": store_hours_sat,
        "store_hours_sunday": store_hours_sun,
        "network_status_notes": site.network_status_notes if site else None,
        "direct_connect_available": direct_connect.available,
        "direct_connect_host": direct_connect.host,
        "direct_connect_port": direct_connect.port,
        **_gateway_release_status(edge_node.agent_version, edge_node.ui_version),
        **_effective_status(edge_node, now),
    }


def _get_gateway_or_404(db: Session, gateway_id: str) -> EdgeNode:
    edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == gateway_id))
    if edge_node is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return edge_node


def _get_gateway_with_site_or_404(db: Session, gateway_id: str) -> EdgeNode:
    edge_node = db.scalar(select(EdgeNode).options(joinedload(EdgeNode.site)).where(EdgeNode.gateway_id == gateway_id))
    if edge_node is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return edge_node


def _require_gateway_site_access(db: Session, auth: AdminAuthContext, gateway_id: str) -> EdgeNode:
    edge_node = _get_gateway_with_site_or_404(db, gateway_id)
    require_site_access(db, auth, edge_node.site)
    return edge_node


def _require_group_site_access(db: Session, auth: AdminAuthContext, group_id: str) -> GatewayGroup:
    group = db.get(GatewayGroup, _tree_id(group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    _require_gateway_site_access(db, auth, group.gateway_id)
    return group


def _require_device_site_access(db: Session, auth: AdminAuthContext, device_id: str) -> SavedBacnetDevice:
    device = db.get(SavedBacnetDevice, _tree_id(device_id))
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    _require_gateway_site_access(db, auth, device.gateway_id)
    return device


def _require_point_site_access(db: Session, auth: AdminAuthContext, point_id: str) -> SavedBacnetPoint:
    point = db.get(SavedBacnetPoint, _tree_id(point_id))
    if point is None:
        raise HTTPException(status_code=404, detail="Point not found")
    _require_gateway_site_access(db, auth, point.gateway_id)
    return point


def _scoped_gateway_statement(db: Session, auth: AdminAuthContext):
    statement = select(EdgeNode).options(joinedload(EdgeNode.site)).order_by(EdgeNode.gateway_id)
    allowed_site_ids = visible_site_ids(db, auth)
    if allowed_site_ids is not None:
        statement = statement.where(EdgeNode.site_id.in_(select(Site.site_id).where(Site.id.in_(allowed_site_ids))))
    return statement


FULL_NON_PROVISIONING_PUBLIC_SCOPE = "full_non_provisioning"
FULL_NON_PROVISIONING_STORED_SCOPE = "edge_release"


def _approved_release_version() -> str:
    # Development keeps the historic display default; production is validated
    # by Settings at startup and can never reach this fallback.
    return (settings.edge_release_version or "0.1.9").strip()


def _approved_release_targets() -> tuple[str, str, str]:
    # Re-read the environment only when an operator submits an update.  This
    # makes a reviewed Render release promotion effective without creating any
    # work at startup, and never accepts browser-provided commit values.
    active_settings = Settings()
    version = (active_settings.edge_release_version or "").strip()
    ui_commit = (active_settings.edge_ui_release_commit or "").strip().lower()
    agent_commit = (active_settings.edge_agent_release_commit or "").strip().lower()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise HTTPException(status_code=503, detail="Approved release version configuration is unavailable")
    if not re.fullmatch(r"[0-9a-f]{40}", ui_commit) or not re.fullmatch(r"[0-9a-f]{40}", agent_commit):
        raise HTTPException(status_code=503, detail="Approved release commit configuration is unavailable")
    return version, ui_commit, agent_commit


def _stored_gateway_update_scope(scope: str) -> str:
    if scope == FULL_NON_PROVISIONING_PUBLIC_SCOPE:
        return FULL_NON_PROVISIONING_STORED_SCOPE
    return scope


def _gateway_update_public_scope(update: GatewayUpdateRequest) -> str:
    if update.update_scope == FULL_NON_PROVISIONING_STORED_SCOPE:
        return FULL_NON_PROVISIONING_PUBLIC_SCOPE
    return update.update_scope


def _gateway_update_target_agent_version(update: GatewayUpdateRequest) -> str | None:
    if update.target_agent_version:
        return update.target_agent_version
    return _approved_release_version() if _gateway_update_public_scope(update) == FULL_NON_PROVISIONING_PUBLIC_SCOPE else None


def _gateway_update_target_ui_version(update: GatewayUpdateRequest) -> str | None:
    if update.target_ui_version:
        return update.target_ui_version
    return _approved_release_version() if _gateway_update_public_scope(update) == FULL_NON_PROVISIONING_PUBLIC_SCOPE else None


def _version_at_least(actual: str | None, required: str) -> tuple[bool, bool]:
    value = (actual or "").strip()
    if value.lower() == "current":
        return True, True
    actual_parts = value.split(".")
    required_parts = required.split(".")
    if len(actual_parts) != 3:
        return False, False
    try:
        actual_numbers = [int(part) for part in actual_parts]
        required_numbers = [int(part) for part in required_parts]
    except ValueError:
        return False, False
    return actual_numbers >= required_numbers, True


def _gateway_release_status(agent_version: str | None, ui_version: str | None) -> dict[str, object]:
    approved_version = _approved_release_version()
    agent_current, agent_known = _version_at_least(agent_version, approved_version)
    ui_current, ui_known = _version_at_least(ui_version, approved_version)
    # The normal operator status intentionally answers one question only.
    # Component commits and transition details remain diagnostics, not UI text.
    current = agent_current and ui_current
    reason = approved_version if current else "Update Needed"
    return {
        "gateway_release_status": reason,
        "gateway_release_reason": reason,
        "gateway_update_required": not current,
        "required_agent_version": approved_version,
        "required_ui_version": approved_version,
    }


def _gateway_update_out(update: GatewayUpdateRequest, edge_node: EdgeNode) -> dict[str, object]:
    site = edge_node.site
    public_scope = _gateway_update_public_scope(update)
    return {
        "request_id": str(update.id),
        "gateway_id": update.gateway_id,
        "site_id": edge_node.site_id,
        "hostname": edge_node.hostname,
        "gateway_host": edge_node.lan_ip,
        "cradlepoint_host": (site.direct_connect_host or site.cradlepoint_ip or site.external_ip) if site else None,
        "agent_version": edge_node.agent_version,
        "ui_version": edge_node.ui_version,
        "update_scope": public_scope,
        "target_agent_version": _gateway_update_target_agent_version(update),
        "target_ui_version": _gateway_update_target_ui_version(update),
        "target_agent_commit": update.target_agent_commit,
        "target_ui_commit": update.target_ui_commit,
        "provisioning": False,
        "token_writing": False,
        "bacnet_configuration_preserved": public_scope == FULL_NON_PROVISIONING_PUBLIC_SCOPE,
        "status": update.status,
        "requested_by": update.requested_by,
        "requested_at": update.requested_at,
        "started_at": update.started_at,
        "completed_at": update.completed_at,
        "error_message": update.error_message,
    }


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _site_compact_address(site: Site | None) -> str | None:
    if site is None:
        return None
    street = _clean_optional_text(site.address_street)
    city = _clean_optional_text(site.address_city)
    state = _clean_optional_text(site.address_state)
    postal_code = _clean_optional_text(site.address_postal_code)
    city_state_zip = " ".join(part for part in [state, postal_code] if part)
    locality = ", ".join(part for part in [city, city_state_zip] if part)
    compact = ", ".join(part for part in [street, locality] if part)
    return compact or _clean_optional_text(site.address)


def _site_out_columns():
    """Select SiteOut fields without hydrating the legacy primary-key column.

    Early databases used integer site IDs while the current model uses UUIDs.
    The browser-facing list does not need that internal key, so this preserves
    existing sites while the legacy identity migration remains isolated.
    """
    columns = Site.__table__.c
    return (
        columns.site_id,
        columns.name,
        columns.external_ip,
        columns.address,
        columns.address_street,
        columns.address_city,
        columns.address_state,
        columns.address_postal_code,
        columns.latitude,
        columns.longitude,
        columns.store_hours_mf,
        columns.store_hours_sat,
        columns.store_hours_sun,
        columns.cradlepoint_ip,
        columns.direct_connect_host,
        columns.direct_connect_port,
        columns.gateway_ui_port,
        columns.store_hours_monday_friday,
        columns.store_hours_saturday,
        columns.store_hours_sunday,
        columns.network_status_notes,
        cast(columns.organization_id, String).label("organization_id"),
    )


def _weather_condition(code: int | None) -> str | None:
    if code is None:
        return None
    labels = {
        0: "Clear",
        1: "Mostly clear",
        2: "Partly cloudy",
        3: "Cloudy",
        45: "Fog",
        48: "Freezing fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Heavy drizzle",
        56: "Light freezing drizzle",
        57: "Freezing drizzle",
        61: "Light rain",
        63: "Rain",
        65: "Heavy rain",
        66: "Light freezing rain",
        67: "Freezing rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Light showers",
        81: "Showers",
        82: "Heavy showers",
        85: "Light snow showers",
        86: "Snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with hail",
        99: "Severe thunderstorm with hail",
    }
    return labels.get(code, f"Weather code {code}")


def _parse_open_meteo_time(value: str | None, utc_offset_seconds: int | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(seconds=utc_offset_seconds or 0)))
    return parsed.astimezone(timezone.utc)


def _solar_noon(sunrise: datetime | None, sunset: datetime | None) -> datetime | None:
    if sunrise is None or sunset is None:
        return None
    return sunrise + ((sunset - sunrise) / 2)


def _fetch_open_meteo_weather(latitude: float, longitude: float) -> dict[str, object]:
    params = urlencode(
        {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "current": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "apparent_temperature",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                ]
            ),
            "daily": "sunrise,sunset",
            "forecast_days": 1,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto",
        }
    )
    request = UrlRequest(
        f"{OPEN_METEO_FORECAST_URL}?{params}",
        headers={"User-Agent": "iot-edge-to-cloud/0.1 weather-cache"},
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _site_weather_out(
    site_id: str,
    weather: SiteWeather | None,
    *,
    available: bool = True,
    reason: str | None = None,
    now: datetime | None = None,
) -> SiteWeatherOut:
    now = now or utc_now()
    if weather is None:
        return SiteWeatherOut(available=False, site_id=site_id, reason=reason or "Weather is not cached yet.")
    fetched_at = _aware_utc(weather.fetched_at) or weather.fetched_at
    cache_age_seconds = int((now - fetched_at).total_seconds()) if fetched_at else None
    return SiteWeatherOut(
        available=available,
        reason=reason,
        site_id=site_id,
        provider=weather.provider,
        latitude=weather.latitude,
        longitude=weather.longitude,
        temperature_f=weather.temperature_f,
        apparent_temperature_f=weather.apparent_temperature_f,
        relative_humidity_percent=weather.relative_humidity_percent,
        precipitation_in=weather.precipitation_in,
        wind_speed_mph=weather.wind_speed_mph,
        weather_code=weather.weather_code,
        condition=weather.condition,
        timezone=weather.timezone,
        timezone_abbreviation=weather.timezone_abbreviation,
        observed_at=weather.observed_at,
        sunrise_at=weather.sunrise_at,
        sunset_at=weather.sunset_at,
        solar_noon_at=weather.solar_noon_at,
        fetched_at=weather.fetched_at,
        cache_age_seconds=max(0, cache_age_seconds) if cache_age_seconds is not None else None,
    )


def _refresh_site_weather(site: Site, db: Session, now: datetime | None = None) -> SiteWeatherOut:
    now = now or utc_now()
    if site.latitude is None or site.longitude is None:
        return SiteWeatherOut(
            available=False,
            site_id=site.site_id,
            reason="Site latitude and longitude are required for weather.",
        )
    weather = db.get(SiteWeather, site.site_id)
    if weather is not None and _aware_utc(weather.fetched_at) and now - _aware_utc(weather.fetched_at) < WEATHER_CACHE_TTL:
        return _site_weather_out(site.site_id, weather, now=now)
    try:
        payload = _fetch_open_meteo_weather(site.latitude, site.longitude)
    except Exception as exc:  # pragma: no cover - network behavior is mocked in tests
        if weather is not None:
            return _site_weather_out(
                site.site_id,
                weather,
                available=True,
                reason=f"Showing cached weather; refresh failed: {exc}",
                now=now,
            )
        raise HTTPException(status_code=502, detail=f"Weather provider request failed: {exc}") from exc

    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    weather_code = current.get("weather_code")
    weather_code = int(weather_code) if isinstance(weather_code, int | float) else None
    observed_at = _parse_open_meteo_time(
        str(current.get("time")) if current.get("time") is not None else None,
        int(payload.get("utc_offset_seconds") or 0),
    )
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    sunrise_values = daily.get("sunrise") if isinstance(daily.get("sunrise"), list) else []
    sunset_values = daily.get("sunset") if isinstance(daily.get("sunset"), list) else []
    sunrise_at = _parse_open_meteo_time(
        str(sunrise_values[0]) if sunrise_values else None,
        int(payload.get("utc_offset_seconds") or 0),
    )
    sunset_at = _parse_open_meteo_time(
        str(sunset_values[0]) if sunset_values else None,
        int(payload.get("utc_offset_seconds") or 0),
    )
    if weather is None:
        weather = SiteWeather(site_id=site.site_id, latitude=site.latitude, longitude=site.longitude)
        db.add(weather)
    weather.provider = "open-meteo"
    weather.latitude = site.latitude
    weather.longitude = site.longitude
    weather.temperature_f = current.get("temperature_2m")
    weather.apparent_temperature_f = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    weather.relative_humidity_percent = int(humidity) if isinstance(humidity, int | float) else None
    weather.precipitation_in = current.get("precipitation")
    weather.wind_speed_mph = current.get("wind_speed_10m")
    weather.weather_code = weather_code
    weather.condition = _weather_condition(weather_code)
    weather.timezone = str(payload.get("timezone")) if payload.get("timezone") else None
    weather.timezone_abbreviation = (
        str(payload.get("timezone_abbreviation")) if payload.get("timezone_abbreviation") else None
    )
    weather.observed_at = observed_at
    weather.sunrise_at = sunrise_at
    weather.sunset_at = sunset_at
    weather.solar_noon_at = _solar_noon(sunrise_at, sunset_at)
    weather.fetched_at = now
    weather.raw_json = payload
    db.commit()
    db.refresh(weather)
    return _site_weather_out(site.site_id, weather, now=now)


def _validate_direct_connect_host(host: str | None) -> str | None:
    host = _clean_optional_text(host)
    if host is None:
        return None
    if "://" in host or "/" in host or "\\" in host or "?" in host or "#" in host or "@" in host:
        raise HTTPException(status_code=422, detail="Direct connect host must be a host or IP only")
    if not DIRECT_CONNECT_HOST_PATTERN.fullmatch(host):
        raise HTTPException(status_code=422, detail="Direct connect host contains unsafe characters")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if any(not label or label.startswith("-") or label.endswith("-") for label in labels):
            raise HTTPException(status_code=422, detail="Direct connect host is not a valid host or IP") from None
    return host


def _tunnel_proxy_prefix(gateway_id: str) -> str:
    return f"/gateways/{quote(gateway_id, safe='')}/tunnel/proxy"


def _tunnel_session_prefix(gateway_id: str, session_id: str) -> str:
    return f"/gateways/{quote(gateway_id, safe='')}/tunnel/session/{quote(session_id, safe='')}"


def _rewrite_tunnel_session_root_relative_url(url: str, redirect_prefix: str) -> str:
    if not url.startswith("/") or url.startswith("//"):
        return url
    if url == redirect_prefix or url.startswith(f"{redirect_prefix}/"):
        return url
    return f"{redirect_prefix}{url}"


def _is_tunnel_gateway_local_url(url: str) -> bool:
    if not url.startswith("/") or url.startswith("//"):
        return False
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        return False
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in TUNNEL_GATEWAY_LOCAL_ROUTE_PREFIXES)


def _rewrite_tunnel_session_json_url(url: str, redirect_prefix: str) -> str:
    if url == redirect_prefix or url.startswith(f"{redirect_prefix}/"):
        return url
    if not _is_tunnel_gateway_local_url(url):
        return url
    return _rewrite_tunnel_session_root_relative_url(url, redirect_prefix)


def _rewrite_tunnel_json_value(value: object, redirect_prefix: str) -> object:
    if isinstance(value, str):
        return _rewrite_tunnel_session_json_url(value, redirect_prefix)
    if isinstance(value, list):
        return [_rewrite_tunnel_json_value(item, redirect_prefix) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_tunnel_json_value(item, redirect_prefix) for key, item in value.items()}
    return value


def _rewrite_tunnel_json_body(body: bytes, redirect_prefix: str) -> bytes:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body

    rewritten = _rewrite_tunnel_json_value(parsed, redirect_prefix)
    if rewritten == parsed:
        return body
    return json.dumps(rewritten, separators=(",", ":")).encode("utf-8")


def _rewrite_tunnel_javascript_body(body: bytes, redirect_prefix: str) -> bytes:
    try:
        script = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        if not _is_tunnel_gateway_local_url(url):
            return match.group(0)
        rewritten = _rewrite_tunnel_session_root_relative_url(url, redirect_prefix)
        return f"{match.group('quote')}{rewritten}{match.group('quote')}"

    return TUNNEL_JS_ROOT_RELATIVE_PATH_PATTERN.sub(replace, script).encode("utf-8")


def _tunnel_fetch_xhr_helper_script(redirect_prefix: str) -> str:
    prefix_json = json.dumps(redirect_prefix)
    return f"""<script>
(function () {{
    "use strict";
    var tunnelPrefix = {prefix_json};
    function rewriteRootRelativeUrl(value) {{
        if (typeof value !== "string") {{
            return value;
        }}
        if (value.charAt(0) !== "/" || value.charAt(1) === "/") {{
            return value;
        }}
        if (value === tunnelPrefix || value.indexOf(tunnelPrefix + "/") === 0) {{
            return value;
        }}
        return tunnelPrefix + value;
    }}
    function rewriteSameOriginUrl(value) {{
        if (typeof value === "string") {{
            return rewriteRootRelativeUrl(value);
        }}
        if (value instanceof URL && value.origin === window.location.origin) {{
            var originalPath = value.pathname + value.search + value.hash;
            var rewrittenPath = rewriteRootRelativeUrl(originalPath);
            if (rewrittenPath !== originalPath) {{
                return new URL(rewrittenPath, window.location.origin);
            }}
        }}
        return value;
    }}
    if (window.fetch) {{
        var originalFetch = window.fetch;
        window.fetch = function (input, init) {{
            return originalFetch.call(this, rewriteSameOriginUrl(input), init);
        }};
    }}
    if (window.XMLHttpRequest && window.XMLHttpRequest.prototype.open) {{
        var originalOpen = window.XMLHttpRequest.prototype.open;
        window.XMLHttpRequest.prototype.open = function (method, url) {{
            var args = Array.prototype.slice.call(arguments);
            args[1] = rewriteSameOriginUrl(url);
            return originalOpen.apply(this, args);
        }};
    }}
}})();
</script>"""


def _rewrite_tunnel_redirect_location(redirect_prefix: str, location: str) -> str:
    location = location.strip()
    if not location or "\r" in location or "\n" in location:
        raise HTTPException(status_code=502, detail="Gateway tunnel redirect target is not allowlisted")

    parsed = urlsplit(location)
    if parsed.scheme or parsed.netloc:
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "http" or host not in {"127.0.0.1", "localhost"} or parsed.port != 5000:
            raise HTTPException(status_code=502, detail="Gateway tunnel redirect target is not allowlisted")

    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    rewritten = f"{redirect_prefix}{path}"
    if parsed.query:
        rewritten = f"{rewritten}?{parsed.query}"
    return rewritten


def _rewrite_tunnel_set_cookie(set_cookie: str, redirect_prefix: str) -> str:
    parts = [part.strip() for part in set_cookie.split(";")]
    rewritten = [parts[0]]
    has_path = False
    for attribute in parts[1:]:
        lower = attribute.lower()
        if lower.startswith("domain="):
            continue
        if lower.startswith("path="):
            rewritten.append(f"Path={redirect_prefix}/")
            has_path = True
        else:
            rewritten.append(attribute)
    if not has_path:
        rewritten.append(f"Path={redirect_prefix}/")
    return "; ".join(rewritten)


def _rewrite_tunnel_html_body(body: bytes, redirect_prefix: str) -> bytes:
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        if url.startswith(("#", "mailto:", "tel:", "data:", "javascript:")):
            rewritten = url
        else:
            try:
                rewritten = _rewrite_tunnel_redirect_location(redirect_prefix, url)
            except HTTPException:
                rewritten = url
        return f"{match.group('attr')}{match.group('quote')}{rewritten}{match.group('quote')}"

    html = TUNNEL_HTML_ATTR_PATTERN.sub(replace, html)
    html = _rewrite_tunnel_javascript_body(html.encode("utf-8"), redirect_prefix).decode("utf-8")
    if not re.search(r"</?(?:html|head|body|script|a|form|img|link|button)\b", html, re.IGNORECASE):
        return html.encode("utf-8")

    helper = _tunnel_fetch_xhr_helper_script(redirect_prefix)
    if re.search(r"</head\s*>", html, re.IGNORECASE):
        html = re.sub(r"</head\s*>", f"{helper}</head>", html, count=1, flags=re.IGNORECASE)
    elif re.search(r"</body\s*>", html, re.IGNORECASE):
        html = re.sub(r"</body\s*>", f"{helper}</body>", html, count=1, flags=re.IGNORECASE)
    else:
        html = f"{html}{helper}"
    return html.encode("utf-8")


def _safe_tunnel_query_keys(query_string: str) -> str:
    if not query_string:
        return ""
    keys = sorted({key for key, _ in parse_qsl(query_string, keep_blank_values=True)})
    return ",".join(keys) if keys else "<blank>"


def _parse_cookie_pairs(cookie_header: str | None) -> list[tuple[str, str]]:
    if not cookie_header:
        return []
    pairs: list[tuple[str, str]] = []
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            pairs.append((name, value))
    return pairs


def _safe_cookie_summary(cookie_header: str | None) -> tuple[str, int]:
    pairs = _parse_cookie_pairs(cookie_header)
    if not pairs:
        return "", 0
    names = ",".join(name for name, _ in pairs)
    return names, len(pairs)


def _deduplicate_cookie_header(cookie_header: str | None) -> str | None:
    pairs = _parse_cookie_pairs(cookie_header)
    if not pairs:
        return None
    seen: set[str] = set()
    forwarded: list[tuple[str, str]] = []
    for name, value in pairs:
        if name in seen:
            continue
        seen.add(name)
        forwarded.append((name, value))
    return "; ".join(f"{name}={value}" for name, value in forwarded)


def _tunnel_location_shape(location: str | None) -> str:
    if not location:
        return "none"
    try:
        parsed = urlsplit(location.strip())
    except ValueError:
        return "invalid"
    path = parsed.path or "/"
    if parsed.scheme or parsed.netloc:
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() == "http" and host in {"127.0.0.1", "localhost"} and parsed.port == 5000:
            return f"gateway-local:{path}"
        return "external"
    return f"relative:{path}"


def _tunnel_response_headers(
    tunnel_response: TunnelResponse,
    *,
    redirect_prefix: str,
    allow_set_cookie: bool,
    rewrite_html_body: bool,
) -> dict[str, str]:
    excluded_headers = {"content-encoding", "content-length", "connection", "transfer-encoding"}
    response_headers = {
        key: value for key, value in tunnel_response.headers.items() if key.lower() not in excluded_headers
    }
    location_header = next((key for key in response_headers if key.lower() == "location"), None)
    if 300 <= tunnel_response.status_code < 400 and location_header is not None:
        response_headers[location_header] = _rewrite_tunnel_redirect_location(redirect_prefix, response_headers[location_header])

    set_cookie_header = next((key for key in response_headers if key.lower() == "set-cookie"), None)
    if set_cookie_header is not None:
        if allow_set_cookie:
            response_headers[set_cookie_header] = _rewrite_tunnel_set_cookie(
                response_headers[set_cookie_header], redirect_prefix
            )
        else:
            response_headers.pop(set_cookie_header, None)
    return response_headers


def _direct_connect_for_site(site: Site | None) -> DirectConnectOut:
    if site is None:
        return DirectConnectOut(available=False, reason="Direct connect is not configured for this site or gateway.")

    host = site.direct_connect_host or site.cradlepoint_ip or site.external_ip
    try:
        host = _validate_direct_connect_host(host)
    except HTTPException:
        return DirectConnectOut(available=False, reason="Direct connect host is invalid.")
    if host is None:
        return DirectConnectOut(available=False, reason="Direct connect is not configured for this site or gateway.")

    port = site.direct_connect_port or 5002
    if port < 1 or port > 65535:
        return DirectConnectOut(available=False, reason="Direct connect port is invalid.")

    return DirectConnectOut(
        available=True,
        url=f"http://{host}:{port}",
        host=host,
        port=port,
    )


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


def _mark_device_seen(device: SavedBacnetDevice, now: datetime) -> None:
    device.first_seen_at = device.first_seen_at or now
    device.last_seen_at = now
    device.latest_discovered_at = now
    device.lifecycle_state = "active"
    device.retired_at = None
    device.enabled = True


def _mark_point_seen(point: SavedBacnetPoint, now: datetime) -> None:
    point.first_seen_at = point.first_seen_at or now
    point.last_seen_at = now
    point.lifecycle_state = "active"
    point.retired_at = None
    point.enabled = True


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
        "template_key": device.template_key,
        "mapping_template_id": device.mapping_template_id,
        "edge_device_profile_id": device.edge_device_profile_id,
        "device_instance": device.device_instance,
        "device_name": device.device_name,
        "vendor_name": device.vendor_name,
        "network_number": device.network_number,
        "mac_address": device.mac_address,
        "latest_discovered_at": device.latest_discovered_at,
        "first_seen_at": device.first_seen_at,
        "last_seen_at": device.last_seen_at,
        "lifecycle_state": device.lifecycle_state,
        "retired_at": device.retired_at,
        "enabled": device.enabled,
        "created_at": device.created_at,
        "updated_at": device.updated_at,
    }


def _point_out(point: SavedBacnetPoint, trend_config: PointTrendConfig | None = None) -> dict[str, object]:
    return {
        "id": str(point.id),
        "gateway_id": point.gateway_id,
        "saved_device_id": str(point.saved_device_id),
        "device_instance": point.device_instance,
        "object_type": point.object_type,
        "object_instance": point.object_instance,
        "object_name": point.object_name,
        "logical_role": point.logical_role,
        "display_label": point.display_label,
        "property": point.property_name,
        "present_value": point.present_value,
        "units": point.units,
        "writable": point.writable,
        "active_priority": point.active_priority,
        "priority_array": point.priority_array,
        "relinquish_default": point.relinquish_default,
        "state_text": point.state_text,
        "latest_read_at": point.latest_read_at,
        "first_seen_at": point.first_seen_at,
        "last_seen_at": point.last_seen_at,
        "lifecycle_state": point.lifecycle_state,
        "retired_at": point.retired_at,
        "enabled": point.enabled,
        "trend_enabled": bool(trend_config and trend_config.enabled),
        "trend_interval_sec": trend_config.interval_sec if trend_config else None,
        "created_at": point.created_at,
        "updated_at": point.updated_at,
    }


def _device_category(db: Session, device: SavedBacnetDevice) -> str | None:
    if not device.group_id:
        return None
    group = db.get(GatewayGroup, device.group_id)
    return group.name.strip() if group else None


def _validate_template_assignment(db: Session, device: SavedBacnetDevice, template_key: str | None) -> None:
    if template_key is None:
        return
    template = template_for(template_key)
    if template is None:
        raise HTTPException(status_code=422, detail="Unknown equipment template")
    category = _device_category(db, device)
    if category not in template["categories"]:
        raise HTTPException(status_code=422, detail=f"Template {template_key} is not compatible with device group {category or 'Uncategorized'}")
    if not device.id:
        return
    roles = [point.logical_role for point in db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id, SavedBacnetPoint.logical_role.is_not(None))).all()]
    invalid_roles = [role for role in roles if role not in template["roles"]]
    if invalid_roles:
        raise HTTPException(status_code=409, detail="Template change would leave incompatible bindings: " + ", ".join(sorted(set(invalid_roles))))


def _validate_logical_role(db: Session, point: SavedBacnetPoint, role: str | None) -> None:
    if role is None:
        return
    device = db.get(SavedBacnetDevice, point.saved_device_id)
    template = template_for(device.template_key if device else None)
    if template is None:
        raise HTTPException(status_code=422, detail="Assign a compatible equipment template before binding roles")
    if role not in template["roles"]:
        raise HTTPException(status_code=422, detail=f"Role {role} is not supported by template {device.template_key}")


def _write_audit_actor(auth: AdminAuthContext) -> str:
    if auth.email:
        return auth.email.strip().lower()
    return "admin_api_token"


def _write_command_out(command: BacnetWriteCommand) -> dict[str, object]:
    return {
        "id": str(command.id),
        "edge_job_id": command.edge_job_id,
        "saved_point_id": command.saved_point_id,
        "device_instance": command.device_instance,
        "object_type": command.object_type,
        "object_instance": command.object_instance,
        "property": command.property_name,
        "action": command.action,
        "requested_value": command.requested_value,
        "priority": command.priority,
        "status": command.status,
        "result": command.result_json,
        "error_message": command.error_message,
        "created_at": command.created_at,
        "completed_at": command.completed_at,
    }


def _write_batch_out(batch: BacnetWriteBatch) -> dict[str, object]:
    commands = list(batch.commands)
    return {
        "batch_id": str(batch.id),
        "gateway_id": batch.gateway_id,
        "requested_by": batch.requested_by,
        "approved_by": batch.approved_by,
        "status": batch.status,
        "write_count": batch.write_count,
        "queued_count": sum(command.status in {"queued", "claimed"} for command in commands),
        "job_ids": list(dict.fromkeys(command.edge_job_id for command in commands if command.edge_job_id)),
        "requested_at": batch.requested_at,
        "approved_at": batch.approved_at,
        "completed_at": batch.completed_at,
        "commands": [_write_command_out(command) for command in commands],
    }


def _refresh_write_batch_status(db: Session, batch_id: UUID, now: datetime) -> None:
    batch = db.get(BacnetWriteBatch, batch_id)
    if batch is None:
        return
    statuses = set(db.scalars(select(BacnetWriteCommand.status).where(BacnetWriteCommand.batch_id == batch_id)).all())
    if not statuses:
        return
    if statuses & {"queued", "claimed"}:
        batch.status = "claimed" if "claimed" in statuses else "queued"
        batch.completed_at = None
    elif statuses == {"succeeded"}:
        batch.status = "completed"
        batch.completed_at = now
    elif statuses == {"failed"}:
        batch.status = "failed"
        batch.completed_at = now
    elif statuses == {"deferred"}:
        batch.status = "deferred"
        batch.completed_at = now
    else:
        batch.status = "partial"
        batch.completed_at = now


def _readback_value(result: dict[str, object], field: str) -> tuple[bool, object | None]:
    if field in result:
        return True, result[field]
    snapshot = result.get("snapshot")
    if isinstance(snapshot, dict) and field in snapshot:
        return True, snapshot[field]
    return False, None


def _apply_write_readback(point: SavedBacnetPoint, result: dict[str, object], now: datetime) -> None:
    readback_seen = False
    present_value_found, present_value = _readback_value(result, "present_value")
    if present_value_found:
        point.present_value = None if present_value is None else str(present_value)
        readback_seen = True

    active_priority_found, active_priority = _readback_value(result, "active_priority")
    if active_priority_found and (
        active_priority is None
        or (not isinstance(active_priority, bool) and isinstance(active_priority, int) and 1 <= active_priority <= 16)
    ):
        point.active_priority = active_priority
        readback_seen = True

    for field in ("priority_array", "relinquish_default", "state_text"):
        found, value = _readback_value(result, field)
        if found and (value is None or isinstance(value, str)):
            setattr(point, field, value)
            readback_seen = True

    if readback_seen:
        point.latest_read_at = now
        point.updated_at = now


def _reconcile_bacnet_write_result(
    db: Session,
    job: EdgeJob,
    payload: JobResultIn,
) -> None:
    commands = list(
        db.scalars(
            select(BacnetWriteCommand)
            .where(BacnetWriteCommand.edge_job_id == job.job_id)
            .order_by(BacnetWriteCommand.created_at, BacnetWriteCommand.id)
        ).all()
    )
    if not commands:
        return

    now = utc_now()
    results_by_point_id: dict[str, dict[str, object]] = {}
    if payload.status == "completed" and isinstance(payload.result, dict):
        raw_results = payload.result.get("results")
        if isinstance(raw_results, list):
            for raw_result in raw_results:
                if not isinstance(raw_result, dict):
                    continue
                point_id = raw_result.get("saved_point_id")
                if isinstance(point_id, str) and point_id not in results_by_point_id:
                    results_by_point_id[point_id] = raw_result

    affected_batch_ids: set[UUID] = set()
    for command in commands:
        affected_batch_ids.add(command.batch_id)
        result = results_by_point_id.get(command.saved_point_id)
        command.completed_at = now
        if result is None:
            command.status = "deferred" if payload.status == "deferred" else "failed"
            command.error_message = payload.error_message or "Edge response omitted the command result"
            continue

        command.result_json = result
        command.status = "succeeded" if result.get("ok") is True else "failed"
        message = result.get("message")
        command.error_message = None if command.status == "succeeded" else (
            str(message)[:1000] if message is not None else "BACnet write failed"
        )

        point = db.get(SavedBacnetPoint, command.saved_point_id)
        if (
            point is not None
            and point.gateway_id == job.gateway_id
            and point.device_instance == command.device_instance
            and point.object_type == command.object_type
            and point.object_instance == command.object_instance
        ):
            _apply_write_readback(point, result, now)

    db.flush()
    for batch_id in affected_batch_ids:
        _refresh_write_batch_status(db, batch_id, now)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=307)


@app.get("/health")
def health() -> dict[str, str | None]:
    # Environment identity for humans and tooling (staging vs production).
    # Never include secrets, URLs, tokens, or credentials here.
    return {
        "status": "ok",
        "environment": settings.environment,
        "version": app.version,
        "approved_edge_release": settings.edge_release_version,
        "approved_edge_ui_commit": settings.edge_ui_release_commit,
        "approved_edge_agent_commit": settings.edge_agent_release_commit,
    }


@app.get("/health/db")
def database_health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("select 1"))
    return {"status": "ok"}


@app.get("/api/admin/cloud-metrics")
def admin_cloud_metrics(
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Return safe, live application and database-pool health for admins.

    This deliberately exposes neither database URLs nor credentials. Render's
    service charts stay in Render; an optional configured dashboard URL is
    returned only to authenticated platform admins.
    """
    db.execute(text("select 1"))
    pool = engine.pool

    def pool_count(method_name: str) -> int | None:
        method = getattr(pool, method_name, None)
        if not callable(method):
            return None
        try:
            return int(method())
        except (TypeError, ValueError):
            return None

    return {
        "status": "ok",
        "environment": settings.environment,
        "version": app.version,
        "uptime_seconds": round(time.monotonic() - app_started_monotonic, 1),
        "database": {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "timeout_seconds": settings.db_pool_timeout_sec,
            "recycle_seconds": settings.db_pool_recycle_sec,
            "checked_out": pool_count("checkedout"),
            "checked_in": pool_count("checkedin"),
            "overflow": pool_count("overflow"),
        },
        "tunnels": tunnel_metrics.snapshot(
            active_tunnels=tunnel_manager.active_count(),
            auth_gate_in_use=tunnel_auth_gate.in_use,
            auth_gate_limit=settings.gateway_tunnel_auth_concurrency,
        ),
        "schema": schema_revision_status(engine, auto_create_tables=settings.auto_create_tables).as_dict(),
        "render_metrics_url": (settings.render_metrics_url or "").strip() or None,
    }


@app.get("/health/schema")
def schema_health() -> dict[str, object]:
    return schema_revision_status(engine, auto_create_tables=settings.auto_create_tables).as_dict()


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page() -> HTMLResponse:
    return HTMLResponse(login_html())


@app.get("/signup", response_class=HTMLResponse, include_in_schema=False)
def signup_page() -> HTMLResponse:
    return HTMLResponse(signup_html())


@app.get("/auth/check-email", response_class=HTMLResponse, include_in_schema=False)
def check_email_page() -> HTMLResponse:
    return HTMLResponse(check_email_html())


@app.get("/auth/confirm", response_class=HTMLResponse, include_in_schema=False)
def auth_confirm_page() -> HTMLResponse:
    return HTMLResponse(auth_confirm_html())


@app.get("/auth/reset-password", response_class=HTMLResponse, include_in_schema=False)
def reset_password_page() -> HTMLResponse:
    return HTMLResponse(reset_password_html())


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


@app.get("/gateways/{gateway_id}/points", response_class=HTMLResponse, include_in_schema=False)
def gateway_points_page(gateway_id: str) -> HTMLResponse:
    return HTMLResponse(gateway_points_html(gateway_id))


@app.get("/gateways/{gateway_id}/devices/{device_id}/points", response_class=HTMLResponse, include_in_schema=False)
def gateway_device_points_page(gateway_id: str, device_id: str) -> HTMLResponse:
    return HTMLResponse(gateway_points_html(gateway_id, device_id))


@app.get("/gateways/{gateway_id}/devices/{device_id}", response_class=HTMLResponse, include_in_schema=False)
def gateway_device_page(gateway_id: str, device_id: str) -> HTMLResponse:
    return HTMLResponse(gateway_bms_shell_html(gateway_id, "device", device_id))


@app.get("/gateways/{gateway_id}/trends", response_class=HTMLResponse, include_in_schema=False)
def gateway_trends_page(gateway_id: str) -> HTMLResponse:
    return HTMLResponse(gateway_bms_shell_html(gateway_id, "trends"))


@app.get("/gateways/{gateway_id}/weather", response_class=HTMLResponse, include_in_schema=False)
def gateway_weather_page(gateway_id: str) -> HTMLResponse:
    return HTMLResponse(gateway_bms_shell_html(gateway_id, "weather"))


@app.get("/gateways/{gateway_id}/configure-tree", response_class=HTMLResponse, include_in_schema=False)
def gateway_configure_tree_page(gateway_id: str) -> HTMLResponse:
    return HTMLResponse(gateway_bms_shell_html(gateway_id, "configure-tree"))


@app.get("/gateways/{gateway_id}/configure", include_in_schema=False)
def configure_gateway_page(
    gateway_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _get_gateway_or_404(db, gateway_id)
    return RedirectResponse(f"/gateways/{quote(gateway_id, safe='')}/tunnel/")


@app.get("/gateways/{gateway_id}/tunnel/", response_class=HTMLResponse, include_in_schema=False)
def tunnel_console_page(
    gateway_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _get_gateway_or_404(db, gateway_id)
    return HTMLResponse(tunnel_console_html(gateway_id))


@app.get("/gateways/{gateway_id}/tunnel/connecting", response_class=HTMLResponse, include_in_schema=False)
def tunnel_connecting_page(gateway_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    _get_gateway_or_404(db, gateway_id)
    return HTMLResponse(tunnel_connecting_html(gateway_id))


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
        if operator.status == "active":
            operator.last_user_activity_at = now
        operator.updated_at = now
    db.commit()
    db.refresh(operator)
    return operator


@app.get("/api/auth/me", response_model=CurrentOperatorOut)
def current_operator(auth: AdminAuthContext = Depends(require_known_user_auth)) -> CurrentOperatorOut:
    return CurrentOperatorOut(email=auth.email, role=auth.role, status=auth.status, auth_type=auth.auth_type)


@app.post("/api/ui/session/activity", status_code=204)
def record_user_activity(auth=Depends(require_supabase_user_auth), db: Session = Depends(get_db)) -> None:
    operator = db.scalar(select(OperatorUser).where(OperatorUser.email == auth.email))
    if operator is None or operator.status != "active":
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    operator.last_user_activity_at = utc_now()
    operator.updated_at = utc_now()
    db.commit()


@app.get("/api/admin/users", response_model=list[OperatorUserOut])
def list_operator_users(
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> list[OperatorUser]:
    return list(db.scalars(select(OperatorUser).order_by(OperatorUser.email)).all())


def _send_supabase_invitation(*, email: str, display_name: str | None, redirect_to: str) -> None:
    """Ask Supabase Auth to send its configured Invite user email."""
    supabase_url = (settings.supabase_url or "").strip().rstrip("/")
    secret_key = (settings.supabase_service_role_key or "").strip()
    if not supabase_url or not secret_key:
        raise HTTPException(status_code=503, detail="Supabase invitations are not configured")

    import httpx

    payload: dict[str, object] = {"email": email, "redirect_to": redirect_to}
    if display_name:
        payload["data"] = {"display_name": display_name}
    try:
        response = httpx.post(
            f"{supabase_url}/auth/v1/invite",
            headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            json=payload,
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Supabase invite request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not send the invitation email") from exc
    if response.is_error:
        logger.warning("Supabase invite request failed with status %s", response.status_code)
        raise HTTPException(status_code=502, detail="Could not send the invitation email")


@app.post("/api/admin/users/invite", response_model=OperatorUserOut)
def invite_operator_user(
    payload: OperatorInviteIn,
    request: Request,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> OperatorUser:
    """Send Supabase's branded invitation while keeping app access pending."""
    email = payload.email.strip().lower()
    display_name = (payload.display_name or "").strip() or None
    operator = db.scalar(select(OperatorUser).where(OperatorUser.email == email))
    if operator is not None and operator.status == "active":
        raise HTTPException(status_code=409, detail="This user is already active")

    redirect_to = (settings.supabase_invite_redirect_url or "").strip()
    if not redirect_to:
        redirect_to = f"{str(request.base_url).rstrip('/')}/auth/confirm"
    _send_supabase_invitation(email=email, display_name=display_name, redirect_to=redirect_to)

    now = utc_now()
    if operator is None:
        operator = OperatorUser(
            email=email,
            display_name=display_name,
            role="pending",
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(operator)
    else:
        operator.display_name = display_name or operator.display_name
        operator.role = "pending"
        operator.status = "pending"
        operator.updated_at = now
    db.commit()
    db.refresh(operator)
    return operator


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


@app.get("/api/admin/organizations", response_model=list[OrganizationOut])
def list_organizations(
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    columns = Organization.__table__.c
    rows = db.execute(
        select(cast(columns.id, String).label("id"), columns.name, columns.created_at).order_by(columns.name)
    ).mappings()
    return [dict(row) for row in rows]


@app.post("/api/admin/organizations", response_model=OrganizationOut)
def create_organization(
    payload: OrganizationCreateIn,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> Organization:
    organization = Organization(name=payload.name.strip())
    db.add(organization)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Organization name already exists") from None
    db.refresh(organization)
    return organization


def _operator_for_membership_or_404(db: Session, email: str) -> OperatorUser:
    operator = db.scalar(select(OperatorUser).where(OperatorUser.email == email.strip().lower()))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator user not found")
    return operator


@app.get("/api/admin/access-overview", response_model=AccessOverviewOut)
def admin_access_overview(
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> AccessOverviewOut:
    organization_memberships = db.execute(
        select(OrganizationMembership, Organization, OperatorUser)
        .join(Organization, OrganizationMembership.organization_id == Organization.id)
        .join(OperatorUser, OrganizationMembership.operator_user_id == OperatorUser.id)
        .order_by(Organization.name, OperatorUser.email)
    ).all()
    site_memberships = db.execute(
        select(SiteMembership, Site, OperatorUser)
        .join(Site, SiteMembership.site_uuid == Site.id)
        .join(OperatorUser, SiteMembership.operator_user_id == OperatorUser.id)
        .order_by(Site.name, OperatorUser.email)
    ).all()
    memberships = [
        AccessMembershipRecordOut(
            email=operator.email,
            role=membership.role,
            scope_kind="organization",
            scope_id=str(organization.id),
            scope_name=organization.name,
        )
        for membership, organization, operator in organization_memberships
    ]
    memberships.extend(
        AccessMembershipRecordOut(
            email=operator.email,
            role=membership.role,
            scope_kind="site",
            scope_id=site.site_id,
            scope_name=site.name,
        )
        for membership, site, operator in site_memberships
    )
    return AccessOverviewOut(memberships=memberships)


@app.put("/api/admin/organizations/{organization_id}/members", response_model=AccessMembershipOut)
def upsert_organization_membership(
    organization_id: str,
    payload: AccessMembershipUpsertIn,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> AccessMembershipOut:
    organization = db.get(Organization, _uuid(organization_id))
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    operator = _operator_for_membership_or_404(db, payload.email)
    membership = db.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization.id,
            OrganizationMembership.operator_user_id == operator.id,
        )
    )
    if membership is None:
        membership = OrganizationMembership(organization_id=organization.id, operator_user_id=operator.id)
        db.add(membership)
    membership.role = payload.role
    membership.updated_at = utc_now()
    db.commit()
    return AccessMembershipOut(email=operator.email, role=membership.role)


@app.put("/api/admin/sites/{site_id}/organization/{organization_id}", response_model=SiteOut)
def assign_site_organization(
    site_id: str,
    organization_id: str,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> Site:
    site = db.scalar(select(Site).where(Site.site_id == site_id))
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    organization = db.get(Organization, _uuid(organization_id))
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    site.organization_id = organization.id
    db.commit()
    db.refresh(site)
    return site


@app.put("/api/admin/sites/{site_id}/members", response_model=AccessMembershipOut)
def upsert_site_membership(
    site_id: str,
    payload: AccessMembershipUpsertIn,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> AccessMembershipOut:
    site = db.scalar(select(Site).where(Site.site_id == site_id))
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    operator = _operator_for_membership_or_404(db, payload.email)
    membership = db.scalar(
        select(SiteMembership).where(
            SiteMembership.site_uuid == site.id,
            SiteMembership.operator_user_id == operator.id,
        )
    )
    if membership is None:
        membership = SiteMembership(site_uuid=site.id, operator_user_id=operator.id)
        db.add(membership)
    membership.role = payload.role
    membership.updated_at = utc_now()
    db.commit()
    return AccessMembershipOut(email=operator.email, role=membership.role)


@app.get("/api/ui/gateways", response_model=list[GatewayOut])
def ui_list_gateways(
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
    status_filter: str = "all",
) -> list[dict[str, object]]:
    now = utc_now()
    edge_nodes = db.scalars(_scoped_gateway_statement(db, auth)).all()
    duplicate_statuses = _duplicate_identity_statuses(db, [edge_node.gateway_id for edge_node in edge_nodes], now)
    gateways = [_gateway_out(edge_node, now, duplicate_identity=duplicate_statuses.get(edge_node.gateway_id)) for edge_node in edge_nodes]
    if status_filter != "all":
        gateways = [gateway for gateway in gateways if gateway["effective_status"] == status_filter]
    return gateways


@app.post("/api/ui/gateway-updates", response_model=list[GatewayUpdateRequestOut])
def ui_request_gateway_updates(
    payload: GatewayUpdateRequestIn,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    gateway_ids = list(dict.fromkeys(gateway_id.strip() for gateway_id in payload.gateway_ids if gateway_id.strip()))
    if not gateway_ids:
        raise HTTPException(status_code=400, detail="Select at least one gateway to update")

    approved_version, approved_ui_commit, approved_agent_commit = _approved_release_targets()
    now = utc_now()
    updates: list[GatewayUpdateRequest] = []
    for gateway_id in gateway_ids:
        _require_gateway_site_access(db, auth, gateway_id)
        existing = db.scalar(
            select(GatewayUpdateRequest)
            .where(
                GatewayUpdateRequest.gateway_id == gateway_id,
                GatewayUpdateRequest.status.in_(["queued", "running"]),
            )
            .order_by(GatewayUpdateRequest.requested_at.desc())
        )
        if existing is None:
            existing = GatewayUpdateRequest(
                gateway_id=gateway_id,
                requested_by=auth.email or "admin-token",
                update_scope=_stored_gateway_update_scope(payload.update_scope),
                target_agent_version=approved_version
                if payload.update_scope in {"agent", "edge_release", "full_non_provisioning"} else None,
                target_ui_version=approved_version
                if payload.update_scope in {"ui_only", "edge_release", "full_non_provisioning"} else None,
                target_agent_commit=approved_agent_commit
                if payload.update_scope in {"agent", "edge_release", "full_non_provisioning"} else None,
                target_ui_commit=approved_ui_commit
                if payload.update_scope in {"ui_only", "edge_release", "full_non_provisioning"} else None,
                status="queued",
                requested_at=now,
            )
            db.add(existing)
            db.flush()
        updates.append(existing)

    db.commit()
    return [
        _gateway_update_out(update, _get_gateway_with_site_or_404(db, update.gateway_id))
        for update in updates
    ]


@app.get("/api/ui/gateway-updates", response_model=list[GatewayUpdateRequestOut])
def ui_list_gateway_updates(
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
    limit: int = 100,
) -> list[dict[str, object]]:
    limit = max(1, min(limit, 500))
    updates = db.scalars(
        select(GatewayUpdateRequest)
        .where(GatewayUpdateRequest.status.in_(["queued", "running", "failed"]))
        .order_by(GatewayUpdateRequest.requested_at.desc())
        .limit(limit)
    ).all()
    allowed_site_ids = visible_site_ids(db, auth)
    gateways_by_id = {
        gateway.gateway_id: gateway
        for gateway in db.scalars(
            select(EdgeNode).options(joinedload(EdgeNode.site)).where(EdgeNode.gateway_id.in_([update.gateway_id for update in updates]))
        ).all()
    }
    visible_updates: list[dict[str, object]] = []
    for update in updates:
        gateway = gateways_by_id.get(update.gateway_id)
        if gateway is None:
            continue
        if allowed_site_ids is None or str(gateway.site.id) in allowed_site_ids:
            visible_updates.append(_gateway_update_out(update, gateway))
    return visible_updates


@app.get("/api/ui/gateways/summary", response_model=GatewaySummaryOut)
def ui_gateway_summary(
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> GatewaySummaryOut:
    counts = {"total": 0, "online": 0, "stale": 0, "offline": 0}
    now = utc_now()
    for edge_node in db.scalars(_scoped_gateway_statement(db, auth)).all():
        counts["total"] += 1
        counts[str(_effective_status(edge_node, now)["effective_status"])] += 1
    return GatewaySummaryOut(**counts)


@app.get("/api/ui/sites", response_model=list[SiteOut])
def ui_list_sites(
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    statement = select(*_site_out_columns()).order_by(Site.__table__.c.site_id)
    allowed_site_ids = visible_site_ids(db, auth)
    if allowed_site_ids is not None:
        statement = statement.where(Site.__table__.c.id.in_(allowed_site_ids))
    return [dict(row) for row in db.execute(statement).mappings()]


@app.get("/api/ui/sites/{site_id}", response_model=SiteOut)
def ui_get_site(
    site_id: str,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> Site:
    site = db.scalar(select(Site).where(Site.site_id == site_id))
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    require_site_access(db, auth, site)
    return site


@app.patch("/api/ui/sites/{site_id}", response_model=SiteOut)
def ui_update_site(
    site_id: str,
    payload: SiteUpdate,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> Site:
    site = db.scalar(select(Site).where(Site.site_id == site_id))
    if site is None:
        site = Site(site_id=site_id, name=payload.name or site_id)
        db.add(site)

    updates = payload.model_dump(exclude_unset=True)
    if "direct_connect_host" in updates:
        updates["direct_connect_host"] = _validate_direct_connect_host(updates["direct_connect_host"])
    if "cradlepoint_ip" in updates:
        updates["cradlepoint_ip"] = _validate_direct_connect_host(updates["cradlepoint_ip"])
    for field, value in updates.items():
        if field == "name" and value is None:
            continue
        setattr(site, field, value)

    db.commit()
    db.refresh(site)
    return site


@app.get("/api/ui/gateways/{gateway_id}", response_model=GatewayOut)
def ui_get_gateway(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return _gateway_out(_require_gateway_site_access(db, auth, gateway_id), db=db)


@app.get("/api/ui/gateways/{gateway_id}/heartbeat-trend", response_model=list[GatewayHeartbeatTrendOut])
def ui_gateway_heartbeat_trend(
    gateway_id: str,
    limit: int = Query(default=96, ge=1, le=720),
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """Return recorded edge heartbeats, oldest first, for the dashboard trend."""
    _require_gateway_site_access(db, auth, gateway_id)
    heartbeats = list(
        db.scalars(
            select(EdgeHeartbeat)
            .where(EdgeHeartbeat.gateway_id == gateway_id)
            .order_by(EdgeHeartbeat.timestamp_utc.desc())
            .limit(limit)
        ).all()
    )
    return [
        {
            "timestamp_utc": heartbeat.timestamp_utc,
            "received_at": heartbeat.received_at,
            "status": "online" if heartbeat.sqlite_db_ok else "degraded",
            "sqlite_db_ok": heartbeat.sqlite_db_ok,
            "queued_upload_count": heartbeat.queued_upload_count,
            "hostname": heartbeat.hostname,
            "lan_ip": heartbeat.lan_ip,
            "machine_id": heartbeat.machine_id,
            "primary_mac": heartbeat.primary_mac,
            "trend_pending_upload_count": heartbeat.trend_pending_upload_count,
            "trend_deferred_upload_count": heartbeat.trend_deferred_upload_count,
            "trend_oldest_pending_at": heartbeat.trend_oldest_pending_at,
            "trend_max_upload_attempt_count": heartbeat.trend_max_upload_attempt_count,
            "cpu_load_pct": heartbeat.cpu_load_pct,
            "memory_used_pct": heartbeat.memory_used_pct,
            "disk_used_pct": heartbeat.disk_used_pct,
            "agent_version": heartbeat.agent_version,
            "ui_version": heartbeat.ui_version,
        }
        for heartbeat in reversed(heartbeats)
    ]


@app.get("/api/ui/gateways/{gateway_id}/site", response_model=SiteOut)
def ui_get_gateway_site(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> Site:
    return _require_gateway_site_access(db, auth, gateway_id).site


@app.get("/api/ui/gateways/{gateway_id}/weather", response_model=SiteWeatherOut)
def ui_get_gateway_weather(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> SiteWeatherOut:
    gateway = _require_gateway_site_access(db, auth, gateway_id)
    return _refresh_site_weather(gateway.site, db)


@app.patch("/api/ui/gateways/{gateway_id}/site", response_model=SiteOut)
def ui_update_gateway_site(
    gateway_id: str,
    payload: SiteUpdate,
    auth: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> Site:
    gateway = _get_gateway_with_site_or_404(db, gateway_id)
    return ui_update_site(gateway.site_id, payload, auth, db)


@app.get("/api/ui/gateways/{gateway_id}/direct-connect", response_model=DirectConnectOut)
def ui_gateway_direct_connect(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> DirectConnectOut:
    gateway = _require_gateway_site_access(db, auth, gateway_id)
    return _direct_connect_for_site(gateway.site)


@app.get("/api/ui/gateways/{gateway_id}/tunnel-status", response_model=TunnelStatusOut)
def ui_gateway_tunnel_status(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> TunnelStatusOut:
    _require_gateway_site_access(db, auth, gateway_id)
    expires_at = tunnel_allowlist.expires_at(gateway_id)
    connected = tunnel_manager.is_connected(gateway_id)
    if expires_at is None:
        return TunnelStatusOut(connected=False, status="closed")
    remaining = max(0, int((expires_at - utc_now()).total_seconds()))
    return TunnelStatusOut(
        connected=connected,
        status="connected" if connected else "opening",
        expires_at=expires_at,
        remaining_seconds=remaining,
    )


async def _expire_active_tunnel(gateway_id: str, expires_at: datetime) -> None:
    try:
        await asyncio.sleep(max(0, (expires_at - utc_now()).total_seconds()))
        # A renewed lease owns a newer expiry task and must not be closed by
        # the superseded one.
        if not tunnel_allowlist.remove_if_current(gateway_id, expires_at):
            return
        tunnel_session_manager.revoke_gateway(gateway_id)
        await tunnel_manager.close_gateway(gateway_id, code=1000)
    finally:
        if _tunnel_expiry_tasks.get(gateway_id) is asyncio.current_task():
            _tunnel_expiry_tasks.pop(gateway_id, None)


def _schedule_tunnel_expiry(gateway_id: str, expires_at: datetime) -> None:
    prior = _tunnel_expiry_tasks.get(gateway_id)
    if prior is not None and not prior.done():
        prior.cancel()
    _tunnel_expiry_tasks[gateway_id] = asyncio.create_task(_expire_active_tunnel(gateway_id, expires_at))


@app.post("/api/ui/gateways/{gateway_id}/tunnel/open", response_model=TunnelStatusOut)
async def ui_open_gateway_tunnel(
    gateway_id: str,
    payload: TunnelOpenIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> TunnelStatusOut:
    _require_gateway_site_access(db, auth, gateway_id)
    if settings.gateway_tunnel_websockets_disabled:
        raise HTTPException(status_code=503, detail="Gateway tunnels are currently disabled")
    expires_at = utc_now() + timedelta(minutes=payload.duration_minutes)
    if _relay_canary_selected(gateway_id):
        # gateway_id is the primary key: one durable authority row, safely
        # replaced/extended by repeated operator opens across Cloud workers.
        request = db.get(GatewayTunnelRequest, gateway_id)
        if request is None:
            request = GatewayTunnelRequest(gateway_id=gateway_id)
            db.add(request)
        request.requested_duration_minutes = payload.duration_minutes
        request.requested_at = utc_now()
        request.expires_at = expires_at
        request.requested_by = auth.email or auth.auth_type
        request.state = "requested"
        db.commit()
        with job_wait_condition:
            job_wait_condition.notify_all()
        return TunnelStatusOut(connected=False, status="opening", expires_at=expires_at, remaining_seconds=max(0, int((expires_at - utc_now()).total_seconds())))
    if not tunnel_allowlist.reserve(
        gateway_id,
        expires_at,
        maximum=settings.gateway_tunnel_max_active,
    ):
        raise HTTPException(status_code=409, detail="Tunnel capacity reached")
    _schedule_tunnel_expiry(gateway_id, expires_at)
    return TunnelStatusOut(
        connected=tunnel_manager.is_connected(gateway_id),
        status="connected" if tunnel_manager.is_connected(gateway_id) else "opening",
        expires_at=expires_at,
        remaining_seconds=max(0, int((expires_at - utc_now()).total_seconds())),
    )


@app.post("/api/ui/gateways/{gateway_id}/tunnel/close", response_model=TunnelStatusOut)
async def ui_close_gateway_tunnel(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> TunnelStatusOut:
    _require_gateway_site_access(db, auth, gateway_id)
    if _relay_canary_selected(gateway_id):
        request = db.get(GatewayTunnelRequest, gateway_id)
        if request is not None:
            request.state = "closed"
            request.expires_at = utc_now()
            db.commit()
        with job_wait_condition:
            job_wait_condition.notify_all()
        return TunnelStatusOut(connected=False, status="closed")
    task = _tunnel_expiry_tasks.pop(gateway_id, None)
    if task is not None:
        task.cancel()
    tunnel_allowlist.remove(gateway_id)
    tunnel_session_manager.revoke_gateway(gateway_id)
    await tunnel_manager.close_gateway(gateway_id, code=1000)
    return TunnelStatusOut(connected=False, status="closed")


@app.post("/api/ui/gateways/{gateway_id}/tunnel-session", response_model=TunnelSessionOut)
def ui_create_gateway_tunnel_session(
    gateway_id: str,
    payload: TunnelSessionCreateIn | None = Body(default=None),
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> TunnelSessionOut:
    _require_gateway_site_access(db, auth, gateway_id)
    if not tunnel_manager.is_connected(gateway_id):
        raise HTTPException(status_code=503, detail="Gateway tunnel is not connected")
    subject = auth.email or auth.auth_type
    session_kwargs = {"gateway_id": gateway_id, "subject": subject}
    if payload is not None:
        session_kwargs["ttl_seconds"] = payload.ttl_minutes * 60
    session = tunnel_session_manager.create(**session_kwargs)
    return TunnelSessionOut(url=f"{_tunnel_session_prefix(gateway_id, session.session_id)}/")


@app.websocket("/api/edge/tunnels/{gateway_id}")
async def edge_tunnel(
    gateway_id: str,
    websocket: WebSocket,
    authorization: str | None = Header(default=None),
) -> None:
    async def reject(code: int) -> None:
        # A peer may abandon a rejected handshake before the ASGI close send.
        # That race is normal for the fleet's expected admission denials.
        try:
            await websocket.close(code=code)
        except (RuntimeError, WebSocketDisconnect):
            pass

    if settings.gateway_tunnel_websockets_disabled:
        tunnel_metrics.record_rejected()
        await reject(1013)
        return

    canary_relay = _relay_canary_selected(gateway_id)
    # This is intentionally before authentication, SQLAlchemy, and the
    # concurrency gate: unattended legacy Agents knock every five seconds.
    # Unrequested gateways must be as cheap as the global kill switch.
    if not canary_relay and not tunnel_allowlist.allows(gateway_id):
        tunnel_metrics.record_rejected()
        await reject(1008)
        return

    # Leases reserve capacity at operator Open time. This check also protects
    # the live registry without involving SQLAlchemy or gateway auth.
    if (
        not canary_relay
        and not tunnel_manager.is_connected(gateway_id)
        and tunnel_manager.active_count() >= settings.gateway_tunnel_max_active
    ):
        tunnel_metrics.record_rejected()
        await reject(1013)
        return

    tunnel_metrics.record_auth_attempt()
    if not tunnel_auth_gate.try_acquire(settings.gateway_tunnel_auth_concurrency):
        tunnel_metrics.record_rejected()
        await reject(1013)
        return

    # QueuePool-exhaustion hotfix (2026-07-14): this endpoint previously took
    # `db: Session = Depends(get_db)`, whose pooled connection stayed checked
    # out for the WebSocket's entire lifetime — one connection held per
    # connected gateway tunnel, indefinitely. A fleet of tunnels exhausted the
    # pool (QueuePool size 10 + overflow 15). Authenticate with a short-lived
    # session instead, closed before accept() and before the receive loop.
    auth_started_at = time.monotonic()
    db = None
    try:
        db = SessionLocal()
        checkout_started_at = time.monotonic()
        db.connection()
        tunnel_metrics.record_db_checkout_wait((time.monotonic() - checkout_started_at) * 1000)
        auth = require_gateway_auth(authorization=authorization, db=db)
        if auth.gateway_id != gateway_id:
            tunnel_metrics.record_rejected()
            await reject(1008)
            return
    except HTTPException:
        tunnel_metrics.record_rejected()
        await reject(1008)
        return
    finally:
        if db is not None:
            db.close()
        tunnel_metrics.record_auth_duration((time.monotonic() - auth_started_at) * 1000)
        tunnel_auth_gate.release()

    if canary_relay:
        durable_db = SessionLocal()
        try:
            if _active_durable_tunnel_request(durable_db, gateway_id) is None:
                tunnel_metrics.record_rejected()
                await reject(1008)
                return
        finally:
            durable_db.close()
    await websocket.accept()
    tunnel_metrics.record_accepted()
    if canary_relay:
        await relay_client(gateway_id, websocket, owner_url=settings.iot_tunnel_relay_owner_url, owner_secret=settings.iot_tunnel_relay_owner_secret)
        return
    tunnel, replaced_tunnel = tunnel_manager.register(gateway_id, websocket)
    if replaced_tunnel is not None:
        tunnel_metrics.record_duplicate_replacement()
    try:
        while True:
            tunnel.resolve_response(await websocket.receive_json())
    except WebSocketDisconnect:
        tunnel_manager.unregister(gateway_id, tunnel)
    except Exception:
        tunnel_manager.unregister(gateway_id, tunnel)
        raise


@app.api_route(
    "/gateways/{gateway_id}/tunnel/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_gateway_tunnel(
    gateway_id: str,
    path: str,
    request: Request,
    _: AdminAuthContext = Depends(require_job_operator_auth),
) -> Response:
    with SessionLocal() as db:
        _get_gateway_or_404(db, gateway_id)
    return await _proxy_gateway_tunnel_request(
        gateway_id=gateway_id,
        path=path,
        request=request,
        redirect_prefix=_tunnel_proxy_prefix(gateway_id),
        allow_cookie_headers=False,
        rewrite_html_body=False,
    )


@app.api_route(
    "/gateways/{gateway_id}/tunnel/session/{session_id}/",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/gateways/{gateway_id}/tunnel/session/{session_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_gateway_tunnel_session(
    gateway_id: str,
    session_id: str,
    request: Request,
    path: str = "",
) -> Response:
    with SessionLocal() as db:
        _get_gateway_or_404(db, gateway_id)
    try:
        tunnel_session_manager.get(gateway_id=gateway_id, session_id=session_id)
    except TunnelUnavailable as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await _proxy_gateway_tunnel_request(
        gateway_id=gateway_id,
        path=path,
        request=request,
        redirect_prefix=_tunnel_session_prefix(gateway_id, session_id),
        allow_cookie_headers=True,
        rewrite_html_body=True,
    )


async def _proxy_gateway_tunnel_request(
    *,
    gateway_id: str,
    path: str,
    request: Request,
    redirect_prefix: str,
    allow_cookie_headers: bool,
    rewrite_html_body: bool,
) -> Response:
    stripped_headers = {"host", "content-length", "connection", "authorization"}
    if not allow_cookie_headers:
        stripped_headers.add("cookie")
    request_body = await request.body()
    forward_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in stripped_headers
    }
    incoming_cookie_header = request.headers.get("cookie")
    if allow_cookie_headers:
        deduplicated_cookie = _deduplicate_cookie_header(incoming_cookie_header)
        if deduplicated_cookie:
            forward_headers["cookie"] = deduplicated_cookie
        else:
            forward_headers.pop("cookie", None)
    upstream_path = f"/{path}"
    incoming_headers = {key.lower(): value for key, value in request.headers.items()}
    inbound_cookie_names, inbound_cookie_count = _safe_cookie_summary(incoming_cookie_header)
    forwarded_cookie_names, forwarded_cookie_count = _safe_cookie_summary(forward_headers.get("cookie"))
    logger.warning(
        "TUNNEL_PROXY_DEBUG request gateway=%s inbound_method=%s inbound_path=%s upstream_method=%s upstream_path=%s "
        "query_keys=%s body_bytes=%s content_type=%s inbound_cookie_names=%s inbound_cookie_count=%s "
        "forwarded_cookie_names=%s forwarded_cookie_count=%s html_rewrite_enabled=%s",
        gateway_id,
        request.method,
        request.url.path,
        request.method,
        upstream_path,
        _safe_tunnel_query_keys(request.url.query),
        len(request_body),
        incoming_headers.get("content-type", ""),
        inbound_cookie_names,
        inbound_cookie_count,
        forwarded_cookie_names,
        forwarded_cookie_count,
        rewrite_html_body,
    )
    try:
        tunnel = tunnel_manager.get(gateway_id)
        tunnel_response = await tunnel.request(
            method=request.method,
            path=upstream_path,
            query_string=request.url.query,
            headers=forward_headers,
            body=request_body,
            timeout_sec=settings.tunnel_request_timeout_sec,
        )
    except TunnelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Gateway tunnel request timed out") from exc
    except TunnelRequestFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    response_headers = _tunnel_response_headers(
        tunnel_response,
        redirect_prefix=redirect_prefix,
        allow_set_cookie=allow_cookie_headers,
        rewrite_html_body=rewrite_html_body,
    )
    content_type = next((value for key, value in response_headers.items() if key.lower() == "content-type"), "")
    response_body = tunnel_response.body
    html_rewritten = False
    json_rewritten = False
    javascript_rewritten = False
    if rewrite_html_body and "text/html" in content_type.lower():
        response_body = _rewrite_tunnel_html_body(tunnel_response.body, redirect_prefix)
        html_rewritten = response_body != tunnel_response.body
    elif rewrite_html_body and "application/json" in content_type.lower():
        response_body = _rewrite_tunnel_json_body(tunnel_response.body, redirect_prefix)
        json_rewritten = response_body != tunnel_response.body
    elif rewrite_html_body and (
        "javascript" in content_type.lower() or "ecmascript" in content_type.lower()
    ):
        response_body = _rewrite_tunnel_javascript_body(tunnel_response.body, redirect_prefix)
        javascript_rewritten = response_body != tunnel_response.body

    response_header_names = {key.lower() for key in response_headers}
    upstream_location = next((value for key, value in tunnel_response.headers.items() if key.lower() == "location"), None)
    response_location = next((value for key, value in response_headers.items() if key.lower() == "location"), None)
    logger.warning(
        "TUNNEL_PROXY_DEBUG response gateway=%s method=%s path=%s status=%s content_type=%s body_bytes=%s "
        "upstream_location_shape=%s response_location_shape=%s response_location_session_slash=%s "
        "set_cookie_received=%s set_cookie_forwarded=%s html_rewritten=%s json_rewritten=%s javascript_rewritten=%s",
        gateway_id,
        request.method,
        upstream_path,
        tunnel_response.status_code,
        content_type,
        len(response_body),
        _tunnel_location_shape(upstream_location),
        _tunnel_location_shape(response_location),
        f"{redirect_prefix}/" in response_location if response_location else False,
        any(key.lower() == "set-cookie" for key in tunnel_response.headers),
        "set-cookie" in response_header_names,
        html_rewritten,
        json_rewritten,
        javascript_rewritten,
    )

    return Response(
        content=response_body,
        status_code=tunnel_response.status_code,
        headers=response_headers,
    )


@app.get("/api/ui/gateways/{gateway_id}/tree", response_model=GatewayTreeOut)
def ui_get_gateway_tree(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> GatewayTreeOut:
    gateway = _require_gateway_site_access(db, auth, gateway_id)
    groups = list(db.scalars(select(GatewayGroup).where(GatewayGroup.gateway_id == gateway_id).order_by(GatewayGroup.name)).all())
    devices = list(
        db.scalars(
            select(SavedBacnetDevice)
            .where(SavedBacnetDevice.gateway_id == gateway_id, SavedBacnetDevice.enabled.is_(True))
            .order_by(SavedBacnetDevice.device_instance)
        ).all()
    )
    points = list(
        db.scalars(
            select(SavedBacnetPoint)
            .where(SavedBacnetPoint.gateway_id == gateway_id, SavedBacnetPoint.enabled.is_(True))
            .order_by(SavedBacnetPoint.device_instance, SavedBacnetPoint.object_type, SavedBacnetPoint.object_instance)
        ).all()
    )
    trend_configs = {
        config.point_id: config
        for config in db.scalars(select(PointTrendConfig).where(PointTrendConfig.point_id.in_([point.id for point in points]))).all()
    }
    return GatewayTreeOut(
        gateway=GatewayOut(**_gateway_out(gateway, db=db)),
        groups=[GatewayGroupOut(**_group_out(group)) for group in groups],
        devices=[SavedDeviceOut(**_device_out(device)) for device in devices],
        points=[SavedPointOut(**_point_out(point, trend_configs.get(point.id))) for point in points],
    )


@app.get("/api/ui/equipment-templates")
def ui_equipment_templates(auth: AdminAuthContext = Depends(require_operator_auth)) -> dict[str, object]:
    """Registry metadata for configuring and rendering Cloud equipment."""
    return {
        key: {
            "label": item["label"],
            "categories": sorted(item["categories"]),
            "roles": list(item["roles"]),
            "role_labels": {role: default_display_label(role) for role in item["roles"]},
            "summary_roles": list(item["summary_roles"]),
        }
        for key, item in TEMPLATES.items()
    }


def _mapping_template_out(template: MappingTemplate) -> dict[str, object]:
    return {"id": template.id, "name": template.name, "graphic_template_key": template.graphic_template_key, "rules": [{"logical_role": rule.logical_role, "display_label": rule.display_label, "match_field": rule.match_field, "match_value": rule.match_value, "object_type": rule.object_type, "required": rule.required} for rule in sorted(template.rules, key=lambda item: item.logical_role)]}


def _validate_mapping_template(payload: MappingTemplateIn) -> None:
    graphic = template_for(payload.graphic_template_key)
    if graphic is None:
        raise HTTPException(status_code=422, detail="Unknown graphic template")
    roles = [rule.logical_role for rule in payload.rules]
    if len(roles) != len(set(roles)):
        raise HTTPException(status_code=422, detail="Duplicate logical role in mapping template")
    for rule in payload.rules:
        if rule.match_field != "object_name":
            raise HTTPException(status_code=422, detail="Unsupported match field")
        if rule.logical_role not in graphic["roles"]:
            raise HTTPException(status_code=422, detail=f"Role {rule.logical_role} is not supported by {payload.graphic_template_key}")


@app.get("/api/ui/mapping-templates", response_model=list[MappingTemplateOut])
def ui_list_mapping_templates(auth: AdminAuthContext = Depends(require_operator_auth), db: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [_mapping_template_out(template) for template in db.scalars(select(MappingTemplate).order_by(MappingTemplate.name)).all()]


@app.post("/api/ui/mapping-templates", response_model=MappingTemplateOut)
def ui_create_mapping_template(payload: MappingTemplateIn, auth: AdminAuthContext = Depends(require_job_operator_auth), db: Session = Depends(get_db)) -> dict[str, object]:
    _validate_mapping_template(payload)
    template = MappingTemplate(name=payload.name.strip(), graphic_template_key=payload.graphic_template_key)
    template.rules = [
        MappingTemplateRule(**(rule.model_dump() | {"display_label": (rule.display_label or "").strip() or None}))
        for rule in payload.rules
    ]
    db.add(template)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Mapping template name already exists") from None
    db.refresh(template)
    return _mapping_template_out(template)


@app.get("/api/ui/mapping-templates/{template_id}/export")
def ui_export_mapping_template(template_id: str, auth: AdminAuthContext = Depends(require_operator_auth), db: Session = Depends(get_db)) -> Response:
    template = db.get(MappingTemplate, _tree_id(template_id))
    if template is None:
        raise HTTPException(status_code=404, detail="Mapping template not found")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["template_name", "graphic_template", "logical_role", "display_label", "match_field", "match_value", "object_type", "required"])
    writer.writeheader()
    for rule in sorted(template.rules, key=lambda item: item.logical_role):
        writer.writerow({"template_name": template.name, "graphic_template": template.graphic_template_key, "logical_role": rule.logical_role, "display_label": rule.display_label or default_display_label(rule.logical_role), "match_field": rule.match_field, "match_value": rule.match_value, "object_type": rule.object_type or "", "required": str(rule.required).lower()})
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{template.name}.csv"'})


@app.post("/api/ui/mapping-templates/import", response_model=MappingTemplateOut)
def ui_import_mapping_template(csv_text: str = Body(..., media_type="text/plain"), auth: AdminAuthContext = Depends(require_job_operator_auth), db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        rows = list(csv.DictReader(io.StringIO(csv_text)))
    except csv.Error as exc:
        raise HTTPException(status_code=422, detail="Malformed CSV") from exc
    legacy_columns = {"template_name", "graphic_template", "logical_role", "match_field", "match_value", "object_type", "required"}
    current_columns = legacy_columns | {"display_label"}
    if not rows or frozenset(rows[0]) not in {frozenset(legacy_columns), frozenset(current_columns)}:
        raise HTTPException(status_code=422, detail="CSV columns are invalid")
    names = {row["template_name"].strip() for row in rows}
    graphics = {row["graphic_template"].strip() for row in rows}
    if len(names) != 1 or len(graphics) != 1 or not next(iter(names)):
        raise HTTPException(status_code=422, detail="CSV must contain one named mapping template")
    rules = []
    for row in rows:
        value = row["required"].strip().lower()
        if value not in {"true", "false"}:
            raise HTTPException(status_code=422, detail="required must be true or false")
        rules.append({"logical_role": row["logical_role"].strip(), "display_label": (row.get("display_label") or "").strip() or None, "match_field": row["match_field"].strip(), "match_value": row["match_value"].strip(), "object_type": row["object_type"].strip() or None, "required": value == "true"})
    return ui_create_mapping_template(MappingTemplateIn(name=next(iter(names)), graphic_template_key=next(iter(graphics)), rules=rules), auth, db)


@app.post("/api/ui/gateways/{gateway_id}/groups", response_model=GatewayGroupOut)
def ui_create_group(
    gateway_id: str,
    payload: GatewayGroupIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_gateway_site_access(db, auth, gateway_id)
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
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    group = _require_group_site_access(db, auth, group_id)
    group.name = payload.name.strip()
    group.updated_at = utc_now()
    db.commit()
    db.refresh(group)
    return _group_out(group)


@app.delete("/api/ui/groups/{group_id}", status_code=204)
def ui_delete_group(
    group_id: str,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> None:
    group = _require_group_site_access(db, auth, group_id)
    # Deleting a folder must not delete its controllers. Preserve the saved
    # inventory and place its controllers under the tree's Ungrouped branch.
    for device in db.scalars(select(SavedBacnetDevice).where(SavedBacnetDevice.group_id == group.id)).all():
        device.group_id = None
        device.updated_at = utc_now()
    db.delete(group)
    db.commit()


@app.post("/api/ui/gateways/{gateway_id}/devices", response_model=SavedDeviceOut)
def ui_save_device(
    gateway_id: str,
    payload: SavedDeviceIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_gateway_site_access(db, auth, gateway_id)
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
        first_seen_at=utc_now(),
        last_seen_at=utc_now(),
        lifecycle_state="active",
        enabled=payload.enabled,
        template_key=payload.template_key,
    )
    _validate_template_assignment(db, device, payload.template_key)
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
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    device = _require_device_site_access(db, auth, device_id)
    if "group_id" in payload.model_fields_set:
        if payload.group_id is None:
            device.group_id = None
        else:
            group_id = _tree_id(payload.group_id)
            group = db.get(GatewayGroup, group_id)
            if group is None or group.gateway_id != device.gateway_id:
                raise HTTPException(status_code=404, detail="Group not found")
            device.group_id = group_id
        _validate_template_assignment(db, device, device.template_key)
    if payload.device_name is not None:
        device.device_name = payload.device_name
    if payload.vendor_name is not None:
        device.vendor_name = payload.vendor_name
    if payload.enabled is not None:
        device.enabled = payload.enabled
    if "template_key" in payload.model_fields_set:
        _validate_template_assignment(db, device, payload.template_key)
        device.template_key = payload.template_key
    device.updated_at = utc_now()
    db.commit()
    db.refresh(device)
    return _device_out(device)


@app.put("/api/ui/devices/{device_id}/configuration", response_model=DeviceConfigurationOut)
def ui_save_device_configuration(
    device_id: str,
    payload: DeviceConfigurationIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Persist one device's group, graphic template, and manual bindings together.

    The browser deliberately saves only on this explicit action.  It is not a
    template enforcer: every submitted role remains an operator-owned binding.
    """
    device = _require_device_site_access(db, auth, device_id)
    group_name = (payload.group_name or "").strip()
    points = list(db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id)).all())
    points_by_id = {point.id: point for point in points}
    template = template_for(payload.template_key)
    if payload.template_key is not None and template is None:
        raise HTTPException(status_code=422, detail="Unknown equipment template")
    proposed_category = group_name if group_name and group_name != "Uncategorized" else None
    if template is not None and proposed_category not in template["categories"]:
        raise HTTPException(status_code=422, detail=f"Template {payload.template_key} is not compatible with device group {proposed_category or 'Uncategorized'}")

    role_first = "role_points" in payload.model_fields_set or "role_display_labels" in payload.model_fields_set
    if role_first and "point_roles" in payload.model_fields_set and payload.point_roles:
        raise HTTPException(status_code=422, detail="Submit role_points or point_roles, not both")
    final_roles: dict[str, str | None]
    normalized_labels: dict[str, str | None] = {}
    if role_first:
        if template is None:
            if any(payload.role_points.values()):
                raise HTTPException(status_code=422, detail="Assign a compatible equipment template before binding roles")
            supported_roles: set[str] = set()
        else:
            supported_roles = set(template["roles"])
        if unknown_roles := sorted((set(payload.role_points) | set(payload.role_display_labels)) - supported_roles):
            raise HTTPException(status_code=422, detail="Role is not supported by template: " + ", ".join(unknown_roles))
        selected_ids = [point_id for point_id in payload.role_points.values() if point_id is not None]
        if unknown_ids := sorted(set(selected_ids) - set(points_by_id)):
            raise HTTPException(status_code=422, detail="Point does not belong to this device: " + ", ".join(unknown_ids))
        point_claims: dict[str, list[str]] = {}
        for role, point_id in payload.role_points.items():
            if point_id:
                point_claims.setdefault(point_id, []).append(role)
        duplicate_points = {point_id: roles for point_id, roles in point_claims.items() if len(roles) > 1}
        if duplicate_points:
            messages = []
            for point_id, roles in duplicate_points.items():
                point = points_by_id[point_id]
                point_name = point.object_name or f"{point.object_type}:{point.object_instance}"
                messages.append(
                    f"{point_name} is already assigned to {default_display_label(roles[0])} "
                    f"and cannot also be assigned to {default_display_label(roles[1])}."
                )
            raise HTTPException(status_code=409, detail=" ".join(messages))
        for role, value in payload.role_display_labels.items():
            label = (value or "").strip()
            if len(label) > 120:
                raise HTTPException(status_code=422, detail=f"Display label for {role} exceeds 120 characters")
            normalized_labels[role] = None if not label or label == default_display_label(role) else label
        final_roles = {point.id: None for point in points}
        for role, point_id in payload.role_points.items():
            if point_id:
                final_roles[point_id] = role
    else:
        if unknown_ids := sorted(set(payload.point_roles) - set(points_by_id)):
            raise HTTPException(status_code=422, detail="Point does not belong to this device: " + ", ".join(unknown_ids))
        final_roles = {point.id: payload.point_roles.get(point.id, point.logical_role) for point in points}
        role_claims: dict[str, list[SavedBacnetPoint]] = {}
        for point in points:
            role = final_roles[point.id]
            if role is not None:
                role_claims.setdefault(role, []).append(point)
        duplicate_claims = {role: claims for role, claims in role_claims.items() if len(claims) > 1}
        if duplicate_claims:
            detail = "; ".join(f"{role} is assigned to " + " and ".join(point.object_name or f"{point.object_type}:{point.object_instance}" for point in claims) for role, claims in sorted(duplicate_claims.items()))
            raise HTTPException(status_code=409, detail=detail)
    if any(role is not None for role in final_roles.values()) and template is None:
        raise HTTPException(status_code=422, detail="Assign a compatible equipment template before binding roles")
    unsupported = sorted({role for role in final_roles.values() if role is not None and template is not None and role not in template["roles"]})
    if unsupported:
        raise HTTPException(status_code=422, detail="Role is not supported by template: " + ", ".join(unsupported))
    if payload.mapping_template_id:
        mapping_template = db.get(MappingTemplate, _tree_id(payload.mapping_template_id))
        if mapping_template is None or mapping_template.graphic_template_key != payload.template_key:
            raise HTTPException(status_code=422, detail="Mapping template is not compatible with the graphic template")
        device.mapping_template_id = mapping_template.id
    else:
        device.mapping_template_id = None
    if proposed_category:
        group = db.scalar(select(GatewayGroup).where(GatewayGroup.gateway_id == device.gateway_id, GatewayGroup.name == proposed_category))
        if group is None:
            group = GatewayGroup(gateway_id=device.gateway_id, name=proposed_category)
            db.add(group)
            db.flush()
        device.group_id = group.id
    else:
        device.group_id = None
    device.template_key = payload.template_key
    if role_first:
        for point in points:
            point.logical_role = final_roles[point.id]
            point.display_label = normalized_labels.get(point.logical_role) if point.logical_role else None
            point.updated_at = utc_now()
    else:
        for point_id, role in payload.point_roles.items():
            point = points_by_id[point_id]
            point.logical_role = role
            if role is None:
                point.display_label = None
            point.updated_at = utc_now()
    device.updated_at = utc_now()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That logical role is already bound to another point on this device") from None
    db.refresh(device)
    for point in points:
        db.refresh(point)
    return {"device": _device_out(device), "points": [_point_out(point) for point in points]}


@app.post("/api/ui/devices/{device_id}/mapping-template/{template_id}/apply", response_model=MappingApplyOut)
def ui_apply_mapping_template(device_id: str, template_id: str, auth: AdminAuthContext = Depends(require_job_operator_auth), db: Session = Depends(get_db)) -> dict[str, object]:
    device = _require_device_site_access(db, auth, device_id)
    template = db.get(MappingTemplate, _tree_id(template_id))
    if template is None:
        raise HTTPException(status_code=404, detail="Mapping template not found")
    if device.template_key != template.graphic_template_key:
        raise HTTPException(status_code=422, detail="Mapping template is not compatible with the device graphic template")
    device.mapping_template_id = template.id
    points = list(db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id, SavedBacnetPoint.enabled.is_(True))).all())
    conflicts: list[str] = []
    matched = unmatched_optional = missing_required = retained_existing = 0
    claimed: set[str] = set()
    for rule in template.rules:
        matches = [point for point in points if (point.object_name or "").strip().casefold() == rule.match_value.strip().casefold() and (not rule.object_type or point.object_type == rule.object_type)]
        if not matches:
            if rule.required: missing_required += 1
            else: unmatched_optional += 1
            continue
        if len(matches) != 1:
            conflicts.append(f"{rule.logical_role}: {len(matches)} matches")
            continue
        point = matches[0]
        if point.id in claimed:
            conflicts.append(f"{rule.logical_role}: point also matches another role")
            continue
        claimed.add(point.id)
        if point.logical_role == rule.logical_role:
            if point.display_label is None and rule.display_label is not None:
                point.display_label = rule.display_label
                point.updated_at = utc_now()
            retained_existing += 1
            continue
        if point.logical_role is not None:
            retained_existing += 1
            continue
        if any(other.logical_role == rule.logical_role for other in points):
            retained_existing += 1
            continue
        point.logical_role = rule.logical_role
        point.display_label = rule.display_label
        point.updated_at = utc_now()
        matched += 1
    device.updated_at = utc_now()
    db.commit()
    return {"matched": matched, "unmatched_optional": unmatched_optional, "missing_required": missing_required, "conflicts": conflicts, "retained_existing": retained_existing}


@app.post("/api/ui/devices/{device_id}/mapping-template", response_model=MappingTemplateOut)
def ui_create_mapping_template_from_device(device_id: str, payload: MappingTemplateFromDeviceIn, auth: AdminAuthContext = Depends(require_job_operator_auth), db: Session = Depends(get_db)) -> dict[str, object]:
    device = _require_device_site_access(db, auth, device_id)
    if template_for(device.template_key) is None:
        raise HTTPException(status_code=422, detail="Choose a graphic template before saving a mapping template")
    points = list(db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id, SavedBacnetPoint.logical_role.is_not(None))).all())
    if not points:
        raise HTTPException(status_code=422, detail="No saved bindings available to create a mapping template")
    if any(not point.object_name for point in points):
        raise HTTPException(status_code=422, detail="A bound point is missing an object name and cannot become a reusable rule")
    template = MappingTemplate(name=payload.name.strip(), graphic_template_key=device.template_key)
    template.rules = [MappingTemplateRule(logical_role=point.logical_role, display_label=point.display_label or default_display_label(point.logical_role), match_field="object_name", match_value=point.object_name, object_type=point.object_type, required=False) for point in points]
    db.add(template)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Mapping template name already exists") from None
    db.refresh(template)
    return _mapping_template_out(template)


@app.delete("/api/ui/devices/{device_id}", response_model=SavedDeviceOut)
def ui_remove_device(
    device_id: str,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    device = _require_device_site_access(db, auth, device_id)
    device.enabled = False
    device.updated_at = utc_now()
    device.lifecycle_state = "retired"
    device.retired_at = device.updated_at
    device_points = db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id)).all()
    for point in device_points:
        point.enabled = False
        point.updated_at = utc_now()
        point.lifecycle_state = "retired"
        point.retired_at = point.updated_at
    _disable_trend_configs_for_points(db, [point.id for point in device_points], device.updated_at)
    db.commit()
    db.refresh(device)
    return _device_out(device)


@app.post("/api/ui/devices/{device_id}/load-points", response_model=JobOut)
def ui_load_device_points(
    device_id: str,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> EdgeJob:
    device = _require_device_site_access(db, auth, device_id)
    if not device.enabled:
        raise HTTPException(status_code=404, detail="Device not found")
    edge_node = _require_gateway_site_access(db, auth, device.gateway_id)
    _require_online_gateway(edge_node)
    bacnet_port = edge_node.bacnet_port
    job = EdgeJob(
        job_id=f"job-{uuid4().hex}",
        gateway_id=device.gateway_id,
        job_type="bacnet_load_points",
        status="queued",
        request_json={
            "device_instance": device.device_instance,
            "saved_device_id": str(device.id),
            "bacnet_port": bacnet_port,
            "limit": 80,
            "name_limit": 40,
            "include_object_names": True,
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    with job_wait_condition:
        job_wait_condition.notify_all()
    return job


@app.post("/api/ui/devices/{device_id}/points", response_model=SavedPointOut)
def ui_save_point(
    device_id: str,
    payload: SavedPointIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    device = _require_device_site_access(db, auth, device_id)
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
        first_seen_at=utc_now(),
        last_seen_at=utc_now(),
        lifecycle_state="active",
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


def _disable_trend_configs_for_points(db: Session, point_ids: list[str], now: datetime) -> int:
    """Disable enabled trend configs for the given points (same transaction
    as retirement). GW032 incident: a retired point with a live trend config
    keeps the edge sampling ghosts and grows its upload queue."""
    if not point_ids:
        return 0
    configs = db.scalars(
        select(PointTrendConfig).where(
            PointTrendConfig.point_id.in_(point_ids),
            PointTrendConfig.enabled.is_(True),
        )
    ).all()
    for config in configs:
        config.enabled = False
        config.updated_at = now
    return len(configs)


@app.delete("/api/ui/points/{point_id}", response_model=SavedPointOut)
def ui_remove_point(
    point_id: str,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    point = _require_point_site_access(db, auth, point_id)
    point.enabled = False
    point.updated_at = utc_now()
    point.lifecycle_state = "retired"
    point.retired_at = point.updated_at
    _disable_trend_configs_for_points(db, [point.id], point.updated_at)
    db.commit()
    db.refresh(point)
    return _point_out(point)


@app.post("/api/ui/points/bulk-remove", response_model=SavedPointsBulkRemoveOut)
def ui_bulk_remove_points(
    payload: SavedPointsBulkRemoveIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> SavedPointsBulkRemoveOut:
    point_ids: list[str] = []
    for point_id in payload.point_ids:
        point_ids.append(_tree_id(point_id))
    points = list(db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.id.in_(point_ids))).all())
    for point in points:
        _require_gateway_site_access(db, auth, point.gateway_id)
    points_by_id = {str(point.id): point for point in points}
    now = utc_now()
    removed_count = 0
    for point in points:
        if point.enabled:
            point.enabled = False
            point.updated_at = now
            point.lifecycle_state = "retired"
            point.retired_at = now
            removed_count += 1
    _disable_trend_configs_for_points(db, [point.id for point in points], now)
    db.commit()
    missing_ids = [point_id for point_id in payload.point_ids if _tree_id(point_id) not in points_by_id]
    return SavedPointsBulkRemoveOut(
        requested_count=len(payload.point_ids),
        removed_count=removed_count,
        missing_ids=missing_ids,
    )


@app.post("/api/ui/gateways/{gateway_id}/points/read", response_model=SavedPointsReadOut)
def ui_read_saved_points(
    gateway_id: str,
    payload: SavedPointsReadIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> SavedPointsReadOut:
    edge_node = _require_gateway_site_access(db, auth, gateway_id)
    _require_online_gateway(edge_node)
    point_ids = [_tree_id(point_id) for point_id in payload.point_ids]
    points = list(
        db.scalars(
            select(SavedBacnetPoint)
            .where(
                SavedBacnetPoint.id.in_(point_ids),
                SavedBacnetPoint.gateway_id == gateway_id,
                SavedBacnetPoint.enabled.is_(True),
            )
            .order_by(SavedBacnetPoint.device_instance, SavedBacnetPoint.object_type, SavedBacnetPoint.object_instance)
        ).all()
    )
    points_by_id = {str(point.id): point for point in points}
    missing_ids = [point_id for point_id in payload.point_ids if _tree_id(point_id) not in points_by_id]
    job_ids: list[str] = []
    points_by_device: dict[int, list[SavedBacnetPoint]] = {}
    for point in points:
        points_by_device.setdefault(point.device_instance, []).append(point)
    for device_instance, device_points in points_by_device.items():
        job = EdgeJob(
            job_id=f"job-{uuid4().hex}",
            gateway_id=gateway_id,
            job_type="bacnet_read_bulk",
            status="queued",
            request_json={
                "device_instance": device_instance,
                "property": "present-value",
                "points": [
                    {
                        "saved_point_id": str(point.id),
                        "object_type": point.object_type,
                        "object_instance": point.object_instance,
                        "object_name": point.object_name,
                        "read_priority": point.object_type in BACNET_WRITE_OBJECT_TYPES,
                    }
                    for point in device_points
                ],
            },
        )
        db.add(job)
        job_ids.append(job.job_id)
    db.commit()
    with job_wait_condition:
        job_wait_condition.notify_all()
    return SavedPointsReadOut(
        requested_count=len(payload.point_ids),
        queued_count=len(job_ids),
        skipped_count=len(missing_ids),
        job_ids=job_ids,
        missing_ids=missing_ids,
    )


@app.post("/api/ui/gateways/{gateway_id}/points/write", response_model=BacnetWriteBatchOut)
def ui_write_saved_points(
    gateway_id: str,
    payload: SavedPointsWriteIn,
    auth: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    edge_node = _require_gateway_site_access(db, auth, gateway_id)
    _require_online_gateway(edge_node)

    normalized_writes: list[tuple[str, object]] = [(_tree_id(item.point_id), item) for item in payload.writes]
    normalized_point_ids = [point_id for point_id, _ in normalized_writes]
    if len(set(normalized_point_ids)) != len(normalized_point_ids):
        raise HTTPException(status_code=422, detail="writes must not contain duplicate point_id values")

    points = list(
        db.scalars(
            select(SavedBacnetPoint).where(
                SavedBacnetPoint.id.in_(normalized_point_ids),
                SavedBacnetPoint.gateway_id == gateway_id,
                SavedBacnetPoint.enabled.is_(True),
            )
        ).all()
    )
    points_by_id = {str(point.id): point for point in points}
    missing_ids = [item.point_id for point_id, item in normalized_writes if point_id not in points_by_id]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail={"message": "Saved point was not found or is disabled", "point_ids": missing_ids},
        )

    rejected_points: list[dict[str, object]] = []
    for point_id, item in normalized_writes:
        point = points_by_id[point_id]
        reason: str | None = None
        if point.object_type not in BACNET_WRITE_OBJECT_TYPES:
            reason = f"object type {point.object_type} is not writable"
        elif point.property_name != "present-value":
            reason = f"property {point.property_name} is not supported"
        elif point.writable is False:
            reason = "point is marked read-only"
        if reason:
            rejected_points.append({"point_id": item.point_id, "reason": reason})
    if rejected_points:
        raise HTTPException(
            status_code=422,
            detail={"message": "One or more points cannot be written", "points": rejected_points},
        )

    now = utc_now()
    actor = _write_audit_actor(auth)
    batch = BacnetWriteBatch(
        gateway_id=gateway_id,
        requested_by=actor,
        approved_by=None,
        status="pending_approval",
        write_count=len(normalized_writes),
        requested_at=now,
        approved_at=None,
    )
    db.add(batch)
    db.flush()

    for point_id, item in normalized_writes:
        point = points_by_id[point_id]
        batch.commands.append(
            BacnetWriteCommand(
                edge_job_id="",
                gateway_id=gateway_id,
                saved_point_id=str(point.id),
                device_instance=point.device_instance,
                object_type=point.object_type,
                object_instance=point.object_instance,
                property_name=point.property_name,
                action=item.action,
                requested_value=item.value,
                priority=item.priority,
                status="pending_approval",
                created_at=now,
            )
        )

    db.commit()
    db.refresh(batch)
    return _write_batch_out(batch)


@app.post("/api/ui/gateways/{gateway_id}/points/write/{batch_id}/approve", response_model=BacnetWriteBatchOut)
def ui_approve_saved_point_write(
    gateway_id: str,
    batch_id: UUID,
    auth: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_gateway_site_access(db, auth, gateway_id)
    batch = db.get(BacnetWriteBatch, batch_id)
    if batch is None or batch.gateway_id != gateway_id:
        raise HTTPException(status_code=404, detail="Write batch not found")
    if batch.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Write batch is not awaiting approval")

    now = utc_now()
    batch.approved_by = _write_audit_actor(auth)
    batch.approved_at = now
    batch.status = "queued"
    commands_by_device: dict[int, list[BacnetWriteCommand]] = {}
    for command in batch.commands:
        commands_by_device.setdefault(command.device_instance, []).append(command)
    for device_instance, commands in commands_by_device.items():
        job_id = f"job-{uuid4().hex}"
        writes: list[dict[str, object]] = []
        for command in commands:
            request_item: dict[str, object] = {
                "saved_point_id": command.saved_point_id,
                "object_type": command.object_type,
                "object_instance": command.object_instance,
                "action": command.action,
                "priority": command.priority,
            }
            if command.action != "relinquish":
                request_item["value"] = command.requested_value
            writes.append(request_item)
            command.edge_job_id = job_id
            command.status = "queued"
        db.add(
            EdgeJob(
                job_id=job_id,
                gateway_id=gateway_id,
                job_type="bacnet_write_batch",
                status="queued",
                request_json={"device_instance": device_instance, "writes": writes},
            )
        )
    db.commit()
    db.refresh(batch)
    return _write_batch_out(batch)


@app.get("/api/ui/gateways/{gateway_id}/points/write-audit", response_model=list[BacnetWriteBatchOut])
def ui_list_point_write_audit(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    _require_gateway_site_access(db, auth, gateway_id)
    batches = list(
        db.scalars(
            select(BacnetWriteBatch)
            .where(BacnetWriteBatch.gateway_id == gateway_id)
            .order_by(BacnetWriteBatch.requested_at.desc(), BacnetWriteBatch.id.desc())
            .limit(limit)
        ).all()
    )
    return [_write_batch_out(batch) for batch in batches]


@app.patch("/api/ui/points/{point_id}", response_model=SavedPointOut)
def ui_patch_point(
    point_id: str,
    payload: SavedPointPatchIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    point = _require_point_site_access(db, auth, point_id)
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
    if "logical_role" in payload.model_fields_set:
        _validate_logical_role(db, point, payload.logical_role)
        point.logical_role = payload.logical_role
        if payload.logical_role is None:
            point.display_label = None
    if "display_label" in payload.model_fields_set:
        label = (payload.display_label or "").strip()
        point.display_label = None if not label or (point.logical_role and label == default_display_label(point.logical_role)) else label
    point.updated_at = utc_now()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That logical role is already bound to another point on this device") from None
    db.refresh(point)
    return _point_out(point)


@app.put("/api/ui/points/{point_id}/trend", response_model=PointTrendConfigOut)
def ui_upsert_point_trend(
    point_id: str,
    payload: PointTrendConfigIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> PointTrendConfig:
    point = _require_point_site_access(db, auth, point_id)
    config = db.get(PointTrendConfig, point.id)
    if config is None:
        config = PointTrendConfig(point_id=point.id, gateway_id=point.gateway_id)
        db.add(config)
    config.enabled = payload.enabled
    config.interval_sec = payload.interval_sec
    config.updated_at = utc_now()
    db.commit()
    db.refresh(config)
    return config


@app.put("/api/ui/gateways/{gateway_id}/trends", response_model=list[PointTrendConfigOut])
def ui_upsert_gateway_point_trends(
    gateway_id: str,
    payload: PointTrendConfigBulkIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> list[PointTrendConfig]:
    _require_gateway_site_access(db, auth, gateway_id)
    point_ids = {_tree_id(point_id) for point_id in payload.point_ids}
    points = list(
        db.scalars(
            select(SavedBacnetPoint).where(
                SavedBacnetPoint.gateway_id == gateway_id,
                SavedBacnetPoint.id.in_(point_ids),
            )
        ).all()
    )
    if len(points) != len(point_ids):
        raise HTTPException(status_code=404, detail="One or more saved points were not found in this gateway")
    configs = {
        config.point_id: config
        for config in db.scalars(select(PointTrendConfig).where(PointTrendConfig.point_id.in_(point_ids))).all()
    }
    updated_at = utc_now()
    for point in points:
        config = configs.get(point.id)
        if config is None:
            config = PointTrendConfig(point_id=point.id, gateway_id=gateway_id)
            db.add(config)
        config.enabled = payload.enabled
        config.interval_sec = payload.interval_sec
        config.updated_at = updated_at
        configs[point.id] = config
    db.commit()
    return [configs[point.id] for point in points]


@app.get("/api/ui/points/{point_id}/trend", response_model=list[PointTrendSampleOut])
def ui_point_trend_samples(
    point_id: str,
    limit: int = Query(default=288, ge=1, le=5000),
    since: datetime | None = Query(default=None),
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> list[PointTrendSample]:
    point = _require_point_site_access(db, auth, point_id)
    statement = select(PointTrendSample).where(PointTrendSample.point_id == point.id)
    if since is not None:
        statement = statement.where(PointTrendSample.sampled_at >= since)
    samples = db.scalars(statement.order_by(PointTrendSample.sampled_at.desc()).limit(limit)).all()
    return list(reversed(samples))


@app.post("/api/ui/gateways/{gateway_id}/commissioning-template/import", response_model=CommissioningTemplateImportOut)
def ui_import_commissioning_template(
    gateway_id: str,
    payload: CommissioningTemplateIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> CommissioningTemplateImportOut:
    _require_gateway_site_access(db, auth, gateway_id)
    if payload.gateway_id and payload.gateway_id != gateway_id:
        raise HTTPException(status_code=400, detail="Template gateway_id does not match target gateway")

    now = utc_now()
    groups_by_name = {
        group.name: group
        for group in db.scalars(select(GatewayGroup).where(GatewayGroup.gateway_id == gateway_id)).all()
    }
    created_groups = 0
    updated_groups = 0
    touched_group_names: set[str] = set()

    def ensure_group(name: str | None) -> GatewayGroup | None:
        nonlocal created_groups, updated_groups
        group_name = (name or "").strip()
        if not group_name:
            return None
        existing = groups_by_name.get(group_name)
        if existing is not None:
            if group_name not in touched_group_names:
                existing.updated_at = now
                updated_groups += 1
                touched_group_names.add(group_name)
            return existing
        group = GatewayGroup(gateway_id=gateway_id, name=group_name)
        db.add(group)
        db.flush()
        groups_by_name[group_name] = group
        touched_group_names.add(group_name)
        created_groups += 1
        return group

    for group_payload in payload.groups:
        ensure_group(group_payload.name)

    created_devices = 0
    updated_devices = 0
    created_points = 0
    updated_points = 0
    skipped_duplicate_points = 0

    for device_payload in payload.devices:
        group = ensure_group(device_payload.group_name)
        device = db.scalar(
            select(SavedBacnetDevice).where(
                SavedBacnetDevice.gateway_id == gateway_id,
                SavedBacnetDevice.device_instance == device_payload.device_instance,
            )
        )
        if device is None:
            device = SavedBacnetDevice(
                gateway_id=gateway_id,
                group_id=group.id if group is not None else None,
                device_instance=int(device_payload.device_instance),
                device_name=device_payload.device_name,
                vendor_name=device_payload.vendor_name,
                network_number=device_payload.network_number,
                mac_address=device_payload.mac_address,
                latest_discovered_at=now,
                first_seen_at=now,
                last_seen_at=now,
                lifecycle_state="active",
                enabled=True,
            )
            db.add(device)
            db.flush()
            created_devices += 1
        else:
            device.group_id = group.id if group is not None else device.group_id
            device.device_name = device_payload.device_name or device.device_name
            device.vendor_name = device_payload.vendor_name or device.vendor_name
            device.network_number = device_payload.network_number if device_payload.network_number is not None else device.network_number
            device.mac_address = device_payload.mac_address or device.mac_address
            _mark_device_seen(device, now)
            device.updated_at = now
            updated_devices += 1

        imported_point_keys: set[tuple[str, int, str]] = set()
        for point_payload in device_payload.points:
            point_key = (
                point_payload.object_type,
                int(point_payload.object_instance),
                point_payload.property,
            )
            if point_key in imported_point_keys:
                skipped_duplicate_points += 1
                continue
            imported_point_keys.add(point_key)
            point = db.scalar(
                select(SavedBacnetPoint).where(
                    SavedBacnetPoint.saved_device_id == device.id,
                    SavedBacnetPoint.object_type == point_payload.object_type,
                    SavedBacnetPoint.object_instance == point_payload.object_instance,
                    SavedBacnetPoint.property_name == point_payload.property,
                )
            )
            if point is None:
                point = SavedBacnetPoint(
                    gateway_id=gateway_id,
                    saved_device_id=device.id,
                    device_instance=device.device_instance,
                    object_type=point_payload.object_type,
                    object_instance=int(point_payload.object_instance),
                    object_name=point_payload.object_name,
                    property_name=point_payload.property,
                    units=point_payload.units,
                    writable=point_payload.writable,
                    first_seen_at=now,
                    last_seen_at=now,
                    lifecycle_state="active",
                    enabled=True,
                )
                db.add(point)
                created_points += 1
            else:
                point.object_name = point_payload.object_name or point.object_name
                point.units = point_payload.units if point_payload.units is not None else point.units
                point.writable = point_payload.writable if point_payload.writable is not None else point.writable
                _mark_point_seen(point, now)
                point.updated_at = now
                updated_points += 1

    db.commit()
    return CommissioningTemplateImportOut(
        group_count=len(groups_by_name),
        device_count=len(payload.devices),
        point_count=sum(len(device.points) for device in payload.devices),
        created_groups=created_groups,
        updated_groups=updated_groups,
        created_devices=created_devices,
        updated_devices=updated_devices,
        created_points=created_points,
        updated_points=updated_points,
        skipped_duplicate_points=skipped_duplicate_points,
    )


@app.post("/api/ui/gateways/{gateway_id}/discover-devices", response_model=JobOut)
def ui_discover_devices(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> EdgeJob:
    edge_node = _require_gateway_site_access(db, auth, gateway_id)
    _require_online_gateway(edge_node)
    bacnet_port = edge_node.bacnet_port
    job = EdgeJob(
        job_id=f"job-{uuid4().hex}",
        gateway_id=gateway_id,
        job_type="bacnet_discover",
        status="queued",
        request_json={"bacnet_port": bacnet_port},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    with job_wait_condition:
        job_wait_condition.notify_all()
    return job


@app.get("/api/edge/{gateway_id}/trend-configs", response_model=list[EdgeTrendConfigOut])
def edge_list_trend_configs(
    gateway_id: str,
    auth: GatewayAuthContext = Depends(require_gateway_auth),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    if auth.gateway_id != gateway_id:
        raise HTTPException(status_code=403, detail="Gateway credential does not match requested gateway_id")
    # GW032 incident (docs/gw032-trend-backlog-incident.md): retired points
    # must never reach the edge trend workload. Require the saved point to be
    # enabled, not just the trend config.
    # joinedload eliminates the per-config lazy load of config.point below:
    # with N configs that was N+1 statements, which at cross-region latency
    # (~70ms/statement, 2026-07-13 incident) made this poll take ~50s.
    configs = db.scalars(
        select(PointTrendConfig)
        .options(joinedload(PointTrendConfig.point))
        .join(SavedBacnetPoint, PointTrendConfig.point_id == SavedBacnetPoint.id)
        .where(
            PointTrendConfig.gateway_id == gateway_id,
            PointTrendConfig.enabled.is_(True),
            SavedBacnetPoint.enabled.is_(True),
        )
    ).all()
    return [{"point_id": config.point_id, "gateway_id": config.gateway_id, "enabled": config.enabled, "interval_sec": config.interval_sec, "updated_at": config.updated_at, "device_instance": config.point.device_instance, "object_type": config.point.object_type, "object_instance": config.point.object_instance} for config in configs]


@app.post("/api/edge/{gateway_id}/trend-samples", response_model=list[PointTrendSampleOut])
def edge_upload_trend_samples(
    gateway_id: str,
    payload: list[PointTrendSampleIn] = Body(min_length=1, max_length=500),
    auth: GatewayAuthContext = Depends(require_gateway_auth),
    db: Session = Depends(get_db),
) -> list[PointTrendSample]:
    if auth.gateway_id != gateway_id:
        raise HTTPException(status_code=403, detail="Gateway credential does not match trend sample gateway_id")
    point_ids = {_tree_id(sample.point_id) for sample in payload}
    sample_keys = [(_tree_id(sample.point_id), sample.sampled_at) for sample in payload]
    if len(sample_keys) != len(set(sample_keys)):
        raise HTTPException(status_code=422, detail="Trend sample batch must not contain duplicate point_id and sampled_at pairs")
    points = {point.id: point for point in db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.id.in_(point_ids), SavedBacnetPoint.gateway_id == gateway_id)).all()}
    existing_predicates = [and_(PointTrendSample.point_id == point_id, PointTrendSample.sampled_at == sampled_at) for point_id, sampled_at in sample_keys]
    trend_key = lambda point_id, sampled_at: (str(point_id), sampled_at.astimezone(timezone.utc).replace(tzinfo=None) if sampled_at.tzinfo else sampled_at)
    existing_by_key = {
        trend_key(existing.point_id, existing.sampled_at): existing
        for existing in db.scalars(select(PointTrendSample).where(or_(*existing_predicates))).all()
    }
    stored: list[PointTrendSample] = []
    for sample in payload:
        point_id = _tree_id(sample.point_id)
        if point_id not in points:
            raise HTTPException(status_code=403, detail="Trend sample point does not belong to gateway")
        existing = existing_by_key.get(trend_key(point_id, sample.sampled_at))
        if existing is None:
            existing = PointTrendSample(
                point_id=point_id,
                gateway_id=gateway_id,
                sampled_at=sample.sampled_at,
                value=sample.value,
                quality=sample.quality,
                source="edge-agent",
            )
            db.add(existing)
        stored.append(existing)
    db.commit()
    return stored


@app.put("/api/edge/{gateway_id}/inventory", response_model=EdgeInventorySyncOut)
def edge_sync_inventory(
    gateway_id: str,
    payload: EdgeInventorySnapshotIn,
    auth: GatewayAuthContext = Depends(require_gateway_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Mirror the Edge UI's saved Live Device profiles without BACnet I/O."""
    if auth.gateway_id != gateway_id:
        raise HTTPException(status_code=403, detail="Gateway credential does not match inventory gateway_id")
    now = utc_now()
    counts = {"created_devices": 0, "updated_devices": 0, "retired_devices": 0, "created_points": 0, "updated_points": 0, "retired_points": 0}
    mirrored = {
        device.edge_device_profile_id: device
        for device in db.scalars(
            select(SavedBacnetDevice).where(
                SavedBacnetDevice.gateway_id == gateway_id,
                SavedBacnetDevice.edge_device_profile_id.is_not(None),
            )
        ).all()
    }
    incoming_ids = {item.edge_device_profile_id for item in payload.devices}
    for device in mirrored.values():
        if device.edge_device_profile_id not in incoming_ids and device.enabled:
            device.enabled = False
            device.lifecycle_state = "retired"
            device.retired_at = now
            device.updated_at = now
            counts["retired_devices"] += 1
            for point in db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id)).all():
                if point.enabled:
                    point.enabled = False
                    point.lifecycle_state = "retired"
                    point.retired_at = now
                    point.updated_at = now
                    counts["retired_points"] += 1
            continue

    for incoming in payload.devices:
        device = mirrored.get(incoming.edge_device_profile_id)
        metadata = incoming.metadata
        vendor = metadata.get("vendor")
        network = metadata.get("network")
        mac = metadata.get("mac")
        if device is None:
            device = SavedBacnetDevice(
                gateway_id=gateway_id,
                edge_device_profile_id=incoming.edge_device_profile_id,
                device_instance=incoming.device_instance,
                device_name=incoming.device_name,
                vendor_name=vendor if isinstance(vendor, str) else None,
                network_number=network if isinstance(network, int) and not isinstance(network, bool) else None,
                mac_address=mac if isinstance(mac, str) else None,
                first_seen_at=now,
            )
            db.add(device)
            db.flush()
            counts["created_devices"] += 1
        else:
            device.device_instance = incoming.device_instance
            device.device_name = incoming.device_name
            if isinstance(vendor, str):
                device.vendor_name = vendor
            if isinstance(network, int) and not isinstance(network, bool):
                device.network_number = network
            if isinstance(mac, str):
                device.mac_address = mac
            counts["updated_devices"] += 1
        device.enabled = True
        device.lifecycle_state = "active"
        device.retired_at = None
        device.last_seen_at = now
        device.latest_discovered_at = incoming.updated_at or now
        device.updated_at = now

        existing_points = {
            (point.object_type, point.object_instance, point.property_name): point
            for point in db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id)).all()
        }
        incoming_point_keys = {(point.object_type, point.object_instance, point.property_name) for point in incoming.points}
        for key, point in existing_points.items():
            if key not in incoming_point_keys and point.enabled:
                point.enabled = False
                point.lifecycle_state = "retired"
                point.retired_at = now
                point.updated_at = now
                counts["retired_points"] += 1
        for incoming_point in incoming.points:
            key = (incoming_point.object_type, incoming_point.object_instance, incoming_point.property_name)
            point = existing_points.get(key)
            if point is None:
                point = SavedBacnetPoint(
                    gateway_id=gateway_id,
                    saved_device_id=device.id,
                    device_instance=incoming.device_instance,
                    object_type=incoming_point.object_type,
                    object_instance=incoming_point.object_instance,
                    property_name=incoming_point.property_name,
                    first_seen_at=now,
                )
                db.add(point)
                counts["created_points"] += 1
            else:
                counts["updated_points"] += 1
            point.device_instance = incoming.device_instance
            point.object_name = incoming_point.object_name
            point.enabled = True
            point.lifecycle_state = "active"
            point.retired_at = None
            point.last_seen_at = now
            point.updated_at = now
            if incoming_point.last_known is not None:
                value = incoming_point.last_known.raw_value
                if value is None:
                    value = incoming_point.last_known.display_value
                if value is not None:
                    point.present_value = value
                point.active_priority = incoming_point.last_known.active_priority
                point.priority_array = incoming_point.last_known.priority_array
                point.latest_read_at = incoming_point.last_known.source_timestamp
    db.commit()
    return {"gateway_id": gateway_id, "inventory_hash": payload.inventory_hash, **counts}


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
    edge_node.machine_id = payload.machine_id
    edge_node.primary_mac = payload.primary_mac
    edge_node.bacnet_port = payload.bacnet_port
    edge_node.agent_version = payload.agent_version
    edge_node.ui_version = payload.ui_version
    edge_node.sqlite_db_ok = payload.sqlite_db_ok
    edge_node.queued_upload_count = payload.queued_upload_count
    edge_node.trend_pending_upload_count = payload.trend_pending_upload_count
    edge_node.trend_deferred_upload_count = payload.trend_deferred_upload_count
    edge_node.trend_oldest_pending_at = payload.trend_oldest_pending_at
    edge_node.trend_max_upload_attempt_count = payload.trend_max_upload_attempt_count
    edge_node.cpu_count = payload.cpu_count
    edge_node.cpu_load_1m = payload.cpu_load_1m
    edge_node.cpu_load_pct = payload.cpu_load_pct
    edge_node.memory_used_pct = payload.memory_used_pct
    edge_node.memory_available_mb = payload.memory_available_mb
    edge_node.disk_used_pct = payload.disk_used_pct
    edge_node.disk_free_mb = payload.disk_free_mb
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
            machine_id=payload.machine_id,
            primary_mac=payload.primary_mac,
            bacnet_port=payload.bacnet_port,
            agent_version=payload.agent_version,
            ui_version=payload.ui_version,
            sqlite_db_ok=payload.sqlite_db_ok,
            queued_upload_count=payload.queued_upload_count,
            trend_pending_upload_count=payload.trend_pending_upload_count,
            trend_deferred_upload_count=payload.trend_deferred_upload_count,
            trend_oldest_pending_at=payload.trend_oldest_pending_at,
            trend_max_upload_attempt_count=payload.trend_max_upload_attempt_count,
            cpu_count=payload.cpu_count,
            cpu_load_1m=payload.cpu_load_1m,
            cpu_load_pct=payload.cpu_load_pct,
            memory_used_pct=payload.memory_used_pct,
            memory_available_mb=payload.memory_available_mb,
            disk_used_pct=payload.disk_used_pct,
            disk_free_mb=payload.disk_free_mb,
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
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    now = utc_now()
    # Same site scoping as /api/ui/gateways: scoped operators must not see
    # gateways belonging to other organizations or sites.
    return [
        _gateway_out(edge_node, now, db)
        for edge_node in db.scalars(_scoped_gateway_statement(db, auth)).all()
    ]


@app.post("/api/edge/jobs", response_model=JobOut)
def create_job(
    payload: JobCreateIn,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> EdgeJob:
    # Tenant boundary: scoped operators may only queue jobs on gateways within
    # their site scope, and cannot probe unknown gateway IDs. Platform admins
    # retain the legacy ability to queue jobs for gateways that have not yet
    # heartbeated (pre-provisioning flow).
    edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == payload.gateway_id))
    if edge_node is not None:
        require_site_access(db, auth, edge_node.site)
    elif not is_platform_admin(auth):
        raise HTTPException(status_code=404, detail="Gateway not found")
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
    with job_wait_condition:
        job_wait_condition.notify_all()
    return job


@app.get("/api/admin/gateway-updates", response_model=list[GatewayUpdateRequestOut])
def admin_list_gateway_updates(
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
    status_filter: str = "queued",
    limit: int = 100,
) -> list[dict[str, object]]:
    allowed_statuses = {"queued", "running", "completed", "failed", "all"}
    if status_filter not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Invalid gateway update status filter")
    limit = max(1, min(limit, 500))
    query = select(GatewayUpdateRequest).order_by(GatewayUpdateRequest.requested_at, GatewayUpdateRequest.id).limit(limit)
    if status_filter != "all":
        query = query.where(GatewayUpdateRequest.status == status_filter)
    updates = db.scalars(query).all()
    return [
        _gateway_update_out(update, _get_gateway_with_site_or_404(db, update.gateway_id))
        for update in updates
    ]


@app.post("/api/admin/gateway-updates/{request_id}/claim", response_model=GatewayUpdateRequestOut)
def admin_claim_gateway_update(
    request_id: UUID,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    update = db.get(GatewayUpdateRequest, request_id)
    if update is None:
        raise HTTPException(status_code=404, detail="Gateway update request not found")
    if update.status != "queued":
        raise HTTPException(status_code=409, detail=f"Gateway update request is already {update.status}")
    update.status = "running"
    update.started_at = utc_now()
    db.commit()
    return _gateway_update_out(update, _get_gateway_with_site_or_404(db, update.gateway_id))


@app.post("/api/admin/gateway-updates/{request_id}/complete", response_model=GatewayUpdateRequestOut)
def admin_complete_gateway_update(
    request_id: UUID,
    payload: GatewayUpdateCompleteIn,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    update = db.get(GatewayUpdateRequest, request_id)
    if update is None:
        raise HTTPException(status_code=404, detail="Gateway update request not found")
    if update.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail=f"Gateway update request is already {update.status}")
    update.status = payload.status
    update.error_message = payload.error_message
    update.completed_at = utc_now()
    if payload.status == "completed" and update.update_scope == "ui_only" and update.target_ui_version:
        edge_node = _get_gateway_with_site_or_404(db, update.gateway_id)
        edge_node.ui_version = update.target_ui_version
    if payload.status == "completed" and _gateway_update_public_scope(update) == FULL_NON_PROVISIONING_PUBLIC_SCOPE:
        # A successful non-provisioning deployment updates only release markers.
        # Gateway identity, tokens, BACnet settings, and routing are preserved
        # by the gateway updater workflow and intentionally untouched here.
        edge_node = _get_gateway_with_site_or_404(db, update.gateway_id)
        edge_node.agent_version = _gateway_update_target_agent_version(update) or _approved_release_version()
        edge_node.ui_version = _gateway_update_target_ui_version(update) or _approved_release_version()
    db.commit()
    return _gateway_update_out(update, _get_gateway_with_site_or_404(db, update.gateway_id))


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


def _credential_out(credential: GatewayCredential) -> dict[str, object]:
    """Serialize a credential for admin views. Never includes the token hash."""
    if credential.revoked_at is not None:
        status_value = "revoked"
    else:
        expires_at = _aware_utc(credential.expires_at)
        status_value = "expired" if expires_at is not None and expires_at <= utc_now() else "active"
    return {
        "credential_id": str(credential.id),
        "gateway_id": credential.gateway_id,
        "name": credential.name,
        "token_prefix": credential.token_prefix,
        "scopes": list(credential.scopes or []),
        "created_at": credential.created_at,
        "last_used_at": credential.last_used_at,
        "expires_at": credential.expires_at,
        "revoked_at": credential.revoked_at,
        "status": status_value,
    }


@app.get("/api/admin/gateways/{gateway_id}/credentials", response_model=list[GatewayCredentialOut])
def admin_list_gateway_credentials(
    gateway_id: str,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    _get_gateway_with_site_or_404(db, gateway_id)
    credentials = db.scalars(
        select(GatewayCredential)
        .where(GatewayCredential.gateway_id == gateway_id)
        .order_by(GatewayCredential.created_at.desc(), GatewayCredential.token_prefix)
    ).all()
    return [_credential_out(credential) for credential in credentials]


@app.post("/api/admin/credentials/{credential_id}/revoke", response_model=GatewayCredentialOut)
def admin_revoke_gateway_credential(
    credential_id: str,
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        credential_uuid = UUID(credential_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Credential not found") from None
    credential = db.get(GatewayCredential, credential_uuid)
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    # Idempotent: revoking an already-revoked credential returns it unchanged.
    if credential.revoked_at is None:
        credential.revoked_at = utc_now()
        db.commit()
    return _credential_out(credential)


@app.post("/api/admin/maintenance/disable-retired-trend-configs", response_model=TrendConfigRepairOut)
def admin_disable_retired_trend_configs(
    gateway_id: str | None = Query(default=None),
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Idempotent repair for the GW032 defect class: disable every enabled
    trend config whose saved point is retired/disabled. Reports the affected
    count; a second run reports zero. Optionally scoped to one gateway."""
    statement = (
        select(PointTrendConfig)
        .join(SavedBacnetPoint, PointTrendConfig.point_id == SavedBacnetPoint.id)
        .where(PointTrendConfig.enabled.is_(True), SavedBacnetPoint.enabled.is_(False))
    )
    if gateway_id:
        statement = statement.where(PointTrendConfig.gateway_id == gateway_id)
    configs = db.scalars(statement).all()
    now = utc_now()
    for config in configs:
        config.enabled = False
        config.updated_at = now
    db.commit()
    return {"disabled_count": len(configs), "gateway_id": gateway_id}


alert_logger = logging.getLogger("iot-cloud-api.alerts")
_ensure_visible_logging(alert_logger)


def _deliver_alert_webhook(webhook_url: str, payload: dict[str, object]) -> bool:
    """POST one alert to the configured webhook. Best-effort; never raises."""
    import httpx

    try:
        response = httpx.post(webhook_url, json=payload, timeout=5.0)
        return 200 <= response.status_code < 300
    except Exception as exc:  # noqa: BLE001 - delivery must never break evaluation
        alert_logger.warning("alert webhook delivery failed: %s", exc)
        return False


@app.post("/api/admin/alerts/evaluate", response_model=AlertEvaluationOut)
def admin_evaluate_alerts(
    _: AdminAuthContext = Depends(require_admin_or_admin_token_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Transition-based fleet alert evaluation.

    Designed to be called by an external scheduler (e.g. Render cron) every
    few minutes; also safe to invoke manually. Alerts fire on state CHANGES
    only: offline->online, backlog appears/clears. Gateways that have never
    heartbeated (preprovisioned, awaiting installation) are ineligible and
    stay silent. With no webhook configured, transitions are recorded and
    acknowledged silently so enabling a webhook later does not replay
    historical events.
    """
    now = utc_now()
    webhook_url = (settings.alert_webhook_url or "").strip()
    events: list[dict[str, object]] = []
    delivery_failures = 0
    deliveries_attempted = 0

    eligible = db.scalars(
        select(EdgeNode)
        .options(joinedload(EdgeNode.site))
        .where(EdgeNode.latest_heartbeat_at.isnot(None))
        .order_by(EdgeNode.gateway_id)
    ).all()

    states = {
        (state.gateway_id, state.alert_type): state
        for state in db.scalars(select(GatewayAlertState)).all()
    }

    def process(edge_node: EdgeNode, alert_type: str, condition_active: bool, alert_text: str, recovery_text: str) -> None:
        nonlocal delivery_failures, deliveries_attempted
        key = (edge_node.gateway_id, alert_type)
        state = states.get(key)
        if state is None:
            state = GatewayAlertState(gateway_id=edge_node.gateway_id, alert_type=alert_type, active=False)
            db.add(state)
            states[key] = state
        if condition_active != state.active:
            state.active = condition_active
            state.last_transition_at = now
            state.last_notified_at = None
            state.updated_at = now
        if state.last_transition_at is None or state.last_notified_at is not None:
            return  # nothing pending

        event_type = alert_type if state.active else f"{alert_type}_recovered"
        text = alert_text if state.active else recovery_text
        delivered = False
        if not webhook_url:
            # Groundwork mode: acknowledge silently; report in response only.
            state.last_notified_at = now
        elif deliveries_attempted < settings.alert_max_deliveries_per_run:
            deliveries_attempted += 1
            payload = {
                "type": event_type,
                "gateway_id": edge_node.gateway_id,
                "site_id": edge_node.site_id,
                "hostname": edge_node.hostname,
                "environment": settings.environment,
                "occurred_at": (_aware_utc(state.last_transition_at) or now).isoformat(),
                "text": text,
            }
            delivered = _deliver_alert_webhook(webhook_url, payload)
            if delivered:
                state.last_notified_at = now
            else:
                delivery_failures += 1  # stays pending; retried next evaluation
        events.append(
            {
                "type": event_type,
                "gateway_id": edge_node.gateway_id,
                "site_id": edge_node.site_id,
                "hostname": edge_node.hostname,
                "text": text,
                "occurred_at": state.last_transition_at,
                "delivered": delivered,
            }
        )

    backlog_cutoff = now - timedelta(hours=settings.alert_trend_backlog_age_hours)
    for edge_node in eligible:
        offline_now = _effective_status(edge_node, now)["effective_status"] == "offline"
        heartbeat_age = _heartbeat_age_seconds(edge_node, now)
        process(
            edge_node,
            "gateway_offline",
            offline_now,
            f"Gateway {edge_node.gateway_id} ({edge_node.site_id}) is OFFLINE - last heartbeat {heartbeat_age}s ago",
            f"Gateway {edge_node.gateway_id} ({edge_node.site_id}) recovered - heartbeats resumed",
        )
        oldest_pending = _aware_utc(edge_node.trend_oldest_pending_at)
        backlog_now = (not offline_now) and oldest_pending is not None and oldest_pending < backlog_cutoff
        process(
            edge_node,
            "trend_backlog",
            backlog_now,
            f"Gateway {edge_node.gateway_id} ({edge_node.site_id}) trend backlog not draining - oldest pending sample {oldest_pending.isoformat() if oldest_pending else '?'}, {edge_node.trend_pending_upload_count} pending",
            f"Gateway {edge_node.gateway_id} ({edge_node.site_id}) trend backlog cleared",
        )

    db.commit()
    return {
        "environment": settings.environment,
        "webhook_configured": bool(webhook_url),
        "evaluated_gateways": len(eligible),
        "events": events,
        "delivery_failures": delivery_failures,
    }


@app.get("/api/edge/{gateway_id}/jobs/next", response_model=EdgeJobClaimOut | None)
def claim_next_job(
    gateway_id: str,
    response: Response,
    wait_seconds: int = Query(default=0, ge=0, le=600),
    auth: GatewayAuthContext = Depends(require_gateway_auth),
    db: Session = Depends(get_db),
) -> EdgeJobClaimOut | None:
    if auth.gateway_id != gateway_id:
        raise HTTPException(status_code=403, detail="Gateway credential does not match requested gateway_id")

    canary_relay = _relay_canary_selected(gateway_id)
    _set_tunnel_instruction_headers(response, db, gateway_id)

    # Stale-claim recovery: a gateway that dies mid-job leaves the job
    # 'claimed' forever. Requeue this gateway's stale claims at poll time.
    # BACnet write jobs are excluded — a partially executed write must never
    # be re-executed blindly; they stay 'claimed' for manual review (see
    # docs/disaster-recovery-runbook.md, Job recovery).
    stale_cutoff = utc_now() - timedelta(seconds=settings.job_claim_timeout_sec)
    stale_jobs = db.scalars(
        select(EdgeJob).where(
            EdgeJob.gateway_id == gateway_id,
            EdgeJob.status == "claimed",
            EdgeJob.claimed_at < stale_cutoff,
            EdgeJob.job_type != "bacnet_write_batch",
        )
    ).all()
    if stale_jobs:
        for stale_job in stale_jobs:
            stale_job.status = "queued"
            stale_job.claimed_at = None
        db.commit()

    # Row-level lock with SKIP LOCKED prevents two app workers/instances from
    # claiming the same job during concurrent polls. SQLite (dev/tests)
    # ignores FOR UPDATE, preserving existing single-process behavior.
    job = db.scalar(
        select(EdgeJob)
        .where(EdgeJob.gateway_id == gateway_id, EdgeJob.status == "queued")
        .order_by(EdgeJob.created_at, EdgeJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None and wait_seconds:
        # Release the SQLAlchemy connection while the request waits. A create
        # wakes this condition promptly; timeout preserves the Agent's single
        # renewable request and bounds every worker/resource commitment.
        deadline = time.monotonic() + wait_seconds
        while job is None:
            # Canary-only bounded durable recheck. close() releases the pool
            # connection before every sleep; this remains one Agent HTTP poll.
            db.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with job_wait_condition:
                job_wait_condition.wait(timeout=min(remaining, RELAY_CANARY_DURABLE_RECHECK_SECONDS if canary_relay else remaining))
            job = db.scalar(
                select(EdgeJob)
                .where(EdgeJob.gateway_id == gateway_id, EdgeJob.status == "queued")
                .order_by(EdgeJob.created_at, EdgeJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if canary_relay:
                _set_tunnel_instruction_headers(response, db, gateway_id)
                if response.headers.get("X-IOT-Tunnel-Requested") == "true":
                    break
    if job is None:
        return None

    now = utc_now()
    job.status = "claimed"
    job.claimed_at = now
    if job.job_type == "bacnet_write_batch":
        commands = list(
            db.scalars(select(BacnetWriteCommand).where(BacnetWriteCommand.edge_job_id == job.job_id)).all()
        )
        affected_batch_ids: set[UUID] = set()
        for command in commands:
            if command.status == "queued":
                command.status = "claimed"
            affected_batch_ids.add(command.batch_id)
        db.flush()
        for batch_id in affected_batch_ids:
            _refresh_write_batch_status(db, batch_id, now)
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
    if job.job_type == "bacnet_discover" and payload.status == "completed" and isinstance(payload.result, dict):
        discovered = payload.result.get("devices")
        now = utc_now()
        if isinstance(discovered, list):
            for item in discovered:
                if not isinstance(item, dict):
                    continue
                device_instance = item.get("device_id")
                if isinstance(device_instance, bool) or not isinstance(device_instance, int):
                    continue
                device = db.scalar(
                    select(SavedBacnetDevice).where(
                        SavedBacnetDevice.gateway_id == job.gateway_id,
                        SavedBacnetDevice.device_instance == device_instance,
                    )
                )
                if device is None:
                    device = SavedBacnetDevice(
                        gateway_id=job.gateway_id,
                        device_instance=device_instance,
                        network_number=item.get("network") if isinstance(item.get("network"), int) else None,
                        mac_address=item.get("mac") if isinstance(item.get("mac"), str) else None,
                        first_seen_at=now,
                        last_seen_at=now,
                        latest_discovered_at=now,
                        lifecycle_state="active",
                        enabled=True,
                    )
                    db.add(device)
                else:
                    if isinstance(item.get("network"), int):
                        device.network_number = item["network"]
                    if isinstance(item.get("mac"), str):
                        device.mac_address = item["mac"]
                    _mark_device_seen(device, now)
                    device.updated_at = now
    if job.job_type == "bacnet_load_points" and payload.status == "completed" and isinstance(payload.result, dict):
        saved_device_id = job.request_json.get("saved_device_id") if isinstance(job.request_json, dict) else None
        loaded_points = payload.result.get("points")
        device = db.get(SavedBacnetDevice, _tree_id(saved_device_id)) if isinstance(saved_device_id, str) else None
        now = utc_now()
        if device is not None and device.gateway_id == job.gateway_id and isinstance(loaded_points, list):
            _mark_device_seen(device, now)
            for item in loaded_points:
                if not isinstance(item, dict):
                    continue
                object_type = item.get("object_type")
                object_instance = item.get("object_instance")
                if not isinstance(object_type, str) or isinstance(object_instance, bool) or not isinstance(object_instance, int):
                    continue
                point = db.scalar(
                    select(SavedBacnetPoint).where(
                        SavedBacnetPoint.saved_device_id == device.id,
                        SavedBacnetPoint.object_type == object_type,
                        SavedBacnetPoint.object_instance == object_instance,
                        SavedBacnetPoint.property_name == "present-value",
                    )
                )
                if point is None:
                    point = SavedBacnetPoint(
                        gateway_id=job.gateway_id,
                        saved_device_id=device.id,
                        device_instance=device.device_instance,
                        object_type=object_type,
                        object_instance=object_instance,
                        object_name=item.get("object_name") if isinstance(item.get("object_name"), str) else None,
                        property_name="present-value",
                        first_seen_at=now,
                        last_seen_at=now,
                        lifecycle_state="active",
                        enabled=True,
                    )
                    db.add(point)
                else:
                    if isinstance(item.get("object_name"), str):
                        point.object_name = item["object_name"]
                    _mark_point_seen(point, now)
                    point.updated_at = now
    if job.job_type == "bacnet_read" and payload.status == "completed" and isinstance(payload.result, dict):
        saved_point_id = job.request_json.get("saved_point_id") if isinstance(job.request_json, dict) else None
        value = payload.result.get("value", payload.result.get("raw_value"))
        if isinstance(saved_point_id, str) and value is not None:
            point = db.get(SavedBacnetPoint, _tree_id(saved_point_id))
            if point is not None and point.gateway_id == job.gateway_id:
                point.present_value = str(value)
                point.latest_read_at = utc_now()
                point.updated_at = utc_now()
    if job.job_type == "bacnet_read_bulk" and payload.status == "completed" and isinstance(payload.result, dict):
        values = payload.result.get("values")
        if isinstance(values, list):
            now = utc_now()
            for value_payload in values:
                if not isinstance(value_payload, dict):
                    continue
                saved_point_id = value_payload.get("saved_point_id")
                value = value_payload.get("value", value_payload.get("raw_value"))
                if not isinstance(saved_point_id, str):
                    continue
                point = db.get(SavedBacnetPoint, _tree_id(saved_point_id))
                if point is not None and point.gateway_id == job.gateway_id:
                    changed = False
                    if value is not None:
                        point.present_value = str(value)
                        changed = True
                    if "active_priority" in value_payload:
                        active_priority = value_payload["active_priority"]
                        if active_priority is None or (
                            not isinstance(active_priority, bool)
                            and isinstance(active_priority, int)
                            and 1 <= active_priority <= 16
                        ):
                            point.active_priority = active_priority
                            changed = True
                    if "priority_array" in value_payload:
                        priority_array = value_payload["priority_array"]
                        if priority_array is None or isinstance(priority_array, str):
                            point.priority_array = priority_array
                            changed = True
                    if changed:
                        point.latest_read_at = now
                        point.updated_at = now
    if job.job_type == "bacnet_write_batch":
        _reconcile_bacnet_write_result(db, job, payload)
    db.commit()
    db.refresh(job)
    return job


@app.get("/api/edge/jobs", response_model=list[JobOut])
def list_jobs(
    auth: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
    limit: int = 50,
) -> list[EdgeJob]:
    limit = max(1, min(limit, 200))
    statement = select(EdgeJob).order_by(EdgeJob.created_at.desc(), EdgeJob.id.desc())
    allowed_site_ids = visible_site_ids(db, auth)
    if allowed_site_ids is not None:
        statement = statement.where(
            EdgeJob.gateway_id.in_(
                select(EdgeNode.gateway_id).where(EdgeNode.site_id.in_(select(Site.site_id).where(Site.id.in_(allowed_site_ids))))
            )
        )
    return list(db.scalars(statement.limit(limit)).all())
