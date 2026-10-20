# data/: what the territory loader reads

`scripts/load_territory_model.py` turns three public files (plus two optional ones) into one row per postcode
district in `postcode_districts`, with every modelled column computed by `app/services/territory_rules.py`
(`docs/territory-model.md` explains the model; `docs/pricing-bands.md` the fee it implies). This page says
exactly which files to download, where to put them, what columns the loader looks for, and how to run it.

None of the real files could be downloaded from the build machine (its network policy blocks ons.gov.uk and
most other hosts), so the loader was written against the documented layouts and tested on the hand-made files
under `data/sample/`. **Nothing in `data/sample/` is a fact about a district**: every row carries a `sample`
column, the loader marks the run as sample data, and every screen says so until the real files are loaded.

## The files

Put the real files here under these names, or pass any path with the matching flag. Do not commit the real
files: the ONS Postcode Directory alone is over 100 MB. (Ask for `data/*.csv` to be added to `.gitignore`;
the sample files and `district_centroids.json` are the only data files meant to be in git.)

| File here | Flag | Source | Required |
|---|---|---|---|
| `onspd-extract.csv` | `--onspd` | ONS Postcode Directory | yes |
| `census-ts054.csv` | `--census` | Census 2021 table TS054 (tenure) by local authority | yes |
| `pipr-rents.csv` | `--rents` | ONS Price Index of Private Rents, local authority rents by bedroom | yes |
| `dwellings.csv` | `--dwellings` | Property Predator address points, counted by district | no |
| `licensing.csv` | `--licensing` | council registers and Rent Smart Wales, typed by hand | no |
| `current_territories.json` | `--territories` | who holds today's five areas (in git, edit it here) | applied by default |

### 1. ONS Postcode Directory (ONSPD)

Where: the ONS Open Geography Portal (geoportal.statistics.gov.uk), search "ONS Postcode Directory", take the
latest quarterly release (February, May, August, November). The download is a zip of about 1 GB; inside it,
`Data/ONSPD_<MON>_<YEAR>_UK.csv` has one row per postcode, live and terminated (about 2.7 million rows).

What the loader reads: `pcds` (the postcode), `oslaua` (the local authority code), `lat`, `long`, and when
present `doterm` (a terminated postcode has a value here and is skipped) and `ctry` (the nation). Any other
column is ignored, so the full file works as it is:

```sh
python scripts/load_territory_model.py --onspd ~/Downloads/ONSPD_AUG_2026_UK/Data/ONSPD_AUG_2026_UK.csv ...
```

To keep a smaller copy under `data/`, cut it down to the six columns first:

```sh
python - <<'EOF'
import csv
keep = ["pcds", "doterm", "oslaua", "lat", "long", "ctry"]
with open("ONSPD_AUG_2026_UK.csv", newline="", encoding="utf-8-sig") as src, open("data/onspd-extract.csv", "w", newline="") as out:
    reader = csv.DictReader(src)
    writer = csv.DictWriter(out, fieldnames=keep)
    writer.writeheader()
    for row in reader:
        writer.writerow({k: row[k] for k in keep})
EOF
```

How it is used: one row per district (outward code) from the live postcodes: the local authority that most of
its postcodes fall in (`la_gss`), the mean of their coordinates (the centroid the map uses) and the nation from
`ctry`. The local authority name comes from the census or rents file; the ONSPD zip also has
`Documents/LA_UA names and codes UK as at <date>.csv`, which `--la-names` reads for any authority those two
files do not name.

Licence: Open Government Licence v3.0. Contains OS data (c) Crown copyright and database right; contains Royal
Mail data (c) Royal Mail copyright and database right; contains National Statistics data (c) Crown copyright
and database right.

### 2. Census 2021 TS054, tenure by local authority

Where: ons.gov.uk, "Create a custom dataset", Census 2021, topic summary TS054 (Tenure of household), area type
"Lower tier local authorities", download as CSV. The same table is on nomis (nomisweb.co.uk) as TS054.

Layout A, the ONS custom-dataset download (what the sample uses): one row per authority and tenure category.

```
Lower tier local authorities Code,Lower tier local authorities,Tenure of household (9 categories) Code,Tenure of household (9 categories),Observation
E07000093,Test Valley,0,Owned: Owns outright,...
E07000093,Test Valley,4,Private rented: Private landlord or letting agency,...
```

The loader finds the geography code column (a header containing "authorit" or "geography" and ending "Code"),
the tenure column and "Observation" by name, so the number of categories does not matter. Total households is
every category except "Does not apply"; private rented is every category whose label contains "Private rented"
(or "Private landlord").

