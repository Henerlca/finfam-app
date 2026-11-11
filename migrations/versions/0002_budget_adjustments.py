"""Audit log for user-approved future pot reallocations.

Revision ID: 0002_budget_adjustments
Revises: 0001_initial
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_budget_adjustments"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("budget_adjustments"):
        op.create_table(
            "budget_adjustments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.Column("start_month", sa.String(length=10), nullable=False),
            sa.Column("end_month", sa.String(length=10), nullable=False),
            sa.Column("target_pot", sa.String(length=191), nullable=False),
            sa.Column("source_pot", sa.String(length=191), nullable=False),
            sa.Column("target_before", sa.Float(), nullable=False),
            sa.Column("target_after", sa.Float(), nullable=False),
            sa.Column("source_before", sa.Float(), nullable=False),
            sa.Column("source_after", sa.Float(), nullable=False),
            sa.Column("sample_months", sa.Integer(), nullable=False),
        )
        inspector = sa.inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes("budget_adjustments")}
    if "ix_budget_adjustments_created_at" not in index_names:
        op.create_index("ix_budget_adjustments_created_at", "budget_adjustments", ["created_at"])


def downgrade() -> None:
    raise RuntimeError("Downgrade desativado para preservar o histórico de ajustes financeiros.")
