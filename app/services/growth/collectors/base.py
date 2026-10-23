"""The collector base: BaseConnector's transport, timeout, dry run, ConnectorError and redacted plans, for reading.

    class BrevoCollector(MetricCollector):
        source = "brevo"; env_names = ("BREVO_API_KEY_HQ", "BREVO_LIST_PRODUCTS"); window_days = 7
        def pull(self, since, until) -> PullResult: ...     # rows: SnapshotRow | SpendRow | PurchaseRow
        def probe(self) -> ConnectorResult: ...             # one cheap authenticated call, for the Test button

Every request a collector makes (or, in dry run, would make) is kept in `self.plans` with its credential headers
redacted, so a dry run returns the plans and no rows, and a live run can show what it asked. Credentials are read from
app.config at construction and never logged: a platform's error text is passed through scrub(), which blanks the
collector's own credentials and anything shaped like a key, and drops NUL and the other control characters Postgres
or a page would choke on, before it reaches a ConnectorError, a metric_runs row, an audit row or a page.

Rows that share a key: the store refuses two rows with one (date, product, metric, dimension) and different values,
so a collector whose platform can answer two rows for one key (www and apex versions of a page, two campaigns with one
name, a code in two cases, two long values that share their first DIMENSION_MAX characters) adds them up first, with
add_up() or its own sums. A pull that answers for a whole window of some metrics says so in PullResult.replace, and the
store clears those rows before writing the new answer, so a row whose key moved (a list remapped to another product, a
query that dropped out of the top rows) does not stay beside its replacement.

Dry run (source_dry_run): the job's --dry-run, or CONNECTORS_DRY_RUN=1 on the server. The per-product list form of
CONNECTORS_DRY_RUN and the product switches on Settings govern what is SENT to a product, not these reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .... import config
from ....models import DIMENSION_MAX
from ...connectors.base import BaseConnector, ConnectorError, ConnectorResult, _decode, _safe_exc

LONDON = ZoneInfo("Europe/London")
REDACTED_VALUE = "[redacted]"
BEARER_REDACTED = "Bearer [redacted]"
USER_AGENT = "GroupHQ-Growth/1.0"
MAX_PAGES = 50                      # a guard on every paged read: a runaway cursor stops here, with a note

_KEY_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]+"),       # Stripe
    re.compile(r"\bxkeysib-[A-Za-z0-9-]+"),                          # Brevo
    re.compile(r"\bnapi_[A-Za-z0-9]+"),                              # Neon
    re.compile(r"\bya29\.[A-Za-z0-9._-]+"),                          # a Google access token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
)


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def scrub(text: Any, secrets: tuple[str, ...] = ()) -> str:
    """Text safe to store and show: the given secrets and anything shaped like a key replaced by [redacted], and NUL and
    the other control characters (Postgres refuses NUL in a text column) replaced by a space."""
    out = str(text or "")
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, REDACTED_VALUE)
    for pattern in _KEY_PATTERNS:
        out = pattern.sub(REDACTED_VALUE, out)
    return _CONTROL.sub(" ", out)


def dimension_key(value: str | None) -> str:
    """A dimension as the store keys it: cut at DIMENSION_MAX characters."""
    return (value or "")[:DIMENSION_MAX]


@dataclass
class SnapshotRow:
    """One metric_snapshots row before the store adds the source."""
    date: date
    product: str
    metric: str
    value: Decimal | int | float
    dimension: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class SpendRow:
    """One ad_spend row: a platform campaign on one day."""
    date: date
    platform: str
    account_id: str
    campaign_id: str
    campaign_name: str
    product: str
    spend_pence: int = 0
    impressions: int = 0
    clicks: int = 0
    conversions: Decimal | int | float = 0
    conversions_sign_up: Decimal | int | float = 0
    conversions_lead: Decimal | int | float = 0
    conversions_purchase: Decimal | int | float = 0
    conversions_trial: Decimal | int | float = 0
    conversion_value_pence: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class PurchaseRow:
    """One product_purchases row as a product's GET .../purchases reports it."""
    product: str
    purchase_ref: str
    occurred_at: datetime
    kind: str
    plan: str = ""
    amount_pence: int = 0
    currency: str = "gbp"
    status: str = "paid"
    promo_code: str = ""
    affiliate_ref: str = ""
    first_touch: dict = field(default_factory=dict)
    last_touch: dict = field(default_factory=dict)
    account_key: str = ""
    stripe_customer_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Replace:
    """Snapshot rows a pull answers for in full: this source's rows of these metrics from since to until (and, when
    dimension_like is set, only dimensions LIKE it). The store deletes them in the same transaction before it writes
    the pull's rows, so the newest answer for those days is the whole answer."""
    metrics: tuple[str, ...]
    since: date
    until: date
    dimension_like: str | None = None


