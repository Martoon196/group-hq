"""The operator API's write tools for the admin screens (docs/operator-api.md): people, mentees, grants, the LAPS
board, property deals, programmes, territories, the franchise pipeline and sellers.

Every tool runs the screen's own code path, never a copy of its rules. Where a route held its logic inline, that
logic now lives in a function the route and the tool both call (app.routers.people.create_person,
app.routers.mentees.record_payment, app.routers.grants.run_retry and the like), which raises app.templating.Refused
with the sentence the screen shows. Where a route is a plain synchronous function of its form fields that answers with go(path, ok=, err=), the tool presses it
directly (_press) and reads the flash back from the redirect. Either way the same validation, audit rows, commits and
product-connector calls happen, and a refusal comes back to the model as the screen's own sentence (ToolError, so
isError). The acting admin is the user acting_user resolved; the MCP router adds its operator.tool audit row and
commits after a tool succeeds.

Tools that edit a record take only the fields to change: a field left out keeps its current value, which is passed
to the screen's code exactly as the form would carry it. Nothing here sends an email, SMS, WhatsApp or other message
to a person, charges a card or moves money; the excluded actions are listed in docs/operator-api.md.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from ..models import (FRANCHISE_PAYMENT_BASES, MENTEE_STATUSES, OPPORTUNITY_STAGES, PAYMENT_METHODS, PAYMENT_MODES, SELLER_KINDS,
                      Acquisition, EntitlementGrant, LapsDeal, Person, Programme, User)
from ..routers import acquisitions as acquisitions_screen
from ..routers import franchise as franchise_screen
from ..routers import grants as grants_screen
from ..routers import laps as laps_screen
from ..routers import mentees as mentees_screen
from ..routers import people as people_screen
from ..routers import programmes as programmes_screen
from ..routers import sellers as sellers_screen
from ..routers import territories as territories_screen
from ..templating import Refused
from . import acquisitions as acquisitions_service
from . import laps as laps_service
from .operator_base import Tool, ToolError, _int, _schema

MAX_DECISIONS = 100
ONCE_WITHHELD = ("Hello New Home issued a one-time temporary password in this run. It is shown only on the screen and "
                 "is never returned over the operator API, so the person may need a password reset in Hello New Home.")
LIVE_ACCESS = ("This changes the person's real access in the product, live unless that product is in Dry run on Settings "
               "(or the deployment forces dry run).")


# ---- running the screen's code ------------------------------------------------------------------------------------
def _screen(fn: Callable, *args, **kwargs):
    """Run a screen action. Its refusal, or the 404 / 409 / 503 the route would answer, becomes a ToolError."""
    try:
        return fn(*args, **kwargs)
    except Refused as refusal:
        raise ToolError(str(refusal)) from None
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None


def _press(handler: Callable, **form) -> str:
    """Press a route whose answer is go(path, ok=, err=): call it with every field and the session and admin, and
    turn its redirect into the flash text. An err flash is the screen refusing (or reporting a failure it has
    recorded) and becomes a ToolError."""
    response = _screen(handler, **form)
    if not isinstance(response, RedirectResponse):
        raise RuntimeError(f"{handler.__name__} did not redirect")
    query = parse_qs(urlsplit(response.headers.get("location", "")).query)
    if query.get("err"):
        raise ToolError(query["err"][0])
    return query.get("ok", [""])[0]


def _done(message: str, **ids) -> dict:
    return {"ok": True, "message": message or "Nothing changed.", **ids}


# ---- arguments ----------------------------------------------------------------------------------------------------
def _given(args: dict, name: str) -> bool:
    return args.get(name) is not None


def _field(args: dict, name: str, limit: int = 20_000, current: str = "") -> str:
    """A text field as the form would post it: the argument when given, else current."""
    if not _given(args, name):
        return current
    raw = args[name]
    if not isinstance(raw, str):
        raise ToolError(f"{name} must be text")
    if len(raw) > limit:
        raise ToolError(f"{name} is longer than {limit} characters")
    return raw


def _number_text(args: dict, name: str, current: str = "") -> str:
    """A number field (an id, a count, an amount in pounds) as text: a JSON number or a string; else current."""
    if not _given(args, name):
        return current
    raw = args[name]
    if isinstance(raw, bool):
        raise ToolError(f"{name} must be a number")
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        return format(Decimal(str(raw)), "f")
    if isinstance(raw, str) and len(raw) <= 40:
        return raw
    raise ToolError(f"{name} must be a number")


def _flag(args: dict, name: str, current: bool = False) -> str:
    """A checkbox as the form posts it: "1" ticked, "" not."""
    if not _given(args, name):
        return "1" if current else ""
    raw = args[name]
    if not isinstance(raw, bool):
        raise ToolError(f"{name} must be true or false")
    return "1" if raw else ""


def _list(args: dict, name: str, limit: int = 50) -> list[str]:
    raw = args.get(name)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or len(raw) > limit or not all(isinstance(item, str) and len(item) <= 200 for item in raw):
        raise ToolError(f"{name} must be a list of at most {limit} short texts")
    return list(raw)


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _pounds(pence: int | None) -> str:
    """Pence as the edit forms show them (app.routers.laps.deal_page, app.routers.acquisitions.deal_page)."""
    return "" if pence is None else f"{pence / 100:.2f}".removesuffix(".00")


# ---- schema pieces ------------------------------------------------------------------------------------------------
def _id(description: str) -> dict:
    return {"type": "integer", "minimum": 1, "description": description}


def _str(description: str, max_length: int = 200, **extra) -> dict:
    return {"type": "string", "maxLength": max_length, "description": description, **extra}


def _enum(values, description: str) -> dict:
    return {"type": "string", "enum": list(values), "description": description}


def _date(description: str) -> dict:
    return {"type": "string", "maxLength": 20, "description": description + " As YYYY-MM-DD (dd/mm/yyyy is also read)."}


def _money(description: str) -> dict:
    return {"type": ["string", "number"], "description": description + " In pounds, for example 1250.00."}


def _bool(description: str) -> dict:
    return {"type": "boolean", "description": description}


KEEP = " A field left out keeps its current value."


# ---- engine results -----------------------------------------------------------------------------------------------
def _decisions(decisions) -> dict:
    rows = [{"person_id": d.person_id, "person_key": d.person_key, "grant_id": d.grant_id, "product": d.product, "kind": d.kind,
             "value": d.value, "action": d.action, "outcome": d.outcome, "ends_at": _iso(d.ends_at) or None, "call_id": d.call_id,
             "detail": (d.detail or "")[:300] or None, "dry_run": d.dry_run} for d in list(decisions)[:MAX_DECISIONS]]
    out = {"decisions": rows, "decisions_total": len(list(decisions))}
    if any(getattr(d, "once", "") for d in decisions):
        out["note"] = ONCE_WITHHELD
    return out


def _run(summary, **ids) -> dict:
    return {"ok": True, "message": summary.line(), **ids, "today": summary.today.isoformat(), "dry_run": summary.dry_run,
            "people": summary.people, "failed": summary.failed, "note_from_run": summary.note or None,
            "stopped": list(summary.stopped) or None, **_decisions(summary.decisions)}


# ==== people ===========================================================================================================
def people_create(db: Session, user: User, args: dict) -> dict:
    person, message = _screen(people_screen.create_person, db, user, _field(args, "email", 254), _field(args, "name", 120),
                              _field(args, "phone", 40), _field(args, "notes"))
    return _done(message, person_id=person.id)


def people_update(db: Session, user: User, args: dict) -> dict:
    person = _screen(people_screen.get_or_404, db, Person, _int(args, "person_id"))
    message = _screen(people_screen.update_person, db, user, person, _field(args, "email", 254, person.person_key),
                      _field(args, "name", 120, person.name or ""), _field(args, "phone", 40, person.phone or ""),
                      _field(args, "notes", 20_000, person.notes or ""))
    return _done(message, person_id=person.id)


def people_set_links(db: Session, user: User, args: dict) -> dict:
    person = _screen(people_screen.get_or_404, db, Person, _int(args, "person_id"))
    submitted = {}
    for col in people_screen.LINK_COLUMNS:
        current = getattr(person, col)
        submitted[col] = _number_text(args, col, "" if current is None else str(current)).strip()[:64]
    message = _screen(people_screen.save_links, db, user, person, submitted)
    return _done(message, person_id=person.id)


# ==== mentees ==========================================================================================================
def mentee_create(db: Session, user: User, args: dict) -> dict:
    vals = mentees_screen._mentee_vals(_number_text(args, "person_id"), _number_text(args, "programme_id"), _field(args, "payment_mode", 20),
                                _field(args, "start_date", 20), _field(args, "cohort", 200), _field(args, "notes"))
    mentee, message = _screen(mentees_screen.create_mentee, db, user, vals)
    return _done(message, mentee_id=mentee.id, person_id=mentee.person_id, programme_id=mentee.programme_id)


def mentee_record_payment(db: Session, user: User, args: dict) -> dict:
    mentee = _screen(mentees_screen._load_mentee, db, _int(args, "mentee_id"))
    vals = mentees_screen._payment_vals(_number_text(args, "amount"), _field(args, "paid_at", 20), _field(args, "cleared_at", 20),
                                 _field(args, "method", 20, "manual"), _field(args, "period_start", 20), _field(args, "period_end", 20),
                                 _field(args, "reference", 200), _field(args, "notes"))
    payment, message = _screen(mentees_screen.record_payment, db, user, mentee, vals)
    return _done(message, mentee_id=mentee.id, payment_id=payment.id, cleared=payment.cleared_at is not None)


def mentee_clear_payment(db: Session, user: User, args: dict) -> dict:
    mentee_id, payment_id = _int(args, "mentee_id"), _int(args, "payment_id")
    message = _press(mentees_screen.payment_clear, mentee_id=mentee_id, payment_id=payment_id, cleared_at=_field(args, "cleared_at", 20),
                     db=db, user=user)
    return _done(message, mentee_id=mentee_id, payment_id=payment_id)


def mentee_recompute(db: Session, user: User, args: dict) -> dict:
    mentee_id = _int(args, "mentee_id")
    return _done(_press(mentees_screen.mentee_recompute, mentee_id=mentee_id, db=db, user=user), mentee_id=mentee_id)


def mentee_revoke_all(db: Session, user: User, args: dict) -> dict:
    mentee_id = _int(args, "mentee_id")
    message = _press(mentees_screen.mentee_revoke, mentee_id=mentee_id, reason=_field(args, "reason", 500), db=db, user=user)
    return _done(message, mentee_id=mentee_id)


def mentee_pause_all(db: Session, user: User, args: dict) -> dict:
    mentee_id = _int(args, "mentee_id")
    message = _press(mentees_screen.mentee_pause, mentee_id=mentee_id, reason=_field(args, "reason", 500), db=db, user=user)
    return _done(message, mentee_id=mentee_id)


def mentee_resume_all(db: Session, user: User, args: dict) -> dict:
    mentee = _screen(mentees_screen._load_mentee, db, _int(args, "mentee_id"))
    decisions = _screen(mentees_screen.resume_mentee, db, user, mentee)
    return {"ok": True, "message": f"All access resumed for {mentee.person.person_key} and recomputed.", "mentee_id": mentee.id,
            **_decisions(decisions)}


def mentee_update(db: Session, user: User, args: dict) -> dict:
    mentee = _screen(mentees_screen._load_mentee, db, _int(args, "mentee_id"))
    vals = mentees_screen._detail_vals(_field(args, "status", 20, mentee.status), _field(args, "cohort", 200, mentee.cohort or ""),
                                _field(args, "payment_mode", 20, mentee.payment_mode),
                                _field(args, "start_date", 20, mentee.start_date.isoformat()), _field(args, "notes", 20_000, mentee.notes or ""))
    return _done(_screen(mentees_screen.update_mentee, db, user, mentee, vals), mentee_id=mentee.id)


# ==== grants ===========================================================================================================
def grant_recompute_all(db: Session, user: User, args: dict) -> dict:
    summary = _screen(grants_screen.run_recompute_all, db, user, _flag(args, "dry_run"), _field(args, "today", 20))
    return _run(summary)


def _grant(db: Session, args: dict) -> EntitlementGrant:
    return _screen(grants_screen._grant_or_404, db, _int(args, "grant_id"))


def grant_recompute_person(db: Session, user: User, args: dict) -> dict:
    grant = _grant(db, args)
    summary = _screen(grants_screen.run_recompute_one, db, user, grant, _flag(args, "dry_run"), _field(args, "today", 20))
    return _run(summary, grant_id=grant.id, person_id=grant.person_id)


def grant_retry(db: Session, user: User, args: dict) -> dict:
    grant = _grant(db, args)
    summary = _screen(grants_screen.run_retry, db, user, grant, _flag(args, "dry_run"))
    return _run(summary, grant_id=grant.id, person_id=grant.person_id)


def grant_pause(db: Session, user: User, args: dict) -> dict:
    grant_id = _int(args, "grant_id")
    message = _press(grants_screen.pause, grant_id=grant_id, reason=_field(args, "reason", 500), next="", db=db, user=user)
    return _done(message, grant_id=grant_id)


def grant_resume(db: Session, user: User, args: dict) -> dict:
    grant = _grant(db, args)
    summary = _screen(grants_screen.run_resume, db, user, grant)
    return _run(summary, grant_id=grant.id, person_id=grant.person_id)


# ==== LAPS =============================================================================================================
def laps_refresh(db: Session, user: User, args: dict) -> dict:
    return _done(_press(laps_screen.refresh, view="", db=db, user=user))


def laps_create_deals(db: Session, user: User, args: dict) -> dict:
    deals, message = _screen(laps_screen.create_deals, db, user, name=_field(args, "name", 400), contact=_field(args, "contact", 400),
                             brands=_list(args, "brands"), offer=_field(args, "offer", 240), source=_field(args, "source", 200),
                             value=_number_text(args, "value"), next_step=_field(args, "next_step", 400),
                             next_step_on=_field(args, "next_step_on", 20), notes=_field(args, "notes", 10_000),
                             arrived=_field(args, "arrived", 20), stage=_field(args, "stage", 20, "lead"))
    return _done(message, deal_ids=[d.id for d in deals], stage=deals[0].stage)


def _laps_deal(db: Session, args: dict) -> LapsDeal:
    return _screen(people_screen.get_or_404, db, LapsDeal, _int(args, "deal_id"))


def laps_add_business(db: Session, user: User, args: dict) -> dict:
    deal = _laps_deal(db, args)
    twins, message = _screen(laps_screen.add_businesses, db, user, deal, _list(args, "brands"))
    return _done(message, deal_id=deal.id, new_deal_ids=[t.id for t in twins])


def laps_edit_deal(db: Session, user: User, args: dict) -> dict:
    deal = _laps_deal(db, args)
    message = _press(laps_screen.save_deal, deal_id=deal.id, name=_field(args, "name", 400, deal.name),
                     contact=_field(args, "contact", 400, deal.contact or ""), brand=_field(args, "brand", 20, deal.brand),
                     offer=_field(args, "offer", 240, deal.offer or ""), source=_field(args, "source", 200, deal.source or ""),
                     value=_number_text(args, "value", _pounds(deal.value_pence)),
                     next_step=_field(args, "next_step", 400, deal.next_step or ""),
                     next_step_on=_field(args, "next_step_on", 20, _iso(deal.next_step_on)),
                     notes=_field(args, "notes", 10_000, deal.notes or ""), db=db, user=user)
    return _done(message, deal_id=deal.id)


def laps_move_deal(db: Session, user: User, args: dict) -> dict:
    deal_id = _int(args, "deal_id")
    message = _press(laps_screen.move_deal, deal_id=deal_id, to=_field(args, "to", 20), value=_number_text(args, "value"), view="",
                     back="deal", db=db, user=user)
    deal = db.get(LapsDeal, deal_id)
    return _done(message, deal_id=deal_id, stage=deal.stage if deal is not None else None)


def laps_set_targets(db: Session, user: User, args: dict) -> dict:
    current = laps_service.targets(db)
    message = _press(laps_screen.save_targets, monthly_goal=_number_text(args, "monthly_goal", _pounds(current["monthly_goal_pence"])),
                     typical_sale=_number_text(args, "typical_sale", _pounds(current["typical_sale_pence"])), view="", db=db, user=user)
    after = laps_service.targets(db)
    return _done(message, monthly_goal_pence=after["monthly_goal_pence"], typical_sale_pence=after["typical_sale_pence"])


# ==== property deals ===================================================================================================
ACQ_TEXT = ("postcode", "seller_name", "seller_contact", "situation", "source", "solicitor", "next_step", "lost_reason")
ACQ_PRICES = ("asking", "offer", "agreed", "value", "works")


def _acquisition_form(args: dict, deal: Acquisition | None = None) -> dict[str, str]:
    """The deal form's fields by name, as the screen posts them: what was given, else the deal's current values (the
    blank form for a new deal)."""
    def current(name: str) -> str:
        if deal is None:
            return ""
        value = getattr(deal, name)
        return "" if value is None else str(value)
    form = {"title": _field(args, "title", 400, current("title")), "kind": _field(args, "kind", 20, current("kind")),
            "plan": _field(args, "plan", 20, current("plan")), "seller_type": _field(args, "seller_type", 30, current("seller_type")),
            "units": _number_text(args, "units", current("units")), "notes": _field(args, "notes", 10_000, current("notes"))}
    for name in ACQ_TEXT:
        form[name] = _field(args, name, 400, current(name))
    for name in ACQ_PRICES:
        form[name] = _number_text(args, name, "" if deal is None else _pounds(getattr(deal, name + "_pence")))
    for name in ("target_completion", "next_step_on"):
        form[name] = _field(args, name, 20, "" if deal is None else _iso(getattr(deal, name)))
    return form


def _threads(db: Session, deal_id: int) -> list[dict]:
    return [{"account_key": t.account_key, "thread_sha256": t.thread_sha256, "name": t.name or None}
            for t in acquisitions_service.attached(db, deal_id)]


def acquisition_create(db: Session, user: User, args: dict) -> dict:
    form = _acquisition_form(args)
    form.update({"arrived": _field(args, "arrived", 20), "stage": _field(args, "stage", 20), "thread_ticket": _field(args, "thread_ticket", 4000)})
    deal, message = _screen(acquisitions_screen.create_deal, db, user, form)
    return _done(message, acquisition_id=deal.id, stage=deal.stage, conversations=_threads(db, deal.id))


def _acquisition(db: Session, args: dict) -> Acquisition:
    return _screen(people_screen.get_or_404, db, Acquisition, _int(args, "deal_id"))


def acquisition_edit(db: Session, user: User, args: dict) -> dict:
    deal = _acquisition(db, args)
    message = _screen(acquisitions_screen.edit_deal, db, user, deal, _acquisition_form(args, deal))
    return _done(message, acquisition_id=deal.id)


def acquisition_move(db: Session, user: User, args: dict) -> dict:
    deal_id = _int(args, "deal_id")
    message = _press(acquisitions_screen.move_deal, deal_id=deal_id, to=_field(args, "to", 20), price=_number_text(args, "price"),
                     lost_reason=_field(args, "lost_reason", 400), back="", db=db, user=user)
    deal = db.get(Acquisition, deal_id)
    return _done(message, acquisition_id=deal_id, stage=deal.stage if deal is not None else None)


def acquisition_toggle_check(db: Session, user: User, args: dict) -> dict:
    deal_id = _int(args, "deal_id")
    key = _field(args, "key", 40)
    message = _press(acquisitions_screen.check, deal_id=deal_id, key=key, db=db, user=user)
    deal = db.get(Acquisition, deal_id)
    ticked = key in (deal.checks or {})
    return _done(message or f"Checklist item {key} {'ticked' if ticked else 'unticked'}.", acquisition_id=deal_id, key=key, ticked=ticked)


def acquisition_link(db: Session, user: User, args: dict) -> dict:
    deal_id = _int(args, "deal_id")
    message = _press(acquisitions_screen.link, thread_ticket=_field(args, "thread_ticket", 4000), deal_id=deal_id, db=db, user=user)
    return _done(message, acquisition_id=deal_id, conversations=_threads(db, deal_id))


def acquisition_unlink(db: Session, user: User, args: dict) -> dict:
    deal_id = _int(args, "deal_id")
    message = _press(acquisitions_screen.unlink, deal_id=deal_id, account_key=_field(args, "account_key", 200),
                     thread_sha256=_field(args, "thread_sha256", 128), db=db, user=user)
    return _done(message, acquisition_id=deal_id, conversations=_threads(db, deal_id))


# ==== programmes =======================================================================================================
def programme_update(db: Session, user: User, args: dict) -> dict:
    programme = _screen(people_screen.get_or_404, db, Programme, _int(args, "programme_id"))
    template = args.get("grant_template")
    if template is None:
        template_text = programmes_screen._template_text(programme.grant_template)
    elif isinstance(template, list):
        template_text = json.dumps(template, ensure_ascii=False)
    elif isinstance(template, str):
        template_text = template
    else:
        raise ToolError("grant_template must be a list of items or the template's JSON text")
    vals = programmes_screen._programme_vals(_field(args, "name", 240, programme.name), _field(args, "description", 20_000, programme.description or ""),
                                      _flag(args, "active", programme.active), template_text,
                                      _number_text(args, "price", f"{programme.price:.2f}" if programme.price is not None else ""))
    return _done(_screen(programmes_screen.update_programme, db, user, programme, vals), programme_id=programme.id)


# ==== territories ======================================================================================================
def _district(db: Session, args: dict):
    return _screen(territories_screen._load, db, _field(args, "district", 10))


def territory_reserve(db: Session, user: User, args: dict) -> dict:
    d = _district(db, args)
    vals = territories_screen._vals(_number_text(args, "partner_id"), _number_text(args, "person_id"), _field(args, "reserved_until", 20))
    message = _screen(territories_screen.reserve_district, db, user, d, vals)
    return _done(message, district=d.district, state=d.state, partner_id=d.partner_id, reserved_until=_iso(d.reserved_until) or None)


def territory_assign(db: Session, user: User, args: dict) -> dict:
    d = _district(db, args)
    vals = territories_screen._vals(_number_text(args, "partner_id"), _number_text(args, "person_id"), "")
    message = _screen(territories_screen.assign_district, db, user, d, vals)
    return _done(message, district=d.district, state=d.state, partner_id=d.partner_id)


def territory_release(db: Session, user: User, args: dict) -> dict:
    d = _district(db, args)
    return _done(_press(territories_screen.territory_release, district=d.district, db=db, user=user), district=d.district)


def territory_keep_for_hq(db: Session, user: User, args: dict) -> dict:
    d = _district(db, args)
    return _done(_press(territories_screen.territory_hq, district=d.district, db=db, user=user), district=d.district)


# ==== franchise ========================================================================================================
def franchise_create(db: Session, user: User, args: dict) -> dict:
    vals = {"person_id": _number_text(args, "person_id").strip()[:12], "email": _field(args, "email", 400).strip()[:254],
            "name": _field(args, "name", 400).strip()[:120], "phone": _field(args, "phone", 200).strip()[:40],
            "districts": franchise_screen.parse_districts(_list(args, "districts", 200)),
            "seller_code": _field(args, "seller_code", 100).strip()[:24], "source": _field(args, "source", 200).strip()[:60],
            "fee_waived": _flag(args, "fee_waived"), "notes": _field(args, "notes").strip()[:20_000]}
    opportunity, message = _screen(franchise_screen.create_opportunity, db, user, vals)
    return _done(message, opportunity_id=opportunity.id, person_id=opportunity.person_id, band=opportunity.band,
                 quoted_upfront_pence=opportunity.quoted_upfront_pence, quoted_monthly_pence=opportunity.quoted_monthly_pence)


def _opportunity(db: Session, args: dict):
    return _screen(franchise_screen._load, db, _int(args, "opportunity_id"))


def franchise_move_stage(db: Session, user: User, args: dict) -> dict:
    opportunity_id = _int(args, "opportunity_id")
    message = _press(franchise_screen.change_stage, opportunity_id=opportunity_id, stage=_field(args, "stage", 20),
                     note=_field(args, "note"), override=_flag(args, "override"), paid_at=_field(args, "paid_at", 20), db=db, user=user)
    return _done(message, opportunity_id=opportunity_id)


def franchise_requote(db: Session, user: User, args: dict) -> dict:
    opportunity_id = _int(args, "opportunity_id")
    message = _press(franchise_screen.requote, opportunity_id=opportunity_id, fee_waived=_flag(args, "fee_waived"), db=db, user=user)
    return _done(message, opportunity_id=opportunity_id)


def franchise_update_details(db: Session, user: User, args: dict) -> dict:
    opportunity = _opportunity(db, args)
    vals = {"notes": _field(args, "notes", 20_000, opportunity.notes or "").strip()[:20_000],
            "source": _field(args, "source", 200, opportunity.source or "").strip()[:60],
            "seller_code": _field(args, "seller_code", 100, opportunity.seller.code if opportunity.seller else "").strip()[:24]}
    return _done(_screen(franchise_screen.update_opportunity, db, user, opportunity, vals), opportunity_id=opportunity.id)


def franchise_add_activity(db: Session, user: User, args: dict) -> dict:
    opportunity = _opportunity(db, args)
    vals = {"kind": _field(args, "kind", 20, "note").strip(), "body": _field(args, "body").strip()[:20_000]}
    row, message = _screen(franchise_screen.add_activity_note, db, user, opportunity, vals)
    return _done(message, opportunity_id=opportunity.id, activity_id=row.id)


def franchise_task_done(db: Session, user: User, args: dict) -> dict:
    opportunity_id, task_id = _int(args, "opportunity_id"), _int(args, "task_id")
    return _done(_press(franchise_screen.task_done, opportunity_id=opportunity_id, task_id=task_id, db=db, user=user),
                 opportunity_id=opportunity_id, task_id=task_id)


def franchise_task_skip(db: Session, user: User, args: dict) -> dict:
    opportunity_id, task_id = _int(args, "opportunity_id"), _int(args, "task_id")
    return _done(_press(franchise_screen.task_skip, opportunity_id=opportunity_id, task_id=task_id, db=db, user=user),
                 opportunity_id=opportunity_id, task_id=task_id)


def franchise_record_payment(db: Session, user: User, args: dict) -> dict:
    opportunity = _opportunity(db, args)
    vals = {"basis": _field(args, "basis", 20, "upfront").strip(), "trail_month": _number_text(args, "trail_month").strip(),
            "amount": _number_text(args, "amount").strip(), "paid_at": _field(args, "paid_at", 20).strip(),
            "cleared_at": _field(args, "cleared_at", 20).strip(), "method": _field(args, "method", 20, "manual").strip(),
            "reference": _field(args, "reference", 200).strip()[:120], "notes": _field(args, "notes").strip()[:20_000]}
    payment, message = _screen(franchise_screen.record_franchise_payment, db, user, opportunity, vals)
    return _done(message, opportunity_id=opportunity.id, payment_id=payment.id, cleared=payment.cleared_at is not None)


def franchise_clear_payment(db: Session, user: User, args: dict) -> dict:
    opportunity_id, payment_id = _int(args, "opportunity_id"), _int(args, "payment_id")
    message = _press(franchise_screen.payment_clear, opportunity_id=opportunity_id, payment_id=payment_id,
                     cleared_at=_field(args, "cleared_at", 20), db=db, user=user)
    return _done(message, opportunity_id=opportunity_id, payment_id=payment_id)


def franchise_refund_payment(db: Session, user: User, args: dict) -> dict:
    opportunity_id, payment_id = _int(args, "opportunity_id"), _int(args, "payment_id")
    message = _press(franchise_screen.payment_refund, opportunity_id=opportunity_id, payment_id=payment_id,
                     refunded_at=_field(args, "refunded_at", 20), reason=_field(args, "reason", 2000), db=db, user=user)
    return _done(message, opportunity_id=opportunity_id, payment_id=payment_id)


def franchise_create_hnh_partner(db: Session, user: User, args: dict) -> dict:
    opportunity = _opportunity(db, args)
    done = _screen(franchise_screen.create_hnh_partner, db, user, opportunity, _flag(args, "dry_run"))
    call, result, error = done["call"], done["result"], done["error"]
    if error is not None:
        raise ToolError(f"Hello New Home refused to create the partner: {error.summary()}"[:600])
    out = _done("Dry run of the Hello New Home hand-off recorded; nothing was sent." if result.dry_run else
                f"Local Partner created in Hello New Home (partner id {result.remote_ref or '?'}).",
                opportunity_id=opportunity.id, connector_call_id=call.id, dry_run=result.dry_run, remote_ref=result.remote_ref,
                links=done["links"] or None, postcodes=done["postcodes"], fee_waived=done["fee_waived"])
    if getattr(result, "once", ""):
        out["note"] = ONCE_WITHHELD
    return out


# ==== sellers ==========================================================================================================
def seller_create(db: Session, user: User, args: dict) -> dict:
    vals = sellers_screen._seller_vals(_number_text(args, "person_id"), _field(args, "email", 400), _field(args, "name", 400),
                                _field(args, "phone", 200), _field(args, "kind", 20), _field(args, "code", 100),
                                _field(args, "bank_reference", 400), _flag(args, "self_billing"), _field(args, "self_billing_date", 20),
                                _flag(args, "active", True))
    seller, message = _screen(sellers_screen.create_seller, db, user, vals)
    return _done(message, seller_id=seller.id, person_id=seller.person_id, code=seller.code)


def seller_update(db: Session, user: User, args: dict) -> dict:
    seller = _screen(sellers_screen._load, db, _int(args, "seller_id"))
    agreed = seller.self_billing_agreed_at
    vals = sellers_screen._update_vals(_field(args, "kind", 20, seller.kind), _field(args, "code", 100, seller.code),
                                _field(args, "bank_reference", 400, seller.bank_reference or ""), _flag(args, "self_billing", agreed is not None),
                                _field(args, "self_billing_date", 20, agreed.date().isoformat() if agreed else ""),
                                _flag(args, "active", seller.active))
    return _done(_screen(sellers_screen.update_seller, db, user, seller, vals), seller_id=seller.id, code=seller.code)


def seller_clawback_entry(db: Session, user: User, args: dict) -> dict:
    seller_id, entry_id = _int(args, "seller_id"), _int(args, "entry_id")
    message = _press(sellers_screen.entry_clawback, seller_id=seller_id, entry_id=entry_id, reason=_field(args, "reason", 2000), db=db, user=user)
    return _done(message, seller_id=seller_id, entry_id=entry_id)


def seller_generate_statements(db: Session, user: User, args: dict) -> dict:
    return _done(_press(sellers_screen.generate, period=_field(args, "period", 20), db=db, user=user))


def seller_mark_statement_paid(db: Session, user: User, args: dict) -> dict:
    statement_id = _int(args, "statement_id")
    message = _press(sellers_screen.statement_paid, statement_id=statement_id, paid_at=_field(args, "paid_at", 20), db=db, user=user)
    return _done(message, statement_id=statement_id)


def seller_confirm_rule(db: Session, user: User, args: dict) -> dict:
    rule_id = _int(args, "rule_id")
    return _done(_press(sellers_screen.rule_confirm, rule_id=rule_id, db=db, user=user), rule_id=rule_id)


# ==== the catalogue ====================================================================================================
LAPS_BRANDS = list(laps_service.BRANDS)
LAPS_MOVES = list(laps_screen.MOVES)
ACQ_PROPS = {
    "title": _str("The address or a working name.", 200), "kind": _enum(acquisitions_service.KINDS, "What it is; other by default."),
    "plan": _enum(acquisitions_service.PLANS, "The plan for it; undecided by default."),
    "seller_type": _enum(acquisitions_service.SELLERS, "Who is selling; unknown by default."),
    "units": {"type": ["integer", "string"], "description": "Number of properties or units, 1 to 10000; blank for none."},
    "postcode": _str("Postcode.", 10), "seller_name": _str("Seller's name."), "seller_contact": _str("Seller's phone or email."),
    "situation": _str("Why they are selling.", 300), "source": _str("Where the deal came from.", 100),
    "solicitor": _str("Solicitor."), "next_step": _str("The next step."), "next_step_on": _date("When the next step is due."),
    "lost_reason": _str("Why it was lost."), "asking": _money("Asking price."), "offer": _money("Our offer."),
    "agreed": _money("Agreed price."), "value": _money("Value after works."), "works": _money("Cost of works."),
    "target_completion": _date("Target completion date."), "notes": _str("Notes.", 5000),
}
ACQ_STAGES = list(acquisitions_service.PIPELINE)
PRICE_ON_MOVE_NOTE = "Only for to=offer (sets the offer) or to=agreed (sets the agreed price)."

WRITE_TOOLS: tuple[Tool, ...] = (
    # people
    Tool("people_create", "Add a person to the people register, keyed on their email. Refused if the email is already on the register.",
         _schema({"email": _str("Their email address: the key that links them across the products.", 254),
                  "name": _str("Name.", 120), "phone": _str("Phone.", 40), "notes": _str("Notes.", 20_000)}, ["email"]),
         people_create, write=True),
    Tool("people_update", "Change a person's email, name, phone or notes. The email cannot change while they hold a live grant."
         + KEEP,
         _schema({"person_id": _id("The person."), "email": _str("Email.", 254), "name": _str("Name.", 120), "phone": _str("Phone.", 40),
                  "notes": _str("Notes.", 20_000)}, ["person_id"]), people_update, write=True),
    Tool("people_set_links", "Set a person's linked product ids by hand (Property Predator account, Ordris Clerk user and organisation, "
         "Key Properties user and investor, Hello New Home partner and user). An empty string clears an id; a field left out "
         "keeps its value. Changes only Group HQ's record; nothing is sent to a product.",
         _schema({"person_id": _id("The person."),
                  "pp_account_id": _str("Property Predator account id (users.id), letters, digits and hyphens.", 64),
                  "ordris_user_id": _str("Ordris Clerk user id.", 64), "ordris_org_id": _str("Ordris Clerk organisation id.", 64),
                  "kp_user_id": {"type": ["integer", "string"], "description": "Key Properties user id (whole number)."},
                  "kp_investor_id": {"type": ["integer", "string"], "description": "Key Properties investor id (whole number)."},
                  "hnh_partner_id": {"type": ["integer", "string"], "description": "Hello New Home partner id (whole number)."},
                  "hnh_user_id": {"type": ["integer", "string"], "description": "Hello New Home user id (whole number)."}},
                 ["person_id"]), people_set_links, write=True),
    # mentees
    Tool("mentee_create", "Enrol a person from the register on an active programme. Refused if they are already active on it. "
         "Grants follow at the next recompute.",
         _schema({"person_id": _id("The person."), "programme_id": _id("The programme."),
                  "payment_mode": _enum(PAYMENT_MODES, "full (12 months once paid) or monthly (while paying)."),
                  "start_date": _date("Start date."), "cohort": _str("Cohort label.", 60), "notes": _str("Notes.", 20_000)},
                 ["person_id", "programme_id", "payment_mode", "start_date"]), mentee_create, write=True),
    Tool("mentee_record_payment", "Record a payment a mentee has made, as the Payments tab does: it only writes the register; "
         "no card is charged and no money moves. Only a cleared payment counts towards access.",
         _schema({"mentee_id": _id("The mentee."), "amount": _money("Amount paid."), "paid_at": _date("Date paid."),
                  "cleared_at": _date("Date the money cleared; leave out if it has not."),
                  "method": _enum(PAYMENT_METHODS, "How it arrived; manual by default."),
                  "period_start": _date("Start of the period it pays for (monthly payers)."),
                  "period_end": _date("End of the period it pays for; required for a monthly payer."),
                  "reference": _str("Reference.", 120), "notes": _str("Notes.", 20_000)},
                 ["mentee_id", "amount", "paid_at"]), mentee_record_payment, write=True),
    Tool("mentee_clear_payment", "Mark a recorded mentee payment as cleared (the money has arrived), so it counts towards access. "
         "No card is charged and no money moves.",
         _schema({"mentee_id": _id("The mentee."), "payment_id": _id("The payment."), "cleared_at": _date("Date cleared; today when left out.")},
                 ["mentee_id", "payment_id"]), mentee_clear_payment, write=True),
    Tool("mentee_recompute", "Recompute the mentee's person now: grants and revokes in every product follow from their payments. "
         + LIVE_ACCESS, _schema({"mentee_id": _id("The mentee.")}, ["mentee_id"]), mentee_recompute, write=True),
    Tool("mentee_revoke_all", "Revoke every grant the mentee's person holds and end their mentee rows. " + LIVE_ACCESS,
         _schema({"mentee_id": _id("The mentee."), "reason": _str("Why; kept in Group HQ.", 200)}, ["mentee_id"]),
         mentee_revoke_all, write=True),
    Tool("mentee_pause_all", "Pause all access for the mentee's person: every grant is revoked now and kept off, whatever is paid, "
         "until resumed. " + LIVE_ACCESS,
         _schema({"mentee_id": _id("The mentee."), "reason": _str("Why; kept in Group HQ, never sent to a product.", 200)}, ["mentee_id"]),
         mentee_pause_all, write=True),
    Tool("mentee_resume_all", "Resume all access for the mentee's person and recompute at once, so what the rule supports is granted "
         "again. " + LIVE_ACCESS + " A one-time Hello New Home password is never returned.",
         _schema({"mentee_id": _id("The mentee.")}, ["mentee_id"]), mentee_resume_all, write=True),
    Tool("mentee_update", "Change a mentee's status (active or ended; lapsed is worked out, never set), cohort, payment mode, start "
         "date or notes. Run mentee_recompute afterwards so the products follow." + KEEP,
         _schema({"mentee_id": _id("The mentee."), "status": _enum(MENTEE_STATUSES, "active or ended (lapsed is refused)."),
                  "cohort": _str("Cohort.", 60), "payment_mode": _enum(PAYMENT_MODES, "full or monthly."),
                  "start_date": _date("Start date."), "notes": _str("Notes.", 20_000)}, ["mentee_id"]), mentee_update, write=True),
    # grants
    Tool("grant_recompute_all", "Run the nightly entitlement recompute now for everyone. " + LIVE_ACCESS
         + " With dry_run the calls are only planned and nothing is sent.",
         _schema({"dry_run": _bool("Plan the calls only; nothing is sent."),
                  "today": _date("As-of date; only a dry run may use a day other than today.")}), grant_recompute_all, write=True),
    Tool("grant_recompute_person", "Recompute the person who holds this grant. " + LIVE_ACCESS + " With dry_run nothing is sent.",
         _schema({"grant_id": _id("Any grant of the person."), "dry_run": _bool("Plan the calls only."),
                  "today": _date("As-of date; only a dry run may use a day other than today.")}, ["grant_id"]),
         grant_recompute_person, write=True),
    Tool("grant_retry", "Clear a failed or blocked grant's failure and recompute the person, so the call is tried again. "
         + LIVE_ACCESS + " With dry_run the counter is reset but nothing is sent.",
         _schema({"grant_id": _id("The failed or blocked grant."), "dry_run": _bool("Plan the calls only.")}, ["grant_id"]),
         grant_retry, write=True),
    Tool("grant_pause", "Pause one grant: it is revoked in its product now and kept off by the recompute until resumed. " + LIVE_ACCESS,
         _schema({"grant_id": _id("The grant."), "reason": _str("Why; kept in Group HQ, never sent to the product.", 200)}, ["grant_id"]),
         grant_pause, write=True),
    Tool("grant_resume", "Resume a paused grant and recompute the person at once. " + LIVE_ACCESS
         + " A one-time Hello New Home password is never returned.",
         _schema({"grant_id": _id("The paused grant.")}, ["grant_id"]), grant_resume, write=True),
    # LAPS
    Tool("laps_refresh", "Pull every business's CRM leads onto the LAPS board now (reads the CRMs; nothing is sent to a lead).",
         _schema(), laps_refresh, write=True),
    Tool("laps_create_deals", "Add a lead to the LAPS board: one card per business in brands, linked to each other when there are "
         "several.",
         _schema({"name": _str("The lead's name.", 200), "brands": {"type": "array", "items": {"type": "string", "enum": LAPS_BRANDS},
                                                                    "minItems": 1, "description": "The businesses it is for."},
                  "contact": _str("Phone or email.", 200), "offer": _str("What they want.", 120), "source": _str("Where it came from.", 100),
                  "value": _money("Expected value."), "next_step": _str("The next step."), "next_step_on": _date("When the next step is due."),
                  "notes": _str("Notes.", 5000), "arrived": _date("The day it came in; today when left out, never in the future."),
                  "stage": _enum(laps_service.PIPELINE, "The column to start in; lead by default.")}, ["name", "brands"]),
         laps_create_deals, write=True),
    Tool("laps_add_business", "The same person wants something from another business: add a linked lead card there for each brand "
         "they are not already under.",
         _schema({"deal_id": _id("An existing LAPS card."), "brands": {"type": "array", "items": {"type": "string", "enum": LAPS_BRANDS},
                                                                       "minItems": 1}}, ["deal_id", "brands"]),
         laps_add_business, write=True),
    Tool("laps_edit_deal", "Change a LAPS card's details." + KEEP,
         _schema({"deal_id": _id("The card."), "name": _str("Name.", 200), "contact": _str("Contact.", 200),
                  "brand": _enum(LAPS_BRANDS, "Business."), "offer": _str("Offer.", 120), "source": _str("Source.", 100),
                  "value": _money("Value; an empty string clears it."), "next_step": _str("Next step."),
                  "next_step_on": _date("Next step date; an empty string clears it."), "notes": _str("Notes.", 5000)}, ["deal_id"]),
         laps_edit_deal, write=True),
    Tool("laps_move_deal", "Move a LAPS card: appointment booked, presentation done, sale made, back to lead, lost or reopen.",
         _schema({"deal_id": _id("The card."), "to": _enum(LAPS_MOVES, "Where to move it."),
                  "value": _money("The sale value, to set as it moves.")}, ["deal_id", "to"]), laps_move_deal, write=True),
    Tool("laps_set_targets", "Set the LAPS income goal: the monthly goal and the typical sale value the weekly numbers are worked back "
         "from. An empty string clears one." + KEEP,
         _schema({"monthly_goal": _money("Monthly income goal."), "typical_sale": _money("Typical sale value.")}),
         laps_set_targets, write=True),
    # property deals
    Tool("acquisition_create", "Add a property deal (the buying side) to the board. A thread_ticket from an Inbox conversation in "
         "Group HQ attaches the deal to it.",
         _schema({**ACQ_PROPS, "arrived": _date("The day it came in; today when left out."),
                  "stage": _enum(ACQ_STAGES, "The stage to start at; lead by default."),
                  "thread_ticket": _str("The conversation ticket the Inbox's Add to deal form carries; it expires after two hours.", 4000)},
                 ["title"]), acquisition_create, write=True),
    Tool("acquisition_edit", "Change a property deal's details. An empty string clears a field." + KEEP,
         _schema({"deal_id": _id("The deal."), **ACQ_PROPS}, ["deal_id"]), acquisition_edit, write=True),
    Tool("acquisition_move", "Move a property deal to another stage, mark it lost, or reopen it.",
         _schema({"deal_id": _id("The deal."), "to": _enum(acquisitions_service.MOVES, "Where to move it."),
                  "price": _money("The price to record as it moves. " + PRICE_ON_MOVE_NOTE),
                  "lost_reason": _str("Why it was lost (to=lost).")}, ["deal_id", "to"]), acquisition_move, write=True),
    Tool("acquisition_toggle_check", "Tick a property deal's checklist item, or untick it if it is already ticked.",
         _schema({"deal_id": _id("The deal."), "key": _enum(acquisitions_service.CHECK_KEYS, "The checklist item.")}, ["deal_id", "key"]),
         acquisition_toggle_check, write=True),
    Tool("acquisition_link", "Attach an Inbox conversation to a property deal and file it under Property in the Inbox. Nothing is "
         "sent to anyone.",
         _schema({"deal_id": _id("The deal."),
                  "thread_ticket": _str("The conversation ticket the Inbox carries for this admin; it expires after two hours.", 4000)},
                 ["deal_id", "thread_ticket"]), acquisition_link, write=True),
    Tool("acquisition_unlink", "Remove a conversation from a property deal. The Inbox still has it.",
         _schema({"deal_id": _id("The deal."), "account_key": _str("The conversation's inbox account key.", 200),
                  "thread_sha256": _str("The conversation's thread hash, as the deal's conversations list it.", 128)},
                 ["deal_id", "account_key", "thread_sha256"]), acquisition_unlink, write=True),
    # programmes
    Tool("programme_update", "Change a programme's name, description, active flag, price or grant template (what it grants in "
         "each product). Existing mentees pick up a new template at their next recompute." + KEEP,
         _schema({"programme_id": _id("The programme."), "name": _str("Name.", 120), "description": _str("Description.", 20_000),
                  "active": _bool("Whether new mentees can be enrolled."),
                  "grant_template": {"type": ["array", "string"], "items": {"type": "object"},
                                     "description": "Items {product, kind, value[, months]} (plus fee_share_pct, area_slug, postcodes or "
                                                    "fee_waived where the product reads them), as a list or as JSON text."},
                  "price": _money("Price; an empty string for no price.")}, ["programme_id"]), programme_update, write=True),
    # territories
    Tool("territory_reserve", "Reserve a postcode district for a Hello New Home partner (first refusal), until a date (12 months "
         "by default). Give partner_id, or person_id of someone with a partner id. Hello New Home is not told.",
         _schema({"district": _str("Postcode district, such as SP10.", 10), "partner_id": _id("Hello New Home partner id."),
                  "person_id": _id("A person whose Hello New Home partner id to use."),
                  "reserved_until": _date("When the reservation ends.")}, ["district"]), territory_reserve, write=True),
    Tool("territory_assign", "Make a postcode district a partner's core (exclusive) district. Hello New Home is not told.",
         _schema({"district": _str("Postcode district.", 10), "partner_id": _id("Hello New Home partner id."),
                  "person_id": _id("A person whose Hello New Home partner id to use.")}, ["district"]), territory_assign, write=True),
    Tool("territory_release", "Release a postcode district back to open, whatever it was.",
         _schema({"district": _str("Postcode district.", 10)}, ["district"]), territory_release, write=True),
    Tool("territory_keep_for_hq", "Keep a postcode district for head office, never offered. Refused while a partner holds it.",
         _schema({"district": _str("Postcode district.", 10)}, ["district"]), territory_keep_for_hq, write=True),
    # franchise
    Tool("franchise_create", "Create a franchise opportunity for a person (person_id, or email to find or add them) over postcode "
         "districts, with the quote copied from the territory model and an optional seller code.",
         _schema({"person_id": _id("A person on the register."), "email": _str("Their email, when not giving person_id.", 254),
                  "name": _str("Name, for a person added by email.", 120), "phone": _str("Phone, for a person added by email.", 40),
                  "districts": {"type": "array", "items": {"type": "string", "maxLength": 10}, "minItems": 1,
                                "description": "Postcode districts, open or reserved."},
                  "seller_code": _str("The attributed seller's code.", 24), "source": _str("Source.", 60),
                  "fee_waived": _bool("Waive the fees (a mentee)."), "notes": _str("Notes.", 20_000)}, ["districts"]),
         franchise_create, write=True),
    Tool("franchise_move_stage", "Move a franchise opportunity to another stage. Pack sent creates the follow-up tasks (no email is "
         "sent); Paid records the upfront payment if none is on record. Paid and Onboarding need a cleared upfront fee unless "
         "override is set with a note.",
         _schema({"opportunity_id": _id("The opportunity."), "stage": _enum(OPPORTUNITY_STAGES, "The new stage."),
                  "note": _str("Note for the timeline; required with override.", 20_000),
                  "override": _bool("Move to Paid or Onboarding without a cleared payment."),
                  "paid_at": _date("For Paid: the date the upfront fee was paid; today by default.")}, ["opportunity_id", "stage"]),
         franchise_move_stage, write=True),
    Tool("franchise_requote", "Recompute an opportunity's quote from its districts and today's assumptions (not at Agreement or later).",
         _schema({"opportunity_id": _id("The opportunity."), "fee_waived": _bool("Quote with the fees waived.")}, ["opportunity_id"]),
         franchise_requote, write=True),
    Tool("franchise_update_details", "Change an opportunity's notes, source or seller attribution (an empty seller_code removes it)."
         + KEEP,
         _schema({"opportunity_id": _id("The opportunity."), "notes": _str("Notes.", 20_000), "source": _str("Source.", 60),
                  "seller_code": _str("Seller code.", 24)}, ["opportunity_id"]), franchise_update_details, write=True),
    Tool("franchise_add_activity", "Record a note, call or email on an opportunity's timeline. It records what happened; nothing is sent.",
         _schema({"opportunity_id": _id("The opportunity."), "kind": _enum(("note", "call", "email"), "What happened; note by default."),
                  "body": _str("What happened.", 20_000)}, ["opportunity_id", "body"]), franchise_add_activity, write=True),
    Tool("franchise_task_done", "Mark an opportunity's follow-up task done (no email is sent).",
         _schema({"opportunity_id": _id("The opportunity."), "task_id": _id("The task.")}, ["opportunity_id", "task_id"]),
         franchise_task_done, write=True),
    Tool("franchise_task_skip", "Skip an opportunity's follow-up task.",
         _schema({"opportunity_id": _id("The opportunity."), "task_id": _id("The task.")}, ["opportunity_id", "task_id"]),
         franchise_task_skip, write=True),
    Tool("franchise_record_payment", "Record a franchise fee received (the upfront fee or one month of the monthly fee). It only "
         "writes the register: no card is charged and no money moves. Recorded as cleared, it accrues any seller commission.",
         _schema({"opportunity_id": _id("The opportunity."), "basis": _enum(FRANCHISE_PAYMENT_BASES, "upfront (default) or monthly."),
                  "trail_month": _id("For a monthly fee: the month it pays for, 1 for the first."), "amount": _money("Amount paid."),
                  "paid_at": _date("Date paid."), "cleared_at": _date("Date cleared; leave out if not yet."),
                  "method": _enum(PAYMENT_METHODS, "How it arrived; manual by default."), "reference": _str("Reference.", 120),
                  "notes": _str("Notes.", 20_000)}, ["opportunity_id", "amount", "paid_at"]), franchise_record_payment, write=True),
    Tool("franchise_clear_payment", "Mark a recorded franchise fee as cleared, which is when seller commission accrues. No money moves.",
         _schema({"opportunity_id": _id("The opportunity."), "payment_id": _id("The payment."), "cleared_at": _date("Date cleared; today by default.")},
                 ["opportunity_id", "payment_id"]), franchise_clear_payment, write=True),
    Tool("franchise_refund_payment", "Mark a franchise fee as refunded (the refund itself is made outside Group HQ; no money moves "
         "here). Seller commission earned on it is clawed back inside the 90-day window.",
         _schema({"opportunity_id": _id("The opportunity."), "payment_id": _id("The payment."), "reason": _str("Why it is refunded.", 500),
                  "refunded_at": _date("Date refunded; today by default.")}, ["opportunity_id", "payment_id", "reason"]),
         franchise_refund_payment, write=True),
    Tool("franchise_create_hnh_partner", "Create the Local Partner in Hello New Home for a sold territory (Paid or Onboarding, upfront "
         "fee cleared or waived), with the opportunity's districts as their area. This creates a real partner account in Hello New "
         "Home, live unless Hello New Home is in Dry run or dry_run is set. A one-time password is never returned.",
         _schema({"opportunity_id": _id("The opportunity."), "dry_run": _bool("Record the plan only; nothing is sent.")}, ["opportunity_id"]),
         franchise_create_hnh_partner, write=True),
    # sellers
    Tool("seller_create", "Create a seller (who may earn commission for selling a territory) from a person (person_id, or email to "
         "find or add them). The code is generated when left out.",
         _schema({"person_id": _id("A person on the register."), "email": _str("Their email, when not giving person_id.", 254),
                  "name": _str("Name, for a person added by email.", 120), "phone": _str("Phone.", 40),
                  "kind": _enum(SELLER_KINDS, "mentee, franchisee or outsider."), "code": _str("Tracked-link code.", 24),
                  "bank_reference": _str("Bank reference.", 120), "self_billing": _bool("A self-billing agreement is signed."),
                  "self_billing_date": _date("The date it was signed (required with self_billing)."),
                  "active": _bool("Active; true by default.")}, ["kind"]), seller_create, write=True),
    Tool("seller_update", "Change a seller's kind, code, bank reference, self-billing agreement or active flag."
         + KEEP,
         _schema({"seller_id": _id("The seller."), "kind": _enum(SELLER_KINDS, "Kind."), "code": _str("Code.", 24),
                  "bank_reference": _str("Bank reference.", 120), "self_billing": _bool("A self-billing agreement is signed."),
                  "self_billing_date": _date("The date it was signed."), "active": _bool("Active.")}, ["seller_id"]),
         seller_update, write=True),
    Tool("seller_clawback_entry", "Claw back one commission entry by hand, with a reason.",
         _schema({"seller_id": _id("The seller."), "entry_id": _id("The entry."), "reason": _str("Why.", 500)},
                 ["seller_id", "entry_id", "reason"]), seller_clawback_entry, write=True),
    Tool("seller_generate_statements", "Generate the self-billing commission statements for a month that has ended (the previous month "
         "by default).", _schema({"period": _str("The month as YYYY-MM.", 7)}), seller_generate_statements, write=True),
    Tool("seller_mark_statement_paid", "Record that a commission statement was paid on a date (the transfer is made outside Group HQ; "
         "no money moves here). Stamps the statement and its entries.",
         _schema({"statement_id": _id("The statement."), "paid_at": _date("Date of the transfer; today by default.")}, ["statement_id"]),
         seller_mark_statement_paid, write=True),
    Tool("seller_confirm_rule", "Record the founder's sign-off of a commission rule, which removes its [CONFIRM] badge.",
         _schema({"rule_id": _id("The commission rule.")}, ["rule_id"]), seller_confirm_rule, write=True),
)

