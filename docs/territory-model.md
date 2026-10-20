# The territory model

How Group HQ turns public data about a postcode district into a view of what a Hello New Home territory
there could earn, for the partner and for head office, and who holds it. Written 20 October 2026 for the
plan's Phase 4. Every figure the model produces is a proposal until the founder confirms the inputs behind
it: the screens show a **[CONFIRM]** badge on every unconfirmed input and on every number derived from one.
Partners never see any of this; Group HQ is admin-only, and the only public output is the state of each
district (open, reserved, taken) for the partners page.

Where it lives:

| Piece | Where |
|---|---|
| Tables | `postcode_districts`, `model_runs`, `assumptions` (migration `alembic/versions/0003_franchise.py`, models in `app/models.py`) |
| Rules | `app/services/territory_rules.py`: pure functions, no database, no clock; `model_district()` computes every modelled column from the public inputs |
| Loader | `scripts/load_territory_model.py` reads the CSVs in `data/` and writes one `model_runs` row per run |
| Sample data | `data/sample/` covers the districts of today's five areas with clearly marked sample values, because ons.gov.uk cannot be reached from the build machine; `data/README.md` says which files to download and where to put them |
| Tests | `tests/test_territory_rules.py` pins the plan's Andover check and the operating model's section 4 tables |
| Related | `docs/pricing-bands.md` (the fee a territory is sold for), `docs/commission-rules.md` (what a seller earns) |

Sources: the plan (Phase 4, "Territory model" and "Pricing bands"); `NATIONAL-LOCAL-OPERATING-MODEL.md`
section 2 (territory model) and section 4 (economics per area at 10, 30 and 60 units); `OFFER-ARCHITECTURE.md`
section 2C (the Local Partner programme); `PARTNER-PROSPECTUS.md` section 3 and 8; `hq/content/areas-seed.json`
for today's five areas. Each function's docstring quotes the paragraph it comes from.

## 1. The unit is the postcode district

Operating model section 2.1: "A territory is a set of whole postcode districts (DH1, SP10, SE15). Never a town
name, a radius or a ward: districts are what the CRM routes on, what the portals search on, and what the Land
Registry data carries. A district is held by exactly one of: HQ, one Area Partner, or nobody (open)."

`postcode_districts.state` carries that holding:

| State | Meaning (operating model section 2.1 and 2.3) | Columns |
|---|---|---|
| `open` | Nobody holds it. Leads route to HQ. The default. | |
| `reserved` | First refusal for 12 months; converts to core at 3 live instructions **[CONFIRM]** | `partner_id`, `reserved_until` |
| `core` | Exclusive; the partner must serve it | `partner_id` |
| `hq` | Never offered: SA1 to SA7 permanently **[CONFIRM]**; SA8 to SA18 are HQ-served today and releasable **[CONFIRM]** | |

`partner_id` is the Hello New Home partner id (`people.hnh_partner_id`), not a foreign key: Group HQ never reads
another app's tables. Reserve and Assign are meant to call HNH `POST /api/partner/partners` (plan, Phase 4);
the call is not wired yet, the screen says so, and the partner's area must be set in Hello New Home HQ as well.

Today's five areas, from `areas-seed.json`, and how the loader expands them:

| Area | Districts | Note |
|---|---|---|
| Swansea (HQ) | SA1 to SA18 | SA1 to SA7 load as `hq`; SA8 to SA18 as `hq` too until the founder releases them **[CONFIRM]** |
| Durham | DH1 to DH9, DL4, DL5, DL12 to DL17 | Core at launch DH1, DH2, DH3, DH7; reserved DH4, DH5, DH6, DH8, DH9, DL14 to DL17 (operating model section 2.4, proposal) |
| Andover | SP10, SP11, SP9, RG28, SO20 | Core SP10, SP11; reserved SP9, RG28, SO20 |
| London | the whole areas E, EC, N, NW, SE, SW, W, WC, BR, CR, HA, IG, RM, SM, UB, plus DA5 to DA8, DA14 to DA18, EN1 to EN5, KT1 to KT6, KT9, TW1 to TW14 | One partner for now; whole areas are expanded to every district in the ONS Postcode Directory |
| Somerset | TA and BA (whole areas), BS20 to BS29, BS39 to BS41, BS48, BS49 | Local Partner Martin Howard |

