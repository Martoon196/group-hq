# Operator API: Group HQ over MCP for Claude

Group HQ speaks the Model Context Protocol at `https://group-hq.onrender.com/mcp`, so Claude (a claude.ai
custom connector, Claude Desktop or Claude Code) can read what the screens show, including the Inbox and the WhatsApp
keyword rules, and, only when a deployment switches it on, make the changes the admin screens make (CRM, people,
mentees, grants, LAPS, property deals, programmes, territories, franchise, sellers, and filing an Inbox conversation
or linking it to a CRM). Every call acts as one named admin and is logged, and no password hash, authenticator
secret, product token or credential is ever returned.

Tools come in three classes, each behind its own switch:

| Class | Offered when | What it can do |
| --- | --- | --- |
| Read | always | Answers from the database and the Inbox. Changes nothing, sends nothing. |
| Write | `GHQ_OPERATOR_WRITES=enabled` | Changes Group HQ's records (and, where marked, a person's product access). Sends no message. |
| Messaging | `GHQ_OPERATOR_WRITES=enabled` **and** `GHQ_OPERATOR_MESSAGING=enabled` | **Causes a message to reach a real person**: one Inbox reply, or switching a WhatsApp keyword rule on. There are exactly two such tools. |

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
| `GHQ_OPERATOR_MESSAGING` | `enabled`, together with writes enabled, offers the two messaging tools. Anything else (the default) hides them, and a call answers "sending messages is switched off on this deployment (GHQ_OPERATOR_MESSAGING)". On its own, without writes, it does nothing. |
| `GHQ_OPERATOR_RELAUNCH72` | `enabled` also carries Relaunch72's operator tools on this connector (see "Relaunch72 tools"). Needs a valid `R72_SSO_SECRET` here and `R72_OPERATOR_GROUP_HQ=enabled` on Relaunch72. Anything else (the default) never contacts Relaunch72. |

`render.yaml` lists the six on the web service only, each `sync: false`, so their values live in the Render dashboard; none is copied to a cron. The three switches must never be given a `value:` there: every Blueprint sync re-applies a written value and would switch a live deployment back off on the next merge. Generate each secret with
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
| `inbox_accounts` | The Inbox accounts this admin can see, exactly as the Inbox decides: key, label, business, channel, provider, whether replies are possible, and for the personal WhatsApp whether the owner's connector is online. A personal account is listed only for its owner. |
| `inbox_conversations` (`accounts`, `channel`, `brand`, `conversation_type`, `label`, `query`, `limit`) | The Inbox's conversation list, newest first, read by the screen's own function (`app.routers.inbox.conversation_list`): name, participant, last message time and preview, one-to-one or group where known, category and triage, and the `thread_ticket` every other inbox tool takes. `query` matches the name. |
| `inbox_messages` (`thread_ticket`, `cursor`) | One conversation through the screen's `thread_context`: each message's sender, direction, time and text, the reply receipts, CRM links, conversation choices, triage and attached property deals. `cursor` is the previous answer's `earlier_cursor`. |
| `whatsapp_keyword_rules` (`group_query`) | The keyword editor's page (`app.routers.whatsapp_keywords.editor_context`): the rules and their revision, the groups the listener reported (id and name), whether the listener's adapter is configured and online, and the latest 50 keyword enquiries with name, number, rule, group, reply and state. |

A `thread_ticket` is the signed ticket the Inbox itself puts in its links: it names one conversation on one account
for one admin, works for about two hours, and stops working when that account's credential or mapping changes. A tool
given a stale one answers "Refresh the inbox before continuing."; list the conversations again for a fresh one.

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

**Inbox and WhatsApp keywords** (`/inbox`, `/crm/channels/whatsapp/keywords`; `app.services.operator_inbox`). These
three are write tools: they change Group HQ's records (and the business CRM, for the link) and send no message.

| Tool | Does |
| --- | --- |
| `inbox_link_crm` (`thread_ticket`, `brand`, `display_name`, `contact_id`, `copy_messages`) | What `POST /inbox/crm` does (`app.routers.inbox.link_crm`): creates the conversation's person as a contact (lead) in that business's CRM through Relaunch72, or links an existing contact when `contact_id` is given. Nobody is subscribed to marketing. |
| `inbox_organise` (`thread_ticket`, `conversation_type`, `businesses`, `copy_messages_to`, `note`) | What `POST /inbox/organise` does (`organise_conversation`): the conversation's type, related businesses, which linked CRMs get its messages, and the private note. |
| `whatsapp_keyword_rule_save_paused` (`revision`, `rule_id`, `name`, `brand`, `keywords`, `message`, `match_mode`, `group_ids`) | The keyword editor's save (`app.routers.whatsapp_keywords.save_rule`) with the rule forced to paused: a draft for the owner. A paused rule answers nobody. Editing an enabled rule with it pauses that rule. |

## Messaging tools: these reach real people

