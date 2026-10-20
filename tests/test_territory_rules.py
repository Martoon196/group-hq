"""The territory model's pure functions (app.services.territory_rules) against the figures they are built from:
the plan's worked Andover check (fee 144, 30 units gives about 2,160 a month partner share, upfront about
13,000), the operating model's section 4 tables and break-even figures (Durham 26, Andover 17, London 20
units), the band rule, and the Phase 4 schema (migration 0003): seeds that match the module's defaults, the
check constraints and the one-live-entry index on the commission ledger."""
from __future__ import annotations

import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app import config
from app.models import (Assumption, CommissionEntry, CommissionRule, FranchiseOpportunity, Person, PostcodeDistrict,
                        Seller)
from app.services import territory_rules as tr

TOLERANCE = Decimal("1.50")        # the operating model's tables are rounded to the pound


def close(actual, expected, tolerance=TOLERANCE) -> bool:
    return abs(Decimal(str(actual)) - Decimal(str(expected))) <= tolerance


# ---- the plan's worked Andover check ------------------------------------------------------------------------
def test_andover_worked_check_from_the_plan():
    fee = tr.achievable_fee(1200, cap=150, pct=0.12)
    assert fee == Decimal("144.00")
    share = tr.partner_net_month(30, fee, split=0.5, systems_fee=99, month=1)
    assert share == Decimal("2160.00"), "30 units x 144 x 50% before the systems fee"
    assert tr.partner_net_month(30, fee, 0.5, 99, month=3) == Decimal("2160.00")
    assert tr.partner_net_month(30, fee, 0.5, 99, month=4) == Decimal("2061.00"), "the 99 systems fee from month 4"
    assert tr.upfront_fee(share, multiplier=6, floor_by_band=6000) == Decimal("13000.00"), "6 x 2,160 = 12,960 to the nearest 500"
    assert tr.upfront_fee(share, 6, tr.upfront_floors(), band="town") == Decimal("13000.00")
    assert tr.band_for("SP10", "Test Valley", 3500) == "town"
    assert tr.monthly_fee("town") == Decimal("79.00")


def test_achievable_fee_is_twelve_percent_capped_at_150():
    assert tr.achievable_fee(700, 150, 0.12) == Decimal("84.00")        # Durham: the cap never bites
    assert tr.achievable_fee(2332, 150, 0.12) == Decimal("150.00")      # London: the cap bites on almost every property
    assert tr.achievable_fee(1250, 150, 0.12) == Decimal("150.00")      # the rent where the cap starts
    assert tr.achievable_fee("1135.00", Decimal("150"), Decimal("0.12")) == Decimal("136.20")
    assert tr.achievable_fee(None, 150, 0.12) is None


def test_units_follow_capture_rounded_half_up():
    assert tr.units(3500, 0.003) == 11          # 10.5 rounds up
    assert tr.units(3500, 0.007) == 25          # 24.5 rounds up
    assert tr.units(3500, 0.012) == 42
    assert tr.units(100, 0.003) == 0
    assert tr.units(None, 0.003) == 0


# ---- the band rule ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("district, la_name, prs, gss, expected", [
    ("SE1", "Southwark", 20000, None, "london"),
    ("EC1A", "Islington", 500, None, "london"),
    ("WC1A", "Camden", 500, None, "london"),
    ("BR1", "Bromley", 500, None, "london"),
    ("DA5", "Bexley", 500, None, "london"),
    ("DA18", "Bexley", 500, None, "london"),
    ("DA1", "Dartford", 5000, None, "town"),            # Dartford is not a London district
    ("EN5", "Barnet", 500, None, "london"),
    ("EN6", "Hertsmere", 500, None, "rural"),
    ("KT9", "Kingston upon Thames", 500, None, "london"),
    ("KT7", "Elmbridge", 500, None, "rural"),
    ("TW14", "Hounslow", 500, None, "london"),
    ("TW15", "Spelthorne", 500, None, "rural"),
    ("BS1", "Bristol, City of", 20000, None, "major_city"),
    ("BS1", "", 20000, "E06000023", "major_city"),      # by ONS code when the name is missing
    ("G1", "Glasgow City", 20000, None, "major_city"),
    ("NE1", "Newcastle upon Tyne", 20000, None, "major_city"),
    ("ST5", "Newcastle-under-Lyme", 20000, None, "town"),
    ("CF10", "Cardiff", 1000, None, "major_city"),       # a core city whatever its PRS count
    ("SA1", "Swansea", 8000, "W06000011", "town"),       # not a core city under the rule [CONFIRM]
    ("BA1", "Bath and North East Somerset", 6000, None, "town"),
    ("DH1", "County Durham", 3000, None, "town"),        # exactly the threshold is town
    ("DH7", "County Durham", 2999, None, "rural"),
    ("SP9", "Wiltshire", None, None, "rural"),            # no census figure yet
    ("ta1", "Somerset", 4000, None, "town"),              # case does not matter
])
def test_band_rule(district, la_name, prs, gss, expected):
    assert tr.band_for(district, la_name, prs, la_gss=gss) == expected