The heads of terms and the prospectus still say HQ keeps "likely SA1 to SA3"; the operating model says SA1 to
SA7. The loader follows the operating model; the inconsistency is one of the four the plan asks to resolve in
one revision of the heads of terms **[CONFIRM]**.

## 2. The public data behind a district

| Column | Source | How it gets there |
|---|---|---|
| `district`, `area`, `nation` | the district code itself | `area` is the letters (SA, WC); `nation` from the postcode area rule in `docs/shared-keys.md` (PP `services/jurisdiction.py` is the reference) |
| `la_gss`, `la_name` | ONS Postcode Directory (ONSPD), the `oslaua` column, with the local authority names lookup | the majority local authority of the district's live postcodes |
| `dwellings` | a one-off export of PP `address_points` counted by outward code, read-only | PP never queried live; a CSV of `district,dwellings` |
| `prs_households`, `prs_share` | Census 2021 table TS054 (tenure) at local authority level | the authority's private-rented share, apportioned to the district by `dwellings`; `prs_share` is the authority's share, `prs_households` the district's apportioned count |
| `rent_1bed`, `rent_2bed`, `rent_3bed`, `rent_date` | ONS Price Index of Private Rents (PIPR), local authority level, by bedroom count | the latest month in the file; `rent_date` is that month |
| `licensing`, `licensing_note` | typed by hand from council registers and Rent Smart Wales | `licensing` is one of `unknown`, `mandatory_only`, `additional`, `selective`, `selective_additional` (what the council adds on top of mandatory HMO licensing); the note carries the detail and, in Wales, Rent Smart Wales |

Four honest limits. Rents are authority-level, so SA1 and SA3 carry the same rent until a finer source
exists. The PRS count is apportioned by dwellings, so a student district and a suburb with the same number of
homes get the same count. TS054 covers England and Wales only: Scottish (S12...) and Northern Irish (N09...)
authorities will be listed under "authorities with no census row"; their districts band rural (or major city for
Glasgow and Belfast) with no units until a Scotland 2022 or NI 2021 tenure table is added in layout B. And none
of the three files could be downloaded from the build machine (the network policy blocks ons.gov.uk and most
hosts), so the loader is written against the documented CSV layouts and the sample under `data/sample/` is
hand-made and marked as such in every row. Nothing in the sample is a fact about a district.

## 3. The model

All of it is in `app/services/territory_rules.py`. The inputs are the `assumptions` table (section 5), read
through `load_assumptions()`; the defaults below are what migration 0003 seeds, every one with
`confirmed = false`.

### 3.1 The band

Plan, Phase 4: "band london|major_city|town|rural (London postcode areas; core-cities list; prs_households >=
3,000 is town; else rural)".

1. **london**: the postcode areas E, EC, N, NW, SE, SW, W, WC, BR, CR, HA, IG, RM, SM and UB, plus the London
   districts of DA (DA5 to DA8, DA14 to DA18), EN (EN1 to EN5), KT (KT1 to KT6, KT9) and TW (TW1 to TW14), as
   the operating model section 2.4 lists London. DA1, EN6, KT7 and TW15 are not London.
2. **major_city**: the local authority is one of the Core Cities (Belfast, Birmingham, Bristol, Cardiff,
   Glasgow, Leeds, Liverpool, Manchester, Newcastle upon Tyne, Nottingham, Sheffield), matched on the ONS code
   first and the name second.
3. **town**: `prs_households >= 3000` (`town_min_prs_households`) **[CONFIRM]**.
4. **rural**: everything else, including a district with no census figure yet.

