# Shared keys across the group

Written 20 October 2026 for Property Predator (PP), Key Properties (KP), Hello New Home (HNH), Ordris
and Group HQ. Group HQ holds the master copy; each repo carries a copy at `docs/shared-keys.md` and
changes go here first. The plan's rule stands: every app keeps its own database, nothing is shared
physically, and these keys are how records in different databases are known to be the same person,
the same place and the same product.

## 1. The person key

```
person_key = lower(trim(email))
```

- `trim` removes leading and trailing whitespace, including Unicode whitespace.
- `lower` lower-cases the whole address, local part included. Nobody in the group runs a mailbox that
  distinguishes `Amy@` from `amy@`, and PP, KP and HNH already lower-case on write.
- Nothing else: no dot-stripping, no removal of `+tags`, no punycode. `amy+pp@example.test` and
  `amy@example.test` are two people until a human links them in Group HQ.
- Comparisons are always on the normalised form. Store the normalised form; where the original
  spelling must be kept (Clerk in Ordris), compare case-insensitively.

Where it lives: Group HQ `people.person_key` (unique, case-insensitive index); PP `users.email`
(lower-cased at registration, `LOWER(email)` in every lookup, including `promo.grant_comp`); KP
`users.email`; HNH `users.email` and `partners.email` (case-insensitive unique index from Phase 0);
Ordris keeps Clerk's record as typed and the Group HQ partner endpoint (Phase 2) resolves the user by
lower-cased email through the Clerk backend API.

## 2. The shared person id: `pp_account_id`

PP's `users.id` is the group-wide person id. KP already stores it as `users.pp_account_id`; Group HQ
stores it as `people.pp_account_id` (unique, nullable) and fills it from the PP connector's
`account_id` (the comp response or `GET /api/partner/grouphq/account?email=`).

- A product never invents a `pp_account_id`. It stores one that PP or Group HQ handed it, or none.
- A person with no PP account has no `pp_account_id`; the person key is then the only key, and Group
  HQ's manual Link on the person screen is how a mismatch is resolved.
- Ordris, KP and HNH ids (`ordris_user_id`, `ordris_org_id`, `kp_user_id`, `kp_investor_id`,
  `hnh_partner_id`, `hnh_user_id`) are held in Group HQ beside the person key. Those products do not
  need to store each other's ids.

## 3. One postcode normaliser

The reference implementation is PP `backend/services/jurisdiction.py` `normalise_postcode`.

1. Upper-case the input and remove every whitespace character: `" eh3  7rf "` becomes `EH37RF`.
2. If the result is 5 characters or longer it is a full postcode: insert a single space before the
   last three characters: `EH3 7RF`.
   - outward code (district) = everything before the space = all but the last three characters: `EH3`
   - inward code = the last three characters: `7RF`
   - area = the leading letters of the outward code, one or two: `EH`
3. A shorter result (2 to 4 characters) is an outward code on its own: `EH3`, `SW1A`, `G1`. No space
   is inserted and there is no inward code.
4. Whether the result is a plausible UK postcode is a separate check each repo does as it needs
   (HNH's outward regex `^[A-Z]{1,2}\d[A-Z\d]?$`, KP's Zod schema). The normaliser never rejects.

```python
import re

def normalise_postcode(pc) -> str:
    s = re.sub(r"\s+", "", str(pc or "").upper())
    return s[:-3] + " " + s[-3:] if len(s) >= 5 else s

def outward(pc) -> str:
    return normalise_postcode(pc).split(" ")[0]
```

```js
export function normalisePostcode(pc) {
  const s = String(pc || "").toUpperCase().replace(/\s+/g, "");
  return s.length >= 5 ? s.slice(0, -3) + " " + s.slice(-3) : s;
}
export function outward(pc) { return normalisePostcode(pc).split(" ")[0]; }
```

Store the normalised full postcode where a full postcode is stored, and the outward code where the
record is about a district (HNH areas, KP `postcode_district`, Group HQ territories in Phase 4).

## 4. The nation rule

