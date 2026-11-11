"""Store invoice installment overrides separately from purchase totals.

Revision ID: 0006_installment_overrides
Revises: 0005_open_finance
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_installment_overrides"
down_revision = "0005_open_finance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "installment_overrides" not in columns:
        op.add_column("transactions", sa.Column("installment_overrides", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "installment_overrides" in columns:
        op.drop_column("transactions", "installment_overrides")
