"""Shared database helper utilities.

Identical design to the Flask template: a single shared engine, a context-managed
connection, and environment-variable-based configuration. Works behind a load
balancer across multiple pods as long as DATABASE_URL points to the HA Postgres
endpoint (e.g. your-postgres-host).
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine


def _build_database_url() -> str:
    """Build a SQLAlchemy database URL from environment variables.

    Supports two styles:
    1. DATABASE_URL: full SQLAlchemy URL
       e.g. postgresql+psycopg2://user:pass@your-postgres-host:5432/myapp
    2. DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME: individual fields.
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "app_user")
    password = os.getenv("DB_PASSWORD", "changeme")
    name = os.getenv("DB_NAME", "app_db")

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a shared SQLAlchemy engine.

    Cached so all parts of the app share one pool. Safe across multiple
    uvicorn workers because each worker process gets its own engine instance.
    """
    return create_engine(
        _build_database_url(),
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
    )


@contextmanager
def get_connection() -> Generator[Connection, None, None]:
    """Yield a database connection wrapped in a transaction.

    Commits on success, rolls back on error. Use this everywhere you need
    to run queries — do not create connections ad-hoc.

    Usage:
        with get_connection() as conn:
            conn.execute(text("SELECT 1"))
    """
    with get_engine().begin() as connection:
        yield connection


def dispose_engine() -> None:
    """Dispose of the shared engine and its connection pool.

    Called during app shutdown (lifespan) or in tests to reset pooled
    connections cleanly.
    """
    get_engine().dispose()
    get_engine.cache_clear()
