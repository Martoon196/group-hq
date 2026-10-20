"""The territory model as pure functions: band, achievable fee, units, partner earnings, HQ contribution,
break-even, upfront and monthly fee. Nothing here touches the database or the clock, so the loader, the
screens and the tests all compute the same figure from the same inputs.

Sources, quoted in each docstring: the plan (Phase 4, "Territory model" and "Pricing bands"), the Hello New
Home National-Local Operating Model (section 2 "Territory model" and section 4 "Economics per area at 10, 30
and 60 units") and the Offer Architecture (section 2C, the Local Partner programme). Every number in
DEFAULT_ASSUMPTIONS is a proposal: the assumptions table carries the same keys with confirmed = false and
the screens badge each one [CONFIRM] until the founder signs it off. Fees are the sheet confirmed on
19 October 2026 (12% capped at 150 a month); their use in this model is still a proposal.

Money is Decimal, quantised to pence; percentages are fractions (0.12, not 12). Inputs may be int, float,
str or Decimal and are converted through str(), so a float read from JSON never leaks binary noise into a
quote. The worked check the tests pin (plan, Phase 4): "Andover at a 144 fee and 30 units gives a partner
share of about 2,160 a month, so about 13,000 upfront, inside the town band".
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BANDS = ("london", "major_city", "town", "rural")

# Plan, Phase 4 and operating model section 2.4: "London | Every London borough for now: postcode areas E, EC,
# N, NW, SE, SW, W, WC, BR, CR, HA, IG, RM, SM, UB, plus the London districts of DA (DA5 to DA8, DA14 to DA18),
# EN (EN1 to EN5), KT (KT1 to KT6, KT9) and TW (TW1 to TW14)".
LONDON_AREAS = frozenset({"E", "EC", "N", "NW", "SE", "SW", "W", "WC", "BR", "CR", "HA", "IG", "RM", "SM", "UB"})
LONDON_DISTRICTS = frozenset(
    {f"DA{n}" for n in (5, 6, 7, 8, 14, 15, 16, 17, 18)}
    | {f"EN{n}" for n in range(1, 6)}
    | {f"KT{n}" for n in (1, 2, 3, 4, 5, 6, 9)}
    | {f"TW{n}" for n in range(1, 15)}
)

# The Core Cities group (Belfast, Birmingham, Bristol, Cardiff, Glasgow, Leeds, Liverpool, Manchester, Newcastle,
# Nottingham, Sheffield), matched on the ONS local authority code first and the name second. The plan's rule:
# "band london|major_city|town|rural (London postcode areas; core-cities list; prs_households >= 3,000 is town;
# else rural)".
CORE_CITY_GSS = frozenset({
    "N09000003",    # Belfast
    "E08000025",    # Birmingham
    "E06000023",    # Bristol, City of
    "W06000015",    # Cardiff
    "S12000049",    # Glasgow City
    "E08000035",    # Leeds
    "E08000012",    # Liverpool
    "E08000003",    # Manchester
    "E08000021",    # Newcastle upon Tyne
    "E06000018",    # Nottingham
    "E08000019",    # Sheffield
})
CORE_CITY_NAMES = frozenset({
    "belfast", "birmingham", "bristol", "cardiff", "glasgow", "leeds", "liverpool", "manchester",
    "newcastle upon tyne", "nottingham", "sheffield",
})

# Mirrored by alembic/versions/0003_franchise.py (ASSUMPTIONS), which seeds the assumptions table from the same
# list with confirmed = false. tests/test_territory_rules.py checks the two agree.
DEFAULT_ASSUMPTIONS: list[tuple[str, Any, str]] = [
    ("capture_y1", 0.003, "Share of a district's private-rented households under management by the end of year one. "
                          "Plan, Phase 4: units_y1..y3 = prs_households x capture with capture 0.3% / 0.7% / 1.2% [CONFIRM]."),
    ("capture_y2", 0.007, "Share under management by the end of year two. Plan, Phase 4 [CONFIRM]."),
    ("capture_y3", 0.012, "Share under management by the end of year three. Plan, Phase 4 [CONFIRM]."),
    ("fee_cap", 150, "Full Management fee cap, pounds a month. The fee sheet confirmed on 19 October 2026 (12% capped at "
                     "150 a month) is a fact; using it as the achievable fee per managed unit in this model is the "
                     "proposal [CONFIRM]."),
    ("management_pct", 0.12, "Full Management fee as a share of rent. Fee sheet confirmed on 19 October 2026; its use as "
                             "the modelled fee per unit is the proposal [CONFIRM]."),
    ("cost_to_serve", 25, "HQ cost to serve per unit per month outside London. Operating model section 4.1: staff time for "
                          "rent, maintenance desk, statements, compliance. Replace with Swansea actuals [CONFIRM]."),
    ("cost_to_serve_london", 35, "HQ cost to serve per unit per month in London. Operating model section 4.1: higher for "
                                 "trades prices and tenant expectations [CONFIRM]."),
    ("portals", 550, "Area-attributable portal cost per month. Operating model section 4.1 [VERIFY: Rightmove's average "
                     "branch spend was 1,636 a month in its H1 2026 report; virtual-branch pricing is not published] [CONFIRM]."),
    ("ads", 760, "Ads per month at launch outside London (25 a day), rising to 1,000 at 60 units. Operating model "
                 "section 4.1. Year one uses the launch figure throughout; the rise at 60 units is not modelled [CONFIRM]."),
    ("ads_london", 1520, "Ads per month at launch in London (50 a day), rising to 2,000. Operating model section 4.1. Year one "
                         "uses the launch figure throughout; the rise at 60 units is not modelled [CONFIRM]."),
    ("letters", 300, "Ongoing letters and Land Registry lookups per month (100 letters, 7 a title). Operating model "
                     "section 4.1 [CONFIRM]."),
    ("systems_fee", 99, "Partner systems and brand fee per month, received by HQ. Offer architecture section 2C and "
                        "operating model section 4.1. The banded monthly fee replaces it for paying franchisees once the "
                        "pricing bands are confirmed [CONFIRM]."),
    ("systems_fee_from_month", 4, "The first month the systems fee is charged. Offer architecture section 2C: from month 4. The "
                                  "heads of terms (section 6) count from the fourth full calendar month after the effective date, "
                                  "which is month 5 for a mid-month start; the founder to pick one [CONFIRM]."),
    ("partner_split_own", 0.5, "Partner share of the net management fee on their own instructions. Operating model "
                               "section 4.1 and prospectus section 3 [CONFIRM]."),
    ("partner_split_hq_leads", 0.25, "Partner share on HQ leads in their territory that they service. Operating model "
                                     "section 4.1 and prospectus section 3 [CONFIRM]."),
    ("partner_own_lead_share", 0.8, "Share of a partner's units that are their own instructions; the rest are HQ leads at "
                                    "the lower split. Operating model section 4.1: assumed 80% of units, a 45% blend [CONFIRM]."),
    ("town_min_prs_households", 3000, "A district with at least this many private-rented households is a town; below it is "
                                      "rural, unless it is London or a core city. Plan, Phase 4 [CONFIRM]."),
    ("upfront_units", 30, "The portfolio size the upfront fee is priced at. Plan, Phase 4: 6 x modelled partner net monthly "
                          "earnings at 30 units [CONFIRM]."),
    ("upfront_multiplier", 6, "Months of modelled partner earnings the upfront fee represents. Plan, Phase 4 [CONFIRM]."),
    ("upfront_round_to", 500, "The upfront fee is rounded to the nearest 500. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_rural", 3000, "Minimum upfront fee, rural band. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_town", 6000, "Minimum upfront fee, town band. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_major_city", 10000, "Minimum upfront fee, major city band. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_london", 15000, "Minimum upfront fee, London band. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_rural", 49, "Monthly fee, rural band, replacing the flat 99 systems fee for paying franchisees. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_town", 79, "Monthly fee, town band. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_major_city", 99, "Monthly fee, major city band. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_london", 149, "Monthly fee, London band. Plan, Phase 4 [CONFIRM]."),
]
DEFAULT_VALUES: dict[str, Any] = {key: value for key, value, _note in DEFAULT_ASSUMPTIONS}

# Mirrored by alembic/versions/0003_franchise.py (COMMISSION_RULES_V1). (seller_kind, upfront_pct, trail_pct,
# trail_months). Plan, Phase 4: "proposal: mentees and franchisees 20% of the upfront fee plus 10% of months 1 to
# 12 of the monthly fee; outsiders 10% of the upfront only [CONFIRM]".
DEFAULT_COMMISSION_RULES: list[tuple[str, str, str, int]] = [
    ("mentee", "0.2000", "0.1000", 12),
    ("franchisee", "0.2000", "0.1000", 12),
    ("outsider", "0.1000", "0.0000", 0),
]
COMMISSION_RULES_VERSION = 1

PENCE = Decimal("0.01")
FOUR_DP = Decimal("0.0001")
MONTHS_IN_YEAR = 12


# ---- numbers -------------------------------------------------------------------------------------
def money(value: Any) -> Decimal:
    """Any number as pence-precision Decimal, through str() so a float from JSON stays what it printed as."""
    if isinstance(value, Decimal):
        return value.quantize(PENCE, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(PENCE, rounding=ROUND_HALF_UP)


def fraction(value: Any) -> Decimal:
    """A percentage as a fraction, unquantised (0.003 must survive)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_to(value: Any, step: Any) -> Decimal:
    """Nearest multiple of step, halves up: 12,960 to the nearest 500 is 13,000; 12,250 is 12,500."""
    step_d = Decimal(str(step))
    if step_d <= 0:
        return money(value)
    return (Decimal(str(value)) / step_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step_d


def assumption_values(assumptions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The defaults overlaid with whatever the caller supplies (a {key: value} dict, for example the assumptions
    table read through load_assumptions). Unknown keys pass through untouched."""
    values = dict(DEFAULT_VALUES)
    if assumptions:
        values.update({k: v for k, v in assumptions.items() if v is not None})
    return values


# ---- the band --------------------------------------------------------------------------------------
def postcode_area(district: str) -> str:
    """The letters of a district: SA1 is SA, WC1A is WC."""
    code = (district or "").strip().upper()
    letters = ""
    for ch in code:
        if ch.isalpha():
            letters += ch
        else:
            break
    return letters


def is_london(district: str) -> bool:
    """Operating model section 2.4: London is "postcode areas E, EC, N, NW, SE, SW, W, WC, BR, CR, HA, IG, RM, SM,
    UB, plus the London districts of DA (DA5 to DA8, DA14 to DA18), EN (EN1 to EN5), KT (KT1 to KT6, KT9) and TW
    (TW1 to TW14)". DA1, EN6, KT7 and TW15 are not London."""
    code = (district or "").strip().upper()
    return postcode_area(code) in LONDON_AREAS or code in LONDON_DISTRICTS


def is_core_city(la_name: str | None, la_gss: str | None = None) -> bool:
    """One of the Core Cities, by ONS code when it is known and by name otherwise ("Bristol, City of" and "Glasgow
    City" count; "Newcastle-under-Lyme" does not)."""
    if la_gss and la_gss.strip().upper() in CORE_CITY_GSS:
        return True
    name = (la_name or "").strip().lower()
    if not name:
        return False
    if name in CORE_CITY_NAMES:
        return True
    for suffix in (", city of", " city"):
        if name.endswith(suffix) and name[: -len(suffix)].strip() in CORE_CITY_NAMES:
            return True
    return False


def band_for(district: str, la_name: str | None, prs_households: int | None, *, la_gss: str | None = None,
             town_min: Any = None) -> str:
    """Plan, Phase 4: "band london|major_city|town|rural (London postcode areas; core-cities list; prs_households
    >= 3,000 is town; else rural)". The band decides the upfront floor, the monthly fee, the cost to serve and the
    ads budget. A district with no PRS count is rural until the census file says otherwise. Swansea, Durham, Bath
    and Andover all fall in town or rural under this rule [CONFIRM]."""
    if is_london(district):
        return "london"
    if is_core_city(la_name, la_gss):
        return "major_city"
    threshold = int(town_min if town_min is not None else DEFAULT_VALUES["town_min_prs_households"])
    if prs_households is not None and int(prs_households) >= threshold:
        return "town"
    return "rural"


def cost_to_serve_for(band: str, assumptions: Mapping[str, Any] | None = None) -> Decimal:
    """Operating model section 4.1: "HQ cost to serve per unit per month | 25 | 25 | 35 | Staff time for rent,
    maintenance desk, statements, compliance. London higher for trades prices and tenant expectations. Replace
    with actuals." [CONFIRM]"""
    a = assumption_values(assumptions)
    return money(a["cost_to_serve_london"] if band == "london" else a["cost_to_serve"])


def fixed_costs_for(band: str, assumptions: Mapping[str, Any] | None = None) -> Decimal:
    """Operating model section 4.1: "Area-attributable fixed costs per month | Portals 550; ads 760 (25 a day) at
    launch rising to 1,000 at 60 units; ongoing letters and lookups 300 (100 letters, Land Registry 7 a title)
    | Same | Ads 1,520 (50 a day) rising to 2,000; otherwise same". The launch-phase figure, before the systems
    fee, which hq_contribution_month adds back from month 4. Year one uses the launch ads budget throughout: the
    rise at 60 units is not modelled, so a district that reaches 60 units inside year one understates ads by up
    to 240 (480 in London) a month in its later months. These are costs per AREA: model_district() apportions
    them across a territory's districts (fixed_share) and never charges a whole area to one district unless the
    district is the whole territory. [CONFIRM]"""
    a = assumption_values(assumptions)
    ads = a["ads_london"] if band == "london" else a["ads"]
    return money(a["portals"]) + money(ads) + money(a["letters"])


# ---- fee and units -----------------------------------------------------------------------------------
def achievable_fee(rent_2bed: Any, cap: Any, pct: Any) -> Decimal | None:
    """Plan, Phase 4: "achievable_fee = min(150, 0.12 x rent_2bed)". Operating model section 4.1: "Full
    Management fee per single let (12%, cap 150) | 84 | 144 | 150 (capped) | At London rents the cap bites on
    almost every property." A two-bed rent is the typical managed single let; with no rent on file there is no
    fee and the district is unpriced. 1,200 gives 144.00; 2,332 gives 150.00; 700 gives 84.00."""
    if rent_2bed is None:
        return None
    fee = money(fraction(pct) * Decimal(str(rent_2bed)))
    return min(fee, money(cap))


def units(prs: int | None, capture: Any) -> int:
    """Plan, Phase 4: "units_y1..y3 = prs_households x capture with capture 0.3% / 0.7% / 1.2% [CONFIRM]". The
    count of managed units at the end of the year, rounded half up (3,500 households at 0.3% is 10.5, so 11).
    No PRS count means no units."""
    if prs is None:
        return 0
    return int((Decimal(int(prs)) * fraction(capture)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def units_in_month(units_at_year_end: int, month: int) -> int:
    """A linear ramp inside year one: units_at_year_end x month / 12, rounded half up, so year one earns about
    half of a full year at the year-end count. The operating model's 10 / 30 / 60 tables are steady-state months;
    the ramp is this module's own, so that partner_net_y1 and hq_contribution_y1 are whole-year totals [CONFIRM]."""
    if month < 1:
        return 0
    if month >= MONTHS_IN_YEAR:
        return int(units_at_year_end)
    return int((Decimal(int(units_at_year_end)) * month / MONTHS_IN_YEAR).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---- the split and the monthly figures -------------------------------------------------------------------
def blended_split(own: Any, hq_leads: Any, own_share: Any) -> Decimal:
    """Operating model section 4.1: "Partner share | 45% blended | 45% | 45% | Area Partner: 50% on own
    instructions (assumed 80% of units), 25% on HQ leads serviced (20%). [CONFIRM]". 0.8 x 0.50 + 0.2 x 0.25 =
    0.45."""
    own_f, share = fraction(own_share), fraction(own)
    return own_f * share + (Decimal(1) - own_f) * fraction(hq_leads)


def systems_fee_in_month(systems_fee: Any, month: int, from_month: int = 4) -> Decimal:
    """Offer architecture section 2C: "0 upfront for the founding cohort; 99/month systems & brand fee from month 4".
    Operating model section 4.1: "partner systems fee received from month 4 +99"."""
    return money(systems_fee) if month >= from_month else money(0)


def partner_net_month(units: int, fee: Any, split: Any, systems_fee: Any, month: int, *, from_month: int = 4) -> Decimal:
    """What the partner takes home in one month: units x fee x split, less the systems fee once it applies.
    Operating model section 4.2: "A Durham Area Partner at 30 units earns about 2,044 gross before the 99 fee
    and running costs"; section 4.1: "Partner share 45% blended" and "partner systems fee received from month 4
    +99". With the own-instruction split of 50% the plan's Andover check reads 30 x 144 x 0.5 = 2,160 (month 1 to
    3) and 2,061 from month 4. Running costs (mileage, phone, print) are the partner's own and are not modelled."""
    gross = money(Decimal(int(units)) * money(fee) * fraction(split))
    return gross - systems_fee_in_month(systems_fee, month, from_month)


def hq_contribution_month(units: int, fee: Any, split: Any, cost_to_serve: Any, fixed_costs: Any, systems_fee: Any,
                          month: int, *, from_month: int = 4) -> Decimal:
    """Operating model section 4.2 to 4.4, one column of the tables: HQ gross recurring fee (units x fee), less
    the partner commission (split), less "Portals, ads, letters, less systems fee" (fixed_costs, with the systems
    fee added back from month 4), less "Cost to serve" (cost_to_serve x units), gives "HQ contribution per
    month". Durham at 30 units: 4,542 x 55% = 2,498, less 1,511, less 750, is 237."""
    gross = money(Decimal(int(units)) * money(fee))
    retained = money(gross * (Decimal(1) - fraction(split)))
    serve = money(Decimal(int(units)) * money(cost_to_serve))
    return retained - serve - money(fixed_costs) + systems_fee_in_month(systems_fee, month, from_month)


def break_even_units(fee: Any, split: Any, cost_to_serve: Any, fixed_costs: Any, systems_fee: Any) -> Decimal | None:
    """The units at which hq_contribution_month reaches zero in a steady-state month (systems fee received):
    (fixed_costs - systems_fee) / (fee x (1 - split) - cost_to_serve). Operating model: "Break-even at roughly 26
    units" (Durham, section 4.2), "roughly 17 units" (Andover, 4.3), "roughly 20 units" (London, 4.4), each on
    that area's blended per-unit fee. None when a unit does not cover its own cost to serve."""
    margin = money(fee) * (Decimal(1) - fraction(split)) - money(cost_to_serve)
    if margin <= 0:
        return None
    return ((money(fixed_costs) - money(systems_fee)) / margin).quantize(PENCE, rounding=ROUND_HALF_UP)


def unit_mix_fee(single_fee: Any, hmo_fee: Any, sa_fee: Any, singles: int, hmos: int, sas: int) -> Decimal:
    """Operating model section 4.1: "Unit mix at 10 / 30 / 60 | 8 single + 2 SA / 22 single + 2 HMO + 6 SA / 40
    single + 4 HMO + 16 SA" and "HMO gross room rent per house (5 rooms) ... 15% fee = 375, 412.50, 675", "SA net
    booking revenue ... Full SA at 18% = 324 and 360 ... London 585". The gross monthly fee for a mix; the
    per-district model prices single lets only, so this is here to reproduce the section 4 tables."""
    return (money(single_fee) * singles + money(hmo_fee) * hmos + money(sa_fee) * sas).quantize(PENCE)


# ---- year one ------------------------------------------------------------------------------------------
def partner_net_year1(units_y1: int, fee: Any, split: Any, systems_fee: Any, *, from_month: int = 4) -> Decimal:
    """Twelve months of partner_net_month on the linear ramp to units_y1, the blended split, the systems fee from
    month 4. What a partner could take home in year one in this district on the plan's capture [CONFIRM]."""
    total = money(0)
    for month in range(1, MONTHS_IN_YEAR + 1):
        total += partner_net_month(units_in_month(units_y1, month), fee, split, systems_fee, month, from_month=from_month)
    return total


def hq_contribution_year1(units_y1: int, fee: Any, split: Any, cost_to_serve: Any, fixed_costs: Any, systems_fee: Any,
                          *, from_month: int = 4) -> Decimal:
    """Twelve months of hq_contribution_month on the same ramp. Operating model section 4.5: "At 2 to 4 new
    instructions a month these [one-off fees] roughly cover the launch-phase monthly loss in each area, which is
    why the first 90 days are planned as a proving period rather than a profit period." One-off income (setup,
    Tenant Find, SA Launch) is not in this figure; it is a recurring-fee contribution and is negative for almost
    every district in year one [CONFIRM]."""
    total = money(0)
    for month in range(1, MONTHS_IN_YEAR + 1):
        total += hq_contribution_month(units_in_month(units_y1, month), fee, split, cost_to_serve, fixed_costs,
                                       systems_fee, month, from_month=from_month)
    return total


# ---- pricing ----------------------------------------------------------------------------------------------
def upfront_floor(band: str, assumptions: Mapping[str, Any] | None = None) -> Decimal:
    """Plan, Phase 4: "floors 3,000 rural / 6,000 town / 10,000 major city / 15,000 London" [CONFIRM]."""
    a = assumption_values(assumptions)
    return money(a[f"upfront_floor_{band}"])


def upfront_fee(partner_net_at_30_units: Any, multiplier: Any, floor_by_band: Mapping[str, Any] | Any, *,
                band: str | None = None, round_to_step: Any = 500) -> Decimal:
    """Plan, Phase 4, "Pricing bands, all [CONFIRM] before use: upfront fee = 6 x modelled partner net monthly
    earnings at 30 units, rounded to 500, with floors 3,000 rural / 6,000 town / 10,000 major city / 15,000
    London ... Worked check: Andover at a 144 fee and 30 units gives a partner share of about 2,160 a month, so
    about 13,000 upfront, inside the town band and the 10 to 20k that OFFER-ARCHITECTURE.md section 3 shelved."
    The basis is the partner's share at 30 units before the systems fee, as the plan's own arithmetic has it
    (6 x 2,160 = 12,960, so 13,000); taking the 99 off first would give 12,500 [CONFIRM]. floor_by_band is the
    floor itself, or the {band: floor} mapping with band= to pick from. Mentees pay nothing upfront."""
    if isinstance(floor_by_band, Mapping):
        if band is None:
            raise ValueError("band is needed to pick a floor from floor_by_band")
        floor = money(floor_by_band[band])
    else:
        floor = money(floor_by_band)
    priced = round_to(money(partner_net_at_30_units) * fraction(multiplier), round_to_step)
    return max(money(priced), floor)


def monthly_fee(band: str, assumptions: Mapping[str, Any] | None = None) -> Decimal:
    """Plan, Phase 4: "monthly fee replaces the flat 99 with 49 / 79 / 99 / 149 by band. Mentees: upfront
    waived, monthly waived while the mentorship is active" [CONFIRM]."""
    a = assumption_values(assumptions)
    return money(a[f"monthly_fee_{band}"])


def upfront_floors(assumptions: Mapping[str, Any] | None = None) -> dict[str, Decimal]:
    return {band: upfront_floor(band, assumptions) for band in BANDS}


def monthly_fees(assumptions: Mapping[str, Any] | None = None) -> dict[str, Decimal]:
    return {band: monthly_fee(band, assumptions) for band in BANDS}


# ---- a territory's fixed costs, shared between its districts ---------------------------------------------------
FIXED_COST_BASIS = "territory units_y1"   # recorded in model_runs.assumptions by the loader


def territory_shares(members: Mapping[str, Mapping[str, Any]], territories: list[Mapping[str, Any]]) -> dict[str, Decimal]:
    """{district: the share of its territory's fixed costs and systems fee it carries}. members is {district:
    {"area": .., "units_y1": ..}} for every district loaded; territories is the parsed
    data/current_territories.json (each entry's "districts" lists codes, or a bare area such as "E" meaning
    every loaded district of that area). A district is grouped with the first territory that lists it, and a
    district no territory lists with the other unlisted districts of its postcode area. The share is the
    district's units_y1 over the territory's; with no units anywhere in the territory, 1/n. Operating model
    section 4.1 gives portals, ads and letters per AREA and the systems fee per PARTNER, so summing the
    per-district figures of a territory must count each once, which these weights guarantee."""
    groups: dict[str, list[str]] = {}
    placed: set[str] = set()
    for i, entry in enumerate(territories):
        codes: list[str] = []
        for raw in entry.get("districts", []):
            code = str(raw).strip().upper()
            if code.isalpha():
                codes.extend(sorted(d for d in members if postcode_area(d) == code and d not in placed))
            elif code in members and code not in placed:
                codes.append(code)
        unique = [c for c in dict.fromkeys(codes) if c not in placed]
        if unique:
            groups[f"territory:{entry.get('area') or entry.get('name') or i}"] = unique
            placed.update(unique)
    for district in sorted(members):
        if district not in placed:
            groups.setdefault(f"area:{postcode_area(district)}", []).append(district)
    shares: dict[str, Decimal] = {}
    for codes in groups.values():
        total = sum(int(members[c].get("units_y1") or 0) for c in codes)
        for c in codes:
            if total > 0:
                shares[c] = (Decimal(int(members[c].get("units_y1") or 0)) / Decimal(total)).quantize(FOUR_DP, rounding=ROUND_HALF_UP)
            else:
                shares[c] = (Decimal(1) / Decimal(len(codes))).quantize(FOUR_DP, rounding=ROUND_HALF_UP)
        # four decimal places leave a residual of a few ten-thousandths; the largest district takes it, so a
        # territory's shares sum to exactly 1 and its districts' figures add up to the territory's
        residual = Decimal(1) - sum(shares[c] for c in codes)
        if residual:
            largest = max(codes, key=lambda c: (int(members[c].get("units_y1") or 0), shares[c]))
            shares[largest] += residual
    return shares


# ---- one district end to end -------------------------------------------------------------------------------
def model_district(*, district: str, la_name: str | None, prs_households: int | None, rent_2bed: Any,
                   la_gss: str | None = None, assumptions: Mapping[str, Any] | None = None,
                   fixed_share: Any = 1, systems_fee_share: Any = 1) -> dict[str, Any]:
    """Every modelled column of a postcode_districts row from its public inputs, plus the band's upfront and
    monthly fee, so the loader, the district screen and the territory report all agree. None for the money
    columns when there is no rent on file.

    fixed_share and systems_fee_share are the fractions of the territory's fixed costs (portals, ads,
    letters) and of the one systems fee that this district's partner_net_y1 and hq_contribution_y1 carry
    (territory_shares(); the loader passes the same weight to both). The default of 1 is "this district is
    the whole territory". break_even_units and the upfront fee stay on the whole-area figures, because they
    price a territory, not a district."""
    a = assumption_values(assumptions)
    band = band_for(district, la_name, prs_households, la_gss=la_gss, town_min=a["town_min_prs_households"])
    fee = achievable_fee(rent_2bed, a["fee_cap"], a["management_pct"])
    cost = cost_to_serve_for(band, a)
    fixed_full = fixed_costs_for(band, a)
    fixed = money(fixed_full * fraction(fixed_share))
    fee_share = money(Decimal(str(a["systems_fee"])) * fraction(systems_fee_share))
    split = blended_split(a["partner_split_own"], a["partner_split_hq_leads"], a["partner_own_lead_share"])
    from_month = int(a["systems_fee_from_month"])
    u1, u2, u3 = (units(prs_households, a[key]) for key in ("capture_y1", "capture_y2", "capture_y3"))
    out: dict[str, Any] = {
        "district": (district or "").strip().upper(), "area": postcode_area(district), "band": band,
        "achievable_fee": fee, "cost_to_serve": cost, "units_y1": u1, "units_y2": u2, "units_y3": u3,
        "partner_net_y1": None, "hq_contribution_y1": None, "break_even_units": None,
        "upfront_fee": None, "monthly_fee": monthly_fee(band, a), "blended_split": split,
        "cost_share": fraction(fixed_share), "fixed_costs": fixed, "systems_fee": fee_share,
    }
    if fee is None:
        return out
    out["partner_net_y1"] = partner_net_year1(u1, fee, split, fee_share, from_month=from_month)
    out["hq_contribution_y1"] = hq_contribution_year1(u1, fee, split, cost, fixed, fee_share, from_month=from_month)
    out["break_even_units"] = break_even_units(fee, split, cost, fixed_full, a["systems_fee"])
    at_30 = partner_net_month(int(a["upfront_units"]), fee, a["partner_split_own"], 0, 1)
    out["upfront_fee"] = upfront_fee(at_30, a["upfront_multiplier"], upfront_floor(band, a), round_to_step=a["upfront_round_to"])
    return out


# ---- the database copies (lazy imports: the functions above need no DATABASE_URL) ---------------------------
def load_assumptions(db) -> dict[str, Any]:
    """The assumptions table as {key: value}, over the defaults, for the pure functions."""
    from ..models import Assumption
    rows = {row.key: row.value for row in db.query(Assumption).all()}
    return assumption_values(rows)


def unconfirmed_assumptions(db) -> list[str]:
    """Keys still carrying the [CONFIRM] badge."""
    from ..models import Assumption
    return [row.key for row in db.query(Assumption).filter(Assumption.confirmed.is_(False)).order_by(Assumption.key).all()]


def ensure_assumptions(db) -> int:
    """Add any default assumption that is missing, confirmed = false; never overwrite one (a value edited or
    confirmed in the app stands). Returns how many were added. The migration seeds the same rows; this is for a
    database that predates a new key. Caller commits."""
    from ..models import Assumption
    existing = {row.key for row in db.query(Assumption.key).all()}
    added = 0
    for key, value, note in DEFAULT_ASSUMPTIONS:
        if key in existing:
            continue
        db.add(Assumption(key=key, value=value, note=note, confirmed=False))
        added += 1
    return added


def ensure_commission_rules(db, active_from=None) -> int:
    """Seed commission_rules version 1 when no version 1 rule exists for the kind, confirmed = false and
    "confirm=false" in the note. Never touches an existing row. Caller commits."""
    from datetime import date

    from ..models import CommissionRule
    existing = {row.seller_kind for row in db.query(CommissionRule).filter(CommissionRule.version == COMMISSION_RULES_VERSION).all()}
    note = ("confirm=false. Proposal from the plan (Phase 4, seller commission ledger), not approved by the founder: "
            "no pack, link or conversation may quote it until it is confirmed.")
    added = 0
    for kind, upfront, trail, months in DEFAULT_COMMISSION_RULES:
        if kind in existing:
            continue
        db.add(CommissionRule(version=COMMISSION_RULES_VERSION, seller_kind=kind, upfront_pct=Decimal(upfront),
                              trail_pct=Decimal(trail), trail_months=months, active_from=active_from or date(2026, 10, 20),
                              note=note, confirmed=False))
        added += 1
    return added