def test_band_threshold_is_an_assumption():
    assert tr.band_for("DH1", "County Durham", 2500, town_min=2000) == "town"
    assert tr.band_for("DH1", "County Durham", 2500, town_min=3000) == "rural"


def test_postcode_area_and_london_membership():
    assert tr.postcode_area("SA1") == "SA" and tr.postcode_area("wc1a") == "WC" and tr.postcode_area("E1") == "E"
    assert tr.is_london("E1") and tr.is_london("TW1") and not tr.is_london("TW15") and not tr.is_london("SA1")
    assert len(tr.LONDON_DISTRICTS) == 9 + 5 + 7 + 14


# ---- the operating model's section 4 tables -------------------------------------------------------------
def test_blended_split_is_45_percent():
    assert tr.blended_split(0.5, 0.25, 0.8) == Decimal("0.450")


@pytest.mark.parametrize("area, single, hmo, sa, cost, ads, gross, contribution", [
    # (area, single-let fee, HMO fee, SA fee, cost to serve, ads at launch, [gross at 10, 30, 60], [contribution at 10, 30, 60])
    ("durham", 84, 375, 324, 25, 760, (1320, 4542, 10044), (-1134, 237, 2273)),
    ("andover", 144, "412.50", 360, 25, 760, (1872, 6153, 13170), (-830, 1123, 3993)),
    ("london", 150, 675, 585, 35, 1520, (2370, 8160, 18060), (-1416, 1167, 5082)),
])
def test_operating_model_section_4_tables(area, single, hmo, sa, cost, ads, gross, contribution):
    mixes = ((8, 0, 2), (22, 2, 6), (40, 4, 16))                 # section 4.1 unit mix at 10 / 30 / 60
    ads_at = (ads, ads, 1000 if area != "london" else 2000)     # "rising to 1,000 at 60 units" (2,000 in London)
    months = (1, 12, 12)                                        # launch month, then steady state with the systems fee
    for mix, expected_gross, expected_contribution, ads_now, month in zip(mixes, gross, contribution, ads_at, months):
        singles, hmos, sas = mix
        total_units = singles + hmos + sas
        gross_fee = tr.unit_mix_fee(single, hmo, sa, singles, hmos, sas)
        assert gross_fee == Decimal(expected_gross), f"{area} gross at {total_units} units"
        per_unit = gross_fee / total_units
        fixed = Decimal(550) + Decimal(ads_now) + Decimal(300)
        actual = tr.hq_contribution_month(total_units, per_unit, 0.45, cost, fixed, 99, month)
        assert close(actual, expected_contribution), f"{area} contribution at {total_units} units: {actual}"
        partner = tr.partner_net_month(total_units, per_unit, 0.45, 99, 1)
        assert close(partner, gross_fee * Decimal("0.45")), "partner commission (45%) before the systems fee"


