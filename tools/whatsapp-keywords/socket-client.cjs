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
  const eventId='group-keyword:'+crypto.createHash('sha256').update(JSON.stringify([packet.key.remoteJid,packet.key.participant||'',packet.key.id])).digest('hex');
  return {from,author,body,timestamp,fromMe:false,id:{_serialized:eventId},
    getContact:async()=>({pushname:typeof packet.pushName==='string'?packet.pushName.slice(0,200):''})};
}

class SocketClient extends EventEmitter {
  constructor({dataPath,expectedNumber,pairOnly=false,loadProvider=()=>import('@whiskeysockets/baileys'),authFactory=openAuthState,loggerFactory}={}) {
    super();Object.assign(this,{dataPath,expectedNumber,pairOnly,loadProvider,authFactory,loggerFactory});
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
    const socket=p.default({auth:this.auth.state,logger:this.logger,
      browser:fullHistory ? p.Browsers.windows('Desktop') : p.Browsers.ubuntu('Chrome'),markOnlineOnConnect:false,syncFullHistory:fullHistory,
      shouldSyncHistoryMessage:()=>!this.pairOnly && Boolean(this.allowHistory?.()),emitOwnEvents:false,generateHighQualityLinkPreview:false,
      connectTimeoutMs:30000,qrTimeout:60000});
    this.socket=socket;this.ready=false;
    socket.ev.on('creds.update',()=>{void this.auth.saveCreds().catch(()=>this.persistenceFailed());});
    socket.ev.on('connection.update',update=>{void this.connectionUpdate(socket,update).catch(()=>this.persistenceFailed());});
    socket.ev.on('messaging-history.set',bundle=>{
      if (!this.pairOnly && !this.closed && socket===this.socket && this.allowHistory?.()) {
        this.emit('direct_history',bundle,p);
      }
    });
    socket.ev.on('chats.upsert',chats=>{
      if (!this.pairOnly && !this.closed && socket===this.socket && this.allowHistory?.()) {
        const messages=chats.flatMap(c=>(c.messages||[]).map(entry=>entry.message).filter(Boolean));
        this.emit('direct_history',{chats,messages},p);
      }
    });
    socket.ev.on('messages.upsert',event=>{
      if(this.pairOnly||!this.ready||this.closed||socket!==this.socket)return;
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
        const raw=message.author||message.from;
        const alternative=toTransportJid(message.author?packet.key.participantAlt:packet.key.remoteJidAlt);
        if(raw.endsWith('@lid')&&alternative?.endsWith('@c.us'))this.mapping.set(raw,alternative);
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
module.exports={SocketClient,toTransportJid,privateSocketJid,incomingMessage};
