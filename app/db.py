"""SQLAlchemy engine and session helpers. Postgres only, through psycopg v3.

The schema is owned by Alembic (alembic/versions); nothing here calls create_all.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def _check_url(url: str) -> None:
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Group HQ runs on Postgres only: point DATABASE_URL at a Postgres 16 "
            "database, for example postgresql+psycopg://user:password@host:5432/grouphq"
        )
    if url.startswith("sqlite"):
        raise RuntimeError("SQLite is not supported by Group HQ. Use a Postgres 16 database.")
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL must be a Postgres URL (postgresql+psycopg://...). Other databases are not supported.")


_check_url(config.DATABASE_URL)

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
