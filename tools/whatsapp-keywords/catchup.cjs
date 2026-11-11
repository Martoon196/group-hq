// Catch-up: answer people who typed a rule's keyword before now. Growth HQ decides when: nothing here asks the phone
// for earlier messages, or submits one, unless the rules snapshot carries an open catch-up for that rule and group.
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const {findTrigger} = require('./matcher.cjs');
const {openAuthState} = require('./socket-auth.cjs');
const {groupPacket} = require('./socket-client.cjs');
const LISTENER = 'personal-7510';
// The phone is asked for 50 messages a page (socket-client.cjs). A page unanswered after pageTimeout is asked for again
// later: with nothing read yet the phone is probably offline, so the wait doubles from retryFirst up to retryMost until
// the catch-up is about to expire; once pages have arrived, `timeouts` silent requests in a row end the group.
const LIMITS = {pages:200, pageTimeout:45000, pause:1000, timeouts:3, retry:60000, retryFirst:120000, retryMost:1800000, grace:120000, anchors:1000, names:5000};
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const progressFile = (id, groupId) => sha('catchup-progress:' + id + ':' + groupId) + '.json';
// Outbox order: controls first, then requests, then each catch-up group's report (after that group's requests).
const spoolOrder = action => action === 'opt-outs' ? 0 : /^catchups\//.test(action) ? 2 : 1;
// A refusal that will not change on retry is set aside. A server from before catch-up answers 404 to its two actions;
// those must not block the live requests queued behind them.
const setAside = (status, action) => [400,409,413].includes(status) || (status === 404 && /^catchup/.test(action));

// The catch-ups Growth HQ has open in this snapshot and that have not run out by this clock. An older server sends none.
function openCatchups(snapshot, at) {
  const valid = c => c && /^[a-f0-9-]{36}$/.test(c.id) && typeof c.ruleId === 'string' && Array.isArray(c.groupIds)
    && [c.since, c.until, c.expiresAt].every(t => typeof t === 'string' && Number.isFinite(Date.parse(t))) && Date.parse(c.expiresAt) > at;
  return (Array.isArray(snapshot?.catchups) ? snapshot.catchups : []).filter(valid);
}

