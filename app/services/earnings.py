"""Earning links: every way a mentee can earn through the group, product by product, for the mentee and person pages,
the WhatsApp welcome, and the "Needs a region" queue (docs/mentee-earnings.md).

    earning_links(db, person)          asks the four products at once, each within EARNINGS_TIMEOUT, and reads the
                                       person's Group HQ seller; a product in dry run is not asked ("dry run"), one
                                       without a token or base URL is "not configured", and one that fails or does not
                                       answer in time is "couldn't reach <product>". The page that shows it loads it
                                       after it has rendered, so a slow product never holds the page. A product is
                                       asked about the person only when they hold or are due its earning right, or
                                       Group HQ already has their id there (_askable): the person page of a prospect or
                                       an outside seller sends their email to no product.
    welcome_message(person, programme, links)
                                       the plain WhatsApp message the mentee page copies: every link the mentee has,
                                       in British English, with no figure or promise beyond each scheme's own terms
    needs_region(db) / needs_region_person_ids(db)
                                       active mentees whose Hello New Home partner has no area, from what the
                                       recompute last read and kept on the grant (no live call)

What each product answers (contracts 1 to 4, docs/connectors.md): Property Predator the affiliate link, referred
accounts and commission due and paid; Ordris the referral link, referrals and pending, payable and paid (and, when its
answer says so, whether the partner has accepted its partner terms: "terms not accepted" otherwise); Key Properties
the sourcer login, share, whether the sourcer is linked to the PP account, deals, earned and pipeline; Hello New Home
the partner portal, the referral link and the area, or "Needs a region" with a button to the head-office page where
the founder assigns it. Money arrives in pence and is shown in pounds. Where a product cannot be asked live, the code
and link the recompute kept on the grant (remote_info) are shown as "last known" and used in the message. A Property
Predator link Key Properties refused (pp_account_in_use or pp_account_mismatch, kept on the sourcer link grant) is shown
on the Key Properties card with what to put right there and a button to the grant, where Retry is.

Nothing here writes to the database: a page view never changes a grant or a person.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EntitlementGrant, Mentee, Person, Programme, Seller
from . import settings
from .commission import pounds
from .connectors import CONNECTORS, ConnectorError, product_mode
from .connectors.base import safe_url
from .entitlements import link_refusal_heading, link_refusal_note

EARNINGS_TIMEOUT = 4.0              # seconds each product has to answer; the four are asked at the same time
GRACE_SECONDS = 1.5                 # on top of the timeout before a product that has not answered is given up on
PRODUCT_ORDER = ("pp", "ordris", "kp", "hnh")
STATE_LABELS = {"ok": "live", "none": "not enrolled", "dry_run": "dry run", "not_configured": "not configured",
                "unreachable": "couldn't reach"}
TRACKED_LINK = "https://hellonewhome.co.uk/partners-programme?seller={code}"     # a seller's tracked link (also the sellers screens)
LONDON = ZoneInfo("Europe/London")


@dataclass
class Row:
    label: str
    value: str = ""
    url: str | None = None          # a link, shown with a Copy button


@dataclass
class ProductEarnings:
    product: str
    name: str
    state: str                      # ok | none | dry_run | not_configured | unreachable
    headline: str = ""
    detail: str = ""                # why it could not be reached
    rows: list[Row] = field(default_factory=list)
    links: dict = field(default_factory=dict)        # for the welcome message: pp_affiliate, ordris_partner, ...
    cached: bool = False            # the rows are what the recompute last kept, not a live answer
    action_url: str | None = None   # Hello New Home's admin page when the partner needs a region
    action_label: str = ""
    needs_region: bool = False
    warning: str = ""               # what the founder must put right in the product (a Key Properties link it refused)
    warning_title: str = ""         # its bold first words: not linked, or linked to a different account
    warning_url: str | None = None  # the grant page, where Retry is

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)


@dataclass
class SellerEarnings:
    seller_id: int | None = None
    code: str = ""
    link: str = ""
    active: bool = False
    self_billing_agreed_at: datetime | None = None


@dataclass
class EarningLinks:
    products: list[ProductEarnings]
    seller: SellerEarnings

    def product(self, code: str) -> ProductEarnings | None:
        return next((p for p in self.products if p.product == code), None)

    @property
    def links(self) -> dict:
        merged: dict = {}
        for p in self.products:
            merged.update({k: v for k, v in p.links.items() if v})
        return merged

    @property
    def missing(self) -> list[str]:
        """Products that could not be asked (or did not answer) and have no last known link: their links are not in
        the welcome message, which the page says beside the button."""
        return [p.name for p in self.products
                if p.state in ("unreachable", "dry_run", "not_configured") and not any(p.links.values())]


# ---- the four products ------------------------------------------------------------------------------------------
def _yes(value: bool | None) -> str:
    return "" if value is None else ("Yes" if value else "No")


def _count(value) -> str:
    return "" if value is None else f"{value:,}"


def _money(value) -> str:
    return "" if value is None else pounds(value)


def _rows(*pairs) -> list[Row]:
    """Rows for the panel, leaving out any the product did not answer."""
    out = []
    for label, value, *url in pairs:
        link = url[0] if url else None
        if link:
            out.append(Row(label, link, link))
        elif value not in (None, ""):
            out.append(Row(label, str(value)))
    return out


def _pp(info: dict, has_grant: bool) -> ProductEarnings:
    p = ProductEarnings("pp", settings.product_name("pp"), "ok")
    if not info.get("affiliate"):
        p.state = "none"
        p.headline = ("Not an affiliate yet: their programme enrols them when the recompute grants their access." if has_grant
                      else "Not an affiliate, and none of their programmes enrols them.")
        return p
    p.headline = "Affiliate"
    p.rows = _rows(("Affiliate link", None, info.get("link")), ("Code", info.get("code")),
                   ("Referred accounts", _count(info.get("referred_accounts"))),
                   ("Commission due", _money(info.get("commission_due_pence"))),
                   ("Commission paid", _money(info.get("commission_paid_pence"))))
    p.links = {"pp_affiliate": info.get("link")}
    return p


def _day(value: str | None) -> str:
    """A timestamp a product answered (ISO 8601) as the day in UK time, e.g. 24 Oct 2026; "" when it cannot be read."""
    try:
        when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(LONDON).strftime("%d %b %Y")


def _terms(info: dict) -> str:
    """The Ordris partner terms row: "" when Ordris did not say (contract 2 carries no terms), else accepted or not."""
    if "terms_accepted" not in info:
        return ""
    if not info["terms_accepted"]:
        return "Not accepted"
    day = _day(info.get("terms_accepted_at")) if info.get("terms_accepted_at") else ""
    version = info.get("terms_version")
    return "Accepted" + (f" {day}" if day else "") + (f" (version {version})" if version else "")


def _ordris(info: dict, has_grant: bool) -> ProductEarnings:
    p = ProductEarnings("ordris", settings.product_name("ordris"), "ok")
    if not info.get("partner"):
        p.state = "none"
        p.headline = ("Not a partner yet: their programme enrols them when the recompute grants their access." if has_grant
                      else "Not a partner, and none of their programmes enrols them.")
        return p
    terms = _terms(info)
    p.headline = "Partner programme" + (": terms not accepted" if terms == "Not accepted" else "")
    p.rows = _rows(("Referral link", None, info.get("link")), ("Code", info.get("referral_code")),
                   ("Referrals", _count(info.get("referrals"))), ("Pending", _money(info.get("pending_pence"))),
                   ("Payable", _money(info.get("payable_pence"))), ("Paid", _money(info.get("paid_pence"))),
                   ("Partner terms", terms))
    p.links = {"ordris_partner": info.get("link")}
    return p


def _kp(info: dict, has_grant: bool) -> ProductEarnings:
    p = ProductEarnings("kp", settings.product_name("kp"), "ok")
    if not info.get("sourcer"):
        p.state = "none"
        p.headline = ("No sourcer with this email yet: their programme links them when the recompute grants their access."
                      if has_grant else "No sourcer with this email.")
        return p
    p.headline = "Sourcer" + ("" if info.get("active", True) else " (switched off)")
    share = info.get("fee_share_pct")
    p.rows = _rows(("Sourcer login", None, info.get("login_url")), ("Share of the sourcing fee", f"{share}%" if share else ""),
                   ("Linked to Property Predator", _yes(info.get("pp_linked"))),
                   ("Deals submitted", _count(info.get("deals_submitted"))), ("Earned", _money(info.get("earned_pence"))),
                   ("Pipeline", _money(info.get("pipeline_pence"))))
    p.links = {"kp_login": info.get("login_url")}
    return p


def _hnh(info: dict, has_grant: bool) -> ProductEarnings:
    p = ProductEarnings("hnh", settings.product_name("hnh"), "ok")
    if not info.get("found"):
        p.state = "none"
        p.headline = ("No partner with this email yet: their programme seats them when the recompute grants their access."
                      if has_grant else "No partner with this email.")
        return p
    _hnh_rows(p, info)
    return p


def _hnh_rows(p: ProductEarnings, info: dict) -> None:
    area = info.get("area") if isinstance(info.get("area"), dict) else None
    tier = {"area": "Area Partner", "introducer": "Introducer"}.get(info.get("tier") or "", info.get("tier") or "")
    p.headline = "Partner" + (f": {tier}" if tier else "") + (f" ({info['status']})" if info.get("status") not in (None, "active") else "")
    p.needs_region = bool(info.get("needs_region"))
    if area:
        area_text = area.get("name") or area.get("slug") or ""
    elif p.needs_region:
        area_text = "Needs a region"
    else:
        area_text = "Not known" if not info.get("area_known") else "None"
    p.rows = _rows(("Partner portal", None, info.get("portal_url")), ("Referral link", None, info.get("referral_link")),
                   ("Code", info.get("referral_code")), ("Area", area_text))
    p.links = {"hnh_portal": info.get("portal_url"), "hnh_referral": info.get("referral_link")}
    admin = safe_url(info.get("admin_url"))
    if p.needs_region and admin:
        p.action_url, p.action_label = admin, "Assign a region in Hello New Home"


BUILDERS = {"pp": _pp, "ordris": _ordris, "kp": _kp, "hnh": _hnh}


def _ask(product: str, email: str, timeout: float) -> dict:
    """One product's live answer, in a worker thread: the connector's info for the email. Raises ConnectorError."""
    connector = CONNECTORS[product].from_config(dry_run=False)
    connector.timeout = timeout
    with connector:
        if product == "pp":
            try:
                return connector.affiliate_status(email).info
            except ConnectorError as error:
                if error.reason == "no_account":      # PP's own refusal; any other 404 (a route not deployed) is a failure
                    return {"no_account": True}
                raise
        if product == "ordris":
            return connector.partner_status(email).info
        if product == "kp":
            return connector.sourcer_status(email).info
        return connector.partner_status(email).info


# ---- what the recompute kept, for a product that cannot be asked ---------------------------------------------
def _grants_by_product(db: Session, person: Person) -> dict[tuple[str, str], EntitlementGrant]:
    grants = (db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id)
              .order_by(EntitlementGrant.id).all())
    return {(g.product, g.kind): g for g in grants}


EARNING_KINDS = {"pp": "affiliate", "ordris": "affiliate", "kp": "sourcer_link", "hnh": "partner_seat"}
# The person's ids in each product: a product that already knows the person by one of these may be asked about them.
LINKED_IDS = {"pp": ("pp_account_id",), "ordris": ("ordris_user_id",), "kp": ("kp_user_id",), "hnh": ("hnh_partner_id",)}


def _expected(db: Session, person: Person, grants: dict[tuple[str, str], EntitlementGrant]) -> set[tuple[str, str]]:
    """The (product, kind) rights the person holds or is due: their live grants, and every item of the programmes they
    are an active mentee on (a mentee enrolled today has no grant until the recompute runs)."""
    rights = {key for key, grant in grants.items() if grant.state in ("pending", "active", "failed")}
    templates = (db.query(Programme.grant_template).join(Mentee, Mentee.programme_id == Programme.id)
                 .filter(Mentee.person_id == person.id, Mentee.status == "active").all())
    for (template,) in templates:
        for item in template if isinstance(template, list) else []:
            if isinstance(item, dict):
                rights.add((str(item.get("product")), str(item.get("kind"))))
    return rights


def _askable(product: str, person: Person, grants: dict[tuple[str, str], EntitlementGrant],
             expected: set[tuple[str, str]]) -> bool:
    """May the panel send the person's email to this product? Only when they hold or are due its earning right
    (_expected), hold an affiliate grant of any state (the public programmes keep the member after access ends), or
    Group HQ already has their id in that product. Anyone else, a franchise prospect or an outside seller for example,
    is never looked up: Hello New Home is a separate controller, and no product is asked about a stranger."""
    key = (product, EARNING_KINDS[product])
    if key in expected:
        return True
    grant = grants.get(key)
    if grant is not None and grant.kind == "affiliate":
        return True
    return any(getattr(person, column, None) not in (None, "") for column in LINKED_IDS[product])


def _cached(product: str, grant: EntitlementGrant | None, p: ProductEarnings) -> None:
    """Fill the rows of an unreachable, dry-run or unconfigured product from the grant's remote_info."""
    info = dict(grant.remote_info or {}) if grant is not None else {}
    if product == "pp" and (info.get("link") or info.get("code")):
        p.rows = _rows(("Affiliate link", None, safe_url(info.get("link"))), ("Code", info.get("code")))
        p.links = {"pp_affiliate": safe_url(info.get("link"))}
    elif product == "ordris" and (info.get("link") or info.get("referral_code")):
        p.rows = _rows(("Referral link", None, safe_url(info.get("link"))), ("Code", info.get("referral_code")))
        p.links = {"ordris_partner": safe_url(info.get("link"))}
    elif product == "hnh" and info.get("found"):
        _hnh_rows(p, info)
        p.headline = ""
    else:
        return
    p.cached = True