@pytest.mark.parametrize("area, per_unit_fee_at_30, cost, fixed, expected", [
    ("durham", "151.40", 25, 1610, 26),      # section 4.2: "Break-even at roughly 26 units"
    ("andover", "205.10", 25, 1610, 17),     # section 4.3: "roughly 17 units"
    ("london", "272.00", 35, 2370, 20),      # section 4.4: "roughly 20 units"
])
def test_operating_model_break_even(area, per_unit_fee_at_30, cost, fixed, expected):
    units = tr.break_even_units(per_unit_fee_at_30, 0.45, cost, fixed, 99)
    assert units is not None and abs(units - expected) <= 1, f"{area}: {units}"
    below, above = int(units), int(units) + 1          # the contribution crosses zero between the two whole numbers
    assert tr.hq_contribution_month(below, per_unit_fee_at_30, 0.45, cost, fixed, 99, 12) <= 0
    assert tr.hq_contribution_month(above, per_unit_fee_at_30, 0.45, cost, fixed, 99, 12) >= 0


def test_break_even_is_none_when_a_unit_does_not_cover_its_own_cost():
    assert tr.break_even_units(40, 0.45, 25, 1610, 99) is None       # 40 x 55% = 22 < 25 cost to serve


# ---- year one ---------------------------------------------------------------------------------------------
def test_year_one_ramp_and_totals():
    assert [tr.units_in_month(11, m) for m in (0, 1, 6, 11, 12, 13)] == [0, 1, 6, 10, 11, 11]
    fee = Decimal("144.00")
    expected = sum(tr.partner_net_month(tr.units_in_month(11, m), fee, 0.45, 99, m) for m in range(1, 13))
    assert tr.partner_net_year1(11, fee, 0.45, 99) == expected
    assert tr.partner_net_year1(0, fee, 0.45, 99) == Decimal("-891.00"), "nine months of the systems fee with no units"
    fixed = tr.fixed_costs_for("town")
    assert fixed == Decimal("1610.00") and tr.fixed_costs_for("london") == Decimal("2370.00")
    year = tr.hq_contribution_year1(11, fee, 0.45, 25, fixed, 99)
    assert year < 0, "the first year is a proving period, not a profit period (section 4.5)"
    assert year == sum(tr.hq_contribution_month(tr.units_in_month(11, m), fee, 0.45, 25, fixed, 99, m) for m in range(1, 13))


def test_fixed_costs_and_the_systems_fee_are_apportioned_once_per_territory():
    """Operating model section 4.1: portals, ads and letters are costs per AREA and the systems fee is one fee per
    partner, so a territory's districts share them by units_y1 and their figures add up to the territory's."""
    members = {"SP10": {"area": "SP", "units_y1": 11}, "SP11": {"area": "SP", "units_y1": 10}, "SP9": {"area": "SP", "units_y1": 3},
               "RG28": {"area": "RG", "units_y1": 0}, "SO20": {"area": "SO", "units_y1": 1}, "BS1": {"area": "BS", "units_y1": 40},
               "E1": {"area": "E", "units_y1": 60}, "E2": {"area": "E", "units_y1": 0}, "DH8": {"area": "DH", "units_y1": 0},
               "DH9": {"area": "DH", "units_y1": 0}}
    territories = [{"area": "andover", "districts": ["SP10", "SP11", "SP9", "RG28", "SO20"]}, {"area": "london", "districts": ["E"]},
                   {"area": "durham", "districts": ["DH8", "DH9", "DL17"]}]
    shares = tr.territory_shares(members, territories)
    assert shares["SP10"] == Decimal("0.4400") and shares["SP11"] == Decimal("0.4000") and shares["SP9"] == Decimal("0.1200")
    assert shares["RG28"] == 0 and shares["SO20"] == Decimal("0.0400")
    assert sum(shares[c] for c in ("SP10", "SP11", "SP9", "RG28", "SO20")) == 1
    assert shares["E1"] == 1 and shares["E2"] == 0, "a bare area lists every loaded district of that area"
    assert shares["DH8"] == shares["DH9"] == Decimal("0.5000"), "no units anywhere in the territory: equal shares"
    thirds = tr.territory_shares({"A1": {"units_y1": 1}, "A2": {"units_y1": 1}, "A3": {"units_y1": 1}}, [{"area": "t", "districts": ["A1", "A2", "A3"]}])
    assert sum(thirds.values()) == 1 and sorted(thirds.values()) == [Decimal("0.3333"), Decimal("0.3333"), Decimal("0.3334")], "the residual goes to one district"
    assert shares["BS1"] == 1, "a district in no territory stands for its whole postcode area"

    fee = Decimal("144.00")
    whole = tr.model_district(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed=1200)
    half = tr.model_district(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed=1200, fixed_share="0.5", systems_fee_share="0.5")
    assert half["cost_share"] == Decimal("0.5") and half["fixed_costs"] == Decimal("805.00") and half["systems_fee"] == Decimal("49.50")
    assert whole["cost_share"] == 1 and whole["fixed_costs"] == Decimal("1610.00") and whole["systems_fee"] == Decimal("99.00")
    assert half["hq_contribution_y1"] - whole["hq_contribution_y1"] == 12 * Decimal("805.00") - 9 * Decimal("49.50")
    assert half["partner_net_y1"] - whole["partner_net_y1"] == 9 * Decimal("49.50")
    assert half["break_even_units"] == whole["break_even_units"] and half["upfront_fee"] == whole["upfront_fee"], "priced per territory, not per district"
    nothing = tr.model_district(district="RG28", la_name="Basingstoke and Deane", prs_households=200, rent_2bed=1100, fixed_share=0, systems_fee_share=0)
    assert nothing["partner_net_y1"] == tr.partner_net_year1(nothing["units_y1"], nothing["achievable_fee"], Decimal("0.450"), 0)
    assert nothing["hq_contribution_y1"] == tr.hq_contribution_year1(nothing["units_y1"], nothing["achievable_fee"], Decimal("0.450"), 25, 0, 0)
    # the Andover check from the plan is unaffected: the partner's share at 30 units is before any systems fee
    assert tr.partner_net_month(30, fee, 0.5, 0, 1) == Decimal("2160.00")


