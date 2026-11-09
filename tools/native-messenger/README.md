# Growth HQ native Messenger connection

First bounded implementation: a separate headless Microsoft Edge profile on the
owner's Windows PC, with a private loopback login screen opened inside Codex.
No Beeper dependency, paid inbox broker, imported cookies or personal Chrome
profile. Facebook itself decrypts history after the owner completes its login
and any PIN recovery. The connector reads rendered message DOM, not encryption
keys or private Facebook APIs. There is no claim of official Meta API support.

`reader.cjs` checks exact numeric thread, profile and native message identities.
The current English UI date parser accepts recent weekdays/Today/Yesterday and
clock-only labels. Unsupported dates stop the import; timestamps have minute
precision. The visible introduction is recorded as a window-boundary signal,
not proof that deleted or inaccessible history has been recovered. The existing
server stores a maximum of 100 messages. Larger windows stop rather than page
over and erase previously cached history. Full archival pagination is not built.

`runtime.cjs` provides the adapter boundary (`verifyIdentity`, `observe`,
`sendOnce`) and reuses Growth HQ's HMAC ingestion, owner access, durable reply
queue and canonical Relaunch72 CRM. Native and Beeper workers have distinct
discovery and queue namespaces; default legacy calls remain Beeper-scoped.
The initial private allowlist seeds already named conversations. The owner can
keep selecting named chats or enable **Sync all available chats** at
`/inbox/discover`. This includes rendered incoming/personal one-to-one chats,
checks each participant, and imports in batches without a search phrase. It
uses a fixed number of reusable browser pages rather than a tab per contact.
The registry is bounded to 2,000 discovered candidates; unavailable, unverified
or unsupported chats are excluded. Older/unloaded history may still be missing.
An optional `textOnly: true` descriptor omits unsupported attachment/edited rows,
marks that window incomplete and skips an entirely unsupported window without
overwriting history. Native identities are still checked before filtering.
No other account's inbox is imported. The actual configuration and histories stay outside
Git. Additional channels need independent identity/history/send/receipt proof;
the contract is reusable, not an implementation of every social network.

Each outgoing request records durable encrypted intent before one Enter action.
It is accepted only after the exact new native message survives a Facebook
reload. This is channel persistence evidence, not recipient delivery or read
confirmation. Crashes or uncertain outcomes settle as unknown and are never
automatically resent. Existing unsent drafts are not overwritten.

## Running

Install with `npm ci --ignore-scripts`. Set `GHQ_NATIVE_DATA_DIR` to a private
absolute directory, `GHQ_NATIVE_BROWSER_PATH` to the installed official Edge
binary, `GHQ_NATIVE_CONFIG` to a private JSON file, and
`GHQ_INBOX_PERSONAL_SECRET` to the existing authorised local adapter key.
The JSON contains `ownerUserId` and a `threads` array of existing named objects
with `threadId`, `participantId`, `name` (an empty array is allowed). New chats
can be discovered from the dashboard. Do not commit these values or secrets.

Run `node login.cjs`. Open the URL saved in the private directory's
`connection.json` in Codex. It is a random capability URL bound to 127.0.0.1;
do not publish it. The owner enters Facebook credentials directly in that UI.
Once signed in, press Done signing in. Choose the business and enable the
discovered account at Growth HQ `/inbox/connect`.

The PC and process must remain running. No Windows startup installation is
performed. Browser sessions stay in the separate Edge profile; the connector
does not export them to Render. The loopback UI can display private messages,
so keep its capability URL private. Do not use it as a remotely exposed service.

## Release and recovery

No database migration. The server change is additive to schema0011. Deploy the
server before starting the native runtime; older servers reject its namespace.
Recovery: pause/stop this runtime, disable its account, restore Group HQ6059f72.
Existing Beeper and WhatsApp connectors and CRM records are retained.

Tests in `test.cjs` cover identity, dates, partial-window reporting, replay and
unknown-send persistence. Server tests cover independent account discovery and
queue ownership. A passing fixture test is not live send/receive acceptance.