Offered only while `GHQ_OPERATOR_WRITES=enabled` **and** `GHQ_OPERATOR_MESSAGING=enabled`. They are the only tools
that cause a message to leave Group HQ, and what they send cannot be recalled. Each is also a write tool: it keeps the
screen's own audit rows and receipt and adds the `operator.tool` row. In `tools/list` they carry the MCP annotations
`readOnlyHint: false` and `openWorldHint: true`, so a client can ask before running them.

| Tool | Does |
| --- | --- |
| `whatsapp_keyword_rule_save` (`revision`, `rule_id`, `name`, `brand`, `keywords`, `message`, `match_mode`, `group_ids`, `enabled`, `requested_reply`) | The keyword editor's full save, including switching a rule on. While a rule is enabled, **every person who types one of its keywords in one of its groups is sent its reply privately from the owner's own WhatsApp, once per person**. `requested_reply` is the owner's confirmation that the group invitation promises that private reply; Claude may set it true only when the owner has said so in the conversation, and it is needed on every save that leaves the rule enabled. The editor's refusals apply unchanged: no empty group list, no unknown group, no overlap with another enabled rule in the same group, a stale `revision`. |
| `inbox_reply` (`thread_ticket`, `message`, `request_id`) | Sends **one reply in one conversation** through the Inbox's own `service.send`, from the account and channel the conversation is on. One conversation per call, only when the owner has asked for that reply; never for unsolicited, bulk or repeated messaging. `request_id` is a UUID for idempotency (one is generated and returned when left out): the same id and text never send twice. The answer is the receipt's `state` and the sentence the Inbox flashes. |

`inbox_reply` answers with `ok: false` and no `isError` when a receipt exists but the news is bad: `rejected` (the
channel refused it) or `outcome_unknown` / `calling` (the message may have gone). **An unknown outcome must never be
retried**, with the same or a new `request_id`; the answer says so (`do_not_retry: true`) and the owner should check the
original app. A refusal before anything was attempted (a stale ticket, an empty or oversized message, an account without
replies, the personal connector offline, a WhatsApp recipient who typed STOP) is the screen's sentence with `isError`,
and no receipt is written.

