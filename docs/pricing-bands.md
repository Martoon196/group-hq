# Pricing bands

What a Hello New Home territory is sold for, by band, as the plan (Phase 4, "Pricing bands") proposes it.
Written 20 October 2026. **Every figure on this page is [CONFIRM]**: the founder has not approved any of it,
no pack, link, page or conversation may quote it, and the public pages still say "no franchise fee" until
launch day. The figures are seeded into the `assumptions` table with `confirmed = false` and badged on every
screen until they are confirmed one by one.

Sources: the plan (Phase 4); `OFFER-ARCHITECTURE.md` section 2C (the 99 systems fee) and section 3 (the shelved
10 to 20k full franchise); `NATIONAL-LOCAL-OPERATING-MODEL.md` section 4 (the partner share and the per-area
economics); `docs/territory-model.md` for how a district gets its band and its fee.

## 1. The four bands

A district's band comes from the territory model (`band_for`): London is the London postcode areas and the
listed DA, EN, KT and TW districts; a major city is one of the eleven Core Cities; a town has at least 3,000
private-rented households; everything else is rural **[CONFIRM]**. An opportunity covering several districts
takes the highest band among them **[CONFIRM]**; the district screen shows each district's own.

| Band | Upfront floor | Monthly fee | Examples under the rule |
|---|---|---|---|
| London | 15,000 | 149 | SE1, E1, BR1, DA5, EN1, KT1, TW1 |
| Major city | 10,000 | 99 | BS1 (Bristol), CF10 (Cardiff), NE1 (Newcastle) |
| Town | 6,000 | 79 | SP10 (Andover), DH1 (Durham City), BA1 (Bath), SA1 (Swansea, HQ-held) |
| Rural | 3,000 | 49 | SP9, SO20, RG28, DH7 (where the census count is under 3,000) |

All eight numbers are **[CONFIRM]** (`upfront_floor_*` and `monthly_fee_*` in `assumptions`).

## 2. The upfront fee

Plan, Phase 4: "upfront fee = 6 x modelled partner net monthly earnings at 30 units, rounded to 500, with floors
3,000 rural / 6,000 town / 10,000 major city / 15,000 London".

```
fee        = min(150, 0.12 x rent_2bed)                 the achievable Full Management fee per unit
share      = 30 x fee x 0.50                             the partner's share at 30 own instructions, before the systems fee
upfront    = max(floor[band], round_to_500(6 x share))
```

`upfront_fee(partner_net_at_30_units, multiplier, floor_by_band)` in `app/services/territory_rules.py`;
`model_district()` does the whole chain.

**The plan's worked check (pinned by `tests/test_territory_rules.py`)**: Andover at a 144 fee (12% of 1,200)
and 30 units gives a partner share of 2,160 a month, so 6 x 2,160 = 12,960, rounded to the nearest 500 is
**13,000**, inside the town band and inside the 10 to 20k that `OFFER-ARCHITECTURE.md` section 3 shelved as a
full franchise buy-in.

Two choices in that arithmetic to confirm:

- The basis is the partner's share **before** the 99 systems fee, as the plan's own figures have it (2,160,
  not 2,061). Taking the fee off first would give 6 x 2,061 = 12,366, so 12,500: about 600 less in every band
  **[CONFIRM]**.
- The basis uses the **own-instruction split of 50%**, not the 45% blend the operating model uses for the
  area tables. On the blend the Andover figure is 30 x 144 x 45% = 1,944, so 11,664, so 11,500 **[CONFIRM]**.

### Worked figures on today's rents (all [CONFIRM], all illustrative)

Rents are the ONS figures recorded in `areas-seed.json` and the operating model section 4.1; the real loader
uses the local-authority two-bed rent, which differs.

