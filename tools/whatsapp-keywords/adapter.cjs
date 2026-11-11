// Growth HQ owns group rules, private inbox permissions, durable claims and delivery receipts.
// Run in the owner's terminal: QR codes/session material must never enter chat logs.
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const {findTrigger} = require('./matcher.cjs');
const {createCatchup, spoolOrder, setAside} = require('./catchup.cjs');
const BASE = '/api/whatsapp-keywords/v1';
const LISTENER = 'personal-7510';

function listenerMatches(actualId, configuredNumber) {
  return typeof configuredNumber === 'string' && /^[1-9][0-9]{6,14}$/.test(configuredNumber)
    && actualId === configuredNumber + '@c.us';
}

function makeApi({origin, secret, fetchFn = fetch, now = Date.now}) {
  const url = new URL(origin);
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash || secret.length < 32 || secret.length > 256) throw Error('Invalid Growth HQ connection');
  return async (method, action, value) => {
    const endpoint = BASE + '/' + action;
    const body = value === undefined ? '' : JSON.stringify(value);
    const stamp = String(Math.floor(now() / 1000));
    const signature = crypto.createHmac('sha256', secret).update(stamp + '\n' + method + '\n' + endpoint + '\n' + body).digest('hex');
    const response = await fetchFn(url.origin + endpoint, {method, headers: {'Content-Type':'application/json',
      'X-GHQ-Timestamp':stamp, 'X-GHQ-Signature':signature}, ...(method === 'GET' ? {} : {body}),
      redirect:'error', signal:AbortSignal.timeout(15000)});
    const raw = await response.text();
    if (raw.length > 1000000) throw Error('Oversized Growth HQ response');
    if (!response.ok) { const e = Error('Growth HQ request rejected'); e.status = response.status; throw e; }
    return JSON.parse(raw);
  };
}

function findRule(snapshot, groupId, body) {
  if (snapshot.listenerId !== LISTENER || !Number.isInteger(snapshot.revision) || !Array.isArray(snapshot.rules)) throw Error('Invalid rule snapshot');
  const candidates = snapshot.rules.filter(r => r.enabled && r.groupIds.includes(groupId));
  return candidates.find(r => findTrigger(body, [r], r.matchMode)) || null;
}

async function saveJson(file, value) {
  const temp = file + '.' + crypto.randomUUID() + '.tmp';
  await fs.writeFile(temp, JSON.stringify(value), {mode:0o600, flag:'wx'});
  await fs.rename(temp, file);
}

