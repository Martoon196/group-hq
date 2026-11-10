# Operator API: Group HQ over MCP for Claude

Group HQ speaks the Model Context Protocol at `https://group-hq.onrender.com/mcp`, so Claude (a claude.ai
custom connector, Claude Desktop or Claude Code) can read what the screens show and, only when a deployment
switches it on, make the same CRM follow-up changes the `/crm` page makes. Every call acts as one named admin
and is logged; nothing here sends a message to a lead, and no password hash, authenticator secret, product
token or credential is ever returned.

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
answers "writes are disabled on this deployment"). Each goes through `app.services.campaigns` exactly as the
`/crm` buttons do, with the same rules and audit rows, plus one `operator.tool` audit row naming the admin:

| Tool | Does |
| --- | --- |
| `crm_start_plan` (`lead_id`) | Starts the first-conversation follow-up plan. Plans only; nothing is sent. |
| `crm_hold_plan` (`plan_id`, `reason`) | Holds an active plan. |
| `crm_resume_plan` (`plan_id`) | Resumes a held plan. |
| `crm_record_call` (`action_id`, `outcome`) | Records a call outcome on an active plan. |
| `crm_sync_property_predator` (`cursor`) | Pulls one page of Property Predator leads from Relaunch72. |

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
against Postgres in `tests/test_operator_oauth.py`, including two sessions racing one code.