# ---- pricing --------------------------------------------------------------------------------------------
def test_upfront_fee_rounds_to_500_and_respects_the_floor():
    assert tr.round_to(12960, 500) == 13000 and tr.round_to(12250, 500) == 12500 and tr.round_to(12249, 500) == 12000
    assert tr.upfront_fee(2250, 6, 15000) == Decimal("15000.00"), "London: 6 x 2,250 = 13,500 is under the 15,000 floor"
    assert tr.upfront_fee(1260, 6, tr.upfront_floors(), band="rural") == Decimal("7500.00")   # Durham at 84
    assert tr.upfront_fee(1260, 6, tr.upfront_floors(), band="town") == Decimal("7500.00")
    assert tr.upfront_fee(100, 6, tr.upfront_floors(), band="major_city") == Decimal("10000.00")
    with pytest.raises(ValueError):
        tr.upfront_fee(2160, 6, tr.upfront_floors())
    assert tr.monthly_fees() == {"rural": 49, "town": 79, "major_city": 99, "london": 149}
    assert tr.upfront_floors() == {"rural": 3000, "town": 6000, "major_city": 10000, "london": 15000}


def test_model_district_end_to_end():
    row = tr.model_district(district="sp10", la_name="Test Valley", prs_households=3500, rent_2bed="1135.00")
    assert row["district"] == "SP10" and row["area"] == "SP" and row["band"] == "town"
    assert row["achievable_fee"] == Decimal("136.20") and row["cost_to_serve"] == Decimal("25.00")
    assert (row["units_y1"], row["units_y2"], row["units_y3"]) == (11, 25, 42)
    assert row["upfront_fee"] == Decimal("12500.00") and row["monthly_fee"] == Decimal("79.00")
    assert row["blended_split"] == Decimal("0.450")
    assert row["partner_net_y1"] == tr.partner_net_year1(11, Decimal("136.20"), Decimal("0.450"), 99)
    assert row["hq_contribution_y1"] < 0 and row["break_even_units"] > 0

    london = tr.model_district(district="SE1", la_name="Southwark", prs_households=20000, rent_2bed=2332)
    assert london["band"] == "london" and london["achievable_fee"] == 150 and london["cost_to_serve"] == 35
    assert london["upfront_fee"] == 15000 and london["monthly_fee"] == 149

    unpriced = tr.model_district(district="SP9", la_name="Wiltshire", prs_households=900, rent_2bed=None)
    assert unpriced["band"] == "rural" and unpriced["units_y1"] == 3
    assert unpriced["achievable_fee"] is None and unpriced["upfront_fee"] is None and unpriced["partner_net_y1"] is None
    assert unpriced["monthly_fee"] == 49

    cheaper = tr.model_district(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed=1200,
                                assumptions={"upfront_multiplier": 4, "monthly_fee_town": 59})
    assert cheaper["upfront_fee"] == Decimal("8500.00") and cheaper["monthly_fee"] == Decimal("59.00")


