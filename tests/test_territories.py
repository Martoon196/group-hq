"""The territory loader on the hand-made sample files (row counts; SA1 and DH1 derived values match
app.services.territory_rules; terminated postcodes left out of the centroid; holdings and licensing survive a
second run), the Territories screens (list, map, detail), the holding changes (reserve, assign, release, keep for
HQ, each audited) and the state-only export (no money, 401 without a session or the bearer token)."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import config
from app.models import AuditLog, ModelRun, PostcodeDistrict, utcnow
from app.routers import territories as tv
from app.routers.mentees import add_months
from app.services import territory_rules as tr
from tests.conftest import session
from tests.test_people import create_person, flash_of

SAMPLE = Path(config.ROOT) / "data" / "sample"
SAMPLE_DISTRICTS = 134          # the districts of today's five areas plus BS1, DA1, EN6, KT7 and TW15 as controls
TODAY = utcnow().date()


def _loader():
    path = Path(config.ROOT) / "scripts" / "load_territory_model.py"
    spec = importlib.util.spec_from_file_location("load_territory_model", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module          # dataclasses resolve their string annotations through sys.modules
    spec.loader.exec_module(module)
    return module


loader = _loader()


def load_sample(tmp_path: Path, *extra: str) -> tuple[str, dict]:
    out = io.StringIO()
    report = loader.run(loader.parser().parse_args(["--sample", "--centroids-out", str(tmp_path / "centroids.json"), *extra]), out)
    return out.getvalue(), report


def district(code: str) -> PostcodeDistrict:
    with session() as db:
        row = db.get(PostcodeDistrict, code)
        assert row is not None, code
        db.refresh(row)
        return row


def audit_rows(action: str) -> list[AuditLog]:
    with session() as db:
        return db.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()


@pytest.fixture
def centroids(tmp_path, monkeypatch):
    """Point the screens at the centroids file the loader writes into tmp_path."""
    path = tmp_path / "centroids.json"
    monkeypatch.setattr(tv, "CENTROIDS_PATHS", (path,))
    tv._centroid_cache.clear()
    return path


# ---- the readers --------------------------------------------------------------------------------------
def test_parse_month_and_bedrooms():
    assert loader.parse_month("2026-06") == loader.parse_month("2026-06-01") == loader.parse_month("June 2026") == date(2026, 6, 1)
    assert loader.parse_month("Jun 2026") == loader.parse_month("2026 JUN") == loader.parse_month("01/06/2026") == date(2026, 6, 1)
    assert loader.parse_month("") is None and loader.parse_month("soon") is None
    assert [loader.bedrooms_of(x) for x in ("One bedroom", "1 bed", "2", "Three bedrooms", "Four or more bedrooms", "All categories", "")] == [1, 1, 2, 3, 4, None, None]
    assert loader.outward_code("sa1 1aa") == "SA1" and loader.outward_code("SA1") == "SA1" and loader.outward_code(" sw1a 1aa ") == "SW1A"


def test_census_simple_layout_and_rents_wide_layout(tmp_path):
    census = tmp_path / "census.csv"
    census.write_text("la_gss,la_name,households,prs_households\nE07000093,Test Valley,53200,8600\n", encoding="utf-8")
    rows, src = loader.read_census(census)
    assert rows["E07000093"].prs_share == Decimal("0.1617") and rows["E07000093"].la_name == "Test Valley" and src.rows == 1 and not src.sample
    rents = tmp_path / "rents.csv"
    rents.write_text("area_code,area_name,date,rent_1bed,rent_2bed,rent_3bed\nE07000093,Test Valley,2026-05,940,1120,1380\n"
                     "E07000093,Test Valley,2026-06,950,1135,1400\n", encoding="utf-8")
    rows, src = loader.read_rents(rents)
    assert rows["E07000093"].rent_date == date(2026, 6, 1) and rows["E07000093"].rents[2] == Decimal("1135")
    bad = tmp_path / "bad.csv"
    bad.write_text("district,licensing,licensing_note\nSP10,rent smart,\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        loader.read_licensing(bad)


# ---- the loader on the sample ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(tmp_path):
    text, report = load_sample(tmp_path, "--dry-run")
    assert text.startswith("DRY RUN") and "SAMPLE DATA" in text and len(report["rows"]) == SAMPLE_DISTRICTS
    with session() as db:
        assert db.query(PostcodeDistrict).count() == 0 and db.query(ModelRun).count() == 0
    assert not (tmp_path / "centroids.json").exists()


def test_sample_load_row_counts_and_derived_values(tmp_path, admin_client):
    text, report = load_sample(tmp_path)
    assert report["run_id"] is not None and report["counts"] == {"inserted": SAMPLE_DISTRICTS, "updated": 0}
    with session() as db:
        assert db.query(PostcodeDistrict).count() == SAMPLE_DISTRICTS
        run = db.get(ModelRun, report["run_id"])
        assert run.districts == SAMPLE_DISTRICTS and len(run.source_files) == 5 and all(f["sample"] for f in run.source_files)
        assert {f["name"] for f in run.source_files} == {"SAMPLE-onspd-extract.csv", "SAMPLE-census-ts054.csv", "SAMPLE-pipr-rents.csv",
                                                         "SAMPLE-dwellings.csv", "SAMPLE-licensing.csv"}
        assert run.assumptions["capture_y1"] == 0.003 and run.assumptions["fee_cap"] == 150
        assert db.query(PostcodeDistrict).filter(PostcodeDistrict.model_run_id == run.id).count() == SAMPLE_DISTRICTS
    assert report["sample"] is True and "SAMPLE DATA" in text and "band " in text

    # SA1: Swansea (W06000011), 21,500 of 105,400 households private rented (0.2040), 16,000 dwellings, two-bed 775
    sa1 = district("SA1")
    assert (sa1.area, sa1.nation, sa1.la_gss, sa1.la_name) == ("SA", "wales", "W06000011", "Swansea")
    assert sa1.dwellings == 16000 and sa1.prs_share == Decimal("0.2040") and sa1.prs_households == 3264
    assert (sa1.rent_1bed, sa1.rent_2bed, sa1.rent_3bed, sa1.rent_date) == (Decimal("620.00"), Decimal("775.00"), Decimal("900.00"), date(2026, 6, 1))
    assert sa1.licensing == "additional" and "Rent Smart Wales" in sa1.licensing_note
    expected = tr.model_district(district="SA1", la_name="Swansea", prs_households=3264, rent_2bed=Decimal("775.00"), la_gss="W06000011",
                                 fixed_share=sa1.cost_share, systems_fee_share=sa1.cost_share)
    assert Decimal("0") < sa1.cost_share < Decimal("1"), "SA1 is one of eighteen Swansea districts, so it carries a share of the area's costs"
    for col in ("band", "achievable_fee", "cost_to_serve", "units_y1", "units_y2", "units_y3", "partner_net_y1", "hq_contribution_y1"):
        assert getattr(sa1, col) == expected[col], col
    assert sa1.band == "town" and sa1.achievable_fee == Decimal("93.00") and sa1.units_y1 == 10
    assert sa1.state == "hq" and sa1.partner_id is None

    # DH1: County Durham (E06000047), 37,000 of 234,800 (0.1576), 20,000 dwellings, two-bed 650
    dh1 = district("DH1")
    assert (dh1.nation, dh1.la_name, dh1.dwellings, dh1.prs_share, dh1.prs_households) == ("england", "County Durham", 20000, Decimal("0.1576"), 3152)
    assert dh1.rent_2bed == Decimal("650.00") and dh1.licensing == "selective"
    expected = tr.model_district(district="DH1", la_name="County Durham", prs_households=3152, rent_2bed=Decimal("650.00"), la_gss="E06000047",
                                 fixed_share=dh1.cost_share, systems_fee_share=dh1.cost_share)
    for col in ("band", "achievable_fee", "cost_to_serve", "units_y1", "units_y2", "units_y3", "partner_net_y1", "hq_contribution_y1"):
        assert getattr(dh1, col) == expected[col], col
    assert dh1.band == "town" and dh1.achievable_fee == Decimal("78.00") and dh1.cost_to_serve == Decimal("25.00")
    assert dh1.state == "reserved" and dh1.partner_id is None and dh1.reserved_until is None

    # the band rule across the sample
    assert district("DH7").band == "rural" and district("SP10").band == "town" and district("SP9").band == "rural"
    assert district("E1").band == "london" and district("E1").cost_to_serve == Decimal("35.00") and district("E1").achievable_fee == Decimal("150.00")
    assert district("DA5").band == "london" and district("DA1").band == "town", "DA1 (Dartford) is not a London district"
    assert district("TW14").band == "london" and district("TW15").band == "rural"
    assert district("BS1").band == "major_city" and district("BS1").state == "open", "Bristol is a core city and in no territory"

    # the area fixed costs and the systems fee are charged once per territory, apportioned by units_y1
    andover = [district(c) for c in ("SP10", "SP11", "SP9", "RG28", "SO20")]
    assert sum(d.cost_share for d in andover) == Decimal("1.0000")
    units = sum(d.units_y1 for d in andover)
    assert units > 0 and all(abs(d.cost_share - Decimal(d.units_y1) / Decimal(units)) <= Decimal("0.0002") for d in andover)
    a = tr.assumption_values()
    split = tr.blended_split(a["partner_split_own"], a["partner_split_hq_leads"], a["partner_own_lead_share"])
    variable_hq = sum(tr.hq_contribution_year1(d.units_y1, d.achievable_fee, split, d.cost_to_serve, 0, 0) for d in andover)
    variable_partner = sum(tr.partner_net_year1(d.units_y1, d.achievable_fee, split, 0) for d in andover)
    fixed_once = 12 * tr.fixed_costs_for("town") - 9 * Decimal(99)        # Andover is town or rural: the same fixed costs
    assert abs(sum(d.hq_contribution_y1 for d in andover) - (variable_hq - fixed_once)) <= Decimal("1.50")
    assert abs(sum(d.partner_net_y1 for d in andover) - (variable_partner - Decimal(891))) <= Decimal("1.50"), "the 99 fee nine times, once"
    assert district("BS1").cost_share == Decimal("1.0000"), "a district in no territory stands for its whole area"
    assert "fixed costs and systems fee once" in text
    with session() as db:
        run = db.query(ModelRun).order_by(ModelRun.id.desc()).first()
        assert run.assumptions["fixed_cost_basis"] == tr.FIXED_COST_BASIS and run.assumptions["territories_file"] == "data/current_territories.json"
    page = admin_client.get("/territories/SP9")
    assert page.status_code == 200 and "Share of the territory's fixed costs" in page.text and "sits on the other districts" in page.text
    assert district("SA14").nation == "wales" and district("SA14").la_name == "Carmarthenshire"

    # today's holdings from data/current_territories.json
    assert district("E1").state == "core" and district("E1").partner_id == 3
    assert district("TW6").state == "core" and district("TW6").partner_id == 3
    assert district("TA1").state == "core" and district("TA1").partner_id == 4 and district("BS49").partner_id == 4
    assert district("SP10").state == "reserved" and district("SA18").state == "hq"
    assert report["state_counts"] == {"hq": 18, "reserved": 22, "core": 89, "open": 5}
    assert report["seed"]["london"]["seeded"] and not report["seed"]["london"]["kept"]

    # the centroid leaves the terminated postcode out
    data = json.loads((tmp_path / "centroids.json").read_text())
    assert data["sample"] is True and len(data["districts"]) == SAMPLE_DISTRICTS
    lat, lon = data["districts"]["SA1"]
    assert abs(lat - 51.620) < 0.001 and abs(lon - (-3.940)) < 0.001, "the mean of the two live postcodes, not the 99.999999 of the terminated one"


def test_second_run_keeps_holdings_and_licensing(tmp_path, admin_client):
    load_sample(tmp_path)
    r = admin_client.post("/territories/SA8/release")
    assert r.status_code == 303
    with session() as db:
        e1 = db.get(PostcodeDistrict, "E1")
        e1.state, e1.partner_id, e1.reserved_until = "reserved", 42, date(2027, 1, 31)
        sp11 = db.get(PostcodeDistrict, "SP11")
        sp11.licensing, sp11.licensing_note = "selective", "typed in the app"
        db.commit()
    text, report = load_sample(tmp_path)
    assert report["counts"] == {"inserted": 0, "updated": SAMPLE_DISTRICTS}
    assert district("SA8").state == "open", "a district released in the app is not put back by the next load"
    e1 = district("E1")
    assert (e1.state, e1.partner_id, e1.reserved_until) == ("reserved", 42, date(2027, 1, 31))
    assert district("SP11").licensing == "selective", "licensing typed by hand survives a load with no line for it"
    assert district("SA1").licensing == "additional"
    with session() as db:
        assert db.query(ModelRun).count() == 2
        latest = db.query(ModelRun).order_by(ModelRun.id.desc()).first()
        assert db.query(PostcodeDistrict).filter(PostcodeDistrict.model_run_id == latest.id).count() == SAMPLE_DISTRICTS
    assert report["seed"]["swansea"]["kept"] and "SA8" in report["seed"]["swansea"]["kept"]
    text, report = load_sample(tmp_path, "--reseed-states")
    assert district("SA8").state == "hq", "--reseed-states applies the file to every open, unheld district"
    assert district("E1").partner_id == 42, "but never to one that is held"


def test_territories_file_is_validated(tmp_path):
    bad = tmp_path / "t.json"
    bad.write_text(json.dumps({"territories": [{"area": "x", "state": "sold", "districts": ["SA1"]}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        loader.read_territories(bad)
    good = tmp_path / "g.json"
    good.write_text(json.dumps({"territories": [{"area": "x", "state": "core", "partner_id": 9, "districts": ["E", "SA1", "ZZ9"]}]}), encoding="utf-8")
    entries = loader.read_territories(good)
    assert loader.expand_territory(entries[0]["districts"], {"E1", "E14", "SA1", "N1"}) == ["E1", "E14", "SA1"]


# ---- the screens ---------------------------------------------------------------------------------------------
def test_screens_need_a_session(client):
    assert client.get("/territories").status_code == 303
    assert client.get("/territories/map").status_code == 303
    assert client.get("/territories/SA1").status_code == 303
    assert client.post("/territories/SA1/release").status_code == 303


def test_list_map_and_detail_render(tmp_path, admin_client, centroids):
    empty = admin_client.get("/territories")
    assert empty.status_code == 200 and "No territory model has been loaded yet" in empty.text
    load_sample(tmp_path)

    page = admin_client.get("/territories")
    assert page.status_code == 200
    assert f"{SAMPLE_DISTRICTS} districts" in page.text and "SAMPLE DATA" in page.text and "[CONFIRM]" in page.text
    assert "18 head office" in page.text and "89 core" in page.text
    assert page.text.index('href="/territories/SA1"') < page.text.index('href="/territories/SA2"') < page.text.index('href="/territories/SA10"'), "postcode order"

    hq = admin_client.get("/territories", params={"state": "hq"})
    assert "18 districts matching" in hq.text and 'href="/territories/E1"' not in hq.text
    wales = admin_client.get("/territories", params={"nation": "wales", "band": "rural"})
    assert "districts matching" in wales.text and "Swansea" in wales.text and "Durham" not in wales.text
    area = admin_client.get("/territories", params={"area": "sp"})
    assert "3 districts matching" in area.text, "SP9, SP10 and SP11; RG28 and SO20 are other areas"
    search = admin_client.get("/territories", params={"q": "Test Valley"})
    assert "3 districts matching" in search.text

    mapped = admin_client.get("/territories/map", params={"area": "SA"})
    assert mapped.status_code == 200
    assert 'id="territory-data"' in mapped.text and "/assets/territories.js" in mapped.text
    assert 'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="' in mapped.text and "unpkg.com/leaflet@1.9.4/dist/" in mapped.text
    assert "18 districts on the map" in mapped.text
    start = mapped.text.index('id="territory-data">') + len('id="territory-data">')
    data = json.loads(mapped.text[start:mapped.text.index("</script>", start)])
    assert len(data["districts"]) == 18 and data["sample"] is True
    sa1 = next(d for d in data["districts"] if d["district"] == "SA1")
    assert sa1["state"] == "hq" and abs(sa1["lat"] - 51.62) < 0.001 and sa1["confirm"] is True and sa1["upfront_fee"] == "£8,500.00"

    js = admin_client.get("/assets/territories.js")
    assert js.status_code == 200 and "circleMarker" in js.text and "tile.openstreetmap.org" in js.text

    detail = admin_client.get("/territories/sa1")
    assert detail.status_code == 200
    assert "Swansea" in detail.text and "W06000011" in detail.text and "additional" in detail.text and "Rent Smart Wales" in detail.text
    assert "£93.00" in detail.text and "[CONFIRM]" in detail.text and "16,000" in detail.text and "3,264" in detail.text
    assert "never offered" in detail.text.lower() and "Release" in detail.text and "Reserve" not in detail.text.split("Holding")[1].split("History")[0].replace("Reserved:", "")
    assert admin_client.get("/territories/ZZ9").status_code == 404

    tv._centroid_cache.clear()
    centroids.unlink()
    none = admin_client.get("/territories/map")
    assert none.status_code == 200 and "no centroid on file" in none.text and "0 districts on the map" in none.text


# ---- holding changes ---------------------------------------------------------------------------------------
def _rows(*rows: dict) -> None:
    with session() as db:
        for values in rows:
            base = dict(area=tr.postcode_area(values["district"]), nation="england", band="town")
            base.update(values)
            db.add(PostcodeDistrict(**base))
        db.commit()


def test_reserve_assign_release_and_hq_are_audited(admin_client):
    _rows(dict(district="SP10", la_name="Test Valley"), dict(district="SA1", nation="wales", state="hq"),
          dict(district="E1", band="london", state="core", partner_id=3))

    r = admin_client.post("/territories/SP10/reserve", data={"partner_id": "7", "person_id": "", "reserved_until": ""})
    assert r.status_code == 303 and "reserved for partner 7" in flash_of(r.headers["location"])["ok"]
    sp10 = district("SP10")
    assert (sp10.state, sp10.partner_id, sp10.reserved_until) == ("reserved", 7, add_months(TODAY, 12))
    rows = audit_rows("territory.reserve")
    assert len(rows) == 1 and rows[0].entity == "district:SP10" and rows[0].before == {"state": "open", "partner_id": None, "reserved_until": None}
    assert rows[0].after["state"] == "reserved" and rows[0].after["partner_id"] == 7

    again = admin_client.post("/territories/SP10/reserve", data={"partner_id": "7", "reserved_until": add_months(TODAY, 12).isoformat()})
    assert "Nothing changed" in flash_of(again.headers["location"])["ok"] and len(audit_rows("territory.reserve")) == 1

    r = admin_client.post("/territories/SP10/assign", data={"partner_id": "7"})
    assert r.status_code == 303 and "core district for partner 7" in flash_of(r.headers["location"])["ok"]
    sp10 = district("SP10")
    assert (sp10.state, sp10.partner_id, sp10.reserved_until) == ("core", 7, None)
    assert audit_rows("territory.assign")[0].before["state"] == "reserved"

    r = admin_client.post("/territories/SP10/assign", data={"partner_id": "8"})
    assert r.status_code == 400 and "held by partner 7" in r.text and district("SP10").partner_id == 7

    r = admin_client.post("/territories/SP10/hq")
    assert r.status_code == 303 and "Release it first" in flash_of(r.headers["location"])["err"]

    r = admin_client.post("/territories/SP10/release")
    assert r.status_code == 303 and "open again" in flash_of(r.headers["location"])["ok"]
    assert (district("SP10").state, district("SP10").partner_id) == ("open", None)
    assert audit_rows("territory.release")[0].before == {"state": "core", "partner_id": 7}, "only the fields that moved"
    r = admin_client.post("/territories/SP10/release")
    assert "already open" in flash_of(r.headers["location"])["ok"] and len(audit_rows("territory.release")) == 1

    r = admin_client.post("/territories/SP10/hq")
    assert r.status_code == 303 and district("SP10").state == "hq" and audit_rows("territory.hq")[0].after == {"state": "hq"}

    r = admin_client.post("/territories/SA1/reserve", data={"partner_id": "7"})
    assert r.status_code == 400 and "never offered" in r.text and district("SA1").state == "hq"
    r = admin_client.post("/territories/SA1/assign", data={"partner_id": "7"})
    assert r.status_code == 400 and district("SA1").state == "hq"
    r = admin_client.post("/territories/E1/reserve", data={"partner_id": "9"})
    assert r.status_code == 400 and "core district held by partner 3" in r.text
    r = admin_client.post("/territories/SA1/release")
    assert r.status_code == 303 and district("SA1").state == "open", "head office districts can be released (SA8 to SA18 are releasable)"

    assert admin_client.post("/territories/ZZ9/release").status_code == 404
    assert admin_client.post("/territories/E1/release", headers={"Origin": "https://evil.example"}).status_code == 403


def test_reserve_form_validation_and_person_choice(admin_client):
    _rows(dict(district="SP11", la_name="Test Valley"))
    r = admin_client.post("/territories/SP11/reserve", data={"partner_id": "", "person_id": "", "reserved_until": ""})
    assert r.status_code == 400 and "Reserve it for whom" in r.text
    r = admin_client.post("/territories/SP11/reserve", data={"partner_id": "abc"})
    assert r.status_code == 400 and "whole number" in r.text
    r = admin_client.post("/territories/SP11/reserve", data={"partner_id": "7", "reserved_until": TODAY.isoformat()})
    assert r.status_code == 400 and "after today" in r.text
    r = admin_client.post("/territories/SP11/reserve", data={"partner_id": "7", "reserved_until": add_months(TODAY, 36).isoformat()})
    assert r.status_code == 400 and "typo" in r.text
    r = admin_client.post("/territories/SP11/reserve", data={"partner_id": "7", "reserved_until": "next year"})
    assert r.status_code == 400 and "does not look like a date" in r.text
    assert district("SP11").state == "open" and not audit_rows("territory.reserve")

    pid = create_person(admin_client, "ina@example.test", "Ina")
    r = admin_client.post("/territories/SP11/reserve", data={"person_id": str(pid)})
    assert r.status_code == 400 and "no Hello New Home partner id yet" in r.text
    links = admin_client.post(f"/people/{pid}/links", data={"hnh_partner_id": "12"})
    assert links.status_code == 303
    detail = admin_client.get("/territories/SP11")
    assert f'<option value="{pid}"' in detail.text and "(partner 12)" in detail.text
    r = admin_client.post("/territories/SP11/reserve", data={"person_id": str(pid), "partner_id": "13"})
    assert r.status_code == 400 and "differ" in r.text
    r = admin_client.post("/territories/SP11/reserve", data={"person_id": str(pid), "reserved_until": "2027-03-31"})
    assert r.status_code == 303
    sp11 = district("SP11")
    assert (sp11.state, sp11.partner_id, sp11.reserved_until) == ("reserved", 12, date(2027, 3, 31))
    detail = admin_client.get("/territories/SP11")
    assert "partner 12 (Ina)" in detail.text and "31 Mar 2027" in detail.text and "territory.reserve" in detail.text


# ---- the export --------------------------------------------------------------------------------------------
def test_export_is_state_only_and_guarded(client, admin_client, monkeypatch):
    _rows(dict(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed=Decimal("1135.00"), achievable_fee=Decimal("136.20"),
               units_y1=11, partner_net_y1=Decimal("3521.88"), hq_contribution_y1=Decimal("-14835.48"), state="reserved", partner_id=7),
          dict(district="SA1", nation="wales", state="hq"), dict(district="SA2", nation="wales", state="hq"),
          dict(district="E1", band="london", state="core", partner_id=3))

    anonymous = client.get("/api/territories.json")
    assert anonymous.status_code == 401 and anonymous.json()["ok"] is False

    r = admin_client.get("/api/territories.json")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] is True and body["count"] == 4 and body["sample"] is False
    assert [d["district"] for d in body["districts"]] == ["E1", "SA1", "SA2", "SP10"]
    for d in body["districts"]:
        assert set(d) == {"district", "area", "state"}, d
    assert body["districts"][3] == {"district": "SP10", "area": "SP", "state": "reserved"}
    text = r.text.lower()
    for word in ("fee", "rent", "net", "contribution", "units", "partner_id", "reserved_until", "households", "dwellings", "£",
                 "band", "london", "rural", "town"):
        assert word not in text, word
    assert set(tv.EXPORT_FIELDS) == {"district", "area", "state"}, "no money and no band: the band is an unconfirmed pricing classification"

    monkeypatch.setattr(tv, "EXPORT_TOKEN", "hnh-proxy-token")
    assert client.get("/api/territories.json", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/territories.json", headers={"Authorization": "Basic hnh-proxy-token"}).status_code == 401
    proxied = client.get("/api/territories.json", headers={"Authorization": "Bearer hnh-proxy-token"})
    assert proxied.status_code == 200 and proxied.json()["count"] == 4
    monkeypatch.setattr(tv, "EXPORT_TOKEN", "")
    assert client.get("/api/territories.json", headers={"Authorization": "Bearer hnh-proxy-token"}).status_code == 401, "no token configured means session only"


# ---- the badge and the price helpers -----------------------------------------------------------------------
def test_confirm_badges_follow_the_assumptions():
    everything = {k for k, _v, _n in tr.DEFAULT_ASSUMPTIONS}
    assert all(tv.confirm_badges(everything).values())
    assert not any(tv.confirm_badges(set()).values())
    only_fees = tv.confirm_badges({"monthly_fee_town"})
    assert only_fees["monthly_fee"] and not only_fees["achievable_fee"] and not only_fees["upfront_fee"]
    only_capture = tv.confirm_badges({"capture_y1"})
    assert only_capture["units"] and only_capture["partner_net_y1"] and not only_capture["achievable_fee"]
    assert not only_capture["blended_split"] and not only_fees["blended_split"]
    only_split = tv.confirm_badges({"partner_split_own", "partner_split_hq_leads", "partner_own_lead_share"})
    assert only_split["blended_split"] and only_split["partner_net_y1"] and not only_split["units"]
    assert not tv.confirm_badges({"fee_cap"})["blended_split"], "the share has its own dependency set, not partner_net_y1's"


def test_price_for_matches_model_district():
    row = PostcodeDistrict(district="SP10", area="SP", nation="england", band="town", achievable_fee=Decimal("136.20"), cost_to_serve=Decimal("25.00"))
    price = tv.price_for(row, tr.assumption_values())
    model = tr.model_district(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed="1135.00")
    assert price["upfront_fee"] == model["upfront_fee"] == Decimal("12500.00")
    assert price["monthly_fee"] == model["monthly_fee"] == Decimal("79.00")
    assert price["break_even_units"] == model["break_even_units"]
    unpriced = tv.price_for(PostcodeDistrict(district="SP9", area="SP", nation="england", band="rural"), tr.assumption_values())
    assert unpriced["upfront_fee"] is None and unpriced["monthly_fee"] == Decimal("49.00")
    assert tv.district_code(" sa1 1aa ") == "SA1" and tv.district_code("sw1a") == "SW1A"
