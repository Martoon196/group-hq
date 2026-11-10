# Operator API: Group HQ over MCP for Claude

Group HQ speaks the Model Context Protocol at `https://group-hq.onrender.com/mcp`, so Claude (a claude.ai
custom connector, Claude Desktop or Claude Code) can read what the screens show and, only when a deployment
switches it on, make the changes the admin screens make (CRM, people, mentees, grants, LAPS, property deals,
programmes, territories, franchise and sellers). Every call acts as one named admin and is logged; nothing here
sends a message to a person, and no password hash, authenticator secret, product token or credential is ever
returned.

The server is Streamable HTTP, stateless and JSON only: `POST /mcp` takes JSON-RPC 2.0 (one request or a
batch of up to 20), notifications get `202`, and `GET /mcp` is `405`. It is mounted only when at least one of
the two credentials below is validly set; otherwise the paths do not exist.

## Two ways to connect

**claude.ai custom connector (OAuth).** In claude.ai add a custom connector with the URL
`https://group-hq.onrender.com/mcp`. claude.ai discovers the authorization server from
`/.well-known/oauth-protected-resource`, registers itself (`POST /oauth/register`) and opens
`/oauth/authorize` in your browser. Sign in to Group HQ as usual (password and authenticator code; the login
brings you back to the consent page), read the consent page and press **Allow**. claude.ai then exchanges the
code with PKCE at `POST /oauth/token`. Needs `GHQ_OPERATOR_OAUTH_SECRET`.

**Claude Code or Claude Desktop (static bearer).** Needs `GHQ_OPERATOR_TOKEN` and `GHQ_OPERATOR_USER_EMAIL`:

```sh
claude mcp add --transport http group-hq https://group-hq.onrender.com/mcp \
  --header "Authorization: Bearer <the GHQ_OPERATOR_TOKEN value>"
```

A request with no acceptable bearer gets `401 {"error": "unauthorised"}` with
`WWW-Authenticate: Bearer realm="Group HQ", resource_metadata="https://group-hq.onrender.com/.well-known/oauth-protected-resource"`.

## Settings (web service only; names, never values)

| Variable | What it does |
| --- | --- |
| `GHQ_OPERATOR_TOKEN` | The static bearer for Claude Code / Desktop. Valid only at 32-256 characters. Shared with nothing else. |
| `GHQ_OPERATOR_USER_EMAIL` | The admin a static-token call acts as. Must be an existing, active admin (matched case-insensitively), or every static-token call is refused. |
| `GHQ_OPERATOR_OAUTH_SECRET` | Signs the OAuth client ids, codes, access and refresh tokens. Valid only at 32-256 characters. Shared with nothing else. |
| `GHQ_OPERATOR_WRITES` | `enabled` offers the write tools. Anything else (the default) is read only. |

