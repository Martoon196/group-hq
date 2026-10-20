# Seller commission rules

What someone earns for selling a Hello New Home territory, and how Group HQ records it. Written 20 October
2026 for the plan's Phase 4 ("Seller commission ledger"). **Version 1 of the rules is a proposal: [CONFIRM] on
every percentage and period.** It is seeded with `confirmed = false` and "confirm=false" at the start of its
note; nothing may be promised to a seller from it until the founder confirms it, and the pricing it applies
to is itself unconfirmed (`docs/pricing-bands.md`).

Sources: the plan (Phase 4); `AGREEMENT-HEADS-OF-TERMS.md` section 7 (the statement rhythm, self-billing,
clawback and set-off) and section 6 ("paid when paid"); `PARTNER-PROSPECTUS.md` section 4 (timing). These
documents describe the partner's commission on lettings fees; the seller commission here is a different
payment, for selling a territory, and reuses only their mechanics.

## 1. Who can sell

`sellers`: one row per person who may earn, linked to `people`. `kind` is `mentee`, `franchisee` or
`outsider` and decides the rule. `code` is the tracked-link code (`?seller=<code>` on `/partners-programme`),
unique whatever its case; an enquiry arriving with it lands in `franchise_opportunities.seller_id`.
`bank_reference` is what the statement is paid against; `self_billing_agreed_at` is when the HMRC self-billing
agreement was signed (heads of terms section 7: "Self-billing arrangement (HMRC-compliant self-billing
agreement to be included) or Partner invoice against statement [CONFIRM]"), and no statement is issued without
it. `active` false stops new attributions; earned entries are unaffected.

The sales arm in the plan: Martin closes; a part-time sales lead runs discovery calls and follow-ups from the
pipeline screen; sellers refer through the tracked link.

## 2. The rules, version 1 ([CONFIRM])

Plan, Phase 4: "versioned `commission_rules` (proposal: mentees and franchisees 20% of the upfront fee plus 10%
of months 1 to 12 of the monthly fee; outsiders 10% of the upfront only [CONFIRM])".

| Seller kind | Upfront | Trail | Trail months | Confirmed |
|---|---|---|---|---|
| mentee | 20% of the upfront fee | 10% of the monthly fee | 12 | no |
| franchisee | 20% of the upfront fee | 10% of the monthly fee | 12 | no |
| outsider | 10% of the upfront fee | none | 0 | no |

