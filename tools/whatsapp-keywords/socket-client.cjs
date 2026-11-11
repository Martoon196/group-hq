const {EventEmitter}=require('node:events');
const crypto=require('node:crypto');
const {openAuthState}=require('./socket-auth.cjs');

function toTransportJid(jid) {
  if(typeof jid!=='string')return null;
  const match=/^([1-9][0-9]{4,29})(?::[0-9]+)?@(s\.whatsapp\.net|c\.us|lid)$/.exec(jid);
  if(match)return match[1]+(match[2]==='lid'?'@lid':'@c.us');
  return /^[0-9-]{5,80}@g\.us$/.test(jid)?jid:null;
}
function privateSocketJid(jid) {
  if(!/^[1-9][0-9]{4,29}@(c\.us|lid)$/.test(jid))throw Error('Private recipient required');
  return jid.replace(/@c\.us$/,'@s.whatsapp.net');
}
const eventIdOf=(remoteJid,participant,id)=>'group-keyword:'+crypto.createHash('sha256').update(JSON.stringify([remoteJid,participant||'',id])).digest('hex');
// A group message as the phone keeps it, live or from history: its key (enough to ask the phone for what came before it),
// time, sender and text. Only a message the phone stores as a chat row is returned; no freshness rule applies here.
const STORED=['imageMessage','videoMessage','audioMessage','documentMessage','stickerMessage','contactMessage','locationMessage'];
function groupPacket(packet,provider) {
  const key=packet?.key, groupId=toTransportJid(key?.remoteJid);
  if(!groupId?.endsWith('@g.us')||typeof key.id!=='string'||!key.id.length||key.id.length>200)return null;
  const stamp=packet.messageTimestamp, timestamp=typeof stamp==='object'&&stamp?.toNumber?stamp.toNumber():Number(stamp);
  if(!Number.isFinite(timestamp)||timestamp<=0)return null;
  const content=provider.normalizeMessageContent(packet.message);
  const text=content?.conversation??content?.extendedTextMessage?.text;
  if(typeof text!=='string'&&!STORED.some(type=>content?.[type]))return null;
  // The phone's history copy may carry the sender beside the key rather than inside it.
  const participant=key.participant||packet.participant||'', author=key.fromMe?null:toTransportJid(participant);
  return {groupId,id:key.id,fromMe:Boolean(key.fromMe),timestamp,author:author&&!author.endsWith('@g.us')?author:null,
    alt:toTransportJid(key.participantAlt),body:typeof text==='string'&&text.length<=4096?text:null,
    name:typeof packet.pushName==='string'?packet.pushName.slice(0,200):'',eventId:eventIdOf(key.remoteJid,participant,key.id)};
}
function incomingMessage(packet,connectedAt,provider) {
  if(packet?.key?.fromMe||!packet?.key?.id||typeof packet.key.id!=='string')return null;
  const timestamp=Number(packet.messageTimestamp);
  if(!Number.isFinite(timestamp)||timestamp*1000<connectedAt-2000||timestamp*1000>Date.now()+300000)return null;
  const from=toTransportJid(packet.key.remoteJid);
  if(!from)return null;
  const content=provider.normalizeMessageContent(packet.message);
  const body=content?.conversation??content?.extendedTextMessage?.text;
  if(typeof body!=='string')return null;
  const author=from.endsWith('@g.us')?toTransportJid(packet.key.participant):undefined;
  if(from.endsWith('@g.us')&&(!author||author.endsWith('@g.us')))return null;
  const eventId=eventIdOf(packet.key.remoteJid,packet.key.participant,packet.key.id);
  return {from,author,body,timestamp,fromMe:false,id:{_serialized:eventId},
    getContact:async()=>({pushname:typeof packet.pushName==='string'?packet.pushName.slice(0,200):''})};
}

