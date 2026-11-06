# Growth HQ group-keyword replies

Task GHQ-WA-KEYWORD-001, base `44c2891e5045636751fc6c10bcd9888df62abe73`.

The owner editor is `/crm/channels/whatsapp/keywords`, linked from the existing WhatsApp page. Rules live in existing AppSetting storage and edits use the existing admin session, origin guard and audit log. The portable adapter is `tools/whatsapp-keywords`. Claude's original `normalise` / `findTrigger` implementation is retained in `matcher.cjs`; the original in-memory sending queue is not used.

Each rule selects one business, explicit group IDs, exact or whole-word keywords, a reply and enabled/paused state. `{name}` inserts the requester's first name. Enabling requires the owner to confirm the group invitation promises that private reply. Empty group choices never mean all groups. Overlapping enabled rules for the same group are refused, including across brands.

## Custody and boundaries

The group listener is bound to `447510243096@c.us`. The owner scans its QR in their own terminal. A different account is disconnected before group collection or sending. The QR-linked session and adapter signing secret stay off-web. This uses the unofficial whatsapp-web.js transport; it is separate from MHP's official Meta number, token, webhook and message receipts. It does not grant access to existing phone-created groups through the Cloud API.

Only matching group messages, group-name/ID inventory, and private STOP/START controls are submitted. There is no private-chat backfill. Other personal messages are ignored. STOP/START affects only this listener's replies; it does not manufacture MHP/PP marketing consent, SMS consent or a verified canonical CRM contact.

The adapter needs `WHATSAPP_KEYWORD_ADAPTER_SECRET` (32–256 characters), shared only between this adapter and Group HQ, and `GHQ_BASE_URL` (default `https://group-hq.onrender.com`). Use a dedicated secret, not the read-only R72 bridge credential or a Meta token. Session, event-spool and settlement files live under `GHQ_WA_DATA_DIR` or the ignored local `data` folder. Keep that directory private and persistent. It currently runs while the owner's computer is on; a separately hosted always-on listener requires its own reviewed persistent hosting and QR handoff.

## Adapter contract v1

Prefix `/api/whatsapp-keywords/v1`. Each request has `X-GHQ-Timestamp` (Unix seconds within 120 seconds) and `X-GHQ-Signature` (lowercase hex HMAC-SHA256). Signed bytes are `timestamp + "\n" + METHOD + "\n" + pathname + "\n" + rawBody`. No query parameters or redirects. GET body is empty; JSON bodies are UTF-8. Credentials never enter query strings or browser HTML.

- `GET /rules`: `{schemaVersion:1, listenerId:"personal-7510", revision, rules:[{id,brand,name,keywords,message,groupIds,matchMode,enabled}]}`.
- `POST /groups`: `{listenerId, groups:[{id,name}]}`; group IDs end in `@g.us`. Inventory is capped at 1,000 groups, not selected automatically.
- `POST /events`: `{eventId,listenerId,groupId,senderId,senderPhone,senderName,text,observedAt}`. `senderPhone` is digits only; `senderId` is the actual WhatsApp `@c.us`/`@lid` ID. The adapter obtains the phone mapping from WhatsApp; an unresolved identity is not messaged. Only fresh matching requests are admitted. Exact ID replay is effect-free, changed bytes under the ID conflict (409). Responses identify queued, replayed, already_contacted, ignored or opted_out.
- `GET /pending`: `{deliveryIds:[uuid]}`; pending jobs only.
- `POST /deliveries/{id}/begin`, body `{}`: locks the exact pending delivery, rechecks the current rule and suppression, then returns either `{send:false,state}` or `{send:true,deliveryId,recipientId,message}`. The row changes to calling before any provider action. A replay cannot authorise another call.
- `POST /deliveries/{id}/settle`: `{state:"sent"|"outcome_unknown",providerMessageId}`. Exact result replay succeeds; changed results conflict. “Sent” means WhatsApp accepted it, not a delivered/read receipt.
- `POST /opt-outs`: `{eventId,senderPhone,action:"STOP"|"START",observedAt}`. Event replay rules also apply. Older START cannot release a newer STOP. These controls are distinct from official Meta consent.

Rules and begin admission share a short Postgres lock. The unique `(rule_id,sender_sha256)` prevents repeated DMs across restarts and concurrent event callbacks. Editing a rule never re-sends an old reply; pending jobs from an earlier revision are held. Calling and unknown operations are never retried. Before a provider call, the adapter writes a calling marker. After a crash it settles that operation as unknown; after a lost settlement response it retries only the recorded settlement, never the DM.

## Relaunch72 integration handoff

The keyword screen is the single owner editor. RL72 may add a brand-scoped entry that opens this existing editor, rather than another rule database. This patch has **no canonical CRM write/export API**. The recent enquiries are a separate intake/outbox, explicitly labelled as awaiting identity matching. They are not inserted into `CampaignLead`, and are not represented as signed Meta receipts or verified contact points.

A subsequent RL72 adapter must preserve source `whatsapp_web_group_keyword`, listener ID, event ID, rule ID/revision, brand and existing request digest. Match an existing verified canonical contact only when unambiguous; unknown/ambiguous identities go to review. A keyword request is permission for that promised reply, not general marketing consent. A dedicated read-scoped bridge contract and genuine owner permissions must be agreed before exporting these enquiries; do not reuse the sending adapter key for a member-facing UI or expose it in browser JavaScript.

## Cutover and recovery

1. Exact-source checks for the compatibility bridge and full candidate pass before production changes.
2. Publish/deploy the compatible bridge first. Its only functional addition is additive migration0010; existing journeys remain on the old implementation and no messages start.
3. Deploy the full candidate, verify `/healthz`, owner editor/auth and the adapter authentication rejection. Save no enabled rules as part of deployment.
4. With the founder's dedicated-access approval, save the adapter secret privately in Group HQ and the local adapter. The owner scans the QR, chooses a test group, and writes/explicitly enables one owned test rule. Run a real keyword/DM/replay/STOP acceptance check; do not infer that offline tests prove WhatsApp delivery.
5. For recovery, pause the listener first and restore the retained compatibility bridge artifact. It understands migration0010, leaves records intact and keeps the existing CRM working. Do not downgrade/drop tables containing event history. Official MHP/PP services are unchanged by this release.
