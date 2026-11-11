"""Pareamento Android e caixa de revisão; nenhuma alteração nos dados financeiros."""
from alembic import op
from sqlalchemy import select

revision = "0003_notifications"
down_revision = "0002_budget_adjustments"
branch_labels = None
depends_on = None


def upgrade():
    from app.models import Base
    tables = [Base.metadata.tables[name] for name in (
        "notification_pairing", "notification_devices", "notification_drafts", "notification_rate_limits",
    )]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=True)
    quota = Base.metadata.tables["notification_rate_limits"]
    bind = op.get_bind()
    if bind.execute(select(quota.c.key).where(quota.c.key == "draft-quota")).first() is None:
        bind.execute(quota.insert().values(key="draft-quota", window=0, count=0))


def downgrade():
    raise RuntimeError("Downgrade desativado para preservar os rascunhos e dispositivos.")
