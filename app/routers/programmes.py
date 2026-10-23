"""Programmes: the packages a person can be enrolled on, each with a grant template.

The template is a JSON list of items {product, kind, value[, months]}: product is one of pp, ordris,
kp, hnh; kind one of comp_tier, org_plan, sourcer_link, partner_seat, flag, affiliate; value the product's own
name for the right (pro_buyer, starter, area, mentee, kpuk.submit, affiliate); months, when present, fixes the
item's term instead of following the mentee's computed end date. A few items take one more key the
connector reads: fee_share_pct (kp sourcer_link), area_slug or postcodes and fee_waived (hnh
partner_seat). The shape is validated here so the engine never meets an item it cannot act on, and a
template may name PP comp_tier once only (PP holds one comp per account). An affiliate item enrols the person in
a product's public affiliate or partner programme and exists for Property Predator and Ordris only (Key Properties
pays sourcers through the sourcer link, Hello New Home partners through the partner seat), once per product.
Programmes are seeded (app.seed) and edited here, never created from a screen: the codes are the plan's.

price is what the programme costs: for a mentee paying in full, access starts once the cleared
payments reach it. Left blank, any cleared payment starts the term.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import GRANT_KINDS, PRODUCTS, Mentee, Programme, User
from ..services.audit import audit, changes
from ..templating import go, render
from .people import get_or_404

router = APIRouter(dependencies=[Depends(check_origin)])

ITEM_KEYS = ("product", "kind", "value", "months")
# Keys a connector reads from the item beyond the four above, by (product, kind).
EXTRA_KEYS: dict[tuple[str, str], tuple[str, ...]] = {
    ("kp", "sourcer_link"): ("fee_share_pct",),
    ("hnh", "partner_seat"): ("area_slug", "postcodes", "fee_waived"),
}
# The products whose public affiliate or partner programme Group HQ can enrol someone in (contracts 1 and 2,
# docs/connectors.md).
AFFILIATE_PRODUCTS = {"pp": "Property Predator affiliate programme", "ordris": "Ordris partner programme"}
MAX_MONTHS = 13            # twelve months is the longest term sold; one more covers a renewal landing early
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
OUTWARD_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")


def _check_extras(i: int, item: dict, product: str, kind: str, clean: dict) -> str:
    """Validate and copy the per-product extras into clean; '' when fine, otherwise why not."""
    allowed = EXTRA_KEYS.get((product, kind), ())
    present = [k for k in item if k not in ITEM_KEYS]
    unknown = sorted(k for k in present if k not in allowed)
    if unknown:
        hint = f" For {product} {kind} the extra keys allowed are: {', '.join(allowed)}." if allowed else ""
        return (f"Item {i} has unexpected key{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
                f"Only product, kind, value and months are allowed.{hint}")
    if "fee_share_pct" in item:
        fee = item["fee_share_pct"]
        if isinstance(fee, bool) or not isinstance(fee, int) or not 0 <= fee <= 100:
            return f"Item {i}: fee_share_pct must be a whole number from 0 to 100."
        clean["fee_share_pct"] = fee
    if "area_slug" in item and "postcodes" in item:
        return f"Item {i}: give area_slug or postcodes, not both."
    if "area_slug" in item:
        slug = item["area_slug"]
        if not isinstance(slug, str) or not SLUG_RE.match(slug.strip()):
            return f"Item {i}: area_slug must be a slug such as swansea or south-wales."
        clean["area_slug"] = slug.strip()
    if "postcodes" in item:
        codes = item["postcodes"]
        if not isinstance(codes, list) or not codes or not all(isinstance(c, str) for c in codes):
            return f"Item {i}: postcodes must be a list of outward codes such as [\"SA1\", \"SA2\"]."
        cleaned = [c.strip().upper().replace(" ", "") for c in codes]
        if any(not OUTWARD_RE.match(c) for c in cleaned):
            return f"Item {i}: every postcode must be an outward code such as SA1 or CF10."
        clean["postcodes"] = cleaned
    if "fee_waived" in item:
        if not isinstance(item["fee_waived"], bool):
            return f"Item {i}: fee_waived must be true or false."
        clean["fee_waived"] = item["fee_waived"]
    return ""


def parse_price(raw: str) -> tuple[Decimal | None, str]:
    """(price, '') from the form, or (None, why) when it cannot be read. Blank is no price."""
    text = (raw or "").strip().replace("£", "").replace(",", "")
    if not text:
        return None, ""
    if not re.match(r"^\d{1,8}(?:\.\d{1,2})?$", text):
        return None, "The price must be pounds and pence, for example 4995.00, or blank for no price."
    value = Decimal(text).quantize(Decimal("0.01"))
    if value <= 0:
        return None, "The price must be above zero, or blank for no price."
    return value, ""


def parse_grant_template(raw: str) -> tuple[list[dict], str]:
    """(normalised items, '') or ([], why it was refused). An empty template is allowed: it grants nothing."""
    text = (raw or "").strip()
    if not text:
        return [], ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [], f"The template is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}."
    if not isinstance(data, list):
        return [], "The template must be a JSON list of items, each {\"product\", \"kind\", \"value\"} with an optional \"months\"."
    items: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    pp_comp_seen = False
    affiliate_seen: set[str] = set()
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            return [], f"Item {i} must be an object with product, kind and value."
        product, kind, value = item.get("product"), item.get("kind"), item.get("value")
        if product not in PRODUCTS:
            return [], f"Item {i}: product must be one of {', '.join(PRODUCTS)}."
        if kind not in GRANT_KINDS:
            return [], f"Item {i}: kind must be one of {', '.join(GRANT_KINDS)}."
        if (product, kind) == ("pp", "comp_tier"):
            if pp_comp_seen:
                return [], f"Item {i}: a template may give one Property Predator tier only; the account holds one comp at a time."
            pp_comp_seen = True
        if kind == "affiliate":
            if product not in AFFILIATE_PRODUCTS:
                return [], (f"Item {i}: an affiliate item is for Property Predator or Ordris only. Key Properties pays its "
                            "sourcers through the sourcer link and Hello New Home its partners through the partner seat.")
            if product in affiliate_seen:
                return [], f"Item {i}: a template may enrol someone in the {AFFILIATE_PRODUCTS[product]} once only."
            affiliate_seen.add(product)
        if not isinstance(value, str) or not value.strip():
            return [], f"Item {i}: value must be the product's name for the right, for example pro_buyer or starter."
        value = value.strip()
        if len(value) > 120:
            return [], f"Item {i}: value is longer than 120 characters."
        clean = {"product": product, "kind": kind, "value": value}
        if "months" in item:
            months = item["months"]
            if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= MAX_MONTHS:
                return [], f"Item {i}: months must be a whole number from 1 to {MAX_MONTHS}, or left out to follow the mentee's end date."
            clean["months"] = months
        problem = _check_extras(i, item, product, kind, clean)
        if problem:
            return [], problem
        key = (product, kind, value)
        if key in seen:
            return [], f"Item {i} repeats an earlier item ({product} {kind} {value})."
        seen.add(key)
        items.append(clean)
    return items, ""


def _template_text(items: list) -> str:
    return json.dumps(items or [], indent=2, ensure_ascii=False)


def programme_snapshot(p: Programme) -> dict:
    return {"name": p.name, "description": p.description, "active": p.active, "grant_template": p.grant_template,
            "price": str(p.price) if p.price is not None else None}


@router.get("/programmes", response_class=HTMLResponse)
def programmes_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    programmes = db.query(Programme).order_by(Programme.active.desc(), Programme.name).all()
    counts = {pid: n for pid, n in db.query(Mentee.programme_id, func.count(Mentee.id))
              .filter(Mentee.status == "active").group_by(Mentee.programme_id).all()}
    ctx = {"user": user, "section": "programmes", "programmes": programmes, "active_counts": counts}
    return render(request, "programmes.html", ctx)


def _programme_form(request: Request, user: User, programme: Programme, vals: dict, *, error: str = "", status: int = 200) -> HTMLResponse:
    ctx = {"user": user, "section": "programmes", "programme": programme, "vals": vals, "products": PRODUCTS, "kinds": GRANT_KINDS}
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "programme_form.html", ctx, status)


@router.get("/programmes/{programme_id}", response_class=HTMLResponse)
def programme_edit(programme_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    programme = get_or_404(db, Programme, programme_id)
    vals = {"name": programme.name, "description": programme.description, "active": programme.active,
            "grant_template": _template_text(programme.grant_template),
            "price": f"{programme.price:.2f}" if programme.price is not None else ""}
    return _programme_form(request, user, programme, vals)


@router.post("/programmes/{programme_id}", response_class=HTMLResponse)
def programme_update(programme_id: int, request: Request, name: str = Form(""), description: str = Form(""), active: str = Form(""),
                     grant_template: str = Form(""), price: str = Form(""), db: Session = Depends(get_db),
                     user: User = Depends(require_admin)):
    programme = get_or_404(db, Programme, programme_id)
    vals = {"name": name.strip()[:120], "description": description.strip()[:20_000], "active": active == "1",
            "grant_template": grant_template.strip()[:100_000], "price": price.strip()[:20]}
    errors: list[str] = []
    if not vals["name"]:
        errors.append("A name is required.")
    items, problem = parse_grant_template(vals["grant_template"])
    if problem:
        errors.append(problem)
    price_value, price_problem = parse_price(vals["price"])
    if price_problem:
        errors.append(price_problem)
    if errors:
        return _programme_form(request, user, programme, vals, error=" ".join(errors), status=400)
    before = programme_snapshot(programme)
    programme.name = vals["name"]
    programme.description = vals["description"]
    programme.active = vals["active"]
    programme.grant_template = items
    programme.price = price_value
    b, a = changes(before, programme_snapshot(programme))
    if not a:
        return go(f"/programmes/{programme.id}", ok="No changes to save.")
    db.add(programme)
    audit(db, user, "programme.update", f"programme:{programme.id}", before=b, after=a)
    db.commit()
    hint = " Existing mentees pick up the new template at their next recompute." if "grant_template" in a else ""
    if "price" in a:
        hint += " A mentee paying in full now needs cleared payments that reach the price before the term starts."
    return go(f"/programmes/{programme.id}", ok=f"{programme.name} saved.{hint}")
