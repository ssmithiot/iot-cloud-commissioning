from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM as PostgresEnum
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


def uuid_str() -> str:
    return str(uuid4())


class StringList(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(String))
        return dialect.type_descriptor(JSON)


class EdgeJobStatus(TypeDecorator):
    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(
                PostgresEnum(
                    "queued",
                    "claimed",
                    "completed",
                    "failed",
                    "deferred",
                    name="edge_job_status",
                    schema="public",
                    create_type=False,
                )
            )
        return dialect.type_descriptor(String(40))


class CloudUUID(TypeDecorator):
    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PostgresUUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, UUID) else UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, UUID):
            return value
        return UUID(str(value))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    sites: Mapped[list["Site"]] = relationship(back_populates="organization")
    memberships: Mapped[list["OrganizationMembership"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    site_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    address_street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address_state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    address_postal_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    store_hours_mf: Mapped[str | None] = mapped_column(String(120), nullable=True)
    store_hours_sat: Mapped[str | None] = mapped_column(String(120), nullable=True)
    store_hours_sun: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cradlepoint_ip: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direct_connect_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direct_connect_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gateway_ui_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    store_hours_monday_friday: Mapped[str | None] = mapped_column(String(120), nullable=True)
    store_hours_saturday: Mapped[str | None] = mapped_column(String(120), nullable=True)
    store_hours_sunday: Mapped[str | None] = mapped_column(String(120), nullable=True)
    network_status_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    organization: Mapped[Organization | None] = relationship(back_populates="sites")
    edge_nodes: Mapped[list["EdgeNode"]] = relationship(back_populates="site")
    weather: Mapped["SiteWeather | None"] = relationship(back_populates="site")
    memberships: Mapped[list["SiteMembership"]] = relationship(back_populates="site", cascade="all, delete-orphan")


class SiteWeather(Base):
    __tablename__ = "site_weather"

    site_id: Mapped[str] = mapped_column(String(120), ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False, default="open-meteo")
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    temperature_f: Mapped[float | None] = mapped_column(Float, nullable=True)
    apparent_temperature_f: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_humidity_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    precipitation_in: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_mph: Mapped[float | None] = mapped_column(Float, nullable=True)
    weather_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    condition: Mapped[str | None] = mapped_column(String(120), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(120), nullable=True)
    timezone_abbreviation: Mapped[str | None] = mapped_column(String(40), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sunrise_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sunset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    solar_noon_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    raw_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)

    site: Mapped[Site] = relationship(back_populates="weather")


class EdgeNode(Base):
    __tablename__ = "edge_nodes"
    __table_args__ = (UniqueConstraint("gateway_id", name="uq_edge_nodes_gateway_id"),)

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    gateway_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    site_id: Mapped[str] = mapped_column(String(120), ForeignKey("sites.site_id"), nullable=False, index=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    lan_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bacnet_port: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(80), nullable=False)
    ui_version: Mapped[str] = mapped_column(String(80), nullable=False)
    sqlite_db_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    queued_upload_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_load_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_load_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_available_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_free_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    latest_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    site: Mapped[Site] = relationship(back_populates="edge_nodes")
    heartbeats: Mapped[list["EdgeHeartbeat"]] = relationship(back_populates="edge_node")


class EdgeHeartbeat(Base):
    __tablename__ = "edge_heartbeats"

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    edge_node_id: Mapped[UUID] = mapped_column(ForeignKey("edge_nodes.id"), nullable=False, index=True)
    gateway_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    site_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    lan_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bacnet_port: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(80), nullable=False)
    ui_version: Mapped[str] = mapped_column(String(80), nullable=False)
    sqlite_db_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    queued_upload_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_load_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_load_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_available_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_free_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    edge_node: Mapped[EdgeNode] = relationship(back_populates="heartbeats")


class GatewayUpdateRequest(Base):
    __tablename__ = "gateway_update_requests"

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    gateway_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued", index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class EdgeJob(Base):
    __tablename__ = "edge_jobs"
    __table_args__ = (UniqueConstraint("job_id", name="uq_edge_jobs_job_id"),)

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    job_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    gateway_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(EdgeJobStatus(), nullable=False, default="queued", index=True)
    request_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GatewayCredential(Base):
    __tablename__ = "gateway_credentials"
    __table_args__ = (
        UniqueConstraint("token_prefix", name="uq_gateway_credentials_token_prefix"),
    )

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    gateway_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_prefix: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(StringList(), nullable=False, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class OperatorUser(Base):
    __tablename__ = "operator_users"
    __table_args__ = (UniqueConstraint("email", name="uq_operator_users_email"),)

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    supabase_user_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    organization_memberships: Mapped[list["OrganizationMembership"]] = relationship(back_populates="operator", cascade="all, delete-orphan")
    site_memberships: Mapped[list["SiteMembership"]] = relationship(back_populates="operator", cascade="all, delete-orphan")


class GatewayGroup(Base):
    __tablename__ = "gateway_groups"
    __table_args__ = (UniqueConstraint("gateway_id", "name", name="uq_gateway_groups_gateway_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    gateway_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class SavedBacnetDevice(Base):
    __tablename__ = "saved_bacnet_devices"
    __table_args__ = (UniqueConstraint("gateway_id", "device_instance", name="uq_saved_devices_gateway_instance"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    gateway_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("gateway_groups.id", ondelete="SET NULL"), nullable=True)
    device_instance: Mapped[int] = mapped_column(Integer, nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    network_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mac_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latest_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class SavedBacnetPoint(Base):
    __tablename__ = "saved_bacnet_points"
    __table_args__ = (
        UniqueConstraint(
            "saved_device_id",
            "object_type",
            "object_instance",
            "property_name",
            name="uq_saved_points_device_object_property",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    gateway_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    saved_device_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_bacnet_devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_instance: Mapped[int] = mapped_column(Integer, nullable=False)
    object_type: Mapped[str] = mapped_column(String(80), nullable=False)
    object_instance: Mapped[int] = mapped_column(Integer, nullable=False)
    object_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    property_name: Mapped[str] = mapped_column(String(80), nullable=False, default="present-value")
    present_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    units: Mapped[str | None] = mapped_column(String(80), nullable=True)
    writable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latest_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    trend_config: Mapped["PointTrendConfig | None"] = relationship(back_populates="point", cascade="all, delete-orphan", uselist=False)
    trend_samples: Mapped[list["PointTrendSample"]] = relationship(back_populates="point", cascade="all, delete-orphan")


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (UniqueConstraint("organization_id", "operator_user_id", name="uq_org_memberships_org_operator"),)

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    operator_user_id: Mapped[UUID] = mapped_column(ForeignKey("operator_users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="memberships")
    operator: Mapped[OperatorUser] = relationship(back_populates="organization_memberships")


class SiteMembership(Base):
    __tablename__ = "site_memberships"
    __table_args__ = (UniqueConstraint("site_uuid", "operator_user_id", name="uq_site_memberships_site_operator"),)

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    site_uuid: Mapped[UUID] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True)
    operator_user_id: Mapped[UUID] = mapped_column(ForeignKey("operator_users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    site: Mapped[Site] = relationship(back_populates="memberships")
    operator: Mapped[OperatorUser] = relationship(back_populates="site_memberships")


class PointTrendConfig(Base):
    __tablename__ = "point_trend_configs"

    point_id: Mapped[str] = mapped_column(String(36), ForeignKey("saved_bacnet_points.id", ondelete="CASCADE"), primary_key=True)
    gateway_id: Mapped[str] = mapped_column(String(120), ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interval_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    point: Mapped["SavedBacnetPoint"] = relationship(back_populates="trend_config")


class PointTrendSample(Base):
    __tablename__ = "point_trend_samples"
    __table_args__ = (UniqueConstraint("point_id", "sampled_at", name="uq_point_trend_sample_time"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    point_id: Mapped[str] = mapped_column(String(36), ForeignKey("saved_bacnet_points.id", ondelete="CASCADE"), nullable=False, index=True)
    gateway_id: Mapped[str] = mapped_column(String(120), ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"), nullable=False, index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    point: Mapped["SavedBacnetPoint"] = relationship(back_populates="trend_samples")