| Territory | Rent used | Fee | Share at 30 units | 6 x share | Rounded | Band floor | Upfront |
|---|---|---|---|---|---|---|---|
| Durham (DH1, County Durham 700) | 700 | 84.00 | 1,260 | 7,560 | 7,500 | 6,000 town | **7,500** |
| Durham rural district (DH7) | 700 | 84.00 | 1,260 | 7,560 | 7,500 | 3,000 rural | **7,500** |
| Andover (SP10, modelled 1,200) | 1,200 | 144.00 | 2,160 | 12,960 | 13,000 | 6,000 town | **13,000** |
| Andover (SP10, ONS two-bed 1,135) | 1,135 | 136.20 | 2,043 | 12,258 | 12,500 | 6,000 town | **12,500** |
| Somerset, Taunton (TA1, Somerset 1,016) | 1,016 | 121.92 | 1,828.80 | 10,972.80 | 11,000 | 6,000 town | **11,000** |
| Somerset, Bath (BA1, 1,885) | 1,885 | 150.00 | 2,250 | 13,500 | 13,500 | 6,000 town | **13,500** |
| Bristol (BS1, major city) | 1,500 | 150.00 | 2,250 | 13,500 | 13,500 | 10,000 major city | **13,500** |
| Cardiff (CF10, major city) | 1,100 | 132.00 | 1,980 | 11,880 | 12,000 | 10,000 major city | **12,000** |
| London (any district, 2,332) | 2,332 | 150.00 | 2,250 | 13,500 | 13,500 | 15,000 London | **15,000** |
| Swansea (SA1, 838) | 838 | 100.56 | 1,508.40 | 9,050.40 | 9,000 | 6,000 town | never offered (HQ) |

The shape to notice: because the fee is capped at 150 the formula tops out at 13,500 everywhere the cap
bites, so the London and major-city floors are what separate them from a good town, and a thin rural district
on a low rent sits at its floor. The floors, not the formula, carry the "London and rural cannot command the
same fee" decision **[CONFIRM]**.

## 3. The monthly fee

Plan, Phase 4: "monthly fee replaces the flat 99 with 49 / 79 / 99 / 149 by band". Until confirmed, the
operating model's and the offer architecture's flat 99 from month 4 stands for the founding cohort, and that is
what the territory model's `partner_net_y1` and `hq_contribution_y1` use (`systems_fee`,
`systems_fee_from_month`), whatever the band. Switching the model to the band fee is a one-line change in
`model_district()` (pass `monthly_fee(band, a)` as the systems fee) followed by a loader run; it is not done by
confirming a value, and confirming `monthly_fee_*` on its own changes the quoted monthly fee but not the two
modelled year-one figures **[CONFIRM]**.

## 4. Mentees

Plan, Phase 4: "Mentees: upfront waived, monthly waived while the mentorship is active." The Mentorship
programme's HNH partner seat already carries `fee_waived` (plan, Phase 2 catalogue); the `hnh_franchise`
programme is for a paid territory. A mentee's opportunity therefore carries `quoted_upfront_pence = 0` and
`quoted_monthly_pence = 0` with the band recorded, so the ledger shows the waiver rather than a missing quote,
and no seller commission arises on it (`docs/commission-rules.md`) **[CONFIRM]**.

## 5. Where a quote lives

A quote is copied onto the opportunity when it is made (`franchise_opportunities.band`,
`quoted_upfront_pence`, `quoted_monthly_pence`), so a later model run, a confirmed assumption or a changed
floor never silently changes a figure already given to someone. The heads of terms carry the fee schedule
from those columns. A quote made while any input is unconfirmed is a draft and says so on the pack.

## 6. Points to resolve before any pack carries a fee

1. Confirm or change each of the twelve figures above (four floors, four monthly fees, the 6x multiplier, the
   30-unit basis, the 500 rounding, the 3,000 town threshold) and the two arithmetic choices in section 2.
   Until then the band stays inside Group HQ: the public export (`/api/territories.json`) carries district,
   area and state and no band, because a band is a row of this table and a public JSON cannot carry a
   [CONFIRM] badge. If Hello New Home needs the band later, add it behind the bearer token only, after the
   threshold and the floors are confirmed. The band rule follows the postcode area, so SM7, CR3, CR6, RM4, BR8,
   UB9 and TW19 band London although they lie outside Greater London **[CONFIRM]**.
2. A fee plus a system and a brand almost certainly make the programme a franchise: solicitor to confirm the
   status and what follows (BFA code, disclosure document) before any pack is sent (plan, Phase 4,
   "Regulatory and copy").
3. Resolve the documented inconsistencies in one revision of the heads of terms: cohort 6 vs 10; loss of
   territory after 2 months vs 2 quarters; HQ reserved SA1 to SA7 vs SA1 to SA3; the 82.50 plus 75 that
   exceeds the 150 cap at London rents.
4. The public pages and `OFFER-ARCHITECTURE.md` say "no franchise fee" and change on launch day, not before.
