"""Read-only webinar listing for both brands from the host-bound Relaunch72 bridge.

Relaunch72 runs the webinars (video, registration, reminders, replays, CRM handoff). Group HQ only
lists them and opens their pages through the sign-in handoff. No product writes happen here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from .. import config
from .relaunch72_crm import ORIGIN, UUID
from .relaunch72_sso import BRANDS

PATH = "/api/group-hq/webinars"
KINDS = {"live": "Live", "on_demand": "On demand", "scheduled_recording": "Scheduled recording"}
STATUSES = {"draft": "Draft", "published": "Published", "cancelled": "Cancelled", "archived": "Archived"}
SESSIONS = {"idle", "live", "ended"}
PUBLIC_URL = re.compile(r"^https://[a-z0-9.-]+/w/[a-z0-9][a-z0-9-]{2,70}[a-z0-9]$")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")


class WebinarsUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Webinar:
    id: str
    title: str
    kind: str
    status: str
    starts_at: str | None
    timezone: str
    duration_minutes: int | None
    session_state: str
    registered: int
    crm_linked: int
    public_url: str | None
    manage_path: str
    host_path: str
    studio_path: str
    people_path: str

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]

    @property
    def status_label(self) -> str:
        return STATUSES[self.status]


@dataclass(frozen=True)
class WebinarListing:
    brand: str
    availability: str            # available | not_connected | not_available
    workspace_name: str | None
    webinars: tuple[Webinar, ...]


def _text(value: Any, limit: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise WebinarsUnavailable()
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise WebinarsUnavailable()
    return value


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 1_000_000:
        raise WebinarsUnavailable()
    return value


def _parse(payload: Any, brand: str) -> WebinarListing:
    if not isinstance(payload, dict) or payload.get("brand") != brand:
        raise WebinarsUnavailable()
    availability = payload.get("availability")
    if availability in ("not_connected", "not_available"):
        return WebinarListing(brand, availability, None, ())
    if availability != "available" or not isinstance(payload.get("webinars"), list) or len(payload["webinars"]) > 100:
        raise WebinarsUnavailable()
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        raise WebinarsUnavailable()
    name = _text(workspace.get("name"), 200, required=True)
    seen: set[str] = set()
    webinars: list[Webinar] = []
    for item in payload["webinars"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not UUID.fullmatch(item["id"]):
            raise WebinarsUnavailable()
        webinar_id = item["id"].lower()
        if webinar_id in seen:
            raise WebinarsUnavailable()
        seen.add(webinar_id)
        kind, status = item.get("kind"), item.get("status")
        if kind not in KINDS or status not in STATUSES:
            raise WebinarsUnavailable()
        starts_at = _text(item.get("startsAt"), 40)
        if starts_at is not None and not TIMESTAMP.fullmatch(starts_at):
            raise WebinarsUnavailable()
        duration = item.get("durationMinutes")
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 0 or duration > 1440):
            raise WebinarsUnavailable()
        session = item.get("sessionState")
        if session not in SESSIONS:
            raise WebinarsUnavailable()
        public_url = _text(item.get("publicUrl"), 400)
        if public_url is not None and not PUBLIC_URL.fullmatch(public_url):
            raise WebinarsUnavailable()
        paths = item.get("paths")
        base = f"/portal/webinars/{webinar_id}"
        if not isinstance(paths, dict) or paths.get("manage") != base or paths.get("host") != f"{base}/host" \
                or paths.get("studio") != f"{base}/studio" or paths.get("people") != f"{base}/people":
            raise WebinarsUnavailable()
        webinars.append(Webinar(
            id=webinar_id, title=_text(item.get("title"), 200, required=True) or "", kind=kind, status=status,
            starts_at=starts_at, timezone=_text(item.get("timezone"), 64, required=True) or "", duration_minutes=duration,
            session_state=session, registered=_count(item.get("registered")), crm_linked=_count(item.get("crmLinked")),
            public_url=public_url, manage_path=base, host_path=f"{base}/host", studio_path=f"{base}/studio",
            people_path=f"{base}/people",
        ))
    return WebinarListing(brand, "available", name, tuple(webinars))


def fetch_webinars(brand: str, *, client: httpx.Client | None = None, token: str | None = None) -> WebinarListing:
    if brand not in BRANDS:
        raise WebinarsUnavailable()
    secret = config.R72_CRM_TOKEN if token is None else token
    if not secret or len(secret) < 32:
        raise WebinarsUnavailable()
    own_client = client is None
    http = client or httpx.Client(timeout=5.0, follow_redirects=False)
    try:
        response = http.get(f"{ORIGIN}{PATH}", params={"brand": brand},
                            headers={"Authorization": f"Bearer {secret}", "Accept": "application/json",
                                     "User-Agent": "GroupHQ/1.0"})
        if response.status_code != 200 or len(response.content) > 200_000:
            raise WebinarsUnavailable()
        return _parse(response.json(), brand)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise WebinarsUnavailable() from exc
    finally:
        if own_client:
            http.close()
