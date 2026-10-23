# Connectors

How Group HQ grants and revokes access in the four products, what each product's server-to-server
API looks like, and how far each contract has got. Written 20 October 2026.

| Product | Module | Contract | Status |
|---|---|---|---|
| Property Predator | `app/services/connectors/pp.py` | plan Phase 1, PP branch `feat/grouphq-partner`; affiliate contract 1 (below) | **implemented here; the PP side goes through PP's audit lane before anything is live** |
| Ordris | `app/services/connectors/ordris.py` | plan Phase 2, Ordris branch `grouphq-connector` | **built on the product branch**, verified against the fixtures here; not yet live |
| Key Properties | `app/services/connectors/kp.py` | plan Phase 2, portal branch `grouphq-connector` | **built on the product branch**, verified against the fixtures here; not yet live |
| Hello New Home | `app/services/connectors/hnh.py` | plan Phase 2, HNH branch `partner-api` | **built on the product branch**, verified against the fixtures here; stays in dry run until the data sharing agreement exists |

All four work in dry run today: a call returns the recorded plan (method, URL, query, body, redacted
headers) and sends nothing, so the recompute can be rehearsed end to end before any product is ready.

## What every connector does the same way

- **Auth**: `Authorization: Bearer <token>` from the environment (`PP_SERVICE_TOKEN`,
  `ORDRIS_PARTNER_TOKEN`, `KP_PARTNER_TOKEN`, `HNH_PARTNER_TOKEN`) and `X-Partner-Client: grouphq`
  on every request, plus `Accept: application/json` and `User-Agent: GroupHQ/1.0`. Property Predator
  reads the token from `X-Service-Token` instead (its `SERVICE_TOKEN_*` convention), so the PP
  connector sends both headers. No `Origin` is ever sent; the products refuse browser origins on
  these routes.
- **Timeout**: 10 seconds for the whole request. **No retries inside a connector**: the recompute job
  retries through the outbox on its next run (below).
- **Errors** are `ConnectorError(reason, status, detail, body)`, never a bare exception. `reason` is a
  short code: the product's `reason`, `error` or `code` field when it gives one, otherwise
  `http_<status>`, `timeout`, `unreachable` or `not_configured`. A 2xx answer that still says
  `{"ok": false, "reason": ...}` is an error with that reason.
- **Tokens** are never logged, never stored in a `connector_calls` row and never shown on a page: a
  recorded plan carries `"Authorization": "Bearer [redacted]"`.
- **A product's answer is redacted before it is stored**: `connector_calls.response` goes through the
  audit redaction (`audit._jsonable`), so any key naming a password, secret or token in a body, an error
  body included, is stored as `[redacted]`. HNH's one-time password is taken out of the answer before that
  (below) and shown once on the run page through `ConnectorResult.once`, which is never part of a stored
  row, an audit row or the job's output.
- **Dry run**: `CONNECTORS_DRY_RUN=1` in the environment makes every run a dry run whatever the
  caller asks, and no switch can lift it. Otherwise each product follows its Live / Dry run switch on the
  Settings screen (the `app_settings` table, `product_switch.<product>`) once that has been set; a product
  never switched follows the environment list (`CONNECTORS_DRY_RUN=pp`, the shipped value, keeps only PP in
  dry run; `0` sends to every product). The web service and the nightly job read the same switches
  (`connectors.product_dry_run`, with `settings.product_switches(db)` passed in). The `--dry-run` flag and the
  page toggle make one run a dry run. A run reads the switches as it starts and again before each person, so a
  product switched to Dry run stops being sent anything by a run already under way; a switch to Live waits for
  the next run. Switch a product live only once it has been rehearsed with a test account. A product with no base
  URL or token is skipped without counting an attempt and shows as not configured, whatever its switch says; in
  the nightly job that also makes the run exit 1, so a token never copied to the cron shows as a failed run.

## The outbox and the idempotency key

Every grant, extension and revoke the rule decides on becomes one `connector_calls` row before the
wire is touched:

