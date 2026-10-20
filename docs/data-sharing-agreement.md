# Intra-group data sharing agreement (draft)

Draft of 20 October 2026 for solicitor review before Phase 3 C2 goes live; [CONFIRM] marks each
choice to confirm. No landlord data moves until this is signed and
`privacy-notice-additions.md` is published.

## 1. Parties

- **MH196 2026 Ltd** ("MH196"), company number 16447992, registered office Suite 34, Cariocca
  Business Park, 2 Sawley Road, Manchester M40 8BB, ICO registration C1953908 [CONFIRM it covers all
  three products], operating Property Predator, Key Properties UK and Ordris.
- **Hello New Home 2 Ltd** ("HNH"), company number 13752357, registered office [CONFIRM], head office
  Unit 54, First Floor, Menzies Distribution, Mill Stream Way, Central Business Park, Swansea SA7 0AG,
  ICO registration [CONFIRM], operating Hello New Home.

The parties are independent controllers; this is the written arrangement the ICO data sharing code
recommends [CONFIRM no flow below makes them joint controllers].

## 2. Purpose

Three purposes, no other:

1. introducing landlords and their properties for letting, from Property Predator and Key Properties
   to HNH, at the request of the person dealing with the property and with the landlord's agreement;
2. returning the status of each introduction to the system that sent it;
3. creating landlord, property and tenancy records in HNH's own Ordris organisation once
   instructed, so HNH manages the property there.

Neither party uses the data for anything else. Group HQ (MH196's admin app) holds emails and ids only
and receives no landlord data.

## 3. Roles

- Purposes 1 and 2: independent controllers, MH196 for the Property Predator and Key Properties
  records, HNH for the instruction once received.
- Purpose 3: HNH controls the records in its Ordris organisation; MH196, as Ordris's operator,
  processes them on HNH's instructions under the Ordris terms [CONFIRM they contain UK GDPR Article 28
  clauses; if not, a schedule here must supply them]. MH196 controls the referral record Ordris writes
  (reference and ids, no personal data).

## 4. The data

Field names are as the code sends them on 20 October 2026 (revised the same day after the
cross-repository review).

### Flow 1: Property Predator to HNH (`POST /api/partner/properties`, client `propertypredator`)

