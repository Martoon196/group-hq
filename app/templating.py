"""Jinja2 environment, the render() helper and the flash/redirect conventions shared by every router.

Conventions (as in Hello New Home HQ): a POST redirects with 303 and carries its message in ?ok= or
?err=; render() picks that up as `flash`. Timestamps are naive UTC in the database and shown in UK
time through the `uk` filter.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import config
from .services import settings

LONDON = ZoneInfo("Europe/London")


def fmt_uk(value: datetime | None, fmt: str = "%d %b %Y %H:%M") -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LONDON).strftime(fmt)


def fmt_date(value, fmt: str = "%d %b %Y") -> str:
    return value.strftime(fmt) if value else ""


templates = Jinja2Templates(directory=str(config.APP_DIR / "templates"))
templates.env.globals.update({
    "app_name": "Group HQ",
    "base_url": config.BASE_URL,
    "s": settings.get_all(),
    "Markup": Markup,
    "product_name": settings.product_name,
})
templates.env.filters["uk"] = fmt_uk
templates.env.filters["date"] = fmt_date


def flash(request: Request) -> dict:
    err = request.query_params.get("err", "")
    ok = request.query_params.get("ok", "")
    if err:
        return {"flash": err[:400], "flash_kind": "err"}
    if ok:
        return {"flash": ok[:400], "flash_kind": "ok"}
    return {}


def render(request: Request, template: str, ctx: dict | None = None, status_code: int = 200) -> HTMLResponse:
    base: dict = {"request": request, "section": ""}
    base.update(flash(request))
    base.update(ctx or {})
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def go(path: str, *, ok: str = "", err: str = "") -> RedirectResponse:
    if ok or err:
        path, hash_, fragment = path.partition("#")       # the flash goes in the query, before any #fragment
        path += ("&" if "?" in path else "?") + urlencode({"err": err} if err else {"ok": ok})
        path += hash_ + fragment
    return RedirectResponse(path, status_code=303)
