'use strict';
const {createHash}=require('node:crypto');
const {displayedTime,wallTime,readConversation}=require('./reader.cjs');
const {readPeer}=require('./discovery.cjs');
const sha=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const months=['january','february','march','april','may','june','july','august','september','october','november','december'];

function absoluteTime(label,observedAt,timeZone,context=''){
  let value=label.trim().replace(/^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+/i,'');
  const clock=/^(\d{1,2}):(\d{2})(?:\s*([ap]m))?$/i;
  if(clock.test(value)&&context)value=context.replace(/^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+/i,'')+' '+value;
  const numeric=value.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:,?\s+(?:at\s+)?)?(\d{1,2}):(\d{2})(?:\s*([ap]m))?$/i);
  const match=value.match(/^(?:(\d{1,2})\s+([A-Za-z]+)|([A-Za-z]+)\s+(\d{1,2})),?\s+(\d{4})(?:,?\s+(?:at\s+)?)?(\d{1,2}):(\d{2})(?:\s*([ap]m))?$/i)||
    (numeric?[null,numeric[1],months[+numeric[2]-1]||'invalid',null,null,numeric[3],numeric[4],numeric[5],numeric[6]]:null);
  if(!match){
    // No year is invented for an old month/day label. The existing explicit
    // Today/Yesterday/recent-weekday forms retain their original semantics.
    if(context)throw Error('unsupported_displayed_time');
    if(/^(Today|Yesterday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) \d{1,2}:\d{2}$/.test(label))return displayedTime(label,observedAt,timeZone);
    if(clock.test(label))throw Error('date_context_required');
    throw Error('unsupported_displayed_time');
  }
  const day=+(match[1]||match[4]),monthText=(match[2]||match[3]).toLowerCase();
  const month=months.findIndex(m=>m===monthText||m.slice(0,3)===monthText||(m==='september'&&monthText==='sept')),year=+match[5];
  let hour=+match[6];const minute=+match[7],ampm=match[8]?.toLowerCase();
  if(month<0||minute>59||(ampm?(hour<1||hour>12):hour>23))throw Error('unsupported_displayed_time');
  if(ampm)hour=hour%12+(ampm==='pm'?12:0);
  const wall=Date.UTC(year,month,day,hour,minute),date=new Date(wall),observed=new Date(observedAt);
  if(date.getUTCFullYear()!==year||date.getUTCMonth()!==month||date.getUTCDate()!==day||!Number.isFinite(observed.getTime()))throw Error('unsupported_displayed_time');
  return wallTime(wall,observed,timeZone);
}

function normaliseArchive(raw,target,binding){
  if(raw.threadId!==target.threadId||raw.participantId!==target.participantId||raw.name!==target.name)throw Error('wrong_thread_binding');
  if(binding.channel!=='facebook'||!/^\d+$/.test(binding.user_id)||binding.account_id!==`facebook-web:${binding.user_id}`)throw Error('wrong_account_binding');
  if(!Array.isArray(raw.messages))throw Error('unsupported_message_window');
  const ids=new Set(),messages=[];let omitted=0;
  for(const row of raw.messages){
    const valid=target.threadKind==='page'?typeof row.id==='string'&&target.threadId===target.participantId&&/^mid\.\$[A-Za-z0-9_-]{1,200}$/.test(row.id):
      typeof row.id==='string'&&row.id.startsWith(target.participantId+'@msgr.')&&/^\d+@msgr\.\d+$/.test(row.id);
    if(!valid||ids.has(row.id))throw Error('invalid_message_identity');ids.add(row.id);
    const outgoing=row.heading==='You sent',sender=outgoing?'You':row.heading;
    if(typeof row.label!=='string'||row.label.length>22000||(!outgoing&&!target.name.split(' ').includes(sender))){omitted++;continue;}
    const separator=', '+sender+': ',at=row.label.indexOf(separator);
    if(!row.label.startsWith('At ')||at<3){omitted++;continue;}
    const body=row.label.slice(at+separator.length),dateLabel=row.label.slice(3,at);
    if(!body||body.length>20000){omitted++;continue;}
    let occurredAt;
    try{occurredAt=absoluteTime(dateLabel,raw.observedAt,binding.timeZone,row.context_date||'');}catch{omitted++;continue;}
    messages.push({id:row.id,body,name:outgoing?'Martin Howard':target.name,direction:outgoing?'outgoing':'incoming',
      occurred_at:occurredAt,revision:'dom:'+sha([body,outgoing,occurredAt]),source_time:dateLabel,time_precision:'minute'});
  }
  messages.sort((a,b)=>a.occurred_at.localeCompare(b.occurred_at));
  const boundary=raw.introductionVisible===true&&raw.at_oldest_scroll===true&&!raw.restoring&&!raw.restore_required;
  return {messages,omitted,boundary,rawIds:[...ids],blocked:raw.restore_required?'restore_required':raw.restoring?'history_restoring':null};
}

