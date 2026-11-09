# GHQ-INBOX-005B: public YouTube and TikTok comments

Root-assigned package on fresh authoritative main
`47987ba6f6733237a80f1db5a5f4534a5457f3ef`, tree
`26d1c83cf0c1b677a6700b6afbb2f47676401466`. Isolated branch
`codex/ghq-public-comments-005b-20261109`. Root owns integration and the single
release. No deployment, owner OAuth completion, provider send or production
configuration change is performed by this package.

Outcome: owner can open `/inbox/video-comments`, explicitly choose one existing
business profile and YouTube/TikTok, start provider authorization, refresh the
real account inventory and enable that observed public account. Enabling checks
the actual comment-post API; a posting-only/skipped account cannot become a
ready inbox. Read/reply uses the existing public parser, fixed Zernio transport,
owner auth, origin guard and durable `InboxReply` journal. Reply composers are
blank, explicitly public and isolated from private messages/CRM/marketing grants.

Allowed areas: new video comment provider/service/router/template/tests, small
channel-neutral hooks in the existing **public** LinkedIn parser and provider
dispatch, and documentation. LinkedIn private, WhatsApp/AI, private inbox,
marketing automation and schema changes are excluded. No replacement CRM or
transport was added.

Validation checkpoint: shared public parser hooks and owner connection/reply
flow -> nine focused transport tests, existing LinkedIn public neighbours, six
new PG owner/setup/journal regressions and existing six LinkedIn routes ->
expected one minute in Root's disposable local PG16. The initial nine transport
tests and changed-file pyflakes already pass; no live provider writes occur in
tests. The final exact integrated release gate remains Root's responsibility.
All 12 PG tests passed (six new video, six existing LinkedIn), zero failures or
skips, in 28.66 seconds. Nine new transport and 19 existing LinkedIn unit checks
passed, zero failures/skips. Callback error guidance was then added to the new
GET page so denied/missing permissions and missing YouTube Brand Accounts do
not silently look like successful setup. Smallest follow-up: the two affected
owner setup/read-page tests plus changed-file lint -> expected ten seconds.
The two changed-page regressions passed, zero failures/skips, in 7.61 seconds;
lint and whitespace checks passed. Tests are focused package evidence, not a
claim of live provider reply delivery or a replacement for the integrated gate.

## Real account and capability evidence, 9 November 2026

- Zernio inventory is still 11 accounts; **no YouTube account is connected**.
- TikTok `ukpropertyinvestment`, account `6a98c52f77555aae01c9bccc`, belongs to
  Prop Invest UK profile `6accec6f88d3a22fceb2df31`. The actual filtered account
  inventory confirmed the populated `profileId._id`, not a guessed brand map.
- TikTok health is healthy for publishing, `integrationLane=developer`, only
  posting/profile/analytics permissions. `tiktokAccountType=unknown`; signup
  region was not supplied, so this package does not invent it from the handle.
- Real read-only TikTok comment listing returned `accountsQueried=0`,
  `accountsFailed=0`, empty data, and this exact account in `accountsSkipped`.
  This is unavailable comment capability, not a supported empty inbox.
- Real read-only OAuth-start requests succeeded for MHP YouTube and exact
  PIUK TikTok reconnect (bound account ID and expected handle). Zernio returned
  actual `https://zernio.com/connect/welcome?ticket=...` requests. No owner
  approved either. An initial request lacked required `welcome=true` with
  `brandName`; it failed 400 and was corrected before success.
- The owner links are outside Git in the task artifact
  `GHQ-INBOX-005B-OWNER-CONNECTION-LINKS.md`. Hold until this app page is deployed;
  the in-app button creates a fresh request if an old one expires.

Primary API documentation:

- [Zernio TikTok inbox](https://docs.zernio.com/platforms/tiktok/inbox): public
  comments need the TikTok Business-app connection. Current new connections use
  that app. DMs are replies only and not provided for Business Accounts signed
  up in the UK, EEA or Switzerland. This package does not bypass that restriction.
- [Zernio connection endpoint](https://docs.zernio.com/connect/get-connect-url):
  bound reconnect/expected username, hosted welcome, provider permission errors.
- [YouTube commentThreads.list](https://developers.google.com/youtube/data/reference/rest/v3/commentThreads/list)
  and [comments.insert](https://developers.google.com/youtube/data/reference/rest/v3/comments/insert):
  public comment listing and replies; insert requires `youtube.force-ssl`.
- [TikTok API for Business](https://ads.tiktok.com/resources/help/article/marketing-api?lang=en)
  and [Business Messaging get started](https://business-api.tiktok.com/portal/docs/business-messaging-api-get-started/v1.3)
  confirm that messaging is separately authorized from posting. They do not
  establish this account's eligibility. Zernio's own integration restrictions
  govern the built route.

## Integration and permission boundaries

Root mounts `app.routers.video_comments.router` once and adds a navigation link
labelled **Public YouTube & TikTok comments** to `/inbox/video-comments`. No
shared main/private-template file is edited here.

The owner must match existing `GHQ_PERSONAL_INBOX_OWNER_EMAIL`. The four existing
profiles are fixed to MHP, PP, PIUK and KP. Credentials are reused only from an
existing Zernio inbox account with the **same brand and profile**, compatible
owner and exactly one unique approved token; the read-only growth collector key
is never used. Missing or insufficient existing access produces setup/permission
status. No token is stored in new app settings.

`GET /api/v1/accounts?profileId=...&platform=...&limit=100` returns real inventory;
the parser requires exact returned profile/platform and strips credentials and
unneeded fields. Owner enable tickets are actor/profile/credential-bound for
30 minutes. Enabling rereads inventory and requires a connected exact account,
owner acknowledgement and successful bound comment listing. An advisory lock
preserves other public-channel choices during concurrent setup.

References only are stored in existing `AppSetting` key
`inbox.video.v1.<user-id>`: brand/channel/profile/owner/account ID/name and explicit
reply permission. Audit action is `inbox.video.enable`. Returning from OAuth
does not trust callback `accountId` or automatically grant access; the owner
refreshes actual inventory and explicitly enables the channel.

Public video post/reply tickets use a separate salt and surface from LinkedIn
and private inbox tickets. They bind actor, configured account, profile,
credential hash, post and observed replyable comment. Replies require correct
`public_youtube` / `public_tiktok` audience and a public acknowledgement. The
shared journal commits before provider IO and preserves accepted/rejected/
unknown/idempotent outcomes; no private draft is copied or retry automated.

## Honest limits and recovery

Neither YouTube read/reply nor TikTok Business-app public comments is claimed
live before owner authorization. No public reply was sent for proof. UK TikTok
DMs are not built through this provider route. Actual post/comment pagination
uses provider cursors; setup inventory is bounded to the first 100 accounts per
chosen profile/platform. Provider plan/permission limits are not silently bypassed.

No migration is required. Cutover is Root's integrated release, then the owner
opens the new public page, approves the chosen platform and enables the observed
channel. Existing private and LinkedIn public journeys remain enabled. Recovery:
remove the new navigation/router or restore the prior compatible application;
retain account-reference settings/journal receipts. Re-enabling with replies
unchecked makes the channel read-only and invalidates old composers. Never
retry an unknown public reply as recovery.

Package status: source-complete, not integrated/published/deployed by this agent.
The actual linked-account limitations remain until owner authorization and a
successful live read. Single-release integration and final ratification are
Root's responsibility.
