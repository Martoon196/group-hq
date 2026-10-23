"""The queries the Growth screens read: pure functions over the Growth tables, tested without HTTP.

Every screen reads Postgres only; no page view ever calls a platform. A period is today, the last 7 days or the last
30 days, ending today (Europe/London); the product filter is pp, kp, ordris, hnh or all. Which source a figure comes
from is fixed here, once:
- the products' own ledgers (product_*) for sign-ups, leads, trials, paying, MRR and purchases; for Ordris's sign-ups,
  Clerk (clerk_ordris, the stopgap) on the days the Ordris ledger has written none, so the two never add up;
- GA4 for visitors and the pixel's view (sign_ups_pixel, purchases_pixel), shown beside the ledger, never instead;
- Stripe for its own MRR, shown in the MRR tile's note;
- ad_spend for spend, whichever collector or CSV wrote it;
- sales_attribution (the view) for the money side of every channel.
Search Console's ctr and position are recomputed from clicks and impressions (position weighted by impressions),
never averaged. Neon usage is priced with the founder's rates (Settings); a unit without a rate is shown unpriced,
and each project is named from the current NEON_PROJECT_PRODUCTS at read time. Purchases (Channels, the promo note)
are counted on their London day, paid ones only, like every snapshot.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from ... import config
from ...models import AdSpend, AuditLog, MetricRun, MetricSnapshot, MetricSource, ProductPurchase, PromoChannelMap
from ..settings import product_name
from .collectors import COLLECTORS, SOURCE_INFO, env_names, source_mode
from .collectors.base import today_london
from .collectors.neon import labels as neon_labels
from .registry import NEON_UNITS

PRODUCT_ORDER = ("pp", "kp", "ordris", "hnh")
PERIODS = {"today": 1, "7d": 7, "30d": 30}
PERIOD_LABELS = {"today": "Today", "7d": "Last 7 days", "30d": "Last 30 days"}
LEDGER = ("product_pp", "product_kp", "product_ordris", "product_hnh")
CLERK = "clerk_ordris"            # Ordris sign-ups until the Ordris ledger answers (filled in, never added)
LONDON_DAY = "((occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London')::date"
STRIPE = ("stripe_mh196", "stripe_hnh")
ZERO = Decimal(0)
SPARK_DAYS = 30
SETTLING = {"ga4": "GA4 figures settle for 48 hours", "search_console": "Google supplies Search Console figures 3 days late",
            "meta_ads": "Meta restates 28 days", "google_ads": "Google Ads restates 30 days"}
KNOWN_LABELS = {"grouphq": "Group HQ"}


@dataclass
class Period:
    key: str
    start: date
    end: date
    prev_start: date
    prev_end: date

    @property
    def label(self) -> str:
        return PERIOD_LABELS[self.key]

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


def period_of(key: str | None, today: date | None = None) -> Period:
    key = key if key in PERIODS else "7d"
    today = today or today_london()
    length = PERIODS[key]
    start = today - timedelta(days=length - 1)
    return Period(key, start, today, start - timedelta(days=length), start - timedelta(days=1))


def products_of(product: str | None) -> tuple[str, ...]:
    """The product filter as the product codes it covers ("all" includes group-wide rows)."""
    return (product,) if product in PRODUCT_ORDER else PRODUCT_ORDER + ("group",)


def product_filter(product: str | None) -> str:
    return product if product in PRODUCT_ORDER else "all"


# ---- formatting (the screens show these strings; the numbers stay Decimal underneath) -------------------------------
def gbp(pence) -> str:
    if pence is None:
        return "—"
    pounds = (Decimal(pence) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return f"£{pounds:,}"


def count(value) -> str:
    if value is None:
        return "—"
    value = Decimal(value)
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):,}"


def pct(value) -> str:
    if value is None:
        return "—"
    return f"{(Decimal(value) * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def ratio(numerator, denominator) -> Decimal | None:
    if not numerator or not denominator:
        return None
    return Decimal(numerator) / Decimal(denominator)


def dollars(value) -> str:
    if value is None:
        return "—"
    return f"${Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"


# ---- snapshot helpers ----------------------------------------------------------------------------------------------
def sums(db: Session, sources: tuple[str, ...], metric: str, start: date, end: date, products: tuple[str, ...],
         dimension: str = "") -> dict[str, Decimal]:
    """{product: sum of the metric over the days} for the given sources and dimension."""
    rows = db.execute(
        select(MetricSnapshot.product, func.sum(MetricSnapshot.value))
        .where(MetricSnapshot.source.in_(sources), MetricSnapshot.metric == metric,
               MetricSnapshot.dimension == dimension, MetricSnapshot.product.in_(products),
               MetricSnapshot.date >= start, MetricSnapshot.date <= end)
        .group_by(MetricSnapshot.product)).all()
    return {product: Decimal(value or 0) for product, value in rows}


def latest(db: Session, sources: tuple[str, ...], metric: str, end: date, products: tuple[str, ...],
           dimension: str = "") -> dict[str, Decimal]:
    """{product: the metric on its source's latest day at or before end} (a level, such as paying_active or MRR). The
    latest day is the source's, not the product's: a product the source no longer reports on its latest day (a Stripe
    price remapped to another product) shows nothing rather than its last, stale level beside its replacement. Two
    sources for one product (two Stripe accounts) add up on their own latest days."""
    newest = (select(MetricSnapshot.source, func.max(MetricSnapshot.date).label("d"))
              .where(MetricSnapshot.source.in_(sources), MetricSnapshot.metric == metric,
                     MetricSnapshot.dimension == dimension, MetricSnapshot.date <= end)
              .group_by(MetricSnapshot.source).subquery())
    rows = db.execute(
        select(MetricSnapshot.product, func.sum(MetricSnapshot.value))
        .join(newest, and_(newest.c.source == MetricSnapshot.source, newest.c.d == MetricSnapshot.date))
        .where(MetricSnapshot.metric == metric, MetricSnapshot.dimension == dimension,
               MetricSnapshot.product.in_(products))
        .group_by(MetricSnapshot.product)).all()
    return {product: Decimal(value or 0) for product, value in rows}


def series(db: Session, sources: tuple[str, ...], metric: str, start: date, end: date, products: tuple[str, ...],
           dimension: str = "") -> dict[str, list[Decimal]]:
    """{product: one value per day from start to end, 0 where nothing was written}."""
    rows = db.execute(
        select(MetricSnapshot.product, MetricSnapshot.date, func.sum(MetricSnapshot.value))
        .where(MetricSnapshot.source.in_(sources), MetricSnapshot.metric == metric,
               MetricSnapshot.dimension == dimension, MetricSnapshot.product.in_(products),
               MetricSnapshot.date >= start, MetricSnapshot.date <= end)
        .group_by(MetricSnapshot.product, MetricSnapshot.date)).all()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    found: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for product, day, value in rows:
        found[product][day] = Decimal(value or 0)
    return {product: [found[product].get(d, ZERO) for d in days] for product in products}


def total(values: dict[str, Decimal]) -> Decimal:
    return sum(values.values(), ZERO)


def spend_by_product(db: Session, start: date, end: date, products: tuple[str, ...]) -> dict[str, Decimal]:
    rows = db.execute(select(AdSpend.product, func.sum(AdSpend.spend_pence))
                      .where(AdSpend.date >= start, AdSpend.date <= end, AdSpend.product.in_(products))
                      .group_by(AdSpend.product)).all()
    return {product: Decimal(value or 0) for product, value in rows}


def clerk_fill(db: Session, start: date, end: date, products: tuple[str, ...]) -> dict[date, Decimal]:
    """Ordris sign-ups from Clerk on the days the Ordris ledger wrote no sign_ups total: {day: value}."""
    if "ordris" not in products:
        return {}
    ledger_days = select(MetricSnapshot.date).where(
        MetricSnapshot.source == "product_ordris", MetricSnapshot.metric == "sign_ups", MetricSnapshot.dimension == "",
        MetricSnapshot.product == "ordris", MetricSnapshot.date >= start, MetricSnapshot.date <= end)
    rows = db.execute(
        select(MetricSnapshot.date, MetricSnapshot.value)
        .where(MetricSnapshot.source == CLERK, MetricSnapshot.metric == "sign_ups", MetricSnapshot.dimension == "",
               MetricSnapshot.product == "ordris", MetricSnapshot.date >= start, MetricSnapshot.date <= end,
               MetricSnapshot.date.not_in(ledger_days))).all()
    return {day: Decimal(value or 0) for day, value in rows}


def sign_up_sums(db: Session, start: date, end: date, products: tuple[str, ...]) -> tuple[dict[str, Decimal], int]:
    """{product: sign-ups over the days}: the ledgers, with Clerk's Ordris count on the days the Ordris ledger is
    silent. Also returns how many days Clerk filled in."""
    values = sums(db, LEDGER, "sign_ups", start, end, products)
    filled = clerk_fill(db, start, end, products)
    if filled:
        values["ordris"] = values.get("ordris", ZERO) + sum(filled.values(), ZERO)
    return values, len(filled)


def sign_up_series(db: Session, start: date, end: date, products: tuple[str, ...]) -> dict[str, list[Decimal]]:
    out = series(db, LEDGER, "sign_ups", start, end, products)
    filled = clerk_fill(db, start, end, products)
    if filled and "ordris" in out:
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        out["ordris"] = [value + filled.get(day, ZERO) for value, day in zip(out["ordris"], days)]
    return out


def promo_purchases(db: Session, start: date, end: date, products: tuple[str, ...]) -> dict[str, int]:
    """{product: paid purchases with a promo code}, each on its London day."""
    rows = db.execute(text(
        "SELECT product, count(*) FROM product_purchases WHERE promo_code <> '' AND status = 'paid' "
        f"AND product = ANY(:products) AND {LONDON_DAY} BETWEEN :start AND :end GROUP BY product"),
        {"start": start, "end": end, "products": list(products)}).all()
    return {product: int(n) for product, n in rows}


def health(db: Session) -> dict[str, MetricSource]:
    return {row.source: row for row in db.query(MetricSource).all()}


# ---- Overview ------------------------------------------------------------------------------------------------------
def overview(db: Session, period: Period, product: str | None, switches: dict[str, str], rates: dict) -> dict:
    products = products_of(product)
    p = period
    visitors = sums(db, ("ga4",), "visitors", p.start, p.end, products)
    sign_ups, clerk_days = sign_up_sums(db, p.start, p.end, products)
    sign_ups_pixel = sums(db, ("ga4",), "sign_ups_pixel", p.start, p.end, products)
    trials = sums(db, LEDGER, "trials_started", p.start, p.end, products)
    paying = latest(db, LEDGER, "paying_active", p.end, products)
    mrr = latest(db, LEDGER, "mrr_pence", p.end, products)
    stripe_mrr = latest(db, STRIPE, "mrr_pence", p.end, products)
    purchases = sums(db, LEDGER, "purchases", p.start, p.end, products)
    purchases_pixel = sums(db, ("ga4",), "purchases_pixel", p.start, p.end, products)
    spend = spend_by_product(db, p.start, p.end, products)
    promo = promo_purchases(db, p.start, p.end, products)
    spark = sign_up_series(db, p.end - timedelta(days=SPARK_DAYS - 1), p.end, products)
    visitor_spark = series(db, ("ga4",), "visitors", p.end - timedelta(days=SPARK_DAYS - 1), p.end, products)

    def cpa(spend_pence: Decimal, signups: Decimal):
        return (spend_pence / signups) if spend_pence and signups else None

    def shown(values: dict, fmt=count) -> str:
        """A sum, or "—" when its source has written nothing for the period (nothing pulled is not zero)."""
        return fmt(total(values)) if values else "—"

    rows_health = health(db)
    tiles = {
        "visitors": {"value": shown(visitors), "note": "daily visitors summed", "as_of": _as_of(rows_health, "ga4"),
                     "settling": SETTLING["ga4"]},
        "sign_ups": {"value": shown(sign_ups), "note": f"pixel saw {shown(sign_ups_pixel)}"
                                                      + (f"; Ordris from Clerk on {clerk_days} day{'s' if clerk_days != 1 else ''}"
                                                         if clerk_days else ""),
                     "as_of": _as_of(rows_health, *LEDGER, *((CLERK,) if clerk_days else ()))},
        "trials": {"value": shown(trials), "note": "trials started", "as_of": _as_of(rows_health, *LEDGER)},
        "paying": {"value": shown(paying), "note": f"at {p.end:%d %b}", "as_of": _as_of(rows_health, *LEDGER)},
        "mrr": {"value": shown(mrr, gbp), "note": f"Stripe says {shown(stripe_mrr, gbp)}",
                "as_of": _as_of(rows_health, *LEDGER)},
        "spend": {"value": shown(spend, gbp), "note": "all platforms and CSV imports"},
        "cpa": {"value": gbp(cpa(total(spend), total(sign_ups))), "note": "spend ÷ product sign-ups"},
        "purchases": {"value": shown(purchases), "note": f"{sum(promo.values())} with a promo code; pixel saw "
                                                         f"{shown(purchases_pixel)}"},
    }
    table = []
    for code in (product,) if product in PRODUCT_ORDER else PRODUCT_ORDER:
        table.append({
            "product": code, "name": product_name(code),
            "visitors": count(visitors[code]) if code in visitors else "—",
            "sign_ups": count(sign_ups[code]) if code in sign_ups else "—",
            "trials": count(trials[code]) if code in trials else "—",
            "paying": count(paying[code]) if code in paying else "—",
            "mrr": gbp(mrr[code]) if code in mrr else "—", "spend": gbp(spend[code]) if code in spend else "—",
            "cpa": gbp(cpa(spend.get(code, ZERO), sign_ups.get(code, ZERO))),
            "purchases": count(purchases[code]) if code in purchases else "—", "promo": promo.get(code, 0),
            "spark": [float(v) for v in spark.get(code, [])], "visitor_spark": [float(v) for v in visitor_spark.get(code, [])],
        })
    return {"tiles": tiles, "table": table, "freshness": freshness(db, switches),
            "costs": running_costs(db, rates, today_london())}


def _as_of(rows: dict[str, MetricSource], *sources: str):
    stamps = [rows[s].last_ok_at for s in sources if s in rows and rows[s].last_ok_at]
    return max(stamps) if stamps else None


def freshness(db: Session, switches: dict[str, str]) -> list[dict]:
    rows = health(db)
    out = []
    for source in COLLECTORS:
        mode = source_mode(source, switches=switches)
        row = rows.get(source)
        if mode["state"] == "off" and (row is None or row.last_run_at is None):
            continue
        chip = {"live": "ok", "dry_run": "dry_run"}.get(mode["state"], mode["state"])
        if mode["state"] in ("live", "dry_run") and row is not None:
            chip = {"ok": "ok", "failing": "failed", "not_configured": "not_configured"}.get(row.status, "never_run")
        out.append({"source": source, "name": SOURCE_INFO[source]["name"], "chip": chip,
                    "last_ok_at": row.last_ok_at if row else None})
    return out


# ---- Channels ------------------------------------------------------------------------------------------------------
def channels(db: Session, period: Period, product: str | None) -> dict:
    products = products_of(product)
    p = period
    keyed: dict[tuple[str, str, str], dict] = {}

    def row(platform: str, campaign: str, channel: str = "") -> dict:
        # A line is a platform's campaign; a sale with no platform (affiliate, direct) gets a line per channel.
        platform, campaign = platform or "", campaign or ""
        key = (platform, campaign, "" if platform else channel)
        if key not in keyed:
            keyed[key] = {"platform": platform, "campaign": campaign, "channel": channel, "spend": 0, "impressions": 0,
                          "clicks": 0, "pixel_sign_ups": ZERO, "pixel_purchases": ZERO, "sign_ups": ZERO,
                          "money_purchases": 0, "promo_purchases": 0, "revenue": 0}
        if channel and not keyed[key]["channel"]:
            keyed[key]["channel"] = channel
        return keyed[key]

    spend_rows = db.execute(
        select(AdSpend.platform, AdSpend.campaign_name, func.sum(AdSpend.spend_pence), func.sum(AdSpend.impressions),
               func.sum(AdSpend.clicks), func.sum(AdSpend.conversions_sign_up), func.sum(AdSpend.conversions_purchase))
        .where(AdSpend.date >= p.start, AdSpend.date <= p.end, AdSpend.product.in_(products))
        .group_by(AdSpend.platform, AdSpend.campaign_name)).all()
    for platform, campaign, spend, impressions, clicks, sign_ups, purchases in spend_rows:
        r = row(platform, campaign, "paid_search" if platform == "google" else "paid_social")
        r["spend"] += int(spend or 0)
        r["impressions"] += int(impressions or 0)
        r["clicks"] += int(clicks or 0)
        r["pixel_sign_ups"] += Decimal(sign_ups or 0)
        r["pixel_purchases"] += Decimal(purchases or 0)

    # Money says: paid purchases only (a refunded or trialing one is not a sale), each on its London day.
    money = db.execute(text(
        "SELECT platform, campaign_name, channel, count(*), count(*) FILTER (WHERE promo_code <> ''), "
        "coalesce(sum(amount_pence), 0) FROM sales_attribution "
        f"WHERE {LONDON_DAY} BETWEEN :start AND :end AND product = ANY(:products) AND kind <> 'refund' "
        "AND status = 'paid' GROUP BY platform, campaign_name, channel"),
        {"start": p.start, "end": p.end, "products": list(products)}).all()
    for platform, campaign, channel, n, promo_n, revenue in money:
        # A purchase with no spend row keeps its own line, labelled by channel (email, affiliate, partner, direct).
        r = row(platform or "", campaign or "", channel)
        r["money_purchases"] += int(n)
        r["promo_purchases"] += int(promo_n)
        r["revenue"] += int(revenue)

    mapped = {(m.product, m.code): m for m in db.query(PromoChannelMap).all()}
    redemptions = db.execute(
        select(MetricSnapshot.product, MetricSnapshot.dimension, func.sum(MetricSnapshot.value))
        .where(MetricSnapshot.source.in_(LEDGER), MetricSnapshot.metric == "promo_redemptions",
               MetricSnapshot.product.in_(products), MetricSnapshot.date >= p.start, MetricSnapshot.date <= p.end)
        .group_by(MetricSnapshot.product, MetricSnapshot.dimension)).all()
    unmapped: set[tuple[str, str]] = set()
    for code_product, dimension, value in redemptions:
        code = dimension.split(":", 1)[1] if ":" in dimension else dimension
        entry = mapped.get((code_product, code))
        if entry is None:
            unmapped.add((code_product, code))
            continue
        row(entry.platform, entry.campaign_name, entry.channel)["sign_ups"] += Decimal(value or 0)
    for code_product, code in db.execute(
            select(ProductPurchase.product, ProductPurchase.promo_code).distinct()
            .where(ProductPurchase.promo_code != "", ProductPurchase.product.in_(products))).all():
        if (code_product, code) not in mapped:
            unmapped.add((code_product, code))

    out = []
    for r in sorted(keyed.values(), key=lambda r: (-r["spend"], -r["revenue"], r["platform"], r["campaign"])):
        r["ctr"] = pct(ratio(r["clicks"], r["impressions"]))
        r["cpc"] = gbp(r["spend"] / r["clicks"]) if r["clicks"] and r["spend"] else "—"
        r["cpa_pixel"] = gbp(Decimal(r["spend"]) / r["pixel_purchases"]) if r["spend"] and r["pixel_purchases"] else "—"
        r["cpa_money"] = gbp(Decimal(r["spend"]) / r["money_purchases"]) if r["spend"] and r["money_purchases"] else "—"
        r["roas"] = (f"{(Decimal(r['revenue']) / r['spend']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}x"
                     if r["spend"] and r["revenue"] else "—")
        out.append(r)
    return {"rows": out, "unmapped": sorted(unmapped)}


# ---- Email ---------------------------------------------------------------------------------------------------------
def email(db: Session, period: Period, product: str | None) -> dict:
    products = products_of(product)
    p = period
    newest = db.execute(select(func.max(MetricSnapshot.date)).where(
        MetricSnapshot.source == "brevo", MetricSnapshot.metric == "email_contacts", MetricSnapshot.date <= p.end)).scalar()
    lists = []
    if newest:
        for name, code, value in db.execute(
                select(MetricSnapshot.dimension, MetricSnapshot.product, MetricSnapshot.value)
                .where(MetricSnapshot.source == "brevo", MetricSnapshot.metric == "email_contacts",
                       MetricSnapshot.date == newest, MetricSnapshot.product.in_(products))
                .order_by(MetricSnapshot.value.desc())).all():
            lists.append({"name": name.split(":", 1)[-1], "product": code, "contacts": count(value)})
    new_contacts = sums(db, ("brevo",), "email_contacts_new", p.start, p.end, products)

    stats = ("email_sent", "email_delivered", "email_opens_unique", "email_clicks_unique", "email_unsubscribed",
             "email_bounces", "email_spam_reports")
    campaigns: dict[str, dict] = {}
    for dimension, code, day, metric, value in db.execute(
            select(MetricSnapshot.dimension, MetricSnapshot.product, MetricSnapshot.date, MetricSnapshot.metric,
                   MetricSnapshot.value)
            .where(MetricSnapshot.source == "brevo", MetricSnapshot.dimension.like("campaign:%"),
                   MetricSnapshot.metric.in_(stats), MetricSnapshot.product.in_(products),
                   MetricSnapshot.date >= p.start, MetricSnapshot.date <= p.end)).all():
        entry = campaigns.setdefault(dimension, {"name": dimension.split(":", 1)[1], "product": code, "date": day,
                                                 **{m: ZERO for m in stats}})
        entry[metric] += Decimal(value or 0)
    rows = []
    for entry in sorted(campaigns.values(), key=lambda e: e["date"], reverse=True):
        entry["open_rate"] = pct(ratio(entry["email_opens_unique"], entry["email_delivered"]))
        entry["click_rate"] = pct(ratio(entry["email_clicks_unique"], entry["email_delivered"]))
        rows.append(entry)
    week = today_london() - timedelta(days=6)
    transactional = {m: total(sums(db, ("brevo",), m, week, today_london(), PRODUCT_ORDER + ("group",),
                                   "stream:transactional")) for m in stats}
    return {"lists": lists, "lists_as_of": newest, "new_contacts": {k: count(v) for k, v in new_contacts.items()},
            "new_total": count(total(new_contacts)), "campaigns": rows, "transactional": transactional}


# ---- Social --------------------------------------------------------------------------------------------------------
SOCIAL_METRICS = ("posts", "post_impressions", "post_reach", "post_engagements", "post_clicks", "video_views")
SOCIAL_SOURCES = ("zernio", "meta_pages_instagram", "linkedin")


def social(db: Session, period: Period, product: str | None) -> dict:
    products = products_of(product)
    p = period
    per_product = {m: sums(db, SOCIAL_SOURCES, m, p.start, p.end, products) for m in SOCIAL_METRICS}
    by_product = []
    for code in [c for c in PRODUCT_ORDER + ("group",) if c in products]:
        values = {m: per_product[m].get(code) for m in SOCIAL_METRICS}
        if any(v is not None for v in values.values()):
            by_product.append({"product": code, **{m: count(v) for m, v in values.items()}})
    accounts: dict[str, dict] = {}
    spark_start = p.end - timedelta(days=SPARK_DAYS - 1)
    for dimension, code, day, metric, value in db.execute(
            select(MetricSnapshot.dimension, MetricSnapshot.product, MetricSnapshot.date, MetricSnapshot.metric,
                   MetricSnapshot.value)
            .where(MetricSnapshot.source.in_(SOCIAL_SOURCES), MetricSnapshot.dimension.like("account:%"),
                   MetricSnapshot.metric.in_(("followers", "followers_new")), MetricSnapshot.product.in_(products),
                   MetricSnapshot.date >= min(spark_start, p.start), MetricSnapshot.date <= p.end)
            .order_by(MetricSnapshot.date)).all():
        _, platform, handle = (dimension.split(":", 2) + ["", ""])[:3]
        entry = accounts.setdefault(dimension, {"platform": platform, "handle": handle, "product": code,
                                                "followers": None, "new": ZERO, "spark": {}})
        if metric == "followers":
            entry["followers"] = Decimal(value)
            if day >= spark_start:
                entry["spark"][day] = float(value)
        elif p.start <= day <= p.end:
            entry["new"] += Decimal(value or 0)
    account_rows = []
    for entry in sorted(accounts.values(), key=lambda e: (e["platform"], e["handle"])):
        account_rows.append({**entry, "followers": count(entry["followers"]), "new": count(entry["new"]),
                             "spark": [entry["spark"][d] for d in sorted(entry["spark"])]})
    posts: dict[str, dict] = {}
    for dimension, code, day, metric, value, meta in db.execute(
            select(MetricSnapshot.dimension, MetricSnapshot.product, MetricSnapshot.date, MetricSnapshot.metric,
                   MetricSnapshot.value, MetricSnapshot.meta)
            .where(MetricSnapshot.source.in_(SOCIAL_SOURCES), MetricSnapshot.dimension.like("post:%"),
                   MetricSnapshot.product.in_(products), MetricSnapshot.date >= p.start, MetricSnapshot.date <= p.end)).all():
        entry = posts.setdefault(dimension, {"id": dimension.split(":")[-1], "product": code, "date": day,
                                             "url": (meta or {}).get("url", ""), "platform": (meta or {}).get("platform", ""),
                                             "external": bool((meta or {}).get("external")), **{m: ZERO for m in SOCIAL_METRICS}})
        entry[metric] = Decimal(value or 0)
    top = sorted(posts.values(), key=lambda e: e["post_engagements"], reverse=True)[:10]
    return {"by_product": by_product, "accounts": account_rows,
            "top_posts": [{**e, **{m: count(e[m]) for m in SOCIAL_METRICS}} for e in top],
            "external": any(e["external"] for e in posts.values())}


# ---- Search --------------------------------------------------------------------------------------------------------
def _search_rollup(db: Session, products: tuple[str, ...], start: date, end: date, like: str | None):
    """{(product, dimension): {clicks, impressions, ctr, position}} with ctr and position recomputed."""
    dimension_filter = MetricSnapshot.dimension.like(like) if like else MetricSnapshot.dimension == ""
    raw: dict[tuple, dict] = defaultdict(lambda: {"clicks": ZERO, "impressions": ZERO, "weighted": ZERO})
    clicks = dict(((r[0], r[1], r[2]), Decimal(r[3])) for r in db.execute(
        select(MetricSnapshot.product, MetricSnapshot.dimension, MetricSnapshot.date, MetricSnapshot.value)
        .where(MetricSnapshot.source == "search_console", MetricSnapshot.metric == "search_clicks", dimension_filter,
               MetricSnapshot.product.in_(products), MetricSnapshot.date >= start, MetricSnapshot.date <= end)).all())
    impressions = dict(((r[0], r[1], r[2]), Decimal(r[3])) for r in db.execute(
        select(MetricSnapshot.product, MetricSnapshot.dimension, MetricSnapshot.date, MetricSnapshot.value)
        .where(MetricSnapshot.source == "search_console", MetricSnapshot.metric == "search_impressions", dimension_filter,
               MetricSnapshot.product.in_(products), MetricSnapshot.date >= start, MetricSnapshot.date <= end)).all())
    positions = dict(((r[0], r[1], r[2]), Decimal(r[3])) for r in db.execute(
        select(MetricSnapshot.product, MetricSnapshot.dimension, MetricSnapshot.date, MetricSnapshot.value)
        .where(MetricSnapshot.source == "search_console", MetricSnapshot.metric == "search_position", dimension_filter,
               MetricSnapshot.product.in_(products), MetricSnapshot.date >= start, MetricSnapshot.date <= end)).all())
    for key, value in clicks.items():
        raw[key[:2]]["clicks"] += value
    for key, value in impressions.items():
        raw[key[:2]]["impressions"] += value
        raw[key[:2]]["weighted"] += value * positions.get(key, ZERO)
    out = {}
    for key, r in raw.items():
        out[key] = {"clicks": r["clicks"], "impressions": r["impressions"],
                    "ctr": ratio(r["clicks"], r["impressions"]),
                    "position": (r["weighted"] / r["impressions"]) if r["impressions"] else None}
    return out


def _fmt_search(r: dict | None) -> dict:
    r = r or {}
    position = r.get("position")
    return {"clicks": count(r.get("clicks", ZERO)), "impressions": count(r.get("impressions", ZERO)),
            "ctr": pct(r.get("ctr")),
            "position": f"{position.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}" if position is not None else "—"}


def search(db: Session, period: Period, product: str | None) -> dict:
    products = tuple(c for c in products_of(product) if c != "group")
    p = period
    now, before = (_search_rollup(db, products, p.start, p.end, None), _search_rollup(db, products, p.prev_start, p.prev_end, None))
    sites = []
    for code in products:
        sites.append({"product": code, "name": product_name(code), "now": _fmt_search(now.get((code, ""))),
                      "before": _fmt_search(before.get((code, "")))})

    def top(like: str) -> list[dict]:
        rolled = _search_rollup(db, products, p.start, p.end, like)
        ranked = sorted(rolled.items(), key=lambda kv: (-kv[1]["clicks"], -kv[1]["impressions"], kv[0][1]))[:20]
        return [{"product": k[0], "value": k[1].split(":", 1)[1], **_fmt_search(v)} for k, v in ranked]

    return {"sites": sites, "queries": top("query:%"), "pages": top("page:%"), "devices": top("device:%")}


# ---- Running costs (Neon) ------------------------------------------------------------------------------------------
def neon_label(project_id: str, project_name: str, labels: dict[str, str] | None = None) -> str:
    """A Neon project's name on the screens, from the CURRENT NEON_PROJECT_PRODUCTS (never the label stored with an
    old row), else its Neon name, else its id."""
    labels = neon_labels(config.NEON_PROJECT_PRODUCTS) if labels is None else labels
    return display_label(labels.get(project_id, ""), project_name, project_id)


def display_label(label: str, project_name: str, project_id: str) -> str:
    """pp -> Property Predator, grouphq -> Group HQ, any other label as typed, no label -> the Neon project's name."""
    label = (label or "").strip()
    if label.lower() in PRODUCT_ORDER:
        return product_name(label.lower())
    if label.lower() in KNOWN_LABELS:
        return KNOWN_LABELS[label.lower()]
    if label:
        return label
    return project_name or project_id


