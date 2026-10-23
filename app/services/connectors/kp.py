"""Key Properties connector: the plan's Phase 2 contract, as built on the portal's grouphq-connector branch (docs/connectors.md).

    POST  {KP_BASE_URL}/api/partner/sourcers        {email, name, company, pp_account_id, fee_share_pct, source: "mentee"}
          creates a sourcer, or links the existing invite-only user with that email to the PP account
    PATCH {KP_BASE_URL}/api/partner/sourcers/{id}   {fee_share_pct, active}
    GET   {KP_BASE_URL}/api/partner/sourcers?email=  the flat row, or 404 not_found when there is no sourcer
    GET   {KP_BASE_URL}/api/partner/sourcers?limit=&offset=   (no email) one page of sourcers, newest first, for
          the existing-users import: {ok, sourcers: [{id, email, name, company, source, pp_account_id, active,
          created_at, deals}], total, limit, offset}; admins are never listed

Auth: Bearer KP_PARTNER_TOKEN (GROUPHQ_PARTNER_TOKEN on the portal side, keyed by client) and
X-Partner-Client: grouphq. A grant creates or re-activates the sourcer; a revoke deactivates it
(the user and their deals stay). fee_share_pct is sent only when the programme template item carries it:
the default is the portal's own (schema default 50) until Martin signs the figure off.

Contract 4 of the mentee-earnings packet (docs/connectors.md): the POST and the PATCH both carry pp_account_id when
Group HQ knows it, and the portal sets it on the user row only while that is empty; a row already linked to a
different Property Predator account is refused with 409 pp_account_mismatch, which the recompute records as a failed
grant with a plain message and never retries on its own. The portal's answer carries the pp_account_id it holds,
which is kept on the grant (ConnectorResult.info) so the recompute knows whether the link has reached Key Properties.
The id is PP's users.id, a UUID string, sent and compared as text. link() is the PATCH that carries pp_account_id and
nothing else, for an active sourcer whose row has not confirmed it: it never re-opens a sourcer or resets a share
(the portal's PATCH accepts pp_account_id on its own). When another Key Properties login already holds the PP account,
the portal refuses the link: 409 pp_account_in_use on POST (nothing created) and on a link-only PATCH, while a PATCH
that also opens the sourcer or sets its share is applied without the link and answers 200 with
link_refused {reason, message}. That refusal is read into ConnectorResult.info["link_refused"] (link_refusal()) and the
recompute shows it on the grant and the earning links panel (entitlements.IN_USE_ERROR).
GET ?email= also answers fee_share_pct, active, pp_linked, login_url, deals_submitted, earned_pence and
pipeline_pence, which the earning links panel shows (sourcer_status).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ... import config
from .base import (BaseConnector, ConnectorError, ConnectorResult, _int_or_none, account_ref, normalise_email, route_missing,
                   safe_url, short_text, whole_number)

if TYPE_CHECKING:   # pragma: no cover
    from ...models import EntitlementGrant, Person


def _first(data: Any) -> dict:
    """The sourcer record out of whatever shape the portal answers: a dict, {sourcer: {...}},
    {sourcers: [...]}, or a bare list."""
    if isinstance(data, dict):
        for key in ("sourcer", "user"):
            if isinstance(data.get(key), dict):
                return data[key]
        for key in ("sourcers", "users", "results", "items"):
            if isinstance(data.get(key), list):
                return _first(data[key])
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _company(person: "Person") -> str:
    try:
        links = list(person.organisation_links)
    except Exception:   # noqa: BLE001  (a detached person simply has no company to send)
        return ""
    for link in links:
        if link.organisation is not None and link.organisation.name:
            return link.organisation.name[:200]
    return ""


PLACEHOLDER = "{sourcer_id}"       # stands in for the id in a dry-run plan when none is known yet
MISMATCH_REASON = "pp_account_mismatch"
IN_USE_REASON = "pp_account_in_use"     # the PP account is already on another Key Properties login
LINK_REFUSALS = (MISMATCH_REASON, IN_USE_REASON)


def link_refusal(data: Any) -> dict | None:
    """What a 200 answer says about a link the portal left off: contract 4's PATCH applies a re-grant or extension
    without the link when another login already holds the PP account, and answers the row plus
    link_refused {reason: "pp_account_in_use", message}. None when the answer carries no such refusal."""
    record = data if isinstance(data, dict) else {}
    refused = record.get("link_refused")
    if not isinstance(refused, dict):
        refused = _first(data).get("link_refused")
    if not isinstance(refused, dict):
        return None
    reason = short_text(refused.get("reason"), 80) or short_text(refused.get("error"), 80)
    if not reason:
        return None
    out = {"reason": reason}
    message = short_text(refused.get("message"), 300)
    if message:
        out["message"] = message
    return out


def share_pct(value: Any) -> str | None:
    """The sourcer's fee share as the portal answers it (a number, or text such as "50.00" from a NUMERIC column) shown
    as "50" or "37.5"; None when it is missing or not a percentage from 0 to 100."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number > 100:
        return None
    text = f"{number.normalize():f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def sourcer_info(record: dict) -> dict:
    """What contract 4's lookup answers that the earning links panel shows. Missing or malformed values are left out;
    a login link that is not a plain http(s) URL is dropped."""
    info: dict = {"sourcer": True}
    sourcer_id = whole_number(record.get("id"))
    if sourcer_id is not None:
        info["id"] = sourcer_id
    share = share_pct(record.get("fee_share_pct"))
    if share is not None:
        info["fee_share_pct"] = share
    for key in ("active", "pp_linked"):
        if isinstance(record.get(key), bool):
            info[key] = record[key]
    login = safe_url(record.get("login_url"))
    if login:
        info["login_url"] = login
    for key in ("deals_submitted", "earned_pence", "pipeline_pence"):
        value = whole_number(record.get(key))
        if value is not None:
            info[key] = value
    return info


