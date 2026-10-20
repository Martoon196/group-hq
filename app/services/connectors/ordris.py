"""Ordris connector: the plan's Phase 2 contract (CONTRACT PENDING, see docs/connectors.md).

    POST   {ORDRIS_BASE_URL}/api/partner/grouphq/entitlements   {email, plan, until, reason, external_ref}
    DELETE {ORDRIS_BASE_URL}/api/partner/grouphq/entitlements/{external_ref}
    GET    {ORDRIS_BASE_URL}/api/partner/grouphq/lookup?email=

Auth: Bearer ORDRIS_PARTNER_TOKEN (ORDRIS_PARTNER_TOKEN_GROUPHQ on the Ordris side) and
X-Partner-Client: grouphq. Ordris resolves the Clerk user by email and applies the plan to the
organisation its requireAuth rule picks (orgId ?? userId). The external_ref is grouphq:grant:<id> and
is unique on their side, so a repeated POST is an update, never a second entitlement.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from ... import config
from .base import BaseConnector, ConnectorError, ConnectorResult, iso_utc, normalise_email

if TYPE_CHECKING:   # pragma: no cover
    from ...models import EntitlementGrant, Person

PLANS = ("starter", "pro", "enterprise")


def external_ref(grant: "EntitlementGrant") -> str:
    return f"grouphq:grant:{grant.id}"


class OrdrisConnector(BaseConnector):
    product = "ordris"
    ENTITLEMENTS = "/api/partner/grouphq/entitlements"
    LOOKUP = "/api/partner/grouphq/lookup"

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "OrdrisConnector":
        return cls(config.ORDRIS_BASE_URL, config.ORDRIS_PARTNER_TOKEN, dry_run=dry_run)

    def _with_links(self, result: ConnectorResult) -> ConnectorResult:
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        data = self.data_dict(result)
        links = {}
        for ours, theirs in (("ordris_user_id", "user_id"), ("ordris_org_id", "org_id")):
            value = data.get(theirs)
            if value not in (None, ""):
                links[ours] = str(value)[:64]
        result.links = links
        return result

    def grant(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "",
              extra: dict | None = None) -> ConnectorResult:
        if grant.kind != "org_plan":
            raise self.unsupported(grant)
        plan = (grant.value or "").strip().lower()
        if plan not in PLANS:
            raise ConnectorError("unsupported", detail=f"ordris: unknown plan {grant.value!r}")
        ref = external_ref(grant)
        payload = {"email": normalise_email(person.person_key), "plan": plan, "until": iso_utc(grant.ends_at),
                   "reason": reason or ref, "external_ref": ref}
        result = self._with_links(self.request("POST", self.ENTITLEMENTS, json=payload))
        result.remote_ref = ref
        return result

    def revoke(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "") -> ConnectorResult:
        if grant.kind != "org_plan":
            raise self.unsupported(grant)
        ref = grant.remote_ref or external_ref(grant)
        try:
            result = self._with_links(self.request("DELETE", f"{self.ENTITLEMENTS}/{quote(ref, safe='')}"))
        except ConnectorError as error:
            if error.status == 404:
                return ConnectorResult(ok=True, status=404, data=error.body, noop="entitlement already gone",
                                       plan=error.plan)
            raise
        result.remote_ref = ref
        return result

    def lookup(self, email: str) -> ConnectorResult:
        return self._with_links(self.request("GET", self.LOOKUP, params={"email": normalise_email(email)}))
