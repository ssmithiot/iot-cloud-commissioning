from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import GatewayAuthContext, require_gateway_auth
from app.config import settings
from app.database import Base, engine, get_db
from app.models import EdgeHeartbeat, EdgeJob, EdgeNode, Site, utc_now
from app.schemas import EdgeJobClaimOut, GatewayOut, HeartbeatAccepted, HeartbeatIn, JobCreateIn, JobOut, JobResultIn


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
def list_gateways(db: Session = Depends(get_db)) -> list[EdgeNode]:
    return list(db.scalars(select(EdgeNode).order_by(EdgeNode.gateway_id)).all())


@app.post("/api/edge/jobs", response_model=JobOut)
def create_job(payload: JobCreateIn, db: Session = Depends(get_db)) -> EdgeJob:
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
def list_jobs(db: Session = Depends(get_db), limit: int = 50) -> list[EdgeJob]:
    limit = max(1, min(limit, 200))
    return list(db.scalars(select(EdgeJob).order_by(EdgeJob.created_at.desc(), EdgeJob.id.desc()).limit(limit)).all())
