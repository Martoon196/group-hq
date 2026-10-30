"""Read-only brand-scoped WhatsApp status from Relaunch72; no provider credentials."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from .. import config

BRANDS = {"pp": "Property Predator", "mhp": "Martin Howard Property"}
ORIGIN = "https://hq.propertypredator.com"
PATH = "/api/group-hq/channels/whatsapp"
CONNECTIONS = {"not_configured", "configured", "ready", "degraded", "revoked", "not_composed"}
INBOUND = {"not_supported", "not_ready", "ready", "degraded"}
OUTBOUND = {"not_supported", "effects_disabled", "blocked", "approval_required", "ready", "cap_reached"}
RECEIPTS = {"none", "pending", "healthy", "needs_attention", "outcome_unknown"}
BLOCKERS = {
    "PROVIDER_NOT_CONFIGURED", "LIVE_ADAPTER_NOT_COMPOSED", "EFFECTS_DISABLED", "INGRESS_NOT_READY",
    "CONSENT_REQUIRED", "CONSENT_WITHDRAWN", "SUPPRESSED", "APPROVAL_REQUIRED", "REPLY_WINDOW_CLOSED",
    "CAP_REACHED", "RECEIPT_NEEDS_ATTENTION", "IDENTITY_BINDING_REQUIRED", "OPERATOR_AUTHORITY_REQUIRED",
    "TEMPLATE_REQUIRED", "EMERGENCY_PAUSED", "OUTCOME_UNKNOWN_QUARANTINED", "APPROVED_CONTENT_REQUIRED",
}
LABELS = {
    "not_configured": "Not connected", "configured": "Connected · checks pending", "ready": "Ready",
    "degraded": "Needs attention", "revoked": "Connection revoked", "not_composed": "Not connected",
    "not_supported": "Not available", "not_ready": "Not verified", "effects_disabled": "Sending off",
    "blocked": "Setup required", "approval_required": "Approval required", "cap_reached": "Limit reached",
    "none": "No messages recorded", "pending": "Awaiting delivery", "healthy": "Receipt recorded",
    "needs_attention": "Needs attention", "outcome_unknown": "Delivery needs checking",
}


@dataclass(frozen=True)
class WhatsAppStatus:
    brand: str
    availability: str
    snapshot_at: str | None = None
    connection_state: str | None = None
    inbound_state: str | None = None
    outbound_state: str | None = None
    receipt_state: str | None = None
    blockers: tuple[str, ...] = ()
    last_receipt_at: str | None = None

    @property
    def title(self) -> str:
        if self.availability == "unavailable":
            return "Connection status unavailable"
        if self.availability == "not_connected" or self.connection_state in {"not_configured", "not_composed"}:
            return "Let’s connect WhatsApp"
        return "WhatsApp connection"

    def label(self, value: str | None) -> str:
        return LABELS.get(value or "", "Not checked")


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid status timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Invalid status timestamp")
    return value


def parse_status(payload: object, brand: str) -> WhatsAppStatus:
    if brand not in BRANDS or not isinstance(payload, dict) or payload.get("brand") != brand or payload.get("channel") != "whatsapp":
        raise ValueError("Invalid brand status")
    if payload.get("availability") == "not_connected":
        if set(payload) != {"brand", "channel", "availability"}:
            raise ValueError("Invalid connection status")
        return WhatsAppStatus(brand, "not_connected")
    expected = {"brand", "channel", "availability", "snapshotAt", "connectionState", "inboundState",
                "outboundState", "receiptState", "blockerCodes", "lastReceiptAt"}
    if set(payload) != expected or payload["availability"] != "available":
        raise ValueError("Invalid connection status")
    for key, allowed in (("connectionState", CONNECTIONS), ("inboundState", INBOUND),
                         ("outboundState", OUTBOUND), ("receiptState", RECEIPTS)):
        if not isinstance(payload[key], str) or payload[key] not in allowed:
            raise ValueError("Invalid channel state")
    blockers = payload["blockerCodes"]
    if not isinstance(blockers, list) or len(blockers) > len(BLOCKERS) or any(not isinstance(b, str) or b not in BLOCKERS for b in blockers):
        raise ValueError("Invalid channel blockers")
    receipt_at = _timestamp(payload["lastReceiptAt"]) if payload["lastReceiptAt"] is not None else None
    return WhatsAppStatus(brand, "available", _timestamp(payload["snapshotAt"]), payload["connectionState"],
                          payload["inboundState"], payload["outboundState"], payload["receiptState"],
                          tuple(dict.fromkeys(blockers)), receipt_at)


def fetch_status(brand: str, *, client: httpx.Client | None = None, token: str | None = None) -> WhatsAppStatus:
    if brand not in BRANDS:
        raise ValueError("Unknown brand")
    secret = config.R72_CRM_TOKEN if token is None else token
    if not 32 <= len(secret) <= 256:
        return WhatsAppStatus(brand, "unavailable")
    own_client = client is None
    http = client or httpx.Client(timeout=5.0, follow_redirects=False)
    try:
        response = http.get(f"{ORIGIN}{PATH}", params={"brand": brand}, follow_redirects=False, headers={
            "Authorization": f"Bearer {secret}", "Accept": "application/json", "User-Agent": "GroupHQ/1.0",
        })
        if response.status_code != 200 or len(response.content) > 20_000:
            return WhatsAppStatus(brand, "unavailable")
        return parse_status(response.json(), brand)
    except (httpx.HTTPError, ValueError, TypeError):
        return WhatsAppStatus(brand, "unavailable")
    finally:
        if own_client:
            http.close()
