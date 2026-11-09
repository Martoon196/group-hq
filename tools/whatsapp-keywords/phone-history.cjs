const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const {privateNotification} = require('./direct-inbox.cjs');
const {privateSocketJid,toTransportJid} = require('./socket-client.cjs');
const {packets,submit} = require('./history-packets.cjs');

function archiveMessage(raw, provider, self) {
  const content = provider.normalizeMessageContent(raw.message);
  let body = content?.conversation ?? content?.extendedTextMessage?.text;
  if (!body) {
    const labels = {imageMessage:'Image',videoMessage:'Video',audioMessage:'Audio',documentMessage:'Document',stickerMessage:'Sticker'};
    const type = Object.keys(labels).find(key=>content?.[key]);
    if (!type) return null;
    body = content[type].caption || '[' + labels[type] + ']';
  }
  const normal = privateNotification({...raw, message:{conversation:body}}, 0, {normalizeMessageContent:x=>x}, self);
  if (!normal) return null;
  return {peer:normal.peer, name:normal.name, message:{id:normal.id,body:normal.body,direction:normal.direction,
    occurred_at:normal.at,name:normal.name,revision:''}};
}

async function openSpool(directory, journal) {
  await fs.mkdir(directory,{recursive:true,mode:0o700});
  let secret = journal.state.creds.historyKey;
  if (!secret) {
    if ((await fs.readdir(directory)).some(name=>name.endsWith('.hist'))) throw Error('Missing private history key');
    secret = crypto.randomBytes(32).toString('base64');
    journal.state.creds.historyKey = secret;
    await journal.saveCreds();
  }
  const key = Buffer.from(secret,'base64');
  const list = async()=>(await fs.readdir(directory)).filter(name=>/^[01]-[a-f0-9]{64}\.hist$/.test(name)).sort();
  async function append(packet) {
    const name = (packet.reached_start?'1-':'0-') + packet.chunk_id + '.hist';
    const target=path.join(directory,name);
    try {await fs.access(target);return;} catch(error){if(error.code!=='ENOENT')throw error;}
    const iv = crypto.randomBytes(12), algorithm = crypto.createCipheriv('aes-256-gcm',key,iv);
    const body = Buffer.concat([algorithm.update(JSON.stringify(packet),'utf8'),algorithm.final()]);
    const temporary = target + '.' + crypto.randomUUID() + '.tmp';
    const handle = await fs.open(temporary, 'wx', 0o600);
    try {await handle.writeFile(Buffer.concat([iv,algorithm.getAuthTag(),body]));await handle.sync();} finally {await handle.close();}
    await fs.rename(temporary,target);
  }
  async function read(name) {
    const value=await fs.readFile(path.join(directory,name));
    const algorithm=crypto.createDecipheriv('aes-256-gcm',key,value.subarray(0,12));
    algorithm.setAuthTag(value.subarray(12,28));
    const packet=JSON.parse(Buffer.concat([algorithm.update(value.subarray(28)),algorithm.final()]).toString());
    if (name.slice(2,66)!==packet.chunk_id) throw Error('History packet identity changed');
    return packet;
  }
  return {append,list,read,remove:name=>fs.unlink(path.join(directory,name))};
}