def running_costs(db: Session, rates: dict, today: date, *, start: date | None = None, end: date | None = None) -> dict:
    """Neon usage per project label: month to date and by day, in billing units, priced where a rate is set."""
    month_start = today.replace(day=1)
    start = start or month_start
    end = end or today
    lo = min(start, month_start)
    rows = db.execute(
        select(MetricSnapshot.date, MetricSnapshot.dimension, MetricSnapshot.metric, MetricSnapshot.value, MetricSnapshot.meta)
        .where(MetricSnapshot.source == "neon", MetricSnapshot.date >= lo, MetricSnapshot.date <= max(end, today))).all()
    units = {u.metric: u for u in NEON_UNITS}
    rate_of = {u.metric: rates.get(f"neon.{u.api_name}") for u in NEON_UNITS}
    current = neon_labels(config.NEON_PROJECT_PRODUCTS)
    names: dict[str, tuple[date, str]] = {}           # each project's newest Neon name, so a rename is one line
    for day, dimension, _metric, _value, meta in rows:
        name = str((meta or {}).get("project_name") or "")
        project_id = dimension.split(":", 1)[-1]
        if name and (project_id not in names or day >= names[project_id][0]):
            names[project_id] = (day, name)
    mtd: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    daily: dict[tuple[date, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for day, dimension, metric, value, meta in rows:
        unit = units.get(metric)
        if unit is None:
            continue
        project_id = dimension.split(":", 1)[-1]
        label = neon_label(project_id, names.get(project_id, (day, ""))[1], current)
        billed = Decimal(value or 0) / unit.divisor
        if month_start <= day <= today:
            mtd[label][metric] += billed
        if start <= day <= end:
            daily[(day, label)][metric] += billed

    def priced(usage: dict[str, Decimal]) -> dict:
        cost, unpriced = ZERO, []
        for metric, amount in usage.items():
            if not amount:
                continue
            rate = rate_of.get(metric)
            if rate is None:
                unpriced.append(units[metric].label)
            else:
                cost += amount * rate
        any_rate = any(r is not None for r in rate_of.values())
        return {"cost": dollars(cost) if any_rate else None, "unpriced": unpriced,
                "usage": [{"label": units[m].label, "amount": f"{usage[m].quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)}",
                           "unit": units[m].billing_unit} for m in units if usage.get(m)]}

    month = [{"label": label, **priced(usage)} for label, usage in sorted(mtd.items())]
    by_day = [{"date": day, "label": label, **priced(usage)} for (day, label), usage in sorted(daily.items(), reverse=True)]
    grand: dict[str, Decimal] = defaultdict(Decimal)
    for usage in mtd.values():
        for metric, amount in usage.items():
            grand[metric] += amount
    return {"month": month, "by_day": by_day, "month_start": month_start, "total": priced(grand),
            "rates_set": any(r is not None for r in rate_of.values()), "has_data": bool(rows)}


# ---- Sources -------------------------------------------------------------------------------------------------------
def cron_view(db: Session) -> dict[str, bool]:
    """{source: whether the cron held its variables} from the latest real (not dry-run) growth.pull run of each tier
    that ran the source, so the daily cron's answer is not pushed out by the hourly runs."""
    after = AuditLog.after
    newest: dict[str, tuple[int, bool]] = {}
    for tier in ("daily", "hourly", "all"):
        for entry in (db.query(AuditLog).filter(AuditLog.action == "growth.pull", AuditLog.actor == "job:growth",
                                                after["tier"].astext == tier, after["dry_run"].astext == "false")
                      .order_by(AuditLog.id.desc()).limit(5).all()):
            seen = (entry.after or {}).get("configured") if isinstance(entry.after, dict) else None
            for source, value in (seen.items() if isinstance(seen, dict) else ()):
                if source not in newest or entry.id > newest[source][0]:
                    newest[source] = (entry.id, bool(value))
    return {source: value for source, (_, value) in newest.items()}


def sources(db: Session, switches: dict[str, str], *, runs_per_source: int = 10) -> list[dict]:
    rows = health(db)
    cron_configured = cron_view(db)
    out = []
    for source, info in SOURCE_INFO.items():
        mode = source_mode(source, switches=switches)
        runs = (db.query(MetricRun).filter(MetricRun.source == source).order_by(MetricRun.id.desc())
                .limit(runs_per_source).all())
        names = env_names(source)
        out.append({
            "source": source, "info": info, "mode": mode, "health": rows.get(source), "runs": runs,
            "env": [{"name": n, "set": bool(str(getattr(config, n, "") or "").strip())} for n in names],
            "cron": cron_configured.get(source), "built": source in COLLECTORS,
        })
    return out
