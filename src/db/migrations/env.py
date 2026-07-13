"""Alembic async migration environment."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Load application models so Alembic can detect schema changes
# (models are registered in TASK-002)
# from src.db.models import Base  # uncomment after TASK-002

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata = Base.metadata  # uncomment after TASK-002
target_metadata = None


def get_url() -> str:
    """Read DATABASE_URL from environment (never from alembic.ini)."""
    from src.core.config import settings

    return str(settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL script)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live async DB connection."""
    engine = create_async_engine(get_url())

    async with engine.connect() as connection:
        await connection.run_sync(
            lambda sync_conn: context.configure(
                connection=sync_conn,
                target_metadata=target_metadata,
                compare_type=True,
                # For partitioned tables — include schema comparison
                include_schemas=True,
            )
        )
        async with connection.begin():
            await connection.run_sync(lambda _: context.run_migrations())

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