def _link_warning(p: ProductEarnings, grant: EntitlementGrant | None, person: Person) -> None:
    """A Property Predator link Key Properties refused (contract 4: pp_account_in_use or pp_account_mismatch), from what
    the recompute kept on the sourcer link grant, so it shows whatever the live answer was and whether or not Key
    Properties could be asked just now: the plain message says what to put right there, and the grant has Retry."""
    note = link_refusal_note(grant, person)
    if note and grant is not None:
        p.warning, p.warning_title, p.warning_url = note, link_refusal_heading(note), f"/grants/{grant.id}"


def earning_links(db: Session, person: Person, *, timeout: float | None = None) -> EarningLinks:
    """Every product's panel for the person, asked at the same time (each within `timeout`, EARNINGS_TIMEOUT unless
    given), plus their Group HQ seller."""
    timeout = timeout or EARNINGS_TIMEOUT
    switches = settings.product_switches(db)
    grants = _grants_by_product(db, person)
    expected = _expected(db, person, grants)
    panels: dict[str, ProductEarnings] = {}
    asked = {}
    pool = ThreadPoolExecutor(max_workers=len(PRODUCT_ORDER), thread_name_prefix="earnings")
    try:
        for product in PRODUCT_ORDER:
            mode = product_mode(product, switches=switches)
            name = settings.product_name(product)
            if not _askable(product, person, grants, expected):
                panels[product] = ProductEarnings(product, name, "none", headline=(
                    f"Not asked: none of their programmes includes this and no {name} id is linked here."))
            elif mode["dry_run"]:
                panels[product] = ProductEarnings(product, name, "dry_run", headline="Dry run: not asked.")
            elif not mode["configured"]:
                panels[product] = ProductEarnings(product, name, "not_configured",
                                                  headline="Not configured here: no base URL or token.")
            else:
                asked[product] = pool.submit(_ask, product, person.person_key, timeout)
        deadline = monotonic() + timeout + GRACE_SECONDS     # a product's own timeout normally answers first
        for product, future in asked.items():
            name = settings.product_name(product)
            has_grant = (product, EARNING_KINDS[product]) in expected
            try:
                info = future.result(timeout=max(0.0, deadline - monotonic()))
            except FutureTimeout:
                panels[product] = ProductEarnings(product, name, "unreachable", detail=f"no answer within {timeout:g} seconds")
                continue
            except ConnectorError as error:
                panels[product] = ProductEarnings(product, name, "unreachable", detail=error.summary()[:200])
                continue
            except Exception as error:  # noqa: BLE001  (one product's surprise must not take the panel down)
                panels[product] = ProductEarnings(product, name, "unreachable", detail=f"unexpected: {type(error).__name__}")
                continue
            if product == "pp" and info.get("no_account"):
                panels[product] = ProductEarnings(product, name, "none", headline="No Property Predator account with this email.")
            else:
                panels[product] = BUILDERS[product](info, has_grant)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for product, panel in panels.items():
        if panel.state in ("dry_run", "not_configured", "unreachable"):
            _cached(product, grants.get((product, EARNING_KINDS[product])), panel)
    _link_warning(panels["kp"], grants.get(("kp", "sourcer_link")), person)
    return EarningLinks(products=[panels[p] for p in PRODUCT_ORDER], seller=seller_for(db, person))


