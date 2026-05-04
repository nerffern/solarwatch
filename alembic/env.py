"""Alembic environment configuration.

Reads database connection settings from the same environment variables
the application uses (DATABASE_URL or DB_*). This means you never need
to duplicate credentials — your .env file or K8s Secret is the single
source of truth for both the app and migrations.

Usage:
    # Apply all pending migrations
    alembic upgrade head

    # Roll back one migration
    alembic downgrade -1

    # Generate a new migration from a schema change
    alembic revision --autogenerate -m "add column X to table Y"

    # Show current migration state
    alembic current

    # Show migration history
    alembic history
"""

import importlib
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Load .env if python-dotenv is available, same as the app does.
if importlib.util.find_spec("dotenv") is not None:
    importlib.import_module("dotenv").load_dotenv()


def _build_database_url() -> str:
    """Build the database URL the same way app/db.py does."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "app_user")
    password = os.getenv("DB_PASSWORD", "changeme")
    name = os.getenv("DB_NAME", "app_db")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


# Alembic Config object — gives access to values in alembic.ini.
config = context.config

# Override the placeholder sqlalchemy.url with the real one from env.
config.set_main_option("sqlalchemy.url", _build_database_url())

# Set up Python logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata: set this to your SQLAlchemy MetaData object if you want
# autogenerate support. Since this template uses raw SQL (not ORM models),
# we leave it as None. For autogenerate, import your Base.metadata here:
#
#   from app.models import Base
#   target_metadata = Base.metadata
#
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection needed).

    Useful for generating SQL scripts to review before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # NullPool: no persistent pool for migration runs.
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
