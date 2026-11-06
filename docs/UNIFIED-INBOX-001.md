# Growth HQ unified inbox — first source slice

Task GHQ-UNIFIED-INBOX-001. Base: `6bad98ab4bc7ba2c3eb9c07317bce79e045e3beb`.
Martin explicitly rejected an additional Unipile subscription on 6 November.
There is no Unipile dependency, account provisioning or billing action in this code.

## Implemented in this slice

The owner screen `/inbox` uses the existing Growth HQ authentication, navigation,
origin checks, audit trail and database. Business, channel and multiple account
filters share one list. Each thread/composer carries a signed owner/account/room
binding, invalidated by credential rotation or account remapping. Filter fields
cannot change the reply destination. Provider text is escaped, never rendered as HTML.

The Zernio transport ports the paths, account binding and message envelopes from
Relaunch72 `orchestrator/src/public-social-outbound/zernio-messaging-client.ts`
at `459554640eeddae9c0120857bf087d60f2b5652b`. Facebook Page / Instagram / legacy X
DMs are distinct account connections. The collector's read-only `ZERNIO_API_KEY`
is never reused for sending. A separately configured inbox key needs actual read
and send access. A read-only account can set `can_reply: false`.

The Matrix transport is the self-hosted seam for personal Messenger and LinkedIn
using maintained mautrix bridges. Only explicitly mapped rooms are allowed. Before
read/send it verifies the configured Matrix user's identity, joined membership,
the exact bridge state key, bridge bot and protocol. It refuses encrypted Matrix
rooms rather than downgrading them to plaintext. No account cookies, credentials,
PINs, login or encryption recovery are collected by the Growth HQ UI. A Matrix
event acknowledgement is shown as **connector accepted**, never remote delivery.
It does not prove the bridge has delivered the message to Messenger/LinkedIn.

Manual replies have a committed, per-request receipt before provider IO. Identical
replays cannot send twice; changed content/account under the same request ID
conflicts. Network ambiguity, malformed write acknowledgements and process death
never trigger automatic resend. The UI retains an invalid draft on correction.

## Configuration contract

`GHQ_INBOX_ACCOUNTS` is an operator-managed JSON array. Each record needs `key`,
`brand` (`mhp|pp|kp|hnh|ordris|group`), `channel`, `label`, `provider`, `account_id`,
`credential_env` (a `GHQ_INBOX_*` name) and explicit `can_reply`.
Credential **values** stay in the corresponding private environment setting.

For `provider: zernio`, supply `profile_id`; allowed channels are `facebook`,
`instagram`, `twitter`. Fixed origin: `https://zernio.com`.

For `provider: matrix`, supply the operator's HTTPS homeserver `origin`, actual
`user_id`, and `rooms`, each with `id`, `label`, `bridge_state_key`, `bridge_bot`,
`protocol_id`. Allowed channels are `facebook` and `linkedin`. A personal account
is separate from a business Page even when both say Facebook. No arbitrary origin,
room or provider credential is accepted from the inbox or reply form.

## Still required — not complete or live

- Actual application inbox credentials/account mappings. Connected plugin accounts
  do not automatically install a credential into the deployed application.
- A running self-hosted homeserver and Meta/LinkedIn bridges; owner account pairing,
  encrypted-history access and real Durham history/read/send acceptance. This host
  currently has neither Docker nor Go available. No paid hosting was provisioned.
- Bridge delivery checkpoints need projecting before personal delivery can be called
  confirmed. Matrix transport tests use mocked responses, not a real Facebook account.
- Canonical Relaunch72 CRM create/link/activity command bridge. Its current Growth HQ
  feed is PP-only and read-only. This slice creates no CRM identity, fake email,
  verified contact point, consent, opportunity or duplicate People register.
- Existing WhatsApp/email/SMS inboxes need their native adapters into this view.
  They remain working in their existing workspaces; the paired keyword listener
  and paused five-group Income rule are unchanged.
- TikTok/YouTube/other public comments and reply surfaces. Comments must be labelled
  public and use their original post/comment/account IDs, not the DM composer.
  YouTube is not connected. X's encrypted X Chat is not in the legacy API.

## Checks and release boundary

Focused transport tests: `python -m unittest discover -s unit_checks -p inbox_providers.py -v`.
Database/router tests: `pytest -q tests/test_inbox.py` on a disposable Postgres DB
via the existing test harness. Exact-source CI runs the integrated suite once.

New migration `0011_unified_inbox` adds only reply receipts. **Not applied to
production.** Before release, create a compatible recovery source from the base
that also retains this exact migration in its Alembic registry; the old 0010
binary alone cannot read an unknown 0011 revision. Rehearse upgrade/recovery on
a disposable DB, preserve existing keyword journeys, then release the gated
source. Do not downgrade/drop reply evidence or bypass readiness.

Primary self-hosting references checked on 6 November:

- https://github.com/mautrix/meta
- https://github.com/mautrix/linkedin
- https://docs.mau.fi/bridges/general/docker-setup.html?bridge=meta
- https://docs.mau.fi/bridges/go/meta/authentication.html
- https://spec.matrix.org/v1.16/client-server-api/

These are unofficial personal-account bridges. Self-hosting removes the broker
subscription; it does not remove runtime, account-login, history/encryption or
platform compatibility requirements. No live personal-account proof is claimed.
