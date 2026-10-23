"""Stripe, read with a restricted key: Read on Subscriptions, Checkout Sessions, Balance transactions (Balance),
Promotion codes, Coupons, Products and Prices; None on everything else (Customers, Invoices, Charges, Refunds and
Events included: the collector never calls them, and Events would carry customer emails). Checkout Sessions read still
returns each session's customer_details (the buyer's email); the collector drops it on read and never stores it.

Every request pins Stripe-Version: STRIPE_VERSION, so the shapes below hold whatever the account's default version is
(from 2025-09-30.clover a Discount carries its coupon under source.coupon, unexpanded unless asked; later clover
versions are additive). One live account per company: MH196 2026 Ltd (Property Predator, Ordris, Key Properties) on
STRIPE_RK_MH196, Hello New Home 2 Ltd on STRIPE_RK_HNH (no account yet: not configured until it exists). Calls, all GET
on api.stripe.com:

    /v1/subscriptions?status=all&limit=100&expand[]=data.discounts.source.coupon     every subscription, cursor paged
    /v1/coupons/{id}                                     only for a discount whose coupon still came back as an id
    /v1/checkout/sessions?created[gte]=<since>&limit=100                  completed sessions in the window
    /v1/balance_transactions?created[gte]=<since>&limit=100               money in and refunds by day
    /v1/promotion_codes?active=true&limit=100                             times_redeemed per Stripe-native code

Rows, per product (subscription metadata.product, else the price's metadata.product, else STRIPE_PRODUCT_MAP by price
or product id, else the account's default: group for MH196, hnh for HNH):
- today: paying_active (active + past_due), trials_active (trialing), mrr_pence (active + past_due, every licensed
  item's unit_amount x quantity normalised to a month, net of a percent or amount discount; labelled net_of_discounts);
  one snapshot a day, so churned MRR is a diff of two days;
- each day of the window: paying_new (the day a subscription started paying: its trial end, else its start; never for
  one that has not paid: trialing, incomplete, incomplete_expired, paused (a trial that ended without a payment
  method), or cancelled for a failed payment before its first billing period could have ended), cancelled (the day it
  ended), purchases (completed Checkout Sessions, also by promo:<code> and campaign:<utm_campaign> from the session
  metadata), revenue_pence and refunds_pence from balance transactions (gross, GBP, account-wide);
- promo_times_redeemed per promo:<CODE> for Stripe promotion codes (cumulative).
Every subscription, session and balance transaction of the window is read, so the pull answers for the whole window:
its rows replace the window's earlier rows (a product remapped in STRIPE_PRODUCT_MAP leaves no stale row behind).
Only GBP is counted.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterator
from urllib.parse import quote

from .... import config
from ....models import PRODUCTS
from ...connectors.base import ConnectorError, ConnectorResult
from .base import (MAX_PAGES, MetricCollector, PullResult, Replace, SnapshotRow, days_between, london_date, number,
                   today_london)

STRIPE_VERSION = "2025-09-30.clover"
PAYING = ("active", "past_due")
LEVELS = ("paying_active", "trials_active", "mrr_pence", "promo_times_redeemed")          # written on today only
WINDOW_METRICS = ("paying_new", "cancelled", "purchases", "revenue_pence", "refunds_pence")  # every day of the window
NEVER_PAID = ("trialing", "incomplete", "incomplete_expired", "paused")
INTERVAL_SECONDS = {"day": 86400, "week": 7 * 86400, "month": 28 * 86400, "year": 365 * 86400}   # a billing period, at
# its shortest (a month is at least 28 days), so "ended inside the first period" never wrongly drops a payer


def monthly_pence(unit_amount: Any, quantity: Any, interval: str, interval_count: Any = 1) -> Decimal | None:
    """One subscription item's amount as a monthly figure in pence: month / count, year / 12 / count,
    week x 52 / 12 / count, day x 365 / 12 / count. None when the price has no fixed amount."""
    amount, qty, count = number(unit_amount), number(quantity if quantity is not None else 1), number(interval_count or 1)
    if amount is None or qty is None or count is None or count <= 0:
        return None
    total = amount * qty
    if interval == "month":
        return total / count
    if interval == "year":
        return total / 12 / count
    if interval == "week":
        return total * 52 / 12 / count
    if interval == "day":
        return total * 365 / 12 / count
    return None


def coupon_ref(discount: Any) -> dict | str | None:
    """A discount's coupon as Stripe gave it: source.coupon (2025-09-30.clover onwards), else the older top-level
    coupon; a dict when expanded, an id string when not."""
    if not isinstance(discount, dict):
        return None
    source = discount.get("source")
    if isinstance(source, dict) and source.get("coupon"):
        return source["coupon"]
    return discount.get("coupon") or None


def _coupon(discount: Any, coupons: dict | None = None) -> dict:
    ref = coupon_ref(discount)
    if isinstance(ref, dict):
        return ref
    if isinstance(ref, str):
        return (coupons or {}).get(ref) or {}
    return {}


def _active_discounts(sub: dict, now: int) -> list[dict]:
    raw = []
    if isinstance(sub.get("discounts"), list):
        raw.extend(d for d in sub["discounts"] if isinstance(d, dict))
    if isinstance(sub.get("discount"), dict) and not raw:
        raw.append(sub["discount"])
    return [d for d in raw if not (isinstance(d.get("end"), int) and d["end"] <= now)]


def subscription_mrr(sub: dict, now: int, notes: list | None = None, coupons: dict | None = None) -> Decimal:
    """The subscription's monthly amount in pence, net of discounts. Metered and non-GBP items are left out. coupons
    maps a coupon id to the coupon, for a discount whose coupon came back unexpanded."""
    notes = notes if notes is not None else []
    total = Decimal(0)
    interval, interval_count = "month", 1
    for item in ((sub.get("items") or {}).get("data") or []):
        price = item.get("price") or item.get("plan") or {}
        recurring = price.get("recurring") or {}
        if not recurring and price.get("interval"):          # a legacy plan object
            recurring = {"interval": price.get("interval"), "interval_count": price.get("interval_count"),
                         "usage_type": price.get("usage_type")}
        if (recurring.get("usage_type") or "licensed") != "licensed":
            continue
        if (price.get("currency") or "gbp").lower() != "gbp":
            notes.append(f"subscription {str(sub.get('id'))[:40]}: a {str(price.get('currency'))[:3]} price is left out of MRR")
            continue
        monthly = monthly_pence(price.get("unit_amount", price.get("amount")), item.get("quantity"),
                                str(recurring.get("interval") or ""), recurring.get("interval_count"))
        if monthly is None:
            notes.append(f"subscription {str(sub.get('id'))[:40]}: an item without a fixed amount is left out of MRR")
            continue
        total += monthly
        interval, interval_count = str(recurring.get("interval")), recurring.get("interval_count") or 1
    for discount in _active_discounts(sub, now):
        coupon = _coupon(discount, coupons)
        percent = number(coupon.get("percent_off"))
        amount_off = number(coupon.get("amount_off"))
        if percent is not None:
            total = total * (Decimal(100) - percent) / Decimal(100)
        elif amount_off is not None and (coupon.get("currency") or "gbp").lower() == "gbp":
            total -= monthly_pence(amount_off, 1, interval, interval_count) or Decimal(0)
        elif discount:
            notes.append(f"subscription {str(sub.get('id'))[:40]}: a discount without its coupon was not applied")
    return max(total, Decimal(0))


def billing_seconds(sub: dict) -> int | None:
    """The subscription's billing period at its shortest, in seconds (from its first licensed item), or None."""
    for item in ((sub.get("items") or {}).get("data") or []):
        price = item.get("price") or item.get("plan") or {}
        recurring = price.get("recurring") or ({"interval": price.get("interval"),
                                                 "interval_count": price.get("interval_count")} if price.get("interval") else {})
        unit = INTERVAL_SECONDS.get(str(recurring.get("interval") or ""))
        count = number(recurring.get("interval_count") or 1)
        if unit and count and count > 0:
            return int(unit * count)
    return None


