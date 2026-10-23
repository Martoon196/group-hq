"""Writes for the Growth tables: every write is an upsert on the table's unique key, so a re-pull of the same window
leaves the same rows (the platforms restate recent days; the newest answer wins).

    upsert_snapshots(db, "ga4", rows)      metric_snapshots on (date, source, product, metric, dimension)
    upsert_spend(db, rows)                 ad_spend on (platform, account_id, campaign_id, date)
    upsert_purchases(db, rows)             product_purchases on (product, purchase_ref)

    replace_rows(db, "brevo", replaces)    clears the rows a pull answers for in full (PullResult.replace)

A dimension value is cut at DIMENSION_MAX characters before the key is computed. Two snapshot or spend rows with one
key in one batch are refused with ValueError when their values differ (identical repeats, a post read twice across a
page boundary, are one row): a collector whose platform can answer two rows for one key adds them up first
(collectors.base.add_up), so nothing is ever dropped by a silent last-wins. A purchase seen twice in one batch is the
same purchase restated, so the last one wins. A metric the registry does not know, or a source that may not write it,
is refused with ValueError: nothing is written. write() clears the pull's replace windows and upserts its rows; the
caller commits, one transaction per source, so a refusal rolls back the clearing as well.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...models import (AD_PLATFORMS, DIMENSION_MAX, METRIC_PRODUCTS, PRODUCTS, PURCHASE_KINDS, PURCHASE_STATUSES, SOURCES,
                       AdSpend, MetricSnapshot, ProductPurchase, utcnow)
from .collectors.base import PurchaseRow, Replace, SnapshotRow, SpendRow
from .registry import is_registered

RAW_MAX_BYTES = 8000
CHUNK = 500
FOUR_PLACES = Decimal("0.0001")


def cut(value: str | None, limit: int = DIMENSION_MAX) -> str:
    """A text value as stored: NUL removed (Postgres refuses it in text and JSON alike), cut at limit characters."""
    return (value or "").replace("\x00", "")[:limit]


def nul_free(value):
    """A JSON-able value with NUL removed from every string in it, keys included."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {nul_free(k) if isinstance(k, str) else k: nul_free(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [nul_free(v) for v in value]
    return value


def bounded(raw) -> dict:
    """A platform row as received, kept only while it is small: an oversized one is replaced by a note."""
    if not isinstance(raw, dict):
        return {}
    try:
        text = json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"note": "raw row could not be stored as JSON"}
    if len(text.encode("utf-8")) > RAW_MAX_BYTES:
        return {"note": f"raw row over {RAW_MAX_BYTES} bytes was not stored", "via": nul_free(str(raw.get("via", "")))}
    return nul_free(json.loads(text))


