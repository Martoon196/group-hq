"""Key Properties connector: the plan's Phase 2 contract (CONTRACT PENDING, see docs/connectors.md).

    POST  {KP_BASE_URL}/api/partner/sourcers        {email, name, company, pp_account_id, fee_share_pct, source: "mentee"}
          creates a sourcer, or links the existing invite-only user with that email to the PP account
    PATCH {KP_BASE_URL}/api/partner/sourcers/{id}   {fee_share_pct, active}
    GET   {KP_BASE_URL}/api/partner/sourcers?email=

Auth: Bearer KP_PARTNER_TOKEN (GROUPHQ_PARTNER_TOKEN on the portal side, keyed by client) and
X-Partner-Client: grouphq. A grant creates or re-activates the sourcer; a revoke deactivates it
(the user and their deals stay). fee_share_pct is sent only when the programme template item carries it:
the default is the portal's own (schema default 50) until Martin signs the figure off.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ... import config
from .base import BaseConnector, ConnectorError, ConnectorResult, _int_or_none, normalise_email

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
        if grant.remote_ref:
            patch: dict = {"active": True}
            if fee is not None:
                patch["fee_share_pct"] = fee
            return self._with_sourcer(self.request("PATCH", self._sourcer_path(grant.remote_ref), json=patch))
        payload: dict = {
            "email": normalise_email(person.person_key),
            "name": person.name or "",
            "company": _company(person),
            "pp_account_id": person.pp_account_id,
            "source": (grant.value or "mentee").strip().lower() or "mentee",
        }
        if fee is not None:
            payload["fee_share_pct"] = fee
        return self._with_sourcer(self.request("POST", self.SOURCERS, json=payload))

    def _sourcer_id(self, grant: "EntitlementGrant", person: "Person") -> str | None:
        if grant.remote_ref:
            return grant.remote_ref
        if self.dry_run:
            return PLACEHOLDER
        found = self.lookup(person.person_key)
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
