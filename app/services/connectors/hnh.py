"""Hello New Home connector: the plan's Phase 2 contract, as built on HNH's partner-api branch (docs/connectors.md).

    POST {HNH_BASE_URL}/api/partner/partners              {email, name, tier, status, area_slug | postcodes[],
                                                           create_login, franchise: {fee_waived, reason}}
    POST {HNH_BASE_URL}/api/partner/partners/{id}/status  {status, reason}
    GET  {HNH_BASE_URL}/api/partner/partners?email=

Auth: Bearer HNH_PARTNER_TOKEN (the grouphq api_clients row with the partners:write scope on the HNH
side) and X-Partner-Client: grouphq. A grant creates the partner (with a login that must change its
password on first use; the temporary password comes back once and is handed to the run page through
ConnectorResult.once, never stored) or sets an existing one back to active; a revoke pauses it, which
keeps the record and the area assignment. The area (area_slug or postcodes) comes from the programme template item when the
founder has decided it; without one HNH creates the partner with no area and HQ assigns it by hand.

Hello New Home is a separate company (Hello New Home 2 Ltd): this call moves a name and an email
between two controllers, so it stays in dry run until the intra-group data sharing agreement exists.

Contract 3 of the mentee-earnings packet (docs/connectors.md): the lookup's partner also answers referral_code,
referral_link, area ({slug, name} or null), tier, status, portal_url and admin_url (the head-office page where the
founder assigns the area). partner_status() reads them; the recompute keeps them on the grant (remote_info), which is
what the "Needs a region" queue lists without a live call, and the earning links panel reads them live.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ... import config
from .base import (BaseConnector, ConnectorError, ConnectorResult, _int_or_none, normalise_email, route_missing, safe_url,
                   short_text, whole_number)

if TYPE_CHECKING:   # pragma: no cover
    from ...models import EntitlementGrant, Person

TIERS = ("introducer", "area")
PAUSED = "paused"
PLACEHOLDER = "{partner_id}"       # stands in for the id in a dry-run plan when none is known yet


def _area(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    slug, name = short_text(value.get("slug"), 80), short_text(value.get("name"), 120)
    return {"slug": slug, "name": name or slug} if (slug or name) else None


def partner_info(data: Any) -> dict:
    """What contract 3's lookup answers, for the panel and the region queue: {found: False}, or found with the partner
    id, referral code and link, area ({slug, name} or None), tier, status, portal and admin links, and needs_region.
    Each field is read from the partner record, or from the top of the answer when the record lacks it. area_known
    is False when the answer carries neither area nor areas (an HNH build without contract 3): such a partner is
    never listed as needing a region, because nothing says it has none. An introducer has no territory by design and
    is never listed either."""
    top = data if isinstance(data, dict) else {}
    record = _record(data)
    if top.get("found") is False or not record or (record is top and "id" not in record and top.get("found") is not True):
        return {"found": False}

    def pick(key: str):
        return record[key] if key in record else top.get(key)

    info: dict = {"found": True}
    partner_id = whole_number(record.get("id"))
    if partner_id is not None:
        info["partner_id"] = partner_id
    code = short_text(pick("referral_code"), 64)
    if code:
        info["referral_code"] = code
    for key in ("referral_link", "portal_url", "admin_url"):
        link = safe_url(pick(key))
        if link:
            info[key] = link
    for key in ("tier", "status"):
        text = short_text(pick(key), 20)
        if text:
            info[key] = text.lower()
    area_known = "area" in record or "area" in top
    area = _area(pick("area"))
    if not area_known and isinstance(record.get("areas"), list):
        area_known = True
        area = next((a for a in (_area(raw) for raw in record["areas"]) if a), None)
    info["area"] = area
    info["area_known"] = area_known
    info["needs_region"] = (area_known and area is None and info.get("status", "active") == "active"
                            and info.get("tier") != "introducer")
    return info


def _record(data: Any) -> dict:
    if isinstance(data, dict):
        if isinstance(data.get("partner"), dict):
            return data["partner"]
        if isinstance(data.get("partners"), list):
            return _record(data["partners"])
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


class HNHConnector(BaseConnector):
    product = "hnh"
    PARTNERS = "/api/partner/partners"

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "HNHConnector":
        return cls(config.HNH_BASE_URL, config.HNH_PARTNER_TOKEN, dry_run=dry_run)

    def _with_partner(self, result: ConnectorResult) -> ConnectorResult:
        if result.dry_run:
            return result
        refused = self.refused(result)
        if refused is not None:
            raise refused
        record = _record(result.data)
        # HNH returns the login's temporary password exactly once (hashed on its side, must be changed on
        # first use). It is taken out of the body here so the stored response never carries it, and handed
        # to the person who pressed the button through `once`. A lost one is re-issued by head office from
        # the HNH admin partner page (Reset password), never from here.
        if isinstance(result.data, dict) and "one_time_password" in result.data:
            password = result.data.pop("one_time_password")
            if isinstance(password, str) and password:
                email = record.get("email") or "the partner"
                result.once = (f"HNH temporary portal password for {email}: {password} (shown once, not stored; "
                               "head office can reset it from the HNH admin partner page)")
        links = {}
        partner_id = record.get("id")
        if partner_id is not None:
            as_int = _int_or_none(partner_id)
            if as_int is None or isinstance(partner_id, bool):
                raise ConnectorError("bad_response", status=result.status, detail="hnh: partner id is not a whole number",
                                     body=result.data, plan=result.plan)
            result.remote_ref = str(as_int)
            links["hnh_partner_id"] = as_int
        user_id = _int_or_none(record.get("user_id"))
        if user_id is not None:
            links["hnh_user_id"] = user_id
        result.links = links
        return result

    def _status_path(self, partner_id: str) -> str:
        ref = str(partner_id)
        return f"{self.PARTNERS}/{ref if ref == PLACEHOLDER else quote(ref, safe='')}/status"

    def grant(self, grant: "EntitlementGrant", person: "Person", *, reason: str = "",
              extra: dict | None = None) -> ConnectorResult:
        if grant.kind != "partner_seat":
            raise self.unsupported(grant)
        tier = (grant.value or "area").strip().lower()
        if tier not in TIERS:
            raise ConnectorError("unsupported", detail=f"hnh: unknown partner tier {grant.value!r}")
        reason = reason or f"grouphq:grant:{grant.id}"
        if grant.remote_ref:
            return self._with_partner(self.request("POST", self._status_path(grant.remote_ref),
                                                   json={"status": "active", "reason": reason}))
        extra = extra or {}
        # HNH prints the partner's name on its public area pages and refuses a blank one (422), which the
        # outbox would otherwise retry five times. Refuse it here with a reason the grant page can show.
        name = (person.name or "").strip()
        if not name:
            raise ConnectorError("missing_name",
                                 detail="hnh: the person has no name; add one on the person page before granting a partner seat")
        payload: dict = {
            "email": normalise_email(person.person_key),
            "name": name,
            "tier": tier,
            "status": "active",
            "create_login": True,
            "franchise": {"fee_waived": bool(extra.get("fee_waived", True)), "reason": reason},
        }
        if extra.get("area_slug"):
            payload["area_slug"] = str(extra["area_slug"])
        elif extra.get("postcodes"):
            payload["postcodes"] = [str(p).upper() for p in extra["postcodes"]]
        return self._with_partner(self.request("POST", self.PARTNERS, json=payload))

    def _partner_id(self, grant: "EntitlementGrant", person: "Person") -> str | None:
        """HNH's id for the person, or None when it has no partner with that email. HNH answers the lookup
        200 {found: false, partner: null} today; a 404 is read the same way in case that ever changes."""
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
        if grant.kind != "partner_seat":
            raise self.unsupported(grant)
        partner_id = self._partner_id(grant, person)
        if not partner_id:
            return ConnectorResult(ok=True, noop="no partner with that email: nothing to pause")
        try:
            return self._with_partner(self.request("POST", self._status_path(partner_id),
                                                   json={"status": PAUSED, "reason": reason or f"grouphq:grant:{grant.id}"}))
        except ConnectorError as error:
            if error.status == 404:
                return ConnectorResult(ok=True, status=404, data=error.body, noop="partner already gone", plan=error.plan)
            raise

    def lookup(self, email: str) -> ConnectorResult:
        return self._with_partner(self.request("GET", self.PARTNERS, params={"email": normalise_email(email)}))

    def partner_status(self, email: str) -> ConnectorResult:
        """Contract 3's lookup, for the earning links panel and the region queue. In dry run only the plan; live,
        result.info is partner_info() of the answer ({found: False} for no partner, a 404 refusal included; a 404 that is
        not HNH's own refusal, route_missing, is raised like any other failure)."""
        try:
            result = self.lookup(email)
        except ConnectorError as error:
            if error.status == 404 and not route_missing(error):
                return ConnectorResult(ok=True, status=404, data=error.body, plan=error.plan, info={"found": False})
            raise
        if not result.dry_run:
            result.info = partner_info(result.data)
        return result
