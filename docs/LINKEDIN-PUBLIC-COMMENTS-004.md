# GHQ-INBOX-004: LinkedIn company public comments

Bounded provider package, based on Group HQ commit
`83639933620c4a32a5c45dadb8f623fdd642b4ac` (tree
`f8aecb2c10e7959f0b160e7fd2e683fa36db8247`). Root owns integration,
ratification and production. No provider send or production configuration change
was performed by this package.

## Existing-account proof, 9 November 2026

The connected Zernio account inventory contains 11 accounts. PP and KP LinkedIn
company accounts have healthy credentials and the granted organization-social
read/write scopes, including `r_organization_social`,
`r_organization_social_feed`, `w_organization_social` and
`w_organization_social_feed`; neither health response requires reconnecting.

Actual read-only Zernio calls:

- PP account `6a98ae4677555aae01c8ec39`: listing company comment posts succeeds,
  `accountsQueried=1`, `accountsFailed=0`, no skipped/failed accounts. The
  sampled owned post is `urn:li:share:7525535372681068545` (zero comments).
  Fetching its comments succeeds with exact account, LinkedIn platform and post
  metadata; zero comments returned.
- KP account `6aebc74669f0b74d1604ce03`: listing succeeds,
  `accountsQueried=1`, `accountsFailed=0`, no skipped/failed accounts; empty page.
- No reply was sent. These reads prove the existing provider connection can read
  this public surface, not successful live reply delivery. Fixtures cover
  nonempty/nested comments and outbound acknowledgements.
- The usage tool returned metering, not a plan/quota snapshot. No new plan or
  service was purchased; do not claim remaining quota was verified.

## Provider capability matrix

| Existing connection | Supported surface / present limit | Result for this package |
| --- | --- | --- |
| PP/KP LinkedIn companies | Public company comments; no company DMs/InMail API | Implemented separate public-comments page; current-account read proof above |
| Martin personal LinkedIn | Publishing connection does not establish company comments or personal DMs | Excluded from public registry |
| TikTok `ukpropertyinvestment`, `6a98c52f77555aae01c9bccc` | Current health says `integrationLane=developer`, publishing scopes only. Inbox needs Business-app connection; DMs also have UK/EEA/Swiss region restrictions | Not available from this existing connection; no reconnect attempted |
| YouTube | Public comments, no private messages | No connected YouTube account in current 11-account inventory; connection required |
| WhatsApp through Zernio | Connected WhatsApp Business number; customer-service window / approved templates apply | No Zernio WhatsApp account in current inventory. Existing native WhatsApp work is a separate integration, not evidence of Zernio readiness |

