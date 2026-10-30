"""Connection setup stays read only and each brand consumes only its own status."""
import httpx
import pytest

from app.routers import crm
from app.services.whatsapp import WhatsAppStatus, fetch_status, parse_status


def payload(brand="pp"):
    return {"brand": brand, "channel": "whatsapp", "availability": "available", "snapshotAt": "2026-10-30T01:00:00.000Z",
            "connectionState": "not_configured", "inboundState": "not_ready", "outboundState": "blocked",
            "receiptState": "none", "blockerCodes": ["PROVIDER_NOT_CONFIGURED"], "lastReceiptAt": None}


def test_whatsapp_admin_page_and_brand_scope(client, admin_client, monkeypatch):
    calls = []
    def status(brand):
        calls.append(brand)
        return WhatsAppStatus(brand, "not_connected")
    monkeypatch.setattr(crm, "fetch_status", status)
    assert client.get("/crm/channels/whatsapp").status_code == 303
    assert calls == []
    pp = admin_client.get("/crm/channels/whatsapp?brand=pp")
    assert pp.status_code == 200 and pp.headers["cache-control"] == "no-store"
    assert "+44 7886 068757" in pp.text and "Open Meta setup" in pp.text
    assert calls == ["pp"]
    mhp = admin_client.get("/crm/channels/whatsapp?brand=mhp")
    assert mhp.status_code == 200 and "Choose a number" in mhp.text
    assert "+44 7886 068757" not in mhp.text
    assert calls == ["pp", "mhp"]
    for query in ("brand=other", "brand=pp&brand=mhp", "brand=pp&workspaceId=other"):
        assert admin_client.get("/crm/channels/whatsapp?" + query).status_code == 400
    assert calls == ["pp", "mhp"]
    assert admin_client.post("/crm/channels/whatsapp").status_code == 405
    assert admin_client.post("/crm/channels/whatsapp", headers={"Origin": "https://other.example"}).status_code in (403, 405)


def test_unavailable_status_is_not_a_disconnected_or_live_claim(admin_client, monkeypatch):
    monkeypatch.setattr(crm, "fetch_status", lambda brand: WhatsAppStatus(brand, "unavailable"))
    response = admin_client.get("/crm/channels/whatsapp")
    assert "Connection status unavailable" in response.text
    assert "Group HQ WhatsApp sending is awaiting" in response.text
    assert "Not checked" in response.text
    assert 'type="password"' not in response.text


def test_read_client_pins_host_and_brand_and_never_retries_or_follows_redirects():
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=payload("mhp"))
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        status = fetch_status("mhp", client=client, token="x" * 32)
    assert status.brand == "mhp" and status.connection_state == "not_configured"
    assert len(calls) == 1 and calls[0].method == "GET"
    assert str(calls[0].url) == "https://hq.propertypredator.com/api/group-hq/channels/whatsapp?brand=mhp"
    assert calls[0].headers["Authorization"] == "Bearer " + "x" * 32
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": "https://other.example"}))) as client:
        assert fetch_status("pp", client=client, token="x" * 32).availability == "unavailable"


def test_status_contract_rejects_other_brand_unknown_states_and_extra_fields():
    assert parse_status(payload(), "pp").availability == "available"
    assert parse_status({"brand": "mhp", "channel": "whatsapp", "availability": "not_connected"}, "mhp").availability == "not_connected"
    for change in ({"brand": "mhp"}, {"connectionState": "live"}, {"blockerCodes": ["PRIVATE_PROVIDER_ERROR"]},
                   {"token": "must not render"}, {"snapshotAt": "not-a-date"}, {"lastReceiptAt": "2026-10-30"}):
        with pytest.raises(ValueError):
            parse_status({**payload(), **change}, "pp")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload("pp")))) as client:
        assert fetch_status("mhp", client=client, token="x" * 32).availability == "unavailable"
        assert fetch_status("mhp", client=client, token="").availability == "unavailable"
