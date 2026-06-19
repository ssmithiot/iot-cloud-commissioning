from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    sites: Mapped[list["Site"]] = relationship(back_populates="organization")


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    organization: Mapped[Organization | None] = relationship(back_populates="sites")
    edge_nodes: Mapped[list["EdgeNode"]] = relationship(back_populates="site")


class EdgeNode(Base):
    __tablename__ = "edge_nodes"
    __table_args__ = (UniqueConstraint("gateway_id", name="uq_edge_nodes_gateway_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edge_node_id: Mapped[int] = mapped_column(ForeignKey("edge_nodes.id"), nullable=False, index=True)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    gateway_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued", index=True)
    request_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