def paid_from(sub: dict, now: int) -> int | None:
    """When the subscription started paying (unix seconds), or None when it has not paid. Its trial end, else its
    start; never for a status that has not paid, nor for one cancelled for a failed payment before its first billing
    period after that point could have ended (the failed payment was the first one; Invoices are not read)."""
    status = str(sub.get("status") or "")
    if status in NEVER_PAID:
        return None
    trial_end = sub.get("trial_end") if isinstance(sub.get("trial_end"), int) else None
    start = trial_end if trial_end else sub.get("start_date")
    if not isinstance(start, int) or start > now:
        return None
    ended = sub.get("ended_at") or (sub.get("canceled_at") if status == "canceled" else None)
    if isinstance(ended, int) and ended <= start:
        return None
    reason = str(((sub.get("cancellation_details") or {}).get("reason")) or "")
    period = billing_seconds(sub)
    if status == "canceled" and reason == "payment_failed" and isinstance(ended, int) and period and ended - start < period:
        return None
    return start


class StripeCollector(MetricCollector):
    """One Stripe account. Subclasses name the source, the key's env name and the account's default product."""
    tier = "hourly"
    tiers = ("hourly",)
    window_days = 7
    timeout_seconds = 20.0
    BASE = "https://api.stripe.com"
    key_env = ""
    default_product = "group"

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "StripeCollector":
        return cls(cls.BASE, str(getattr(config, cls.key_env, "") or ""), dry_run=dry_run)

    def __init__(self, *args, product_map: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.product_map = product_map if product_map is not None else config.pairs(config.STRIPE_PRODUCT_MAP)

    def missing_env(self) -> list[str]:
        return [] if self._token else [self.key_env]

    def _headers(self) -> dict:
        return {**super()._headers(), "Stripe-Version": STRIPE_VERSION}

    def _plan_headers(self) -> dict:
        return {**super()._plan_headers(), "Stripe-Version": STRIPE_VERSION}

    def coupons_for(self, subs: list[dict], now: int, notes: list) -> dict[str, dict]:
        """The coupons of live discounts that came back as ids, one GET /v1/coupons/{id} each (normally none: the
        subscription list expands them)."""
        wanted = sorted({ref for sub in subs if str(sub.get("status") or "") in PAYING
                         for ref in (coupon_ref(d) for d in _active_discounts(sub, now)) if isinstance(ref, str)})
        found: dict[str, dict] = {}
        for coupon_id in wanted[:MAX_PAGES]:
            body = self.get_json(f"/v1/coupons/{quote(coupon_id, safe='')}")
            if isinstance(body, dict):
                found[coupon_id] = body
        if len(wanted) > MAX_PAGES:
            notes.append(f"{len(wanted) - MAX_PAGES} coupons were not looked up (over {MAX_PAGES})")
        return found

    # ---- paging ------------------------------------------------------------------------------------------
    def list_all(self, path: str, params: dict, notes: list) -> Iterator[dict]:
        cursor = ""
        for _ in range(MAX_PAGES):
            page = dict(params)
            if cursor:
                page["starting_after"] = cursor
            body = self.get_json(path, params=page)
            if body is None:
                return
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list):
                raise ConnectorError("bad_response", detail=f"{self.source}: {path} did not answer a list")
            for item in data:
                if isinstance(item, dict):
                    yield item
            if not body.get("has_more") or not data:
                return
            cursor = str(data[-1].get("id") or "")
            if not cursor:
                return
        notes.append(f"{path}: stopped after {MAX_PAGES} pages")

    # ---- which product -----------------------------------------------------------------------------------
    def product_of(self, metadata: Any = None, price: Any = None) -> str:
        for source in (metadata, (price or {}).get("metadata") if isinstance(price, dict) else None):
            if isinstance(source, dict) and str(source.get("product") or "").strip().lower() in PRODUCTS:
                return str(source["product"]).strip().lower()
        if isinstance(price, dict):
            for key in (price.get("id"), price.get("product") if isinstance(price.get("product"), str) else None):
                mapped = self.product_map.get(str(key or ""), "").lower()
                if mapped in PRODUCTS:
                    return mapped
        return self.default_product

    def subscription_product(self, sub: dict) -> str:
        items = ((sub.get("items") or {}).get("data") or [])
        price = (items[0].get("price") or items[0].get("plan")) if items else None
        return self.product_of(sub.get("metadata"), price)

    # ---- the pull ----------------------------------------------------------------------------------------
    def pull(self, since: date, until: date, *, tier: str = "hourly") -> PullResult:
        notes: list = []
        now = int(datetime.now(timezone.utc).timestamp())
        today = today_london()
        start = int(calendar.timegm(since.timetuple())) - 86400     # a day early: London midnight is before UTC's in summer
        window = set(days_between(since, until))
        products = {self.default_product}
        state: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        events: dict[tuple, Decimal] = defaultdict(Decimal)          # (date, product, metric, dimension) -> value

        subs = list(self.list_all("/v1/subscriptions",
                                  {"status": "all", "limit": 100, "expand[]": "data.discounts.source.coupon"}, notes))
        coupons = self.coupons_for(subs, now, notes)
        for sub in subs:
            product = self.subscription_product(sub)
            products.add(product)
            status = str(sub.get("status") or "")
            if status in PAYING:
                state[product]["paying_active"] += 1
                state[product]["mrr_pence"] += subscription_mrr(sub, now, notes, coupons)
            elif status == "trialing":
                state[product]["trials_active"] += 1
            started = paid_from(sub, now)
            if started:
                day = london_date(started)
                if day in window:
                    events[(day, product, "paying_new", "")] += 1
            ended = sub.get("ended_at") or (sub.get("canceled_at") if status == "canceled" else None)
            if status == "canceled" and ended:
                day = london_date(ended)
                if day in window:
                    events[(day, product, "cancelled", "")] += 1

        for session in self.list_all("/v1/checkout/sessions", {"created[gte]": start, "limit": 100}, notes):
            session.pop("customer_details", None)
            session.pop("customer_email", None)
            if session.get("status") != "complete" or session.get("payment_status") not in ("paid", "no_payment_required"):
                continue
            day = london_date(session.get("created"))
            if day not in window:
                continue
            metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
            product = self.product_of(metadata)
            products.add(product)
            events[(day, product, "purchases", "")] += 1
            code = str(metadata.get("promo_code") or "").strip().upper()[:40]
            if code:
                events[(day, product, "purchases", f"promo:{code}")] += 1
            campaign = str(metadata.get("utm_campaign") or "").strip()[:150]
            if campaign:
                events[(day, product, "purchases", f"campaign:{campaign}")] += 1

        for txn in self.list_all("/v1/balance_transactions", {"created[gte]": start, "limit": 100}, notes):
            if (txn.get("currency") or "").lower() != "gbp":
                notes.append(f"balance transaction in {str(txn.get('currency'))[:3]} left out (only GBP is counted)")
                continue
            day = london_date(txn.get("created"))
            amount = number(txn.get("amount")) or Decimal(0)
            if day not in window:
                continue
            kind = str(txn.get("type") or "")
            if kind in ("charge", "payment"):
                events[(day, self.default_product, "revenue_pence", "")] += amount
            elif kind in ("refund", "payment_refund"):
                events[(day, self.default_product, "refunds_pence", "")] += abs(amount)

        rows: list = []
        redeemed_by: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        channel_of: dict[tuple[str, str], str] = {}
        for code in self.list_all("/v1/promotion_codes", {"active": "true", "limit": 100}, notes):
            name = str(code.get("code") or "").strip().upper()[:40]
            redeemed = number(code.get("times_redeemed"))
            if name and redeemed is not None:
                key = (self.product_of(code.get("metadata")), name)
                redeemed_by[key] += redeemed            # one code in two cases is one row, added up
                channel_of.setdefault(key, str((code.get("metadata") or {}).get("channel") or "")[:40])
        for (product, name), redeemed in sorted(redeemed_by.items()):
            rows.append(SnapshotRow(today, product, "promo_times_redeemed", redeemed, f"promo:{name}",
                                    {"channel": channel_of[(product, name)]}))

        for product in sorted(products):
            for metric in ("paying_active", "trials_active"):
                rows.append(SnapshotRow(today, product, metric, state[product][metric]))
            mrr = state[product]["mrr_pence"].quantize(Decimal(1), rounding=ROUND_HALF_UP)
            rows.append(SnapshotRow(today, product, "mrr_pence", mrr, meta={"basis": "net_of_discounts"}))
            for day in sorted(window):
                for metric in ("paying_new", "cancelled", "purchases"):
                    events.setdefault((day, product, metric, ""), Decimal(0))
        for day in sorted(window):
            for metric in ("revenue_pence", "refunds_pence"):
                events.setdefault((day, self.default_product, metric, ""), Decimal(0))
        for (day, product, metric, dimension), value in sorted(events.items(), key=lambda kv: (kv[0][0], kv[0][1:])):
            meta = {"basis": "gross"} if metric in ("revenue_pence", "refunds_pence") else {}
            rows.append(SnapshotRow(day, product, metric, value, dimension, meta))
        replace = [Replace(LEVELS, today, today)]
        if window:
            replace.append(Replace(WINDOW_METRICS, min(window), max(window)))
        return self.result(rows, notes, replace)

    def probe(self) -> ConnectorResult:
        result = self.request("GET", "/v1/subscriptions", params={"limit": 1})
        if not result.dry_run:              # never keep a subscription (customer ids, metadata) from a test call
            result.data = {"ok": True, "subscriptions_listed": len((result.data or {}).get("data") or [])
                           if isinstance(result.data, dict) else 0}
        return result


class StripeMH196Collector(StripeCollector):
    """MH196 2026 Ltd's account: Property Predator, Ordris and Key Properties, split by product."""
    source = "stripe_mh196"
    key_env = "STRIPE_RK_MH196"
    env_names = ("STRIPE_RK_MH196", "STRIPE_PRODUCT_MAP")
    default_product = "group"

    def missing_env(self) -> list[str]:
        return [] if self._token else ["STRIPE_RK_MH196"]


class StripeHNHCollector(StripeCollector):
    """Hello New Home 2 Ltd's account (none exists yet: not configured until it does)."""
    source = "stripe_hnh"
    key_env = "STRIPE_RK_HNH"
    env_names = ("STRIPE_RK_HNH",)
    default_product = "hnh"
