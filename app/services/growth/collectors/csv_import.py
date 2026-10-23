"""Ad spend from a platform's own CSV export: the stopgap for every platform whose API is waiting on approval.

The Sources screen takes a daily campaign export (LinkedIn Campaign Manager, Meta Ads Manager, TikTok Ads Manager,
Google Ads or X) and writes one ad_spend row per campaign per day with raw.via = "csv", the same rows the API
collectors will write later, so the Channels screen does not care which arrived. Columns are found by their header
(case and spacing ignored), from the names each platform uses:

    day        Day, Date, Start Date (in UTC), Reporting starts, By Day, Stat time day
    campaign   Campaign Name, Campaign name, Campaign
    campaign id  Campaign ID (else the name stands in: name:<campaign>)
    account    Account ID, Ad account ID, Advertiser ID (else the account typed on the form)
    spend      Amount spent (GBP), Amount Spent, Total Spent, Spend, Cost, Total cost
    impressions, clicks (Clicks, Link clicks, Clicks (all)), conversions (Conversions, Results)

Spend is pounds with or without a currency sign or thousands separators; dates are ISO (2026-11-03) unless the form
says the file uses day/month/year or month/day/year, because 03/11/2026 means different days in the UK and the US
and the importer will not guess. The product is the campaign name's prefix (pp-, kp-, ord-, hnh-), else group. A file
with an unreadable row is refused whole, naming the row, and nothing is written.

Several rows for one campaign on one day (an ad-set or ad-level export, or one broken down by age, placement or
device) are added up into one campaign-day: spend, impressions, clicks and conversions summed, raw.rows_summed and
raw.lines saying which lines of the file made it. So the rows the dry run shows are exactly the rows written.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ....models import AD_PLATFORMS
from ..attribution import product_from_campaign
from .base import SpendRow

MAX_BYTES = 2_000_000
MAX_ROWS = 5000
DATE_FORMATS = {"iso": "%Y-%m-%d", "dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y"}
COLUMNS = {
    "date": ("day", "date", "start date (in utc)", "start date", "reporting starts", "by day", "stat time day"),
    "campaign_name": ("campaign name", "campaign"),
    "campaign_id": ("campaign id",),
    "account_id": ("account id", "ad account id", "advertiser id", "account"),
    "spend": ("amount spent (gbp)", "amount spent", "total spent", "spend", "cost", "total cost", "amount spent (£)"),
    "impressions": ("impressions",),
    "clicks": ("clicks", "link clicks", "clicks (all)", "clicks (destination)"),
    "conversions": ("conversions", "results", "total conversions"),
}


class CSVProblem(ValueError):
    """The file cannot be imported as it is; the message says which row and why."""


@dataclass
class CSVResult:
    rows: list[SpendRow] = field(default_factory=list)
    skipped: int = 0
    columns: dict = field(default_factory=dict)


def _key(header: str) -> str:
    return re.sub(r"\s+", " ", (header or "").replace("﻿", "").strip().lower())


def _find(headers: list[str]) -> dict[str, str]:
    by_key = {_key(h): h for h in headers}
    found = {}
    for column, names in COLUMNS.items():
        for name in names:
            if name in by_key:
                found[column] = by_key[name]
                break
    return found


def _money_pence(raw: str, line: int) -> int:
    text = re.sub(r"[£$€,\s]|GBP", "", raw or "", flags=re.I)
    if text in ("", "-", "--"):
        return 0
    try:
        return int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise CSVProblem(f"row {line}: the spend {raw[:30]!r} is not an amount") from None


def _count(raw: str, line: int, name: str) -> Decimal:
    text = re.sub(r"[,\s]", "", raw or "")
    if text in ("", "-", "--"):
        return Decimal(0)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise CSVProblem(f"row {line}: {name} {raw[:30]!r} is not a number") from None
    if value < 0:
        raise CSVProblem(f"row {line}: {name} is negative")
    return value


def _day(raw: str, line: int, fmt: str) -> date:
    text = (raw or "").strip()[:10] if fmt == "iso" else (raw or "").strip().split(" ")[0]
    try:
        return datetime.strptime(text, DATE_FORMATS[fmt]).date()
    except ValueError:
        hint = "" if fmt != "iso" or "/" not in text else ": choose the file's date format on the form"
        raise CSVProblem(f"row {line}: the day {raw[:20]!r} is not a date in the format chosen{hint}") from None


def parse(text: str, *, platform: str, account_id: str = "", date_format: str = "iso") -> CSVResult:
    """The file as ad_spend rows. Raises CSVProblem, naming the row, when anything cannot be read."""
    if platform not in AD_PLATFORMS:
        raise CSVProblem(f"choose the platform the file came from ({', '.join(AD_PLATFORMS)})")
    if date_format not in DATE_FORMATS:
        raise CSVProblem("choose the date format the file uses")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise CSVProblem(f"the file is over {MAX_BYTES // 1_000_000} MB: export a shorter range")
    lines = text.splitlines()
    # Meta and LinkedIn put a few title lines above the header: start at the first line that names a day and a spend.
    start = 0
    for index, line in enumerate(lines[:20]):
        cols = _find(next(csv.reader([line]), []))
        if "date" in cols and "spend" in cols:
            start = index
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    columns = _find(reader.fieldnames or [])
    for needed in ("date", "spend", "campaign_name"):
        if needed not in columns:
            raise CSVProblem(f"no {needed.replace('_', ' ')} column was found: the header row needs one of "
                             + ", ".join(COLUMNS[needed]))
    result = CSVResult(columns=columns)
    for number_in_file, record in enumerate(reader, start=start + 2):
        if len(result.rows) >= MAX_ROWS:
            raise CSVProblem(f"over {MAX_ROWS} rows: export a shorter range")
        name = (record.get(columns["campaign_name"]) or "").strip()
        day_raw = (record.get(columns["date"]) or "").strip()
        if not name and not day_raw:
            result.skipped += 1          # blank or totals line
            continue
        if not day_raw or {name.lower(), day_raw.lower()} & {"total", "totals", "grand total"}:
            result.skipped += 1          # a totals line, or a row with no day
            continue
        day = _day(day_raw, number_in_file, date_format)
        campaign_id = (record.get(columns.get("campaign_id", ""), "") or "").strip() or f"name:{name}"
        account = (record.get(columns.get("account_id", ""), "") or "").strip() or (account_id or "").strip() or "csv"
        conversions = _count(record.get(columns.get("conversions", ""), "") or "", number_in_file, "conversions")
        result.rows.append(SpendRow(
            date=day, platform=platform, account_id=account[:60], campaign_id=campaign_id[:80], campaign_name=name[:200],
            product=product_from_campaign(name),
            spend_pence=_money_pence(record.get(columns["spend"]) or "", number_in_file),
            impressions=int(_count(record.get(columns.get("impressions", ""), "") or "", number_in_file, "impressions")),
            clicks=int(_count(record.get(columns.get("clicks", ""), "") or "", number_in_file, "clicks")),
            conversions=conversions,
            raw={"via": "csv", "line": number_in_file,
                 **{k: (v or "")[:200] for k, v in record.items() if isinstance(k, str) and k not in ("via", "line")}}))
    if not result.rows:
        raise CSVProblem("the file has no campaign rows")
    result.rows = campaign_days(result.rows)
    return result


def campaign_days(rows: list[SpendRow]) -> list[SpendRow]:
    """One row per (platform, account, campaign, day), the file's rows for it added up, in the file's order."""
    out: dict[tuple, SpendRow] = {}
    for row in rows:
        key = (row.platform, row.account_id, row.campaign_id, row.date)
        line = row.raw.get("line")
        if key not in out:
            out[key] = row
            row.raw = {"via": "csv", "rows_summed": 1, "lines": [line], **{k: v for k, v in row.raw.items()
                                                                          if k not in ("via", "line")}}
            continue
        merged = out[key]
        merged.spend_pence += row.spend_pence
        merged.impressions += row.impressions
        merged.clicks += row.clicks
        merged.conversions = Decimal(str(merged.conversions)) + Decimal(str(row.conversions))
        merged.raw["rows_summed"] += 1
        if len(merged.raw["lines"]) < 50:
            merged.raw["lines"].append(line)
    for row in out.values():
        if row.raw["rows_summed"] > 1:              # the first row's columns would mislead once rows are added up
            row.raw = {"via": "csv", "rows_summed": row.raw["rows_summed"], "lines": row.raw["lines"],
                       "campaign": row.campaign_name}
    return list(out.values())