# ---- the migration's seeds agree with the module ---------------------------------------------------------
def _migration_module():
    path = Path(config.ROOT) / "alembic" / "versions" / "0003_franchise.py"
    spec = importlib.util.spec_from_file_location("migration_0003_franchise", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_seeds_match_the_module_defaults():
    migration = _migration_module()
    assert migration.ASSUMPTIONS == tr.DEFAULT_ASSUMPTIONS
    assert migration.COMMISSION_RULES_V1 == tr.DEFAULT_COMMISSION_RULES
    assert migration.COMMISSION_RULES_V1_NOTE.startswith("confirm=false")
    assert len(tr.DEFAULT_ASSUMPTIONS) == 28 and len({k for k, _v, _n in tr.DEFAULT_ASSUMPTIONS}) == 28
    for key, value, note in tr.DEFAULT_ASSUMPTIONS:
        assert "[CONFIRM]" in note, key
        assert isinstance(value, (int, float)), key
    assert tr.DEFAULT_VALUES["capture_y1"] == 0.003 and tr.DEFAULT_VALUES["fee_cap"] == 150
    assert tr.DEFAULT_VALUES["systems_fee"] == 99 and tr.DEFAULT_VALUES["systems_fee_from_month"] == 4
    assert tr.DEFAULT_VALUES["partner_split_own"] == 0.5 and tr.DEFAULT_VALUES["partner_split_hq_leads"] == 0.25


def test_assumptions_seed_is_idempotent_and_never_overwrites(db):
    assert db.query(Assumption).count() == 0, "conftest truncates the migration's seed before every test"
    assert tr.ensure_assumptions(db) == 28
    db.commit()
    assert tr.ensure_assumptions(db) == 0
    assert tr.unconfirmed_assumptions(db) == sorted(k for k, _v, _n in tr.DEFAULT_ASSUMPTIONS)
    assert tr.load_assumptions(db)["capture_y1"] == 0.003
    row = db.get(Assumption, "upfront_multiplier")
    assert row.value == 6 and row.confirmed is False and "[CONFIRM]" in row.note
    row.value = 5
    row.confirmed = True
    db.commit()
    assert tr.ensure_assumptions(db) == 0
    db.expire_all()
    assert db.get(Assumption, "upfront_multiplier").value == 5
    assert "upfront_multiplier" not in tr.unconfirmed_assumptions(db)
    priced = tr.model_district(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed=1200,
                               assumptions=tr.load_assumptions(db))
    assert priced["upfront_fee"] == Decimal("11000.00"), "5 x 2,160 = 10,800 to the nearest 500"


def test_commission_rules_seed_is_idempotent(db):
    assert tr.ensure_commission_rules(db) == 3
    db.commit()
    assert tr.ensure_commission_rules(db) == 0
    rules = {r.seller_kind: r for r in db.query(CommissionRule).filter(CommissionRule.version == 1).all()}
    assert set(rules) == {"mentee", "franchisee", "outsider"}
    for kind in ("mentee", "franchisee"):
        assert (rules[kind].upfront_pct, rules[kind].trail_pct, rules[kind].trail_months) == (Decimal("0.2000"), Decimal("0.1000"), 12)
    assert (rules["outsider"].upfront_pct, rules["outsider"].trail_pct, rules["outsider"].trail_months) == (Decimal("0.1000"), Decimal("0"), 0)
    for rule in rules.values():
        assert rule.confirmed is False and rule.note.startswith("confirm=false") and rule.active_from == date(2026, 10, 20)


# ---- the Phase 4 schema -----------------------------------------------------------------------------------
def _person(db, email="seller@example.test") -> Person:
    person = Person(person_key=email, name="Sam Seller")
    db.add(person)
    db.commit()
    return person


def _district(**overrides) -> PostcodeDistrict:
    values = dict(district="SP10", area="SP", nation="england", la_gss="E07000093", la_name="Test Valley", band="town")
    values.update(overrides)
    return PostcodeDistrict(**values)


def test_district_enumerations_are_enforced_by_the_database(db):
    for bad in (dict(band="suburb"), dict(state="sold"), dict(nation="cornwall"), dict(licensing="rent smart"),
                dict(prs_share=Decimal("1.5")), dict(dwellings=-1)):
        db.add(_district(**bad))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    db.add(_district(prs_households=3500, rent_2bed=Decimal("1135.00"), rent_date=date(2026, 6, 1), licensing="mandatory_only"))
    db.commit()
    row = db.get(PostcodeDistrict, "SP10")
    assert row.state == "open" and row.licensing == "mandatory_only" and row.model_run_id is None


def test_opportunity_and_seller_constraints(db):
    person = _person(db)
    db.add(FranchiseOpportunity(person_id=person.id, stage="won"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(FranchiseOpportunity(person_id=person.id, quoted_upfront_pence=-1))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    opportunity = FranchiseOpportunity(person_id=person.id, districts=["SP10", "SP11"], band="town",
                                       quoted_upfront_pence=1300000, quoted_monthly_pence=7900, source="seller:sam")
    db.add(opportunity)
    db.commit()
    assert opportunity.stage == "enquiry" and opportunity.stage_changed_at is not None

    db.add(Seller(person_id=person.id, kind="mentee", code="SAM"))
    db.commit()
    db.add(Seller(person_id=_person(db, "other@example.test").id, kind="outsider", code="sam"))
    with pytest.raises(IntegrityError):
        db.commit()                 # codes are unique case-insensitively
    db.rollback()
    db.add(Seller(person_id=person.id, kind="franchisee", code="SAM2"))
    with pytest.raises(IntegrityError):
        db.commit()                 # one seller per person
    db.rollback()


def test_commission_ledger_constraints(db):
    person = _person(db)
    seller = Seller(person_id=person.id, kind="mentee", code="SAM")
    opportunity = FranchiseOpportunity(person_id=_person(db, "buyer@example.test").id, stage="paid", band="town",
                                       quoted_upfront_pence=1300000, quoted_monthly_pence=7900)
    tr.ensure_commission_rules(db)
    db.add_all([seller, opportunity])
    db.commit()
    rule = db.query(CommissionRule).filter_by(version=1, seller_kind="mentee").one()

    def entry(**overrides) -> CommissionEntry:
        values = dict(opportunity_id=opportunity.id, seller_id=seller.id, rule_id=rule.id, basis="upfront",
                      basis_pence=1300000, amount_pence=260000)
        values.update(overrides)
        return CommissionEntry(**values)

    for bad in (dict(basis="bonus"), dict(amount_pence=-1), dict(trail_month=1), dict(basis="monthly", trail_month=None),
                dict(basis="monthly", trail_month=0), dict(basis_pence=-5)):
        db.add(entry(**bad))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    first = entry()
    db.add(first)
    db.commit()
    db.add(entry())
    with pytest.raises(IntegrityError):
        db.commit()                 # one live upfront entry per opportunity and seller
    db.rollback()
    db.add(entry(basis="monthly", trail_month=1, basis_pence=7900, amount_pence=790))
    db.commit()
    db.add(entry(basis="monthly", trail_month=1, basis_pence=7900, amount_pence=790))
    with pytest.raises(IntegrityError):
        db.commit()                 # and one per trail month
    db.rollback()
    db.add(entry(basis="monthly", trail_month=2, basis_pence=7900, amount_pence=790))
    db.commit()

    db.add(entry(amount_pence=5, clawback_of=first.id))
    with pytest.raises(IntegrityError):
        db.commit()                 # a clawback is never positive
    db.rollback()
    clawback = entry(amount_pence=-260000, clawback_of=first.id)
    db.add(clawback)
    db.commit()                     # a clawback of the live entry is allowed beside it
    assert clawback.clawback_source.id == first.id
    assert sum(e.amount_pence for e in db.query(CommissionEntry).filter_by(basis="upfront").all()) == 0
