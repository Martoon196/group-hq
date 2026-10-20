"""Group HQ: the FastAPI application.

Boot order: Alembic upgrade to head, then the idempotent seed (programmes, first admin). Routers are
mounted at the root: /login, /login/code, /logout, /password, / (dashboard), /healthz, /assets.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, migrate
from .db import get_db
from .seed import seed
from .templating import templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.upgrade_head()
    seed(quiet=False)
    yield


app = FastAPI(title="Group HQ", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=str(config.APP_DIR / "static")), name="assets")

from .routers import audit, auth, dashboard, franchise, grants, mentees, people, programmes, sellers, territories  # noqa: E402  (after templates exist)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(people.router)
app.include_router(mentees.router)
app.include_router(grants.router)
app.include_router(programmes.router)
app.include_router(franchise.router)
app.include_router(sellers.router)
app.include_router(territories.router)
app.include_router(audit.router)

NO_STORE_PREFIXES = ("/login", "/password")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Defence in depth for an admin app: never framed, no content sniffing, no referrer leaking a path to
    another site, HSTS when the cookies are Secure, and the credential and enrolment pages never cached."""
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if config.SECURE_COOKIES:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith(NO_STORE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:   # noqa: BLE001  (any driver or network failure is the same answer to a health probe)
        return JSONResponse({"ok": False, "db": False}, status_code=503)
    return {"ok": True, "db": True}


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/") or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
    ctx = {"request": request, "status": exc.status_code, "detail": exc.detail}
    return templates.TemplateResponse(request, "error.html", ctx, status_code=exc.status_code)
