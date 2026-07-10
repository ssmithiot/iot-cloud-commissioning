from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


BACNET_READ_OBJECT_TYPES = {
    "analog-input",
    "analog-output",
    "analog-value",
    "binary-input",
    "binary-output",
    "binary-value",
    "multi-state-input",
    "multi-state-output",
    "multi-state-value",
}
BACNET_LOAD_POINT_OBJECT_TYPES = {
    "analog-input",
    "analog-output",
    "analog-value",
    "binary-input",
    "binary-output",
    "binary-value",
    "calendar",
    "command",
    "event-enrollment",
    "file",
    "loop",
    "multi-state-input",
    "multi-state-output",
    "multi-state-value",
    "notification-class",
    "program",
    "schedule",
    "trend-log",
}


def _read_required_int(request: dict[str, object], field_name: str) -> int:
    value = request.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def normalize_bacnet_read_request(request: dict[str, object]) -> dict[str, object]:
    device_instance = _read_required_int(request, "device_instance")
    object_instance = _read_required_int(request, "object_instance")

    object_type = request.get("object_type")
    if not isinstance(object_type, str) or object_type not in BACNET_READ_OBJECT_TYPES:
        allowed = ", ".join(sorted(BACNET_READ_OBJECT_TYPES))
        raise ValueError(f"object_type received {object_type!r}; must be one of: {allowed}")

    property_name = request.get("property", "present-value")
    if property_name != "present-value":
        raise ValueError("property must be present-value")

    return {
        "device_instance": device_instance,
        "object_type": object_type,
        "object_instance": object_instance,
        "property": "present-value",
    }


def normalize_bacnet_load_points_request(request: dict[str, object]) -> dict[str, object]:
    device_instance = _read_required_int(request, "device_instance")
    normalized: dict[str, object] = {
        "device_instance": device_instance,
        "bacnet_port": 47814,
        "limit": 250,
        "name_limit": 40,
        "include_object_names": True,
    }

    if "limit" in request:
        limit = request["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        normalized["limit"] = limit
        normalized["name_limit"] = min(int(normalized["name_limit"]), limit)

    if "name_limit" in request:
        name_limit = request["name_limit"]
        if (
            isinstance(name_limit, bool)
            or not isinstance(name_limit, int)
            or name_limit < 0
            or name_limit > int(normalized["limit"])
        ):
            raise ValueError("name_limit must be an integer between 0 and limit")
        normalized["name_limit"] = name_limit

    if "include_object_names" in request:
        include_object_names = request["include_object_names"]
        if not isinstance(include_object_names, bool):
            raise ValueError("include_object_names must be a boolean")
        normalized["include_object_names"] = include_object_names

    if "object_types" in request:
        object_types = request["object_types"]
        if not isinstance(object_types, list) or not all(isinstance(item, str) for item in object_types):
            raise ValueError("object_types must be a list of strings")
        invalid = [item for item in object_types if item not in BACNET_LOAD_POINT_OBJECT_TYPES]
        if invalid:
            allowed = ", ".join(sorted(BACNET_LOAD_POINT_OBJECT_TYPES))
            raise ValueError(f"object_types must contain only: {allowed}")
        normalized["object_types"] = object_types

    return normalized


class HeartbeatIn(BaseModel):
    gateway_id: str = Field(min_length=1, max_length=120)
    site_id: str = Field(min_length=1, max_length=120)
    hostname: str = Field(min_length=1, max_length=255)
    lan_ip: str | None = Field(default=None, max_length=64)
    bacnet_port: int = Field(ge=1, le=65535)
    agent_version: str = Field(min_length=1, max_length=80)
    ui_version: str = Field(min_length=1, max_length=80)
    sqlite_db_ok: bool
    queued_upload_count: int = Field(ge=0)
    timestamp_utc: datetime


class HeartbeatAccepted(BaseModel):
    gateway_id: str
    status: str
    latest_heartbeat_at: datetime


class SiteOut(BaseModel):
    site_id: str
    name: str
    external_ip: str | None
    address: str | None
    address_street: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    store_hours_mf: str | None
    store_hours_sat: str | None
    store_hours_sun: str | None
    cradlepoint_ip: str | None = None
    direct_connect_host: str | None = None
    direct_connect_port: int | None = None
    gateway_ui_port: int | None = None
    store_hours_monday_friday: str | None = None
    store_hours_saturday: str | None = None
    store_hours_sunday: str | None = None
    network_status_notes: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    external_ip: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=500)
    address_street: str | None = Field(default=None, max_length=255)
    address_city: str | None = Field(default=None, max_length=120)
    address_state: str | None = Field(default=None, max_length=80)
    address_postal_code: str | None = Field(default=None, max_length=40)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    store_hours_mf: str | None = Field(default=None, max_length=120)
    store_hours_sat: str | None = Field(default=None, max_length=120)
    store_hours_sun: str | None = Field(default=None, max_length=120)
    cradlepoint_ip: str | None = Field(default=None, max_length=255)
    direct_connect_host: str | None = Field(default=None, max_length=255)
    direct_connect_port: int | None = Field(default=None, ge=1, le=65535)
    gateway_ui_port: int | None = Field(default=None, ge=1, le=65535)
    store_hours_monday_friday: str | None = Field(default=None, max_length=120)
    store_hours_saturday: str | None = Field(default=None, max_length=120)
    store_hours_sunday: str | None = Field(default=None, max_length=120)
    network_status_notes: str | None = Field(default=None, max_length=500)


