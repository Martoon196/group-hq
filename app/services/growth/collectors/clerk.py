"""Ordris sign-ups counted in Clerk: a stopgap until Ordris's own GET /api/partner/grouphq/metrics answers.

Not recommended (DASHBOARD-SPEC 3.13): CLERK_SECRET_KEY_ORDRIS is Clerk's full backend key for Ordris, able to delete
and alter users, and shared with Ordris on Render. The source starts Off like every other, is absent from render.yaml
on purpose, and should be switched off and the key removed from Group HQ the day the Ordris metrics endpoint answers
(product_ordris writes the same sign_ups from Ordris's own users table).

    GET https://api.clerk.com/v1/users/count?created_at_after=<ms>&created_at_before=<ms>   -> {"total_count": N}

one call per London day of the window, the bounds in unix milliseconds ([midnight - 1 ms, next midnight), so a user
created on the stroke of midnight counts once); sign_ups for ordris on "".
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .... import config
from ...connectors.base import ConnectorError, ConnectorResult
from .base import LONDON, MetricCollector, PullResult, SnapshotRow, days_between, number


def day_bounds_ms(day: date) -> tuple[int, int]:
    start = datetime.combine(day, time(0), tzinfo=LONDON)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=LONDON)
    return int(start.timestamp() * 1000) - 1, int(end.timestamp() * 1000)


class ClerkOrdrisCollector(MetricCollector):
    source = "clerk_ordris"
    env_names = ("CLERK_SECRET_KEY_ORDRIS",)
    window_days = 7
    BASE = "https://api.clerk.com/v1"

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "ClerkOrdrisCollector":
        return cls(cls.BASE, config.CLERK_SECRET_KEY_ORDRIS, dry_run=dry_run)

    def pull(self, since: date, until: date, *, tier: str = "daily") -> PullResult:
        rows = []
        for day in days_between(since, until):
            after, before = day_bounds_ms(day)
            body = self.get_json("/users/count", params={"created_at_after": after, "created_at_before": before})
            if body is None:
                continue
            count = number(body.get("total_count")) if isinstance(body, dict) else None
            if count is None:
                raise ConnectorError("bad_response", detail="clerk: users/count gave no total_count")
            rows.append(SnapshotRow(day, "ordris", "sign_ups", count, meta={"via": "clerk"}))
        return self.result(rows)

    def probe(self) -> ConnectorResult:
        return self.request("GET", "/users/count")
