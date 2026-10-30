# Group HQ campaign run

## GHQ-OMNI-001 — central workspace

- **Founder instruction:** build PP and MHP follow-up in the separate Group HQ app, not the PP portal. The homepage is being rebuilt independently.
- **Exact base:** `Martoon196/group-hq` `main` commit `bd108f2edefe0de5d68bc03ccc942426be252337`; tree `93b9aab897670a7b05375f03c3a85f7651cfc213` (locally reconstructed and verified).
- **Outcome:** a founder-only, brand-scoped campaign workspace with product-sourced contact projections, versioned journeys, action queue, call/task logging, audit trail and explicit operator holds. No provider effects in this release.
- **Allowed areas:** additive Alembic migration, Group HQ models, CRM router/services/templates/styles, focused tests and this run note. Relaunch72 production tables and provider settings are excluded from 001.
- **Acceptance:** PP lead projection cannot cross brands or exceed the source page contract; MHP is visibly unconnected until its source bridge exists; journey actions are idempotent; all changes are audited; draft actions cannot send; unauthenticated and cross-origin writes fail; existing CRM links and grants continue.
- **Release:** branch and PR; never push straight to auto-deployed `main`. Rehearse migration on disposable database, run focused tests, then exact-release CI. Before merge, verify Render predeploy and recovery path; after merge, check `/healthz`, `/crm` and both product portals. Recovery is a revert commit through the reviewed path while preserving additive tables.
- **Checkpoint before validation:** migration/model/UI changes -> focused campaign tests and migration on local disposable Postgres -> about 10 minutes. No local Postgres is currently identified; if unavailable, run syntax and DB-free route/unit checks and mark integration unverified.

## GHQ-OMNI-002 — owner pilot

Add MHP and PP source contracts and approved email/SMS commands through Relaunch72. Use exact brand, channel, purpose and recipient permission from the product authority, opt-out/suppression, idempotency, owner test allowlist, reply projection, templates and an explicit approval queue. One real owner send per channel only after those checks. No general sending by a Group HQ flag alone.

## GHQ-OMNI-003 — full journeys

Add WhatsApp once the owned Meta account/number, template status and inbound webhook are verified. Calls begin as logged tasks; telephony automation waits for an owned provider contract. Expand to complete PP and MHP journeys after proof for triggers, stop rules, reply handling and brand-specific copy. Neither 002 nor 003 is represented as live by 001.

## Current evidence and limits

- **Release checkpoint, 30 October:** PR #2 first CI run `40984747269` on commit `8dc2a8f759b2e72008346e92d8f8a84b72e68e1e` finished 398 passed, 3 failed. Two failures were stale test assertions naming migration 0008 after additive migration 0009; one was an existing London-day fixture that assumed BST after the October clock change. Astra authorised only those three test corrections. Changed area/concern -> those exact tests plus syntax and whitespace checks -> about 5 minutes, followed by one exact-head CI run. No production growth calculation changed.
- Group HQ `/crm` reads only PP names, stages and task/activity times through `app/services/relaunch72_crm.py`; the product endpoint is `GET /api/group-hq/crm/leads?brand=pp` on the PP host. It exposes no address, phone or consent decision. No MHP lead bridge exists.
- Group HQ's `app/services/followups.py` is a Hello New Home franchise email flow. It is not a PP/MHP sender.
- The prior PP one-lead SMS migration 0124 is unpublished and paused by this founder redirection. Do not deploy or repurpose it.
- General PP SMS is not proven live; the owner verification text/reply was proven. WhatsApp service was suspended at inventory time. The provider channels therefore remain unavailable to campaign sends until their own gates pass.
