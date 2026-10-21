"""Existing users to mentees: tick people who already have a Key Properties or Ordris login and enrol them in one go.

    GET  /mentees/import     the Key Properties sourcers (fetched live), a box for anyone else, the shared fields
    POST /mentees/import     run the batch and show one result row per person

The work itself is app.services.mentee_import. Same conventions as the rest of the register: require_admin on
both routes, the check_origin CSRF guard on the POST, an audit row for every write, and a form that fails
validation re-renders with what was typed (ticks included) and a 400. A finished batch renders its results
page directly rather than redirecting, as a recompute run does, because the page is the report (and may carry
a Hello New Home temporary password that is shown once and never stored).

These routes are mounted before the mentees router, so /mentees/import is never read as /mentees/{mentee_id}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import Programme, User
from ..services import mentee_import as imp
from ..templating import render
from .mentees import MODE_LABELS, engine_available, money, pence_to_amount
from .people import parse_int

router = APIRouter(dependencies=[Depends(check_origin)])

TITLE = "Existing users to mentees"


def _defaults(db: Session) -> dict:
    mentorship = db.query(Programme).filter(Programme.code == "mentorship", Programme.active.is_(True)).first()
    today = imp.today().isoformat()
    return {"programme_id": str(mentorship.id) if mentorship else "", "payment_mode": "full", "start_date": today,
            "amount": "", "paid_on": today, "cleared": "1", "period_end": "", "grant_now": "1", "lines": ""}


def _form_page(request: Request, db: Session, user: User, vals: dict, *, offset: int = 0, picked: set[str] | None = None,
               error: str = "", status: int = 200) -> HTMLResponse:
    listing = imp.kp_listing(offset)
    badges = imp.register_badges(db, [row["email"] for row in listing["rows"]])
    programmes = db.query(Programme).order_by(Programme.active.desc(), Programme.name).all()
    total = listing["total"]
    shown_to = listing["offset"] + len(listing["rows"])
    ctx = {
        "user": user, "section": "mentees_import", "title_text": TITLE, "vals": vals, "listing": listing, "badges": badges,
        "picked": picked or set(), "programmes": programmes, "modes": list(MODE_LABELS.items()),
        "product_modes": imp.product_modes(), "max_batch": imp.MAX_BATCH, "engine_available": engine_available(),
        "prev_offset": max(0, listing["offset"] - listing["limit"]) if listing["offset"] > 0 else None,
        "next_offset": shown_to if (total is not None and shown_to < total) or (total is None and len(listing["rows"]) >= listing["limit"]) else None,
    }
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "mentee_import.html", ctx, status)


@router.get("/mentees/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    offset = max(0, parse_int(request.query_params.get("offset", "0"), 0))
    return _form_page(request, db, user, _defaults(db), offset=offset)


@router.post("/mentees/import", response_class=HTMLResponse)
def import_run(request: Request, pick: list[str] = Form(default=[]), kp_email: list[str] = Form(default=[]),
               kp_name: list[str] = Form(default=[]), lines: str = Form(""), programme_id: str = Form(""),
               payment_mode: str = Form(""), start_date: str = Form(""), amount: str = Form(""), paid_on: str = Form(""),
               cleared: str = Form(""), period_end: str = Form(""), grant_now: str = Form(""), offset: str = Form("0"),
               db: Session = Depends(get_db), user: User = Depends(require_admin)):
    vals = {"programme_id": programme_id.strip(), "payment_mode": payment_mode.strip(), "start_date": start_date.strip(),
            "amount": amount.strip(), "paid_on": paid_on.strip(), "cleared": "1" if cleared else "",
            "period_end": period_end.strip(), "grant_now": "1" if grant_now else "", "lines": lines[:imp.MAX_PASTE_CHARS]}
    page_offset = max(0, parse_int(offset, 0))
    ticked, problems = imp.picked_rows(pick, kp_email, kp_name)
    pasted, line_problems = imp.parse_lines(vals["lines"])
    problems.extend(line_problems)
    shared, shared_errors = imp.check_shared(db, vals)
    entries = imp.merge(ticked, pasted)
    errors = list(shared_errors)
    if problems:
        errors.append("Some people could not be read, so nothing was imported: " + " ".join(problems))
    if not entries and not problems:
        errors.append("Tick at least one Key Properties user or paste at least one line.")
    if len(entries) > imp.MAX_BATCH:
        errors.append(f"A batch is capped at {imp.MAX_BATCH} people and this one has {len(entries)}. Import them in two goes.")
    if errors:
        return _form_page(request, db, user, vals, offset=page_offset, picked={e.email for e in ticked},
                          error=" ".join(errors), status=400)
    assert shared is not None
    outcomes = imp.run_batch(db, user, entries, shared)
    ctx = {
        "user": user, "section": "mentees_import", "title_text": TITLE, "outcomes": outcomes, "shared": shared,
        "amount_text": money(pence_to_amount(shared.pence)) if shared.pence else "", "mode_labels": MODE_LABELS,
        "product_modes": imp.product_modes(), "link_labels": imp.LINK_LABELS,
        "counts": {
            "people": len(outcomes),
            "created": sum(o.person == "created" for o in outcomes),
            "existing": sum(o.person == "existing" for o in outcomes),
            "failed": sum(o.person == "failed" for o in outcomes),
            "mentees": sum(o.mentee == "created" for o in outcomes),
            "skipped": sum(o.mentee == "skipped" for o in outcomes),
            "payments": sum(o.payment_recorded for o in outcomes),
            "sent": sum(o.grant_counts.get("sent", 0) for o in outcomes),
            "planned": sum(o.grant_counts.get("planned", 0) for o in outcomes),
            "grant_failures": sum(o.grant_counts.get("failed", 0) for o in outcomes) + sum(o.grants == "failed" for o in outcomes),
            "lookup_failures": sum(1 for o in outcomes if o.lookup_problems),
        },
    }
    response = render(request, "mentee_import_result.html", ctx)
    response.headers["Cache-Control"] = "no-store"     # may carry a Hello New Home temporary password, shown once
    return response