`commission_rules`: `version`, `seller_kind`, `upfront_pct`, `trail_pct`, `trail_months`, `active_from`,
`note`, `confirmed`. A rule applies to an opportunity whose upfront payment clears on or after `active_from`
(version 1: 20 October 2026, the seed date) and before the next version's; an entry keeps the `rule_id` it was earned under,
so a later version never changes money already earned (heads of terms section 6: "existing Instructions keep
the rate in force when signed [CONFIRM]"). Changing the rates means a new version, never an edit. A fee that cleared before version 1's `active_from`
earns nothing (the ledger refuses it and the payment screen records it as pending), so on a fresh database
the first statement can only cover a month after the seed month; if historical sales are to carry commission
the founder sets `active_from` back, which is a decision, not a default **[CONFIRM]**.

## 3. When an entry exists

Plan, Phase 4: "Entries only exist once `payments.cleared_at` is set." Heads of terms section 6: "Commission is
earned only when HNH receives the fee ('paid when paid'). No commission on signing."

| Basis | Created when | `basis_pence` | `amount_pence` | `trail_month` |
|---|---|---|---|---|
| `upfront` | the upfront payment for the opportunity clears | the upfront fee actually received | `basis_pence x upfront_pct` | NULL |
| `monthly` | each of the first `trail_months` monthly fees clears | that month's monthly fee received | `basis_pence x trail_pct` | 1 to `trail_months` |

So an opportunity at stage `paid` with no cleared payment has no entries, a mentee buyer whose fees are waived
produces entries of zero basis (or none), and a monthly fee that is late produces its trail entry late. Pence
are integers; a percentage of pence is rounded half up to the nearest penny.

## 4. Ordris: the `hnh` partner and `instruction.received` (decision for Martin, [CONFIRM])

A different ledger from the one above, recorded here because it is the only other commission rule in the
group. When Hello New Home marks an instruction `instructed`, Ordris (`lib/hnhInstructions.ts`) writes a
`revenue_events` row with `event_type` `instruction.received`, `amount_pence` 0 and
`internal_referral_source` `hnh`, once per `external_ref` (a landlord client deleted in the app and received
again does not earn a second row). `lib/attribution.ts` then finds the seeded `hnh` partner but needs a
`programme_commission_rules` row for (programme A, `instruction.received`) before any referral record exists,
and because the amount is 0 only a `flat_amount` rule can ever pay on it. No such rule is seeded, so today
the `hnh` partner is credited with nothing, in substance, and the data sharing agreement (section 4, flow 3)
says so.

Two ways to make it pay, pick one:

| Option | What changes | Effect |
|---|---|---|
| A. Flat amount on receipt | Seed one `programme_commission_rules` row: programme A, `instruction.received`, `flat_amount` £[CONFIRM] | The existing code credits `hnh` on the first receipt of every instruction; the Ordris test "credits hnh" becomes true in substance |
| B. Real amount later | Emit a second event (for example `instruction.first_fee`) with the first management fee in pence when Hello New Home records it, and leave `instruction.received` at 0 | Commission follows money received ("paid when paid", as section 3 above); needs a Hello New Home to Ordris call that does not exist yet |

Until one is chosen, no number may be promised to anyone from this row.

Timestamps on an entry: `earned_at` (when the fee cleared), `cleared_at` (when the entry became payable, after
any query window), `paid_at` (when the statement carrying it was paid). The database allows at most one live
entry per opportunity, seller, basis and trail month (`ux_commission_entries_once`), so running the accrual
twice cannot pay twice.

No commission arises on: a seller's own purchase or a connected person's **[CONFIRM]** (heads of terms section
6 has the same rule for partners' own properties); a mentee's waived territory; a payment that is refunded
before it clears; a fee HNH writes off.

## 4. Statements, payment and clawback

Plan, Phase 4: "`statements` generated on the 10th and paid by the 20th (the heads-of-terms rhythm), clawback
if the franchisee is refunded within 90 days." Heads of terms section 7: "Monthly in arrears. HNH issues a
commission statement by the [10th] of each month for the previous calendar month ... payment by the [20th] by
bank transfer. Clawback/set-off: refunds, chargebacks, write-offs and overpayments are deducted from the next
statement(s)".

- On the 10th, every cleared, unpaid entry whose seller has a self-billing agreement is attached to a
  `statements` row for the previous calendar month (`period_start`, `period_end`, one per seller per period);
  `total_pence` is the sum of its entries, clawbacks included, and may be negative, in which case it carries
  forward and nothing is paid.
- Payment by the 20th sets `statements.paid_at` and `paid_at` on every entry it carries.
- A refund of the upfront fee within 90 days of clearing creates a clawback: a second entry with
  `clawback_of` pointing at the original and a negative `amount_pence`, on the next statement. The database
  enforces the sign (a clawback is never positive, a live entry never negative). A refund after 90 days is the
  founder's call **[CONFIRM]**: nothing is written and the opportunity's timeline says so. A refunded monthly
  fee claws back that month's trail entry the same way. The refund is recorded with the Refund button beside
  the payment on the opportunity screen (`POST /franchise/{id}/payments/{pid}/refund`, dated and with a
  reason), which sets `franchise_payments.refunded_at` and calls the ledger's `on_refund`; the manual clawback
  button on the seller page is for a write-off or a dispute, not a refund, and has no window.
- A statement cannot be marked paid while any line on it was earned under an unconfirmed rule: the printed
  statement says nothing on it is payable until the rules are confirmed, and the app holds to that.
- Queries within 30 days of the statement; Group HQ's ledger is the primary record.
- VAT: a VAT-registered seller's self-bill carries VAT; otherwise none. Hello New Home 2 Limited is not
  VAT-registered.

## 5. Worked examples (all [CONFIRM], on unconfirmed prices)

Andover territory sold at the town band, 13,000 upfront and 79 a month (`docs/pricing-bands.md`):

| Seller | Upfront entry | Trail entries | Total over 12 months |
|---|---|---|---|
| mentee or franchisee | 20% x 13,000 = **2,600.00** | 12 x (10% x 79 = 7.90) = **94.80** | **2,694.80** |
| outsider | 10% x 13,000 = **1,300.00** | none | **1,300.00** |

London territory at 15,000 upfront and 149 a month: mentee or franchisee 3,000.00 plus 12 x 14.90 = 178.80,
total 3,178.80; outsider 1,500.00.

If the Andover buyer is refunded in month 2: a clawback of -2,600.00 (and -7.90 for any trail month already
paid) appears on the seller's next statement, netting the statement to zero or below; a negative total carries
forward against future entries.

## 6. The tables

**`sellers`**: `id`, `person_id` (one seller per person), `kind`, `code` (unique, case-insensitive),
`bank_reference`, `self_billing_agreed_at`, `active`, `created_at`, `updated_at`.

**`commission_rules`**: `id`, `version`, `seller_kind`, `upfront_pct`, `trail_pct` (fractions, 0.2000 is 20%),
`trail_months`, `active_from`, `note`, `confirmed`, `created_at`; unique on (`version`, `seller_kind`).

**`commission_entries`**: `id`, `opportunity_id`, `seller_id`, `rule_id`, `basis` (upfront or monthly),
`trail_month`, `basis_pence`, `amount_pence`, `earned_at`, `cleared_at`, `paid_at`, `clawback_of`,
`statement_id`, `created_at`. Checks: a basis is never negative; upfront entries have no trail month and
monthly entries have one from 1; a clawback is never positive and a live entry never negative; one live entry
per opportunity, seller, basis and trail month. Opportunities, sellers and rules cannot be deleted while
entries point at them.

**`statements`**: `id`, `seller_id`, `period_start`, `period_end`, `total_pence`, `generated_at`, `paid_at`,
`created_at`; unique on (`seller_id`, `period_start`, `period_end`).

**`franchise_opportunities`** (the pipeline the ledger hangs off): `person_id`, `stage` (enquiry, discovery,
pack_sent, follow_up, agreement, paid, onboarding, lost), `districts`, `band`, `quoted_upfront_pence`,
`quoted_monthly_pence`, `seller_id`, `source`, `notes`, `stage_changed_at`; `activities` (call, email, note,
task_done) and `tasks` (open, done, skipped; created at day 2, 5, 9 and 14 after `pack_sent`) sit under it.

## 7. Before any seller is told a number

1. Confirm the three rules (or replace them with a version 1 the founder wants) and set `confirmed`.
2. Confirm the pricing bands they apply to.
3. The self-billing agreement text (heads of terms schedule F) and whether landlords or buyers must be told a
   seller is paid a referral commission (heads of terms, point 8 for the solicitor).
4. Whether an outsider should have any trail at all, and whether a mentee's commission is affected by losing
   mentee status (the heads of terms suspend new introductions but keep existing commissions).
