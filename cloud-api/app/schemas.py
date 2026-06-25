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
        raise ValueError(f"object_type must be one of: {allowed}")

    property_name = request.get("property", "present-value")
    if property_name != "present-value":
        raise ValueError("property must be present-value")

    return {
        "device_instance": device_instance,
        "object_type": object_type,
        "object_instance": object_instance,
        "property": "present-value",
    }


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
