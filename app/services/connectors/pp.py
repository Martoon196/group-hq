"""Property Predator connector: the plan's Phase 1 contract, exactly.

    POST {PP_BASE_URL}/api/partner/grouphq/comp     {email, tier, until, revert_to, reason}
         -> {ok, account_id, tier, comp_expires_at} or 409 {ok: false, reason: "paid_subscription"}
    POST {PP_BASE_URL}/api/partner/grouphq/revoke   {email, reason}
    GET  {PP_BASE_URL}/api/partner/grouphq/account?email=
         -> {account_id, tier, subscription_status, comp_expires_at}

Auth: X-Service-Token: PP_SERVICE_TOKEN (SERVICE_TOKEN_GROUPHQ on the PP side, read by
job_auth.token_matches like every other SERVICE_TOKEN_* consumer) and X-Partner-Client: grouphq. The
plan's Phase 1 wording said Bearer; the PP implementation settled on PP's existing header convention, so
both are sent and the recorded plan redacts both.
A Scout (free tier) comp is a no-op recorded locally: there is nothing to comp. A 409 paid_subscription
is raised as ConnectorError("paid_subscription") and the recompute job turns it into a blocked grant that
is never retried automatically (the person is paying; comping them would stop their revenue).

The flag kind (kpuk.submit, ordris_active) belongs to Phase 3 (plan: POST .../flags {email, key, until});
it is implemented here so the mentorship template works in dry run, and marked contract pending in
docs/connectors.md.

The affiliate kind (contract 1 of the mentee-earnings packet, docs/connectors.md):

    POST {PP_BASE_URL}/api/partner/grouphq/affiliate   {email, reason}
         -> {ok: true, account_id, code, link, created} (created false when already an affiliate) or
            404 {ok: false, reason: "no_account"}
    GET  {PP_BASE_URL}/api/partner/grouphq/affiliate?email=
         -> {ok: true, account_id, affiliate: false} or {ok: true, account_id, affiliate: true, code, link,
            referred_accounts, commission_due_pence, commission_paid_pence}

The programme is public and membership is permanent, so there is no revoke route: revoking an affiliate grant is a
recorded no-op (AFFILIATE_KEPT) and nothing is sent. Every PP route answers account_id as PP's users.id, a UUID string;
it is kept as text (people.pp_account_id is VARCHAR since 0008) and is what Key Properties is sent (contract 4). The code and link come back in ConnectorResult.info, which the
recompute keeps on the grant for display. PP's rates, attribution and payouts are PP's; Group HQ only enrols.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ... import config
from .base import (BaseConnector, ConnectorError, ConnectorResult, account_ref, iso_utc, normalise_email, safe_url, short_text,
                   whole_number)

if TYPE_CHECKING:   # pragma: no cover
    from ...models import EntitlementGrant, Person

# Tier vocabulary (docs/shared-keys.md) to PP's keys. PP keys pass through unchanged.
TIER_KEYS = {"scout": "free", "hunter": "pro_buyer", "apex": "pro_investor", "business": "pro_business"}
PP_TIERS = ("free", "pro_buyer", "pro_investor", "pro_business")
SCOUT_NOOP = "Scout is the free tier: nothing to comp, recorded locally"
NOT_ON_COMP_REASONS = {"not_on_comp", "no_comp", "not_comped", "no_active_comp"}
AFFILIATE_KEPT = "kept: public programme"


def affiliate_info(data: dict) -> dict:
    """What PP's affiliate answers carry that is worth showing, from either route: the code and link, whether the
    account was enrolled just now, and (GET) the counts and pence figures. Missing or malformed values are left out;
    a link that is not a plain http(s) URL is dropped."""
    info: dict = {}
    account = account_ref(data.get("account_id"))       # a UUID string, never a number
    if account is not None:
        info["account_id"] = account
    if isinstance(data.get("affiliate"), bool):
        info["affiliate"] = data["affiliate"]
    code = short_text(data.get("code"), 64)
    if code:
        info["code"] = code
    link = safe_url(data.get("link"))
    if link:
        info["link"] = link
    if isinstance(data.get("created"), bool):
        info["created"] = data["created"]
    for key in ("referred_accounts", "commission_due_pence", "commission_paid_pence"):
        value = whole_number(data.get(key))
        if value is not None:
            info[key] = value
    return info


def pp_tier(value: str | None) -> str:
    """'hunter' or 'pro_buyer' -> 'pro_buyer'. Anything outside the vocabulary is refused."""
    raw = (value or "").strip().lower()
    tier = TIER_KEYS.get(raw, raw)
    if tier not in PP_TIERS:
        raise ConnectorError("unsupported", detail=f"pp: unknown tier {value!r}")
    return tier


def _not_on_comp(error: ConnectorError) -> bool:
    if error.reason in NOT_ON_COMP_REASONS:
        return True
    text = (error.detail or "").lower()
    return "on a comp" in text and ("isn't" in text or "is not" in text or "not on" in text)


class PPConnector(BaseConnector):
    product = "pp"
    COMP = "/api/partner/grouphq/comp"
    REVOKE = "/api/partner/grouphq/revoke"
    ACCOUNT = "/api/partner/grouphq/account"
    FLAGS = "/api/partner/grouphq/flags"               # Phase 3 (contract pending)
    FLAGS_REVOKE = "/api/partner/grouphq/flags/revoke"  # Phase 3 (contract pending)
    AFFILIATE = "/api/partner/grouphq/affiliate"        # mentee earnings, contract 1

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "PPConnector":
        return cls(config.PP_BASE_URL, config.PP_SERVICE_TOKEN, dry_run=dry_run)

    def _headers(self) -> dict:
        headers = super()._headers()
        headers["X-Service-Token"] = self._token       # the header PP's job_auth reads
        return headers

    def _plan_headers(self) -> dict:
        return {**super()._plan_headers(), "X-Service-Token": "[redacted]"}

    def _with_account(self, result: ConnectorResult) -> ConnectorResult:
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        account = account_ref(self.data_dict(result).get("account_id"))     # PP's users.id: a UUID string
        if account is not None:
            result.remote_ref = account
            result.links = {"pp_account_id": account}
        return result

    def grant(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "",
              extra: dict | None = None) -> ConnectorResult:
        email = normalise_email(person.person_key)
        reason = reason or f"grouphq:grant:{grant.id}"
        if grant.kind == "comp_tier":
            tier = pp_tier(grant.value)
            if tier == "free":
                return ConnectorResult(ok=True, noop=SCOUT_NOOP)
            payload = {"email": email, "tier": tier, "until": iso_utc(grant.ends_at), "revert_to": "free",
                       "reason": reason}
            return self._with_account(self.request("POST", self.COMP, json=payload))
        if grant.kind == "flag":
            payload = {"email": email, "key": grant.value, "until": iso_utc(grant.ends_at)}
            return self._with_account(self.request("POST", self.FLAGS, json=payload))
        if grant.kind == "affiliate":
            result = self._with_account(self.request("POST", self.AFFILIATE, json={"email": email, "reason": reason[:200]}))
            if not result.dry_run:
                result.info = affiliate_info(self.data_dict(result))
            return result
        raise self.unsupported(grant)

    def revoke(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "") -> ConnectorResult:
        email = normalise_email(person.person_key)
        if grant.kind == "comp_tier":
            if pp_tier(grant.value) == "free":
                return ConnectorResult(ok=True, noop=SCOUT_NOOP)
            payload = {"email": email, "reason": (reason or f"grouphq:revoke:{grant.id}")[:200]}
            try:
                return self._with_account(self.request("POST", self.REVOKE, json=payload))
            except ConnectorError as error:
                if _not_on_comp(error):
                    # The desired state already holds: the account is not comped. Treat as done.
                    return ConnectorResult(ok=True, status=error.status, data=error.body,
                                           noop="already off the comp", plan=error.plan)
                raise
        if grant.kind == "flag":
            return self._with_account(self.request("POST", self.FLAGS_REVOKE, json={"email": email, "key": grant.value}))
        if grant.kind == "affiliate":
            # The programme is public and membership is permanent: there is nothing to take away and no route to call.
            return ConnectorResult(ok=True, noop=AFFILIATE_KEPT)
        raise self.unsupported(grant)

    def affiliate_status(self, email: str) -> ConnectorResult:
        """Contract 1's GET, for the earning links panel. In dry run only the plan; live, result.info is affiliate_info()
        of the answer ({account_id, affiliate: false} or the code, link, referred_accounts and the two pence figures).
        404 no_account is raised as ConnectorError("no_account") for the caller to show."""
        result = self.request("GET", self.AFFILIATE, params={"email": normalise_email(email)})
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        result.info = affiliate_info(self.data_dict(result))
        result.info.setdefault("affiliate", False)
        return result

    def lookup(self, email: str) -> ConnectorResult:
        result = self.request("GET", self.ACCOUNT, params={"email": normalise_email(email)})
        return self._with_account(result)