function chunks(raw,target,binding,normalised,maxBytes=500000){
  const packets=[],groups=[];let group=[];
  for(const message of normalised.messages){
    const next=[...group,message];
    if(group.length&&(next.length>100||Buffer.byteLength(JSON.stringify(next))>maxBytes-10000)){groups.push(group);group=[];}
    group.push(message);
    if(Buffer.byteLength(JSON.stringify(group))>maxBytes-10000)throw Error('history_message_too_large');
  }
  groups.push(group);
  for(const [i,messages] of groups.entries()){
    const reached=normalised.boundary&&i===groups.length-1;
    const packet={connector_id:'native-messenger',account_key:binding.key,account_id:binding.account_id,user_id:binding.user_id,
      observed_at:raw.observedAt,thread:{id:target.threadId,participant_id:target.participantId,name:target.name,
        is_group:false,is_merged:false,messages,preview:messages.at(-1)?.body||'',updated_at:messages.at(-1)?.occurred_at||'',
        has_more:!reached||normalised.omitted>0},reached_start:reached,oldest_id:messages[0]?.id??null,newest_id:messages.at(-1)?.id??null,
      evidence:{transport:'growth-hq-browser',history:'rendered_archive',omitted_rows:i===0?normalised.omitted:0,time_precision:'minute'}};
    packet.chunk_id=sha([packet.account_key,packet.account_id,packet.user_id,packet.thread,packet.reached_start,packet.evidence,
      normalised.rawIds]);
    if(Buffer.byteLength(JSON.stringify(packet))>maxBytes)throw Error('history_chunk_too_large');
    packets.push(packet);
  }
  return packets;
}

function scrollOlder(expected){
  const log=[...document.querySelectorAll('[role="log"]')].find(e=>e.getAttribute('aria-label')===`Messages in conversation with ${expected.name}`);
  if(!log)throw Error('conversation_not_ready');
  let node=log;while(node.parentElement&&node.scrollHeight<=node.clientHeight+1)node=node.parentElement;
  const before=node.scrollTop;node.scrollBy({top:-Math.max(100,node.clientHeight*0.75),behavior:'instant'});
  return {before,after:node.scrollTop};
}

async function acknowledge(cloud,state,packets){
  for(const packet of packets){
    const ack=await cloud.request('history',packet);
    if(ack.ok!==true||ack.chunk_id!==packet.chunk_id||!Number.isInteger(ack.stored)||ack.stored<0||ack.reached_start!==packet.reached_start)throw Error('history_ack_unconfirmed');
  }
  // Caller advances its cursor only after every packet in this rendered window.
  return packets.length;
}

