"""Alembic migration environment.

Uses the ORM metadata (``Base.metadata``) as the autogenerate target and
derives the database URL from the project's own
``build_sync_database_url()`` helper, so migrations and the runtime app
agree on where ``sessions.db`` lives — a single source of truth.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.claude.session import Base, build_sync_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the placeholder URL from alembic.ini with the project helper.
config.set_main_option("sqlalchemy.url", build_sync_database_url())

# Autogenerate target — the ORM models' shared metadata.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
