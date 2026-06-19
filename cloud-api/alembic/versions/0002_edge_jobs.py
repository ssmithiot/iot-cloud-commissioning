"""edge jobs

Revision ID: 0002_edge_jobs
Revises: 0001_initial_schema
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_edge_jobs"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edge_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=80), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_edge_jobs_job_id"),
    )
    op.create_index(op.f("ix_edge_jobs_gateway_id"), "edge_jobs", ["gateway_id"], unique=False)
    op.create_index(op.f("ix_edge_jobs_job_id"), "edge_jobs", ["job_id"], unique=False)
    op.create_index(op.f("ix_edge_jobs_job_type"), "edge_jobs", ["job_type"], unique=False)
    op.create_index(op.f("ix_edge_jobs_status"), "edge_jobs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_edge_jobs_status"), table_name="edge_jobs")
    op.drop_index(op.f("ix_edge_jobs_job_type"), table_name="edge_jobs")
    op.drop_index(op.f("ix_edge_jobs_job_id"), table_name="edge_jobs")
    op.drop_index(op.f("ix_edge_jobs_gateway_id"), table_name="edge_jobs")
    op.drop_table("edge_jobs")
