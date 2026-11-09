const crypto = require('node:crypto');
const path = require('node:path');
const {openAuthState} = require('./socket-auth.cjs');
const {toTransportJid} = require('./socket-client.cjs');

function privateNotification(packet, connectedAt, provider, self, now = Date.now()) {
  const {key} = packet ?? {};
  const peer = toTransportJid(key?.remoteJid);
  if (!peer || peer === self || peer.endsWith('@g.us') || key.participant || typeof key.fromMe !== 'boolean'
      || typeof key.id !== 'string' || !/^[A-Za-z0-9_:-]{1,200}$/.test(key.id)) return null;
  const at = Number(packet.messageTimestamp) * 1000;
  if (!Number.isFinite(at) || at < connectedAt - 2000 || at > now + 300000) return null;
  const content = provider.normalizeMessageContent(packet.message);
  const body = content?.conversation ?? content?.extendedTextMessage?.text;
  if (typeof body !== 'string' || !body.length || Buffer.byteLength(body) > 20000) return null;
  const alternate = toTransportJid(key.remoteJidAlt);
  if (alternate === self) return null;
  return {id:'baileys:' + key.id, peer, direction:key.fromMe ? 'outgoing' : 'incoming', body,
    at:new Date(at).toISOString(), name:!key.fromMe && typeof packet.pushName === 'string' ? packet.pushName.slice(0,200) : '',
    phone:peer.endsWith('@c.us') ? peer.split('@')[0] : alternate?.endsWith('@c.us') ? alternate.split('@')[0] : null};
}

function createDirectInbox({api, client, root, expectedNumber, phoneOf, openJournal, now = Date.now}) {
  const self = expectedNumber + '@c.us';
  const device = 'wa-device:' + crypto.createHash('sha256').update(self).digest('hex');
  const envelope = {listenerId:'personal-7510', device};
  const call = (action, data={}) => api('POST', 'direct/' + action, {...data, ...envelope});
  const bound = () => !client.pairOnly && client.ready && !client.closed && client.info?.wid?._serialized === self;
  let access = {}, checked = 0, journal, busy = false;
  const enabled = () => bound() && access.collect === true && now() - checked < 45000;
  const storage = () => (journal ??= (openJournal ?? (() => openAuthState(path.join(root,'direct-inbox'),
    {BufferJSON:{}, initAuthCreds:()=>({events:{}, deliveries:{}})})))());

  const historyEnabled = () => access.history === true && now()-checked < 45000 && !client.closed && !client.pairOnly
    && (bound() || toTransportJid(client.auth?.state.creds.me?.id) === self);
  const {createPhoneHistory} = require('./phone-history.cjs');
  const history = createPhoneHistory({client,device,self,root,storage,enabled:historyEnabled,call,now});
  async function prepareHistory() {
    if (client.pairOnly || toTransportJid(client.auth?.state.creds.me?.id)!==self) return false;
    const policy = await call('policy');
    access = {...access,history:policy.history===true}; checked=now();
    return historyEnabled();
  }

  async function accept(message) {
    if (!enabled() || !message || Date.parse(message.at) < Date.parse(access.since)) return;
    const saved = await storage();
    if (!enabled()) return;
    const key = crypto.createHash('sha256').update(JSON.stringify([device, message.peer, message.id])).digest('hex');
    const items = saved.state.creds.events;
    if (items[key]) return;
    items[key] = message;
    await saved.saveCreds();
    await history.seed(message);
  }

  async function tick() {
    if (busy || !bound()) return;
    busy = true;
    try {
      access = await call('status');
      checked = now();
      if (!bound()) return;
      // History import owns a separate queue. A blocked phone request cannot delay live replies.
      if (historyEnabled()) void history.tick().catch(()=>{});
      const saved = await storage();
      const {events, deliveries} = saved.state.creds;
      for (const [key, event] of Object.entries(events)) {
        if (enabled() && Date.parse(event.at) >= Date.parse(access.since)) {
          const phone = event.phone ?? await phoneOf(event.peer).catch(()=>null);
          try {
            await call('event', {...event, phone});
          } catch (error) {
            if (![400,409,413].includes(error.status)) throw error;
            // Quarantine only the rejected event in the encrypted journal. Other chats keep syncing.
            const held = (saved.state.creds.held ??= {});
            held[key] = event;
          }
        }
        delete events[key];
        await saved.saveCreds();
      }
      for (const [id, result] of Object.entries(deliveries)) {
        await call('replies/' + id + '/settle', result);
        delete deliveries[id];
        await saved.saveCreds();
      }
      if (!enabled() || !access.reply) return;
      const pending = await call('pending');
      for (const id of pending.reply_ids ?? []) {
        if (!bound() || !/^[0-9a-f-]{36}$/.test(id)) continue;
        // A crash or timeout after reservation can only settle this job, never resend it.
        deliveries[id] = {state:'outcome_unknown'};
        await saved.saveCreds();
        const job = await call('replies/' + id + '/begin');
        if (job.dispatch) {
          if (!enabled() || !access.reply || job.reply_id !== id || job.account_key !== 'personal-whatsapp-device'
              || job.account_id !== device || job.user_id !== device || job.channel !== 'whatsapp'
              || !/^[1-9][0-9]{4,29}@(c\.us|lid)$/.test(job.thread_id) || self === job.thread_id) {
            deliveries[id] = {state:'rejected'};
          } else {
            let sent;
            try { sent = await client.sendMessage(job.thread_id, job.body); } catch { /* Ambiguous result. */ }
            const receipt = sent?.id?._serialized;
            if (typeof receipt === 'string' && /^baileys:[A-Za-z0-9_:-]{1,200}$/.test(receipt)) {
              deliveries[id] = {state:'bridge_accepted', provider_message_id:receipt};
              // Own sends use emitOwnEvents=false. Project the acknowledged message into the original chat.
              await accept({id:receipt, peer:job.thread_id, body:job.body, direction:'outgoing',
                at:new Date(now()).toISOString(), name:'', phone:null});
            }
          }
          await saved.saveCreds();
          await call('replies/' + id + '/settle', deliveries[id]);
        }
        delete deliveries[id];
        await saved.saveCreds();
      }
    } finally { busy = false; }
  }

  return {enabled, accept, tick, historyEnabled, prepareHistory, acceptHistory:history.accept};
}
module.exports = {createDirectInbox, privateNotification};
