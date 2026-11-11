"""Initial finance-family schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-15
"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.models import Base
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Downgrade desativado para proteger dados financeiros persistentes.")
