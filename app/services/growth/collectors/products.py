"""The four products' own ledgers, read through their partner APIs with the tokens Group HQ already holds.

    GET {base}{METRICS}?from=2026-11-01&to=2026-11-14     {"ok": true, "product": "pp", "days": [{"date": ..., ...}]}
    GET {base}{PURCHASES}?since=2026-11-01T00:00:00Z&cursor=   {"ok": true, "purchases": [...], "next_cursor": null}

Paths: Property Predator and Ordris under /api/partner/grouphq/, Key Properties and Hello New Home under
/api/partner/ (Hello New Home has no checkout: its purchases are instructions, at /api/partner/instructions, in the
same envelope). Auth is each product's existing connector's own headers (PPConnector sends X-Service-Token; the
others Bearer plus X-Partner-Client: grouphq), so the plan redacts exactly what the grant calls redact.

A day the product cannot compute is omitted, not zero, and so is a metric missing from a day: nothing is written
for it, and an earlier value for it is kept. What a day does carry is that day's whole answer: a metric present
replaces the day's earlier total, and a breakdown or promo_redemptions object present replaces the day's earlier
rows of that breakdown (a code or sign-up method no longer reported is not left behind). Promo codes are upper-cased,
so two spellings of one code add up; a day listed twice is refused (a product bug, said loudly). account_key must already be a sha256 hex digest (the products hash person_key); anything else is dropped,
so an email can never land here even if a product sends one. Touch records keep only the utm, landing, referrer and
ref keys, each cut at 200 characters; raw keeps only the documented fields.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from ...connectors import CONNECTORS
from ...connectors.base import BaseConnector, ConnectorError, ConnectorResult, iso_utc
from .base import (MAX_PAGES, MetricCollector, PullResult, PurchaseRow, Replace, SnapshotRow, number, parse_day,
                   parse_instant, today_london)

DAY_METRICS = ("sign_ups", "leads", "trials_started", "trials_active", "paying_new", "paying_active", "cancelled",
               "mrr_pence", "revenue_pence", "refunds_pence", "purchases", "reservations", "instructions",
               "affiliate_sign_ups")
BREAKDOWNS = {"sign_ups_by_method": ("sign_ups", "method"), "leads_by_kind": ("leads", "kind")}
TOUCH_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "landing", "referrer", "ref", "ts")
PURCHASE_FIELDS = ("ref", "occurred_at", "kind", "plan", "amount_pence", "currency", "status", "promo_code",
                   "affiliate_ref", "account_key", "stripe_customer_id")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PURCHASE_STATUSES = ("paid", "refunded", "trialing")
PURCHASE_KINDS = ("subscription", "one_off", "renewal", "reservation", "instruction", "refund")


def touch(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in TOUCH_KEYS:
        value = raw.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            out[key] = str(value).strip()[:200]
    return out


class ProductMetricsCollector(MetricCollector):
    """One product's metrics and purchases. connector_cls is that product's grant connector, whose headers it reuses."""
    tier = "hourly"
    tiers = ("hourly",)
    window_days = 14
    connector_cls: type[BaseConnector]
    METRICS = "/api/partner/grouphq/metrics"
    PURCHASES = "/api/partner/grouphq/purchases"
    DEFAULT_KIND = "subscription"

    def __init__(self, base_url: str, token: str, *, dry_run: bool = False, timeout: float | None = None,
                 connector: BaseConnector | None = None):
        super().__init__(base_url, token, dry_run=dry_run, timeout=timeout)
        self.connector = connector or self.connector_cls(base_url, token, dry_run=dry_run)

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "ProductMetricsCollector":
        connector = cls.connector_cls.from_config(dry_run=dry_run)
        return cls(connector.base_url, connector._token, dry_run=dry_run, connector=connector)

    def missing_env(self) -> list[str]:
        missing = []
        if not self.base_url:
            missing.append(self.env_names[0])
        if not self._token:
            missing.append(self.env_names[1])
        return missing

    def _headers(self) -> dict:
        return self.connector._headers()

    def _plan_headers(self) -> dict:
        return self.connector._plan_headers()

    # ---- metrics -----------------------------------------------------------------------------------------
    def metric_rows(self, body: Any, notes: list, replace: list | None = None) -> list[SnapshotRow]:
        """The days as snapshot rows; replace (when given) receives the windows those days answer for in full."""
        replace = replace if replace is not None else []
        if not isinstance(body, dict):
            raise ConnectorError("bad_response", detail=f"{self.source}: metrics answer is not a JSON object")
        if body.get("ok") is False:
            raise ConnectorError(str(body.get("reason") or "refused")[:80], detail=str(body.get("message") or "")[:300])
        if body.get("product") not in (None, self.product):
            raise ConnectorError("bad_response", detail=f"{self.source}: answered for product {str(body.get('product'))[:20]!r}")
        rows: list[SnapshotRow] = []
        for day in body.get("days") or []:
            if not isinstance(day, dict):
                continue
            when = parse_day(str(day.get("date") or ""))
            if when is None:
                notes.append(f"a day without a readable date was skipped: {str(day.get('date'))[:20]!r}")
                continue
            totals = []
            for metric in DAY_METRICS:
                if metric not in day or day[metric] is None:
                    continue
                value = number(day[metric])
                if value is None or value < 0:
                    notes.append(f"{when} {metric}: {str(day[metric])[:20]!r} is not a count, skipped")
                    continue
                rows.append(SnapshotRow(when, self.product, metric, value))
                totals.append(metric)
            if totals:
                replace.append(Replace(tuple(totals), when, when, ""))
            parts: dict[tuple[str, str], Decimal] = {}
            for key, (metric, kind) in BREAKDOWNS.items():
                if not isinstance(day.get(key), dict):
                    continue
                replace.append(Replace((metric,), when, when, f"{kind}:%"))
                for name, raw in day[key].items():
                    value = number(raw)
                    if value is not None and value >= 0:
                        dim = (metric, f"{kind}:{str(name)[:150]}")
                        parts[dim] = parts.get(dim, Decimal(0)) + value
            promos = day.get("promo_redemptions")
            if isinstance(promos, dict):
                replace.append(Replace(("promo_redemptions",), when, when, "promo:%"))
                for code, raw in promos.items():
                    value = number(raw)
                    if value is not None and value >= 0:
                        dim = ("promo_redemptions", f"promo:{str(code).strip().upper()[:40]}")
                        parts[dim] = parts.get(dim, Decimal(0)) + value      # ppmeta and PPMETA are one code
            for (metric, dimension), value in parts.items():
                rows.append(SnapshotRow(when, self.product, metric, value, dimension))
        return rows

    # ---- purchases ---------------------------------------------------------------------------------------
    def purchase_row(self, item: Any, notes: list) -> PurchaseRow | None:
        if not isinstance(item, dict):
            return None
        ref = str(item.get("ref") or "").strip()
        occurred = parse_instant(str(item.get("occurred_at") or ""))
        if not ref or occurred is None:
            notes.append("a purchase without a ref or a readable occurred_at was skipped")
            return None
        kind = str(item.get("kind") or self.DEFAULT_KIND).strip().lower()
        status = str(item.get("status") or "paid").strip().lower()
        if kind not in PURCHASE_KINDS or status not in PURCHASE_STATUSES:
            notes.append(f"purchase {ref[:40]}: kind {kind[:20]!r} or status {status[:20]!r} is not one Group HQ knows, skipped")
            return None
        amount = number(item.get("amount_pence"))
        if amount is None or amount != amount.to_integral_value():
            notes.append(f"purchase {ref[:40]}: amount_pence is not a whole number, skipped")
            return None
        account_key = str(item.get("account_key") or "").strip().lower()
        if account_key and not SHA256.match(account_key):
            notes.append(f"purchase {ref[:40]}: account_key is not a sha256 digest and was dropped")
            account_key = ""
        raw = {k: item.get(k) for k in PURCHASE_FIELDS if k in item and k != "account_key"}
        return PurchaseRow(
            product=self.product, purchase_ref=ref[:120], occurred_at=occurred, kind=kind,
            plan=str(item.get("plan") or "")[:80], amount_pence=int(amount),
            currency=str(item.get("currency") or "gbp")[:3].lower(), status=status,
            promo_code=str(item.get("promo_code") or "").strip().upper()[:40],
            affiliate_ref=str(item.get("affiliate_ref") or "").strip()[:64],
            first_touch=touch(item.get("first_touch")), last_touch=touch(item.get("last_touch")),
            account_key=account_key, stripe_customer_id=str(item.get("stripe_customer_id") or "")[:60], raw=raw)

    def pull(self, since: date, until: date, *, tier: str = "hourly") -> PullResult:
        notes: list = []
        rows: list = []
        body = self.get_json(self.METRICS, params={"from": since.isoformat(), "to": until.isoformat()})
        replace: list = []
        if body is not None:
            rows.extend(self.metric_rows(body, notes, replace))
        cursor = ""
        for page in range(MAX_PAGES):
            params = {"since": iso_utc(since)}
            if cursor:
                params["cursor"] = cursor
            body = self.get_json(self.PURCHASES, params=params)
            if body is None:
                break
            if not isinstance(body, dict) or body.get("ok") is False:
                raise ConnectorError("bad_response", detail=f"{self.source}: purchases answer was not ok")
            items = body.get("purchases")
            if items is None:
                items = body.get("instructions")
            for item in items or []:
                row = self.purchase_row(item, notes)
                if row is not None:
                    rows.append(row)
            cursor = str(body.get("next_cursor") or "")
            if not cursor:
                break
        else:
            notes.append(f"purchases: stopped after {MAX_PAGES} pages")
        return self.result(rows, notes, replace)

    def probe(self) -> ConnectorResult:
        today = today_london().isoformat()
        result = self.request("GET", self.METRICS, params={"from": today, "to": today})
        if not result.dry_run:
            rows = self.metric_rows(result.data, [])
            result.data = {"ok": True, "values": len(rows)}
        return result


def _collector(product: str, metrics: str, purchases: str, kind: str = "subscription") -> type[ProductMetricsCollector]:
    connector_cls = CONNECTORS[product]
    token_env = {"pp": "PP_SERVICE_TOKEN", "ordris": "ORDRIS_PARTNER_TOKEN", "kp": "KP_PARTNER_TOKEN",
                 "hnh": "HNH_PARTNER_TOKEN"}[product]
    return type(f"Product{product.upper()}Collector", (ProductMetricsCollector,), {
        "__doc__": f"{product}: GET {metrics} and GET {purchases} with {connector_cls.__name__}'s headers.",
        "source": f"product_{product}", "product": product, "connector_cls": connector_cls,
        "env_names": (f"{product.upper()}_BASE_URL", token_env), "METRICS": metrics, "PURCHASES": purchases,
        "DEFAULT_KIND": kind,
    })


ProductPPCollector = _collector("pp", "/api/partner/grouphq/metrics", "/api/partner/grouphq/purchases")
ProductOrdrisCollector = _collector("ordris", "/api/partner/grouphq/metrics", "/api/partner/grouphq/purchases")
ProductKPCollector = _collector("kp", "/api/partner/metrics", "/api/partner/purchases", "reservation")
ProductHNHCollector = _collector("hnh", "/api/partner/metrics", "/api/partner/instructions", "instruction")

