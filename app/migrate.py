"""Run Alembic programmatically (used by the app's lifespan and by the test suite).

Render runs `alembic upgrade head` as the pre-deploy command; the lifespan runs the same thing again
on boot so a developer's database is never behind the code. Both share one connection from app.db.
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from . import config


def alembic_config() -> Config:
    cfg = Config(str(config.ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.ROOT / "alembic"))
    cfg.attributes["configure_logging"] = False     # keep uvicorn's logging as it is
    return cfg


def upgrade_head() -> None:
    from .db import engine
    cfg = alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def current_revision() -> str | None:
    from .db import engine
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()