def seller_for(db: Session, person: Person) -> SellerEarnings:
    seller = db.query(Seller).filter(Seller.person_id == person.id).first()
    if seller is None:
        return SellerEarnings()
    return SellerEarnings(seller_id=seller.id, code=seller.code, link=TRACKED_LINK.format(code=seller.code),
                          active=seller.active, self_billing_agreed_at=seller.self_billing_agreed_at)


# ---- the WhatsApp welcome ------------------------------------------------------------------------------------------
def first_name(person: Person) -> str:
    parts = (person.name or "").split()
    return parts[0] if parts else ""


def welcome_message(person: Person, programme_name: str, links: EarningLinks) -> str:
    """The plain message the mentee page copies for WhatsApp: a greeting and every link the mentee has, one per line,
    and a pointer to each scheme's own terms. No figures and no promises: what anyone earns is set by each scheme's
    published terms, which the message asks them to read."""
    have = links.links
    lines = [f"Hi {first_name(person) or 'there'},", "",
             f"Welcome to the {programme_name or 'mentorship'}. Here are your links in one place, so you have them to hand.", ""]
    entries = []
    if have.get("pp_affiliate"):
        entries.append(f"Property Predator affiliate link: {have['pp_affiliate']}")
    if have.get("ordris_partner"):
        entries.append(f"Ordris partner link: {have['ordris_partner']}")
    if have.get("kp_login"):
        entries.append(f"Key Properties: sign in to send your deals at {have['kp_login']}")
    if have.get("hnh_portal"):
        entries.append(f"Hello New Home partner portal: {have['hnh_portal']}")
    if have.get("hnh_referral"):
        entries.append(f"Hello New Home referral link: {have['hnh_referral']}")
    seller = links.seller
    if seller.code and seller.active:
        entries.append(f"Hello New Home franchise enquiries: {seller.link} (ask them to quote your code {seller.code})")
    lines += entries or ["Your links will be ready as soon as your access is set up; I'll send them over then."]
    lines += ["", "Each programme's terms are on its own website, so please have a read before you share a link. "
                  "Any questions, just reply here."]
    return "\n".join(lines)