function createCatchup({client, root, spool, phoneOf, current, pairOnly = false, note = () => {}, openJournal, now = Date.now,
    sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), limits = {}, trace = process.env.GHQ_WA_CATCHUP_TRACE === 'true'}) {
  const limit = {...LIMITS, ...limits};
  // GHQ_WA_CATCHUP_TRACE=true logs the shape of each request and answer (counts, flags and times only: never a message,
  // a name, a number or an ID), to see what a real phone returns.
  const tell = message => { if (trace) note('Catch-up trace: ' + message); };
  const anchors = new Map(), names = new Map(), states = new Map(), finished = new Set();
  let journal, dirty = false, busy = false, active = null;
  // One sealed file: per group, the newest stored message (key, time, sender and its own text). That text is the single
  // message body this listener keeps; a request for "what came before the anchor" would otherwise miss the anchor itself.
  const storage = () => (journal ??= (openJournal ?? (() => openAuthState(path.join(root, 'catchup'),
    {BufferJSON:{}, initAuthCreds:() => ({anchors:{}})})))().then(saved => {
      for (const [groupId, kept] of Object.entries(saved.state.creds.anchors ?? {})) {
        if (!anchors.has(groupId) || anchors.get(groupId).timestamp < kept.timestamp) anchors.set(groupId, kept);
      }
      return saved;
    }));

  function seen(message) {
    if (pairOnly || !message) return;
    if (message.author && message.name) {
      names.delete(message.author); names.set(message.author, message.name);
      if (names.size > limit.names) names.delete(names.keys().next().value);
    }
    if (anchors.get(message.groupId)?.timestamp > message.timestamp) return;
    const {groupId, ...kept} = message;
    anchors.set(groupId, kept); dirty = true;
  }

  async function flush() {
    if (pairOnly || !dirty) return;
    const saved = await storage();
    dirty = false;
    const newest = [...anchors].sort((a, b) => b[1].timestamp - a[1].timestamp).slice(0, limit.anchors);
    if (newest.length < anchors.size) { anchors.clear(); for (const [groupId, kept] of newest) anchors.set(groupId, kept); }
    saved.state.creds.anchors = Object.fromEntries(anchors);
    try { await saved.saveCreds(); } catch (error) { dirty = true; throw error; }
  }

  const wanted = () => Boolean(active);
  function accept(bundle, provider) {
    if (!active || !bundle) return;
    const messages = (bundle.messages ?? []).filter(m => m?.key?.remoteJid === active.groupId);
    const chat = (bundle.chats ?? []).find(c => c?.id === active.groupId);
    const answers = typeof bundle.sessionId === 'string' && bundle.sessionId === active.requestId;
    if (trace) {
      const all = bundle.messages ?? [], times = messages.map(m => Number(m.messageTimestamp?.toNumber?.() ?? m.messageTimestamp)).filter(Number.isFinite);
      const day = t => new Date(t * 1000).toISOString();
      tell(`answer chats=${(bundle.chats ?? []).length} messages=${all.length} forThisGroup=${messages.length}`
        + ` otherChats=${new Set(all.map(m => m?.key?.remoteJid).filter(j => j && j !== active.groupId)).size}`
        + ` groupListed=${Boolean(chat)} end=${chat?.endOfHistoryTransferType ?? 'none'}`
        + ` session=${typeof bundle.sessionId !== 'string' ? 'absent' : answers ? 'match' : 'other'}`
        + (times.length ? ` oldest=${day(Math.min(...times))} newest=${day(Math.max(...times))}` : ''));
    }
    if (!messages.length && !chat && !answers) return;
    active.queue.push({messages, provider, answers, ended:chat?.endOfHistoryTransferType === 1});
    active.wake?.();
  }

  // Into the same durable outbox as live requests. The file appears complete or not at all (the outbox loop may be
  // reading the directory), and one already there is kept unless `replace` says otherwise.
  async function write(name, payload, replace) {
    const file = path.join(spool, name);
    const held = await fs.readFile(file, 'utf8').then(JSON.parse, error => { if (error.code === 'ENOENT') return null; throw error; });
    if (held && !replace(held)) return;
    const temp = file + '.' + crypto.randomUUID() + '.tmp';
    await fs.writeFile(temp, JSON.stringify(payload), {flag:'wx', mode:0o600});
    await fs.rename(temp, file);
  }

  async function consider(job, message, state) {
    const at = message.timestamp * 1000;
    if (message.fromMe || !message.author || typeof message.body !== 'string' || at < job.since || at > job.until) return;
    if (!findTrigger(message.body, [job.rule], job.rule.matchMode)) return;
    state.matched++;
    client.remember?.(message.author, message.alt);
    const phone = await phoneOf(message.author).catch(() => null);
    if (!phone) { state.unresolved++; return; }   // Unresolved identity is never messaged.
    const value = {eventId:message.eventId, listenerId:LISTENER, groupId:job.groupId, senderId:message.author, senderPhone:phone,
      senderName:String(message.name || names.get(message.author) || '').slice(0, 200), text:message.body,
      observedAt:new Date(at).toISOString(), catchupId:job.id};
    // The event ID is the message's own: one already spooled live, or by this catch-up, stays the single event.
    await write(sha(value.eventId) + '.json', {action:'catchup-events', value},
      held => held.action === 'catchup-events' && held.value?.catchupId !== job.id);
  }

  async function report(job, state, status, reachedSince) {
    await write(progressFile(job.id, job.groupId), {action:'catchups/' + job.id + '/progress', value:{listenerId:LISTENER,
      groupId:job.groupId, status, reachedSince, scanned:state.scanned, matched:state.matched, unresolved:state.unresolved}}, () => false);
    finished.add(job.key); states.delete(job.key);
    note(status === 'unavailable' ? 'Catch-up: one group had no earlier messages available.' : 'Catch-up: one group read.');
  }

  const live = id => !pairOnly && client.ready && !client.closed && openCatchups(current(), now()).some(c => c.id === id);

  async function page(job, state) {
    active = {groupId:job.groupId, queue:[], wake:null, requestId:null};
    let timer, silent = false;
    try {
      const requestId = await client.fetchGroupHistory(job.groupId, state.cursor, {seconds:state.seconds});
      tell(`asked page=${state.pages} unit=${state.seconds ? 'seconds' : 'milliseconds'} anchorFromMe=${Boolean(state.cursor.fromMe)}`
        + ` anchorAt=${new Date(state.cursor.timestamp * 1000).toISOString()}`);
      active.requestId = typeof requestId === 'string' ? requestId : null;
      timer = setTimeout(() => { silent = true; active?.wake?.(); }, limit.pageTimeout);
      for (;;) {
        if (!active.queue.length) {
          if (silent) { tell('no answer in time'); return 'silent'; }
          await new Promise(resolve => { active.wake = resolve; });
          active.wake = null;
          continue;
        }
        let fresh = 0, oldest = null, ended = false;
        for (const bundle of active.queue.splice(0)) {
          ended ||= bundle.ended || (bundle.answers && !bundle.messages.length);
          for (const raw of bundle.messages) {
            const message = groupPacket(raw, bundle.provider);
            if (!message || state.read.has(message.id)) continue;
            state.read.add(message.id); state.scanned++; fresh++;
            if (!oldest || message.timestamp < oldest.timestamp) oldest = message;
            await consider(job, message, state);
          }
        }
        if (oldest && oldest.timestamp <= state.cursor.timestamp) state.cursor = {id:oldest.id, fromMe:oldest.fromMe, timestamp:oldest.timestamp};
        if (ended) return 'ended';
        // A late copy of an earlier answer holds nothing new and says nothing about this request: keep waiting.
        if (fresh) return 'read';
      }
    } finally { clearTimeout(timer); active = null; }
  }

  async function group(job) {
    const state = states.get(job.key) ?? {cursor:null, read:new Set(), scanned:0, matched:0, unresolved:0, pages:0, silent:0, nextAt:0, seconds:false};
    states.set(job.key, state);
    if (now() < state.nextAt) return;
    const closing = () => now() >= job.expires - limit.grace;
    if (!state.cursor) {
      const anchor = anchors.get(job.groupId);
      if (!anchor) { if (closing()) await report(job, state, 'unavailable', false); return; }
      state.cursor = {id:anchor.id, fromMe:anchor.fromMe, timestamp:anchor.timestamp};
      state.read.add(anchor.id); state.scanned++;
      await consider(job, {...anchor, groupId:job.groupId}, state);
    }
    for (;;) {
      if (!live(job.id)) return;
      if (state.cursor.timestamp * 1000 < job.since) return report(job, state, 'scanned', true);
      if (state.pages >= limit.pages || (state.pages && closing())) return report(job, state, 'scanned', false);
      const before = state.scanned;
      const outcome = await page(job, state);
      if (outcome === 'ended' && !state.pages && state.scanned === before && !state.seconds) {
        // The very first request came back empty. Before believing a busy group has nothing earlier, ask once more with
        // the stored message's time in seconds, the other form phones are known to be asked in.
        state.seconds = true; tell('first answer empty; asking again with the time in seconds');
        await sleep(limit.pause);
        continue;
      }
      if (outcome === 'ended') return report(job, state, 'scanned', state.cursor.timestamp * 1000 < job.since);   // The phone holds nothing earlier.
      if (outcome === 'silent') {
        state.silent++;
        if (state.pages ? state.silent >= limit.timeouts : closing()) return report(job, state, state.pages ? 'scanned' : 'unavailable', false);
        // Never sleep through the last chance to report before Growth HQ closes the catch-up.
        state.nextAt = Math.min(now() + (state.pages ? limit.retry : Math.min(limit.retryMost, limit.retryFirst * 2 ** (state.silent - 1))),
          job.expires - limit.grace);
        return;
      }
      state.pages++; state.silent = 0;
      await sleep(limit.pause);
    }
  }

  async function tick() {
    if (busy || pairOnly) return;
    const snapshot = current(), open = openCatchups(snapshot, now());
    for (const key of [...states.keys(), ...finished]) {
      if (!open.some(c => key.startsWith(c.id + ':'))) { states.delete(key); finished.delete(key); }
    }
    if (!open.length || !client.ready || client.closed) return;
    busy = true;
    try {
      await storage();
      for (const c of open) {
        const rule = (snapshot.rules ?? []).find(r => r.id === c.ruleId && r.enabled);
        if (!rule) continue;
        const pending = Array.isArray(c.pendingGroupIds) ? c.pendingGroupIds : c.groupIds;
        for (const groupId of pending) {
          const job = {id:c.id, key:c.id + ':' + groupId, groupId, rule, since:Date.parse(c.since), until:Date.parse(c.until), expires:Date.parse(c.expiresAt)};
          if (!live(c.id)) return;
          if (!c.groupIds.includes(groupId) || !rule.groupIds.includes(groupId) || finished.has(job.key)) continue;
          // A report still in the outbox (after a restart) means this group is already read.
          if (await fs.access(path.join(spool, progressFile(c.id, groupId))).then(() => true, () => false)) { finished.add(job.key); continue; }
          // One group's failure (a dropped link, a full disk) leaves it for the next round; the others still run.
          try { await group(job); } catch { const state = states.get(job.key); if (state) state.nextAt = now() + limit.retry; note('Catch-up: one group needs another try.'); }
        }
      }
    } finally { busy = false; }
  }

  return {seen, flush, accept, wanted, tick};
}
module.exports = {createCatchup, openCatchups, spoolOrder, setAside, progressFile, LIMITS};