Under this rule Swansea, Durham, Bath and Andover are all town or rural. Swansea is not a Core City, so SA1
prices as a town **[CONFIRM]**. `band_for(district, la_name, prs_households, la_gss=...)`.

### 3.2 The fee per managed unit

Plan, Phase 4: "achievable_fee = min(150, 0.12 x rent_2bed)". Operating model section 4.1: "Full Management fee
per single let (12%, cap 150) | 84 | 144 | 150 (capped) | At London rents the cap bites on almost every
property." The 12% and the 150 cap are the fee sheet confirmed on 19 October 2026 (a fact); using the two-bed
rent as the typical managed single let is the proposal **[CONFIRM]**. With no rent on file the district has no
fee and no money columns. `achievable_fee(rent_2bed, cap, pct)`: 1,200 gives 144.00, 2,332 gives 150.00, 700
gives 84.00.

### 3.3 Units

Plan, Phase 4: "units_y1..y3 = prs_households x capture with capture 0.3% / 0.7% / 1.2% [CONFIRM]". The number
of managed single lets at the end of each year, rounded half up. 3,500 private-rented households give 11, 25
and 42. `units(prs, capture)`.

Inside year one the count ramps linearly from zero to `units_y1` (`units_in_month`), so `partner_net_y1` and
`hq_contribution_y1` are whole-year totals rather than twelve copies of the year-end month. The ramp is this
model's own assumption, not the operating model's **[CONFIRM]**.

### 3.4 The split

Operating model section 4.1: "Partner share | 45% blended | 45% | 45% | Area Partner: 50% on own instructions
(assumed 80% of units), 25% on HQ leads serviced (20%). [CONFIRM]". `blended_split(0.50, 0.25, 0.80)` is 0.45.
Prospectus section 3 and the heads of terms section 6 carry the same 50% and 25% **[CONFIRM]**.

### 3.5 What the partner takes home

`partner_net_month(units, fee, split, systems_fee, month)` is units x fee x split, less the systems and brand
fee once it applies. Offer architecture section 2C: "0 upfront for the founding cohort; 99/month systems &
brand fee from month 4"; operating model section 4.2: "A Durham Area Partner at 30 units earns about 2,044
gross before the 99 fee and running costs". Running costs (mileage, phone, print) are the partner's own and
are not modelled. `partner_net_y1` is the twelve months of year one on the ramp, blended split, systems fee from
month 4.

### 3.6 What head office keeps

`hq_contribution_month(units, fee, split, cost_to_serve, fixed_costs, systems_fee, month)` is one column of the
operating model's tables: HQ gross recurring fee, less the partner's share, less "Portals, ads, letters, less
systems fee", less "Cost to serve", gives "HQ contribution per month".

