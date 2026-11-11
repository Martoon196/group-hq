// Requests typed while this listener was offline. WhatsApp delivers them when it reconnects; the live path ignores them
// because they are not new. They are offered to Growth HQ, which answers one only if its rule was already on when it
// was typed. Nothing is read from the phone's history here, and nothing is kept: a message waits in memory only until
// the first rules snapshot after the start, then is submitted or dropped.
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const {findTrigger} = require('./matcher.cjs');
const LISTENER = 'personal-7510';
const LIMITS = {hours:72, waiting:5000};
const sha = value => crypto.createHash('sha256').update(value).digest('hex');

function createMissed({client, spool, phoneOf, pairOnly = false, note = () => {}, now = Date.now, limits = {}}) {
  const limit = {...LIMITS, ...limits};
  let waiting = [];

  // A group message as socket-client's groupPacket reads it, delivered late.
  function seen(message) {
    if (pairOnly || !message || message.fromMe || !message.author || typeof message.body !== 'string') return;
    const age = now() - message.timestamp * 1000;
    if (age < 0 || age > limit.hours * 3600000) return;
    if (waiting.length >= limit.waiting) waiting.shift();
    waiting.push(message);
  }

  // Called with each fresh rules snapshot: everything waiting that an enabled rule covers goes to the durable outbox.
  async function submit(snapshot) {
    if (pairOnly || !waiting.length || !Array.isArray(snapshot?.rules)) return 0;
    const batch = waiting; waiting = [];
    let queued = 0;
    for (const message of batch) {
      try {
        const rule = snapshot.rules.find(r => r.enabled && r.groupIds.includes(message.groupId) && findTrigger(message.body, [r], r.matchMode));
        if (!rule) continue;
        client.remember?.(message.author, message.alt);
        const phone = await phoneOf(message.author).catch(() => null);
        if (!phone) { note('A request missed while offline needs phone identity review; no DM sent.'); continue; }
        const value = {eventId:message.eventId, listenerId:LISTENER, groupId:message.groupId, senderId:message.author, senderPhone:phone,
          senderName:String(message.name || '').slice(0, 200), text:message.body, observedAt:new Date(message.timestamp * 1000).toISOString()};
        const file = path.join(spool, sha(value.eventId) + '.json'), temp = file + '.' + crypto.randomUUID() + '.tmp';
        // The event ID is the message's own: one already in the outbox stays the single event.
        if (await fs.access(file).then(() => true, () => false)) continue;
        await fs.writeFile(temp, JSON.stringify({action:'missed-events', value}), {flag:'wx', mode:0o600});
        await fs.rename(temp, file);
        queued++;
      } catch { note('A request missed while offline needs checking; no automatic DM was attempted.'); }
    }
    if (queued) note('Requests missed while offline were passed to Growth HQ.');
    return queued;
  }

  return {seen, submit, waiting:() => waiting.length};
}
module.exports = {createMissed, LIMITS};
