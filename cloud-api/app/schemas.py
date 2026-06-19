from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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


class JobCreateIn(BaseModel):
    gateway_id: str = Field(min_length=1, max_length=120)
    job_type: str = Field(min_length=1, max_length=80)
    request: dict[str, object] = Field(default_factory=dict)


class JobResultIn(BaseModel):
    status: str = Field(pattern="^(completed|failed)$")
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