@dataclass
class PullResult:
    """What one pull produced. In dry run rows is empty and plans holds every request that would be made (headers
    redacted); notes are short remarks for the run page (a skipped value, a page guard, a thresholded report);
    replace names the snapshot rows the pull answers for in full (see Replace)."""
    rows: list = field(default_factory=list)
    plans: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    replace: list = field(default_factory=list)


def add_up(rows: list) -> list:
    """Snapshot rows that share a key once the dimension is cut, added up into one (count and money metrics only: a
    ratio or a position cannot be added, so its collector works it out from its parts). Other rows pass through.
    The first row's meta is kept, with thresholded carried over when any part was thresholded."""
    out: dict[tuple, SnapshotRow] = {}
    others = []
    for row in rows:
        if not isinstance(row, SnapshotRow):
            others.append(row)
            continue
        key = (row.date, row.product, row.metric, dimension_key(row.dimension))
        if key not in out:
            out[key] = SnapshotRow(row.date, row.product, row.metric, Decimal(str(row.value)), key[3], dict(row.meta or {}))
            continue
        if row.metric in ("search_ctr", "search_position"):
            raise ValueError(f"{row.metric} cannot be added up: work it out from clicks and impressions")
        merged = out[key]
        merged.value = Decimal(str(merged.value)) + Decimal(str(row.value))
        if (row.meta or {}).get("thresholded"):
            merged.meta["thresholded"] = True
    return list(out.values()) + others


def source_dry_run(dry_run: bool = False) -> bool:
    """Growth pulls are dry only when asked (--dry-run, a dry-run tick) or under the CONNECTORS_DRY_RUN=1 floor."""
    return bool(dry_run or config.CONNECTORS_DRY_RUN)


