"""People: the register of humans across the four products.

A person is keyed by person_key, the lower-cased and trimmed email (plan, decision 8). It is normalised
on every save and a second spelling of the same address is refused, so two rows can never need merging.
The linked ids per product (pp_account_id, ordris_*, kp_*, hnh_*) are set by hand here until each
connector's lookup fills them.

Conventions (as in Hello New Home HQ): every route sits behind require_admin and the CSRF guard;
writes call audit() then commit; a POST redirects with 303 and a flash in ?ok= or ?err=; a form that
fails validation re-renders with the submitted values and a 400 so nothing typed is lost.

This module also hosts the small form helpers the other register routers share (get_or_404,
parse_int, parse_date, like_pattern, page_of).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..auth import check_origin, normalise_email, require_admin
from ..db import get_db
from ..models import ConnectorCall, EntitlementGrant, Mentee, Person, PersonOrganisation, User
from ..services import settings
from ..services.audit import audit, changes
from ..services.entitlements import hold_of
from ..templating import go, render

router = APIRouter(dependencies=[Depends(check_origin)])

# ASCII only: the register keys on lower(trim(email)) and Python's lower() and Postgres's lower() can disagree
# on a few non-ASCII characters (docs/shared-keys.md), so an address outside ASCII cannot be a reliable key.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
RENAME_REFUSED = ("Revoke every grant before changing the email: the products are keyed on it, and a grant under "
                  "the old address would stay live in the product.")
PAGE_SIZE = 50

# (column, product, label, type). Integer ids are the products' own primary keys; Ordris ids are Clerk strings.
LINK_FIELDS: tuple[tuple[str, str, str, type], ...] = (
    ("pp_account_id", "pp", "Account id (users.id)", int),
    ("ordris_user_id", "ordris", "Clerk user id", str),
    ("ordris_org_id", "ordris", "Clerk organisation id", str),
    ("kp_user_id", "kp", "User id", int),
    ("kp_investor_id", "kp", "Investor id", int),
    ("hnh_partner_id", "hnh", "Partner id", int),
    ("hnh_user_id", "hnh", "User id", int),
)
LINK_COLUMNS = tuple(f[0] for f in LINK_FIELDS)


# ---- helpers shared by the register routers -------------------------------------------
def get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} {obj_id} was not found.")
    return obj


def parse_int(raw, default: int = 0) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def parse_date(raw: str, label: str = "date") -> date | None:
    """A date typed into a form: ISO (what <input type=date> sends) or dd/mm/yyyy. Blank is None."""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"The {label} does not look like a date. Use the picker or type it as 2026-10-01.")


def like_pattern(term: str) -> str:
    """Escape LIKE wildcards in user input so a search for 100% means 100%."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def page_of(query, request: Request, page_size: int = PAGE_SIZE) -> tuple[list, int, int, int]:
    """(rows, total, page, pages) for ?page= on an ordered query."""
    page = max(1, parse_int(request.query_params.get("page", "1"), 1))
    total = query.count()
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return rows, total, page, pages


def qs(filters: dict, **extra) -> str:
    params = {k: v for k, v in filters.items() if v}
    params.update({k: v for k, v in extra.items() if v})
    return urlencode(params)


# ---- person lookups --------------------------------------------------------------------------
def person_key_problem(key: str) -> str:
    """Why an email cannot be a person key, or '' when it can."""
    if not key:
        return "An email address is required: it is the key that links this person across the products."
    if len(key) > 254 or not EMAIL_RE.match(key):
        return "That email address does not look right. The register keys on plain ASCII addresses."
    return ""


def holds_live_grants(db: Session, person: Person) -> bool:
    """True while any grant of theirs is active or pending, or any connector call for one is still pending:
    the products know the person by email, so the key cannot change under a live grant."""
    live = (db.query(EntitlementGrant.id).filter(EntitlementGrant.person_id == person.id,
                                                 EntitlementGrant.state.in_(("active", "pending"))).first())
    if live is not None:
        return True
    pending_call = (db.query(ConnectorCall.id).join(EntitlementGrant, ConnectorCall.grant_id == EntitlementGrant.id)
                    .filter(EntitlementGrant.person_id == person.id, ConnectorCall.status == "pending").first())
    return pending_call is not None