| Key | When |
|---|---|
| `grant:<grant_id>:<ends_at>` | grant or extend to that end, for example `grant:12:2027-10-01T00:00:00` |
| `grant:<grant_id>:<ends_at>:after:<call_id>` | the same, once a revoke of the grant has succeeded (`call_id` is that revoke's row), so an undone Revoke all is sent again rather than matched to the row sent before the revoke |
| `grant:<grant_id>:revoke:<ends_at>` | revoke after that end passed, for example `grant:12:revoke:2027-10-01T00:00:00` |
| `grant:<grant_id>:revoke:<ends_at>:after:<call_id>` | the same, once the grant has been revoked and granted again (`call_id` is that re-grant's row), so pause, resume and pause again on one day is three calls, not a match of the first revoke |

A held grant (Pause on the grant, or Pause all access for the person) is revoked at midnight on the day the hold
began, so a revoke that fails is retried under the same key on the following nights; a held grant that is not
active is left alone (`held` on the run page) and Resume drops any revoke still waiting in the outbox.

The key is unique in the table, so a re-run with the same decision finds the same row: if it has
succeeded nothing is sent again; if it failed the attempt counter goes up and the call is retried;
after **5 attempts** the grant is marked `failed` and left alone until the end date changes (a new
payment gives a new key) or someone presses **Retry** on the grant. A revoke after a late payment
restored access gets its own key because the end date it enforces is different, which is why the
revoke key carries the date rather than the bare word the plan sketched.

In a dry run the row is written with status `pending`, attempts 0 and the planned call as its
response; the next live run sends it. A pending row that was never attempted is deleted when a later
decision supersedes it; a row that was attempted keeps its history and gets `superseded_by` in its
response. An active grant keeps the window the product last confirmed until a new one is confirmed,
which is what makes a failed or dry-run extension go out on the next run.

Guards that hold whatever a caller asks for: a grant or extension is never sent with an end more
than 400 days away, or with an end that is not in the real future; a live run is always as of today
(another as-of date is a dry run only); grants no programme gives any more are revoked before new ones
are granted, because PP holds one comp per account and revokes by email, and a template may name
`pp comp_tier` once; a person is recomputed under a Postgres advisory lock, and the nightly run holds a
run-wide lock, so a button press during the cron cannot send a call twice.

A row is committed with its attempt counted before the request goes out. If the process dies between
the call and the commit of the result, the next run resends the same call with the same key: every
product endpoint is written to be idempotent for a repeated payload (PP comps by email to a date,
Ordris by `external_ref`, KP and HNH by email), so a resend never double-grants.

## Grant states

`entitlement_grants.state` is `pending`, `active`, `revoked` or `failed`. The plan's **blocked** state
(the product refused because the person is on a live paid subscription) cannot be a column value
without a migration this packet does not own, so it is `state = failed` with
`last_error = "paid subscription"`; `entitlements.is_blocked()` and `display_state()` read it back and
the screens show it as `blocked`. A blocked grant is never retried automatically; Retry on the grant
clears it once the subscription has ended.

An active grant whose extension or revoke fails stays `active` (the product still holds the old
window and the person still has access) with `last_error` set, and is retried next run. A first grant
that fails is `failed` and retried next run.

## Property Predator

Base URL `PP_BASE_URL` (live: `https://propertypredator.com`). Token `PP_SERVICE_TOKEN`, sent as
`X-Service-Token`, which on the PP side is `SERVICE_TOKEN_GROUPHQ` checked by
`job_auth.token_matches(token, "grouphq")`; a bare `Authorization: Bearer` is refused there, so the
connector sends both. The PP router refuses any request carrying an `Origin` header and caps `until`
at 13 months.

| Group HQ grant | Call | Answer |
|---|---|---|
| `comp_tier` with value `hunter`/`pro_buyer`, `apex`/`pro_investor`, `business`/`pro_business` | `POST /api/partner/grouphq/comp` `{email, tier, until, revert_to: "free", reason}` | `{ok: true, account_id, tier, comp_expires_at}`; `409 {ok: false, reason: "paid_subscription"}`; `404 {ok: false, reason: "no_account"}` |
| `comp_tier` with value `scout`/`free` | nothing is sent | recorded locally as a no-op (the grant is `active` with no `remote_ref`) |
| revoke of a `comp_tier` | `POST /api/partner/grouphq/revoke` `{email, reason}` | `{ok: true, reverted_to}`; `404 {ok: false, reason: "no_comp"}` (also the answer for an unknown email) is treated as done |
| lookup | `GET /api/partner/grouphq/account?email=` | `{account_id, tier, subscription_status, comp_expires_at}` |
| `flag` (`kpuk.submit`, `ordris_active`), Phase 3 | `POST /api/partner/grouphq/flags` `{email, key, until}` and `POST .../flags/revoke` `{email, key}` | **contract pending** (plan Phase 3 C1); works in dry run |

`until` is sent as `2027-10-01T00:00:00Z`, midnight UTC at the start of the day access ends, which
PP's `grant_comp` parses with `fromisoformat`. `revert_to` is always `free`: a mentee gift ends on
the free tier, never on a tier they were not paying for. `account_id` in any answer (PP's `users.id`, a UUID
string, kept as text) fills `people.pp_account_id` when it is empty and no other person holds it. Asks of the PP endpoint, so the mapping above holds: send
`reason` codes `paid_subscription` (409), `no_account` (404) and `not_on_comp` (409) rather than
prose, and keep the hourly `expire_comps` reverting on its own so a Group HQ outage never makes a
gift permanent.

## Ordris (built on branch `grouphq-connector`)

Base URL `ORDRIS_BASE_URL` (no default: set it when the Render move lands). Token
`ORDRIS_PARTNER_TOKEN`, which on the Ordris side is `ORDRIS_PARTNER_TOKEN_GROUPHQ` compared with
`crypto.timingSafeEqual`; 503 `not_configured` while it is unset, 403 `unknown_client` without
`X-Partner-Client: grouphq`, 401 `bad_token` on a mismatch, 403 `origin_refused` for any `Origin`.

| Group HQ grant | Call | Answer |
|---|---|---|
| `org_plan` with value `starter`, `pro` or `enterprise` | `POST /api/partner/grouphq/entitlements` `{email, plan, until, reason, external_ref: "grouphq:grant:<id>"}` | `{ok: true, org_id, user_id, plan, until, external_ref}`; `user_id` and `org_id` fill `people.ordris_user_id` and `people.ordris_org_id`; `404 {ok: false, reason: "no_account"}`; `400 invalid_request` |
| revoke | `DELETE /api/partner/grouphq/entitlements/grouphq%3Agrant%3A<id>` | `{ok: true, revoked: true}`; a reference already gone answers `{ok: true, revoked: false}` (a 404 is read the same way) |
| lookup | `GET /api/partner/grouphq/lookup?email=` | `{ok: true, user_id, org_id, email, trial_end, subscription_status, entitlement: {plan, until, external_ref} or null, plan_active, plan_source}`; `404 no_account` |

Ordris resolves the Clerk user by email and applies the plan to the organisation `requireAuth` would
give that user's session (`users.org_id`, the value the last session produced; the user id for a solo
account). `external_ref` is unique on their side, so a repeated POST updates rather than duplicates. An
entitlement whose window covers now makes Ordris's server-side plan check (`getPlanStatus`) true, which
`GET /api/users/me/plan` and the web app's subscription gate read; no write route is closed by that
check yet, so the browser-only gate stays as it was until Ordris mounts `requireActivePlan`.

## Key Properties (built on branch `grouphq-connector`)

Base URL `KP_BASE_URL` (live: `https://portal.keypropertiesuk.co.uk`). Token `KP_PARTNER_TOKEN`,
which on the portal side is `GROUPHQ_PARTNER_TOKEN` (`src/routes/partner.js` keys tokens by client:
503 `not_configured` while it is unset, 403 `unknown_client`, 401 `unauthorised`, 403 `browser_origin`
for any `Origin`, and 403 `wrong_client` when a credential calls the other client's routes).

Every answer is a flat row, with `id` at the top: `{ok: true, id, email, name, company, source,
pp_account_id, fee_share_pct, active, created_at, last_login}` (the POST adds `created` and `linked`).
`pp_account_id` comes back as a string.

| Group HQ grant | Call | Answer |
|---|---|---|
| `sourcer_link` with value `mentee` (the portal's `users.source`) | `POST /api/partner/sourcers` `{email, name, company, pp_account_id, fee_share_pct?, source: "mentee"}`: creates the sourcer, or links the user with that email to the PP account | `201` created or `200` linked, the row; `id` becomes `remote_ref` and `people.kp_user_id`; `409 email_is_admin`, `409 linked_elsewhere`, `409 pp_account_in_use`; `422 invalid` |
| grant when `remote_ref` is known | `PATCH /api/partner/sourcers/{id}` `{active: true}` | the row; `404 not_found` for an id that is not a sourcer |
| revoke | `PATCH /api/partner/sourcers/{id}` `{active: false}`; without a `remote_ref` the id is looked up by email first | the row; `404 not_found` is treated as already gone |
| lookup | `GET /api/partner/sourcers?email=` | the row, or `404 {ok: false, error: "not_found"}` when no sourcer has that email (also the answer for an admin's email); the connector reads that 404 as "no sourcer", so a revoke with nothing to revoke is done, not failed |
| list (the existing-users import, `KPConnector.list_sourcers(limit, offset)`) | `GET /api/partner/sourcers?limit=&offset=` with no `email` | `{ok: true, sourcers: [{id, email, name, company, source, pp_account_id, active, created_at, deals}], total, limit, offset}`, newest first, never an admin, `deals` the deals each one has sent for review; `422 invalid` for a paging value out of range (and from a portal without the list yet). Group HQ asks for 100 a page (the connector caps a request at 200, the portal at 500). In dry run the call is only planned and the import page says Key Properties is not listed |

`fee_share_pct` is sent only when the programme template item carries it (`{"product": "kp", "kind":
"sourcer_link", "value": "mentee", "fee_share_pct": 50}`, validated 0 to 100 on the programme screen);
until Martin signs the mentee figure off the portal's own default (50) applies. `company` is the first organisation linked to
the person in Group HQ, or blank.

## The existing-users import

`/mentees/import` (Mentees, **Existing users to mentees**) enrols people who already have a login in one
batch of up to 25. It reads; it never creates anyone in a product except through the grants that follow.
For each person it asks every product that is not in dry run whether it knows the email, with the lookup
rows above: Ordris `GET .../lookup?email=`, Key Properties `GET /api/partner/sourcers?email=`, Hello New
Home `GET /api/partner/partners?email=` and Property Predator `GET .../account?email=`. A 404 (or HNH's
`found: false`) is "not on that product". The ids they answer fill only the person's EMPTY link fields
(`ordris_user_id`, `ordris_org_id`, `kp_user_id`, `hnh_partner_id`, `hnh_user_id`, `pp_account_id`); a
different value already recorded is kept and reported, and an id already linked to another person is not
linked twice (except `ordris_org_id`, which people in one Ordris organisation share). An existing person's
row is locked until their commit, so a double submit of the same batch enrols and records the payment once:
the second waits and then skips them. A lookup that fails is reported on that person's row and the batch
goes on; a product that fails for three people running is not asked again in that batch. With **Grant
access now** ticked and a cleared payment entered, each new mentee gets the same recompute as the mentee
page's **Recompute now**, so grants are sent to live products and planned for products in dry run, and an
HNH temporary password is shown once on the results page and stored nowhere. Once a product a person's
programmes grant has failed for three people running, their recompute is not run in that batch and the
nightly recompute at 03:10 grants instead, so a product that is down cannot hold the page past a proxy's
timeout (the results page is the only place those passwords are shown). The submit button is disabled
after the first press.

## Hello New Home (built on branch `partner-api`)

Base URL `HNH_BASE_URL` (live: `https://hellonewhome.co.uk`). Token `HNH_PARTNER_TOKEN`, which on
the HNH side is the `grouphq` row of `api_clients` (SHA-256 of the secret, scopes `partners:read` and
`partners:write`; `partners:write` also covers the lookup) checked by `require_api_client` with
`hmac.compare_digest`. HNH reads the secret from `X-Service-Token` or from `Authorization: Bearer`;
the connector sends the bearer form. Any `Origin` is refused (403 `origin_not_allowed`); a missing or
wrong secret or client name is 401 (`missing_token`, `missing_client`, `invalid_client`,
`client_inactive`); a missing scope is 403 `scope_missing`.

| Group HQ grant | Call | Answer |
|---|---|---|
| `partner_seat` with value `area` or `introducer` | `POST /api/partner/partners` `{email, name, tier, status: "active", create_login: true, franchise: {fee_waived: true, reason}, area_slug | postcodes[]}` | `201` created or `200` updated: `{ok: true, created, partner_id, user_id, one_time_password, area_slug, area_created, note, partner: {id, name, email, tier, status, franchise, areas[], user_id, login, created_at}}`; `partner.id` and `partner.user_id` become `remote_ref`, `people.hnh_partner_id` and `people.hnh_user_id`; `409 email_in_use`, `409 area_assigned`, `409 postcode_clash`, `409 slug_taken`; `422 invalid_request` |
| grant when `remote_ref` is known | `POST /api/partner/partners/{id}/status` `{status: "active", reason}` | `{ok: true, partner_id, status, previous_status, changed, partner}`; `404 no_partner` |
| revoke | `POST /api/partner/partners/{id}/status` `{status: "paused", reason}`: the partner and their area stay on record; without a `remote_ref` the id is looked up by email first | as above; `404 no_partner` is treated as already gone |
| lookup | `GET /api/partner/partners?email=` | `{ok: true, found: true, partner: {...}}` or `{ok: true, found: false, partner: null}` (a 404 would be read as not found too) |

`name` is required by HNH (it is printed on the public area pages), so the connector refuses to send a
grant for a person with no name (`missing_name`, shown on the grant page) rather than let HNH's 422 be
retried five times: add the name on the person page and press Retry.

**The one-time password.** `create_login: true` makes HNH create the partner's portal login and return
its temporary password once (`one_time_password`; hashed on HNH's side, must be changed on first use,
null on a resend). The connector removes it from the answer before anything is stored and shows it once
on the run page, to the person who pressed **Run nightly now** or **Retry**, as "HNH temporary portal
password for <email>: ..."; pass it on there and then. It is not in `connector_calls`, not in the audit
trail and not in the nightly job's output, so a grant sent by the cron shows no password at all. A lost
or unseen temporary password is re-issued by head office from the HNH admin partner page (Partners,
the partner, **Reset password**), never from Group HQ.

`area_slug` or `postcodes` (a list of outward codes) and `fee_waived` come from the programme
template item (`{"product": "hnh", "kind": "partner_seat", "value": "area", "area_slug": "swansea"}`,
validated on the programme screen) when the territory has been decided;
without one HNH creates the partner with no area and head office assigns it by hand. A slug that
already exists on HNH with the same postcodes is assigned; with different postcodes HNH answers
`409 slug_taken` naming the postcodes it holds, so a changed template never silently leaves the old
territory in place. Hello New Home
is a separate company (Hello New Home 2 Ltd): this call moves a name and an email between two data
controllers, so it stays in dry run until the intra-group data sharing agreement and consent wording
exist (plan, Phase 3).

## Mentee earnings: contracts 1 to 4 (23 October 2026)

Every mentee is enrolled in each scheme the group runs (`docs/mentee-earnings.md`). The product side of each contract
is built in that product's own lane; Group HQ codes to exactly these, and the recorded fixtures in
`tests/fixtures/mentee_earnings/` are the bodies it expects. All four are server to server with the product's
existing Group HQ credential: no `Origin`, `X-Partner-Client: grouphq`, the product's own token header and
constant-time check, a durable daily cap on the product side, and refusals as `{ok: false, reason, message}`.

| # | Product | Group HQ grant or read | Call | Answer |
|---|---|---|---|---|
| 1 | Property Predator | `affiliate` (template item `{"product": "pp", "kind": "affiliate", "value": "affiliate"}`) | `POST /api/partner/grouphq/affiliate` `{email, reason}` | `{ok: true, account_id, code, link, created}` (`created` false when already an affiliate; idempotent) or `404 {ok: false, reason: "no_account"}`; `account_id` (PP's `users.id`, a UUID string) becomes `remote_ref` and fills `people.pp_account_id`, which is text since 0008; `code` and `link` are kept on the grant (`remote_info`) |
| 1 | Property Predator | the earning links panel | `GET /api/partner/grouphq/affiliate?email=` | `{ok: true, account_id, affiliate: false}` or `{ok: true, account_id, affiliate: true, code, link, referred_accounts, commission_due_pence, commission_paid_pence}` |
| 2 | Ordris | `affiliate` (`{"product": "ordris", "kind": "affiliate", "value": "partner"}`) | `POST /api/partner/grouphq/partners` `{email, name, external_ref: "grouphq:grant:<id>"}` | `{ok: true, partner_id, referral_code, link, created}`: an active partner in the programme `ORDRIS_MENTEE_PROGRAMME_CODE` names (the self-serve one by default), no Ordris login needed; `partner_id` becomes `remote_ref`, the code and link are kept on the grant |
| 2 | Ordris | the earning links panel | `GET /api/partner/grouphq/partners?email=` | `{ok: true, partner: false}` or `{ok: true, partner: true, partner_id, referral_code, link, referrals, pending_pence, payable_pence, paid_pence}` |
| 3 | Hello New Home | the recompute (after the partner seat) and the panel | `GET /api/partner/partners?email=` (as before) | the partner also carries `referral_code`, `referral_link`, `area` (`{slug, name}` or null), `tier`, `status`, `portal_url` and `admin_url` (the head-office page where the founder assigns the area). The recompute keeps them on the partner seat grant with `checked_at` and `needs_region` (found, area known and null, status active, tier not introducer), which the Needs a region queue lists without a live call. An answer with neither `area` nor `areas` is an HNH build without contract 3 and is never listed |
| 4 | Key Properties | `sourcer_link` (as before) | `POST /api/partner/sourcers` and `PATCH /api/partner/sourcers/{id}` both carry `pp_account_id` (the UUID string, as text) whenever Group HQ knows it | the portal sets it on the user row only while that is empty; a row linked to a different PP account is `409 {ok: false, reason: "pp_account_mismatch"}`. The `pp_account_id` the portal answers is kept on the grant |
| 4 | Key Properties | the earning links panel | `GET /api/partner/sourcers?email=` | the row also carries `fee_share_pct`, `active`, `pp_linked`, `login_url`, `deals_submitted`, `earned_pence`, `pipeline_pence`; the portal's `404 {ok: false, error: "not_found"}` is "no sourcer" |

**No revoke for an affiliate.** Both programmes are public and membership is permanent, so a revoke of an `affiliate`
grant (lapse, Revoke all, Pause, a template edit) is a recorded no-op (`noop: "kept: public programme"`) and nothing is
sent. The grant reads revoked in Group HQ; the code and link stay on it.

**A route that is not deployed yet.** Every contract refuses with `{ok: false, reason, message}`, so a `404` without
`ok: false` (FastAPI's `{"detail": "Not Found"}`, an HTML page) means the product has not deployed its side yet
(`base.route_missing`). On an `affiliate` grant that is a failed call that does not use up an attempt, so the grant is
asked again each night and never reaches "gave up" while Property Predator or Ordris deploys; the grant shows why. The
panel shows such a product as "couldn't reach", never as "no account" or "not enrolled". Deploying PP contract 1 and
Ordris contract 2 before Group HQ's 0008 is still the tidy order.

**Order inside one person's recompute.** Orphaned grants are revoked first, as before. Then, when the person will be
sent a Key Properties sourcer link and Group HQ does not know their Property Predator account yet, PP's account lookup
(`GET .../account?email=`, live PP only; a 404 is "no account", any other failure is a skipped `lookup` row on the run
page and the link goes without the id). Then every PP grant (whose answers also record the account id), then the KP
sourcer link, then everything else in template order. After the decisions, an active Hello New Home partner seat is
looked up (contract 3) and what HNH answers is kept on the grant; a failed lookup keeps the last answer and records
`check_error`.

**The Key Properties link.** An active sourcer link whose row has not confirmed the person's current PP account
(`remote_info.pp_account_id`, compared as text) is sent a `link` call, `PATCH /api/partner/sourcers/{id}`
`{pp_account_id}` and nothing else (the portal's PATCH takes the id on its own), under its own key
`grant:<id>:link:pp:<account id>`, so a mentee linked before Group HQ knew the id gets it. It never re-opens a sourcer
switched off by hand in the portal or resets a share set there. The link counts as done only when the portal answers
with that id: a `200` without it (a portal that drops the field) is a failed attempt (`entitlements.LINK_UNCONFIRMED`),
and so is a refusal such as the pre-contract-4 portal's `422 invalid` for a PATCH carrying the id alone; both are tried
again on the next runs up to five attempts, as is a transient failure, and none changes the grant's state. Once the
portal's contract-4 build is live, Retry on a grant that has stopped sends the link again at once. A `409 pp_account_mismatch` on any Key Properties grant or link call makes the grant
`failed` with a plain message (`entitlements.MISMATCH_ERROR`) and is never retried on its own: Retry on the grant once
the ids are put right. A grant that failed on its link call goes back to active on Retry and the same run sends the
link again. A mismatched grant whose sourcer the portal still holds active is still switched off when access ends.
A `409 pp_account_in_use` (that PP account is already linked to another Key Properties login) is an ordinary failure
with the portal's message.

**The seller.** Every person with an active mentee row and no seller gets a Group HQ seller of kind mentee
(`commission.ensure_mentee_seller`, audited `seller.create`), so they hold a franchise referral code; a person who is
already a seller, of any kind, is left alone. Statements stay held until self-billing is agreed.

**The earning links panel** (`GET /people/<id>/earnings?fragment=1[&mentee=<id>]`, loaded by the mentee and person pages
after they render) asks the four products at once, each within four seconds. A product is asked about the person only
when they hold or are due its earning right (a grant, or an item of a programme they are an active mentee on), hold an
affiliate grant in any state, or Group HQ already has their id there; anyone else is shown "not asked" and their email
goes to no product. A product in dry run is not asked, one
without a token or base URL is not configured, one that fails is "couldn't reach <product>", and where a product cannot
be asked the code and link the recompute kept are shown as last known. It writes nothing. Every link a product answers
is kept only when it is a plain `http(s)` URL.

## Rehearsing a connector

1. Keep `CONNECTORS_DRY_RUN=1`. Enrol a test person on the programme, record a cleared payment, press
   **Run nightly now** with the dry-run box ticked and read the planned calls on the report page.
2. Set the product's base URL and token on the web service in the Render dashboard and point the base URL at
   the product's preview branch; then add the token's `fromService` entry to the recompute cron in
   `render.yaml` and sync; only then switch the product to Live on the Settings screen. After the next nightly
   run its row on Settings shows the nightly job's token as Set.
3. Press **Run nightly now** with the box unticked for the test person only (from the grant's page).
   Check the product: the account is comped, the entitlement exists, the sourcer or partner is
   active. Check the grant's page: state active, remote ref filled, the audit row.
4. Mark the payment lapsed (or move the as-of date past the end) and run again. Check the product has
   taken the access away and the grant reads revoked.
5. Only then point the base URL at the live product.

The recorded fixtures in `tests/test_connectors.py` are the contract as Group HQ understands it, and
since 20 October 2026 they are the bodies the three product branches send (a flat row for KP,
`{ok, found, partner}` and the `partner` object for HNH, `{ok, revoked}` for an Ordris revoke). When a
product changes an endpoint, run its own tests against the same payloads and change both sides together.
