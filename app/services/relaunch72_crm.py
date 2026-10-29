"""Read-only Property Predator lead list from the host-bound Relaunch72 bridge.

No product writes, database sharing, browser token or automatic retry occurs here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from .. import config

ORIGIN = "https://hq.propertypredator.com"
PATH = "/api/group-hq/crm/leads"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
CURSOR = re.compile(r"^[A-Za-z0-9_-]{4,256}$")


class CrmUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Lead:
    id: str
    name: str
    source: str | None
    lifecycle: str
    stage: str | None
    next_task_at: str | None
    last_activity_at: str | None
    detail_url: str


@dataclass(frozen=True)
class LeadPage:
    leads: tuple[Lead, ...]
    next_cursor: str | None


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise CrmUnavailable()
    return value


def _parse(payload: Any) -> LeadPage:
    if not isinstance(payload, dict) or payload.get("brand") != "pp" or not isinstance(payload.get("leads"), list):
        raise CrmUnavailable()
    raw_leads = payload["leads"]
    if len(raw_leads) > 50:
        raise CrmUnavailable()
    next_cursor = payload.get("nextCursor")
    if next_cursor is not None and (not isinstance(next_cursor, str) or not CURSOR.fullmatch(next_cursor)):
        raise CrmUnavailable()
    leads: list[Lead] = []
    ids: set[str] = set()
    for item in raw_leads:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not UUID.fullmatch(item["id"]):
            raise CrmUnavailable()
        lead_id = item["id"].lower()
        if lead_id in ids:
            raise CrmUnavailable()
        ids.add(lead_id)
        name = _text(item.get("name"), 200)
        if not name or not name.strip():
            raise CrmUnavailable()
        lifecycle = _text(item.get("lifecycle"), 20)
        if lifecycle not in ("lead", "customer", "archived"):
            raise CrmUnavailable()
        leads.append(Lead(
            id=lead_id, name=name, source=_text(item.get("source"), 100), lifecycle=lifecycle,
            stage=_text(item.get("stage"), 100), next_task_at=_text(item.get("nextTaskAt"), 50),
            last_activity_at=_text(item.get("lastActivityAt"), 50),
            detail_url=f"{ORIGIN}/portal/crm/contacts/{lead_id}",
        ))
    return LeadPage(tuple(leads), next_cursor)


def fetch_leads(cursor: str | None = None, *, client: httpx.Client | None = None,
                token: str | None = None) -> LeadPage:
    secret = config.R72_CRM_TOKEN if token is None else token
    if not secret or len(secret) < 32:
        raise CrmUnavailable()
    if cursor is not None and not CURSOR.fullmatch(cursor):
        raise CrmUnavailable()
    own_client = client is None
    http = client or httpx.Client(timeout=5.0, follow_redirects=False)
    try:
        response = http.get(f"{ORIGIN}{PATH}", params={"brand": "pp", **({"cursor": cursor} if cursor else {})},
                            headers={"Authorization": f"Bearer {secret}", "Accept": "application/json",
                                     "User-Agent": "GroupHQ/1.0"})
        if response.status_code != 200 or len(response.content) > 100_000:
            raise CrmUnavailable()
        return _parse(response.json())
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise CrmUnavailable() from exc
    finally:
        if own_client:
            http.close()