def find_person_by_key(db: Session, key: str, exclude_id: int | None = None) -> Person | None:
    """Compared on lower(person_key), the same expression as the unique index, so any spelling is found."""
    norm = normalise_email(key)
    if not norm:
        return None
    q = db.query(Person).filter(func.lower(Person.person_key) == norm)
    if exclude_id is not None:
        q = q.filter(Person.id != exclude_id)
    return q.first()


def person_snapshot(person: Person) -> dict:
    return {"person_key": person.person_key, "name": person.name, "phone": person.phone, "notes": person.notes}


def links_snapshot(person: Person) -> dict:
    return {col: getattr(person, col) for col in LINK_COLUMNS}


def link_groups(person: Person | None, vals: dict | None = None) -> list[dict]:
    """The link fields grouped per product for the detail page, with the current (or re-submitted) values."""
    groups: dict[str, dict] = {}
    for col, product, label, kind in LINK_FIELDS:
        group = groups.setdefault(product, {"code": product, "name": settings.product_name(product), "fields": []})
        value = vals.get(col, "") if vals is not None else (getattr(person, col) if person else None)
        group["fields"].append({"name": col, "label": label, "value": "" if value is None else value,
                                "numeric": kind is int})
    return list(groups.values())


# ---- list ---------------------------------------------------------------------------------
@router.get("/people", response_class=HTMLResponse)
def people_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    term = request.query_params.get("q", "").strip()[:120]
    q = db.query(Person).options(selectinload(Person.mentees))
    if term:
        like = like_pattern(term.lower())
        q = q.filter(or_(func.lower(Person.person_key).like(like, escape="\\"),
                         func.lower(Person.name).like(like, escape="\\"),
                         Person.phone.like(like_pattern(term), escape="\\")))
    q = q.order_by(Person.person_key)
    people, total, page, pages = page_of(q, request)
    filters = {"q": term}
    ctx = {"user": user, "section": "people", "people": people, "total": total, "page": page, "pages": pages,
           "filters": filters, "qs": qs(filters), "qs_prev": qs(filters, page=page - 1), "qs_next": qs(filters, page=page + 1),
           "page_size": PAGE_SIZE}
    return render(request, "people.html", ctx)


# ---- create ------------------------------------------------------------------------------
def _person_form(request: Request, user: User, vals: dict, *, error: str = "", status: int = 200) -> HTMLResponse:
    ctx = {"user": user, "section": "people", "vals": vals}
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "person_form.html", ctx, status)


@router.get("/people/new", response_class=HTMLResponse)
def person_new(request: Request, user: User = Depends(require_admin)):
    return _person_form(request, user, {"email": request.query_params.get("email", "").strip(), "name": "", "phone": "", "notes": ""})


@router.post("/people/new", response_class=HTMLResponse)
def person_create(request: Request, email: str = Form(""), name: str = Form(""), phone: str = Form(""), notes: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(require_admin)):
    vals = {"email": email.strip(), "name": name.strip()[:120], "phone": phone.strip()[:40], "notes": notes.strip()[:20_000]}
    key = normalise_email(email)
    problem = person_key_problem(key)
    if problem:
        return _person_form(request, user, vals, error=problem, status=400)
    existing = find_person_by_key(db, key)
    if existing is not None:
        return _person_form(request, user, vals, status=400,
                            error=f"{key} is already on the register as person #{existing.id}. Open that record rather than adding a second one.")
    person = Person(person_key=key, name=vals["name"], phone=vals["phone"], notes=vals["notes"])
    db.add(person)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _person_form(request, user, vals, error=f"{key} is already on the register.", status=400)
    audit(db, user, "person.create", f"person:{person.id}", after=person_snapshot(person))
    db.commit()
    return go(f"/people/{person.id}", ok=f"{key} added to the register.")


