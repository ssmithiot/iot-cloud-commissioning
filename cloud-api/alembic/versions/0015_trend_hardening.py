"""add trend operational metadata and retrieval index

Revision ID: 0015_trend_hardening
Revises: 0014_bacnet_write_audit
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_trend_hardening"
down_revision = "0014_bacnet_write_audit"
branch_labels = None
depends_on = None


HEARTBEAT_TREND_COLUMNS = (
    ("trend_pending_upload_count", sa.Integer(), "0"),
    ("trend_deferred_upload_count", sa.Integer(), "0"),
    ("trend_oldest_pending_at", sa.DateTime(timezone=True), None),
    ("trend_max_upload_attempt_count", sa.Integer(), "0"),
)


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    for table_name in ("edge_nodes", "edge_heartbeats"):
        existing = _columns(table_name)
        for name, column_type, default in HEARTBEAT_TREND_COLUMNS:
            if name not in existing:
                kwargs = {"nullable": default is None}
                if default is not None:
                    kwargs["server_default"] = sa.text(default)
                op.add_column(table_name, sa.Column(name, column_type, **kwargs))

    sample_columns = _columns("point_trend_samples")
    if "quality" not in sample_columns:
        op.add_column("point_trend_samples", sa.Column("quality", sa.String(length=20), nullable=False, server_default="good"))
    if "source" not in sample_columns:
        op.add_column("point_trend_samples", sa.Column("source", sa.String(length=80), nullable=False, server_default="edge-agent"))
    if "received_at" not in sample_columns:
        op.add_column("point_trend_samples", sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))
        op.execute("UPDATE point_trend_samples SET received_at = created_at WHERE received_at IS NULL")

    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("point_trend_samples")}
    if "ix_point_trend_samples_point_sampled_at" not in indexes:
        op.create_index("ix_point_trend_samples_point_sampled_at", "point_trend_samples", ["point_id", "sampled_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_point_trend_samples_point_sampled_at", table_name="point_trend_samples")
    for name in ("received_at", "source", "quality"):
        op.drop_column("point_trend_samples", name)
    for table_name in ("edge_heartbeats", "edge_nodes"):
        for name, _, _ in reversed(HEARTBEAT_TREND_COLUMNS):
            op.drop_column(table_name, name)
