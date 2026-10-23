"""GA4: visitors and the pixel's view, from the Data API with the service account (Viewer on each property).

Per property in GA4_PROPERTY_IDS ("pp=123456789,kp=...") one call on the daily tier:

    POST https://analyticsdata.googleapis.com/v1beta/properties/{id}:batchRunReports   four reports, returnPropertyQuota
      0  date                                                activeUsers, newUsers, sessions, engagedSessions, purchaseRevenue
      1  date, sessionSource, sessionMedium, sessionCampaignName, sessionDefaultChannelGroup   (same metrics)
      2  date, eventName, sessionSource, sessionMedium, sessionCampaignName   eventCount, for sign_up, generate_lead,
         trial_start and purchase only (eventCount with a filter is valid before an event is ever marked as a key
         event; keyEvents:<name> is refused until it is)
      3  date, orderCoupon                                   purchaseRevenue, ecommercePurchases (the promo-code proof)

and one on the hourly tier: POST ...:runRealtimeReport (activeUsers in the last 30 minutes).

Rows per product: visitors (activeUsers), new_users, sessions, engaged_sessions, revenue_pence on "" (report 0, exact
daily totals) and on source_medium:<source>/<medium>, campaign:<name>, channel:<group> (report 1; users summed across
rows of one dimension can count a person twice); sign_ups_pixel, leads_pixel, trials_started, purchases_pixel on "",
source_medium: and campaign: (report 2); purchases_pixel and revenue_pence on promo:<COUPON> (report 3);
quota_remaining (tokens left today) and, hourly, realtime_users. A report GA4 thresholded carries meta.thresholded.
GA4 restates the last 48 hours, which the 7-day re-pull covers; each daily pull answers for its whole window, so its
rows replace the window's earlier ones (a property remapped to another product, or a campaign that dropped out, leaves
no stale row). Two values that share their first 200 characters are added up into one row.

Days are the property's own time zone (set in GA4 Admin; Europe/London is assumed). The Test button reads each mapped
property's timeZone through the Admin API and warns when it is not Europe/London.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .... import config
from ...connectors.base import ConnectorError, ConnectorResult
from .base import PullResult, Replace, SnapshotRow, add_up, number, parse_day, pounds_to_pence, today_london
from .google import GoogleCollector

DATA_API = "https://analyticsdata.googleapis.com/v1beta"
ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
TOTAL_METRICS = ("activeUsers", "newUsers", "sessions", "engagedSessions", "purchaseRevenue")
TRAFFIC = {"activeUsers": "visitors", "newUsers": "new_users", "sessions": "sessions", "engagedSessions": "engaged_sessions"}
EVENTS = {"sign_up": "sign_ups_pixel", "generate_lead": "leads_pixel", "trial_start": "trials_started",
          "purchase": "purchases_pixel"}
NOT_SET = ("", "(not set)")
DAILY_METRICS = ("visitors", "new_users", "sessions", "engaged_sessions", "revenue_pence", "sign_ups_pixel", "leads_pixel",
                 "trials_started", "purchases_pixel")
LONDON_TZ = "Europe/London"


def _report(dimensions: list[str], metrics: list[str], since: date, until: date, **extra) -> dict:
    body = {"dateRanges": [{"startDate": since.isoformat(), "endDate": until.isoformat()}],
            "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics],
            "keepEmptyRows": False, "limit": 100000, "returnPropertyQuota": True}
    body.update(extra)
    return body


def batch_body(since: date, until: date) -> dict:
    events = {"filter": {"fieldName": "eventName", "inListFilter": {"values": list(EVENTS)}}}
    return {"requests": [
        _report(["date"], list(TOTAL_METRICS), since, until),
        _report(["date", "sessionSource", "sessionMedium", "sessionCampaignName", "sessionDefaultChannelGroup"],
                list(TOTAL_METRICS), since, until),
        _report(["date", "eventName", "sessionSource", "sessionMedium", "sessionCampaignName"], ["eventCount"], since, until,
                dimensionFilter=events),
        _report(["date", "orderCoupon"], ["purchaseRevenue", "ecommercePurchases"], since, until),
    ]}


def table(report: dict) -> list[dict]:
    """A runReport response as a list of {dimension or metric name: value} dicts."""
    dims = [h.get("name") for h in report.get("dimensionHeaders") or []]
    mets = [h.get("name") for h in report.get("metricHeaders") or []]
    out = []
    for row in report.get("rows") or []:
        values = {name: (cell or {}).get("value", "") for name, cell in zip(dims, row.get("dimensionValues") or [])}
        values.update({name: (cell or {}).get("value", "") for name, cell in zip(mets, row.get("metricValues") or [])})
        out.append(values)
    return out


def _value(metric: str, raw) -> Decimal | None:
    if metric == "purchaseRevenue":
        pence = pounds_to_pence(raw)
        return None if pence is None else Decimal(pence)
    return number(raw)


class GA4Collector(GoogleCollector):
    source = "ga4"
    tiers = ("daily", "hourly")
    env_names = ("GOOGLE_SA_JSON", "GA4_PROPERTY_IDS")
    mapping_env = "GA4_PROPERTY_IDS"
    window_days = 7
    scope = SCOPE

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "GA4Collector":
        return cls(DATA_API, config.GOOGLE_SA_JSON, properties(config.GA4_PROPERTY_IDS), dry_run=dry_run)

    def rows_for(self, product: str, body: dict, notes: list) -> list[SnapshotRow]:
        reports = body.get("reports") if isinstance(body, dict) else None
        if not isinstance(reports, list) or len(reports) != 4:
            raise ConnectorError("bad_response", detail="ga4: batchRunReports did not return four reports")
        sums: dict[tuple, Decimal] = defaultdict(Decimal)
        thresholded: set[int] = set()
        for index, report in enumerate(reports):
            if (report.get("metadata") or {}).get("subjectToThresholding"):
                thresholded.add(index)
        for row in table(reports[0]):
            when = parse_day(row.get("date"))
            for api, metric in list(TRAFFIC.items()) + [("purchaseRevenue", "revenue_pence")]:
                value = _value(api, row.get(api))
                if when and value is not None:
                    sums[(when, metric, "", 0)] += value
        for row in table(reports[1]):
            when = parse_day(row.get("date"))
            if when is None:
                continue
            dims = [f"source_medium:{row.get('sessionSource', '')}/{row.get('sessionMedium', '')}"]
            if row.get("sessionCampaignName", "") not in NOT_SET:
                dims.append(f"campaign:{row['sessionCampaignName']}")
            if row.get("sessionDefaultChannelGroup", "") not in NOT_SET:
                dims.append(f"channel:{row['sessionDefaultChannelGroup']}")
            for api, metric in list(TRAFFIC.items()) + [("purchaseRevenue", "revenue_pence")]:
                value = _value(api, row.get(api))
                if value is None:
                    continue
                for dim in dims:
                    sums[(when, metric, dim, 1)] += value
        for row in table(reports[2]):
            when = parse_day(row.get("date"))
            metric = EVENTS.get(row.get("eventName", ""))
            value = number(row.get("eventCount"))
            if when is None or metric is None or value is None:
                continue
            dims = ["", f"source_medium:{row.get('sessionSource', '')}/{row.get('sessionMedium', '')}"]
            if row.get("sessionCampaignName", "") not in NOT_SET:
                dims.append(f"campaign:{row['sessionCampaignName']}")
            for dim in dims:
                sums[(when, metric, dim, 2)] += value
        for row in table(reports[3]):
            when = parse_day(row.get("date"))
            coupon = str(row.get("orderCoupon", "")).strip()
            if when is None or coupon in NOT_SET:
                continue
            dim = f"promo:{coupon.upper()[:40]}"
            for api, metric in (("ecommercePurchases", "purchases_pixel"), ("purchaseRevenue", "revenue_pence")):
                value = _value(api, row.get(api))
                if value is not None:
                    sums[(when, metric, dim, 3)] += value
        rows = []
        for (when, metric, dim, index), value in sorted(sums.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
            rows.append(SnapshotRow(when, product, metric, value, dim, {"thresholded": True} if index in thresholded else {}))
        if thresholded:
            notes.append(f"{product}: GA4 thresholded {len(thresholded)} report(s): small rows may be withheld")
        remaining = [number(((r.get("propertyQuota") or {}).get("tokensPerDay") or {}).get("remaining")) for r in reports]
        remaining = [r for r in remaining if r is not None]
        if remaining:
            rows.append(SnapshotRow(today_london(), product, "quota_remaining", min(remaining)))
        return add_up(rows)

    def pull(self, since: date, until: date, *, tier: str = "daily") -> PullResult:
        notes: list = []
        rows: list = []
        self.ensure_token()
        targets = self.mapping or ({"group": "{propertyId}"} if self.dry_run else {})
        for product, property_id in targets.items():
            if tier == "hourly":
                result = self.request("POST", f"/properties/{property_id}:runRealtimeReport",
                                      json={"metrics": [{"name": "activeUsers"}]})
                if not result.dry_run:
                    total = sum((number(r.get("activeUsers")) or Decimal(0) for r in table(result.data or {})), Decimal(0))
                    rows.append(SnapshotRow(today_london(), product, "realtime_users", total))
                continue
            result = self.request("POST", f"/properties/{property_id}:batchRunReports", json=batch_body(since, until))
            if not result.dry_run:
                rows.extend(self.rows_for(product, result.data, notes))
        today = today_london()
        if tier == "hourly":
            replace = [Replace(("realtime_users",), today, today)]
        else:
            replace = [Replace(DAILY_METRICS, since, until), Replace(("quota_remaining",), today, today)]
        return self.result(rows, notes, replace if self.mapping else [])

    def probe(self) -> ConnectorResult:
        self.ensure_token()
        result = self.request("GET", f"{ADMIN_API}/accountSummaries", params={"pageSize": 50})
        if not result.dry_run and isinstance(result.data, dict):
            summaries = result.data.get("accountSummaries") or []
            seen = {str(p.get("property", "")).split("/")[-1] for s in summaries for p in s.get("propertySummaries") or []}
            wanted = set(self.mapping.values())
            zones, warnings = {}, []
            for product, property_id in sorted(self.mapping.items()):
                if property_id not in seen:
                    continue
                body = self.get_json(f"{ADMIN_API}/properties/{property_id}")
                zone = str((body or {}).get("timeZone") or "")[:40] if isinstance(body, dict) else ""
                zones[product] = zone
                if zone and zone != LONDON_TZ:
                    warnings.append(f"{product}'s GA4 property reports days in {zone}, not {LONDON_TZ}: its visitors "
                                    "will not line up with the London days of the rest (change it in GA4 Admin)")
            result.data = {"ok": True, "properties_visible": len(seen), "missing": sorted(wanted - seen),
                           "time_zones": zones, "warnings": warnings}
        elif result.dry_run:
            for property_id in sorted(set(self.mapping.values())) or ["{propertyId}"]:
                self.request("GET", f"{ADMIN_API}/properties/{property_id}")
        return result


def properties(raw: str) -> dict[str, str]:
    """GA4_PROPERTY_IDS as {product: numeric property id}; "properties/123" is accepted, anything else is dropped."""
    out = {}
    for product, value in config.pairs(raw).items():
        value = value.split("/")[-1].strip()
        if product in ("pp", "kp", "ordris", "hnh") and value.isdigit():
            out[product] = value
    return out