class SiteWeatherOut(BaseModel):
    available: bool
    site_id: str
    provider: str = "open-meteo"
    reason: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    temperature_f: float | None = None
    apparent_temperature_f: float | None = None
    relative_humidity_percent: int | None = None
    precipitation_in: float | None = None
    wind_speed_mph: float | None = None
    weather_code: int | None = None
    condition: str | None = None
    timezone: str | None = None
    timezone_abbreviation: str | None = None
    observed_at: datetime | None = None
    sunrise_at: datetime | None = None
    sunset_at: datetime | None = None
    solar_noon_at: datetime | None = None
    fetched_at: datetime | None = None
    cache_age_seconds: int | None = None


class DirectConnectOut(BaseModel):
    available: bool
    url: str | None = None
    host: str | None = None
    port: int | None = None
    label: str = "Direct Connect"
    reason: str | None = None


class TunnelStatusOut(BaseModel):
    connected: bool
    status: str


class TunnelSessionCreateIn(BaseModel):
    ttl_minutes: int = Field(default=5, ge=5, le=60)


class TunnelSessionOut(BaseModel):
    url: str


class GatewayOut(BaseModel):
    gateway_id: str
    site_id: str
    hostname: str
    lan_ip: str | None
    bacnet_port: int
    agent_version: str
    ui_version: str
    sqlite_db_ok: bool
    queued_upload_count: int
    latest_status: str
    latest_heartbeat_at: datetime | None
    updated_at: datetime
    effective_status: str | None = None
    heartbeat_age_seconds: int | None = None
    is_online: bool | None = None
    is_stale: bool | None = None
    site_name: str | None = None
    site_address: str | None = None
    site_address_street: str | None = None
    site_address_city: str | None = None
    site_address_state: str | None = None
    site_address_postal_code: str | None = None
    site_latitude: float | None = None
    site_longitude: float | None = None
    site_compact_address: str | None = None
    store_hours_monday_friday: str | None = None
    store_hours_saturday: str | None = None
    store_hours_sunday: str | None = None
    network_status_notes: str | None = None
    direct_connect_available: bool | None = None
    direct_connect_host: str | None = None
    direct_connect_port: int | None = None

    model_config = ConfigDict(from_attributes=True)


class GatewayProvisionIn(BaseModel):
    gateway_id: str = Field(min_length=1, max_length=120)
    site_id: str = Field(min_length=1, max_length=120)
    hostname: str = Field(min_length=1, max_length=255)
    lan_ip: str | None = Field(default=None, max_length=64)
    bacnet_port: int = Field(default=47814, ge=1, le=65535)
    agent_version: str = Field(default="0.1.0", min_length=1, max_length=80)
    ui_version: str = Field(default="0.1.0", min_length=1, max_length=80)


class GatewayProvisionOut(BaseModel):
    gateway_id: str
    site_id: str
    hostname: str
    lan_ip: str | None
    bacnet_port: int
    agent_version: str
    ui_version: str
    gateway_api_token: str
    token_prefix: str


class JobCreateIn(BaseModel):
    gateway_id: str = Field(min_length=1, max_length=120)
    job_type: str = Field(min_length=1, max_length=80)
    request: dict[str, object] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_known_job_payloads(self) -> "JobCreateIn":
        if self.job_type == "bacnet_read":
            self.request = normalize_bacnet_read_request(self.request)
        if self.job_type == "bacnet_load_points":
            self.request = normalize_bacnet_load_points_request(self.request)
        return self


class JobResultIn(BaseModel):
    status: str = Field(pattern="^(completed|failed|deferred)$")
    result: dict[str, object] | None = None
    error_message: str | None = Field(default=None, max_length=1000)


class JobOut(BaseModel):
    job_id: str
    gateway_id: str
    job_type: str
    status: str
    request_json: dict[str, object]
    result_json: dict[str, object] | None
    error_message: str | None
    created_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class EdgeJobClaimOut(BaseModel):
    job_id: str
    gateway_id: str
    job_type: str
    request: dict[str, object]


class OperatorUserOut(BaseModel):
    email: str
    display_name: str | None
    role: str
    status: str
    supabase_user_id: str | None
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OperatorUserUpsertIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(pattern="^(admin|operator|viewer|pending)$")
    status: str = Field(pattern="^(active|pending|disabled)$")
    display_name: str | None = Field(default=None, max_length=200)
    supabase_user_id: str | None = Field(default=None, max_length=120)


class CurrentOperatorOut(BaseModel):
    email: str | None
    role: str
    status: str
    auth_type: str


class PublicAuthConfigOut(BaseModel):
    supabase_url: str | None
    supabase_anon_key: str | None
    configured: bool


class GatewaySummaryOut(BaseModel):
    total: int
    online: int
    stale: int
    offline: int


class GatewayGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class GatewayGroupOut(BaseModel):
    id: str
    gateway_id: str
    name: str
    created_at: datetime
    updated_at: datetime


class SavedDeviceIn(BaseModel):
    device_instance: int = Field(ge=0)
    group_id: str | None = None
    device_name: str | None = Field(default=None, max_length=255)
    vendor_name: str | None = Field(default=None, max_length=255)
    network_number: int | None = None
    mac_address: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class SavedDevicePatchIn(BaseModel):
    group_id: str | None = None
    device_name: str | None = Field(default=None, max_length=255)
    vendor_name: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class SavedDeviceOut(BaseModel):
    id: str
    gateway_id: str
    group_id: str | None
    device_instance: int
    device_name: str | None
    vendor_name: str | None
    network_number: int | None
    mac_address: str | None
    latest_discovered_at: datetime | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SavedPointIn(BaseModel):
    object_type: str = Field(min_length=1, max_length=80)
    object_instance: int = Field(ge=0)
    object_name: str | None = Field(default=None, max_length=255)
    property: str = Field(default="present-value", max_length=80)
    present_value: str | None = Field(default=None, max_length=255)
    units: str | None = Field(default=None, max_length=80)
    writable: bool | None = None
    enabled: bool = True


class SavedPointPatchIn(BaseModel):
    object_name: str | None = Field(default=None, max_length=255)
    present_value: str | None = Field(default=None, max_length=255)
    units: str | None = Field(default=None, max_length=80)
    writable: bool | None = None
    enabled: bool | None = None


class SavedPointsBulkRemoveIn(BaseModel):
    point_ids: list[str] = Field(min_length=1, max_length=500)


class SavedPointsBulkRemoveOut(BaseModel):
    requested_count: int
    removed_count: int
    missing_ids: list[str]


class SavedPointsReadIn(BaseModel):
    point_ids: list[str] = Field(min_length=1, max_length=100)


class SavedPointsReadOut(BaseModel):
    requested_count: int
    queued_count: int
    skipped_count: int
    job_ids: list[str]
    missing_ids: list[str]


class CommissioningTemplateGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CommissioningTemplatePointIn(BaseModel):
    object_type: str = Field(min_length=1, max_length=80)
    object_instance: int | None = Field(default=None, ge=0)
    instance: int | None = Field(default=None, ge=0)
    object_name: str | None = Field(default=None, max_length=255)
    property: str = Field(default="present-value", max_length=80)
    units: str | None = Field(default=None, max_length=80)
    writable: bool | None = None

    @model_validator(mode="after")
    def normalize_point(self) -> "CommissioningTemplatePointIn":
        if self.object_instance is None:
            self.object_instance = self.instance
        if self.object_instance is None:
            raise ValueError("object_instance is required")
        self.object_type = self.object_type.strip().lower()
        self.property = (self.property or "present-value").strip() or "present-value"
        return self


class CommissioningTemplateDeviceIn(BaseModel):
    device_instance: int | None = Field(default=None, ge=0)
    device_id: int | str | None = None
    device_name: str | None = Field(default=None, max_length=255)
    vendor_name: str | None = Field(default=None, max_length=255)
    vendor: str | None = Field(default=None, max_length=255)
    network_number: int | None = None
    mac_address: str | None = Field(default=None, max_length=255)
    mac: str | None = Field(default=None, max_length=255)
    group_name: str | None = Field(default=None, max_length=120)
    points: list[CommissioningTemplatePointIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_device(self) -> "CommissioningTemplateDeviceIn":
        if self.device_instance is None and self.device_id is not None:
            try:
                self.device_instance = int(self.device_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("device_id must be numeric when device_instance is omitted") from exc
        if self.device_instance is None:
            raise ValueError("device_instance is required")
        if self.vendor_name is None and self.vendor is not None:
            self.vendor_name = self.vendor
        if self.mac_address is None and self.mac is not None:
            self.mac_address = self.mac
        return self


class CommissioningTemplateIn(BaseModel):
    schema_version: str = Field(default="iot-cx-commissioning-template/v1", max_length=80)
    source: str | None = Field(default=None, max_length=120)
    site_id: str | None = Field(default=None, max_length=120)
    gateway_id: str | None = Field(default=None, max_length=120)
    groups: list[CommissioningTemplateGroupIn] = Field(default_factory=list)
    devices: list[CommissioningTemplateDeviceIn] = Field(default_factory=list)


class CommissioningTemplateImportOut(BaseModel):
    group_count: int
    device_count: int
    point_count: int
    created_groups: int
    updated_groups: int
    created_devices: int
    updated_devices: int
    created_points: int
    updated_points: int


class SavedPointOut(BaseModel):
    id: str
    gateway_id: str
    saved_device_id: str
    device_instance: int
    object_type: str
    object_instance: int
    object_name: str | None
    property: str
    present_value: str | None
    units: str | None
    writable: bool | None
    latest_read_at: datetime | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class GatewayTreeOut(BaseModel):
    gateway: GatewayOut
    groups: list[GatewayGroupOut]
    devices: list[SavedDeviceOut]
    points: list[SavedPointOut]
