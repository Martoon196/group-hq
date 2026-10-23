"""Brevo (API v3), counts only: lists, new contacts by day, sent campaigns and transactional health.

    GET /v3/contacts/lists?limit=50&offset=                    uniqueSubscribers per list -> email_contacts list:<name>
    GET /v3/contacts?createdSince=<iso>&limit=500&offset=      contacts created by day -> email_contacts_new (counted, never kept)
    GET /v3/emailCampaigns?status=sent&statistics=globalStats&limit=100&offset=
                                                               per campaign: email_sent, email_delivered,
                                                               email_opens_unique, email_clicks_unique,
                                                               email_unsubscribed, email_bounces, email_spam_reports
    GET /v3/smtp/statistics/reports?startDate=&endDate=        transactional by day, dimension stream:transactional
    GET /v3/account                                            the Test button

Auth: the api-key header (BREVO_API_KEY_HQ, a key named grouphq; Brevo keys are full access, so Brevo's IP allowlist
is the second lock). Products: BREVO_LIST_PRODUCTS maps list ids to products ("123=kp,456=pp"); a campaign's product
is its name's prefix (pp-, kp-, ord-, hnh-, the UTM convention), else its first mapped list, else group. Campaign
statistics move for weeks after a send, so campaigns sent up to CAMPAIGN_LOOKBACK_DAYS before the window are pulled
again. No email address, name or per-person open history is ever stored: contacts are counted and dropped.

Two lists with one name, or two campaigns with one name sent on one day, are added up into one row. Each pull answers
in full for today's list counts, the window's new contacts and every campaign sent from the look-back to the end of
the window, so those rows replace the earlier ones (a list remapped in BREVO_LIST_PRODUCTS, or renamed, leaves no stale
row behind). The transactional rows keep their fixed key and are simply upserted.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .... import config
from ...connectors.base import ConnectorResult
from ..attribution import product_from_campaign
from .base import (MAX_PAGES, REDACTED_VALUE, MetricCollector, PullResult, Replace, SnapshotRow, days_between,
                   london_date, number, today_london)

CAMPAIGN_LOOKBACK_DAYS = 30
LIST_PAGE = 50
CONTACT_PAGE = 500
CAMPAIGN_PAGE = 100
CAMPAIGN_STATS = (("email_sent", ("sent",)), ("email_delivered", ("delivered",)),
                  ("email_opens_unique", ("uniqueViews",)), ("email_clicks_unique", ("uniqueClicks",)),
                  ("email_unsubscribed", ("unsubscriptions",)), ("email_bounces", ("hardBounces", "softBounces")),
                  ("email_spam_reports", ("complaints",)))
SMTP_STATS = (("email_sent", ("requests",)), ("email_delivered", ("delivered",)),
              ("email_opens_unique", ("uniqueOpens",)), ("email_clicks_unique", ("uniqueClicks",)),
              ("email_unsubscribed", ("unsubscribed",)), ("email_bounces", ("hardBounces", "softBounces")),
              ("email_spam_reports", ("spamReports",)))
TRANSACTIONAL = "stream:transactional"


def _sum(stats: dict, keys: tuple[str, ...]) -> Decimal | None:
    values = [number(stats.get(k)) for k in keys]
    if all(v is None for v in values):
        return None
    return sum((v for v in values if v is not None), Decimal(0))


class BrevoCollector(MetricCollector):
    source = "brevo"
    env_names = ("BREVO_API_KEY_HQ", "BREVO_LIST_PRODUCTS")
    window_days = 7
    timeout_seconds = 20.0
    BASE = "https://api.brevo.com/v3"

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "BrevoCollector":
        return cls(cls.BASE, config.BREVO_API_KEY_HQ, dry_run=dry_run)

    def __init__(self, *args, list_products: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        raw = list_products if list_products is not None else config.pairs(config.BREVO_LIST_PRODUCTS)
        self.list_products = {str(k): v for k, v in raw.items() if v in ("pp", "kp", "ordris", "hnh")}

    def missing_env(self) -> list[str]:
        return [] if self._token else ["BREVO_API_KEY_HQ"]

    def _headers(self) -> dict:
        return {"api-key": self._token, "Accept": "application/json", "User-Agent": "GroupHQ-Growth/1.0"}

    def _plan_headers(self) -> dict:
        return {"api-key": REDACTED_VALUE}

    def _paged(self, path: str, key: str, params: dict, page_size: int, notes: list):
        offset = 0
        for _ in range(MAX_PAGES):
            body = self.get_json(path, params={**params, "limit": page_size, "offset": offset})
            if body is None:
                return
            items = body.get(key) if isinstance(body, dict) else None
            items = items if isinstance(items, list) else []
            yield from (i for i in items if isinstance(i, dict))
            if len(items) < page_size:
                return
            offset += page_size
        notes.append(f"{path}: stopped after {MAX_PAGES} pages")

    def product_of_lists(self, list_ids) -> str:
        for list_id in list_ids or []:
            product = self.list_products.get(str(list_id))
            if product:
                return product
        return "group"

    def pull(self, since: date, until: date, *, tier: str = "daily") -> PullResult:
        notes: list = []
        rows: list = []
        today = today_london()
        window = set(days_between(since, until))

        lists: dict[tuple[str, str], dict] = {}
        for item in self._paged("/contacts/lists", "lists", {}, LIST_PAGE, notes):
            count = number(item.get("uniqueSubscribers"))
            if count is None:
                continue
            product = self.list_products.get(str(item.get("id")), "group")
            entry = lists.setdefault((product, f"list:{str(item.get('name') or item.get('id'))[:150]}"),
                                     {"count": Decimal(0), "ids": []})
            entry["count"] += count
            entry["ids"].append(item.get("id"))
        for (product, dimension), entry in sorted(lists.items()):
            meta = {"list_id": entry["ids"][0]} if len(entry["ids"]) == 1 else {"list_ids": entry["ids"][:20]}
            rows.append(SnapshotRow(today, product, "email_contacts", entry["count"], dimension, meta))

        new: dict[tuple[date, str], int] = defaultdict(int)
        created_since = f"{since - timedelta(days=1):%Y-%m-%d}T00:00:00.000Z"      # Brevo's YYYY-MM-DDTHH:mm:ss.SSSZ
        for contact in self._paged("/contacts", "contacts", {"createdSince": created_since}, CONTACT_PAGE, notes):
            day = london_date(contact.get("createdAt"))
            if day in window:
                new[(day, self.product_of_lists(contact.get("listIds")))] += 1
            contact.clear()                                   # counted; nothing about the person is kept
        for day in sorted(window):
            new.setdefault((day, "group"), 0)
        for (day, product), count in sorted(new.items()):
            rows.append(SnapshotRow(day, product, "email_contacts_new", count))

        oldest = since - timedelta(days=CAMPAIGN_LOOKBACK_DAYS)
        sent: dict[tuple[date, str, str, str], Decimal] = defaultdict(Decimal)
        ids: dict[tuple[date, str, str], list] = defaultdict(list)
        for campaign in self._paged("/emailCampaigns", "campaigns", {"status": "sent", "statistics": "globalStats"},
                                    CAMPAIGN_PAGE, notes):
            day = london_date(campaign.get("sentDate") or campaign.get("scheduledAt"))
            if day is None or day < oldest or day > until:
                continue
            name = str(campaign.get("name") or campaign.get("id"))[:150]
            product = product_from_campaign(name)
            if product == "group":
                product = self.product_of_lists(((campaign.get("recipients") or {}).get("lists")))
            dimension = f"campaign:{name}"
            ids[(day, product, dimension)].append(campaign.get("id"))
            stats = ((campaign.get("statistics") or {}).get("globalStats") or {})
            for metric, keys in CAMPAIGN_STATS:
                value = _sum(stats, keys)
                if value is not None:
                    sent[(day, product, metric, dimension)] += value       # two campaigns with one name add up
        for (day, product, metric, dimension), value in sorted(sent.items()):
            found = ids[(day, product, dimension)]
            meta = {"campaign_id": found[0]} if len(found) == 1 else {"campaign_ids": found[:20]}
            rows.append(SnapshotRow(day, product, metric, value, dimension, meta))

        body = self.get_json("/smtp/statistics/reports", params={"startDate": since.isoformat(), "endDate": until.isoformat()})
        for report in (body or {}).get("reports") or [] if isinstance(body, dict) else []:
            if not isinstance(report, dict):
                continue
            day = london_date(report.get("date")) if "T" in str(report.get("date") or "") else None
            if day is None:
                try:
                    day = date.fromisoformat(str(report.get("date") or "")[:10])
                except ValueError:
                    continue
            for metric, keys in SMTP_STATS:
                value = _sum(report, keys)
                if value is not None:
                    rows.append(SnapshotRow(day, "group", metric, value, TRANSACTIONAL))
        replace = [Replace(("email_contacts",), today, today, "list:%"),
                   Replace(("email_contacts_new",), since, until, ""),
                   Replace(tuple(metric for metric, _ in CAMPAIGN_STATS), oldest, until, "campaign:%")]
        return self.result(rows, notes, replace)

    def probe(self) -> ConnectorResult:
        result = self.request("GET", "/account")
        if not result.dry_run and isinstance(result.data, dict):
            result.data = {"ok": True, "plan": [p.get("type") for p in result.data.get("plan") or [] if isinstance(p, dict)]}
        return result
