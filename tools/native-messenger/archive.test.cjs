'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {absoluteTime,normaliseArchive,chunks,acknowledge,MessengerHistory,scrollOlder}=require('./archive.cjs');
const {readConversation}=require('./reader.cjs');
const {readPeer,discover,readRecentSentPreviews,readSearch,validateTargets}=require('./discovery.cjs');
const {selections}=require('./runtime.cjs');
const target={threadId:'12345',participantId:'98765',name:'Example Buyer',threadKind:'e2ee'};
const binding={key:'native-example',account_id:'facebook-web:123',user_id:'123',channel:'facebook',timeZone:'Europe/London'};
const observedAt='2026-11-09T19:00:00.000Z';
const row=(i,date='1 January 2019 10:00',body='Historic text')=>({id:`98765@msgr.${i}`,heading:i%2?'You sent':'Example',label:`At ${date}, ${i%2?'You':'Example'}: ${body}`});
const raw=(messages=[row(1)],extra={})=>({threadId:target.threadId,participantId:target.participantId,name:target.name,observedAt,messages,introductionVisible:false,at_oldest_scroll:false,...extra});

test('archive accepts 250 native messages across years and splits every message into bounded chronological chunks',()=>{
  const source=raw(Array.from({length:250},(_,i)=>row(i,`1 January ${2019+i%7} 10:00`)));
  const normal=normaliseArchive(source,target,binding),packets=chunks(source,target,binding,normal);
  assert.equal(normal.messages.length,250);assert.equal(normal.omitted,0);
  assert.deepEqual(packets.map(p=>p.thread.messages.length),[100,100,50]);
  assert.equal(new Set(packets.flatMap(p=>p.thread.messages.map(m=>m.id))).size,250);
  assert.equal(packets[0].thread.messages[0].occurred_at,'2019-01-01T10:00:00.000Z');
  assert.equal(packets.at(-1).thread.messages.at(-1).occurred_at,'2025-01-01T10:00:00.000Z');
  assert.ok(packets.every(p=>p.oldest_id===p.thread.messages[0].id&&p.newest_id===p.thread.messages.at(-1).id&&p.thread.has_more));
});
test('absolute dates support real UK and US displayed years, date headings and timezone conversion',()=>{
  for(const date of ['Monday, 1 July 2019 at 10:03','July 1, 2019 10:03 AM','01/07/2019 10:03'])
    assert.equal(absoluteTime(date,observedAt,binding.timeZone),'2019-07-01T09:03:00.000Z');
  assert.equal(absoluteTime('22:15',observedAt,binding.timeZone,'1 Sept 2019'),'2019-09-01T21:15:00.000Z');
  assert.equal(absoluteTime('Yesterday 10:00',observedAt,binding.timeZone),'2026-11-08T10:00:00.000Z');
  for(const date of ['1 January 2027 10:00','31 February 2019 10:00','25 October 2020 01:30','29 March 2020 01:30','1 July 10:03','10:03'])
    assert.throws(()=>absoluteTime(date,observedAt,binding.timeZone));
});
test('undated old text and unsupported rows stay explicitly omitted even at the visible beginning',()=>{
  const source=raw([row(1,'10:00'),{...row(2),label:null}],{introductionVisible:true,at_oldest_scroll:true});
  const normal=normaliseArchive(source,target,binding),packet=chunks(source,target,binding,normal)[0];
  assert.equal(normal.messages.length,0);assert.equal(normal.omitted,2);
  assert.equal(packet.reached_start,true);assert.equal(packet.thread.has_more,true);
  assert.equal(packet.evidence.omitted_rows,2);assert.equal(packet.oldest_id,null);assert.equal(packet.newest_id,null);
});
test('chunk byte bounds preserve unicode and chunk identity is stable across fresh observations',()=>{
  const source=raw(Array.from({length:12},(_,i)=>row(i,'1 July 2019 10:00','😀'.repeat(4000))));
  const normal=normaliseArchive(source,target,binding),packets=chunks(source,target,binding,normal,100000);
  assert.ok(packets.length>1);assert.equal(packets.flatMap(p=>p.thread.messages).length,12);
  assert.ok(packets.every(p=>Buffer.byteLength(JSON.stringify(p))<=100000));
  const fresh=chunks({...source,observedAt:'2026-11-09T20:00:00.000Z'},target,binding,normal,100000);
  assert.deepEqual(fresh.map(p=>p.chunk_id),packets.map(p=>p.chunk_id));
});
test('owner, participant, thread, duplicate and foreign native IDs are rejected before archive ingestion',()=>{
  assert.throws(()=>normaliseArchive(raw(),target,{...binding,user_id:'777'}),/wrong_account/);
  assert.throws(()=>normaliseArchive(raw(),{...target,participantId:'55'},binding),/wrong_thread/);
  assert.throws(()=>normaliseArchive(raw([row(1),row(1)]),target,binding),/invalid_message/);
  assert.throws(()=>normaliseArchive(raw([{...row(1),id:'555@msgr.1',label:null}]),target,binding),/invalid_message/);
});
test('quiet scrolling, restoring history and restore prompts never prove the beginning',()=>{
  for(const extra of [{at_oldest_scroll:true},{introductionVisible:true},{introductionVisible:true,at_oldest_scroll:true,restoring:true},{introductionVisible:true,at_oldest_scroll:true,restore_required:true}])
    assert.equal(normaliseArchive(raw([],extra),target,binding).boundary,false);
  assert.equal(normaliseArchive(raw([],{at_oldest_scroll:true,introductionVisible:true}),target,binding).boundary,true);
});
test('only the final chunk marks a genuine beginning while omitted indicators remain independent',()=>{
  const source=raw(Array.from({length:102},(_,i)=>row(i)),{introductionVisible:true,at_oldest_scroll:true});
  const packets=chunks(source,target,binding,normaliseArchive(source,target,binding));
  assert.deepEqual(packets.map(p=>p.reached_start),[false,true]);assert.equal(packets.at(-1).thread.has_more,false);
});
test('history ACK must bind the chunk, count and beginning before a checkpoint can advance',async()=>{
  const source=raw(),packet=chunks(source,target,binding,normaliseArchive(source,target,binding))[0];
  for(const override of [{ok:false},{chunk_id:'a'.repeat(64)},{stored:-1},{reached_start:true}])
    await assert.rejects(()=>acknowledge({request:async()=>({ok:true,chunk_id:packet.chunk_id,stored:1,reached_start:false,...override})},{},[packet]),/history_ack/);
  assert.equal(await acknowledge({request:async()=>({ok:true,chunk_id:packet.chunk_id,stored:1,reached_start:false})},{},[packet]),1);
});
function fixture(windows=[raw()]){
  let index=0,created=0,verified=0,saved=0,closed=false,scrolls=0;const calls=[];
  const page={isClosed:()=>closed,close:async()=>{closed=true;},goto:async()=>{},waitForFunction:async()=>{},waitForTimeout:async()=>{},
    evaluate:async fn=>{if(fn===readPeer)return {name:target.name,participantId:target.participantId};
      if(fn===readConversation)return windows[Math.min(index,windows.length-1)];
      assert.equal(fn,scrollOlder);scrolls++;index++;return {before:1,after:0};}};
  const history=new MessengerHistory({newPage:async()=>{created++;return page;}}),state={state:{creds:{}},saveCreds:async()=>{saved++;}};
  const adapter={verifyIdentity:async()=>{verified++;}},cloud={request:async(action,packet)=>{calls.push([action,packet]);return {ok:true,chunk_id:packet.chunk_id,stored:packet.thread.messages.length,reached_start:packet.reached_start};}};
  return {history,state,adapter,cloud,calls,stats:()=>({created,verified,saved,scrolls,closed})};
}
test('archive reads older pages on one separate reusable page and advances only acknowledged native anchors',async()=>{
  const f=fixture([raw([row(4)]),raw([row(3)]),raw([row(2)],{introductionVisible:true,at_oldest_scroll:true})]);
  for(let i=0;i<3;i++)await f.history.tick(f.adapter,f.cloud,f.state,binding,[target]);
  const p=f.history.progress(f.state,target,binding);assert.equal(p.chunks,3);assert.equal(p.reached_start,true);
  assert.deepEqual(p.anchor_ids,[row(2).id]);assert.equal(f.stats().created,1);assert.equal(f.stats().verified,3);
  assert.ok(f.calls.every(([action])=>action==='history'));assert.equal(f.stats().scrolls,2);
  assert.equal((await f.history.tick(f.adapter,f.cloud,f.state,binding,[target])).state,'idle');
});
test('lost ACK preserves old checkpoint; restarted archive seeks a real saved native identity',async()=>{
  const f=fixture();const p=f.history.progress(f.state,target,binding);p.anchor_ids=[row(99).id];
  f.cloud.request=async()=>({ok:true,chunk_id:'bad',stored:1,reached_start:false});
  assert.equal((await f.history.tick(f.adapter,f.cloud,f.state,binding,[target])).state,'history_ack_unconfirmed');
  assert.deepEqual(p.anchor_ids,[row(99).id]);assert.equal(p.chunks,0);assert.equal(f.stats().scrolls,0);
  p.retry_at=0;f.cloud.request=async(action,packet)=>({ok:true,chunk_id:packet.chunk_id,stored:1,reached_start:false});
  assert.equal((await f.history.tick(f.adapter,f.cloud,f.state,binding,[target])).state,'seeking_saved_cursor');
  assert.deepEqual(p.anchor_ids,[row(99).id]);assert.equal(f.stats().created,1);
});
test('owner change, pause and history restoration prevent provider ingestion',async()=>{
  const f=fixture();f.adapter.verifyIdentity=async()=>{throw Error('account_changed');};
  assert.equal((await f.history.tick(f.adapter,f.cloud,f.state,binding,[target])).state,'account_changed');assert.equal(f.calls.length,0);assert.equal(f.stats().created,0);
  assert.equal((await f.history.tick(f.adapter,f.cloud,f.state,binding,[target],()=>true)).state,'paused');
  const restoring=fixture([raw([],{restore_required:true})]);
  assert.equal((await restoring.history.tick(restoring.adapter,restoring.cloud,restoring.state,binding,[target])).state,'restore_required');assert.equal(restoring.calls.length,0);
});
test('stable but unproven older boundary remains incomplete and retries instead of silently completing',async()=>{
  const f=fixture();let result;for(let i=0;i<4;i++)result=await f.history.tick(f.adapter,f.cloud,f.state,binding,[target]);
  assert.equal(result.state,'older_boundary_unconfirmed');assert.equal(f.history.progress(f.state,target,binding).reached_start,false);
});
test('selections fetch all pages beyond 2,000 with exact participant identity and no total cap',async()=>{
  const all=Array.from({length:2501},(_,i)=>({...target,threadId:String(10000+i)}));let requests=0;
  const result=await selections({request:async(action,p)=>{assert.equal(action,'selections');assert.equal(p.limit,250);requests++;const offset=+(p.cursor||0);return {threads:all.slice(offset,offset+250),next_cursor:offset+250<all.length?String(offset+250):null,sync_all:true};}},{});
  assert.equal(result.threads.length,2501);assert.equal(requests,11);assert.equal(result.sync_all,true);
  assert.equal(validateTargets(all).length,2501);
  assert.equal((await selections({request:async()=>({threads:[target]})},{})).threads.length,1);
  await assert.rejects(()=>selections({request:async()=>({threads:[target],next_cursor:'repeat'})},{}),/invalid_selection_cursor/);
  let n=0;await assert.rejects(()=>selections({request:async()=>++n===1?{threads:[target],next_cursor:'next'}:{threads:[{...target,participantId:'55'}]}},{}),/selection_identity_changed/);
});
test('phrase discovery verifies and streams all 251 rendered results with final request_done boundary',async()=>{
  const rows=Array.from({length:251},(_,i)=>({threadId:String(1000+i),name:'Provisional',url:'https://www.facebook.com/messages/e2ee/t/'+(1000+i),encrypted:true}));
  let pages=0,peerId='',closed=0;const batches=[];
  const links={first:()=>({waitFor:async()=>{}}),count:async()=>251,last:()=>({scrollIntoViewIfNeeded:async()=>{}}),filter(){return this;}};
  const search={waitFor:async()=>{},fill:async()=>{},click:async()=>{},inputValue:async()=>'hello'};
  const page={goto:async()=>{},close:async()=>{closed++;},getByRole:()=>search,locator:()=>links,waitForTimeout:async()=>{},evaluate:async fn=>fn===readRecentSentPreviews?[]:fn===readSearch?{rows,loading:false,restoring:false}:null};
  const peer={goto:async url=>{peerId=url.split('/').at(-1);},close:async()=>{closed++;},waitForFunction:async()=>{},evaluate:async()=>({name:'Verified '+peerId,participantId:'9'+peerId})};
  const result=await discover({newPage:async()=>++pages===1?page:peer},'hello',async(packet,done)=>batches.push({packet,done}));
  assert.equal(result.streamed,true);assert.deepEqual(batches.map(b=>b.packet.candidates.length),[100,100,51]);
  assert.deepEqual(batches.map(b=>b.done),[false,false,true]);assert.ok(batches.every(b=>b.packet.complete===false));
  assert.equal(closed,2);assert.equal(pages,2);assert.ok(batches.flatMap(b=>b.packet.candidates).every(c=>c.participantId&&!('url' in c)&&!('body' in c)));
});
test('rendered archive date context follows actual heading order and clears unsupported later headings',()=>{
  const vm=require('node:vm');
  const heading=text=>({innerText:text,textContent:text,hasAttribute:()=>false,closest:()=>null,querySelector:()=>null});
  const message=i=>({hasAttribute:key=>key==='data-message-id',getAttribute:key=>key==='data-message-id'?row(i).id:row(i,'10:00').label,querySelector:()=>({textContent:'Example'})});
  const a=message(2),b=message(4),c=message(6);
  const log={scrollHeight:400,clientHeight:100,scrollTop:0,parentElement:null,getAttribute:()=>`Messages in conversation with ${target.name}`,
    querySelector:()=>null,querySelectorAll:selector=>selector==='[data-message-id]'?[a,b,c]:selector==='h3'?[]:[heading('1 January 2019'),a,heading('2 January'),b,heading('03/01/2019'),c]};
  const document={querySelectorAll:selector=>selector==='[role="log"]'?[log]:selector==='a[href]'?[{textContent:target.name,href:'https://www.facebook.com/'+target.participantId}]:[]};
  const result=vm.runInNewContext('('+readConversation.toString()+')('+JSON.stringify({...target,archive:true})+')',
    {URL,location:{href:'https://www.facebook.com/messages/e2ee/t/'+target.threadId},document,Date,getComputedStyle:()=>({flexDirection:'column'})});
  assert.deepEqual(Array.from(result.messages,m=>m.context_date),['1 January 2019','','03/01/2019']);
  assert.equal(result.at_oldest_scroll,true);assert.equal(result.introductionVisible,false);
});