| Group | Items |
|---|---|
| Reference | `external_ref` (`pp:pipeline:<id>` or `pp:pack:<slug>`), `origin_system` `pp`, `origin_url` |
| Introducer (the member) | `account_id` and the same value as `pp_account_id`, `email` and the same value as `person_key` (the group's shared person key, the lower-cased account email), `name` (brand contact, may be blank), `email_verified` |
| Landlord | `name`, `email`, `phone`, `consent_at`, `consent_text_version`, repeated as `consent` {`given`, `at`, `text_version`} |
| Property | `address`, `postcode`, `bedrooms`, `property_type`, `furnished`, `rent_expectation`, `available_from`, `service_line` (residential, hmo, sa, commercial) |
| Free text | `notes` typed by the member, up to 2,000 characters |

HNH stores `person_key` and `pp_account_id` as the introducer keys; `account_id`, `email`,
`name` and `email_verified` are received and kept only in the stored payload.

### Flow 2: Key Properties to HNH (`POST /api/partner/properties`, client `keyproperties`)

| Group | Items |
|---|---|
| Reference | `external_ref` (`kp:<deal_id>`), `origin_system` `kp`, `origin_url` |
| Introducer (the sourcer) | `kp_user_id`, `email`, `person_key`, `name`, `pp_account_id` |
| Landlord | `name`, `email`, `phone`, `consent_at`, `consent_text_version`, repeated as `consent` {`given`, `at`, `text_version`} |
| Property | `address`, `postcode`, `bedrooms`, `property_type`, `rent_expectation`, `service_line` |
| Free text | `notes` |

The investor pack carries only a link to HNH's own form with the deal reference.

Status returned for flows 1 and 2: `GET /api/partner/properties/{external_ref}` answers with `ref`,
`status`, `status_history` (time, from, to, who, note), the area, the local partner's first name, the
Ordris ids and timestamps. "Who" is a role (`head office`, `partner`, `api:<client>`), never a HNH
staff email address. No landlord details go back.

A resend under the same `external_ref` is answered with the record HNH already holds and changes
nothing there: corrected landlord details after a successful send are passed to HNH by
reference, not by resending.

### Flow 3: HNH to Ordris (`POST /api/partner/hnh/instructions`, client `hnh`)

Sent when an instruction becomes `instructed`, into the organisation fixed by `ORDRIS_HNH_ORG_ID`.

| Group | Items |
|---|---|
| Reference | `external_ref` (the HNH reference, `HNH-000123`) |
| Landlord | `name`, `email`, `phone` |
| Property | `name`, `address`, `postcode`, `bedrooms`, `property_type` |
| Tenancy (optional, later) | `start_date`, `rent_amount`, `rent_frequency`, `deposit_amount`, `status` |

Ordris answers with `landlord_client_id`, `property_id` and `tenancy_id`, which HNH stores, and writes
a `revenue_events` row (reference, the three ids, amount 0, source `hnh`) with no personal data; a
referral (commission) record is created from it only when a commission rule for
`instruction.received` exists (`commission-rules.md`, section 4). No introducer details, fee
figures or notes reach Ordris.

## 5. Lawful basis

- **Landlord's name and contact details (flows 1 and 2):** consent, UK GDPR Article 6(1)(a), given to
  the introducer and confirmed by the introducer against consent text `hnh-landlord-v1`. HNH's first
  contact confirms it and offers to stop. [CONFIRM] relayed consent is adequate, or whether legitimate
  interests with that confirmation as safeguard is better.
- **Introducer's business contact details:** legitimate interests, Article 6(1)(f): tracking,
  reporting and attributing the introduction; assessment to be kept [CONFIRM]. No marketing
  preference is transferred by either sender (Property Predator's `users.partner_consent` and Key
  Properties hold none that travels), so HNH may not market to introducers and treats every
  introducer as not opted in. [CONFIRM] whether that stays so, or a `marketing_ok` boolean is added
  to both senders.
- **Landlord's details in Ordris (flow 3):** contract, Article 6(1)(b), HNH's management agreement
  with the landlord; processed by MH196 as HNH's processor.
- **Property facts:** personal data once linked to a named landlord, and treated as such.

## 6. Consent record

Every transfer under flows 1 and 2 carries `consent_at` and `consent_text_version`; HNH refuses one
without them. The wording is in `consent-texts.md`; wording and version change together and retired
versions are kept, dated.

## 7. Retention

- **Introduction not instructed** (new, contacted, appraised, lost): 12 months from the last status
  change, then deleted by HNH (row, payload, notification email) and Property Predator
  (`hnh_handoffs.landlord_name`; Key Properties holds none) [CONFIRM].
- **Instructed** (instructed, let): the management relationship plus 6 years, in HNH's records and its
  Ordris organisation [CONFIRM].
- **Status history and audit rows:** with the record they belong to.
- **Ordris referral record:** reference and ids only; MH196's accounting retention (7 years under
  Ordris's notice).

The deletion jobs do not exist yet; building them before the first 12-month point is a condition
[CONFIRM].

## 8. Security

- TLS on every call; server to server only, never from a browser.
- One secret per client (32 characters or more), constant-time compare, hashed at HNH;
  `X-Partner-Client` names the client; any `Origin` header is refused; unconfigured fails closed.
- Secrets are never logged, stored or shown; log lines carry references and postcodes, not
  names (HNH's notification subject and mail log, and Ordris's failed-query log, were brought
  into line on 20 October 2026).
- Daily caps: 60 new properties per client at HNH; 2,000 calls per address at Ordris.
- Local partners see structured fields for their own area only, never the raw payload or money.

## 9. Processors and sub-processors

- **Neon** (databases): Property Predator and HNH in London; Key Properties and Ordris [CONFIRM; all
  four are to be London].
- **Render** (hosting): Property Predator in Oregon (US); Key Properties and Ordris in Frankfurt; HNH
  [CONFIRM; Render's default is Oregon].
- **Clerk** (Ordris sign-in only, no landlord records): United States.
- **Postmark** (Property Predator), **SMTP or Brevo** (HNH, Key Properties): HNH's notification
  email for each instruction carries the landlord's name, email address and phone number, the
  full address and postcode, the property facts and the introducer's system, and goes to head
  office and to the active partner of the area (who also sees those fields in the partner
  portal); its subject carries only the reference, postcode and area [CONFIRM].

Either party may add a processor on 30 days' written notice [CONFIRM].

## 10. International transfers

None are intended: both parties are in the United Kingdom and every database is to sit in London. Noted:
Property Predator's application runs on Render in Oregon, so landlord details typed
into it pass through a United States server before reaching HNH; and Clerk (United States) holds
Ordris sign-in data for HNH's staff. MH196 relies on Render's and Clerk's terms with the UK
International Data Transfer Addendum [CONFIRM]; moving Property Predator to a European region would
remove the first point [CONFIRM].

## 11. Data subject rights and single point of contact

- MH196: privacy@propertypredator.com [CONFIRM one address for the three products].
- HNH: lettings@hellonewhome.co.uk, subject "Data request" [CONFIRM].

Each party answers requests about data it holds within one month and forwards one touching the
other's records within 5 working days, quoting the `external_ref`. A landlord's objection or
withdrawal of consent is passed on the same way; HNH stops contact and the sender marks the handoff
`lost`.

## 12. Personal data breaches

A party aware of a breach affecting shared data tells the other within 48 hours, at the contacts
above, with what it knows. The party whose systems were affected leads any ICO (72 hours) and
data subject notification; the other assists. Both keep a breach log.

## 13. Term, termination and deletion

- Starts on signature; ends on 30 days' written notice from either party [CONFIRM], or at once if a
  party loses control of its secrets.
- On termination both parties disable the API clients. Un-instructed introductions are deleted within
  30 days; instructed records stay with HNH under section 7; HNH's Ordris organisation continues under
  the Ordris terms, or HNH exports it and MH196 deletes it [CONFIRM].

## 14. General

- Each party is responsible for its own UK GDPR and Data Protection Act 2018 compliance, privacy
  notice and ICO registration.
- No fee is payable; commercial terms for introductions are separate [CONFIRM].
- Changes are in writing, signed by both; section 4 may change by signed schedule.
- Each party indemnifies the other for fines and claims caused by its own breach [CONFIRM scope and
  cap].

## 15. Governing law

England and Wales; its courts have exclusive jurisdiction.

## 16. Signatures

Signed for and on behalf of **MH196 2026 Ltd**

Name: Martin Howard [CONFIRM]  Title: Director  Signature: __________________  Date: __________

Signed for and on behalf of **Hello New Home 2 Ltd**

Name: Martin Howard [CONFIRM]  Title: Director  Signature: __________________  Date: __________

[CONFIRM] whether one person signing for both needs a board minute on each side.
