# Mentee earnings

What every mentee gets, where their links show, and the one thing that still needs a person: giving each Hello New
Home partner a region. Written 23 October 2026 for the founder. The technical contracts are in `docs/connectors.md`.

## What each mentee gets

Every mentee on the **Mentorship** programme is enrolled in every scheme the group has, by the same recompute that
grants their access (the nightly job at 03:10, or **Recompute now** on the mentee's Grants tab):

| Scheme | How Group HQ enrols them | What they keep when the mentorship ends |
|---|---|---|
| Property Predator affiliate programme | the `pp affiliate` item: Property Predator's Group HQ route makes their account an affiliate (it needs a Property Predator account with the same email) | everything: the programme is public and membership is permanent, so ending the mentorship is recorded in Group HQ and nothing is sent |
| Ordris partner programme | the `ordris affiliate` item: Ordris finds or creates an active partner for their email (no Ordris login needed) | everything, for the same reason |
| Key Properties sourcer fee share | the existing `kp sourcer_link` item, which now also tells Key Properties their Property Predator account, so packs they forward from Property Predator land on their sourcer account | their deals stay; the sourcer login is switched off |
| Hello New Home partner seat | the existing `hnh partner_seat` item | the seat is paused; their area stays assigned until head office changes it |
| Group HQ franchise referral code | a seller record (kind mentee) is created for every active mentee who has none, with a code of their own | the seller record stays; switch it off on the seller page if it should stop |

What each scheme pays, and when, is set by that business's own published terms. Group HQ only enrols; it never sets a
rate, an attribution rule or a payout. Hello New Home has no commission figures in its system, so partner statements
stay manual. A Group HQ seller's statements stay held until the self-billing agreement is entered on their seller page,
as for every seller.

Programmes are edited in the app. The two affiliate items were added to the existing Mentorship template once, by the
0008 migration, only where the template did not already have them; an edit you make later is never undone. To stop
enrolling mentees in one of them, remove its item on **Programmes**.

## Where the links show

- **Earning links** on the mentee page (above the tabs) and on the person page. It asks each product when the page
  opens, each within four seconds and all at once, after the page itself has loaded, so a slow product never holds the
  page. Each product shows **live**, **not enrolled**, **dry run** (not asked), **not configured** or **couldn't reach**.
  When a product cannot be asked, the code and link the last recompute kept are shown as *last known*. A product is
  only asked about someone who holds or is due its scheme, or whose id there Group HQ already has: the person page of
  a prospect or an outside seller asks nobody and says "Not asked".
  - Property Predator: affiliate link and code, referred accounts, commission due and paid.
  - Ordris: referral link and code, referrals, pending, payable and paid.
  - Key Properties: sourcer login, share of the sourcing fee, whether the sourcer is linked to Property Predator,
    deals submitted, earned and pipeline.
  - Hello New Home: partner portal, referral link and code, and the area, or **Needs a region** with a button to the
    Hello New Home page where you assign it.
  - Group HQ seller: code, tracked link and whether statements are held.
- **Copy WhatsApp welcome** (mentee page, under the panel) copies a plain message with every link the mentee has. It
  quotes no figures and promises nothing; it asks them to read each programme's terms. A product that could not be
  asked is named beside the button, because its link is not in the message.

## The region process (by hand)

1. **Dashboard > Needs a region** lists every active mentee whose Hello New Home partner has no area, as the last
   recompute found them (the full list is **Mentees**, filter *Hello New Home: Needs a region*). The list is kept by
   the recompute, so opening it calls nobody.
2. Press **Assign a region in Hello New Home** on their row. It opens the head-office partner page in Hello New Home.
3. Assign the area there.
4. The next recompute (03:10, or **Recompute now** on the mentee's Grants tab) reads the area back and takes them off
   the list.

Introducers are never listed (they have no territory), nor is anyone whose access is paused. A mentee is listed only
once Hello New Home is live in Group HQ and its lookup reports an area field; until then the list stays empty.

## When something is red

- **Key Properties links this sourcer to a different Property Predator account.** The sourcer's Key Properties row
  holds another Property Predator account. Check the account id on the person page against the one Key Properties
  holds, put the wrong one right, then press **Retry** on the grant. Group HQ never retries this on its own. The
  Property Predator account id is the long id Property Predator shows (letters, digits and hyphens), not a number.
- **Key Properties answered without the Property Predator account on the sourcer.** Key Properties is running a build
  that does not take the account yet. Group HQ tries again on the next five runs; once Key Properties is updated,
  press **Retry** on the grant if it has stopped.
- **no_account** on a Property Predator affiliate grant: they have no Property Predator account with that email yet.
  Once they sign up, **Retry** on the grant enrols them.
- **answered 404 without its own refusal** on an affiliate grant: Property Predator or Ordris has not deployed its
  side yet. Nothing to do: Group HQ asks again each night without using up the grant's attempts.
