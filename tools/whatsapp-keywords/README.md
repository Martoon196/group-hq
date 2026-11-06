# Portable Growth HQ group listener

See [the contract and release notes](../../docs/whatsapp-keywords.md).

1. In this directory, run `npm ci`. The fixed whatsapp-web.js version uses Puppeteer to launch WhatsApp Web.
2. Privately set `WHATSAPP_KEYWORD_ADAPTER_SECRET` to the dedicated value also saved on Group HQ. Never use a Meta access token here. Set `GHQ_WA_DATA_DIR` to a private persistent directory if needed.
3. Run `npm start` in your own terminal and scan the private QR using the 07510… WhatsApp account → Linked devices.
4. Open `https://group-hq.onrender.com/crm/channels/whatsapp/keywords`. Choose the business and groups, write the keywords/reply, confirm the invitation requests that reply, and deliberately enable the rule.

Keep the terminal running. Ctrl+C stops it. Session/outbox files are ignored by Git; don't share them. Messages require a new matching group request, selected group and enabled rule. Saving configuration alone sends nothing. This adapter has no Meta token and cannot replace the official MHP sender.

Run offline matcher/auth checks with `npm test`. These checks do not connect WhatsApp or prove delivery.