Primary documentation checked:
[LinkedIn](https://docs.zernio.com/platforms/linkedin),
[TikTok](https://docs.zernio.com/platforms/tiktok),
[YouTube](https://docs.zernio.com/platforms/youtube),
[WhatsApp inbox](https://docs.zernio.com/platforms/whatsapp/inbox).

## Root integration and concrete nonsecret configuration

Mount `app.routers.inbox_comments.router` once in `app/main.py`, following the
existing router import/include pattern. Add a clearly labelled **Public LinkedIn
comments** navigation link to `/inbox/comments` from the shared inbox/channels
page. This package intentionally does not edit those shared files.

`GHQ_INBOX_PUBLIC_ACCOUNTS` is a separate JSON setting. This is the exact
nonsecret production mapping; `credential_env` is a reference, never a token:

```json
[
  {
    "key": "pp-linkedin-public",
    "brand": "pp",
    "channel": "linkedin",
    "provider": "zernio",
    "label": "Property Predator",
    "account_id": "6a98ae4677555aae01c8ec39",
    "profile_id": "6a98ac5a647e96173efa7ed8",
    "owner_email": "martin.howard1984@gmail.com",
    "credential_env": "GHQ_INBOX_ZERNIO_TOKEN",
    "can_reply": true
  },
  {
    "key": "kp-linkedin-public",
    "brand": "kp",
    "channel": "linkedin",
    "provider": "zernio",
    "label": "Key Properties UK",
    "account_id": "6aebc74669f0b74d1604ce03",
    "profile_id": "6acfc2c1347304c5e294a88a",
    "owner_email": "martin.howard1984@gmail.com",
    "credential_env": "GHQ_INBOX_ZERNIO_TOKEN",
    "can_reply": true
  }
]
```

Root must use the actual **existing inbox credential** env name if it differs
from `GHQ_INBOX_ZERNIO_TOKEN`. No new token value is included or transferred by
this package. The growth collector's read-only `ZERNIO_API_KEY` is not accepted.
Absent credentials display setup-needed and cannot send. The mapping is limited
to these two exact company account/profile/brand tuples; the personal LinkedIn
account cannot be silently substituted.

## Permission and journal boundaries

- Both routes reuse `require_admin`; each public account additionally requires
  the signed-in email to equal its configured `owner_email`. POST reuses the
  existing same-origin guard.
- `PublicCommentAccount` keeps the existing Account transport contract but adds
  a public-surface binding/identity discriminator. Private account hashes and
  inbox configuration remain unchanged. The dispatcher selects the new adapter
  only for this surface.
- Public targets have a separate signed-ticket salt, actor ID, account key,
  credential-bound binding, exact owned post and observed comment ID. Tickets
  expire after two hours; rotation/remapping/owner or reply-permission changes
  revoke them. Post-only tickets cannot publish. Private DM tickets cannot
  select a public target.
- The public composer is blank and displays the company and comment author.
  POST requires `audience=public_linkedin` and an explicit public acknowledgement.
  No private draft, private history, CRM linking or consent is imported here.
- Only comments reported replyable by the provider receive reply tickets.
  Posts and comment metadata must match the configured account/platform/post;
  nested replies are bounded. Public post/comment text is HTML-escaped.
- POST reuses `app.services.inbox.send`: durable `inbox_replies` receipt before
  provider IO, same-request replay prevention, changed-content conflict,
  accepted/rejected/unknown outcomes and existing audit actions
  `inbox.reply.begin` / `inbox.reply.<state>`. Public account key and binding
  distinguish this surface. Provider comment ID is retained. Body and raw target
  are not stored for this remote transport; hashes bind the request.
- Provider route is `/api/v1/inbox/comments/{postId}`, with exact `accountId`,
  `commentId`, `message`, and idempotency header. It never uses a DM endpoint.
  Network ambiguity/malformed write acknowledgement is outcome-unknown, with no
  automatic retry. No new schema/migration or outbound queue is needed.

## Validation and release boundary

Changed area / concern -> smallest check -> expected duration:
public surface isolation and provider contract -> 18 focused no-DB tests plus
14 existing transport-neighbour tests and changed-file lint -> under one minute.
All 32 passed, zero failures/skips; changed Python files passed pyflakes.

Six `tests/test_linkedin_public_comments.py` route/journal regressions are
provided for the existing local-Postgres / CI harness. They cover auth and
escaping, durable acceptance/idempotency and changed-content conflict, unknown
outcome replay, audience/origin guards, private/post-only target rejection, and
owner/credential revocation. Initially pending, all six subsequently passed
against Root's disposable local PG16 fixture, together with 22 personal-inbox
regressions (28 passed, zero failures/skips). The tests mount the separate router
when absent. Root still owns mounting/navigation and the normal exact integrated
CI gate. This package is source-complete subject to that integration gate, not
published or verified live.

Cutover: integrate package, mount/navigation, pass six route tests and required
exact-release gate, bind existing credential env reference, deploy, then perform
owner GET walkthrough at `/inbox/comments` for both company mappings. Empty
comment pages are valid and match current read proof. A deliberate owner public
reply is a real publication, not part of this package's no-send verification.

Recovery: set `GHQ_INBOX_PUBLIC_ACCOUNTS=[]` (or `can_reply=false` for read-only)
and redeploy using normal Root procedure; private inbox/accounts and enabled
customer journeys are untouched. Existing durable reply receipts remain; never
retry an unknown public reply as recovery.
