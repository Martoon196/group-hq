# Consent texts

Written 20 October 2026. The sentences a person confirms before personal data moves between MH196
2026 Ltd and Hello New Home 2 Ltd, each with a version. A version names one exact wording: change the
wording and the version together, never one without the other, and keep retired versions here with
their dates. Hello New Home refuses a transfer whose `consent.text_version` is missing or malformed
(letters, digits, dots and dashes only).

## 1. `hnh-landlord-v1`: the introducer confirms the landlord's agreement

Used by "Let this property with Hello New Home" in Property Predator (pipeline deal page and deal pack
page) and by "Send to Hello New Home" in the Key Properties portal. In force from 20 October 2026.

**Exact wording:**

> Hello New Home 2 Ltd will contact the landlord about letting this property. You confirm the landlord
> has agreed to be contacted.

Where it is shown:

- Property Predator `frontend/pipeline.html` and `frontend/packs.html`: the amber line above the
  landlord fields (`HNH_CONSENT`, word for word `services/hnh_lettings.CONSENT_TEXT`; a test checks
  they agree). The member taps the button once to open the form and a second time, reading "Confirm:
  send to Hello New Home", to send. There is no checkbox: the second tap is the confirmation.
- Key Properties `views/partials/hnh-panel.ejs`: the label of the required checkbox `consent`
  (`src/hnh.js` `CONSENT_TEXT`). The route refuses a send without a ticked box
  (`consent_required`).

What the person must have done first: told the landlord that Hello New Home 2 Ltd, a separate company
from the one they are using, will contact them by phone or email about letting the property, and heard
the landlord agree. A suggested sentence for the member to use with the landlord, not stored anywhere:
"Hello New Home, our sister lettings company, can find and manage a tenant for this property. Are you
happy for them to contact you about it?" [CONFIRM]

Where the record is stored:

| System | Where |
|---|---|
| Property Predator | `hnh_handoffs.consent_at` and `consent_text_version` (a resend keeps the first moment of consent, as Hello New Home does); sent as `landlord.consent_at`, `landlord.consent_text_version` and the top-level `consent {given: true, at, text_version}` |
| Key Properties | `deals.hnh_sent_at`; the `events` row of type `hnh_sent` whose note reads "landlord consent hnh-landlord-v1 recorded at <time>"; sent as `landlord.consent_at`, `landlord.consent_text_version` and the top-level `consent {given: true, at, text_version}` |
| Hello New Home | `instructions.consent_at` and `consent_text_version`, the stored `payload`, the audit row ("consent hnh-landlord-v1") and the notification email's "Consent:" line |

Contract note: Hello New Home's intake validates the top-level `consent` object and refuses a body
without it (`consent_required`). Both senders send both forms (Property Predator from 20 October
2026; before that it sent the version only inside `landlord` and every send was refused). A
resend carries a fresh `consent.at`, which Hello New Home ignores: it writes nothing on a resend,
and the sender's record keeps the first moment too, so the two records of the one consent agree.

What Hello New Home does with it: the first contact with the landlord says who made the introduction
and offers to stop. Suggested opening, `hnh-first-contact-v1` [CONFIRM]: "<Introducer name> at
<Property Predator / Key Properties UK> told us you would like to hear from Hello New Home about
letting <address>. If that is not right, reply "stop" and we will close the introduction."

## 2. `hnh-self-v1`: the investor consents for themselves on the Hello New Home form

For the free rent review form at `hellonewhome.co.uk/valuation`, which the Key Properties investor
pack links to as `/valuation?ref=kp:<deal_id>` and which anyone can reach without a reference. Not yet
built: today the form carries neither the reference nor a consent line, and its submit goes to
`POST /api/lead` as a plain lead.

**Exact wording:**

> I am the owner, or the buyer, of this property and I ask Hello New Home 2 Ltd to contact me about
> letting it. If I arrived here from a Key Properties UK or Property Predator link, that company
> (MH196 2026 Ltd) will be told that I asked for a review and how it went, and nothing else I write here.

Shown as a required checkbox directly above "Get my free rent review", keeping the existing privacy
link beneath it. Without a `ref` the second sentence still reads correctly and nothing is reported to
anyone.

Where the record is stored: a Hello New Home `instructions` row with `origin_system` `web`,
`external_ref` `web:` followed by the `ref` parameter (`web:kp:<deal_id>`; `web:<lead id>` when
there is none), `consent_at` the submission time and `consent_text_version` `hnh-self-v1`. The
`web:` prefix is required: the intake refuses an `external_ref` that another system already owns
(`external_ref_taken`), so a web instruction stored under the bare `kp:<deal_id>` would make Key
Properties' own later send of the same deal fail; with the prefix, Key Properties needs no change. Until the form creates instructions, the
`ref` goes into the lead's notes so head office can create the instruction by hand. [CONFIRM] that
reporting the review's outcome back to Key Properties is wanted; if not, drop the second sentence and
the `ref` parameter together.

## 3. `kp-sourcer-v1`: what the Key Properties sourcer is told about their own details

The sourcer is not asked to consent (the basis is legitimate interests) but is told, in one versioned
sentence, so the record shows what they were told when they sent the deal.

**Exact wording:**

> Your name and portal email go to Hello New Home as the person who introduced this property, so it
> can tell you how the introduction is going and credit it to you. Hello New Home will not use them for
> anything else.

Where it is shown: `views/partials/hnh-panel.ejs`, as the last sentence of the explanatory paragraph
above the form (that paragraph says the same today in longer words and carries no version).

Where the record is stored: appended to the `hnh_sent` event note as "sourcer notice kp-sourcer-v1".
Not sent to Hello New Home. Not yet built.

The Property Predator equivalent, `pp-introducer-v1`, for the line above the landlord fields in
`pipeline.html` and `packs.html`:

> Your account email and your brand contact name go to Hello New Home as the introducer. Hello New
> Home will not use them for anything else.

Record it in a new `hnh_handoffs.introducer_notice_version` column when the line is added [CONFIRM].

## 4. Changing a sentence

1. Choose the next version (`hnh-landlord-v2`) and add the new wording to the register below with its
   start date; close the old entry with its end date. Never edit a wording in place.
2. Change the code in the same commit: Property Predator `services/hnh_lettings.CONSENT_TEXT` and
   `CONSENT_TEXT_VERSION` plus `HNH_CONSENT` in both HTML pages (the test fails until they agree);
   Key Properties `src/hnh.js`. Hello New Home needs no change: it stores whatever version it is sent,
   and this file is the record of what each version said.
3. Consent recorded against an earlier version stays valid for that wording. A change that alters the
   meaning (a new purpose, a new recipient) needs fresh consent before the landlord is contacted again
   [CONFIRM]; a change of phrasing does not.

## 5. Register of versions

| Version | In force | Status |
|---|---|---|
| `hnh-landlord-v1` | 20 October 2026 to date | in code (Property Predator, Key Properties) |
| `hnh-self-v1` | not yet | proposed; Hello New Home valuation form |
| `kp-sourcer-v1` | not yet | proposed; Key Properties panel |
| `pp-introducer-v1` | not yet | proposed; Property Predator pages |
| `hnh-first-contact-v1` | not yet | proposed; Hello New Home first contact, not a consent |