Layout B, typed or made in a spreadsheet, one row per authority:

```
la_gss,la_name,households,prs_households
E07000093,Test Valley,53200,8600
```

How it is used: the authority's private-rented share (`prs_share`) is apportioned to each district by its
dwellings (file 4) to give `prs_households`; without a dwellings figure it is apportioned by the district's
share of the authority's live postcodes, a cruder proxy, and the district screen says so. The band rule (a
town has at least 3,000 private-rented households) runs on that count.

TS054 covers England and Wales only. Scottish (S12...) and Northern Irish (N09...) authorities will be listed
under "authorities with no census row"; their districts band rural (or major city for Glasgow and Belfast) with
no units until a Scotland 2022 or NI 2021 tenure table is added in layout B.

A known snag: the ONSPD carries the local authorities as they are today, and 1 April 2023 created Somerset
(E06000066), North Yorkshire (E06000065), Cumberland (E06000063) and Westmorland and Furness (E06000064) from
their districts. If the census download still lists the old districts (Mendip E07000187, Sedgemoor E07000188,
Somerset West and Taunton E07000246, South Somerset E07000189, and so on), add the new codes in layout B by
summing the old rows. The loader prints "authorities with no census row" for any code it could not match.

### 3. ONS Price Index of Private Rents (PIPR), local authority rents by bedroom

Where: ons.gov.uk, the monthly "Private rent and house prices, UK" bulletin, dataset "Price Index of Private
Rents, UK: monthly price indices and rents by local authority" (an xlsx). The sheet with average rents by
bedroom category at local authority level is what is needed; save it as CSV.

Layout A, long (what the sample uses): one row per authority, month and bedroom category.

```
Date,Area code,Area name,Bedroom category,Average rent
2026-06,E07000093,Test Valley,Two bedrooms,1135.00
```

The loader matches the headers by name: a date column ("Date", "Period", "Time" or "Month"), an area code
column, a bedroom column ("Bedroom category" or "Property type") and a rent column ("Average rent", "Rent"
or "Price", not an index or a change). Dates may be `2026-06`, `2026-06-01`, `June 2026` or `Jun 2026`;
bedrooms may be "One bedroom", "1 bed" or "1". Four-or-more and "All categories" rows are ignored.

Layout B, wide, one row per authority and month:

```
area_code,area_name,date,rent_1bed,rent_2bed,rent_3bed
E07000093,Test Valley,2026-06,950,1135,1400
```

How it is used: the latest month in the file per authority becomes `rent_date`; `rent_2bed` is the typical
managed single let and gives `achievable_fee = min(150, 12% x rent_2bed)`. Rents are authority level, so every
district in an authority carries the same rent until a finer source exists. A district whose authority has no
rent is unpriced: no fee, no money columns.

### 4. Dwellings by district from Property Predator (optional)

Property Predator's `address_points` table holds one row per addressable property it has fetched, with a
postcode. Export a count per outward code from a read replica or a one-off query (read-only; never from a
request handler). On Postgres:

```sql
\copy (
  SELECT substr(pc, 1, length(pc) - 3) AS district, count(*) AS dwellings
  FROM (SELECT regexp_replace(upper(postcode), '\s', '', 'g') AS pc FROM address_points WHERE postcode IS NOT NULL) p
  WHERE length(pc) BETWEEN 5 AND 7
  GROUP BY 1 ORDER BY 1
) TO 'data/dwellings.csv' WITH CSV HEADER
```

On the SQLite copy: `SELECT substr(pc, 1, length(pc) - 3) AS district, count(*) AS dwellings FROM (SELECT
replace(upper(postcode), ' ', '') AS pc FROM address_points WHERE postcode IS NOT NULL) WHERE length(pc) BETWEEN
5 AND 7 GROUP BY 1 ORDER BY 1;` with `.mode csv` and `.headers on`.

```
district,dwellings
SA1,16000
```

Honest limit: Property Predator fetches address points cell by cell as members use its map, so a district is
only complete where the whole district has been viewed. A thin count under-states the district; leave such a
district out of the file and the loader falls back to the postcode apportionment for it.

### 5. Licensing, typed by hand (optional)

```
district,licensing,licensing_note
SA1,additional,"Additional HMO licensing covers Castle, Uplands, St Thomas and Waterfront wards from 14 February 2026. Every landlord must be registered with Rent Smart Wales."
```

