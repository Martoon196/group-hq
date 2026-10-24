"""Ordris connector: the plan's Phase 2 contract (CONTRACT PENDING, see docs/connectors.md).

    POST   {ORDRIS_BASE_URL}/api/partner/grouphq/entitlements   {email, plan, until, reason, external_ref}
    DELETE {ORDRIS_BASE_URL}/api/partner/grouphq/entitlements/{external_ref}
    GET    {ORDRIS_BASE_URL}/api/partner/grouphq/lookup?email=

Auth: Bearer ORDRIS_PARTNER_TOKEN (ORDRIS_PARTNER_TOKEN_GROUPHQ on the Ordris side) and
X-Partner-Client: grouphq. Ordris resolves the Clerk user by email and applies the plan to the
organisation its requireAuth rule picks (orgId ?? userId). The external_ref is grouphq:grant:<id> and
is unique on their side, so a repeated POST is an update, never a second entitlement.

The affiliate kind (contract 2 of the mentee-earnings packet, docs/connectors.md): the partner programme.

    POST {ORDRIS_BASE_URL}/api/partner/grouphq/partners   {email, name, external_ref}
         -> {ok: true, partner_id, referral_code, link, created}: Ordris finds or creates an active partner in the
            programme its ORDRIS_MENTEE_PROGRAMME_CODE names (the self-serve one by default); no Ordris login is needed
    GET  {ORDRIS_BASE_URL}/api/partner/grouphq/partners?email=
         -> {ok: true, partner: false} or {ok: true, partner: true, partner_id, referral_code, link, referrals,
            pending_pence, payable_pence, paid_pence}; when the answer also carries terms_accepted, terms_accepted_at or
            terms_version (not part of the contract), partner_terms() reads them and the panel shows the partner terms

There is no revoke: the programme is public and membership is permanent, so revoking an affiliate grant is a
recorded no-op and nothing is sent.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from ... import config
from .base import BaseConnector, ConnectorError, ConnectorResult, iso_utc, normalise_email, safe_url, short_text, whole_number
from .pp import AFFILIATE_KEPT

if TYPE_CHECKING:   # pragma: no cover
    from ...models import EntitlementGrant, Person

PLANS = ("starter", "pro", "enterprise")


def external_ref(grant: "EntitlementGrant") -> str:
    return f"grouphq:grant:{grant.id}"


def partner_terms(data: dict) -> dict:
    """Whether the partner has accepted Ordris's partner terms, when the answer says so. Contract 2 does not carry this;
    the Ordris partner record is gaining terms_accepted_at and terms_version, so an answer may add terms_accepted (a
    bool), terms_accepted_at (a timestamp, null while not accepted) and terms_version. An answer with none of them gives
    {} and the panel says nothing about terms."""
    out: dict = {}
    if isinstance(data.get("terms_accepted"), bool):
        out["terms_accepted"] = data["terms_accepted"]
    if "terms_accepted_at" in data:
        when = short_text(data.get("terms_accepted_at"), 40)
        if when:
            out["terms_accepted_at"] = when
        out.setdefault("terms_accepted", when is not None)
    version = short_text(data.get("terms_version"), 40)
    if version and "terms_accepted" in out:
        out["terms_version"] = version
    return out


def partner_info(data: dict) -> dict:
    """What the partner routes answer that is worth showing: the partner id, referral code and link, whether the
    partner was created just now, and (GET) the referral count, the three pence figures and the partner's status.
    Missing or malformed values are left out; a link that is not a plain http(s) URL is dropped."""
    info: dict = {}
    if isinstance(data.get("partner"), bool):
        info["partner"] = data["partner"]
    partner_id = data.get("partner_id")
    if not isinstance(partner_id, bool) and isinstance(partner_id, (int, str)):
        text = short_text(str(partner_id), 64)
        if text:
            info["partner_id"] = text
    code = short_text(data.get("referral_code"), 64)
    if code:
        info["referral_code"] = code
    link = safe_url(data.get("link"))
    if link:
        info["link"] = link
    if isinstance(data.get("created"), bool):
        info["created"] = data["created"]
    for key in ("referrals", "pending_pence", "payable_pence", "paid_pence"):
        value = whole_number(data.get(key))
        if value is not None:
            info[key] = value
    status = short_text(data.get("status"), 40)
    if status:
        info["status"] = status.lower()
    info.update(partner_terms(data))
    return info


class OrdrisConnector(BaseConnector):
    product = "ordris"
    ENTITLEMENTS = "/api/partner/grouphq/entitlements"
    LOOKUP = "/api/partner/grouphq/lookup"
    PARTNERS = "/api/partner/grouphq/partners"          # mentee earnings, contract 2

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

    def _partner(self, grant: "EntitlementGrant", person: "Person") -> ConnectorResult:
        ref = external_ref(grant)
        payload = {"email": normalise_email(person.person_key), "name": (person.name or "").strip()[:120], "external_ref": ref}
        result = self.request("POST", self.PARTNERS, json=payload)
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        result.info = partner_info(self.data_dict(result))
        result.remote_ref = result.info.get("partner_id") or ref
        return result

    def grant(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "",
              extra: dict | None = None) -> ConnectorResult:
        if grant.kind == "affiliate":
            return self._partner(grant, person)
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
        if grant.kind == "affiliate":
            # Public programme, permanent membership: nothing to take away and no route to call.
            return ConnectorResult(ok=True, noop=AFFILIATE_KEPT)
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

    def partner_status(self, email: str) -> ConnectorResult:
        """Contract 2's GET, for the earning links panel. In dry run only the plan; live, result.info is partner_info()
        of the answer, with partner false when the email is not a partner."""
        result = self.request("GET", self.PARTNERS, params={"email": normalise_email(email)})
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        result.info = partner_info(self.data_dict(result))
        result.info.setdefault("partner", False)
        return result
