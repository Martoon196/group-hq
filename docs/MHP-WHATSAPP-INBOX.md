# MHP WhatsApp in the Growth HQ inbox

Both services use the existing signed Growth HQ CRM connection. The owner can read the latest 100 conversations, open earlier received messages in pages of 32 and follow a conversation to its existing MHP contact. Relaunch72 verifies every returned message through the same live webhook evidence boundary as its operational inbox.

## Enable

1. Deploy the compatible Relaunch72 reader before enabling the Growth HQ connection. Other channels continue using their existing configuration.
2. On the canonical database, identify the active MHP inbox using a read-only operator connection. Expect exactly one UUID for the verified sender. The UUID belongs to the inbox row. The phone number ID, WABA ID and internal sending binding ID identify different objects.

   ```sql
   SELECT i.id
   FROM app.inboxes i
   JOIN app.channel_endpoints e ON e.workspace_id=i.workspace_id AND e.id=i.channel_endpoint_id
   JOIN app.provider_connections c ON c.workspace_id=i.workspace_id AND c.id=i.provider_connection_id
   WHERE i.workspace_id='2789228d-55a6-4688-b1e8-2f70abca990b'
     AND i.channel='whatsapp' AND i.environment='live' AND i.status='active'
     AND e.normalized_address='+447861935832' AND e.status='active'
     AND c.provider_id='meta_whatsapp_cloud' AND c.status='active';
   ```

3. Set `GHQ_MHP_WHATSAPP_INBOX_ID` on both services. On Relaunch72, `GHQ_MHP_WHATSAPP_OWNER_IDS` maps authorised owners’ emails to their native UUIDs. The existing signed CRM scope, verified email and active ownership of the MHP organisation and its marketing workspace are checked independently.
4. As a permitted owner, open Growth HQ → Inbox → Martin Howard Property · WhatsApp. Check a known received message against MHP, then open its CRM contact. An unmapped actor should see no message content. This connection supports received text and opens on demand.

## Recovery

Remove `GHQ_MHP_WHATSAPP_INBOX_ID` from Growth HQ and redeploy it to hide the account. Then remove the matching Relaunch72 setting if required. The live MHP Meta webhook, existing message records and sender keep their separate configuration.