`render.yaml` lists the four on the web service only; none is copied to a cron. Generate each secret with
something like `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Tools

Read tools, always offered:

| Tool | Returns |
| --- | --- |
| `group_hq_overview` | The dashboard's counts (people, mentees, grants, connector calls, programmes), CRM leads and plans, calls due, open LAPS and property deals, and the latest audit entries. |
| `crm_leads` (`limit`, `query`) | Property Predator leads synced into Group HQ with each one's plan state. |
| `crm_lead` (`lead_id`) | One lead with its plan, the plan's actions and the calls recorded. |
| `people_search` (`query`, `limit`) | The people register by email or name, with linked product ids. |
| `mentees_list` (`limit`, `query`, `status`) | Mentees with person and programme. |
| `grants_list` (`limit`, `state`, `product`) | Entitlement grants. |
| `laps_board` (`brand`) | The LAPS board's open columns, counts and score. |
| `acquisitions_list` (`limit`, `stage`) | Property deals, open by default. |
| `webinars_list` (`brand`) | Both brands' webinars, read live from Relaunch72. |
| `settings_get` | The non-secret settings and each product's Live / Dry run mode. |

Write tools, offered only while `GHQ_OPERATOR_WRITES=enabled` (otherwise `tools/list` omits them and a call
answers "writes are disabled on this deployment"). Each one runs the screen's own code path, never a copy of its rules:
the same validation, the same audit rows naming the admin, the same product-connector calls. Where a route held its
logic inline, that logic now lives in a function the route and the tool both call (`app.routers.people.create_person`,
`app.routers.mentees.record_payment`, `app.routers.grants.run_retry` and so on, each raising
`app.templating.Refused` with the screen's sentence); a route that is a plain function of its form fields answering
`go(path, ok=, err=)` is pressed directly and its flash read back. A refusal comes back as the screen's own sentence
with `isError: true` and nothing changed. Every successful write call also leaves one `operator.tool` audit row naming
the admin. A result is `{ok, message, ...ids}`, where `message` is the screen's flash.

Tools that edit a record (`*_update`, `*_edit_deal`, `laps_set_targets`, `people_set_links` and the like) take only the
fields to change: a field left out keeps its current value, an empty string clears it. A tool marked **live access**
changes what the person can do in a product: it is sent to the product live unless that product is in Dry run on
Settings (or the deployment forces dry run); a one-time Hello New Home password such a call can produce is shown only
on the screen and never returned here. Money tools only write Group HQ's register: no card is charged and no money
moves.

**CRM** (`/crm`, through `app.services.campaigns`):

| Tool | Does |
| --- | --- |
| `crm_start_plan` (`lead_id`) | Starts the first-conversation follow-up plan. Plans only; nothing is sent. |
| `crm_hold_plan` (`plan_id`, `reason`) | Holds an active plan. |
| `crm_resume_plan` (`plan_id`) | Resumes a held plan. |
| `crm_record_call` (`action_id`, `outcome`) | Records a call outcome on an active plan. |
| `crm_sync_property_predator` (`cursor`) | Pulls one page of Property Predator leads from Relaunch72. |

**People** (`/people`):

| Tool | Does |
| --- | --- |
| `people_create` (`email`, `name`, `phone`, `notes`) | Adds a person to the register, keyed on the email. |
| `people_update` (`person_id`, `email`, `name`, `phone`, `notes`) | Edits a person; the email cannot change while they hold a live grant. |
| `people_set_links` (`person_id`, `pp_account_id`, `ordris_user_id`, `ordris_org_id`, `kp_user_id`, `kp_investor_id`, `hnh_partner_id`, `hnh_user_id`) | Sets the linked product ids by hand (Group HQ's record only). |

**Mentees** (`/mentees`):

| Tool | Does |
| --- | --- |
| `mentee_create` (`person_id`, `programme_id`, `payment_mode`, `start_date`, `cohort`, `notes`) | Enrols a person on an active programme. |
| `mentee_record_payment` (`mentee_id`, `amount`, `paid_at`, `cleared_at`, `method`, `period_start`, `period_end`, `reference`, `notes`) | Records a payment received. No card is charged. |
| `mentee_clear_payment` (`mentee_id`, `payment_id`, `cleared_at`) | Marks a payment cleared, so it counts towards access. |
| `mentee_recompute` (`mentee_id`) | Recomputes the person's grants. **Live access.** |
| `mentee_revoke_all` (`mentee_id`, `reason`) | Revokes every grant and ends the mentee rows. **Live access.** |
| `mentee_pause_all` (`mentee_id`, `reason`) | Pauses all access until resumed. **Live access.** |
| `mentee_resume_all` (`mentee_id`) | Resumes all access and recomputes. **Live access.** |
| `mentee_update` (`mentee_id`, `status`, `cohort`, `payment_mode`, `start_date`, `notes`) | Edits the mentee's details (lapsed is never set by hand). |

**Grants** (`/grants`):

| Tool | Does |
| --- | --- |
| `grant_recompute_all` (`dry_run`, `today`) | Runs the nightly recompute now for everyone. **Live access** unless `dry_run`. |
| `grant_recompute_person` (`grant_id`, `dry_run`, `today`) | Recomputes the grant's person. **Live access** unless `dry_run`. |
| `grant_retry` (`grant_id`, `dry_run`) | Clears a failed or blocked grant and recomputes. **Live access** unless `dry_run`. |
| `grant_pause` (`grant_id`, `reason`) | Pauses one grant: revoked now, kept off. **Live access.** |
| `grant_resume` (`grant_id`) | Resumes a paused grant and recomputes. **Live access.** |

**LAPS board** (`/laps`):

| Tool | Does |
| --- | --- |
| `laps_refresh` | Pulls every business's CRM leads onto the board (reads only). |
| `laps_create_deals` (`name`, `brands`, `contact`, `offer`, `source`, `value`, `next_step`, `next_step_on`, `notes`, `arrived`, `stage`) | Adds a lead, one linked card per business. |
| `laps_add_business` (`deal_id`, `brands`) | Adds the same person as a lead for another business. |
| `laps_edit_deal` (`deal_id`, `name`, `contact`, `brand`, `offer`, `source`, `value`, `next_step`, `next_step_on`, `notes`) | Edits a card. |
| `laps_move_deal` (`deal_id`, `to`, `value`) | Moves a card (appointment, presentation, sale, lead, lost, reopen). |
| `laps_set_targets` (`monthly_goal`, `typical_sale`) | Sets the income goal. |

**Property deals** (`/acquisitions`):

| Tool | Does |
| --- | --- |
| `acquisition_create` (the deal form's fields, `arrived`, `stage`, `thread_ticket`) | Adds a deal, attached to an Inbox conversation when a ticket is given. |
| `acquisition_edit` (`deal_id`, the deal form's fields) | Edits a deal. |
| `acquisition_move` (`deal_id`, `to`, `price`, `lost_reason`) | Moves a deal, marks it lost or reopens it. |
| `acquisition_toggle_check` (`deal_id`, `key`) | Ticks (or unticks) a checklist item. |
| `acquisition_link` (`deal_id`, `thread_ticket`) | Attaches an Inbox conversation (the ticket the Inbox carries for this admin). |
| `acquisition_unlink` (`deal_id`, `account_key`, `thread_sha256`) | Removes a conversation from a deal. |

**Programmes, territories, franchise and sellers**:

| Tool | Does |
| --- | --- |
| `programme_update` (`programme_id`, `name`, `description`, `active`, `grant_template`, `price`) | Edits a programme and its grant template. |
| `territory_reserve` (`district`, `partner_id` or `person_id`, `reserved_until`) | Reserves a district for a Hello New Home partner. |
| `territory_assign` (`district`, `partner_id` or `person_id`) | Makes it a partner's core district. |
| `territory_release` (`district`) | Releases it back to open. |
| `territory_keep_for_hq` (`district`) | Keeps it for head office. |
| `franchise_create` (`person_id` or `email`/`name`/`phone`, `districts`, `seller_code`, `source`, `fee_waived`, `notes`) | Creates an opportunity with its quote. |
| `franchise_move_stage` (`opportunity_id`, `stage`, `note`, `override`, `paid_at`) | Moves the stage (pack sent creates the follow-up tasks; nothing is emailed). |
| `franchise_requote` (`opportunity_id`, `fee_waived`) | Recomputes the quote. |
| `franchise_update_details` (`opportunity_id`, `notes`, `source`, `seller_code`) | Edits notes, source and seller attribution. |
| `franchise_add_activity` (`opportunity_id`, `kind`, `body`) | Records a note, call or email on the timeline (nothing is sent). |
| `franchise_task_done` / `franchise_task_skip` (`opportunity_id`, `task_id`) | Marks a follow-up task done or skipped (nothing is sent). |
| `franchise_record_payment` (`opportunity_id`, `basis`, `trail_month`, `amount`, `paid_at`, `cleared_at`, `method`, `reference`, `notes`) | Records a fee received. No card is charged. |
| `franchise_clear_payment` (`opportunity_id`, `payment_id`, `cleared_at`) | Marks a fee cleared; seller commission accrues. |
| `franchise_refund_payment` (`opportunity_id`, `payment_id`, `reason`, `refunded_at`) | Marks a fee refunded; commission is clawed back inside 90 days. No money moves. |
| `franchise_create_hnh_partner` (`opportunity_id`, `dry_run`) | Creates the Local Partner in Hello New Home. **Live access** unless Dry run. |
| `seller_create` (`person_id` or `email`/`name`/`phone`, `kind`, `code`, `bank_reference`, `self_billing`, `self_billing_date`, `active`) | Creates a seller. |
| `seller_update` (`seller_id`, `kind`, `code`, `bank_reference`, `self_billing`, `self_billing_date`, `active`) | Edits a seller. |
| `seller_clawback_entry` (`seller_id`, `entry_id`, `reason`) | Claws back one commission entry by hand. |
| `seller_generate_statements` (`period`) | Generates a month's self-billing statements. |
| `seller_mark_statement_paid` (`statement_id`, `paid_at`) | Records that a statement was paid (no money moves here). |
| `seller_confirm_rule` (`rule_id`) | Records the founder's sign-off of a commission rule. |

Left out on purpose (no tool, and none is to be added without a decision):

| Action | Why |
| --- | --- |
| Franchise follow-up email (`POST /franchise/{id}/tasks/{tid}/send`), the Inbox and personal inbox (replies, sends, automations), comments (LinkedIn, video comments), WhatsApp keywords | Each sends an email, SMS, WhatsApp, LinkedIn or other message to a person. |
| Everything on `/settings`: a product's Live / Dry run switch, admin users, passwords, the authenticator, the audit screens | These decide what the operator itself may do and who can sign in; they stay with the founder at the screen. |
| Bulk deletes | Not reversible from a sentence. |
| Mentee import (`/mentees/import`) | A bulk import of existing users that grants access in every product at once. |
| LAPS Start (`POST /laps/start`) and property-deal Start (`POST /acquisitions/start`) | They only open the add form pre-filled; nothing is saved. `laps_create_deals` and `acquisition_create` do the saving. |

Every result is capped (100 rows a list, 40 a LAPS column). A tool failure comes back as a sentence with
`isError: true`, never a stack trace. The log line per call (`grouphq.operator`) carries the tool name, the
acting admin and ok / error, never the arguments or the result.

## How the OAuth server works

Registration is unauthenticated, so a **client id** is not stored: it is a signed description (HMAC-SHA256 with
`GHQ_OPERATOR_OAUTH_SECRET`, the byte format of the Relaunch72 sign-in handoff) of the redirect URIs the client
registered (`https://` anywhere, or `http://` on `127.0.0.1` / `localhost` for a local MCP client) and its name.

