"""Send a WhatsApp: the owner's one-message-to-one-person screen for a CRM contact (app.services.whatsapp_send).

Three steps on one page: who, what, check. Relaunch72 decides everything and reports every outcome; this screen only
asks it. A plain page load asks Relaunch72 once (twice when an approved-message list is needed), and nothing is sent
until "Send this message" is pressed with the request id minted when that button was rendered.
"""
from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import whatsapp_send as service
from ..services.audit import audit
from ..templating import go, render, templates

router = APIRouter(dependencies=[Depends(check_origin)])
PAGE = service.PAGE


def url(brand: str, contact_id: str, **more) -> str:
    """The screen for one contact, or "" when this brand or contact cannot have one."""
    try:
        if brand not in service.BRANDS or str(uuid.UUID(contact_id)) != contact_id:
            return ""
    except (ValueError, TypeError, AttributeError):
        return ""
    return PAGE + "?" + urlencode({"brand": brand, "contact": contact_id} | {k: v for k, v in more.items() if v})


templates.env.globals["whatsapp_send_url"] = url


def _target(brand: str, contact: str) -> tuple[str, str]:
    if not url(brand, contact):
        raise HTTPException(status_code=404, detail="Contact not found")
    return brand, contact


def _page(request: Request, user: User, view: dict, status_code: int = 200, **ctx) -> HTMLResponse:
    response = render(request, "whatsapp_send.html", {
        "user": user, "section": "crm", "view": view, "page": PAGE, "off_title": service.OFF, "sender": service.SENDER,
        "reasons": service.REASONS, "confirm_number": service.CONFIRM_NUMBER, "unavailable_text": service.UNAVAILABLE,
        "what": None, "check": None, "status": None, "problem": None, "values": [], "draft": "", **ctx}, status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _open(view: dict) -> bool:
    """Whether there is a number to send to and nothing stopping it."""
    return bool(not view["off"] and not view["unavailable"] and not view["stop"] and view["chosen"]
                and not view["chosen"]["stop"])


def _what(user: User, view: dict, message: str, mode: str) -> dict:
    """Step 2: a typed reply while the person's 24 hours are open, otherwise the approved messages."""
    if view["chosen"]["window_open"] and mode != "approved":
        return {"typed": True}
    if view["remaining"] == 0:
        return {"stop": service.plain("daily_cap_reached", limit=view["limit"])}
    listing = service.approved_messages(user, view["brand"])
    if "messages" not in listing:
        return {"stop": listing.get("stop") or (service.OFF + "." if listing.get("off") else service.UNAVAILABLE)}
    return {"messages": listing["messages"], "picked": next((m for m in listing["messages"] if m["id"] == message), None)}


@router.get(PAGE, response_class=HTMLResponse)
def page(request: Request, brand: str = "", contact: str = "", link: str = "", message: str = "", mode: str = "",
         sent: str = "", checked: str = "", db: Session = Depends(get_db), user: User = Depends(require_admin)):
    brand, contact = _target(brand, contact)
    view = service.load(db, user, brand, contact, link)
    ctx: dict = {"checked": checked}
    if _open(view):
        try:
            sent = service.canonical_uuid(sent) if sent else ""
        except service.Invalid:
            sent = ""
        if sent:
            ctx["status"] = service.status(user, brand, contact, sent)
        if not ctx.get("status"):
            ctx["what"] = _what(user, view, message, mode)
    return _page(request, user, view, **ctx)


@router.post(PAGE + "/number")
def add_number(brand: str = Form(""), contact: str = Form(""), point: str = Form(""), number: str = Form(""),
               confirm: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Step 1: link one of the contact's numbers, or one the owner types, to the sender. Sends nothing."""
    brand, contact = _target(brand, contact)
    here = url(brand, contact)
    if confirm != "yes":
        return go(here, err="Tick the box to say you have checked this is their WhatsApp number.")
    if not number.strip() and not point:
        return go(here, err="Type their WhatsApp number with its country code, for example +44 7700 900123.")
    try:
        answer = service.link_number(user, brand, contact, point, number)
    except service.Invalid:
        raise HTTPException(status_code=400, detail="Choose a number again.") from None
    audit(db, user, "whatsapp.send.number", f"crm:{brand}:{contact}",
          after={"from": "typed" if number.strip() else "crm", "linked": "link" in answer, "reason": answer.get("reason")})
    db.commit()
    if "link" in answer:
        return go(url(brand, contact, link=answer["link"], checked=service.number_checked(user, answer["link"])))
    return go(here, err=answer.get("stop") or (service.OFF + "." if answer.get("off") else service.UNAVAILABLE))


def _message(kind: str, template: str, layout: str, names: list[str], blanks: list[str], body: str) -> dict:
    """The posted message as this screen's forms carry it. Raises Invalid for anything a form here cannot hold."""
    if kind == "template":
        if not service.META_ID.fullmatch(template):
            raise service.Invalid()
        blanks = [b.strip() for b in blanks]
        return {"kind": kind, "template": template, "layout": layout, "names": names, "blanks": blanks, "body": "",
                "values": service.variables(layout, names, blanks)}
    if kind != "text" or "\0" in body:
        raise service.Invalid()
    return {"kind": kind, "template": "", "layout": "", "names": [], "blanks": [], "body": body, "values": None}


@router.post(PAGE + "/check", response_class=HTMLResponse)
def check(request: Request, brand: str = Form(""), contact: str = Form(""), link: str = Form(""), kind: str = Form(""),
          template: str = Form(""), layout: str = Form(""), name: list[str] = Form([]), blank: list[str] = Form([]),
          body: str = Form(""), checked: str = Form(""), reason: str = Form(""), note: str = Form(""),
          db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Step 3: the exact message as Relaunch72 renders it, who it goes to, and the Send button. Sends nothing."""
    brand, contact = _target(brand, contact)
    try:
        link = service.canonical_uuid(link)
        message = _message(kind, template, layout, name, blank, body)
    except service.Invalid:
        raise HTTPException(status_code=400, detail="Start this message again.") from None
    view = service.load(db, user, brand, contact, link)
    if not _open(view):
        return _page(request, user, view)
    if view["chosen"]["link"] != link:
        return go(url(brand, contact), err="That number is no longer linked to this contact. Choose the number again.")
    chosen = view["chosen"]

    def back(error: str, mode: str = "") -> HTMLResponse:
        return _page(request, user, view, what=_what(user, view, template, mode), values=message["blanks"],
                     draft=message["body"], checked=checked, flash=error, flash_kind="err")

    if kind == "text":
        if not body.strip() or len(body.encode()) > 4096:
            return back("Write your message first. It can be up to 4,096 characters.")
        if not chosen["window_open"]:
            return back(service.plain("window_closed"), "approved")
        text, digest = body, ""
    else:
        if any(not b for b in message["blanks"]):
            return back("Fill in every blank.", "approved")
        shown = service.preview(user, brand, template, message["values"])
        if "text" not in shown:
            return back(shown.get("stop") or (service.OFF + "." if shown.get("off") else service.UNAVAILABLE), "approved")
        text, digest = shown["text"], shown["sha256"]
    needs_reason = kind == "template" and chosen["needs_reason"]
    return _page(request, user, view, check={
        **message, "text": text, "sha": digest, "request_id": str(uuid.uuid4()), "needs_reason": needs_reason,
        "needs_tick": needs_reason and chosen["needs_tick"], "ticked": service.was_checked(user, link, checked),
        "checked": checked, "reason": reason if reason in service.REASONS else "", "note": note[:500]})


@router.post(PAGE, response_class=HTMLResponse)
def send(request: Request, brand: str = Form(""), contact: str = Form(""), link: str = Form(""), kind: str = Form(""),
         request_id: str = Form(""), template: str = Form(""), layout: str = Form(""), name: list[str] = Form([]),
         blank: list[str] = Form([]), sha: str = Form(""), body: str = Form(""), reason: str = Form(""),
         note: str = Form(""), needs_tick: str = Form(""), confirm: str = Form(""), checked: str = Form(""),
         db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Send this message: one attempt per request id. The page that follows reads the outcome from Relaunch72."""
    brand, contact = _target(brand, contact)
    try:
        link, request_id = service.canonical_uuid(link), service.canonical_uuid(request_id)
        message = _message(kind, template, layout, name, blank, body)
        if (kind == "template" and (not service.SHA256.fullmatch(sha) or any(not b for b in message["blanks"]))) or (
                kind == "text" and (not body.strip() or len(body.encode()) > 4096)) or (reason and reason not in service.REASONS):
            raise service.Invalid()
    except service.Invalid:
        raise HTTPException(status_code=400, detail="Check this message again before sending.") from None
    note = " ".join(note.split())
    attest = confirm == "yes" or service.was_checked(user, link, checked)
    lead = service.local_lead(db, brand, contact)
    view = {"brand": brand, "brand_name": service.BRANDS[brand], "contact": contact, "back": url(brand, contact, link=link),
            "name": lead.display_name if lead else "", "off": False, "unavailable": False, "stop": None, "bare": True}
    carried = {**message, "link": link, "sha": sha, "request_id": request_id, "reason": reason, "note": note,
               "needs_tick": needs_tick, "confirm": confirm, "checked": checked}

    def problem(sentence: str, again: str = "") -> HTMLResponse:
        return _page(request, user, view, problem={"sentence": sentence, "again": again, **carried})

    if reason and not 8 <= len(note) <= 500:
        return problem(service.plain("evidence_note_invalid"), "check")
    if reason and needs_tick and not attest:
        return problem(service.plain("number_unverified"), "check")
    outcome = service.send(db, user, brand, contact, link, request_id, kind=kind, meta=template,
                           values=message["values"], sha256=sha, body=body, reason=reason, note=note, attest=attest)
    if outcome.get("sent"):
        return go(url(brand, contact, link=link, sent=outcome["sent"]))
    if outcome.get("off"):
        return _page(request, user, view | {"off": True})
    if outcome.get("unconfirmed"):
        return problem(service.UNCONFIRMED, "send")
    code = outcome.get("reason")
    sentence = outcome.get("stop") or service.plain(code)
    if code in ("account_restricted", "recipient_paused"):
        sentence = service.paused(code, outcome.get("hold"))
    return problem(sentence, "check" if code in service.CHECK_AGAIN else "approved" if code in service.USE_APPROVED else "")
