# Portable Growth HQ group listener

See [the contract and release notes](../../docs/whatsapp-keywords.md).

1. In this directory, run `npm ci --ignore-scripts`. The pinned Baileys socket transport needs no Chrome window or browser profile.
2. Privately set `WHATSAPP_KEYWORD_ADAPTER_SECRET` to the dedicated value also saved on Group HQ, and `GHQ_WA_LISTENER_NUMBER` to the chosen account's international digits (no plus sign). Never use a Meta access token here. Set `GHQ_WA_DATA_DIR` to a private persistent directory if needed.
3. Run `npm start`. Open the private loopback address saved in `pairing-url.txt` beside your data directory **inside Codex**, then scan it with the selected WhatsApp account → Settings → Linked devices → Link a device. QR values stay in memory, never in terminal logs. The address survives process restarts; a stale QR expires automatically. For a pairing-only check, set `GHQ_WA_PAIR_ONLY=true`: group/contact reads, incoming-message export and sending are refused even after successful linking. Stop that process and restart without this flag to sync groups and run enabled replies.
4. Open `https://group-hq.onrender.com/crm/channels/whatsapp/keywords`. Choose the business and groups, write the keywords/reply, confirm the invitation requests that reply, and deliberately enable the rule.

Keep the process running. Ctrl+C stops it. Credentials and Signal keys use one encrypted atomic snapshot in your private data directory; keep its separate storage key private too. Session/outbox files are ignored by Git; don't share them. Messages require a new matching group request, selected group and enabled rule. Saving configuration alone sends nothing. This adapter has no Meta token and cannot replace the official MHP sender.

For personal one-to-one text, open Growth HQ's Inbox > Personal connections with this same listener running. Select **Collect personal WhatsApp conversations and available history** and save. Enable **Allow replies from this WhatsApp number** separately to reply in the original chat. The device collects incoming and outgoing text while online. With collection enabled, the next normal listener start advertises desktop history support and accepts history the phone supplies. An already-paired phone may supply fewer initial chats. It also requests older pages for known direct chats until the phone reports its earliest available message. The phone controls how much it supplies. Media history retains captions or media-type labels. The owner can link a conversation to an individual business CRM from the inbox.

Update the one existing listener process in place. Reuse its private data directory and sealed socket session. The personal controls start off. To pause, clear them in Personal connections. On server rollback the older server ignores the separate personal setting, and its group rules continue to work. The scoped worker retries recorded send outcomes without sending again after recovery.

Run offline matcher/auth checks with `npm test`. These checks do not connect WhatsApp or prove delivery.