| Input | Outside London | London | Source (operating model section 4.1) |
|---|---|---|---|
| Cost to serve per unit per month | 25 | 35 | "Staff time for rent, maintenance desk, statements, compliance. London higher for trades prices and tenant expectations. Replace with actuals." **[CONFIRM]** |
| Portals per month | 550 | 550 | **[VERIFY: Rightmove's average branch spend was 1,636 a month in its H1 2026 report; virtual-branch pricing is not published]** **[CONFIRM]** |
| Ads per month at launch | 760 (25 a day), rising to 1,000 at 60 units | 1,520 (50 a day), rising to 2,000 | **[CONFIRM]** |
| Letters and lookups per month | 300 (100 letters, Land Registry 7 a title) | 300 | **[CONFIRM]** |
| Systems fee received | +99 from month 4 | +99 from month 4 | **[CONFIRM]** |

`hq_contribution_y1` is the year-one total on the ramp with the launch-phase fixed costs. It is negative for
almost every district: operating model section 4.5, "the first 90 days are planned as a proving period rather
than a profit period". One-off income (setup 295, Tenant Find 495, SA Launch 495, HMO room let-only 249) is not
in the figure. `break_even_units()` is the steady-state count at which the monthly contribution reaches zero.

Year one uses the launch ads budget throughout; the rise to 1,000 (2,000 in London) at 60 units is not modelled
**[CONFIRM]**, so a district that reaches 60 units inside year one understates ads by up to 240 (480) a month in
its later months.

**The fixed costs are per area, so the loader apportions them.** Section 4.1 gives portals, ads and letters as
costs per area and the systems fee as one fee per partner. A district is not an area: the loader groups the
districts of each territory (the entry in `data/current_territories.json` that lists them, or the postcode area
for a district no territory lists), gives each district `cost_share = units_y1 / the territory's units_y1`
(1/n when the territory has no units), and `model_district(fixed_share=, systems_fee_share=)` charges that share
of the fixed costs and of the systems fee to `partner_net_y1` and `hq_contribution_y1`. Summing either figure
across a territory therefore counts the costs once, which is what `/territories` totals, the loader's per-band
summary and the territory report rely on; a district in no territory stands for its whole area and carries the
whole figure, as the first loader did for every district. The share is stored on the row (`cost_share`) and
shown on the district screen; `break_even_units` and the upfront fee stay on the whole-area figures, because
they price a territory, not a district. `model_runs.assumptions` records `fixed_cost_basis` so a run can be
told from one made before the apportionment existed.

### 3.7 Cross-checks the tests pin

The plan's worked Andover check: a 144 fee (12% of 1,200) and 30 units give a partner share of 2,160 a month
(2,061 from month 4 after the systems fee), so an upfront fee of 13,000 (6 x 2,160 = 12,960, rounded to the
nearest 500), inside the town band's 6,000 floor. See `docs/pricing-bands.md`.

The operating model's section 4 tables, reproduced to the pound with `unit_mix_fee()` and the 45% blend:

| Area | Gross fee at 10 / 30 / 60 units | HQ contribution at 10 / 30 / 60 | Break-even (section 4) | Model |
|---|---|---|---|---|
| Durham | 1,320 / 4,542 / 10,044 | -1,134 / 237 / 2,273 | roughly 26 units | 25.93 |
| Andover | 1,872 / 6,153 / 13,170 | -830 / 1,123 / 3,993 | roughly 17 units | 17.21 |
| London | 2,370 / 8,160 / 18,060 | -1,416 / 1,167 / 5,082 | roughly 20 units | 19.82 |

Those tables mix single lets, HMOs and SA units (section 4.1: "8 single + 2 SA / 22 single + 2 HMO + 6 SA / 40
single + 4 HMO + 16 SA"). The per-district model prices single lets only, so a district's own break-even is
higher than the area's: Andover at 144 a unit breaks even at about 28 units, not 17. HMO and SA units are what
make an area pay and are not modelled per district **[CONFIRM]**.

## 4. A worked district (sample values, not facts)

SP10 with 3,500 private-rented households and a two-bed rent of 1,135 (the ONS Test Valley figure quoted in
`areas-seed.json`), on the default assumptions:

| Column | Value | How |
|---|---|---|
| `band` | town | not London, not a core city, 3,500 >= 3,000 |
| `achievable_fee` | 136.20 | 12% of 1,135, under the cap |
| `cost_to_serve` | 25.00 | outside London |
| `units_y1`, `units_y2`, `units_y3` | 11, 25, 42 | 3,500 x 0.3%, 0.7%, 1.2% |
| `partner_net_y1` | 3,521.88 | twelve months on the ramp at 45%, systems fee from month 4, as the whole territory (`fixed_share` 1) |
| `hq_contribution_y1` | -14,835.48 | the proving year: the whole area's launch fixed costs against a handful of units; in the Andover territory SP10 carries only its share of them (section 3.6) |
| break-even | 30.27 units | steady state, single lets only |
| upfront fee | 12,500 | 30 x 136.20 x 50% = 2,043 a month; x 6 = 12,258; to the nearest 500 |
| monthly fee | 79 | town band |

`model_district(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed=1135)` returns exactly
these. Every one of them carries **[CONFIRM]** on the screen because every input does.

## 5. The tables

**`postcode_districts`**: `district` (primary key, upper case outward code), `area`, `nation` (england, wales,
scotland, northern_ireland), `la_gss`, `la_name`, `dwellings`, `prs_households`, `prs_share` (0 to 1),
`rent_1bed`, `rent_2bed`, `rent_3bed`, `rent_date`, `licensing`, `licensing_note`, `band`, `achievable_fee`,
`cost_to_serve`, `units_y1`, `units_y2`, `units_y3`, `partner_net_y1`, `hq_contribution_y1`, `cost_share`
(migration `0005_district_cost_share`: the share of its territory's fixed costs and systems fee in the two
year-one figures; NULL on a row loaded before it existed, read as 1), `state`, `partner_id`, `reserved_until`,
`model_run_id`, `created_at`, `updated_at`. Every enumerated column has a check constraint; counts cannot be
negative; `prs_share` and `cost_share` are between 0 and 1.

**`model_runs`**: `id`, `ran_at`, `source_files` (JSON list of `{name, path, sha256, rows}`), `assumptions` (a
`{key: value}` snapshot of the assumptions table as used, plus `fixed_cost_basis` and `territories_file`, which
say how the fixed costs were apportioned), `districts` (rows written). Every loader run writes
one, and every district row it touches points at it, so two runs on the same files give the same rows and the
difference between two runs can be explained by the snapshot.

**`assumptions`**: `key` (primary key), `value` (JSON, a number), `note` (which paragraph it comes from),
`confirmed` (false until the founder signs it off), `confirmed_at`, `updated_at`. The 28 seeded keys:

| Key | Default | Key | Default |
|---|---|---|---|
| `capture_y1` / `capture_y2` / `capture_y3` | 0.003 / 0.007 / 0.012 | `town_min_prs_households` | 3000 |
| `fee_cap` | 150 | `upfront_units` | 30 |
| `management_pct` | 0.12 | `upfront_multiplier` | 6 |
| `cost_to_serve` / `cost_to_serve_london` | 25 / 35 | `upfront_round_to` | 500 |
| `portals` | 550 | `upfront_floor_rural` / `town` / `major_city` / `london` | 3000 / 6000 / 10000 / 15000 |
| `ads` / `ads_london` | 760 / 1520 | `monthly_fee_rural` / `town` / `major_city` / `london` | 49 / 79 / 99 / 149 |
| `letters` | 300 | `partner_split_own` / `partner_split_hq_leads` | 0.50 / 0.25 |
| `systems_fee` / `systems_fee_from_month` | 99 / 4 | `partner_own_lead_share` | 0.80 |

All 28 are **[CONFIRM]**. A value edited or confirmed in the app is never overwritten: `ensure_assumptions()`
only adds keys that are missing, and the migration seeds only once. Confirming a figure sets `confirmed` and
`confirmed_at`; the badge disappears for that input and for nothing else.

## 6. What is a fact and what is a proposal

Facts: the fee sheet of 19 October 2026 (12% capped at 150, no VAT); the district lists in `areas-seed.json`;
the public data once the real files are loaded (ONSPD, TS054, PIPR), with their dates.

Proposals, all **[CONFIRM]**: every assumption in section 5; the band rule and the 3,000 threshold; the use of
the two-bed rent as the managed rent; the linear year-one ramp; the single-let-only district model; the
apportionment of a territory's fixed costs by year-one units; HQ keeping SA1 to SA7; the pricing bands and the
seller commission in the two companion documents. The public pages and
`OFFER-ARCHITECTURE.md` still say "no franchise fee" and change on launch day, not before; a solicitor confirms
the franchise status before any pack is sent.
