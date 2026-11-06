# GHQ-UNIFIED-INBOX-003

Base: `5a3b923db23f3dc88852862d90bb980392587e69`. Founder scope: replace the named three-chat Messenger pilot with owner-selected discovery, classify personal/business conversations, support several brand CRM links independently of reply channel, and expose connection capability honestly.

The existing inbox, personal HMAC connector, AppSetting registry and canonical Relaunch72 CRM command are reused. No schema migration. No automatic CRM creation, marketing consent or campaign messages. The default account brand does not classify a personal conversation. Existing explicit CRM links retain their previous activity projection until changed; new UI links require a separate message-copy checkbox. Marking a conversation Personal stops future projection without deleting previous CRM history.

Native discovery searches rendered Messenger UI and resolves one native participant before allowing selection. Search results can be incomplete while encrypted history restores. Unsupported conversation formats remain visible. The desktop connector seeds existing imported conversations once, then follows owner choices from Growth HQ. Deselection blocks further import and queued reply dispatch; existing cached history is retained. A failed conversation does not stop other conversations. Existing durable unknown-send receipts are never resent.

## Validation and cutover

Review corrections: short versioned setting keys fit the existing80-character column; local CRM link and message-copy choice commit atomically; adding another brand preserves Personal classification. A signed deselected conversation cannot dispatch a queued reply. An explicitly opened official-provider conversation now reuses the same idempotent CRM activity projection, gated by the chosen brand's copy setting; this is not unattended provider polling.

Observed coverage correction: Dave Buys Houses uses the standard Page conversation path and native `mid.$…` IDs. Astra approved its bounded reader support under explicit `threadKind=page`, matching numeric thread/profile identity, with E2EE identity checks retained. Changed area -> Page/native-ID focused test and exactCI supersession -> estimated10minutes. No additional send test.

1. Run focused native/personal connector checks and exact-source Postgres CI, including selection/account isolation, personal projection gating, two-brand links and no-copy defaults.
2. Review exact source and CI; publish Group HQ through the Git database REST publisher. Deploy app first, preserving existing environment settings. Its endpoints retain the existing connector protocol during cutover.
3. Verify `/healthz`, owner inbox, classifications and discovery page. Stop only the native Messenger helper, preserving its encrypted profile and journal; start the new helper against the same private directory and saved owner identity. Keep WhatsApp listener untouched.
4. Confirm existing conversations stay selected, then search and select evidenced Durham candidates. Read-only live acceptance: confirm imported native identities, received messages and owner controls. No additional outbound test is required; retain the exact baseline's Sabir send and Sabrina receive proof.

Recovery: stop the new native helper before restoring the prior helper/app at `5a3b923`. Preserve profile, journal and cached history. An older app does not enforce new classification/projection choices, so pause CRM activity projection before app rollback if an owner has marked previously linked conversations Personal; prefer correcting forward or keep the new compatible app while reverting only the helper. Never erase receipts or retry outcome_unknown sends.

## Remaining channel limits

Facebook Page, Instagram and X transport code already exists; permission-ready provider accounts are not proof of private Growth HQ credential binding. LinkedIn native DMs, current TikTok inbox and YouTube channel comments are not delivered by this patch. WhatsApp keyword listening is not a full unified conversation inbox. HNH and Prop Invest CRM routing requires actual canonical workspace proof; no alias to another brand is permitted. The local native helper needs an awake PC; startup installation and full attachment/archive import are not part of this patch.