function createPhoneHistory({client, device, self, root, storage, enabled, call, now=Date.now, spoolFactory=openSpool}) {
  let spool, busy=false, incoming=Promise.resolve();
  const queue = ()=>(spool ??= storage().then(journal=>spoolFactory(path.join(root,'direct-history'),journal)));
  function peerState(journal,peer) {
    const peers=(journal.state.creds.historyPeers ??= {});
    const hash=crypto.createHash('sha256').update(peer).digest('hex');
    return (peers[hash] ??= {peer,requestedAt:0});
  }
  function remember(peer,m) {
    if (!m) return;
    if (!peer.oldest || Date.parse(m.occurred_at) < Date.parse(peer.oldest.occurred_at)) {
      peer.oldest={id:m.id,occurred_at:m.occurred_at,direction:m.direction};
    }
  }
  async function seed(event) {
    if (!enabled()) return;
    const messages=[{id:event.id,body:event.body,name:event.name||'',direction:event.direction,occurred_at:event.at,revision:''}];
    const projection={account_key:'personal-whatsapp-device',account_id:device,user_id:device,observed_at:new Date(now()).toISOString(),
      thread:{id:event.peer,participant_id:event.peer,name:event.name||'WhatsApp contact',is_group:false,is_merged:false,
        preview:'',updated_at:event.at,has_more:true,messages}};
    const files=await queue();
    for(const packet of packets(projection,false,'whatsapp-device'))await files.append(packet);
  }
  async function storeBundle(bundle,provider) {
    if (!enabled()) return;
    const groups=new Map();
    const get=peer=>{
      if (!groups.has(peer)) groups.set(peer,{name:'',items:new Map(),end:false,omitted:0});
      return groups.get(peer);
    };
    for (const c of bundle.chats ?? []) {
      const peer=toTransportJid(c.id);
      if (!peer || peer.endsWith('@g.us') || peer===self) continue;
      const group=get(peer);
      group.name=typeof c.name==='string' ? c.name.slice(0,200) : '';
      group.end=Object.hasOwn(c,'endOfHistoryTransferType') && c.endOfHistoryTransferType===1;
    }
    for (const raw of bundle.messages ?? []) {
      const entry=archiveMessage(raw,provider,self);
      if (!entry) {
        const peer=toTransportJid(raw.key?.remoteJid);
        if (peer && peer!==self && !peer.endsWith('@g.us') && !raw.key?.participant) get(peer).omitted++;
        continue;
      }
      const group=get(entry.peer);
      group.name ||= entry.name;
      group.items.set(entry.message.id,entry.message);
    }
    const files=await queue();
    for (const [peer,group] of groups) {
      if (!enabled()) return;
      if (!group.items.size && !group.end && !group.omitted) continue;
      const messages=[...group.items.values()].sort((a,b)=>Date.parse(a.occurred_at)-Date.parse(b.occurred_at) || a.id.localeCompare(b.id));
      const projection={account_key:'personal-whatsapp-device',account_id:device,user_id:device,
        observed_at:new Date(now()).toISOString(),evidence:{omitted_rows:group.omitted},thread:{id:peer,participant_id:peer,name:group.name||'WhatsApp contact',
          is_group:false,is_merged:false,preview:'',updated_at:messages.at(-1)?.occurred_at||'',has_more:!group.end,messages}};
      for(const packet of packets(projection,group.end,'whatsapp-device')) await files.append(packet);
    }
  }
  function accept(bundle,provider) {
    const next=incoming.then(()=>storeBundle(bundle,provider));
    incoming=next.catch(()=>{});
    return next;
  }
  async function tick() {
    if (busy || !enabled()) return;
    busy=true;
    try {
      await incoming;
      const journal=await storage(), files=await queue(), names=await files.list();
      for(const name of names.slice(0,8)) {
        if(!enabled())return;
        const packet=await files.read(name);
        packet.observed_at=new Date(now()).toISOString();
        await submit(p=>call('history',p),[packet]);
        const peer=peerState(journal,packet.thread.id);
        remember(peer,packet.thread.messages[0]);
        if(packet.reached_start)peer.complete=true;
        await journal.saveCreds();
        await files.remove(name);
      }
      if(names.length || !enabled())return;
      const peers=Object.values(journal.state.creds.historyPeers ?? {});
      const peer=peers.filter(p=>!p.complete && p.oldest &&
        (p.requestedFor!==p.oldest.id || now()-p.requestedAt>=600000)).sort((a,b)=>a.requestedAt-b.requestedAt)[0];
      if(!peer)return;
      const from=peer.oldest;
      const key={remoteJid:privateSocketJid(peer.peer),fromMe:from.direction==='outgoing',id:from.id.slice('baileys:'.length)};
      peer.requestedAt=now();peer.requestedFor=from.id;
      await journal.saveCreds();
      if(enabled() && client.ready && !client.closed) await client.socket.fetchMessageHistory(100,key,Date.parse(from.occurred_at));
    } finally {busy=false;}
  }
  return {accept,seed,tick};
}
module.exports={createPhoneHistory,archiveMessage,openSpool};