LIST_MAX = 200                     # the most rows one list call asks for
_LIST_KEYS = ("sourcers", "users", "results", "items", "rows")


def _count(value: Any) -> int | None:
    """A deal count however the portal spells it: a number, a list of deals, or {total|count: n}."""
    if isinstance(value, bool):
        return None
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return _count(value.get("total", value.get("count")))
    return _int_or_none(value)


def sourcer_row(raw: Any) -> dict | None:
    """One listed sourcer as the import page shows it, or None when it carries no usable email (such a row
    cannot be ticked: the register keys people on their email)."""
    if not isinstance(raw, dict):
        return None
    email = normalise_email(raw.get("email") if isinstance(raw.get("email"), str) else "")
    if not email or len(email) > 254:   # never cut an address short: a cut one could be someone else's
        return None
    sourcer_id = raw.get("id")
    deals = next((raw[k] for k in ("deals", "deal_count", "deals_count") if k in raw), None)
    active = raw.get("active")
    return {
        "id": None if isinstance(sourcer_id, bool) else _int_or_none(sourcer_id),
        "email": email,
        "name": str(raw.get("name") or "").strip()[:120],
        "company": str(raw.get("company") or "").strip()[:200],
        "source": str(raw.get("source") or "").strip()[:40],
        "active": active if isinstance(active, bool) else None,
        "deals": _count(deals),
        "pp_account_id": account_ref(raw.get("pp_account_id")),
        "created_at": str(raw.get("created_at") or "")[:40] or None,
    }


