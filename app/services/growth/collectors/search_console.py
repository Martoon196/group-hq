"""Search Console: Google Search clicks, impressions, CTR and position, per site, with the same service account
(a Restricted user on each domain property).

Per site in GSC_SITES ("pp=sc-domain:propertypredator.com,..."), five calls:

    POST https://www.googleapis.com/webmasters/v3/sites/{siteUrl, encoded}/searchAnalytics/query
         {startDate, endDate, dimensions, type: "web", dataState: "final", rowLimit: 1000, startRow}
    dimensions [date], [date, query], [date, page], [date, device], [date, country]

for today-7 .. today-3 (Google finalises a day about three days late; the four older days are re-pulled because
they can still move). Rows: search_clicks, search_impressions, search_ctr and search_position on "" (the true daily
totals, anonymised queries included), query:<q>, page:<path>, device:<d> and country:<c>. Google's rows that land on
one stored key (the apex, www, http and query-string versions of one page, or two values that share their first 200
characters) are added up first: clicks and impressions summed, ctr worked out again from them and position weighted
by impressions. Every roll-up on the screens does the same, never averaging the stored ratios. A grouping that
returns a full page is read on from startRow, up to MAX_ROWS rows. The pull answers for its whole window, so its rows
replace the window's earlier ones (a query that dropped out of Google's rows does not linger).

Days are Search Console's own: Pacific Time calendar days, not London ones (the screens say so).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote, urlsplit

from .... import config
from ...connectors.base import ConnectorResult
from .base import PullResult, Replace, SnapshotRow, dimension_key, number, parse_day, today_london
from .google import GoogleCollector

API = "https://www.googleapis.com/webmasters/v3"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GROUPINGS = ((), ("query",), ("page",), ("device",), ("country",))
ROW_LIMIT = 1000
MAX_ROWS = 5000
FINAL_LAG_DAYS = 3
METRICS = ("search_clicks", "search_impressions", "search_ctr", "search_position")


def page_key(url: str, site: str) -> str:
    """https://propertypredator.com/valuation?x=1 -> /valuation; another subdomain keeps its host."""
    parts = urlsplit(url or "")
    domain = site.split(":", 1)[1] if site.startswith("sc-domain:") else urlsplit(site).hostname or ""
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if not host or host in (domain, f"www.{domain}"):
        return path
    return f"{host}{path}"


class SearchConsoleCollector(GoogleCollector):
    source = "search_console"
    env_names = ("GOOGLE_SA_JSON", "GSC_SITES")
    mapping_env = "GSC_SITES"
    window_days = 7
    scope = SCOPE

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "SearchConsoleCollector":
        sites = {k: v for k, v in config.pairs(config.GSC_SITES).items() if k in ("pp", "kp", "ordris", "hnh")}
        return cls(API, config.GOOGLE_SA_JSON, sites, dry_run=dry_run)

    def rows_for(self, product: str, site: str, grouping: tuple[str, ...], rows_in: list) -> list[SnapshotRow]:
        """Google's rows as snapshot rows, one per stored key: rows that share a key once normalised and cut are
        added up (clicks, impressions), with ctr and position worked out again (position weighted by impressions)."""
        parts: dict[tuple[date, str], dict] = defaultdict(
            lambda: {"clicks": Decimal(0), "impressions": Decimal(0), "weighted": Decimal(0), "rows": 0, "ctr": None,
                     "position": None})
        for row in rows_in:
            keys = row.get("keys") or []
            when = parse_day(str(keys[0])) if keys else None
            if when is None:
                continue
            if grouping:
                value = str(keys[1]) if len(keys) > 1 else ""
                if grouping[0] == "page":
                    value = page_key(value, site)
                dimension = dimension_key(f"{grouping[0]}:{value}")
            else:
                dimension = ""
            clicks, impressions = number(row.get("clicks")), number(row.get("impressions"))
            position, ctr = number(row.get("position")), number(row.get("ctr"))
            part = parts[(when, dimension)]
            part["rows"] += 1
            part["clicks"] += clicks or 0
            part["impressions"] += impressions or 0
            if position is not None and impressions:
                part["weighted"] += position * impressions
            part["ctr"], part["position"] = ctr, position        # kept as Google gave them when the row is alone
            part["has_clicks"] = part.get("has_clicks") or clicks is not None
            part["has_impressions"] = part.get("has_impressions") or impressions is not None
        rows = []
        for (when, dimension), part in parts.items():
            if part.get("has_clicks"):
                rows.append(SnapshotRow(when, product, "search_clicks", part["clicks"], dimension))
            if part.get("has_impressions"):
                rows.append(SnapshotRow(when, product, "search_impressions", part["impressions"], dimension))
            if part["rows"] == 1:
                ctr, position = part["ctr"], part["position"]
            else:
                ctr = part["clicks"] / part["impressions"] if part["impressions"] else None
                position = part["weighted"] / part["impressions"] if part["impressions"] else None
            if ctr is not None:
                rows.append(SnapshotRow(when, product, "search_ctr", ctr, dimension))
            if position is not None:
                rows.append(SnapshotRow(when, product, "search_position", position, dimension))
        return rows

    def pull(self, since: date, until: date, *, tier: str = "daily") -> PullResult:
        notes: list = []
        rows: list = []
        last = min(until, today_london() - timedelta(days=FINAL_LAG_DAYS))
        first = min(since, last)
        self.ensure_token()
        targets = self.mapping or ({"group": "sc-domain:{site}"} if self.dry_run else {})
        for product, site in targets.items():
            path = f"/sites/{quote(site, safe='')}/searchAnalytics/query"
            for grouping in GROUPINGS:
                start_row = 0
                collected: list = []                 # every page of the grouping, added up together
                while start_row < MAX_ROWS:
                    body = {"startDate": first.isoformat(), "endDate": last.isoformat(),
                            "dimensions": ["date", *grouping], "type": "web", "dataState": "final",
                            "rowLimit": ROW_LIMIT, "startRow": start_row}
                    result = self.request("POST", path, json=body)
                    if result.dry_run:
                        break
                    found = (result.data or {}).get("rows") or [] if isinstance(result.data, dict) else []
                    collected.extend(r for r in found if isinstance(r, dict))
                    if len(found) < ROW_LIMIT:
                        break
                    start_row += ROW_LIMIT
                else:
                    notes.append(f"{product} {'/'.join(('date',) + grouping)}: stopped at {MAX_ROWS} rows (Google sorts by clicks)")
                rows.extend(self.rows_for(product, site, grouping, collected))
        return self.result(rows, notes, [Replace(METRICS, first, last)] if self.mapping and first <= last else [])

    def probe(self) -> ConnectorResult:
        self.ensure_token()
        result = self.request("GET", "/sites")
        if not result.dry_run and isinstance(result.data, dict):
            visible = {str(s.get("siteUrl")) for s in result.data.get("siteEntry") or [] if isinstance(s, dict)}
            result.data = {"ok": True, "sites_visible": len(visible),
                           "missing": sorted(set(self.mapping.values()) - visible)}
        return result

