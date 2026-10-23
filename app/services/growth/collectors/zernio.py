"""Zernio, the organic social feed for every connected account, with a read-only scoped key (ZERNIO_API_KEY).

Per profile in ZERNIO_PROFILE_PRODUCTS ("<profileId>=pp,..."), all GET on https://zernio.com/api/v1:

    /analytics/daily-metrics?profileId=&fromDate=&toDate=&attribution=received
        dailyData[]{date, postCount, metrics{impressions, reach, likes, comments, shares, saves, clicks, views}} and
        platformBreakdown[] -> posts, post_impressions, post_reach, post_engagements, post_clicks, video_views by day
        (profile total on ""; "received" buckets engagement on the day it arrived)
    /accounts/follower-stats?profileId=&fromDate=&toDate=&granularity=daily
        accounts[]{_id, platform, username} and stats{<accountId>: [{date, followers}]}
        -> followers and followers_new per account:<platform>:<handle>
    /analytics?profileId=&fromDate=&page=&limit=50
        per-post rows for posts published in the last POST_DAYS days -> post_* per post:zernio:<id> on the publish day,
        meta {url, platform, external}; isAd posts are left out of organic figures
and, for each ad account in ZERNIO_AD_ACCOUNTS ("<accountId>=tiktok,<accountId>=x"), once it is connected:
    /ads/timeline?accountId=&fromDate=&toDate=      daily account totals -> ad_spend rows, campaign "account", raw.via zernio

Zernio caches post analytics for about an hour and followers once a day; reach is not additive across days. The
dashboard never calls Zernio on a page view: only this collector does, from the daily cron or a Pull now. A post read
twice (it moved across a page boundary while the pages were read) is kept once. Each pull answers in full for the
window's profile totals and followers and for the posts of the last POST_DAYS days, so those rows replace the earlier
ones (a profile remapped in ZERNIO_PROFILE_PRODUCTS leaves no stale row behind).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .... import config
from ...connectors.base import ConnectorResult
from .base import (MAX_PAGES, MetricCollector, PullResult, Replace, SnapshotRow, SpendRow, london_date, number,
                   parse_day, pounds_to_pence)

POST_DAYS = 30
POST_PAGE = 50
ENGAGEMENT_KEYS = ("likes", "comments", "shares", "saves")
AD_PLATFORMS = ("tiktok", "x", "meta", "linkedin", "google")
POST_METRICS = ("post_impressions", "post_reach", "post_engagements", "post_clicks", "video_views")


def engagements(metrics: dict) -> Decimal:
    return sum((number(metrics.get(k)) or Decimal(0) for k in ENGAGEMENT_KEYS), Decimal(0))


def metric_values(metrics: dict) -> dict[str, Decimal]:
    out = {"post_impressions": number(metrics.get("impressions")), "post_reach": number(metrics.get("reach")),
           "post_clicks": number(metrics.get("clicks")), "video_views": number(metrics.get("views")),
           "post_engagements": engagements(metrics)}
    return {k: v for k, v in out.items() if v is not None}


class ZernioCollector(MetricCollector):
    source = "zernio"
    env_names = ("ZERNIO_API_KEY", "ZERNIO_PROFILE_PRODUCTS")
    window_days = 7
    timeout_seconds = 20.0
    BASE = "https://zernio.com/api/v1"

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "ZernioCollector":
        return cls(cls.BASE, config.ZERNIO_API_KEY, dry_run=dry_run)

    def __init__(self, *args, profiles: dict | None = None, ad_accounts: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        raw = profiles if profiles is not None else config.pairs(config.ZERNIO_PROFILE_PRODUCTS)
        self.profiles = {k: v for k, v in raw.items() if v in ("pp", "kp", "ordris", "hnh", "group")}
        raw_ads = ad_accounts if ad_accounts is not None else config.pairs(config.ZERNIO_AD_ACCOUNTS)
        self.ad_accounts = {k: v for k, v in raw_ads.items() if v in AD_PLATFORMS}

    @property
    def configured(self) -> bool:
        return bool(self._token and self.profiles)

    def pull(self, since: date, until: date, *, tier: str = "daily") -> PullResult:
        notes: list = []
        rows: list = []
        span = {"fromDate": since.isoformat(), "toDate": until.isoformat()}
        profiles = self.profiles or ({"{profileId}": "group"} if self.dry_run else {})
        totals: dict[tuple[date, str, str], Decimal] = defaultdict(Decimal)     # two profiles of one product add up
        for profile_id, product in profiles.items():
            body = self.get_json("/analytics/daily-metrics", params={"profileId": profile_id, **span, "attribution": "received"})
            for day in (body or {}).get("dailyData") or [] if isinstance(body, dict) else []:
                when = parse_day(str(day.get("date") or "")) if isinstance(day, dict) else None
                if when is None:
                    continue
                posts = number(day.get("postCount"))
                if posts is not None:
                    totals[(when, product, "posts")] += posts
                for metric, value in metric_values(day.get("metrics") or {}).items():
                    totals[(when, product, metric)] += value

            body = self.get_json("/accounts/follower-stats",
                                 params={"profileId": profile_id, **span, "granularity": "daily"})
            if isinstance(body, dict):
                rows.extend(self.follower_rows(body, product))

            post_from = (until - timedelta(days=POST_DAYS)).isoformat()
            seen_posts: dict[str, dict] = {}            # by id: a post read twice across a page boundary counts once
            for page in range(1, MAX_PAGES + 1):
                body = self.get_json("/analytics", params={"profileId": profile_id, "fromDate": post_from,
                                                           "page": page, "limit": POST_PAGE})
                if not isinstance(body, dict):
                    break
                for post in body.get("posts") or []:
                    if isinstance(post, dict) and post.get("_id"):
                        seen_posts[str(post["_id"])] = post
                pages = (body.get("pagination") or {}).get("pages")
                if not isinstance(pages, int) or page >= pages:
                    break
            else:
                notes.append(f"posts for {profile_id[:24]}: stopped after {MAX_PAGES} pages")
            rows.extend(self.post_rows(list(seen_posts.values()), product))

        for (when, product, metric), value in sorted(totals.items()):
            rows.append(SnapshotRow(when, product, metric, value))
        for account_id, platform in self.ad_accounts.items():
            body = self.get_json("/ads/timeline", params={"accountId": account_id, **span})
            rows.extend(self.spend_rows(body, account_id, platform, notes))
        replace = [Replace(("posts",) + POST_METRICS, since, until, ""),
                   Replace(("followers", "followers_new"), since, until, "account:%"),
                   Replace(POST_METRICS, until - timedelta(days=POST_DAYS), until, "post:%")]
        return self.result(rows, notes, replace if self.profiles else [])

    @staticmethod
    def follower_rows(body: dict, product: str) -> list[SnapshotRow]:
        rows = []
        accounts = {str(a.get("_id")): a for a in body.get("accounts") or [] if isinstance(a, dict)}
        for account_id, series in (body.get("stats") or {}).items():
            account = accounts.get(str(account_id), {})
            handle = str(account.get("username") or account_id)[:80]
            platform = str(account.get("platform") or "unknown")[:20]
            dimension = f"account:{platform}:{handle}"
            points = sorted(((parse_day(str(p.get("date") or "")), number(p.get("followers")))
                             for p in series or [] if isinstance(p, dict)), key=lambda p: (p[0] is None, p[0]))
            previous = None
            for when, followers in points:
                if when is None or followers is None:
                    continue
                rows.append(SnapshotRow(when, product, "followers", followers, dimension, {"account_id": account_id}))
                if previous is not None:
                    rows.append(SnapshotRow(when, product, "followers_new", followers - previous, dimension))
                previous = followers
        return rows

    @staticmethod
    def post_rows(posts: list, product: str) -> list[SnapshotRow]:
        rows = []
        for post in posts:
            if not isinstance(post, dict) or post.get("isAd"):
                continue
            when = london_date(post.get("publishedAt"))
            post_id = str(post.get("_id") or "")[:60]
            if when is None or not post_id:
                continue
            meta = {"url": str(post.get("platformPostUrl") or "")[:300], "platform": str(post.get("platform") or "")[:20],
                    "external": bool(post.get("isExternal"))}
            for metric, value in metric_values(post.get("analytics") or {}).items():
                rows.append(SnapshotRow(when, product, metric, value, f"post:zernio:{post_id}", meta))
        return rows

    @staticmethod
    def spend_rows(body, account_id: str, platform: str, notes: list) -> list[SpendRow]:
        rows = []
        for row in (body or {}).get("rows") or [] if isinstance(body, dict) else []:
            if not isinstance(row, dict):
                continue
            when = parse_day(str(row.get("date") or ""))
            currency = str(row.get("currency") or "GBP").upper()
            if when is None:
                continue
            if currency != "GBP":
                notes.append(f"{platform} spend on {when} is in {currency[:3]}: left out (only GBP is counted)")
                continue
            rows.append(SpendRow(date=when, platform=platform, account_id=account_id[:60], campaign_id="account",
                                 campaign_name="", product="group", spend_pence=pounds_to_pence(row.get("spend")) or 0,
                                 impressions=int(number(row.get("impressions")) or 0),
                                 clicks=int(number(row.get("clicks")) or 0),
                                 conversions=number(row.get("conversions")) or 0,
                                 raw={"via": "zernio", **{k: row.get(k) for k in ("date", "currency", "spend", "impressions",
                                                                                  "clicks", "conversions")}}))
        return rows

    def probe(self) -> ConnectorResult:
        result = self.request("GET", "/accounts")
        if not result.dry_run and isinstance(result.data, dict):
            accounts = result.data.get("accounts") or []
            result.data = {"ok": True, "accounts": len(accounts) if isinstance(accounts, list) else None}
        return result