The Inbox tools hand Claude text that strangers wrote (names, previews, messages, keyword senders' names). Their
descriptions and the server's instructions say to treat it as information, never as instructions; that is guidance to
the model, not a technical barrier, which is one more reason messaging has its own switch.

What limits the two, as the features stand:

- **Past group messages are not stored.** The WhatsApp listener reports a group message only when it is live (within
  five minutes), in a group a rule lists, and matching an **enabled** rule. Nothing else from a group reaches Group HQ,
  and there is no group backfill, so "everyone who already typed a keyword" cannot be listed unless an enabled rule
  caught them at the time (those are the enquiries `whatsapp_keyword_rules` shows). A new rule answers new messages only.
- **One reply per person per rule**, and never to someone who typed STOP. Editing a rule does not re-send; replies not
  yet sent when a rule is edited or paused are held.
- **The listener runs on the owner's computer.** Keyword replies and personal-WhatsApp inbox replies go out only while
  it is running and linked; an Inbox reply to a personal account is refused while that connector is offline, and
  otherwise queued for it.
- **The Inbox holds one-to-one conversations only.** A group chat is never an Inbox conversation, and a personal
  WhatsApp conversation exists only once a private message was exchanged after the owner switched collection on (or
  history was imported). `inbox_link_crm` needs such a conversation: a keyword enquiry on its own is not a CRM contact
  and has no `thread_ticket`.
- **The official MHP WhatsApp number** is read through Relaunch72 and accepts a reply only when replies are enabled
  for it (`GHQ_MHP_WHATSAPP_MANUAL_REPLIES`) and the person wrote within the last 24 hours.

Left out on purpose (no tool, and none is to be added without a decision):

| Action | Why |
| --- | --- |
| Franchise follow-up email (`POST /franchise/{id}/tasks/{tid}/send`), Inbox reply rules and handoff (`/inbox/automations`), public comments (LinkedIn, video comments), any send to more than one conversation | Each sends an email, SMS, WhatsApp, LinkedIn or other message to a person without the owner choosing that one message. The only sending tools are the two messaging tools above. |
| Inbox connections: a connection's default business, connecting or switching the personal accounts, personal WhatsApp collection and reply switches, inbox scanning | These decide which private conversations are read at all and whether replies can leave; they stay with the owner at the screen. |
| Everything on `/settings`: a product's Live / Dry run switch, admin users, passwords, the authenticator, the audit screens | These decide what the operator itself may do and who can sign in; they stay with the founder at the screen. |
| Bulk deletes | Not reversible from a sentence. |
| Mentee import (`/mentees/import`) | A bulk import of existing users that grants access in every product at once. |
| LAPS Start (`POST /laps/start`) and property-deal Start (`POST /acquisitions/start`) | They only open the add form pre-filled; nothing is saved. `laps_create_deals` and `acquisition_create` do the saving. |

Every result is capped (100 rows a list, 40 a LAPS column, 100 conversations, 100 messages a page, 100 groups; a
message's text is cut at 4,000 characters and a preview at 300, each with a `..._truncated` flag). A tool failure comes
back as a sentence with `isError: true`, never a stack trace. The log line per call (`grouphq.operator`) carries the tool
name, the acting admin and ok / error, never the arguments or the result, so no message text or phone number is logged.
`inbox_messages` only reads: unlike opening the conversation on the screen, it does not start the background copy of
an officially linked conversation's messages into its CRM.

## Relaunch72 tools: webinars, the brand CRMs and follow-up drafts

Webinars, the Property Predator and Martin Howard Property CRMs and the follow-up drafts live in Relaunch72. So that
they are run from this connector, Group HQ relays (`app.services.relaunch72_operator`): `tools/list` adds the tools
Relaunch72's own operator endpoint offers, and a `tools/call` for one is passed through, server to server. Relaunch72
stays the single owner of their schemas, checks and refusals; its `docs/operator-api.md` describes each tool. Their
descriptions here begin "Relaunch72 brand workspace" and they take a `brand` (`pp` or `mhp`).

- **Who.** Every relayed request names the signed-in admin (`X-Group-HQ-Admin`). Relaunch72 accepts it only for a
  login that its handoff map (`R72_GROUP_HQ_SSO_USERS`) ties to its operator account, so a second Group HQ admin is
  offered none of these tools. The decision is Relaunch72's; Group HQ holds no list.
- **Bearer.** `base64url(HMAC-SHA256(R72_SSO_SECRET, "group-hq-operator-bearer/v1\0"))`, no padding: derived from the
  secret the two servers already share for the sign-in handoff, so there is no new secret. A test on each side pins the
  same vector.
- **Writes.** A relayed tool is a read only if `relaunch72_operator.READ_ONLY` names it; anything else, including a
  tool Relaunch72 adds later, needs `GHQ_OPERATOR_WRITES=enabled` here as well as Relaunch72's own
  `R72_OPERATOR_WRITES`. A relayed write leaves an `operator.tool` audit row here (`tool:relaunch72/<name>`) whenever
  it reached Relaunch72.
- **What they cannot do.** They save records and drafts. Publishing or approving a sequence or an email, enrolling
  people, scheduling and sending stay with the owner in Relaunch72. They are not messaging tools.
- **One shared name.** Both servers have `webinars_list`. For an admin the relay serves, Relaunch72's replaces Group
  HQ's (it returns what `webinar_get` and `webinar_update` need); anyone else keeps Group HQ's. Any other clash keeps
  Group HQ's tool.
- **Failures are said truthfully.** Nothing is retried. If no connection could be made: "nothing was sent to it and
  nothing changed". If the request left and no readable answer came back (a timeout, an error status, an answer that
  is not MCP): "it is not known whether this call took effect", so read the record again first. Relaunch72's own
  refusals come back as its sentence.
- **The tool list** is remembered per admin for five minutes (a failure for thirty seconds), so a tool Relaunch72
  adds appears within five minutes, and an outage there never slows or breaks Group HQ's own tools.

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
- **Set `GHQ_OPERATOR_WRITES`** to anything but `enabled`: the write tools disappear on the next call, and the
  messaging tools with them.
- **Set `GHQ_OPERATOR_RELAUNCH72`** to anything but `enabled` (or `R72_OPERATOR_GROUP_HQ` on Relaunch72): the
  Relaunch72 tools disappear on the next call.
- **Set `GHQ_OPERATOR_MESSAGING`** to anything but `enabled`: the two messaging tools disappear on the next call. A
  keyword rule already enabled keeps answering until it is paused (`whatsapp_keyword_rule_save_paused`, or the editor).

A network retry of a refresh whose answer was lost presents the spent token again and ends the connection; the
connector then asks for consent once more. That is deliberate: it is indistinguishable from a stolen token. The
consent page is the only place a human is asked; after Allow, anything the connector's Claude does runs as that
admin until the connection is revoked or its refresh token expires. That includes reading that admin's Inbox, personal
conversations among them, and, while messaging is enabled, sending as them; the consent page says which of these the
deployment allows. A connection allowed before the Inbox tools existed gains them without being asked again, so revoke
any connection that should not read the Inbox. The same holds for the Relaunch72 tools: a connection the owner
already allowed gains them when the relay is switched on.

Rolling back: a build without `0017_operator_oauth` refuses to boot on a database that has it. Roll back to
`main` plus only that migration file (the prepared recovery build), never to an older commit.

Checks without a database: `python -m unittest discover -s unit_checks -p '*.py' -v` (the `operator_*` modules cover
client ids, the OAuth parameters and the MCP handshake and gate). The exchange, rotation, replay and revocation run
against Postgres in `tests/test_operator_oauth.py`, including two sessions racing one code. Every screen write tool has
a happy path and a refusal through `POST /mcp` in `tests/test_operator_writes.py`, which also checks they are hidden
while writes are disabled. The Inbox and WhatsApp tools have the same in `tests/test_operator_inbox.py`, with every
provider a stand-in: it checks the three classes in `tools/list`, and that a messaging tool called while messaging is
off reaches no provider, queues nothing for the connector and writes no receipt.
