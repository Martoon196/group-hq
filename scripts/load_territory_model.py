"""Load the territory model: public data in, one row per postcode district out, every modelled column
computed through app.services.territory_rules, one model_runs row per run.

    python scripts/load_territory_model.py --sample --dry-run      # read data/sample, write nothing, print the summary
    python scripts/load_territory_model.py --sample                # load the sample into the database named by DATABASE_URL
    python scripts/load_territory_model.py                         # the real files under data/ (see data/README.md)
    python scripts/load_territory_model.py --onspd ~/Downloads/onspd.csv --census ~/Downloads/ts054.csv --rents ~/Downloads/pipr.csv

Inputs (data/README.md says where each one comes from and what its columns are):

  --onspd       ONS Postcode Directory extract: pcds, oslaua, lat, long (doterm and ctry are used when present).
                Reduced to one row per district: the majority local authority and the mean centroid of the live
                postcodes with a coordinate.
  --census      Census 2021 TS054 (tenure) by local authority: total households and private-rented households,
                in the ONS custom-dataset layout or the simple la_gss,la_name,households,prs_households layout.
  --rents       ONS Price Index of Private Rents, local authority rents by bedroom with a date; the latest month
                in the file is used and becomes rent_date.
  --dwellings   optional: district,dwellings from Property Predator's address points (the SQL is in data/README.md).
                Without it, or for a district missing from it, the authority's private-rented households are
                apportioned by live postcodes instead, a cruder proxy, and dwellings stays empty.
  --licensing   optional: district,licensing,licensing_note typed by hand.
  --la-names    optional: the ONSPD "LA_UA names and codes" lookup, for authorities the census file does not name.
  --territories data/current_territories.json: who holds today's five areas. Applied only to districts inserted
                by this run (the first load, or a district new to the extract), so a holding changed on the
                Territories screen is never put back by a load. --reseed-states applies it to every district still
                open with no partner; --no-seed-states skips it. Whatever the flags, the file also says which
                districts form one territory, so the area fixed costs and the systems fee are apportioned across
                them by units_y1 (cost_share) instead of being charged in full to every district.

What the loader never touches: state, partner_id and reserved_until on an existing row. A row's licensing is
replaced only when the licensing file has a line for it.

Outputs: postcode_districts upserted, a model_runs row with the files' names, sha256 digests and row counts and
the assumptions snapshot, the district centroids as JSON (data/district_centroids.json, or
data/sample/district_centroids.json with --sample; the map reads that file, so commit it after a real run), and
a per-band summary on stdout. --dry-run does everything in a transaction that is rolled back and writes no
file. Any file whose header has a `sample` column marks the run as sample data and the screens say so.

Every modelled figure is a proposal: the assumptions are read from the assumptions table (seeded with
confirmed = false) and the screens badge them [CONFIRM]. Nothing in a sample file is a fact about a district.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import territory_rules as tr  # noqa: E402

DATA_DIR = ROOT / "data"
SAMPLE_DIR = DATA_DIR / "sample"
DEFAULT_FILES = {
    "onspd": "onspd-extract.csv", "census": "census-ts054.csv", "rents": "pipr-rents.csv",
    "dwellings": "dwellings.csv", "licensing": "licensing.csv",
}
SAMPLE_FILES = {key: f"SAMPLE-{name}" for key, name in DEFAULT_FILES.items()}
TERRITORIES_FILE = DATA_DIR / "current_territories.json"
CENTROIDS_FILE = DATA_DIR / "district_centroids.json"
SAMPLE_CENTROIDS_FILE = SAMPLE_DIR / "district_centroids.json"

OUTWARD_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")
# ONSPD country codes (ctry) and the first letter of a local authority GSS code, to the nation vocabulary.
CTRY = {"E92000001": "england", "W92000004": "wales", "S92000003": "scotland", "N92000002": "northern_ireland"}
GSS_NATION = {"E": "england", "W": "wales", "S": "scotland", "N": "northern_ireland"}
# The area rule from docs/shared-keys.md section 4 (PP services/jurisdiction.py is canonical), used only when a
# district carries neither a country code nor a local authority code. TD is "likely Scotland" there; CF, LD, LL,
# NP and SA are Hello New Home's presentation region for Wales (NP and LL contain English districts).
SCOTLAND_AREAS = frozenset({"AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY", "ML", "PA", "PH", "ZE", "TD"})
NI_AREAS = frozenset({"BT"})
WALES_AREAS = frozenset({"CF", "LD", "LL", "NP", "SA"})
NO_COORDINATE_LAT = 99.0     # ONSPD writes 99.999999 / 0.000000 for a postcode with no grid reference

FOUR_DP = Decimal("0.0001")
PENCE = Decimal("0.01")


# ---- small helpers -------------------------------------------------------------------------------------
def outward_code(raw: str) -> str:
    """The district of a postcode as docs/shared-keys.md section 3 has it: 'sa1 1aa' and 'SA1' both give SA1."""
    s = re.sub(r"\s+", "", str(raw or "").upper())
    if len(s) >= 5:
        s = s[:-3]
    return s


def column(headers: list[str], *needles: str, exclude: tuple[str, ...] = ()) -> str | None:
    """The first header containing every needle (case-insensitive) and none of the excluded words."""
    for header in headers:
        low = header.lower()
        if all(n in low for n in needles) and not any(x in low for x in exclude):
            return header
    return None


def to_int(raw: Any) -> int | None:
    text = str(raw or "").strip().replace(",", "")
    if text in ("", "-", "..", "x", "c"):          # the ONS marks suppressed and missing cells this way
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def to_decimal(raw: Any) -> Decimal | None:
    text = str(raw or "").strip().replace(",", "").replace("£", "")
    if text in ("", "-", "..", "x", "c"):
        return None
    try:
        return Decimal(text)
    except ArithmeticError:
        return None


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def parse_month(raw: Any) -> date | None:
    """'2026-06', '2026-06-01', 'June 2026', 'Jun 2026', '2026 JUN', '01/06/2026' all give 1 June 2026."""
    text = str(raw or "").strip()
    if not text:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if m:
        return date(int(m.group(3)), int(m.group(2)), 1)
    m = re.match(r"^([A-Za-z]{3,9})\s+(\d{4})$", text) or re.match(r"^(\d{4})\s+([A-Za-z]{3,9})$", text)
    if m:
        a, b = m.group(1), m.group(2)
        word, year = (a, b) if a.isalpha() else (b, a)
        month = _MONTHS.get(word[:3].lower())
        if month:
            return date(int(year), month, 1)
    return None


def bedrooms_of(raw: Any) -> int | None:
    """'One bedroom', '1 bed', '2' give 1, 1, 2; 'Four or more' gives 4; 'All categories' gives None."""
    text = str(raw or "").strip().lower()
    if not text or text.startswith("all") or text.startswith("total"):
        return None
    m = re.match(r"^\s*(\d+)", text)
    if m:
        return int(m.group(1))
    for word, n in (("one", 1), ("two", 2), ("three", 3), ("four", 4), ("studio", 0)):
        if text.startswith(word):
            return n
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_csv(path: Path):
    return path.open("r", encoding="utf-8-sig", newline="")


@dataclass
class SourceFile:
    name: str
    path: str
    sha256: str
    rows: int
    sample: bool

    def as_json(self) -> dict:
        return {"name": self.name, "path": self.path, "sha256": self.sha256, "rows": self.rows, "sample": self.sample}


def describe(path: Path, rows: int, sample: bool) -> SourceFile:
    try:
        shown = str(path.resolve().relative_to(ROOT))
    except ValueError:
        shown = str(path)
    return SourceFile(name=path.name, path=shown, sha256=sha256_of(path), rows=rows, sample=sample)


def is_sample(headers: list[str]) -> bool:
    return any(h.strip().lower() == "sample" for h in headers)


# ---- the readers ---------------------------------------------------------------------------------------
@dataclass
class DistrictGeo:
    district: str
    la_counts: Counter = field(default_factory=Counter)
    ctry_counts: Counter = field(default_factory=Counter)
    postcodes: int = 0
    lat_sum: float = 0.0
    lon_sum: float = 0.0
    coords: int = 0

    @property
    def la_gss(self) -> str | None:
        return self.la_counts.most_common(1)[0][0] if self.la_counts else None

    @property
    def centroid(self) -> tuple[float, float] | None:
        if not self.coords:
            return None
        return (round(self.lat_sum / self.coords, 6), round(self.lon_sum / self.coords, 6))


def read_onspd(path: Path) -> tuple[dict[str, DistrictGeo], SourceFile]:
    """One DistrictGeo per outward code from the live postcodes in the extract."""
    geo: dict[str, DistrictGeo] = {}
    rows = 0
    with open_csv(path) as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        pcds = column(headers, "pcds") or column(headers, "pcd") or column(headers, "postcode")
        oslaua = column(headers, "oslaua") or column(headers, "laua") or column(headers, "lad")
        lat = column(headers, "lat")
        lon = column(headers, "long") or column(headers, "lon")
        if not (pcds and oslaua and lat and lon):
            raise SystemExit(f"{path}: expected the columns pcds, oslaua, lat and long; found {headers}")
        doterm = column(headers, "doterm")
        ctry = column(headers, "ctry")
        for row in reader:
            rows += 1
            if doterm and (row.get(doterm) or "").strip():
                continue                                   # a terminated postcode
            district = outward_code(row.get(pcds, ""))
            if not OUTWARD_RE.match(district):
                continue
            g = geo.setdefault(district, DistrictGeo(district))
            g.postcodes += 1
            gss = (row.get(oslaua) or "").strip().upper()
            if gss:
                g.la_counts[gss] += 1
            if ctry:
                code = (row.get(ctry) or "").strip().upper()
                if code:
                    g.ctry_counts[code] += 1
            try:
                la_f, lo_f = float(row.get(lat) or ""), float(row.get(lon) or "")
            except ValueError:
                continue
            if la_f >= NO_COORDINATE_LAT or (la_f == 0 and lo_f == 0):
                continue
            g.lat_sum += la_f
            g.lon_sum += lo_f
            g.coords += 1
        sample = is_sample(headers)
    return geo, describe(path, rows, sample)


@dataclass
class CensusRow:
    la_gss: str
    la_name: str
    households: int
    prs_households: int

    @property
    def prs_share(self) -> Decimal:
        if not self.households:
            return Decimal("0")
        return (Decimal(self.prs_households) / Decimal(self.households)).quantize(FOUR_DP, rounding=ROUND_HALF_UP)


def read_census(path: Path) -> tuple[dict[str, CensusRow], SourceFile]:
    """TS054 by local authority. Two layouts: the ONS custom-dataset one (a row per authority and tenure category,
    'Observation' as the count: the total is every category but 'Does not apply', private rented is every category
    whose label says so) and the simple one (la_gss, la_name, households, prs_households)."""
    out: dict[str, CensusRow] = {}
    rows = 0
    with open_csv(path) as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        simple_gss = column(headers, "la_gss") or column(headers, "gss")
        simple_total = column(headers, "households", exclude=("prs", "private"))
        simple_prs = column(headers, "prs_households") or column(headers, "private", "households")
        if simple_gss and simple_total and simple_prs:
            name_col = column(headers, "la_name") or column(headers, "name")
            for row in reader:
                rows += 1
                gss = (row.get(simple_gss) or "").strip().upper()
                total, prs = to_int(row.get(simple_total)), to_int(row.get(simple_prs))
                if gss and total is not None and prs is not None:
                    out[gss] = CensusRow(gss, (row.get(name_col) or "").strip() if name_col else "", total, prs)
            return out, describe(path, rows, is_sample(headers))

        code_col = column(headers, "authorit", "code") or column(headers, "geography", "code") or column(headers, "area", "code")
        tenure_code_col = column(headers, "tenure", "code")
        tenure_col = column(headers, "tenure", exclude=("code",))
        value_col = column(headers, "observation") or column(headers, "value") or column(headers, "count")
        if not (code_col and tenure_col and value_col):
            raise SystemExit(f"{path}: could not find the census columns. Expected either la_gss, la_name, households, "
                             f"prs_households, or the ONS layout with a '... Code' geography column, a 'Tenure ...' column "
                             f"and 'Observation'. Found {headers}")
        name_col = code_col[: -len("code")].rstrip() if code_col.lower().endswith("code") else None
        if name_col not in headers:
            name_col = column(headers, "authorit", exclude=("code",)) or column(headers, "geography", exclude=("code",))
        totals: dict[str, int] = defaultdict(int)
        prs: dict[str, int] = defaultdict(int)
        names: dict[str, str] = {}
        for row in reader:
            rows += 1
            gss = (row.get(code_col) or "").strip().upper()
            label = (row.get(tenure_col) or "").strip().lower()
            tcode = (row.get(tenure_code_col) or "").strip() if tenure_code_col else ""
            value = to_int(row.get(value_col))
            if not gss or value is None:
                continue
            if name_col and row.get(name_col):
                names[gss] = row[name_col].strip()
            if tcode == "-8" or label.startswith("does not apply"):
                continue
            if label.startswith("total") or label.startswith("all "):
                totals[gss] = value              # a file that carries its own total row wins over the sum
                continue
            totals.setdefault(gss, 0)
            totals[gss] += value
            if "private rented" in label or "private landlord" in label:
                prs[gss] += value
        for gss, total in totals.items():
            out[gss] = CensusRow(gss, names.get(gss, ""), total, prs.get(gss, 0))
        return out, describe(path, rows, is_sample(headers))


@dataclass
class RentRow:
    la_gss: str
    la_name: str
    rent_date: date
    rents: dict[int, Decimal] = field(default_factory=dict)       # bedrooms -> pounds a month


def read_rents(path: Path) -> tuple[dict[str, RentRow], SourceFile]:
    """PIPR local authority rents by bedroom. Long layout (date, area code, area name, bedroom category, rent) or
    wide (area_code, area_name, date, rent_1bed, rent_2bed, rent_3bed). The latest month per authority wins."""
    out: dict[str, RentRow] = {}
    rows = 0
    with open_csv(path) as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        code_col = (column(headers, "area_code") or column(headers, "la_gss") or column(headers, "area", "code")
                    or column(headers, "geography", "code") or column(headers, "authorit", "code") or column(headers, "code"))
        name_col = (column(headers, "area_name") or column(headers, "la_name") or column(headers, "area", "name")
                    or column(headers, "geography", exclude=("code",)) or column(headers, "authorit", exclude=("code",)))
        date_col = column(headers, "date") or column(headers, "period") or column(headers, "time") or column(headers, "month")
        wide = {n: column(headers, f"rent_{n}bed") for n in (1, 2, 3)}
        if not (code_col and date_col):
            raise SystemExit(f"{path}: expected a date column and an area code column in the rents file; found {headers}")
        if all(wide.values()):
            for row in reader:
                rows += 1
                gss = (row.get(code_col) or "").strip().upper()
                when = parse_month(row.get(date_col))
                if not gss or when is None:
                    continue
                current = out.get(gss)
                if current is not None and current.rent_date > when:
                    continue
                rents = {n: to_decimal(row.get(col)) for n, col in wide.items()}
                out[gss] = RentRow(gss, (row.get(name_col) or "").strip() if name_col else "", when,
                                   {n: v for n, v in rents.items() if v is not None})
            return out, describe(path, rows, is_sample(headers))

        bed_col = column(headers, "bedroom") or column(headers, "property type") or column(headers, "type")
        rent_col = (column(headers, "average rent") or column(headers, "rent", exclude=("index", "change", "code", "name", "date"))
                    or column(headers, "price", exclude=("index", "change")))
        if not (bed_col and rent_col):
            raise SystemExit(f"{path}: expected a bedroom category column and a rent column (or rent_1bed, rent_2bed, "
                             f"rent_3bed); found {headers}")
        for row in reader:
            rows += 1
            gss = (row.get(code_col) or "").strip().upper()
            when = parse_month(row.get(date_col))
            beds = bedrooms_of(row.get(bed_col))
            rent = to_decimal(row.get(rent_col))
            if not gss or when is None or beds not in (1, 2, 3) or rent is None:
                continue
            current = out.get(gss)
            if current is None or current.rent_date < when:
                current = RentRow(gss, (row.get(name_col) or "").strip() if name_col else "", when)
                out[gss] = current
            elif current.rent_date > when:
                continue
            current.rents[beds] = rent
    return out, describe(path, rows, is_sample(headers))


def read_dwellings(path: Path) -> tuple[dict[str, int], SourceFile]:
    out: dict[str, int] = {}
    rows = 0
    with open_csv(path) as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        dcol = column(headers, "district") or column(headers, "outward") or column(headers, "postcode")
        ncol = column(headers, "dwellings") or column(headers, "count") or column(headers, "points")
        if not (dcol and ncol):
            raise SystemExit(f"{path}: expected the columns district and dwellings; found {headers}")
        for row in reader:
            rows += 1
            district = outward_code(row.get(dcol, ""))
            n = to_int(row.get(ncol))
            if OUTWARD_RE.match(district) and n is not None and n >= 0:
                out[district] = n
    return out, describe(path, rows, is_sample(headers))


def read_licensing(path: Path) -> tuple[dict[str, tuple[str, str]], SourceFile]:
    from app.models import LICENSING_SCHEMES
    out: dict[str, tuple[str, str]] = {}
    rows = 0
    with open_csv(path) as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        dcol = column(headers, "district")
        scol = column(headers, "licensing", exclude=("note",))
        ncol = column(headers, "note")
        if not (dcol and scol):
            raise SystemExit(f"{path}: expected the columns district, licensing and licensing_note; found {headers}")
        for row in reader:
            rows += 1
            district = outward_code(row.get(dcol, ""))
            scheme = (row.get(scol) or "").strip().lower().replace(" ", "_").replace("+", "_")
            if not OUTWARD_RE.match(district):
                continue
            if scheme not in LICENSING_SCHEMES:
                raise SystemExit(f"{path}: {district} has licensing {scheme!r}; use one of {', '.join(LICENSING_SCHEMES)}")
            out[district] = (scheme, (row.get(ncol) or "").strip() if ncol else "")
    return out, describe(path, rows, is_sample(headers))


def read_la_names(path: Path) -> tuple[dict[str, str], SourceFile]:
    """The ONSPD lookup 'LA_UA names and codes': a code column ending CD and a name column ending NM."""
    out: dict[str, str] = {}
    rows = 0
    with open_csv(path) as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        ccol = next((h for h in headers if h.upper().endswith("CD")), None) or column(headers, "code")
        ncol = next((h for h in headers if h.upper().endswith("NM")), None) or column(headers, "name")
        if not (ccol and ncol):
            raise SystemExit(f"{path}: expected a ...CD code column and a ...NM name column; found {headers}")
        for row in reader:
            rows += 1
            code = (row.get(ccol) or "").strip().upper()
            if code:
                out[code] = (row.get(ncol) or "").strip()
    return out, describe(path, rows, is_sample(headers))


# ---- the territories file ------------------------------------------------------------------------------------
def read_territories(path: Path) -> list[dict]:
    from app.models import DISTRICT_STATES
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("territories", data) if isinstance(data, dict) else data
    out: list[dict] = []
    for entry in entries:
        state = str(entry.get("state", "")).strip().lower()
        if state not in DISTRICT_STATES:
            raise SystemExit(f"{path}: {entry.get('area')!r} has state {state!r}; use one of {', '.join(DISTRICT_STATES)}")
        partner = entry.get("partner_id")
        if partner is not None and (not isinstance(partner, int) or partner <= 0):
            raise SystemExit(f"{path}: {entry.get('area')!r} has a partner_id that is not a positive whole number")
        until_raw = entry.get("reserved_until")
        until = date.fromisoformat(until_raw) if until_raw else None
        codes = [str(c).strip().upper() for c in entry.get("districts", []) if str(c).strip()]
        out.append({"area": entry.get("area", ""), "name": entry.get("name", ""), "state": state, "partner_id": partner,
                    "reserved_until": until, "districts": codes, "note": entry.get("note", "")})
    return out


def expand_territory(codes: list[str], loaded: set[str]) -> list[str]:
    """'E' means every loaded district whose area is E; 'SA1' means itself (when loaded)."""
    out: list[str] = []
    for code in codes:
        if code.isalpha():
            out.extend(sorted(d for d in loaded if tr.postcode_area(d) == code))
        elif code in loaded:
            out.append(code)
    return out


# ---- building the rows -----------------------------------------------------------------------------------
def nation_for(district: str, geo: DistrictGeo | None, la_gss: str | None) -> str:
    if geo is not None and geo.ctry_counts:
        code = geo.ctry_counts.most_common(1)[0][0]
        if code in CTRY:
            return CTRY[code]
    if la_gss and la_gss[0] in GSS_NATION:
        return GSS_NATION[la_gss[0]]
    area = tr.postcode_area(district)
    if area in SCOTLAND_AREAS:
        return "scotland"
    if area in NI_AREAS:
        return "northern_ireland"
    if area in WALES_AREAS:
        return "wales"
    return "england"


def build_rows(geo: dict[str, DistrictGeo], census: dict[str, CensusRow], rents: dict[str, RentRow],
               dwellings: dict[str, int], licensing: dict[str, tuple[str, str]], la_names: dict[str, str],
               assumptions: dict[str, Any], territories: list[dict] | None = None) -> list[dict]:
    """Every column of postcode_districts the loader owns, for every district in the ONSPD extract, plus the
    modelled extras (upfront_fee, monthly_fee, break_even_units) for the summary.

    The area fixed costs (portals, ads, letters) and the one systems fee are apportioned once per territory,
    not charged in full to every district: each district carries units_y1 over its territory's units_y1 of
    them (territory_rules.territory_shares; the territory is the entry in data/current_territories.json that
    lists the district, or the postcode area when none does), and that share is stored as cost_share. Summing
    partner_net_y1 or hq_contribution_y1 across a territory therefore counts the fixed costs and the fee once."""
    la_postcodes: Counter = Counter()
    for g in geo.values():
        if g.la_gss:
            la_postcodes[g.la_gss] += g.postcodes
    inputs: dict[str, dict] = {}
    for district in sorted(geo):
        g = geo[district]
        gss = g.la_gss
        cen = census.get(gss) if gss else None
        rent = rents.get(gss) if gss else None
        la_name = (cen.la_name if cen and cen.la_name else "") or (rent.la_name if rent and rent.la_name else "") \
            or (la_names.get(gss, "") if gss else "")
        dw = dwellings.get(district)
        prs_households: int | None = None
        prs_basis = ""
        if cen is not None:
            if dw is not None:
                prs_households = int((Decimal(dw) * cen.prs_share).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                prs_basis = "dwellings"
            elif gss and la_postcodes[gss]:
                prs_households = int((Decimal(cen.prs_households) * g.postcodes / la_postcodes[gss])
                                     .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                prs_basis = "postcodes"
        inputs[district] = {"g": g, "gss": gss, "cen": cen, "rent": rent, "la_name": la_name, "dw": dw,
                            "prs_households": prs_households, "prs_basis": prs_basis,
                            "units_y1": tr.units(prs_households, assumptions["capture_y1"]), "area": tr.postcode_area(district)}
    shares = tr.territory_shares(inputs, territories or [])
    rows: list[dict] = []
    for district, inp in inputs.items():
        g, gss, cen, rent = inp["g"], inp["gss"], inp["cen"], inp["rent"]
        rent_2bed = rent.rents.get(2) if rent else None
        share = shares.get(district, Decimal(1))
        model = tr.model_district(district=district, la_name=inp["la_name"], prs_households=inp["prs_households"], rent_2bed=rent_2bed,
                                  la_gss=gss, assumptions=assumptions, fixed_share=share, systems_fee_share=share)
        lic = licensing.get(district)
        row = {
            "district": district, "area": inp["area"], "nation": nation_for(district, g, gss),
            "la_gss": gss, "la_name": inp["la_name"][:120], "dwellings": inp["dw"], "prs_households": inp["prs_households"],
            "prs_share": cen.prs_share if cen else None,
            "rent_1bed": (rent.rents.get(1).quantize(PENCE) if rent and rent.rents.get(1) is not None else None),
            "rent_2bed": (rent_2bed.quantize(PENCE) if rent_2bed is not None else None),
            "rent_3bed": (rent.rents.get(3).quantize(PENCE) if rent and rent.rents.get(3) is not None else None),
            "rent_date": rent.rent_date if rent else None,
            "licensing": lic[0] if lic else None, "licensing_note": lic[1] if lic else None,
            "band": model["band"], "achievable_fee": model["achievable_fee"], "cost_to_serve": model["cost_to_serve"],
            "units_y1": model["units_y1"], "units_y2": model["units_y2"], "units_y3": model["units_y3"],
            "partner_net_y1": model["partner_net_y1"], "hq_contribution_y1": model["hq_contribution_y1"], "cost_share": share,
            # not stored: for the summary
            "upfront_fee": model["upfront_fee"], "monthly_fee": model["monthly_fee"], "break_even_units": model["break_even_units"],
            "centroid": g.centroid, "postcodes": g.postcodes, "prs_basis": inp["prs_basis"],
        }
        rows.append(row)
    return rows


STORED_COLUMNS = ("area", "nation", "la_gss", "la_name", "dwellings", "prs_households", "prs_share", "rent_1bed", "rent_2bed",
                  "rent_3bed", "rent_date", "band", "achievable_fee", "cost_to_serve", "units_y1", "units_y2", "units_y3",
                  "partner_net_y1", "hq_contribution_y1", "cost_share")


def apply_rows(db, rows: list[dict], run) -> tuple[dict[str, int], set[str]]:
    """Upsert postcode_districts. An existing row keeps state, partner_id and reserved_until, and keeps its licensing
    unless the licensing file has a line for it. Returns the counts and the set of districts inserted this run."""
    from app.models import PostcodeDistrict
    existing = {d.district: d for d in db.query(PostcodeDistrict).all()}
    counts = {"inserted": 0, "updated": 0}
    inserted: set[str] = set()
    for row in rows:
        current = existing.get(row["district"])
        if current is None:
            current = PostcodeDistrict(district=row["district"], state="open", licensing=row["licensing"] or "unknown",
                                       licensing_note=row["licensing_note"] or "")
            db.add(current)
            counts["inserted"] += 1
            inserted.add(row["district"])
        else:
            counts["updated"] += 1
            if row["licensing"] is not None:
                current.licensing = row["licensing"]
                current.licensing_note = row["licensing_note"] or ""
        for col in STORED_COLUMNS:
            setattr(current, col, row[col])
        current.model_run = run
    return counts, inserted


def seed_states(db, territories: list[dict], loaded: set[str], eligible: set[str] | None = None) -> dict[str, Any]:
    """Apply data/current_territories.json. By default only to the districts inserted this run (eligible), so a
    holding released or changed on the Territories screen is never put back by a load; with --reseed-states
    (eligible None) to every district still open with no partner. Returns what it did, per area."""
    from app.models import PostcodeDistrict
    report: dict[str, Any] = {}
    for entry in territories:
        wanted = expand_territory(entry["districts"], loaded)
        missing = [c for c in entry["districts"] if not c.isalpha() and c not in loaded]
        seeded: list[str] = []
        kept: list[str] = []
        for code in wanted:
            district = db.get(PostcodeDistrict, code)
            if district is None:
                continue
            if district.state != "open" or district.partner_id is not None or (eligible is not None and code not in eligible):
                kept.append(code)
                continue
            district.state = entry["state"]
            district.partner_id = entry["partner_id"]
            district.reserved_until = entry["reserved_until"] if entry["state"] == "reserved" else None
            seeded.append(code)
        report[entry["area"] or entry["name"]] = {"state": entry["state"], "partner_id": entry["partner_id"], "seeded": seeded,
                                                  "kept": kept, "missing": missing}
    return report


# ---- outputs ----------------------------------------------------------------------------------------------
def write_centroids(path: Path, rows: list[dict], sources: list[SourceFile], sample: bool) -> int:
    districts = {r["district"]: list(r["centroid"]) for r in rows if r["centroid"] is not None}
    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": next((s.name for s in sources if "onspd" in s.name.lower()), sources[0].name if sources else ""),
        "sample": sample,
        "note": ("Mean of the live postcodes with a coordinate in the ONS Postcode Directory extract, per district. "
                 "SAMPLE: hand-made stand-ins, not real centroids." if sample else
                 "Mean of the live postcodes with a coordinate in the ONS Postcode Directory extract, per district. "
                 "Contains OS data (c) Crown copyright and database right; contains Royal Mail data (c) Royal Mail copyright "
                 "and database right; source: Office for National Statistics licensed under the Open Government Licence v3.0."),
        "districts": districts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # One district per line, so a re-run after a new ONSPD release gives a readable diff in git.
    head = {k: v for k, v in payload.items() if k != "districts"}
    lines = [json.dumps(head, sort_keys=True)[:-1] + ', "districts": {']
    for i, code in enumerate(sorted(districts)):
        lines.append(f'  "{code}": {json.dumps(districts[code])}' + ("," if i < len(districts) - 1 else ""))
    lines.append("}}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(districts)


def money(value) -> str:
    if value is None:
        return "-"
    return f"{Decimal(str(value)):,.0f}"


def summary_lines(rows: list[dict], state_counts: dict[str, int] | None = None) -> list[str]:
    lines: list[str] = []
    by_band: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_band[row["band"]].append(row)
    lines.append(f"{'band':<11} {'districts':>9} {'priced':>6} {'prs hh':>9} {'units y1':>8} {'fee':>7} {'upfront':>15} "
                 f"{'monthly':>7} {'partner y1':>12} {'hq y1':>12}")
    for band in tr.BANDS:
        group = by_band.get(band, [])
        if not group:
            continue
        priced = [r for r in group if r["achievable_fee"] is not None]
        prs = sum(r["prs_households"] or 0 for r in group)
        units = sum(r["units_y1"] or 0 for r in group)
        fees = [r["achievable_fee"] for r in priced]
        mean_fee = (sum(fees) / len(fees)) if fees else None
        upfronts = [r["upfront_fee"] for r in priced]
        partner = sum((r["partner_net_y1"] or 0) for r in priced)
        hq = sum((r["hq_contribution_y1"] or 0) for r in priced)
        upfront = f"{money(min(upfronts))} to {money(max(upfronts))}" if upfronts else "-"
        monthly = money(group[0]["monthly_fee"])
        lines.append(f"{band:<11} {len(group):>9} {len(priced):>6} {prs:>9,} {units:>8,} {money(mean_fee):>7} {upfront:>15} "
                     f"{monthly:>7} {money(partner):>12} {money(hq):>12}")
    unpriced = [r["district"] for r in rows if r["achievable_fee"] is None]
    if unpriced:
        shown = ", ".join(unpriced[:12]) + (f" and {len(unpriced) - 12} more" if len(unpriced) > 12 else "")
        lines.append(f"no rent on file (unpriced): {shown}")
    no_prs = [r["district"] for r in rows if r["prs_households"] is None]
    if no_prs:
        shown = ", ".join(no_prs[:12]) + (f" and {len(no_prs) - 12} more" if len(no_prs) > 12 else "")
        lines.append(f"no census figure (rural by default): {shown}")
    by_postcodes = sum(1 for r in rows if r["prs_basis"] == "postcodes")
    if by_postcodes:
        lines.append(f"{by_postcodes} district{'s' if by_postcodes != 1 else ''} apportioned by live postcodes (no dwellings figure)")
    if state_counts:
        lines.append("states: " + ", ".join(f"{state} {n}" for state, n in sorted(state_counts.items())))
    lines.append("partner y1 and hq y1 carry each territory's fixed costs and systems fee once, apportioned by units_y1 "
                 f"({tr.FIXED_COST_BASIS}); a district in no territory stands for its whole postcode area")
    lines.append("every modelled figure is a proposal [CONFIRM]; see docs/territory-model.md")
    return lines


# ---- the run ---------------------------------------------------------------------------------------------
def _path(raw: str | None, sample: bool, key: str, required: bool) -> Path | None:
    if raw:
        path = Path(raw).expanduser()
    elif sample:
        path = SAMPLE_DIR / SAMPLE_FILES[key]
    else:
        path = DATA_DIR / DEFAULT_FILES[key]
    if not path.exists():
        if required or raw:
            raise SystemExit(f"{key}: {path} does not exist. data/README.md says where to get it.")
        return None
    return path


def run(args: argparse.Namespace, out=sys.stdout) -> dict:
    """The whole load. Returns a report dict (the tests read it); prints the summary."""
    from app.db import SessionLocal
    from app.models import ModelRun, PostcodeDistrict

    onspd_path = _path(args.onspd, args.sample, "onspd", required=True)
    census_path = _path(args.census, args.sample, "census", required=True)
    rents_path = _path(args.rents, args.sample, "rents", required=True)
    dwellings_path = _path(args.dwellings, args.sample, "dwellings", required=False)
    licensing_path = _path(args.licensing, args.sample, "licensing", required=False)
    assert onspd_path and census_path and rents_path

    sources: list[SourceFile] = []
    geo, src = read_onspd(onspd_path)
    sources.append(src)
    census, src = read_census(census_path)
    sources.append(src)
    rents, src = read_rents(rents_path)
    sources.append(src)
    dwellings: dict[str, int] = {}
    if dwellings_path:
        dwellings, src = read_dwellings(dwellings_path)
        sources.append(src)
    licensing: dict[str, tuple[str, str]] = {}
    if licensing_path:
        licensing, src = read_licensing(licensing_path)
        sources.append(src)
    la_names: dict[str, str] = {}
    if args.la_names:
        la_names, src = read_la_names(Path(args.la_names).expanduser())
        sources.append(src)
    # The territories file groups districts for the fixed-cost apportionment whenever it exists; whether it also
    # seeds holdings is governed by --no-seed-states and --reseed-states.
    territories: list[dict] = []
    tpath = Path(args.territories).expanduser() if args.territories else TERRITORIES_FILE
    if tpath.exists():
        territories = read_territories(tpath)
    elif args.territories:
        raise SystemExit(f"territories: {tpath} does not exist")
    sample = bool(args.sample) or any(s.sample for s in sources)
    if not geo:
        raise SystemExit(f"{onspd_path}: no live postcode with a valid outward code was found")

    db = SessionLocal()
    try:
        tr.ensure_assumptions(db)
        assumptions = tr.load_assumptions(db)
        rows = build_rows(geo, census, rents, dwellings, licensing, la_names, assumptions, territories)
        snapshot = {**_jsonable(assumptions), "fixed_cost_basis": tr.FIXED_COST_BASIS,
                    "territories_file": str(tpath.relative_to(ROOT)) if tpath.exists() and tpath.is_relative_to(ROOT) else (str(tpath) if tpath.exists() else "")}
        model_run = ModelRun(source_files=[s.as_json() for s in sources], assumptions=snapshot, districts=len(rows))
        db.add(model_run)
        counts, inserted = apply_rows(db, rows, model_run)
        db.flush()
        seed_report = (seed_states(db, territories, {r["district"] for r in rows}, None if args.reseed_states else inserted)
                       if territories and not args.no_seed_states else {})
        db.flush()
        state_counts = dict(Counter(s for (s,) in db.query(PostcodeDistrict.state).all()))
        if args.dry_run:
            db.rollback()
            run_id = None
        else:
            db.commit()
            run_id = model_run.id
    finally:
        db.close()

    centroids_written = 0
    centroids_path = Path(args.centroids_out).expanduser() if args.centroids_out else (SAMPLE_CENTROIDS_FILE if args.sample else CENTROIDS_FILE)
    if not args.dry_run:
        centroids_written = write_centroids(centroids_path, rows, sources, sample)

    las_without_census = sorted({g.la_gss for g in geo.values() if g.la_gss and g.la_gss not in census})
    las_without_rents = sorted({g.la_gss for g in geo.values() if g.la_gss and g.la_gss not in rents})

    print(("DRY RUN: " if args.dry_run else "") + f"{len(rows)} districts from {len(geo)} in the extract; "
          f"{counts['inserted']} inserted, {counts['updated']} updated" + (f"; model run #{run_id}" if run_id else ""), file=out)
    for s in sources:
        print(f"  {s.name}: {s.rows:,} rows, sha256 {s.sha256[:12]}{'  SAMPLE' if s.sample else ''}", file=out)
    if las_without_census:
        print(f"  authorities with no census row: {', '.join(las_without_census)}", file=out)
    if las_without_rents:
        print(f"  authorities with no rent: {', '.join(las_without_rents)}", file=out)
    for area, rep in seed_report.items():
        print(f"  {area}: {rep['state']}" + (f" (partner {rep['partner_id']})" if rep["partner_id"] else "") +
              f": {len(rep['seeded'])} seeded, {len(rep['kept'])} already held" +
              (f", not in the extract: {', '.join(rep['missing'])}" if rep["missing"] else ""), file=out)
    if not args.dry_run:
        print(f"  centroids: {centroids_written} written to {centroids_path}"
              + ("" if args.sample else " (commit this file so the map on Render has it)"), file=out)
    if sample:
        print("  SAMPLE DATA: nothing above is a fact about a district", file=out)
    for line in summary_lines(rows, state_counts):
        print(line, file=out)
    return {"run_id": run_id, "rows": rows, "sources": sources, "counts": counts, "seed": seed_report, "sample": sample,
            "state_counts": state_counts, "centroids_path": centroids_path, "centroids_written": centroids_written,
            "las_without_census": las_without_census, "las_without_rents": las_without_rents}


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python scripts/load_territory_model.py", description="Load the territory model from public data.")
    p.add_argument("--onspd", help="ONS Postcode Directory extract (pcds, oslaua, lat, long)")
    p.add_argument("--census", help="Census 2021 TS054 by local authority")
    p.add_argument("--rents", help="ONS Price Index of Private Rents, local authority rents by bedroom")
    p.add_argument("--dwellings", help="district,dwellings from Property Predator's address points (optional)")
    p.add_argument("--licensing", help="district,licensing,licensing_note typed by hand (optional)")
    p.add_argument("--la-names", dest="la_names", help="ONSPD 'LA_UA names and codes' lookup (optional)")
    p.add_argument("--territories", help=f"who holds today's areas (default {TERRITORIES_FILE.relative_to(ROOT)})")
    p.add_argument("--no-seed-states", dest="no_seed_states", action="store_true", help="do not apply the territories file")
    p.add_argument("--reseed-states", dest="reseed_states", action="store_true",
                   help="apply the territories file to every district still open with no partner, not only to new ones")
    p.add_argument("--centroids-out", dest="centroids_out", help="where to write the district centroids JSON")
    p.add_argument("--sample", action="store_true", help="read the hand-made files under data/sample")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="compute and print, write nothing")
    return p


def main(argv: list[str] | None = None, out=sys.stdout) -> int:
    args = parser().parse_args(argv)
    run(args, out)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
