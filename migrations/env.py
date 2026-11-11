from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.database import engine_url_and_connect_args
from app.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url, _ = engine_url_and_connect_args()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url, connect_args = engine_url_and_connect_args()
    connectable = create_engine(url, connect_args=connect_args, poolclass=pool.NullPool, pool_pre_ping=True)
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
