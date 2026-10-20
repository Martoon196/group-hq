"""Shared plumbing for the four product connectors.

Every connector talks to one product's server-to-server API with a bearer token and the header
X-Partner-Client: grouphq, a 10 second timeout and no retries of its own (the recompute job retries on
its next run through the connector_calls outbox). Failures come back as ConnectorError carrying a short
reason code, the HTTP status and the decoded body, never as a bare httpx exception. Tokens are never
logged, never stored and never appear in a recorded plan: the Authorization header in a plan reads
"Bearer [redacted]".

Dry run: request() returns a ConnectorResult with dry_run=True and the plan (method, URL, query, body
and redacted headers) instead of sending anything, so every connector can be rehearsed end to end
without a product being reachable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from ..keys import normalise_email

if TYPE_CHECKING:   # pragma: no cover
    from ...models import EntitlementGrant, Person

CLIENT_HEADER = "grouphq"
TIMEOUT_SECONDS = 10.0
MAX_BODY_CHARS = 4000
REDACTED = "Bearer [redacted]"
USER_AGENT = "GroupHQ/1.0"

__all__ = ["BaseConnector", "ConnectorError", "ConnectorResult", "CLIENT_HEADER", "REDACTED", "TIMEOUT_SECONDS",
           "iso_utc", "normalise_email"]


def iso_utc(value: datetime | date | None) -> str | None:
    """A naive UTC datetime (or a date) as 2027-10-01T00:00:00Z, the one form every product accepts."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime(value.year, value.month, value.day).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ConnectorError(Exception):
    """A call that did not do what was asked. `reason` is a short code (timeout, unreachable,
    not_configured, paid_subscription, http_502, ...) the recompute job keys its handling on."""

    def __init__(self, reason: str, *, status: int | None = None, detail: str = "", body: Any = None,
                 plan: dict | None = None):
        self.reason = (reason or "failed")[:80]
        self.status = status
        self.detail = (detail or "")[:500]
        self.body = body
        self.plan = plan
        super().__init__(self.summary())

    def summary(self) -> str:
        text = self.reason
        if self.status is not None:
            text += f" (HTTP {self.status})"
        if self.detail:
            text += f": {self.detail}"
        return text

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.reason, "status": self.status, "detail": self.detail, "body": self.body}


@dataclass
class ConnectorResult:
    """What a connector call produced. `remote_ref` is the product's id for the grant (stored on the
    grant row); `links` are person ids the product told us (pp_account_id, kp_user_id, ...) and are
    filled into the person row when empty; `noop` says why nothing needed sending."""
    ok: bool = True
    status: int | None = None
    data: Any = None
    remote_ref: str | None = None
    links: dict = field(default_factory=dict)
    noop: str = ""
    dry_run: bool = False
    plan: dict | None = None

    def as_dict(self) -> dict:
        out: dict = {"ok": self.ok}
        if self.status is not None:
            out["status"] = self.status
        if self.data is not None:
            out["data"] = self.data
        if self.remote_ref:
            out["remote_ref"] = self.remote_ref
        if self.links:
            out["links"] = self.links
        if self.noop:
            out["noop"] = self.noop
        if self.dry_run:
            out["dry_run"] = True
        if self.plan:
            out["plan"] = self.plan
        return out


def _decode(resp: httpx.Response) -> Any:
    """The JSON body, or {"raw": text} when a product (or the proxy in front of it) answered something else."""
    try:
        return resp.json()
    except ValueError:
        return {"raw": (resp.text or "")[:MAX_BODY_CHARS]}


def _unwrap(body: Any) -> dict:
    """The dict that carries reason/error/message: FastAPI puts a dict detail under "detail"."""
    if not isinstance(body, dict):
        return {}
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail
    return body


def _reason(body: Any, status: int) -> str:
    inner = _unwrap(body)
    for key in ("reason", "error", "code"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    detail = inner.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:80]
    return f"http_{status}"


def _detail(body: Any) -> str:
    inner = _unwrap(body)
    for key in ("message", "detail", "reason", "error"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    if isinstance(body, dict) and isinstance(body.get("raw"), str):
        return body["raw"][:120]
    return ""


def _safe_exc(exc: BaseException) -> str:
    """An httpx transport error as text. These carry the URL at most, never a header."""
    return f"{type(exc).__name__}: {exc}"[:300]


class BaseConnector:
    """One product. Subclasses set `product`, implement grant/revoke/lookup and a from_config()."""
    product: str = ""

    def __init__(self, base_url: str, token: str, *, dry_run: bool = False, timeout: float = TIMEOUT_SECONDS):
        self.base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self.dry_run = bool(dry_run)
        self.timeout = timeout
        self._client: httpx.Client | None = None

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "BaseConnector":   # pragma: no cover (subclasses override)
        raise NotImplementedError

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self._token)

    def __repr__(self) -> str:   # never shows the token
        return f"<{type(self).__name__} {self.base_url or '(no base url)'} dry_run={self.dry_run}>"

    # ---- transport ----------------------------------------------------------------
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self.timeout), follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "BaseConnector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "X-Partner-Client": CLIENT_HEADER,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _plan_headers(self) -> dict:
        """The headers as a recorded plan shows them: every credential redacted."""
        return {"Authorization": REDACTED, "X-Partner-Client": CLIENT_HEADER}

    def url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else self.base_url + path

    def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> ConnectorResult:
        """One HTTP call, or in dry run the plan of it. Raises ConnectorError on any failure."""
        url = self.url(path)
        plan = {
            "product": self.product, "method": method.upper(), "url": url,
            "params": params or None, "json": json,
            "headers": self._plan_headers(),
        }
        if self.dry_run:
            return ConnectorResult(ok=True, dry_run=True, plan=plan)
        if not self.configured:
            raise ConnectorError("not_configured", detail=f"{self.product}: base URL or token is not set", plan=plan)
        try:
            resp = self.client().request(method.upper(), url, json=json, params=params, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ConnectorError("timeout", detail=f"no answer within {self.timeout:g} seconds", plan=plan) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError("unreachable", detail=_safe_exc(exc), plan=plan) from exc
        body = _decode(resp)
        if resp.status_code >= 400:
            raise ConnectorError(_reason(body, resp.status_code), status=resp.status_code, detail=_detail(body),
                                 body=body, plan=plan)
        return ConnectorResult(ok=True, status=resp.status_code, data=body, plan=plan)

    # ---- the three operations every connector offers ----------------------------------
    def grant(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "",
              extra: dict | None = None) -> ConnectorResult:
        raise NotImplementedError

    def revoke(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "") -> ConnectorResult:
        raise NotImplementedError

    def lookup(self, email: str) -> ConnectorResult:
        raise NotImplementedError

    # ---- helpers for subclasses -------------------------------------------------------
    @staticmethod
    def data_dict(result: ConnectorResult) -> dict:
        """The response body as a dict (an empty one in dry run or for a list body)."""
        return result.data if isinstance(result.data, dict) else {}

    @staticmethod
    def refused(result: ConnectorResult) -> ConnectorError | None:
        """A 2xx answer that still says no ({"ok": false, "reason": ...}) becomes a ConnectorError."""
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("ok") is False:
            return ConnectorError(_reason(data, result.status or 200), status=result.status, detail=_detail(data),
                                  body=data, plan=result.plan)
        return None

    def unsupported(self, grant: "EntitlementGrant") -> ConnectorError:
        return ConnectorError("unsupported", detail=f"{self.product} cannot hold a {grant.kind} grant")