Codes and tokens are opaque random values; only their SHA-256 hashes live in Postgres
(`alembic/versions/0017_operator_oauth.py`, `app/services/operator_oauth_store.py`):

- a **code** remembers the client, the exact redirect URI, the PKCE S256 challenge and the admin, lives two
  minutes and is redeemed once **across every instance**: redemption locks its row, and a second presentation
  revokes the connection the first one created;
- each redemption creates a **connection** (grant) with a one-hour **access token** and a 30-day **refresh
  token**, both bound to that client;
- a refresh **rotates**: the presented refresh token is spent and a new pair is issued. Presenting a spent
  refresh token again, or presenting one with another client's id, revokes the whole connection;
- `/mcp` checks the access token against the table on every call, so a revoked connection or a deactivated
  admin stops at once.

Only `/oauth/authorize` and `/oauth/connections` need the admin's session. An unregistered redirect URI, a forged
client id or a bad PKCE challenge gets a 400 page and never a redirect. The consent and revoke POSTs are guarded by
the app's origin check and a form token bound to the session cookie.

## Revoking access, and the honest limits

- **Connected apps** (`/oauth/connections`, linked from the consent page) lists each connection with Revoke.
- **RFC 7009**: a client can end its own connection at `POST /oauth/revoke`.
- **Deactivate the admin**: every token naming them stops on its next call.
- **Rotate `GHQ_OPERATOR_OAUTH_SECRET`**: every registered client id stops verifying, so every connector must
  register and be allowed again.
- **Clear `GHQ_OPERATOR_TOKEN`** (or set a new one): static access stops.
- **Set `GHQ_OPERATOR_WRITES`** to anything but `enabled`: the write tools disappear on the next call.

A network retry of a refresh whose answer was lost presents the spent token again and ends the connection; the
connector then asks for consent once more. That is deliberate: it is indistinguishable from a stolen token. The
consent page is the only place a human is asked; after Allow, anything the connector's Claude does runs as that
admin until the connection is revoked or its refresh token expires.

Rolling back: a build without `0017_operator_oauth` refuses to boot on a database that has it. Roll back to
`main` plus only that migration file (the prepared recovery build), never to an older commit.

Checks without a database: `python -m unittest discover -s unit_checks -p '*.py' -v` (the `operator_*` modules cover
client ids, the OAuth parameters and the MCP handshake and gate). The exchange, rotation, replay and revocation run
against Postgres in `tests/test_operator_oauth.py`, including two sessions racing one code. Every screen write tool has
a happy path and a refusal through `POST /mcp` in `tests/test_operator_writes.py`, which also checks they are hidden
while writes are disabled.
