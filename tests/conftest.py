"""Shared fixtures for the Group HQ test suite.

The suite needs DATABASE_URL pointing at a LOCAL Postgres 16 and refuses to run without it. It never
touches the named database: it drops and recreates "<database>_test" beside it, migrates it to head
with Alembic, truncates every table between tests and re-seeds the programmes and one admin whose
authenticator secret is known (TEST_TOTP_SECRET), so admin_client can log in with a real code.

The environment is fixed at import time, BEFORE app.config (which reads the variables once) is
imported. Product tokens are replaced with test values, and the growth credentials blanked, so a developer's
shell can never leak a real one into a test, and CONNECTORS_DRY_RUN is forced on.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import pytest

ADMIN_EMAIL = "founder@example.test"
ADMIN_PASSWORD = "test-admin-pass-123"
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
TEST_SUFFIX = "_test"

_RAW_URL = os.environ.get("DATABASE_URL", "").strip()
if not _RAW_URL:
    pytest.exit(
        "DATABASE_URL is not set. The Group HQ tests need a local Postgres 16, for example\n"
        "  DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55453/grouphq\n"
        "The suite creates <database>_test beside it and never writes to the named database.",
        returncode=2,
    )


def _normalise(url: str) -> str:
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


_parts = urlsplit(_normalise(_RAW_URL))
if not _parts.scheme.startswith("postgresql"):
    pytest.exit("DATABASE_URL must be a Postgres URL; Group HQ does not support any other database.", returncode=2)
_host = _parts.hostname or ""
if _host not in ("", "localhost", "127.0.0.1", "::1") and os.environ.get("GROUPHQ_TEST_ALLOW_REMOTE_DB") != "1":
    pytest.exit(f"Refusing to run the tests against a non-local database host ({_host}). Point DATABASE_URL at a "
                "local Postgres, or set GROUPHQ_TEST_ALLOW_REMOTE_DB=1 if you really mean it.", returncode=2)
_dbname = _parts.path.lstrip("/")
if not _dbname:
    pytest.exit("DATABASE_URL has no database name; the suite derives <database>_test from it.", returncode=2)
TEST_DB = _dbname if _dbname.endswith(TEST_SUFFIX) else _dbname + TEST_SUFFIX
TEST_URL = urlunsplit(_parts._replace(path="/" + TEST_DB))
_MAINTENANCE_URL = urlunsplit(_parts._replace(path="/postgres"))


def _plain(url: str) -> str:
    """psycopg wants postgresql://, not SQLAlchemy's postgresql+psycopg://."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _recreate_test_database() -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(_plain(_MAINTENANCE_URL), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))


_recreate_test_database()

os.environ["DATABASE_URL"] = TEST_URL
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["BASE_URL"] = "http://testserver"
os.environ["SECURE_COOKIES"] = "0"       # TestClient speaks plain http; a Secure cookie would never come back
os.environ["ADMIN_EMAIL"] = ADMIN_EMAIL
os.environ["ADMIN_PASSWORD"] = ADMIN_PASSWORD
os.environ["CONNECTORS_DRY_RUN"] = "1"
for _key in ("PP_SERVICE_TOKEN", "ORDRIS_PARTNER_TOKEN", "KP_PARTNER_TOKEN", "HNH_PARTNER_TOKEN"):
    os.environ[_key] = f"test-{_key.lower()}"
# The growth credentials are blanked for the same reason: a developer's real key must never reach a test. Tests that
# need one set a test value on app.config (tests/test_growth.py, the creds fixture).
for _key in ("GOOGLE_SA_JSON", "GA4_PROPERTY_IDS", "GSC_SITES", "STRIPE_RK_MH196", "STRIPE_RK_HNH", "STRIPE_PRODUCT_MAP",
             "BREVO_API_KEY_HQ", "BREVO_LIST_PRODUCTS", "ZERNIO_API_KEY", "ZERNIO_PROFILE_PRODUCTS", "ZERNIO_AD_ACCOUNTS",
             "CLERK_SECRET_KEY_ORDRIS", "NEON_API_KEY", "NEON_ORG_ID", "NEON_PROJECT_PRODUCTS"):
    os.environ[_key] = ""
for _key, _value in (("PP_BASE_URL", "https://pp.example.test"), ("ORDRIS_BASE_URL", "https://ordris.example.test"),
                     ("KP_BASE_URL", "https://kp.example.test"), ("HNH_BASE_URL", "https://hnh.example.test")):
    os.environ[_key] = _value

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import auth, config, migrate, seed  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

migrate.upgrade_head()

# Hashed once: argon2 is deliberately slow and the admin is re-created before every test.
ADMIN_HASH = auth.hash_password(ADMIN_PASSWORD)


@pytest.fixture(scope="session", autouse=True)
def _dispose_engine():
    yield
    engine.dispose()


def _truncate_everything() -> None:
    with engine.begin() as conn:
        names = [row[0] for row in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"))]
        if names:
            conn.execute(text("TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " RESTART IDENTITY CASCADE"))


def reseed() -> None:
    """Programmes plus one enrolled admin, exactly as every test expects to find them."""
    with SessionLocal() as db:
        seed.ensure_programmes(db)
        db.add(User(email=ADMIN_EMAIL, password_hash=ADMIN_HASH, totp_secret=TEST_TOTP_SECRET, is_active=True))
        db.commit()


@pytest.fixture(autouse=True)
def _clean_db():
    """Every test starts from an empty, freshly seeded database; the login throttle lives in login_attempts and
    is truncated with everything else."""
    _truncate_everything()
    reseed()
    yield


@contextmanager
def session():
    """A short-lived SQLAlchemy session; open one per read so nothing stale is cached between requests."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def db():
    with session() as s:
        yield s


@pytest.fixture(scope="session")
def client():
    """Anonymous client. Entering the context runs the lifespan (migrate to head, seed)."""
    with TestClient(app, follow_redirects=False) as c:
        yield c


def totp_now(secret: str = TEST_TOTP_SECRET) -> str:
    return pyotp.TOTP(secret).now()


def forget_last_code(email: str = ADMIN_EMAIL) -> None:
    """Pretend the 30-second window has moved on, so a second login in one test is not refused as a replay."""
    with SessionLocal() as db:
        user = auth.find_user_by_email(db, email)
        if user is not None:
            user.totp_last_used = None
            db.commit()


def login(c: TestClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD, secret: str = TEST_TOTP_SECRET):
    """Password, then authenticator code. Returns the final redirect. The replay guard is reset first; it has
    its own test."""
    forget_last_code(email)
    r = c.post("/login", data={"email": email, "password": password})
    assert r.status_code == 303 and r.headers["location"] == "/login/code", f"password step failed: {r.status_code}"
    r = c.post("/login/code", data={"code": totp_now(secret)})
    assert r.status_code == 303, f"code step failed: {r.status_code} {r.text[:300]}"
    assert config.SESSION_COOKIE in r.headers.get("set-cookie", ""), "no session cookie set on login"
    return r


@pytest.fixture
def admin_client(client):
    c = TestClient(app, follow_redirects=False)
    login(c)
    return c
