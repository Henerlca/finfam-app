"""Additive recovery audit and restore snapshot tables.

No existing financial or notification table is altered by this migration.
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_recovery"
down_revision = "0003_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("activity_events"):
        op.create_table(
            "activity_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("entity_type", sa.String(length=64), nullable=False),
            sa.Column("entity_id", sa.String(length=128), nullable=False),
            sa.Column("before_payload", sa.JSON(), nullable=True),
            sa.Column("after_payload", sa.JSON(), nullable=True),
            sa.Column("undo_payload", sa.JSON(), nullable=True),
            sa.Column("undo_expires_at", sa.String(length=40), nullable=True),
            sa.Column("undone_at", sa.String(length=40), nullable=True),
        )
    inspector = sa.inspect(bind)
    activity_indexes = {index["name"] for index in inspector.get_indexes("activity_events")}
    if "ix_activity_events_created_at" not in activity_indexes:
        op.create_index("ix_activity_events_created_at", "activity_events", ["created_at"])
    if "ix_activity_events_entity" not in activity_indexes:
        op.create_index("ix_activity_events_entity", "activity_events", ["entity_type", "entity_id"])

    if not inspector.has_table("restore_snapshots"):
        op.create_table(
            "restore_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False, server_default="pre-restore"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="available"),
            sa.Column("restored_at", sa.String(length=40), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
    inspector = sa.inspect(bind)
    snapshot_indexes = {index["name"] for index in inspector.get_indexes("restore_snapshots")}
    if "ix_restore_snapshots_created_at" not in snapshot_indexes:
        op.create_index("ix_restore_snapshots_created_at", "restore_snapshots", ["created_at"])


def downgrade() -> None:
    raise RuntimeError("Downgrade desativado para preservar o histórico de recuperação.")
