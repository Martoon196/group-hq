const crypto = require('node:crypto');
const {snapshot} = require('./client.cjs');

const {packets,submit} = require('../whatsapp-keywords/history-packets.cjs');

function before(page, cursor) {
  if (page.hasMore !== true) return null;
  if (typeof page.oldestCursor !== 'string' || !page.oldestCursor || page.oldestCursor === cursor)
    throw Error('history_cursor_did_not_advance');
  return page.oldestCursor;
}

// Counts below bound one poll's time. Every acknowledged cursor is revisited until the provider reaches its end.
async function archiveCycle(engine, cloud, state, binding, now=Date.now()) {
  const registry = (state.state.creds.history ??= {});
  const key = crypto.createHash('sha256').update(JSON.stringify([binding.key,binding.account_id,binding.user_id])).digest('hex');
  const progress = (registry[key] ??= {jobs:{}, cursor:null, discoveredAt:0, turn:0});
  if (progress.cursor || now - progress.discoveredAt >= 3600000) {
    const page = await engine.chats(binding, progress.cursor);
    for (const c of page.items) {
      if (c.type !== 'single' || c.merge || c.accountID !== binding.account_id) continue;
      const id = crypto.createHash('sha256').update(c.id).digest('hex');
      if (!progress.jobs[id]) progress.jobs[id] = {id:c.id, cursor:null};
    }
    progress.cursor = before(page, progress.cursor);
    if (!progress.cursor) progress.discoveredAt = now;
    await state.saveCreds();
  }
  const waiting = Object.values(progress.jobs).filter(job => (!job.doneAt || now - job.doneAt >= 86400000) && (!job.retryAt || now >= job.retryAt));
  for (let i=0; i < Math.min(3, waiting.length); i++) {
    const job = waiting[progress.turn++ % waiting.length];
    try {
      const c = await engine.chat(binding, job.id, false);
      const page = await engine.messages(binding, c, job.cursor);
      const next = before(page, job.cursor);
      const chunks = packets(snapshot(binding, c, page), next === null);
      await submit(packet=>cloud.request('history',packet),chunks);
      job.cursor = next || page.oldestCursor || job.cursor;
      job.doneAt = next ? 0 : now;
      delete job.retryAt;
    } catch (error) {
      // A read-only or encrypted chat can stall independently. Preserve its exact last acknowledged cursor.
      job.retryAt = now + 60000;
      if (/cross_account/.test(error.message)) throw error;
    }
    await state.saveCreds();
  }
}
module.exports = {packets, submit, archiveCycle};
