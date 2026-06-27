import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import ipaddress
import json
import logging
import re
from urllib.parse import parse_qsl, quote, urlsplit
from uuid import UUID
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select, text
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
    CommissioningTemplateImportOut,
    CommissioningTemplateIn,
    CurrentOperatorOut,
    DirectConnectOut,
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
    SavedPointsBulkRemoveIn,
    SavedPointsBulkRemoveOut,
    SiteOut,
    SiteUpdate,
    TunnelSessionOut,
    TunnelStatusOut,
)
from app.tunnel import TunnelRequestFailed, TunnelResponse, TunnelUnavailable, tunnel_manager, tunnel_session_manager
from app.ui import (
    admin_users_html,
    app_html,
    check_email_html,
    gateway_workspace_html,
    login_html,
    signup_html,
    tunnel_console_html,
    unauthorized_html,
    waiting_approval_html,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="IOT Cloud Commissioning API", version="0.1.0", lifespan=lifespan)
logger = logging.getLogger("iot-cloud-api.tunnel")


DIRECT_CONNECT_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
TUNNEL_HTML_ATTR_PATTERN = re.compile(r"""(?P<attr>\b(?:href|src|action|formaction)=)(?P<quote>["'])(?P<url>[^"']+)(?P=quote)""", re.IGNORECASE)


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
    site = edge_node.site
    store_hours_mf = (site.store_hours_monday_friday or site.store_hours_mf) if site else None
    store_hours_sat = (site.store_hours_saturday or site.store_hours_sat) if site else None
    store_hours_sun = (site.store_hours_sunday or site.store_hours_sun) if site else None
    direct_connect = _direct_connect_for_site(site) if site else DirectConnectOut(available=False)
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
        "site_name": site.name if site else None,
        "site_address": site.address if site else None,
        "site_address_street": site.address_street if site else None,
        "site_address_city": site.address_city if site else None,
        "site_address_state": site.address_state if site else None,
        "site_address_postal_code": site.address_postal_code if site else None,
        "site_compact_address": _site_compact_address(site),
        "store_hours_monday_friday": store_hours_mf,
        "store_hours_saturday": store_hours_sat,
        "store_hours_sunday": store_hours_sun,
        "network_status_notes": site.network_status_notes if site else None,
        "direct_connect_available": direct_connect.available,
        "direct_connect_host": direct_connect.host,
        "direct_connect_port": direct_connect.port,
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
    gateways = [
        _gateway_out(edge_node, now)
        for edge_node in db.scalars(select(EdgeNode).options(joinedload(EdgeNode.site)).order_by(EdgeNode.gateway_id)).all()
    ]
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