# ---- small parsers every collector shares ------------------------------------------------------------------
def london_date(value: datetime | int | float | str | None) -> date | None:
    """A UTC instant (datetime, unix seconds or an ISO string) as the Europe/London calendar date."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        moment = parse_instant(str(value))
        if moment is None:
            return None
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LONDON).date()


def parse_instant(value: str | None) -> datetime | None:
    """An ISO 8601 instant as naive UTC, or None."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def parse_day(value: str | None) -> date | None:
    text = (value or "").strip()
    if len(text) == 8 and text.isdigit():                 # GA4: 20261022
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def number(value: Any) -> Decimal | None:
    """A JSON number (or numeric string) as Decimal; None for anything else, booleans included."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def pounds_to_pence(value: Any) -> int | None:
    amount = number(value)
    if amount is None:
        return None
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def days_between(since: date, until: date) -> list[date]:
    if until < since:
        return []
    return [since + timedelta(days=i) for i in range((until - since).days + 1)]


def today_london() -> date:
    return datetime.now(timezone.utc).astimezone(LONDON).date()


def _platform_error(body: Any) -> tuple[str, str]:
    """(reason, message) out of the error shapes these platforms use: {"error": {...}} (Google, Stripe),
    {"errors": [{...}]} (Clerk), {"code", "message"} (Brevo, Neon), {"error": "..."} (Zernio)."""
    inner: Any = body
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):
            inner = body["error"]
        elif isinstance(body.get("errors"), list) and body["errors"] and isinstance(body["errors"][0], dict):
            inner = body["errors"][0]
    if not isinstance(inner, dict):
        return "", ""
    reason = ""
    for key in ("status", "code", "type", "reason"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            reason = value.strip()
            break
    message = ""
    for key in ("message", "long_message", "error_description", "error", "detail"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            message = value.strip()
            break
    if not message and isinstance(body, dict) and isinstance(body.get("raw"), str):
        message = body["raw"][:120]
    return reason, message


class MetricCollector(BaseConnector):
    """One platform, read only. Subclasses set source, tier, env_names and window_days, read their credentials in
    from_config(), and implement pull() and probe()."""
    source: str = ""
    tier: str = "daily"
    tiers: tuple[str, ...] = ("daily",)      # the job tiers that run it; GA4 also runs hourly for realtime users
    env_names: tuple[str, ...] = ()          # for the Sources screen: names only, never values
    window_days: int = 7                     # how far back a normal run re-pulls (the restatement window)
    product: str = "group"
    timeout_seconds: float = 10.0

    def __init__(self, base_url: str, token: str, *, dry_run: bool = False, timeout: float | None = None):
        super().__init__(base_url, token, dry_run=dry_run, timeout=timeout or self.timeout_seconds)
        self.plans: list[dict] = []

    def __repr__(self) -> str:   # never shows a credential
        return f"<{type(self).__name__} {self.source} dry_run={self.dry_run}>"

    # ---- what the Sources screen asks --------------------------------------------------------------------
    def missing_env(self) -> list[str]:
        """The env names this collector needs that are empty on this server (names only)."""
        return [name for name in self.env_names if not str(getattr(config, name, "") or "").strip()]

    def secrets(self) -> tuple[str, ...]:
        """Every credential value this collector holds, for scrub()."""
        return (self._token,)

    # ---- transport ---------------------------------------------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json", "User-Agent": USER_AGENT}

    def _plan_headers(self) -> dict:
        return {"Authorization": BEARER_REDACTED}

    def record(self, plan: dict) -> dict:
        self.plans.append(plan)
        return plan

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None,
                headers: dict | None = None) -> ConnectorResult:
        """One read, or in dry run the plan of it. Raises ConnectorError on any failure, its text scrubbed."""
        url = self.url(path)
        plan = self.record({"source": self.source, "method": method.upper(), "url": url, "params": params or None,
                            "json": json, "headers": self._plan_headers()})
        if self.dry_run:
            return ConnectorResult(ok=True, dry_run=True, plan=plan)
        if not self.configured:
            raise ConnectorError("not_configured", detail=f"{self.source}: missing {', '.join(self.missing_env()) or 'settings'}",
                                 plan=plan)
        try:
            resp = self.client().request(method.upper(), url, json=json, params=params,
                                         headers={**self._headers(), **(headers or {})})
        except httpx.TimeoutException as exc:
            raise ConnectorError("timeout", detail=f"no answer within {self.timeout:g} seconds", plan=plan) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError("unreachable", detail=scrub(_safe_exc(exc), self.secrets()), plan=plan) from exc
        body = _decode(resp)
        if resp.status_code >= 400:
            reason, message = _platform_error(body)
            reason = scrub(reason, self.secrets())[:80] or f"http_{resp.status_code}"
            raise ConnectorError(reason, status=resp.status_code, detail=scrub(message, self.secrets())[:300], plan=plan)
        return ConnectorResult(ok=True, status=resp.status_code, data=body, plan=plan)

    def get_json(self, path: str, *, params: Any = None) -> Any:
        """GET and return the body (None in dry run)."""
        result = self.request("GET", path, params=params)
        return None if result.dry_run else result.data

    # ---- the two operations ------------------------------------------------------------------------------
    def pull(self, since: date, until: date, *, tier: str = "daily") -> PullResult:   # pragma: no cover (subclasses)
        raise NotImplementedError

    def probe(self) -> ConnectorResult:   # pragma: no cover (subclasses)
        raise NotImplementedError

    def result(self, rows: list, notes: list | None = None, replace: list | None = None) -> PullResult:
        return PullResult(rows=[] if self.dry_run else rows, plans=list(self.plans), notes=list(notes or []),
                          replace=[] if self.dry_run else list(replace or []))

    # BaseConnector's grant/revoke/lookup are for products we send to; a collector never sends.
    def grant(self, *args, **kwargs):   # pragma: no cover
        raise ConnectorError("unsupported", detail="a growth collector only reads")

    revoke = grant
    lookup = grant


__all__ = ["BEARER_REDACTED", "ConnectorError", "ConnectorResult", "LONDON", "MAX_PAGES", "MetricCollector",
           "PullResult", "PurchaseRow", "REDACTED_VALUE", "Replace", "SnapshotRow", "SpendRow", "add_up", "days_between",
           "dimension_key", "london_date", "number", "parse_day", "parse_instant", "pounds_to_pence", "scrub",
           "source_dry_run", "today_london"]