class KPConnector(BaseConnector):
    product = "kp"
    SOURCERS = "/api/partner/sourcers"

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "KPConnector":
        return cls(config.KP_BASE_URL, config.KP_PARTNER_TOKEN, dry_run=dry_run)

    def _with_sourcer(self, result: ConnectorResult) -> ConnectorResult:
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        record = _first(result.data)
        sourcer_id = record.get("id") if record else None
        if sourcer_id is not None:
            as_int = _int_or_none(sourcer_id)
            if as_int is None or isinstance(sourcer_id, bool):
                raise ConnectorError("bad_response", status=result.status, detail="kp: sourcer id is not a whole number",
                                     body=result.data, plan=result.plan)
            result.remote_ref = str(as_int)
            result.links = {"kp_user_id": as_int}
        linked = account_ref(record.get("pp_account_id")) if record else None
        if linked is not None:
            result.info = {"pp_account_id": linked}
        refused = link_refusal(result.data)
        if refused is not None:
            result.info = {**result.info, "link_refused": refused}
        return result

    def _sourcer_path(self, sourcer_id: str) -> str:
        ref = str(sourcer_id)
        return f"{self.SOURCERS}/{ref if ref == PLACEHOLDER else quote(ref, safe='')}"

    def grant(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "",
              extra: dict | None = None) -> ConnectorResult:
        if grant.kind != "sourcer_link":
            raise self.unsupported(grant)
        extra = extra or {}
        fee = _int_or_none(extra.get("fee_share_pct"))
        account = account_ref(person.pp_account_id)
        if grant.remote_ref:
            patch: dict = {"active": True}
            if fee is not None:
                patch["fee_share_pct"] = fee
            if account is not None:
                patch["pp_account_id"] = account        # set by the portal only while its row has none
            return self._with_sourcer(self.request("PATCH", self._sourcer_path(grant.remote_ref), json=patch))
        payload: dict = {
            "email": normalise_email(person.person_key),
            "name": person.name or "",
            "company": _company(person),
            "pp_account_id": account,
            "source": (grant.value or "mentee").strip().lower() or "mentee",
        }
        if fee is not None:
            payload["fee_share_pct"] = fee
        return self._with_sourcer(self.request("POST", self.SOURCERS, json=payload))

    def link(self, grant: "EntitlementGrant", person: "Person") -> ConnectorResult:
        """Contract 4's link on its own: PATCH {pp_account_id} for a sourcer the grant already holds. Nothing else is
        sent, so a sourcer switched off or given another share in the portal by hand stays exactly as it is."""
        account = account_ref(person.pp_account_id)
        if grant.kind != "sourcer_link" or not grant.remote_ref or account is None:
            raise ConnectorError("unsupported", detail="kp: a link needs the sourcer id and the person's PP account id")
        result = self._with_sourcer(self.request("PATCH", self._sourcer_path(grant.remote_ref), json={"pp_account_id": account}))
        refused = (result.info or {}).get("link_refused")
        if refused:
            # The link was all this call asked for (the portal answers 409 for that today): a refusal is a failure.
            raise ConnectorError(refused["reason"], status=result.status, detail=refused.get("message", ""),
                                 body=result.data, plan=result.plan)
        return result

    def _sourcer_id(self, grant: "EntitlementGrant", person: "Person") -> str | None:
        """The portal's id for the person, or None when it has no sourcer with that email. The portal answers
        the lookup with 404 not_found in that case (and for an admin's email), which is "nothing to do"
        here, not a failure."""
        if grant.remote_ref:
            return grant.remote_ref
        if self.dry_run:
            return PLACEHOLDER
        try:
            found = self.lookup(person.person_key)
        except ConnectorError as error:
            if error.status == 404:
                return None
            raise
        return found.remote_ref

    def revoke(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "") -> ConnectorResult:
        if grant.kind != "sourcer_link":
            raise self.unsupported(grant)
        sourcer_id = self._sourcer_id(grant, person)
        if not sourcer_id:
            return ConnectorResult(ok=True, noop="no sourcer with that email: nothing to deactivate")
        try:
            return self._with_sourcer(self.request("PATCH", self._sourcer_path(sourcer_id), json={"active": False}))
        except ConnectorError as error:
            if error.status == 404:
                return ConnectorResult(ok=True, status=404, data=error.body, noop="sourcer already gone", plan=error.plan)
            raise

    def lookup(self, email: str) -> ConnectorResult:
        return self._with_sourcer(self.request("GET", self.SOURCERS, params={"email": normalise_email(email)}))

    def sourcer_status(self, email: str) -> ConnectorResult:
        """Contract 4's GET, for the earning links panel. In dry run only the plan; live, result.info is sourcer_info()
        of the row, or {"sourcer": False} when the portal refuses with 404 (no sourcer with that email). A 404 that is
        not the portal's own refusal (route_missing) is raised like any other failure."""
        try:
            result = self.lookup(email)
        except ConnectorError as error:
            if error.status == 404 and not route_missing(error):
                return ConnectorResult(ok=True, status=404, data=error.body, plan=error.plan, info={"sourcer": False})
            raise
        if not result.dry_run:
            result.info = sourcer_info(_first(result.data))
        return result

    def list_sourcers(self, limit: int = 100, offset: int = 0) -> ConnectorResult:
        """One page of the portal's sourcers, newest first: GET /api/partner/sourcers?limit=&offset= with no email
        (the portal answers at most 500 a page; this asks for at most LIST_MAX). In dry run
        the result carries the planned call and no rows, and nothing is sent. Live, result.data is
        {"sourcers": [sourcer_row, ...], "total": int or None, "limit": int, "offset": int}; a row without an
        email is left out. A body with no list in it is a bad_response, never an empty page."""
        limit = max(1, min(_int_or_none(limit) or 1, LIST_MAX))
        offset = max(0, _int_or_none(offset) or 0)
        result = self.request("GET", self.SOURCERS, params={"limit": limit, "offset": offset})
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        body = result.data
        raw_rows = body if isinstance(body, list) else None
        if isinstance(body, dict):
            raw_rows = next((body[k] for k in _LIST_KEYS if isinstance(body.get(k), list)), None)
        if raw_rows is None:
            raise ConnectorError("bad_response", status=result.status, detail="kp: the sourcer list is not a list",
                                 body=body, plan=result.plan)
        rows = [row for row in (sourcer_row(raw) for raw in raw_rows) if row is not None]
        total = _int_or_none(body.get("total", body.get("count"))) if isinstance(body, dict) else None
        result.data = {"sourcers": rows, "total": total, "limit": limit, "offset": offset}
        return result
