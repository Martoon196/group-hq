# Inbox migration registry bridge

Based on live Group HQ main 6bad98ab4bc7ba2c3eb9c07317bce79e045e3beb. Keeps its application, paired WhatsApp keyword connector and owner journeys unchanged. Adds only the exact additive 0011 migration used by the personal inbox candidate, and the matching migration CI expectation. No personal message read, reply, CRM command or engine credential is introduced by this bridge.

Gate this bridge and the final candidate before deploying either. Deploy this first: Render's existing pre-deploy command applies the additive tables, while this bridge knows the new revision and can boot. Then deploy the exact gated final tree. If final fails, redeploy this bridge; never return to an application registry ending at 0010 after 0011 is applied. Do not run the down migration or remove stored messages/receipts as recovery. Existing keyword, grant, finance and member journeys remain enabled.