@app.get("/api/ui/sites", response_model=list[SiteOut])
def ui_list_sites(
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> list[Site]:
    return list(db.scalars(select(Site).order_by(Site.site_id)).all())


@app.get("/api/ui/sites/{site_id}", response_model=SiteOut)
def ui_get_site(
    site_id: str,
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> Site:
    site = db.scalar(select(Site).where(Site.site_id == site_id))
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
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
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return _gateway_out(_get_gateway_with_site_or_404(db, gateway_id))


@app.get("/api/ui/gateways/{gateway_id}/site", response_model=SiteOut)
def ui_get_gateway_site(
    gateway_id: str,
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> Site:
    return _get_gateway_with_site_or_404(db, gateway_id).site


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
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> DirectConnectOut:
    gateway = _get_gateway_with_site_or_404(db, gateway_id)
    return _direct_connect_for_site(gateway.site)


@app.get("/api/ui/gateways/{gateway_id}/tunnel-status", response_model=TunnelStatusOut)
def ui_gateway_tunnel_status(
    gateway_id: str,
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> TunnelStatusOut:
    _get_gateway_or_404(db, gateway_id)
    connected = tunnel_manager.is_connected(gateway_id)
    return TunnelStatusOut(connected=connected, status="connected" if connected else "not_connected")


@app.post("/api/ui/gateways/{gateway_id}/tunnel-session", response_model=TunnelSessionOut)
def ui_create_gateway_tunnel_session(
    gateway_id: str,
    auth: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> TunnelSessionOut:
    _get_gateway_or_404(db, gateway_id)
    if not tunnel_manager.is_connected(gateway_id):
        raise HTTPException(status_code=503, detail="Gateway tunnel is not connected")
    subject = auth.email or auth.auth_type
    session = tunnel_session_manager.create(gateway_id=gateway_id, subject=subject)
    return TunnelSessionOut(url=f"{_tunnel_session_prefix(gateway_id, session.session_id)}/")


@app.websocket("/api/edge/tunnels/{gateway_id}")
async def edge_tunnel(
    gateway_id: str,
    websocket: WebSocket,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    try:
        auth = require_gateway_auth(authorization=authorization, db=db)
        if auth.gateway_id != gateway_id:
            await websocket.close(code=1008)
            return
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    tunnel = tunnel_manager.register(gateway_id, websocket)
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
    db: Session = Depends(get_db),
) -> Response:
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
    db: Session = Depends(get_db),
) -> Response:
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
    if rewrite_html_body and "text/html" in content_type.lower():
        response_body = _rewrite_tunnel_html_body(tunnel_response.body, redirect_prefix)

    response_header_names = {key.lower() for key in response_headers}
    upstream_location = next((value for key, value in tunnel_response.headers.items() if key.lower() == "location"), None)
    response_location = next((value for key, value in response_headers.items() if key.lower() == "location"), None)
    logger.warning(
        "TUNNEL_PROXY_DEBUG response gateway=%s method=%s path=%s status=%s content_type=%s body_bytes=%s "
        "upstream_location_shape=%s response_location_shape=%s response_location_session_slash=%s "
        "set_cookie_received=%s set_cookie_forwarded=%s html_rewritten=%s",
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
        response_body != tunnel_response.body,
    )

    return Response(
        content=response_body,
        status_code=tunnel_response.status_code,
        headers=response_headers,
    )


@app.get("/api/ui/gateways/{gateway_id}/tree", response_model=GatewayTreeOut)
def ui_get_gateway_tree(
    gateway_id: str,
    _: AdminAuthContext = Depends(require_operator_auth),
    db: Session = Depends(get_db),
) -> GatewayTreeOut:
    gateway = _get_gateway_or_404(db, gateway_id)
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


@app.delete("/api/ui/devices/{device_id}", response_model=SavedDeviceOut)
def ui_remove_device(
    device_id: str,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    device = db.get(SavedBacnetDevice, _tree_id(device_id))
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.enabled = False
    device.updated_at = utc_now()
    for point in db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.saved_device_id == device.id)).all():
        point.enabled = False
        point.updated_at = utc_now()
    db.commit()
    db.refresh(device)
    return _device_out(device)


@app.post("/api/ui/devices/{device_id}/load-points", response_model=JobOut)
def ui_load_device_points(
    device_id: str,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> EdgeJob:
    device = db.get(SavedBacnetDevice, _tree_id(device_id))
    if device is None or not device.enabled:
        raise HTTPException(status_code=404, detail="Device not found")
    edge_node = _get_gateway_or_404(db, device.gateway_id)
    _require_online_gateway(edge_node)
    job = EdgeJob(
        job_id=f"job-{uuid4().hex}",
        gateway_id=device.gateway_id,
        job_type="bacnet_load_points",
        status="queued",
        request_json={
            "device_instance": device.device_instance,
            "saved_device_id": str(device.id),
            "bacnet_port": 47814,
            "limit": 80,
            "name_limit": 40,
            "include_object_names": True,
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


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


@app.delete("/api/ui/points/{point_id}", response_model=SavedPointOut)
def ui_remove_point(
    point_id: str,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    point = db.get(SavedBacnetPoint, _tree_id(point_id))
    if point is None:
        raise HTTPException(status_code=404, detail="Point not found")
    point.enabled = False
    point.updated_at = utc_now()
    db.commit()
    db.refresh(point)
    return _point_out(point)


@app.post("/api/ui/points/bulk-remove", response_model=SavedPointsBulkRemoveOut)
def ui_bulk_remove_points(
    payload: SavedPointsBulkRemoveIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> SavedPointsBulkRemoveOut:
    point_ids: list[str] = []
    for point_id in payload.point_ids:
        point_ids.append(_tree_id(point_id))
    points = list(db.scalars(select(SavedBacnetPoint).where(SavedBacnetPoint.id.in_(point_ids))).all())
    points_by_id = {str(point.id): point for point in points}
    now = utc_now()
    removed_count = 0
    for point in points:
        if point.enabled:
            point.enabled = False
            point.updated_at = now
            removed_count += 1
    db.commit()
    missing_ids = [point_id for point_id in payload.point_ids if _tree_id(point_id) not in points_by_id]
    return SavedPointsBulkRemoveOut(
        requested_count=len(payload.point_ids),
        removed_count=removed_count,
        missing_ids=missing_ids,
    )


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


@app.post("/api/ui/gateways/{gateway_id}/commissioning-template/import", response_model=CommissioningTemplateImportOut)
def ui_import_commissioning_template(
    gateway_id: str,
    payload: CommissioningTemplateIn,
    _: AdminAuthContext = Depends(require_job_operator_auth),
    db: Session = Depends(get_db),
) -> CommissioningTemplateImportOut:
    _get_gateway_or_404(db, gateway_id)
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
            device.latest_discovered_at = now
            device.enabled = True
            device.updated_at = now
            updated_devices += 1

        for point_payload in device_payload.points:
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
                    enabled=True,
                )
                db.add(point)
                created_points += 1
            else:
                point.object_name = point_payload.object_name or point.object_name
                point.units = point_payload.units if point_payload.units is not None else point.units
                point.writable = point_payload.writable if point_payload.writable is not None else point.writable
                point.enabled = True
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
    )


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
    return [
        _gateway_out(edge_node, now)
        for edge_node in db.scalars(select(EdgeNode).options(joinedload(EdgeNode.site)).order_by(EdgeNode.gateway_id)).all()
    ]


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