`licensing` is what the council adds on top of the mandatory HMO licensing that applies everywhere in England
and Wales: one of `unknown`, `mandatory_only`, `additional`, `selective`, `selective_additional`. Rent Smart
Wales is implied by the nation and belongs in the note. Sources: each council's private sector housing pages,
the GOV.UK list of selective licensing designations, Rent Smart Wales. A district with no line keeps whatever
it has (a value typed in the app survives a load).

### 6. `current_territories.json`, who holds today's areas

Mirrors `hq/content/areas-seed.json` in the Hello New Home workspace: Swansea SA1 to SA18 as `hq`; London
(the whole areas E, EC, N, NW, SE, SW, W, WC, BR, CR, HA, IG, RM, SM, UB and the listed DA, EN, KT and TW
districts) as `core` for partner id 3; Somerset (TA, BA and the listed BS districts) as `core` for partner id 4;
Durham and Andover as `reserved` with no partner yet. The ids 3 and 4 are placeholders until the partner
records exist in Hello New Home: edit them here or on the district screen. A bare area ("E", "TA") means every
district of that area in the ONSPD extract.

The file is applied only to districts the run inserts (the first load, or a district new to the extract), so a
holding changed on the Territories screen is never put back by a load. `--reseed-states` applies it to every
district still open with no partner; `--no-seed-states` skips it.

## Running the loader

```sh
# see the screens with the hand-made sample (writes to the database named by DATABASE_URL)
python scripts/load_territory_model.py --sample

# the real files under data/, computing and printing only
python scripts/load_territory_model.py --dry-run

# the real files, for real
python scripts/load_territory_model.py
python scripts/load_territory_model.py --onspd ~/Downloads/ONSPD_AUG_2026_UK.csv --census ~/Downloads/TS054.csv --rents ~/Downloads/pipr.csv
```

Each run reads the assumptions table (seeded by migration 0003 with `confirmed = false`), computes every
district through `territory_rules.model_district()`, upserts `postcode_districts` without touching `state`,
`partner_id` or `reserved_until`, writes one `model_runs` row (file names, sha256 digests, row counts, the
assumptions snapshot) and prints a per-band summary. Run it after confirming or changing an assumption so every
district is refreshed; the district screen says when its stored figures no longer match the current assumptions.

**Centroids.** `postcode_districts` has no coordinate columns, so the loader writes the district centroids to
`data/district_centroids.json` (`data/sample/district_centroids.json` for a sample run) and the map reads that
file, preferring the real one. The loader runs on your machine against the Neon database, but the file only
reaches Render through git: **commit `data/district_centroids.json` and push after a real run**, or the map
keeps showing the sample centroids and says so.

**The export.** `/api/territories.json` carries state, district and area for every district and nothing else:
no money and no band, because the band is a row of an unconfirmed pricing table (`docs/pricing-bands.md`
section 6). It answers to a Group HQ session, or to `Authorization: Bearer <TERRITORY_EXPORT_TOKEN>` when that
environment variable is set on Render, which is how Hello New Home will proxy it for `/partners-programme`.

**Fixed costs.** The file also tells the loader which districts form one territory, so the area fixed costs
(portals, ads, letters) and the one systems fee are shared across them by year-one units (`cost_share`) rather
than charged in full to every district; `docs/territory-model.md` section 3.6 explains.

## The sample files

| File | Rows | What it stands in for |
|---|---|---|
| `sample/SAMPLE-onspd-extract.csv` | 271 | two live postcodes for each of 134 districts, plus a terminated postcode in SA1, DH1 and SE1 that the loader must skip |
| `sample/SAMPLE-census-ts054.csv` | 336 | layout A for the 42 authorities those districts fall in |
| `sample/SAMPLE-pipr-rents.csv` | 420 | layout A, two months (May and June 2026) so the latest is seen to win |
| `sample/SAMPLE-dwellings.csv` | 134 | a count per district |
| `sample/SAMPLE-licensing.csv` | 10 | a few hand-typed lines |
| `sample/district_centroids.json` | 134 | written by `--sample`; marked `"sample": true` |

The 134 districts are today's five areas (SA1 to SA18; DH1 to DH9 and DL4, DL5, DL12 to DL17; SP9 to SP11,
RG28, SO20; two districts for each whole London area plus every listed DA, EN, KT and TW district; TA1, TA2,
TA6, TA21, BA1, BA2, BA5, BA16 and the listed BS districts) plus BS1 (Bristol, a core city), DA1, EN6, KT7 and
TW15 (neighbours that are not London) so the band rule is seen to tell them apart. The authority codes and
names are real; the counts, rents, dwellings and coordinates are plausible stand-ins chosen so that SA1, DH1
and SP10 fall in the town band and DH7, SP9, SP11, RG28 and SO20 in the rural band.
