"""Hybridbetrieb KI: eigener Rechner (local_workers, local_ai_jobs)

Revision ID: 026d11b8a1f1
Revises: a52d3aad31b6
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "026d11b8a1f1"
down_revision = "a52d3aad31b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "local_workers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("model_name", sa.String(120), nullable=True),
        sa.Column("worker_version", sa.String(30), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_local_workers_token_hash"),
    )
    op.create_index("ix_local_workers_tenant_id", "local_workers", ["tenant_id"])
    op.create_table(
        "local_ai_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("origin_type", sa.String(30), nullable=True),
        sa.Column("origin_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("message", sa.String(300), nullable=True),
        sa.Column("worker_id", sa.Integer(), sa.ForeignKey("local_workers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lease_id", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_local_ai_jobs_tenant_id", "local_ai_jobs", ["tenant_id"])
    op.create_index("ix_local_ai_jobs_tenant_status", "local_ai_jobs", ["tenant_id", "status"])
    op.create_index("ix_local_ai_jobs_created_by_user_id", "local_ai_jobs", ["created_by_user_id"])
    op.create_index("ix_local_ai_jobs_origin_id", "local_ai_jobs", ["origin_id"])


def downgrade():
    op.drop_table("local_ai_jobs")
    op.drop_table("local_workers")