def _decimal(value) -> Decimal:
    return Decimal(str(value)).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def _chunks(items: list, size: int = CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def snapshot_values(source: str, rows: Iterable[SnapshotRow], pulled_at: datetime | None = None) -> list[dict]:
    """The rows as table values, checked against the registry and the vocabularies, keys merged (last wins)."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    pulled_at = pulled_at or utcnow()
    merged: dict[tuple, dict] = {}
    for row in rows:
        if not is_registered(row.metric, source):
            raise ValueError(f"{source} may not write metric {row.metric!r}: it is not in the registry for that source")
        if row.product not in METRIC_PRODUCTS:
            raise ValueError(f"unknown product {row.product!r} on {row.metric}")
        if not isinstance(row.date, date) or isinstance(row.date, datetime):
            raise ValueError(f"{row.metric}: date must be a date, not {type(row.date).__name__}")
        dimension = cut(row.dimension)
        values = {"date": row.date, "source": source, "product": row.product, "metric": row.metric,
                  "dimension": dimension, "value": _decimal(row.value), "meta": nul_free(dict(row.meta or {})),
                  "pulled_at": pulled_at}
        key = (row.date, source, row.product, row.metric, dimension)
        if key in merged and merged[key]["value"] != values["value"]:
            raise ValueError(f"{source} gave two different values for {row.metric} {dimension[:60]!r} ({row.product}) on "
                             f"{row.date}: the collector must add them up before they are stored")
        merged[key] = values
    return list(merged.values())


def upsert_snapshots(db: Session, source: str, rows: Iterable[SnapshotRow], *, pulled_at: datetime | None = None) -> int:
    values = snapshot_values(source, rows, pulled_at)
    for chunk in _chunks(values):
        statement = insert(MetricSnapshot).values(chunk)
        db.execute(statement.on_conflict_do_update(
            constraint="uq_metric_snapshots_key",
            set_={"value": statement.excluded.value, "meta": statement.excluded.meta,
                  "pulled_at": statement.excluded.pulled_at}))
    return len(values)


def spend_values(rows: Iterable[SpendRow], pulled_at: datetime | None = None) -> list[dict]:
    pulled_at = pulled_at or utcnow()
    merged: dict[tuple, dict] = {}
    for row in rows:
        if row.platform not in AD_PLATFORMS:
            raise ValueError(f"unknown ad platform {row.platform!r}")
        if row.product not in METRIC_PRODUCTS:
            raise ValueError(f"unknown product {row.product!r}")
        values = {
            "date": row.date, "platform": row.platform, "account_id": cut(row.account_id, 60),
            "campaign_id": cut(row.campaign_id, 80), "campaign_name": cut(row.campaign_name, 200), "product": row.product,
            "spend_pence": int(row.spend_pence), "impressions": int(row.impressions), "clicks": int(row.clicks),
            "conversions": _decimal(row.conversions), "conversions_sign_up": _decimal(row.conversions_sign_up),
            "conversions_lead": _decimal(row.conversions_lead), "conversions_purchase": _decimal(row.conversions_purchase),
            "conversions_trial": _decimal(row.conversions_trial), "conversion_value_pence": int(row.conversion_value_pence),
            "raw": bounded(row.raw), "pulled_at": pulled_at,
        }
        key = (values["platform"], values["account_id"], values["campaign_id"], values["date"])
        if key in merged and {k: v for k, v in merged[key].items() if k != "raw"} != {k: v for k, v in values.items() if k != "raw"}:
            raise ValueError(f"two different spend rows for {values['platform']} campaign {values['campaign_id'][:60]!r} on "
                             f"{values['date']}: they must be added up before they are stored")
        merged[key] = values
    return list(merged.values())


def upsert_spend(db: Session, rows: Iterable[SpendRow], *, pulled_at: datetime | None = None) -> int:
    values = spend_values(rows, pulled_at)
    keep = ("campaign_name", "product", "spend_pence", "impressions", "clicks", "conversions", "conversions_sign_up",
            "conversions_lead", "conversions_purchase", "conversions_trial", "conversion_value_pence", "raw", "pulled_at")
    for chunk in _chunks(values):
        statement = insert(AdSpend).values(chunk)
        db.execute(statement.on_conflict_do_update(
            constraint="uq_ad_spend_key", set_={k: getattr(statement.excluded, k) for k in keep}))
    return len(values)


def purchase_values(rows: Iterable[PurchaseRow], pulled_at: datetime | None = None) -> list[dict]:
    pulled_at = pulled_at or utcnow()
    merged: dict[tuple, dict] = {}
    for row in rows:
        if row.product not in PRODUCTS:
            raise ValueError(f"unknown product {row.product!r}")
        if row.kind not in PURCHASE_KINDS:
            raise ValueError(f"unknown purchase kind {row.kind!r}")
        if row.status not in PURCHASE_STATUSES:
            raise ValueError(f"unknown purchase status {row.status!r}")
        values = {
            "product": row.product, "purchase_ref": cut(row.purchase_ref, 120), "occurred_at": row.occurred_at,
            "kind": row.kind, "plan": cut(row.plan, 80), "amount_pence": int(row.amount_pence),
            "currency": cut((row.currency or "gbp").lower(), 3), "status": row.status,
            "promo_code": cut((row.promo_code or "").strip().upper(), 40), "affiliate_ref": cut(row.affiliate_ref, 64),
            "first_touch": nul_free(dict(row.first_touch or {})), "last_touch": nul_free(dict(row.last_touch or {})),
            "account_key": cut(row.account_key, 64), "stripe_customer_id": cut(row.stripe_customer_id, 60),
            "raw": bounded(row.raw), "pulled_at": pulled_at,
        }
        merged[(values["product"], values["purchase_ref"])] = values
    return list(merged.values())


def upsert_purchases(db: Session, rows: Iterable[PurchaseRow], *, pulled_at: datetime | None = None) -> int:
    values = purchase_values(rows, pulled_at)
    keep = ("occurred_at", "kind", "plan", "amount_pence", "currency", "status", "promo_code", "affiliate_ref",
            "first_touch", "last_touch", "account_key", "stripe_customer_id", "raw", "pulled_at")
    for chunk in _chunks(values):
        statement = insert(ProductPurchase).values(chunk)
        db.execute(statement.on_conflict_do_update(
            constraint="uq_product_purchases_ref", set_={k: getattr(statement.excluded, k) for k in keep}))
    return len(values)


def replace_rows(db: Session, source: str, replaces: Iterable[Replace]) -> int:
    """Delete this source's snapshot rows that each Replace window covers (a pull that answers for them in full).
    Returns the number of rows deleted. Only metric_snapshots: ad spend and purchases keep their own upserts."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    deleted = 0
    for window in replaces:
        if not isinstance(window, Replace) or not window.metrics:
            raise ValueError(f"{source}: a replace window needs its metrics")
        for metric in window.metrics:
            if not is_registered(metric, source):
                raise ValueError(f"{source} may not replace metric {metric!r}: it is not in the registry for that source")
        statement = delete(MetricSnapshot).where(
            MetricSnapshot.source == source, MetricSnapshot.metric.in_(window.metrics),
            MetricSnapshot.date >= window.since, MetricSnapshot.date <= window.until)
        if window.dimension_like is not None:
            statement = statement.where(MetricSnapshot.dimension.like(window.dimension_like))
        deleted += db.execute(statement.execution_options(synchronize_session=False)).rowcount or 0
    return deleted


def write(db: Session, source: str, rows: list, replace: Iterable[Replace] = ()) -> int:
    """Every row a collector returned, each kind to its table, after clearing the pull's replace windows. Returns the
    number of rows written. Every row is checked before anything is deleted or written."""
    snapshots = [r for r in rows if isinstance(r, SnapshotRow)]
    spend = [r for r in rows if isinstance(r, SpendRow)]
    purchases = [r for r in rows if isinstance(r, PurchaseRow)]
    other = len(rows) - len(snapshots) - len(spend) - len(purchases)
    if other:
        raise ValueError(f"{source}: {other} row(s) of an unknown kind")
    snapshot_values(source, snapshots)          # refuse a bad batch before the windows are cleared
    spend_values(spend)
    purchase_values(purchases)
    replace = list(replace or ())
    if replace:
        replace_rows(db, source, replace)
    total = 0
    if snapshots:
        total += upsert_snapshots(db, source, snapshots)
    if spend:
        total += upsert_spend(db, spend)
    if purchases:
        total += upsert_purchases(db, purchases)
    return total