class MessengerHistory {
  constructor(context){this.context=context;this.page=null;this.active=null;this.offset=0;this.seeking=false;}
  async close(){if(this.page&&!this.page.isClosed())await this.page.close();this.page=null;}
  key(target,binding){return sha([binding.account_id,binding.user_id,target.threadId,target.participantId]);}
  progress(state,target,binding){state.state.creds.history??={};const key=this.key(target,binding);
    return state.state.creds.history[key]??=( {parser:1,anchor_ids:[],chunks:0,omitted_observations:0,reached_start:false,stalls:0} );}
  noticeRecent(state,target,binding,messages){
    const progress=this.progress(state,target,binding),current=sha(messages.map(m=>[m.id,m.revision]));
    if(progress.recent_notice&&progress.recent_notice!==current&&progress.reached_start)progress.pending_latest=true;
    progress.recent_notice=current;
  }
  async tick(adapter,cloud,state,binding,targets,isPaused=()=>false){
    if(isPaused()||!targets.length)return {state:'paused'};
    let target=targets.find(t=>t.threadId===this.active);
    if(target){const p=this.progress(state,target,binding);if((p.reached_start&&!p.pending_latest)||(p.retry_at??0)>Date.now())target=null;}
    if(!target){for(let i=0;i<targets.length;i++){const candidate=targets[(this.offset+i)%targets.length],p=this.progress(state,candidate,binding);
      if((!p.reached_start||p.pending_latest)&&(p.retry_at??0)<=Date.now()){target=candidate;this.offset=(this.offset+i+1)%targets.length;break;}}}
    if(!target)return {state:'idle'};
    const progress=this.progress(state,target,binding),latestOnly=progress.reached_start&&progress.pending_latest;
    try{
      await adapter.verifyIdentity();if(isPaused())return {state:'paused'};
      if(!this.page||this.page.isClosed()){this.page=await this.context.newPage();this.active=null;}
      if(this.active!==target.threadId||latestOnly){
        await this.page.goto(`https://www.facebook.com/messages/${target.threadKind==='page'?'':'e2ee/'}t/${target.threadId}`,{waitUntil:'domcontentloaded',timeout:30000});
        this.active=target.threadId;this.seeking=progress.anchor_ids.length>0&&!latestOnly;
      }
      await this.page.waitForFunction(readPeer,target.threadId,{timeout:10000});
      const peer=await this.page.evaluate(readPeer,target.threadId);
      if(!peer||peer.participantId!==target.participantId)throw Error('wrong_participant');
      const actual={...target,name:peer.name,archive:true},raw=await this.page.evaluate(readConversation,actual),normalised=normaliseArchive(raw,actual,binding);
      if(normalised.blocked){progress.reason=normalised.blocked;progress.retry_at=Date.now()+60000;this.active=null;await state.saveCreds();return {state:normalised.blocked};}
      if(isPaused())return {state:'paused'};
      const packets=chunks(raw,actual,binding,normalised);
      await acknowledge(cloud,state,packets);
      progress.chunks+=packets.length;progress.omitted_observations+=normalised.omitted;
      const overlap=progress.anchor_ids.some(id=>normalised.rawIds.includes(id));
      if(!this.seeking||overlap||normalised.boundary){progress.anchor_ids=normalised.rawIds.slice(0,25);this.seeking=false;}
      const window=sha(normalised.rawIds);progress.stalls=progress.window===window?progress.stalls+1:0;progress.window=window;
      progress.pending_latest=false;progress.reason=normalised.omitted?'some_rows_unsupported':null;progress.retry_at=0;
      if(normalised.boundary)progress.reached_start=true;
      await state.saveCreds();
      if(latestOnly||normalised.boundary){this.active=null;return {state:'boundary_observed',chunks:packets.length,omitted:normalised.omitted};}
      if(progress.stalls>=3){progress.reason='older_boundary_unconfirmed';progress.retry_at=Date.now()+60000;this.active=null;await state.saveCreds();return {state:'older_boundary_unconfirmed'};}
      await this.page.evaluate(scrollOlder,actual);await this.page.waitForTimeout(700);
      return {state:this.seeking?'seeking_saved_cursor':'reading_older',chunks:packets.length,omitted:normalised.omitted};
    }catch(error){progress.reason=/^[a-z_]+$/.test(error.message)?error.message:'history_needs_attention';progress.retry_at=Date.now()+60000;this.active=null;
      // No cursor or body is fabricated after an unconfirmed ACK or UI failure.
      await state.saveCreds();return {state:progress.reason};}
  }
}
module.exports={absoluteTime,normaliseArchive,chunks,scrollOlder,acknowledge,MessengerHistory};
