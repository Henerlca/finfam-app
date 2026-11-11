"""Add optional, read-only Open Finance cache tables.

No financial transaction, account, card, status, setting or forecast table is modified.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_open_finance"
down_revision = "0004_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("open_finance_items"):
        op.create_table(
            "open_finance_items",
            sa.Column("item_key", sa.String(length=32), primary_key=True),
            sa.Column("owner", sa.String(length=32), nullable=False),
            sa.Column("institution", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="UNKNOWN"),
            sa.Column("execution_status", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("provider_updated_at", sa.String(length=40), nullable=True),
            sa.Column("next_auto_sync_at", sa.String(length=40), nullable=True),
            sa.Column("synced_at", sa.String(length=40), nullable=True),
            sa.Column("last_error_code", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("last_error_message", sa.String(length=255), nullable=False, server_default=""),
        )
    if not inspector.has_table("open_finance_account_snapshots"):
        op.create_table(
            "open_finance_account_snapshots",
            sa.Column("account_id", sa.String(length=128), primary_key=True),
            sa.Column("snapshot_date", sa.String(length=10), primary_key=True),
            sa.Column("item_key", sa.String(length=32), nullable=False),
            sa.Column("owner", sa.String(length=32), nullable=False),
            sa.Column("institution", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=191), nullable=False),
            sa.Column("account_type", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("subtype", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("card_last4", sa.String(length=4), nullable=False, server_default=""),
            sa.Column("balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("closing_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("currency_code", sa.String(length=8), nullable=False, server_default="BRL"),
            sa.Column("balance_close_date", sa.String(length=10), nullable=True),
            sa.Column("balance_due_date", sa.String(length=10), nullable=True),
            sa.Column("credit_limit", sa.Numeric(18, 2), nullable=True),
            sa.Column("available_credit_limit", sa.Numeric(18, 2), nullable=True),
            sa.Column("minimum_payment", sa.Numeric(18, 2), nullable=True),
            sa.Column("synced_at", sa.String(length=40), nullable=False),
        )
        op.create_index("ix_open_finance_accounts_snapshot_date", "open_finance_account_snapshots", ["snapshot_date"])
        op.create_index("ix_open_finance_account_snapshots_item_key", "open_finance_account_snapshots", ["item_key"])
    if not inspector.has_table("open_finance_transactions"):
        op.create_table(
            "open_finance_transactions",
            sa.Column("transaction_id", sa.String(length=128), primary_key=True),
            sa.Column("item_key", sa.String(length=32), nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("occurred_on", sa.String(length=10), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=False),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("currency_code", sa.String(length=8), nullable=False, server_default="BRL"),
            sa.Column("transaction_type", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("category", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("provider_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("installment_number", sa.Integer(), nullable=True),
            sa.Column("total_installments", sa.Integer(), nullable=True),
            sa.Column("last_seen_at", sa.String(length=40), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.create_index("ix_open_finance_transactions_item_key", "open_finance_transactions", ["item_key"])
        op.create_index("ix_open_finance_transactions_account_date", "open_finance_transactions", ["account_id", "occurred_on"])
        op.create_index("ix_open_finance_transactions_current_date", "open_finance_transactions", ["is_current", "occurred_on"])
    if not inspector.has_table("open_finance_bills"):
        op.create_table(
            "open_finance_bills",
            sa.Column("bill_id", sa.String(length=128), primary_key=True),
            sa.Column("item_key", sa.String(length=32), nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("due_date", sa.String(length=10), nullable=True),
            sa.Column("closing_date", sa.String(length=10), nullable=True),
            sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("minimum_payment", sa.Numeric(18, 2), nullable=True),
            sa.Column("payment_amount_observed", sa.Numeric(18, 2), nullable=False, server_default="0"),
            sa.Column("last_payment_date", sa.String(length=10), nullable=True),
            sa.Column("currency_code", sa.String(length=8), nullable=False, server_default="BRL"),
            sa.Column("synced_at", sa.String(length=40), nullable=False),
        )
        op.create_index("ix_open_finance_bills_item_key", "open_finance_bills", ["item_key"])
        op.create_index("ix_open_finance_bills_account_due", "open_finance_bills", ["account_id", "due_date"])
    if not inspector.has_table("heartbeat_tasks"):
        op.create_table(
            "heartbeat_tasks",
            sa.Column("task_uid", sa.String(length=128), primary_key=True),
            sa.Column("task_name", sa.String(length=80), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.Column("last_run_at", sa.String(length=40), nullable=True),
        )


def downgrade() -> None:
    raise RuntimeError("Downgrade desativado para preservar snapshots externos e compatibilidade do aplicativo.")
