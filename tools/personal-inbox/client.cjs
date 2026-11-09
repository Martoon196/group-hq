const crypto=require('node:crypto');
const ENGINE='http://127.0.0.1:23373';
const GHQ='https://group-hq.onrender.com';
const BASE='/api/personal-inbox/v1';
const opaque=value=>{if(typeof value!=='string'||value.length<1||value.length>500||/[\x00-\x20\x7f-\uffff]/u.test(value)||['.','..'].includes(value))throw Error('invalid_identity');return value;};
const text=(value,max=20000)=>{if(typeof value!=='string'||value.length>max)throw Error('invalid_text');return value;};
const channel=account=>({facebook:'facebook',facebookgo:'facebook',meta:'facebook',messenger:'facebook',linkedin:'linkedin',linkedingo:'linkedin'}[account.bridge?.type]);

class EngineClient {
  constructor(token,fetcher=fetch){this.token=token;this.fetcher=fetcher;}
  async request(path,{method='GET',body}={}){
    if(!path.startsWith('/v1/')||path.includes('://'))throw Error('invalid_path');
    let response;try{response=await this.fetcher(ENGINE+path,{method,redirect:'error',signal:AbortSignal.timeout(10000),
      headers:{Authorization:'Bearer '+this.token,'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});}
    catch{throw Error(method==='GET'?'engine_unavailable':'send_outcome_unknown');}
    if(!response.ok)throw Error(method!=='GET'&&(response.status>=500||response.status===408)?'send_outcome_unknown':'engine_http_'+response.status);
    const content=await response.text();if(Buffer.byteLength(content)>2_000_000)throw Error(method==='GET'?'invalid_response':'send_outcome_unknown');
    try{return JSON.parse(content);}catch{throw Error(method==='GET'?'invalid_response':'send_outcome_unknown');}
  }
  async accounts(){
    const rows=await this.request('/v1/accounts');if(!Array.isArray(rows))throw Error('invalid_accounts');
    return rows.filter(a=>channel(a)&&['local','cloud','self-hosted','platform-sdk'].includes(a.bridge?.provider)).map(a=>{
      opaque(a.accountID);opaque(a.user?.id);return a;
    });
  }
  async boundAccount(binding){
    const found=(await this.accounts()).find(a=>a.accountID===binding.account_id&&a.user.id===binding.user_id&&channel(a)===binding.channel);
    if(!found||!['connected','backfilling'].includes(found.status))throw Error('account_not_ready');
    return found;
  }
  async chats(binding,cursor){
    await this.boundAccount(binding);
    const query=new URLSearchParams({accountIDs:binding.account_id,limit:'200'});if(cursor)query.set('cursor',cursor);
    const page=await this.request('/v1/chats?'+query);
    if(!Array.isArray(page.items)||page.items.length>200)throw Error('invalid_chats');
    for(const c of page.items)if(c.accountID!==binding.account_id)throw Error('cross_account_chat');
    return page;
  }
  async chat(binding,chatId,forSend=true){
    const c=await this.request('/v1/chats/'+encodeURIComponent(opaque(chatId)));
    if(c.id!==chatId||c.accountID!==binding.account_id||c.type!=='single'||c.merge||(forSend && c.isReadOnly===true))throw Error('unbound_chat');
    return c;
  }
  async messages(binding,c,cursor){
    if(c.accountID!==binding.account_id||c.type!=='single'||c.merge)throw Error('unbound_chat');
    const query = cursor ? '?' + new URLSearchParams({cursor, direction:'before'}) : '';
    const page=await this.request('/v1/chats/'+encodeURIComponent(opaque(c.id))+'/messages'+query);
    if(!Array.isArray(page.items))throw Error('invalid_messages');
    for(const m of page.items)if(m.chatID!==c.id||m.accountID!==binding.account_id)throw Error('cross_account_message');
    return page;
  }
  async send(binding,chatId,body){
    if(typeof body!=='string'||!body.trim()||Buffer.byteLength(body)>10000)throw Error('invalid_reply');
    await this.boundAccount(binding);await this.chat(binding,chatId);
    const result=await this.request('/v1/chats/'+encodeURIComponent(opaque(chatId))+'/messages',{method:'POST',body:{text:body}});
    if(result.chatID!==chatId)throw Error('send_outcome_unknown');
    try{return opaque(result.pendingMessageID);}catch{throw Error('send_outcome_unknown');}
  }
  async outcome(binding,chatId,pendingId){
    await this.boundAccount(binding);
    const m=await this.request('/v1/chats/'+encodeURIComponent(opaque(chatId))+'/messages/'+encodeURIComponent(opaque(pendingId)));
    if(m.chatID!==chatId||m.accountID!==binding.account_id||m.isSender!==true)throw Error('unbound_outcome');
    return {id:opaque(m.id),status:m.sendStatus?.status??'PENDING'};
  }
}

function snapshot(binding,c,page){
  if(c.type!=='single'||c.merge||c.accountID!==binding.account_id)throw Error('unbound_chat');
  const people=c.participants?.items;
  if(!Array.isArray(people))throw Error('missing_participants');
  const peers=people.filter(p=>p.isSelf!==true&&p.id!==binding.user_id);
  if(peers.length!==1||c.participants.hasMore===true)throw Error('ambiguous_participant');
  const messages=page.items.filter(m=>!m.isHidden).map(m=>{
    if(m.accountID!==binding.account_id||m.chatID!==c.id)throw Error('cross_account_message');
    return {id:opaque(m.id),body:m.isDeleted?'[Message deleted]':text(m.text||'[Attachment]'),
      name:text(m.senderName||'',500),direction:m.isSender===true?'outgoing':'incoming',
      occurred_at:text(m.timestamp,100),revision:text((m.editedTimestamp||'')+(m.isDeleted?':deleted':''),100)};
  }).sort((a,b)=>a.occurred_at.localeCompare(b.occurred_at));
  return {account_key:binding.key,account_id:binding.account_id,user_id:binding.user_id,observed_at:new Date().toISOString(),
    thread:{id:opaque(c.id),participant_id:opaque(peers[0].id),name:text(c.title||peers[0].fullName||'Unnamed contact',500),
      preview:messages.at(-1)?.body||'',updated_at:text(c.lastActivity||messages.at(-1)?.occurred_at||'',100),
      is_group:false,is_merged:false,messages,has_more:page.hasMore===true}};
}

class GrowthHqClient {
  constructor(secret,fetcher=fetch){if(typeof secret!=='string'||secret.length<32||secret.length>256)throw Error('missing_connector_key');this.secret=secret;this.fetcher=fetcher;}
  async request(action,value,method='POST'){
    if(!/^(accounts|threads|history|pending|candidates|selections|replies\/[a-f0-9-]{36}\/(begin|settle))$/u.test(action))throw Error('invalid_action');
    const path=BASE+'/'+action,body=method==='GET'?'':JSON.stringify(value??{}),stamp=String(Math.floor(Date.now()/1000));
    const signature=crypto.createHmac('sha256',this.secret).update(`${stamp}\n${method}\n${path}\n`).update(body).digest('hex');
    const r=await this.fetcher(GHQ+path,{method,redirect:'error',signal:AbortSignal.timeout(20000),
      headers:{'content-type':'application/json','x-ghq-timestamp':stamp,'x-ghq-signature':signature},body:method==='GET'?undefined:body});
    if(!r.ok)throw Error('growth_hq_http_'+r.status);
    const bytes=await r.text();if(Buffer.byteLength(bytes)>750000)throw Error('invalid_growth_hq_response');return JSON.parse(bytes);
  }
}
module.exports={EngineClient,GrowthHqClient,snapshot,channel,opaque,ENGINE,GHQ,BASE};