# ---- the "Needs a region" queue ---------------------------------------------------------------------------------
def _needs_region_grants():
    """Active, unheld Hello New Home partner seats whose last lookup said the partner has no area, held by someone with
    an active mentee row and no Pause all access."""
    active_mentees = select(Mentee.person_id).where(Mentee.status == "active")
    return (select(EntitlementGrant, Person).join(Person, EntitlementGrant.person_id == Person.id)
            .where(EntitlementGrant.product == "hnh", EntitlementGrant.kind == "partner_seat",
                   EntitlementGrant.state == "active", EntitlementGrant.held_at.is_(None), Person.access_held_at.is_(None),
                   EntitlementGrant.remote_info["needs_region"].as_boolean().is_(True),
                   Person.id.in_(active_mentees)))


def needs_region_person_ids(db: Session):
    """The person ids in the queue, as a select for an IN (the /mentees filter)."""
    return _needs_region_grants().with_only_columns(EntitlementGrant.person_id)


def needs_region(db: Session, limit: int | None = None) -> list[dict]:
    """The queue, oldest check first: [{person, mentee, grant, admin_url, checked_at}], one per person."""
    rows = db.execute(_needs_region_grants().order_by(Person.person_key)).all()
    out, seen = [], set()
    for grant, person in rows:
        if person.id in seen:
            continue
        seen.add(person.id)
        mentee = (db.query(Mentee).filter(Mentee.person_id == person.id, Mentee.status == "active")
                  .order_by(Mentee.id).first())
        info = grant.remote_info or {}
        out.append({"person": person, "mentee": mentee, "grant": grant, "admin_url": safe_url(info.get("admin_url")),
                    "checked_at": info.get("checked_at") or "", "partner_id": info.get("partner_id")})
    return out[:limit] if limit else out


__all__ = ["EARNINGS_TIMEOUT", "EarningLinks", "ProductEarnings", "Row", "SellerEarnings", "TRACKED_LINK", "earning_links",
           "needs_region", "needs_region_person_ids", "seller_for", "welcome_message"]
