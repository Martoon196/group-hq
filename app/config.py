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

# Eight is the floor NIST SP 800-63B sets for a memorised secret; every login here also needs the authenticator
# code, which carries most of the protection. Raise it with MIN_PASSWORD_LENGTH if the policy changes.
MIN_PASSWORD_LENGTH = max(8, _int("MIN_PASSWORD_LENGTH", 8))


# Outbound email for the franchise follow-ups (app.services.followups). With SMTP_HOST unset the emails are
# rendered on the opportunity page for copying and nothing is ever sent; MAIL_FROM is the From address and
# should be a Hello New Home mailbox, since the prospect is talking to Hello New Home, not to Group HQ.
SMTP_HOST = (os.environ.get("SMTP_HOST") or "").strip()
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_STARTTLS = _bool("SMTP_STARTTLS", True)
MAIL_FROM = (os.environ.get("MAIL_FROM") or "").strip()
# Growth (app.services.growth): the read-only collectors behind the Growth screens. Every credential is an environment
# variable on the web service, copied to the growth cron that reads it by a fromService entry added once it is set
# there (render.yaml); none has a default, and none is ever logged, stored or shown (a recorded plan reads
# "[redacted]"). The *_PRODUCTS and *_IDS values are not secret: they map a platform's id to a product code,
# "id=code,id=code".
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SA_JSON", "")                  # the service-account key file's JSON, one line
GA4_PROPERTY_IDS = os.environ.get("GA4_PROPERTY_IDS", "")              # pp=123456789,kp=...
GSC_SITES = os.environ.get("GSC_SITES", "")                            # pp=sc-domain:propertypredator.com,...
STRIPE_RK_MH196 = os.environ.get("STRIPE_RK_MH196", "")                # restricted key, read only (MH196 2026 Ltd)
STRIPE_RK_HNH = os.environ.get("STRIPE_RK_HNH", "")                    # restricted key, read only (Hello New Home 2 Ltd)
STRIPE_PRODUCT_MAP = os.environ.get("STRIPE_PRODUCT_MAP", "")          # prod_...=pp,price_...=ordris (metadata.product wins)
BREVO_API_KEY_HQ = os.environ.get("BREVO_API_KEY_HQ", "")
BREVO_LIST_PRODUCTS = os.environ.get("BREVO_LIST_PRODUCTS", "")        # 123=kp,456=pp
ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY", "")                  # read-only scoped key
ZERNIO_PROFILE_PRODUCTS = os.environ.get("ZERNIO_PROFILE_PRODUCTS", "")  # <profileId>=pp,...
ZERNIO_AD_ACCOUNTS = os.environ.get("ZERNIO_AD_ACCOUNTS", "")          # <accountId>=tiktok,<accountId>=x, once connected
# Not recommended (DASHBOARD-SPEC 3.13): Clerk's backend key can delete Ordris users. Off unless switched on, and absent
# from render.yaml on purpose.
CLERK_SECRET_KEY_ORDRIS = os.environ.get("CLERK_SECRET_KEY_ORDRIS", "")
# Neon's organisation API key can change and delete projects: it lives on the web service and the daily growth cron
# only, and is never logged, stored or shown. NEON_PROJECT_PRODUCTS labels each project, "projectId=label,...";
# a label is free text (pp, kp, ordris, hnh and grouphq show as names, anything else as typed).
NEON_API_KEY = os.environ.get("NEON_API_KEY", "")
NEON_ORG_ID = os.environ.get("NEON_ORG_ID", "")
NEON_PROJECT_PRODUCTS = os.environ.get("NEON_PROJECT_PRODUCTS", "")


def pairs(raw: str) -> dict[str, str]:
    """"a=pp,b=kp" as {"a": "pp", "b": "kp"}: blanks and entries without "=" are dropped, keys and values trimmed.
    The value is everything after the first "=", so a key may not contain one but a value may."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key and value:
            out[key] = value
    return out


# CONNECTORS_DRY_RUN: "1" records every outbound connector call instead of sending it (tests and rehearsals
# run this way), a hard floor no switch on the Settings screen can lift. Otherwise each product follows its Live /
# Dry run switch on the Settings screen once that has been set, and this value is only the default for a product
# never switched: "0" defaults every product to live, and a comma-separated list ("pp") defaults those to dry run.
# For an emergency stop, use the product's Dry run switch, or "1" for everything.
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