PP `backend/services/jurisdiction.py` is canonical. It routes on the postcode area:

| Areas | `nation_of_postcode` |
|---|---|
| AB DD DG EH FK G HS IV KA KW KY ML PA PH ZE | `scotland` |
| TD | `scotland-likely` (TD15 is Berwick-upon-Tweed, England) |
| BT | `northern-ireland` |
| everything else | `england-wales` |

Ownership and title data are England and Wales only; tax has three regimes (England and Northern
Ireland, Scotland, Wales); EPC registers are split three ways. None of that needs England and Wales
told apart, so PP does not do it. HNH does (region `wales` for CF, LD, LL, NP, SA) for its area pages:
that is a presentation region, and NP and LL contain English districts, so anyone needing England
against Wales at district precision asks postcodes.io `country` rather than the area lists.

Rules: mirror `jurisdiction.py` rather than writing a fifth list (PP keeps
`frontend/pp-jurisdiction.js` identical to it); when a caller must be sure about TD, ask postcodes.io
or try Scotland first and fall through. Known difference to resolve: HNH `app/services/postcodes.py`
puts TD in `SCOTLAND` outright; it should treat TD as `scotland-likely` like PP.

## 5. Tier vocabulary

| Group word | PP key (`users.subscription_tier`) | Meaning |
|---|---|---|
| `scout` | `free` | the free tier; never comped, a Scout grant is a no-op recorded locally |
| `hunter` | `pro_buyer` | the mentee gift tier (plan decision 3) |
| `apex` | `pro_investor` | Decision-Maker, owner intelligence |
| `business` | `pro_business` | Predator Business |

PP also accepts the legacy aliases `pro` (Hunter) and `enterprise` (Apex) and the `_annual` spellings,
all through `entitlements.normalise_tier`. Group HQ grant values may use either column; the PP
connector maps the group word to the PP key before sending. KP forwarding for mentees is the
separate flag `kpuk.submit` granted beside the tier (plan decision 3), not the Business tier. Ordris
plans (`starter`, `pro`, `enterprise`) and HNH partner tiers (`introducer`, `area`) are those
products' own vocabularies and are not mapped onto this one.

## 6. Strategy vocabulary

| Key | Meaning |
|---|---|
| `brr` | buy, refurbish, refinance |
| `hmo` | house in multiple occupation |
| `flip` | buy to sell |
| `r2r` | rent to rent |
| `dev` | development (conversion or new build) |

PP's appraisal engines and buy box are the source; KP deal packs and the Ordris development project
handoff (Phase 6) carry the same keys so a deal keeps its strategy as it moves between products.

## 7. How each repo applies this

| Repo | Person key | `pp_account_id` | Postcode | Nation | Tiers and strategies |
|---|---|---|---|---|---|
| Property Predator | lower-cased at registration; `LOWER(email)` in lookups | the source: `users.id` | `services/jurisdiction.normalise_postcode` is the reference | `services/jurisdiction.py` is canonical | `entitlements.normalise_tier`; strategy keys in `appraisal/engines.py` |
| Key Properties | lower-cased in `src/partner.js` intake | stored as `users.pp_account_id`; linked by email for mentees (Phase 3 C1) | `src/partner.js` `outward()`, `postcode_district` on deals | not needed (England and Wales deals) | tier sent by PP is ignored today; `category` on deal packs |
| Hello New Home | `app/auth.py` lower-cases; case-insensitive unique index (Phase 0) | not stored; Group HQ holds `hnh_partner_id` | `app/services/postcodes.normalise` returns the outward code only | `region_for` adds a Wales split; align TD with PP | none; partner tiers are HNH's own |
| Ordris | Clerk as typed; partner endpoint compares lower-cased (Phase 2) | not stored; Group HQ holds `ordris_user_id` and `ordris_org_id` | property records carry the full postcode | not needed | `org_entitlements.plan` is Ordris's own |
| Group HQ | `people.person_key` | `people.pp_account_id`, filled by the PP connector | territories by outward code (Phase 4) | reads PP's rule; never its own list | connectors map group words to product keys |
