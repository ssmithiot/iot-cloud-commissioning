from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM as PostgresEnum
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


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


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[UUID] = mapped_column(CloudUUID(), primary_key=True, default=uuid4)
    site_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    organization: Mapped[Organization | None] = relationship(back_populates="sites")
    edge_nodes: Mapped[list["EdgeNode"]] = relationship(back_populates="site")


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
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    edge_node: Mapped[EdgeNode] = relationship(back_populates="heartbeats")


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