class SocketClient extends EventEmitter {
  constructor({dataPath,expectedNumber,pairOnly=false,pairedProfile,loadProvider=()=>import('@whiskeysockets/baileys'),authFactory=openAuthState,loggerFactory}={}) {
    if(pairedProfile!==undefined&&!['chrome','desktop'].includes(pairedProfile))throw Error('Paired profile must be chrome or desktop');
    super();Object.assign(this,{dataPath,expectedNumber,pairOnly,pairedProfile,loadProvider,authFactory,loggerFactory});
    this.ready=false;this.closed=false;this.retries=0;this.mapping=new Map();
  }
  async initialize() {
    if(!/^[1-9][0-9]{6,14}$/.test(this.expectedNumber||''))throw Error('Selected account is required');
    this.provider=await this.loadProvider();
    this.auth=await this.authFactory(this.dataPath,this.provider);
    this.logger=this.loggerFactory?this.loggerFactory():require('pino')({level:'silent'});
    return this.connect();
  }
  async connect() {
    if(this.closed)return;
    const p=this.provider;
    let fullHistory=false;
    if (!this.pairOnly && this.prepareHistory) {
      try { fullHistory=await this.prepareHistory(); } catch { /* Older servers still support group rules. */ }
    }
    // A linked device must keep the identity it was paired with: WhatsApp closes the stream (428) when a paired
    // session reconnects as a different browser. Only a fresh pairing may choose the desktop history profile.
    const creds=this.auth.state.creds;
    const profile=creds.ghqBrowser || (creds.me?.id ? (this.pairedProfile || 'chrome') : (fullHistory ? 'desktop' : 'chrome'));
    this.profile=profile;
    const socket=p.default({auth:this.auth.state,logger:this.logger,
      browser:profile==='desktop' ? p.Browsers.windows('Desktop') : p.Browsers.ubuntu('Chrome'),markOnlineOnConnect:false,syncFullHistory:fullHistory,
      shouldSyncHistoryMessage:()=>!this.pairOnly && Boolean(this.allowHistory?.()),emitOwnEvents:false,generateHighQualityLinkPreview:false,
      connectTimeoutMs:30000,qrTimeout:60000});
    this.socket=socket;this.ready=false;
    socket.ev.on('creds.update',()=>{void this.auth.saveCreds().catch(()=>this.persistenceFailed());});
    socket.ev.on('connection.update',update=>{void this.connectionUpdate(socket,update).catch(()=>this.persistenceFailed());});
    socket.ev.on('messaging-history.set',bundle=>{
      if (this.pairOnly || this.closed || socket!==this.socket) return;
      for (const packet of bundle.messages||[]) this.groupSeen(packet);
      if (this.allowHistory?.()) this.emit('direct_history',bundle,p);
    });
    socket.ev.on('chats.upsert',chats=>{
      if (!this.pairOnly && !this.closed && socket===this.socket && this.allowHistory?.()) {
        const messages=chats.flatMap(c=>(c.messages||[]).map(entry=>entry.message).filter(Boolean));
        this.emit('direct_history',{chats,messages},p);
      }
    });
    socket.ev.on('messages.upsert',event=>{
      if(this.pairOnly||!this.ready||this.closed||socket!==this.socket)return;
      for(const packet of event.messages||[]) {
        // The phone's answer to fetchGroupHistory is a notice in this stream; it is read only while a catch-up waits for it.
        if(this.wantGroupHistory?.())void this.groupHistoryNotice(socket,packet).catch(()=>{});
        this.groupSeen(packet);
      }
      if (this.allowHistory?.()) {
        const archived=(event.messages||[]).filter(m=>event.type==='append' || event.requestId ||
          Number(m.messageTimestamp)*1000 < this.connectedAt-2000);
        if (archived.length) this.emit('direct_history',{chats:[],messages:archived},p);
      }
      if(event.type!=='notify'||event.requestId)return;
      for(const packet of event.messages||[]) {
        if (this.allowDirect?.()) {
          const {privateNotification} = require('./direct-inbox.cjs');
          const event = privateNotification(packet, this.connectedAt, p, this.info.wid._serialized);
          if (event) this.emit('direct_message', event);
        }
        const message=incomingMessage(packet,this.connectedAt,p);
        if(!message)continue;
        this.remember(message.author||message.from,toTransportJid(message.author?packet.key.participantAlt:packet.key.remoteJidAlt));
        this.emit('message',message);
      }
    });
  }
  async connectionUpdate(socket,update) {
    if(this.closed||socket!==this.socket)return;
    if(update.qr)this.emit('qr',update.qr);
    if(update.connection==='open') {
      const actual=toTransportJid(socket.user?.id);
      if(actual!==this.expectedNumber+'@c.us') {
        this.closed=true;this.ready=false;socket.end(undefined);this.emit('auth_failure');return;
      }
      if(!this.auth.state.creds.ghqBrowser)this.auth.state.creds.ghqBrowser=this.profile;
      await this.auth.saveCreds();await this.auth.flush();
      if(this.closed||socket!==this.socket)return;
      this.info={wid:{_serialized:actual}};this.connectedAt=Date.now();this.ready=true;this.retries=0;this.emit('ready');
    }
    if(update.connection==='close') {
      this.ready=false;this.emit('disconnected');
      const code=update.lastDisconnect?.error?.output?.statusCode;
      if(code===this.provider.DisconnectReason.loggedOut||code===this.provider.DisconnectReason.badSession||++this.retries>5) {
        this.closed=true;this.emit('auth_failure');return;
      }
      await this.auth.flush();
      const delay=code===this.provider.DisconnectReason.restartRequired?100:Math.min(30000,this.retries*3000);
      this.reconnectTimer=setTimeout(()=>{void this.connect().catch(()=>this.persistenceFailed());},delay);
    }
  }
  remember(lid,pn) {if(lid?.endsWith('@lid')&&pn?.endsWith('@c.us'))this.mapping.set(lid,pn);}
  // Every group's newest stored message is the anchor a later catch-up pages back from (catchup.cjs keeps one per group).
  groupSeen(packet) {try{const seen=groupPacket(packet,this.provider);if(seen)this.emit('group_seen',seen);}catch{/* Live intake does not depend on it. */}}
  async groupHistoryNotice(socket,packet) {
    const p=this.provider, notice=p.getHistoryMsg(packet.message);
    // Only this account's own phone can answer; Baileys drops the same notice from anyone else.
    if(!notice||packet.key?.fromMe!==true||notice.syncType!==p.proto.HistorySync.HistorySyncType.ON_DEMAND)return;
    // Read here rather than from Baileys' messaging-history.set: its event buffer drops any message it has already
    // emitted in a history set, so a second catch-up over the same days would silently see nothing.
    const data=await p.downloadAndProcessHistorySyncNotification(notice,{});
    if(this.pairOnly||this.closed||socket!==this.socket||!this.wantGroupHistory?.())return;
    for(const pair of data.lidPnMappings||[])this.remember(toTransportJid(pair.lid),toTransportJid(pair.pn));
    this.emit('group_history',{messages:data.messages||[],chats:data.chats||[],sessionId:notice.peerDataRequestSessionId||null},p);
  }
  persistenceFailed() {this.ready=false;this.closed=true;clearTimeout(this.reconnectTimer);this.socket?.end(undefined);this.emit('auth_failure');}
  requireReady() {if(!this.ready||this.closed)throw Error('WhatsApp is not linked');}
  async getChats() {
    if(this.pairOnly)throw Error('Pairing only; group access is off');
    this.requireReady();
    const groups=await this.socket.groupFetchAllParticipating();
    return Object.values(groups).filter(g=>/^[0-9-]{5,80}@g\.us$/.test(g.id)).map(g=>({isGroup:true,id:{_serialized:g.id},name:g.subject||'WhatsApp group'}));
  }
  async getContactLidAndPhone(ids) {
    if(this.pairOnly)throw Error('Pairing only; contact access is off');
    this.requireReady();
    return Promise.all(ids.map(async id=>{
      const pn=id.endsWith('@c.us')?id:this.mapping.get(id)||toTransportJid(await this.socket.signalRepository.lidMapping.getPNForLID(id));
      return {pn:pn?.endsWith('@c.us')?pn:null};
    }));
  }
  // One page of a group's earlier messages, older than the given stored message. The phone answers out of band.
  async fetchGroupHistory(groupId,before) {
    if(this.pairOnly)throw Error('Pairing only; group history is off');
    this.requireReady();
    if(!/^[0-9-]{5,80}@g\.us$/.test(groupId)||typeof before?.id!=='string'||!Number.isFinite(before.timestamp))throw Error('Group message required');
    return this.socket.fetchMessageHistory(50,{remoteJid:groupId,fromMe:Boolean(before.fromMe),id:before.id},before.timestamp*1000);
  }
  async sendMessage(recipient,message) {
    if(this.pairOnly)throw Error('Pairing only; sending is off');
    this.requireReady();const jid=privateSocketJid(recipient);
    if(typeof message!=='string'||!message.length||message.length>4000)throw Error('Invalid private reply');
    await this.auth.flush();this.requireReady();
    const sent=await this.socket.sendMessage(jid,{text:message,linkPreview:null});
    return {id:{_serialized:typeof sent?.key?.id==='string'?'baileys:'+sent.key.id:''}};
  }
  async destroy() {this.closed=true;this.ready=false;clearTimeout(this.reconnectTimer);this.socket?.end(undefined);await this.auth?.flush();}
}
module.exports={SocketClient,toTransportJid,privateSocketJid,incomingMessage,groupPacket};
