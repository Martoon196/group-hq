"""Runtime configuration, read once from environment variables.

Production needs DATABASE_URL (Postgres on Neon) and SECRET_KEY (Render generates it). A missing
SECRET_KEY falls back to a random per-process key so a developer can boot the app, at the price of
every session ending on restart. Nothing here has a default that points at a live product.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repository root (alembic.ini lives here)
APP_DIR = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def normalise_database_url(url: str) -> str:
    """Force the psycopg (v3) driver whatever spelling the hosting platform hands us.

    Render and Neon give postgres:// or postgresql://; SQLAlchemy would pick psycopg2 for the latter,
    which is not installed. Anything else (SQLite included) is passed through for app.db to refuse.
    """
    url = (url or "").strip()
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = normalise_database_url(os.environ.get("DATABASE_URL", ""))

SECRET_KEY = os.environ.get("SECRET_KEY") or ""
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)      # dev only: sessions reset on every restart
    DEV_SECRET = True
else:
    DEV_SECRET = False

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8030").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")       # empty: generated and printed once at first boot

SESSION_COOKIE = "grouphq_session"
PENDING_COOKIE = "grouphq_pending"                          # between the password and the authenticator code
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", 60 * 60 * 24 * 14))   # 14 days
PENDING_MAX_AGE = int(os.environ.get("PENDING_MAX_AGE", 5 * 60))              # 5 minutes to type the code
SECURE_COOKIES = _bool("SECURE_COOKIES", BASE_URL.startswith("https"))
TOTP_ISSUER = os.environ.get("TOTP_ISSUER", "Group HQ")
MIN_PASSWORD_LENGTH = 12

# Product connectors. Tokens are secrets and never have defaults; base URLs are the live hosts.
PP_BASE_URL = os.environ.get("PP_BASE_URL", "https://propertypredator.com").rstrip("/")
PP_SERVICE_TOKEN = os.environ.get("PP_SERVICE_TOKEN", "")
ORDRIS_BASE_URL = os.environ.get("ORDRIS_BASE_URL", "").rstrip("/")
ORDRIS_PARTNER_TOKEN = os.environ.get("ORDRIS_PARTNER_TOKEN", "")
KP_BASE_URL = os.environ.get("KP_BASE_URL", "https://portal.keypropertiesuk.co.uk").rstrip("/")
KP_PARTNER_TOKEN = os.environ.get("KP_PARTNER_TOKEN", "")
HNH_BASE_URL = os.environ.get("HNH_BASE_URL", "https://hellonewhome.co.uk").rstrip("/")
HNH_PARTNER_TOKEN = os.environ.get("HNH_PARTNER_TOKEN", "")


def _int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Outbound email for the franchise follow-ups (app.services.followups). With SMTP_HOST unset the emails are
# rendered on the opportunity page for copying and nothing is ever sent; MAIL_FROM is the From address and
# should be a Hello New Home mailbox, since the prospect is talking to Hello New Home, not to Group HQ.
SMTP_HOST = (os.environ.get("SMTP_HOST") or "").strip()
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_STARTTLS = _bool("SMTP_STARTTLS", True)
MAIL_FROM = (os.environ.get("MAIL_FROM") or "").strip()
# CONNECTORS_DRY_RUN: "1" records every outbound connector call instead of sending it (tests and rehearsals
# run this way); "0" sends to every product; a comma-separated list of products ("ordris,kp,hnh") keeps only
# those in dry run while the others go live, which is how PP can be live before the other contracts exist.
_DRY_RAW = (os.environ.get("CONNECTORS_DRY_RUN") or "").strip().lower()
if _DRY_RAW in ("", "0", "false", "no", "off"):
    CONNECTORS_DRY_RUN = False
    CONNECTORS_DRY_RUN_PRODUCTS: frozenset[str] = frozenset()
elif _DRY_RAW in ("1", "true", "yes", "on"):
    CONNECTORS_DRY_RUN = True
    CONNECTORS_DRY_RUN_PRODUCTS = frozenset()
else:
    CONNECTORS_DRY_RUN = False
    CONNECTORS_DRY_RUN_PRODUCTS = frozenset(p.strip() for p in _DRY_RAW.split(",") if p.strip())
