# Connectors

How Group HQ grants and revokes access in the four products, what each product's server-to-server
API looks like, and how far each contract has got. Written 20 October 2026.

| Product | Module | Contract | Status |
|---|---|---|---|
| Property Predator | `app/services/connectors/pp.py` | plan Phase 1, PP branch `feat/grouphq-partner` | **implemented here; the PP side goes through PP's audit lane before anything is live** |
| Ordris | `app/services/connectors/ordris.py` | plan Phase 2 | **contract pending**: the Ordris endpoints do not exist yet |
| Key Properties | `app/services/connectors/kp.py` | plan Phase 2 | **contract pending**: the sourcer endpoints do not exist yet |
| Hello New Home | `app/services/connectors/hnh.py` | plan Phase 2 | **contract pending**: `api_clients` and the partner endpoints do not exist yet |

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
- **Dry run**: `CONNECTORS_DRY_RUN=1` in the environment makes every run a dry run whatever the
  caller asks; a list of products (`CONNECTORS_DRY_RUN=ordris,kp,hnh`, the shipped value) keeps only
  those in dry run while the others go live; `0` sends to every product. The `--dry-run` flag and the
  page toggle make one run a dry run. Take a product off the list only once it has been rehearsed with
  a test account. A product with no base URL or token is skipped without counting an attempt.

## The outbox and the idempotency key

Every grant, extension and revoke the rule decides on becomes one `connector_calls` row before the
wire is touched:

| Key | When |
|---|---|
| `grant:<grant_id>:<ends_at>` | grant or extend to that end, for example `grant:12:2027-10-01T00:00:00` |
| `grant:<grant_id>:<ends_at>:after:<call_id>` | the same, once a revoke of the grant has succeeded (`call_id` is that revoke's row), so an undone Revoke all is sent again rather than matched to the row sent before the revoke |
| `grant:<grant_id>:revoke:<ends_at>` | revoke after that end passed, for example `grant:12:revoke:2027-10-01T00:00:00` |

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
the free tier, never on a tier they were not paying for. `account_id` in any answer fills
`people.pp_account_id` when it is empty. Asks of the PP endpoint, so the mapping above holds: send
`reason` codes `paid_subscription` (409), `no_account` (404) and `not_on_comp` (409) rather than
prose, and keep the hourly `expire_comps` reverting on its own so a Group HQ outage never makes a
gift permanent.

## Ordris (contract pending)

Base URL `ORDRIS_BASE_URL` (no default: set it when the Render move lands). Token
`ORDRIS_PARTNER_TOKEN`, which on the Ordris side is `ORDRIS_PARTNER_TOKEN_GROUPHQ` compared with
`crypto.timingSafeEqual`.

| Group HQ grant | Call |
|---|---|
| `org_plan` with value `starter`, `pro` or `enterprise` | `POST /api/partner/grouphq/entitlements` `{email, plan, until, reason, external_ref: "grouphq:grant:<id>"}`; the answer may carry `user_id` and `org_id`, stored on the person |
| revoke | `DELETE /api/partner/grouphq/entitlements/grouphq%3Agrant%3A<id>`; a 404 is treated as done |
| lookup | `GET /api/partner/grouphq/lookup?email=` |

Ordris resolves the Clerk user by email and applies the plan to the organisation `requireAuth` would
pick (`orgId ?? userId`). `external_ref` is unique on their side, so a repeated POST updates rather
than duplicates. The plan's `requireActivePlan` must treat an active entitlement like a paid
subscription, which also closes the browser-only gate.

## Key Properties (contract pending)

Base URL `KP_BASE_URL` (live: `https://portal.keypropertiesuk.co.uk`). Token `KP_PARTNER_TOKEN`,
which on the portal side is `GROUPHQ_PARTNER_TOKEN` once `src/routes/partner.js` keys tokens by
client.

| Group HQ grant | Call |
|---|---|
| `sourcer_link` with value `mentee` (the portal's `users.source`) | `POST /api/partner/sourcers` `{email, name, company, pp_account_id, fee_share_pct?, source: "mentee"}`: creates the sourcer, or links the invite-only user with that email to the PP account; the answer's `id` becomes `remote_ref` and `people.kp_user_id` |
| grant when `remote_ref` is known | `PATCH /api/partner/sourcers/{id}` `{active: true}` |
| revoke | `PATCH /api/partner/sourcers/{id}` `{active: false}`; without a `remote_ref` the id is looked up by email first; no match is treated as done |
| lookup | `GET /api/partner/sourcers?email=` |

`fee_share_pct` is sent only when the programme template item carries it (`{"product": "kp", "kind":
"sourcer_link", "value": "mentee", "fee_share_pct": 50}`, validated 0 to 100 on the programme screen);
until Martin signs the mentee figure off the portal's own default (50) applies. `company` is the first organisation linked to
the person in Group HQ, or blank.

## Hello New Home (contract pending)

Base URL `HNH_BASE_URL` (live: `https://hellonewhome.co.uk`). Token `HNH_PARTNER_TOKEN`, which on
the HNH side is an `api_clients` row (hashed token, scope `partners`) checked by
`require_api_client` with `hmac.compare_digest`; any `Origin` is rejected.

| Group HQ grant | Call |
|---|---|
| `partner_seat` with value `area` or `introducer` | `POST /api/partner/partners` `{email, name, tier, status: "active", create_login: true, franchise: {fee_waived: true, reason}, area_slug | postcodes[]}`; the answer's `id` (and `user_id`) become `remote_ref`, `people.hnh_partner_id` and `people.hnh_user_id` |
| grant when `remote_ref` is known | `POST /api/partner/partners/{id}/status` `{status: "active", reason}` |
| revoke | `POST /api/partner/partners/{id}/status` `{status: "paused", reason}`: the partner and their area stay on record; without a `remote_ref` the id is looked up by email first |
| lookup | `GET /api/partner/partners?email=` |

`area_slug` or `postcodes` (a list of outward codes) and `fee_waived` come from the programme
template item (`{"product": "hnh", "kind": "partner_seat", "value": "area", "area_slug": "swansea"}`,
validated on the programme screen) when the territory has been decided;
without one HNH creates the partner with no area and head office assigns it by hand. Hello New Home
is a separate company (Hello New Home 2 Ltd): this call moves a name and an email between two data
controllers, so it stays in dry run until the intra-group data sharing agreement and consent wording
exist (plan, Phase 3).

## Rehearsing a connector

1. Keep `CONNECTORS_DRY_RUN=1`. Enrol a test person on the programme, record a cleared payment, press
   **Run nightly now** with the dry-run box ticked and read the planned calls on the report page.
2. Set the product's base URL and token in the Render dashboard, point the base URL at the product's
   preview branch, and take the product off the `CONNECTORS_DRY_RUN` list.
3. Press **Run nightly now** with the box unticked for the test person only (from the grant's page).
   Check the product: the account is comped, the entitlement exists, the sourcer or partner is
   active. Check the grant's page: state active, remote ref filled, the audit row.
4. Mark the payment lapsed (or move the as-of date past the end) and run again. Check the product has
   taken the access away and the grant reads revoked.
5. Only then point the base URL at the live product.

The recorded fixtures in `tests/test_connectors.py` are the contract as Group HQ understands it; when a
product's endpoint lands, run its own tests against the same payloads and change both sides together.