# ---- detail -----------------------------------------------------------------------------
def _person_page(request: Request, db: Session, user: User, person: Person, *, vals: dict | None = None,
                 link_vals: dict | None = None, error: str = "", status: int = 200) -> HTMLResponse:
    mentees = (db.query(Mentee).options(selectinload(Mentee.programme)).filter(Mentee.person_id == person.id)
               .order_by(Mentee.id.desc()).all())
    grants = (db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id)
              .order_by(EntitlementGrant.product, EntitlementGrant.kind, EntitlementGrant.id).all())
    org_links = (db.query(PersonOrganisation).options(selectinload(PersonOrganisation.organisation))
                 .filter(PersonOrganisation.person_id == person.id).all())
    ctx = {"user": user, "section": "people", "person": person, "mentees": mentees, "grants": grants, "org_links": org_links,
           "vals": vals or {"email": person.person_key, "name": person.name, "phone": person.phone, "notes": person.notes},
           "link_groups": link_groups(person, link_vals), "hold_of": hold_of}
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "person.html", ctx, status)


@router.get("/people/{person_id}", response_class=HTMLResponse)
def person_detail(person_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    person = get_or_404(db, Person, person_id)
    return _person_page(request, db, user, person)


@router.post("/people/{person_id}", response_class=HTMLResponse)
def person_update(person_id: int, request: Request, email: str = Form(""), name: str = Form(""), phone: str = Form(""),
                  notes: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    person = get_or_404(db, Person, person_id)
    vals = {"email": email.strip(), "name": name.strip()[:120], "phone": phone.strip()[:40], "notes": notes.strip()[:20_000]}
    key = normalise_email(email)
    problem = person_key_problem(key)
    if problem:
        return _person_page(request, db, user, person, vals=vals, error=problem, status=400)
    other = find_person_by_key(db, key, exclude_id=person.id)
    if other is not None:
        return _person_page(request, db, user, person, vals=vals, status=400,
                            error=f"{key} already belongs to person #{other.id}. Two records cannot share an email.")
    if key != person.person_key and holds_live_grants(db, person):
        return _person_page(request, db, user, person, vals=vals, status=400, error=RENAME_REFUSED)
    before = person_snapshot(person)
    person.person_key = key
    person.name = vals["name"]
    person.phone = vals["phone"]
    person.notes = vals["notes"]
    b, a = changes(before, person_snapshot(person))
    if not a:
        return go(f"/people/{person.id}", ok="No changes to save.")
    db.add(person)
    audit(db, user, "person.update", f"person:{person.id}", before=b, after=a)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _person_page(request, db, user, person, vals=vals, error=f"{key} is already on the register.", status=400)
    return go(f"/people/{person.id}", ok="Person saved.")


@router.post("/people/{person_id}/links", response_class=HTMLResponse)
async def person_links(person_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Set the product ids by hand. Blank clears an id; a PP account id held by another person is refused."""
    person = get_or_404(db, Person, person_id)
    form = await request.form()
    submitted = {col: (form.get(col) if isinstance(form.get(col), str) else "").strip()[:64] for col in LINK_COLUMNS}
    new: dict[str, int | str | None] = {}
    errors: list[str] = []
    for col, product, label, kind in LINK_FIELDS:
        raw = submitted[col]
        if raw == "":
            new[col] = None
        elif kind is int:
            if not raw.isdigit() or int(raw) <= 0:
                errors.append(f"{settings.product_name(product)} {label.lower()} must be a whole number.")
            else:
                new[col] = int(raw)
        else:
            new[col] = raw
    if not errors and new.get("pp_account_id") is not None:
        holder = (db.query(Person).filter(Person.pp_account_id == new["pp_account_id"], Person.id != person.id).first())
        if holder is not None:
            errors.append(f"Property Predator account {new['pp_account_id']} is already linked to {holder.person_key} (person #{holder.id}).")
    if errors:
        return _person_page(request, db, user, person, link_vals=submitted, error=" ".join(errors), status=400)
    before = links_snapshot(person)
    for col, value in new.items():
        setattr(person, col, value)
    b, a = changes(before, links_snapshot(person))
    if not a:
        return go(f"/people/{person.id}", ok="The linked ids are unchanged.")
    db.add(person)
    audit(db, user, "person.links", f"person:{person.id}", before=b, after=a)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _person_page(request, db, user, person, link_vals=submitted, status=400,
                            error="That Property Predator account id is already linked to another person.")
    return go(f"/people/{person.id}", ok="Linked ids saved.")