async function main() {
  const {SocketClient} = require('./socket-client.cjs');
  const {createPairingPage} = require('./pairing-page.cjs');
  const root = path.resolve(process.env.GHQ_WA_DATA_DIR || path.join(__dirname, 'data'));
  const spool = path.join(root, 'events'), outcomes = path.join(root, 'outcomes');
  await fs.mkdir(spool,{recursive:true,mode:0o700}); await fs.mkdir(outcomes,{recursive:true,mode:0o700});
  const api = makeApi({origin:process.env.GHQ_BASE_URL || 'https://group-hq.onrender.com',
                       secret:process.env.WHATSAPP_KEYWORD_ADAPTER_SECRET || ''});
  const expectedNumber = process.env.GHQ_WA_LISTENER_NUMBER;
  const pairOnly = process.env.GHQ_WA_PAIR_ONLY === 'true';
  if (!listenerMatches(String(expectedNumber) + '@c.us', expectedNumber)) throw Error('Set the selected listening number privately');
  let snapshot = {revision:0, listenerId:LISTENER, rules:[]}, ready = false, busy = false, groupsAt = 0;
  // Optional recovery override for a session paired before its browser profile was recorded: chrome or desktop.
  const pairedProfile = process.env.GHQ_WA_PAIRED_BROWSER || undefined;
  const client = new SocketClient({dataPath:path.join(root,'socket-session'),expectedNumber,pairOnly,pairedProfile});
  const pairing = await createPairingPage(path.dirname(root));
  const note = message => console.log(new Date().toISOString(), message); // No message bodies, phone numbers, tokens or provider errors.
  client.on('qr', value => {void pairing.showQr(value).catch(()=>{pairing.setStatus('error');});note('A fresh QR is available on the private Codex pairing page.');});
  client.on('auth_failure', () => {ready=false;pairing.setStatus('error'); note('WhatsApp sign-in needs attention.');});
  client.on('disconnected', () => {ready=false;pairing.setStatus('reconnecting'); note('WhatsApp reconnecting.');});
  client.on('ready', () => {
    if (!listenerMatches(client.info?.wid?._serialized, expectedNumber)) {
      ready=false; note('This is not the selected group-listening account. Disconnecting without reading groups or sending.');
      void client.destroy(); return;
    }
    ready=true;pairing.setStatus('connected'); note('WhatsApp linked. Waiting for Growth HQ rules and deliberately selected groups.');
  });

  const phoneOf = async senderId => {
    if (/^[1-9][0-9]{6,14}@c\.us$/.test(senderId)) return senderId.split('@')[0];
    const mapping = await client.getContactLidAndPhone([senderId]);
    const pn = mapping?.[0]?.pn;
    return typeof pn === 'string' && /^[1-9][0-9]{6,14}@c\.us$/.test(pn) ? pn.split('@')[0] : null;
  };
  const {createDirectInbox} = require('./direct-inbox.cjs');
  const direct = createDirectInbox({api,client,root,expectedNumber,phoneOf});
  client.allowDirect = direct.enabled;
  client.allowHistory = direct.historyEnabled;
  client.prepareHistory = direct.prepareHistory;
  client.on('direct_history',(bundle,provider)=>{void direct.acceptHistory(bundle,provider).catch(()=>note('Private history import needs attention.'));});
  client.on('direct_message', event => { void direct.accept(event).catch(() => note('Private WhatsApp intake needs attention.')); });
  // Earlier group messages are read only for a catch-up Growth HQ has open; its failures never reach the loops below.
  const catchup = createCatchup({client,root,spool,phoneOf,pairOnly,note,current:()=>snapshot});
  client.wantGroupHistory = catchup.wanted;
  client.on('group_seen', message => { try { catchup.seen(message); } catch { /* Live rules do not depend on it. */ } });
  client.on('group_history', (bundle,provider) => { try { catchup.accept(bundle,provider); } catch { /* The page is asked for again. */ } });
  client.on('message', async msg => {
    try {
      if (pairOnly || !ready || msg.fromMe || typeof msg.body !== 'string') return;
      const observedAt = new Date(Number(msg.timestamp)*1000).toISOString();
      let payload;
      if (msg.from?.endsWith('@g.us')) {
        if (Math.abs(Date.now()-Date.parse(observedAt)) > 300000) return; // Live only. Earlier messages are read solely for an open catch-up (catchup.cjs).
        if (!findRule(snapshot, msg.from, msg.body) || !msg.author) return;
        const phone = await phoneOf(msg.author); if (!phone) {note('A matching request needs phone identity review; no DM sent.');return;}
        const contact = await msg.getContact().catch(()=>null);
        payload = {action:'events',value:{eventId:msg.id._serialized,listenerId:LISTENER,groupId:msg.from,
          senderId:msg.author,senderPhone:phone,senderName:String(contact?.pushname || contact?.name || '').slice(0,200),
          text:msg.body,observedAt}};
      } else if (/^(STOP|STOPALL|UNSUBSCRIBE|CANCEL|END|QUIT|OPTOUT|OPT OUT|START)$/i.test(msg.body.trim())) {
        const age=Date.now()-Date.parse(observedAt);
        if (age < -300000 || age > (msg.body.trim().toUpperCase()==='START'?300000:604800000)) return;
        const phone = await phoneOf(msg.from); if (!phone) return;
        payload={action:'opt-outs',value:{eventId:msg.id._serialized,senderPhone:phone,
          action:msg.body.trim().toUpperCase()==='START'?'START':'STOP',observedAt}};
      } else return;
      const key = crypto.createHash('sha256').update(payload.value.eventId).digest('hex');
      try { await fs.writeFile(path.join(spool,key+'.json'),JSON.stringify(payload),{flag:'wx',mode:0o600}); }
      catch(e) {
        if(e.code!=='EEXIST') throw e;
        if(await fs.readFile(path.join(spool,key+'.json'),'utf8')!==JSON.stringify(payload)) {
          await fs.writeFile(path.join(spool,key+'.'+crypto.randomUUID()+'.conflict'),JSON.stringify(payload),{flag:'wx',mode:0o600});
          note('An event ID returned different evidence. Preserved for review; no extra reply queued.');
        }
      }
    } catch {note('A request needs checking; no automatic DM was attempted.');}
  });

  async function drain() {
    if (pairOnly || !ready || busy) return; busy=true;
    try {
      snapshot = await api('GET','rules');
      void catchup.tick().catch(()=>note('Catch-up needs attention; live replies continue.'));
      if (Date.now()-groupsAt > 300000) {
        const chats = await client.getChats();
        await api('POST','groups',{listenerId:LISTENER,groups:chats.filter(c=>c.isGroup).map(c=>({id:c.id._serialized,name:String(c.name).slice(0,200)}))});
        groupsAt=Date.now();
      }
      const files=await Promise.all((await fs.readdir(spool)).filter(n=>n.endsWith('.json')).map(async name=>{
        const file=path.join(spool,name);return {file,event:JSON.parse(await fs.readFile(file,'utf8'))};
      }));
      // Controls precede every send even when a large intake backlog exists; a catch-up group's report follows its events.
      files.sort((a,b)=>spoolOrder(a.event.action)-spoolOrder(b.event.action));
      for (const {file,event} of files) {
        try {await api('POST',event.action,event.value);await fs.unlink(file);}
        catch(e) {if(setAside(e.status,event.action)){await fs.rename(file,file+'.held');note('An intake event needs review; preserved locally.');}else throw e;}
      }
      // Recovery never re-sends a calling operation. Only its recorded outcome is retried.
      for (const name of (await fs.readdir(outcomes)).filter(n=>n.endsWith('.json'))) {
        const file=path.join(outcomes,name), result=JSON.parse(await fs.readFile(file,'utf8'));
        await api('POST','deliveries/'+result.id+'/settle',result.result || {state:'outcome_unknown',providerMessageId:''});
        await fs.unlink(file);
      }
      const pending = await api('GET','pending');
      for (const id of pending.deliveryIds) {
        if (!/^[a-f0-9-]{36}$/.test(id)) throw Error('Invalid delivery ID');
        const permission = await api('POST','deliveries/'+id+'/begin',{});
        if (permission.send !== true) continue;
        const file=path.join(outcomes,id+'.json');
        // Persist calling marker before the provider side effect; a crash => unknown, never a resend.
        await saveJson(file,{id});
        let result;
        try {
          const sent = await client.sendMessage(permission.recipientId,permission.message);
          const providerMessageId=sent?.id?._serialized;
          result = typeof providerMessageId==='string' && providerMessageId.length<=250 ?
            {state:'sent',providerMessageId}:{state:'outcome_unknown',providerMessageId:''};
        } catch {result={state:'outcome_unknown',providerMessageId:''};}
        await saveJson(file,{id,result});
        await api('POST','deliveries/'+id+'/settle',result); await fs.unlink(file);
        note(result.state==='sent'?'Requested private reply accepted by WhatsApp.':'Send outcome needs checking; it will not be retried.');
      }
    } catch {note('Growth HQ connection needs checking. Saved requests remain in the outbox.');}
    finally {busy=false;}
  }
  const timer=setInterval(()=>{void catchup.flush().catch(()=>{});void drain();},5000);
  // A disabled or older inbox server must never interrupt the existing group rule loop.
  const directTimer = setInterval(() => { void direct.tick().catch(() => {}); }, 5000);
  const stop=async()=>{clearInterval(timer);clearInterval(directTimer);ready=false;await catchup.flush().catch(()=>{});await client.destroy().catch(()=>{});await pairing.close();process.exit(0);};
  process.on('SIGTERM',stop);process.on('SIGINT',stop);
  note('Private pairing page ready. No external browser is needed.');
  try {await client.initialize();}catch(error){pairing.setStatus('error');clearInterval(timer);clearInterval(directTimer);await client.destroy().catch(()=>{});throw error;}
}

if (require.main===module) main().catch(()=>{console.error('Listener could not start. Check its private configuration.');process.exitCode=1;});
module.exports={makeApi,findRule,listenerMatches};
