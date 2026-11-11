const {test}=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {EventEmitter}=require('node:events');
const {SocketClient,incomingMessage,groupPacket}=require('./socket-client.cjs');
const {createCatchup,openCatchups,spoolOrder,setAside,progressFile}=require('./catchup.cjs');

// Synthetic fixtures only: no WhatsApp connection, no Growth HQ request, no real number.
const GROUP='123456789@g.us', OTHER='555000111@g.us', ID='0f8fad5b-d9cb-469f-a165-70867728950e';
const T0=1800000000, hour=3600, plain={normalizeMessageContent:x=>x};
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const rule={id:'rule-1',enabled:true,groupIds:[GROUP],keywords:['Land'],matchMode:'exact'};
// As the phone's history copy carries a group message: seconds as a Long, the sender sometimes beside the key.
const long=n=>({toNumber:()=>n,toString:()=>String(n)});
const past=(n,text,{sender='4477009'+String(10000+n),beside=false,fromMe=false,group=GROUP,name}={})=>({
  key:{remoteJid:group,fromMe,id:'H'+n,...(!fromMe&&!beside?{participant:sender+'@s.whatsapp.net'}:{})},
  ...(beside?{participant:sender+'@s.whatsapp.net'}:{}),messageTimestamp:long(T0-n*60),message:{conversation:text},...(name?{pushName:name}:{})});
const snapshotOf=(changes={})=>({revision:3,listenerId:'personal-7510',rules:[rule],catchups:[{id:ID,ruleId:rule.id,
  since:new Date((T0-48*hour)*1000).toISOString(),until:new Date(T0*1000).toISOString(),
  expiresAt:new Date((T0+2*hour)*1000).toISOString(),groupIds:[GROUP],pendingGroupIds:[GROUP],...changes}]});

async function bench(t,{phone=[],snapshot=snapshotOf(),answer=true,pairOnly=false,limits={},openJournal,phoneOf,trace=false}={}) {
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ghq-catchup-fixture-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const spool=path.join(root,'events');await fs.mkdir(spool);
  const state={snapshot,clock:T0*1000,requests:[],notes:[],answer,phone};
  let catchup;
  // The stand-in phone: asked for what came before a stored message, it answers out of band with at most 50 older ones.
  const client={ready:true,closed:false,pairOnly,learned:[],remember:(lid,pn)=>client.learned.push([lid,pn]),
    fetchGroupHistory:async(groupId,before,options={})=>{
      const requestId='REQ'+(state.requests.push({groupId,before,seconds:Boolean(options.seconds)})),reply=state.answer;
      if(reply)setImmediate(()=>{
        const older=state.phone.filter(m=>m.key.remoteJid===groupId&&m.messageTimestamp.toNumber()<before.timestamp)
          .sort((a,b)=>b.messageTimestamp.toNumber()-a.messageTimestamp.toNumber()).slice(0,50);
        const bundle=typeof reply==='function'?reply(older,requestId,Boolean(options.seconds)):{messages:older,chats:[],sessionId:null};
        if(bundle)state.receiver.accept(bundle,plain);
      });
      return requestId;
    },sendMessage:()=>assert.fail('A catch-up never sends: Growth HQ releases each reply')};
  catchup=createCatchup({client,root,spool,pairOnly,current:()=>state.snapshot,now:()=>state.clock,sleep:async()=>{},
    note:message=>state.notes.push(message),openJournal,phoneOf:phoneOf??(async id=>/^[1-9][0-9]{6,14}@c\.us$/.test(id)?id.split('@')[0]:null),
    limits:{pageTimeout:20,...limits},trace});
  state.receiver=catchup;
  const outbox=async()=>Object.fromEntries(await Promise.all((await fs.readdir(spool)).sort().map(async name=>[name,JSON.parse(await fs.readFile(path.join(spool,name),'utf8'))])));
  const submitted=async()=>Object.values(await outbox()).filter(e=>e.action==='catchup-events').map(e=>e.value);
  const report=async()=>(await outbox())[progressFile(ID,GROUP)]?.value;
  return {catchup,client,state,spool,root,outbox,submitted,report};
}

test('a group message reads the same live and from the phone\'s history, and keeps the live event ID',()=>{
  const live={key:{id:'M1',remoteJid:GROUP,participant:'987654321@lid',participantAlt:'447700900002@s.whatsapp.net',fromMe:false},
    messageTimestamp:Math.floor(Date.now()/1000),message:{conversation:'Land'},pushName:'Test person'};
  const seen=groupPacket(live,plain);
  assert.deepEqual({...seen,eventId:undefined},{groupId:GROUP,id:'M1',fromMe:false,timestamp:live.messageTimestamp,author:'987654321@lid',
    alt:'447700900002@c.us',body:'Land',name:'Test person',eventId:undefined});
  assert.equal(seen.eventId,incomingMessage(live,Date.now(),plain).id._serialized);
  // The history copy: no freshness rule, a Long timestamp, the sender beside the key.
  const old=groupPacket(past(90,'land!',{beside:true}),plain);
  assert.equal(old.timestamp,T0-90*60);assert.equal(old.author,'447700910090@c.us');assert.equal(old.body,'land!');
  assert.equal(old.eventId,groupPacket(past(90,'land!'),plain).eventId);
  const mine=groupPacket(past(1,'sent from my phone',{fromMe:true}),plain);
  assert.equal(mine.fromMe,true);assert.equal(mine.author,null);
  assert.equal(groupPacket({...past(2,''),message:{imageMessage:{caption:'x'}}},plain).body,null);
  // Nothing the phone does not keep as a chat row can anchor a request, and nothing outside a group is read at all.
  assert.equal(groupPacket({...past(3,''),message:{protocolMessage:{type:0}}},plain),null);
  assert.equal(groupPacket({...past(3,''),message:{reactionMessage:{text:'x'}}},plain),null);
  assert.equal(groupPacket({...past(4,'Land'),key:{id:'P',remoteJid:'447700900002@s.whatsapp.net',fromMe:false}},plain),null);
  assert.equal(groupPacket({...past(5,'Land'),messageTimestamp:undefined},plain),null);
  assert.equal(groupPacket({...past(6,'x'.repeat(4097))},plain).body,null);
});

test('the pinned Baileys decodes an on-demand history bundle into the shape the catch-up reads',async()=>{
  const p=await import('@whiskeysockets/baileys'), type=p.proto.HistorySync.HistorySyncType;
  assert.equal(type.ON_DEMAND,6);
  const sync=p.proto.HistorySync.decode(p.proto.HistorySync.encode({syncType:type.ON_DEMAND,conversations:[{id:GROUP,endOfHistoryTransferType:1,messages:[
    {message:{key:{remoteJid:GROUP,fromMe:false,id:'A1',participant:'987654321@lid'},messageTimestamp:T0-60,message:{conversation:'Land'},pushName:'Test person'}},
    {message:{key:{remoteJid:GROUP,fromMe:false,id:'A2'},participant:'447700900002@s.whatsapp.net',messageTimestamp:T0-120,message:{extendedTextMessage:{text:'land please'}}}}]}],
    phoneNumberToLidMappings:[{pnJid:'447700900002@s.whatsapp.net',lidJid:'987654321@lid'}]}).finish());
  const data=p.processHistoryMessage(sync);
  assert.equal(data.syncType,type.ON_DEMAND);assert.equal(data.chats[0].id,GROUP);assert.equal(data.chats[0].endOfHistoryTransferType,1);
  assert.deepEqual(data.lidPnMappings,[{lid:'987654321@lid',pn:'447700900002@s.whatsapp.net'}]);
  const [first,second]=data.messages.map(m=>groupPacket(m,p));
  assert.deepEqual([first.id,first.timestamp,first.author,first.body,first.name],['A1',T0-60,'987654321@lid','Land','Test person']);
  assert.deepEqual([second.id,second.timestamp,second.author,second.body],['A2',T0-120,'447700900002@c.us','land please']);
  const notice={protocolMessage:{historySyncNotification:{syncType:type.ON_DEMAND,peerDataRequestSessionId:'REQ1'}}};
  assert.equal(p.getHistoryMsg(notice).peerDataRequestSessionId,'REQ1');
  assert.equal(typeof p.downloadAndProcessHistorySyncNotification,'function');
});

function socketFixture() {
  const sockets=[],calls=[],downloads=[],auth={state:{creds:{},keys:{}},saveCreds:async()=>{},flush:async()=>{}};
  const provider={normalizeMessageContent:x=>x,Browsers:{ubuntu:()=>['Ubuntu','Chrome','22'],windows:()=>['Windows','Desktop','10']},
    DisconnectReason:{restartRequired:515,loggedOut:401,badSession:500},proto:{HistorySync:{HistorySyncType:{RECENT:2,ON_DEMAND:6}}},
    getHistoryMsg:message=>message?.protocolMessage?.historySyncNotification,
    downloadAndProcessHistorySyncNotification:async notice=>{downloads.push(notice);return notice.fixture;},
    default:options=>{const socket={options,ev:new EventEmitter(),user:{id:'447700900001:2@s.whatsapp.net'},end:()=>{},
      signalRepository:{lidMapping:{getPNForLID:async()=>null}},
      fetchMessageHistory:async(...args)=>{calls.push(args);return 'REQ1';}};sockets.push(socket);return socket;}};
  const client=new SocketClient({dataPath:'unused',expectedNumber:'447700900001',loadProvider:async()=>provider,authFactory:async()=>auth,loggerFactory:()=>({})});
  return {client,sockets,calls,downloads};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const notice=(fixture,changes={})=>({key:{id:'N1',remoteJid:'447700900001@s.whatsapp.net',fromMe:true,...changes.key},messageTimestamp:T0,
  message:{protocolMessage:{historySyncNotification:{syncType:6,peerDataRequestSessionId:'REQ1',fixture,...changes.notification}}}});

test('the socket reports each group\'s newest stored message, asks the phone for one bounded page, and reads only its own phone\'s answer while a catch-up waits',async()=>{
  const f=socketFixture(), seen=[], history=[], live=[];
  f.client.on('group_seen',m=>seen.push(m));f.client.on('group_history',b=>history.push(b));f.client.on('message',m=>live.push(m));
  await f.client.initialize();const socket=f.sockets[0];
  await assert.rejects(f.client.fetchGroupHistory(GROUP,{id:'H1',fromMe:false,timestamp:T0}),/not linked/);
  socket.ev.emit('messages.upsert',{type:'notify',messages:[past(1,'Land')]});assert.equal(seen.length,0);
  await f.client.connectionUpdate(socket,{connection:'open'});
  // Offline-delivered and the owner's own phone messages still move the anchor; neither is answered live.
  socket.ev.emit('messages.upsert',{type:'append',messages:[past(2,'Land')]});
  socket.ev.emit('messages.upsert',{type:'notify',messages:[past(1,'posted from my phone',{fromMe:true})]});
  socket.ev.emit('messaging-history.set',{chats:[],messages:[past(9,'older',{group:OTHER})]});
  assert.deepEqual(seen.map(m=>[m.groupId,m.id,m.fromMe]),[[GROUP,'H2',false],[GROUP,'H1',true],[OTHER,'H9',false]]);
  assert.equal(live.length,0);
  assert.equal(await f.client.fetchGroupHistory(GROUP,{id:'H2',fromMe:false,timestamp:T0-120}),'REQ1');
  assert.deepEqual(f.calls,[[50,{remoteJid:GROUP,fromMe:false,id:'H2'},(T0-120)*1000]]);
  await assert.rejects(f.client.fetchGroupHistory('447700900002@c.us',{id:'H2',fromMe:false,timestamp:T0}),/Group message required/);
  await assert.rejects(f.client.fetchGroupHistory(GROUP,{id:'H2'}),/Group message required/);
  const bundle={messages:[past(30,'Land',{sender:'987654321',beside:true})],chats:[{id:GROUP}],lidPnMappings:[{lid:'555666777@lid',pn:'447700900003@s.whatsapp.net'}]};
  // No catch-up is waiting: the phone's notice is not even downloaded.
  socket.ev.emit('messages.upsert',{type:'notify',messages:[notice(bundle)]});await settle();
  assert.equal(f.downloads.length,0);assert.equal(history.length,0);
  f.client.wantGroupHistory=()=>true;
  socket.ev.emit('messages.upsert',{type:'notify',messages:[notice(bundle,{key:{fromMe:false,remoteJid:'447700900009@s.whatsapp.net'}})]});   // not this account's phone
  socket.ev.emit('messages.upsert',{type:'notify',messages:[notice(bundle,{notification:{syncType:2}})]});            // not an on-demand answer
  await settle();assert.equal(f.downloads.length,0);
  socket.ev.emit('messages.upsert',{type:'append',messages:[notice(bundle)]});await settle();
  assert.equal(f.downloads.length,1);assert.equal(history.length,1);
  assert.deepEqual([history[0].messages.length,history[0].chats[0].id,history[0].sessionId],[1,GROUP,'REQ1']);
  assert.deepEqual(await f.client.getContactLidAndPhone(['555666777@lid']),[{pn:'447700900003@c.us'}]);
  assert.equal(live.length,0,'earlier messages never enter the live path');
  await f.client.destroy();
});

test('pair-only mode reports no group message, reads no history notice and refuses to ask the phone',async()=>{
  const f=socketFixture(), seen=[], history=[];f.client.pairOnly=true;f.client.wantGroupHistory=()=>true;
  f.client.on('group_seen',m=>seen.push(m));f.client.on('group_history',b=>history.push(b));
  await f.client.initialize();await f.client.connectionUpdate(f.sockets[0],{connection:'open'});
  f.sockets[0].ev.emit('messages.upsert',{type:'notify',messages:[past(1,'Land'),notice({messages:[past(2,'Land')],chats:[]})]});
  f.sockets[0].ev.emit('messaging-history.set',{chats:[],messages:[past(3,'Land')]});await settle();
  assert.equal(seen.length,0);assert.equal(history.length,0);assert.equal(f.downloads.length,0);
  await assert.rejects(f.client.fetchGroupHistory(GROUP,{id:'H1',fromMe:false,timestamp:T0}),/Pairing only/);
  assert.equal(f.calls.length,0);await f.client.destroy();
});

test('without an open catch-up from Growth HQ nothing is asked of the phone and nothing is submitted',async t=>{
  const b=await bench(t,{phone:[past(10,'Land')],snapshot:{revision:3,listenerId:'personal-7510',rules:[rule]}});   // an older server: no catchups at all
  b.catchup.seen(groupPacket(past(1,'Land'),plain));
  await b.catchup.tick();
  b.state.snapshot={...snapshotOf(),catchups:[]};await b.catchup.tick();
  b.state.snapshot=snapshotOf({expiresAt:new Date((T0-1)*1000).toISOString()});await b.catchup.tick();   // already run out by this clock
  b.state.snapshot={...snapshotOf(),rules:[{...rule,enabled:false}]};await b.catchup.tick();              // its rule is paused
  b.state.snapshot=snapshotOf({id:'not-a-catchup-id'});await b.catchup.tick();
  assert.equal(b.state.requests.length,0);assert.deepEqual(await b.outbox(),{});assert.equal(b.catchup.wanted(),false);
  assert.deepEqual(openCatchups({catchups:'none'},0),[]);assert.equal(openCatchups(snapshotOf(),T0*1000).length,1);
});

test('an open catch-up pages back from the anchor to the window\'s start and submits only matching requests in the window',async t=>{
  const live=past(5,'chat',{sender:'447700900300'});                       // seen live while the rule was on: already in the outbox
  const phone=[live,past(20,'Land',{name:'Fixture Asker'}),past(30,'no keyword here'),past(40,'LAND!',{beside:true}),
    past(50,'Land',{fromMe:true}),past(60,'land',{sender:'987654321',beside:false}),past(70,'Land',{group:OTHER}),
    ...Array.from({length:120},(_,i)=>past(100+i,'filler')),past(47*60,'land'),past(49*60,'Land')];   // the last is before the window
  phone.find(m=>m.key.id==='H60').key.participant='987654321@lid';      // a sender WhatsApp shows only as a hidden ID
  const b=await bench(t,{phone});
  const anchor=groupPacket(past(1,'Land',{sender:'447700900200',name:'Anchor Person'}),plain);
  await fs.writeFile(path.join(b.spool,sha(groupPacket(past(20,'Land'),plain).eventId)+'.json'),JSON.stringify({action:'events',value:{kept:'live'}}));
  b.catchup.seen(groupPacket(past(3,'earlier',{sender:'447700900200'}),plain));b.catchup.seen(anchor);b.catchup.seen(groupPacket(past(2,'older than the anchor'),plain));
  b.catchup.seen({...groupPacket(past(400,'x'),plain),author:'447700910040@c.us',name:'Known Name'});   // a name learned live fills a history gap
  await b.catchup.tick();
  assert.deepEqual(b.state.requests.map(r=>[r.groupId,r.before.id]),[[GROUP,'H1'],[GROUP,'H143'],[GROUP,'H193']]);
  assert.deepEqual(b.state.requests[0].before,{id:'H1',fromMe:false,timestamp:T0-60});
  const events=await b.submitted();
  // The anchor itself (never returned by "before the anchor"), H40 and the oldest in-window match; H20 stays the live event.
  assert.deepEqual(events.map(e=>e.text).sort(),['LAND!','Land','land']);
  for(const e of events){assert.equal(e.catchupId,ID);assert.equal(e.groupId,GROUP);assert.equal(e.listenerId,'personal-7510');assert.match(e.senderPhone,/^[1-9][0-9]{6,14}$/);}
  const first=events.find(e=>e.text==='Land');
  assert.deepEqual([first.eventId,first.senderId,first.senderPhone,first.senderName,first.observedAt],
    [anchor.eventId,'447700900200@c.us','447700900200','Anchor Person',new Date((T0-60)*1000).toISOString()]);
  assert.equal(events.find(e=>e.text==='LAND!').senderName,'Known Name');
  const outbox=await b.outbox();
  assert.deepEqual(outbox[sha(groupPacket(past(20,'Land'),plain).eventId)+'.json'],{action:'events',value:{kept:'live'}});
  assert.equal(Object.keys(outbox).every(name=>/^[a-f0-9]{64}\.json$/.test(name)),true);
  // 128 stored messages read plus the anchor; the anchor, H20, H40, H60 and H2820 matched in the window; H60 has no phone number.
  assert.deepEqual(await b.report(),{listenerId:'personal-7510',groupId:GROUP,status:'scanned',reachedSince:true,scanned:129,matched:5,unresolved:1});
  assert.equal(outbox[progressFile(ID,GROUP)].action,'catchups/'+ID+'/progress');
  assert.equal(b.state.notes.join(' ').match(/Land|4477|Anchor|Fixture/),null,'no text, name or number in the log');
  await b.catchup.tick();assert.equal(b.state.requests.length,3,'a finished group is not read again');
  assert.equal(b.client.learned.length,5,'each matching sender is offered to the phone-number mapping');
});

test('a second catch-up for the same message replaces the first one\'s unsent copy; a restart does not re-read a reported group',async t=>{
  const b=await bench(t,{phone:[past(10,'Land'),past(49*60,'before the window')]});
  b.catchup.seen(groupPacket(past(1,'hello'),plain));
  const file=path.join(b.spool,sha(groupPacket(past(10,'Land'),plain).eventId)+'.json');
  await fs.writeFile(file,JSON.stringify({action:'catchup-events',value:{catchupId:'an-earlier-catchup'}}));
  await b.catchup.tick();
  assert.equal(JSON.parse(await fs.readFile(file,'utf8')).value.catchupId,ID);
  assert.equal((await fs.stat(file)).mode&0o777,0o600);
  const again=createCatchup({client:b.client,root:b.root,spool:b.spool,current:()=>b.state.snapshot,now:()=>b.state.clock,sleep:async()=>{},phoneOf:async()=>null});
  again.seen(groupPacket(past(1,'hello'),plain));b.state.receiver=again;
  assert.equal((await b.report()).reachedSince,true);
  await again.tick();assert.equal(b.state.requests.length,1);
});

test('the phone saying nothing earlier remains ends the group; a late copy of an old answer does not',async t=>{
  let calls=0;
  const answer=(older,requestId)=>{
    calls++;
    if(calls===1)return {messages:older.slice(0,1),chats:[],sessionId:null};
    if(calls===2){setImmediate(()=>b.catchup.accept({messages:older.slice(0,1),chats:[{id:GROUP,endOfHistoryTransferType:1}],sessionId:requestId},plain));
      return {messages:b.state.phone.slice(0,1),chats:[{id:GROUP,endOfHistoryTransferType:2}],sessionId:'another-request'};}   // the first answer again
    return null;
  };
  const b=await bench(t,{phone:[past(10,'Land'),past(20,'land')],answer});
  b.catchup.seen(groupPacket(past(1,'hello'),plain));
  await b.catchup.tick();
  assert.equal(b.state.requests.length,2);assert.equal((await b.submitted()).length,2);
  assert.deepEqual(await b.report(),{listenerId:'personal-7510',groupId:GROUP,status:'scanned',reachedSince:false,scanned:3,matched:2,unresolved:0});
  // An empty answer counts only when the phone ties it to this very request.
  const c=await bench(t,{phone:[],answer:(_older,requestId)=>({messages:[],chats:[],sessionId:requestId})});
  c.catchup.seen(groupPacket(past(1,'Land'),plain));await c.catchup.tick();
  // A very first answer that is empty is asked for once more in the other time form before it is believed.
  assert.deepEqual(c.state.requests.map(r=>r.seconds),[false,true]);assert.equal((await c.report()).status,'scanned');assert.equal((await c.submitted()).length,1);
});

test('a group never seen is retried until the catch-up is about to expire, then reported unavailable',async t=>{
  const b=await bench(t,{phone:[past(10,'Land')]});
  await b.catchup.tick();await b.catchup.tick();
  assert.equal(b.state.requests.length,0);assert.equal(await b.report(),undefined);
  b.state.clock=(T0+2*hour)*1000-60000;await b.catchup.tick();
  assert.equal(b.state.requests.length,0,'no anchor, so the phone cannot be asked');
  assert.deepEqual(await b.report(),{listenerId:'personal-7510',groupId:GROUP,status:'unavailable',reachedSince:false,scanned:0,matched:0,unresolved:0});
});

test('a silent phone is asked again later a bounded number of times and never blocks; a cancelled catch-up stops the reading',async t=>{
  const b=await bench(t,{phone:[past(10,'Land')],answer:false,limits:{retryFirst:1000,retryMost:4000}});
  b.catchup.seen(groupPacket(past(1,'hello'),plain));
  await b.catchup.tick();assert.equal(b.state.requests.length,1);
  await b.catchup.tick();assert.equal(b.state.requests.length,1,'not before its retry time');
  b.state.clock+=1000;await b.catchup.tick();assert.equal(b.state.requests.length,2);
  b.state.clock+=1999;await b.catchup.tick();assert.equal(b.state.requests.length,2,'the wait doubles');
  b.state.clock+=1;await b.catchup.tick();assert.equal(b.state.requests.length,3);
  assert.equal(await b.report(),undefined);assert.equal(b.catchup.wanted(),false);
  b.state.clock=(T0+2*hour)*1000-60000;await b.catchup.tick();
  assert.equal(b.state.requests.length,4);assert.equal((await b.report()).status,'unavailable');
  // Pages arrived, then silence: three unanswered requests in a row end the group as partly read.
  const c=await bench(t,{phone:[past(10,'Land'),past(20,'x')],limits:{retry:500,pause:0}});
  c.catchup.seen(groupPacket(past(1,'hello'),plain));
  c.state.answer=(older)=>c.state.requests.length===1?{messages:older.slice(0,1),chats:[],sessionId:null}:null;
  await c.catchup.tick();assert.equal(c.state.requests.length,2);
  c.state.clock+=500;await c.catchup.tick();c.state.clock+=500;await c.catchup.tick();
  assert.equal(c.state.requests.length,4);
  assert.deepEqual(await c.report(),{listenerId:'personal-7510',groupId:GROUP,status:'scanned',reachedSince:false,scanned:2,matched:1,unresolved:0});
  // The owner cancels (or edits a rule) while pages are still coming: the next page is never requested.
  const d=await bench(t,{phone:Array.from({length:120},(_,i)=>past(10+i,'Land'))});
  d.catchup.seen(groupPacket(past(1,'hello'),plain));
  d.state.answer=(older)=>{d.state.snapshot={...d.state.snapshot,catchups:[]};return {messages:older,chats:[],sessionId:null};};
  await d.catchup.tick();
  assert.equal(d.state.requests.length,1);assert.equal(await d.report(),undefined);
});

test('pair-only refuses anchors, reading and submitting; a failing group leaves the others and the caller untouched',async t=>{
  const b=await bench(t,{phone:[past(10,'Land')],pairOnly:true});
  b.catchup.seen(groupPacket(past(1,'Land'),plain));await b.catchup.flush();await b.catchup.tick();
  assert.equal(b.state.requests.length,0);assert.deepEqual(await b.outbox(),{});
  assert.deepEqual(await fs.readdir(b.root),['events'],'nothing is kept in pair-only mode');
  const snapshot=snapshotOf({groupIds:[OTHER,GROUP],pendingGroupIds:[OTHER,GROUP]});
  snapshot.rules=[{...rule,groupIds:[OTHER,GROUP]}];
  const c=await bench(t,{phone:[past(10,'Land'),past(49*60,'before the window')],snapshot,limits:{pause:0}});
  const fetch=c.client.fetchGroupHistory;
  c.client.fetchGroupHistory=async(groupId,before)=>{if(groupId===OTHER)throw Error('fixture link dropped');return fetch(groupId,before);};
  c.catchup.seen(groupPacket(past(1,'x',{group:OTHER}),plain));c.catchup.seen(groupPacket(past(1,'hello'),plain));
  await c.catchup.tick();
  assert.equal((await c.submitted()).length,1);assert.equal((await c.report()).status,'scanned');
  assert.equal((await c.outbox())[progressFile(ID,OTHER)],undefined);assert.equal(c.catchup.wanted(),false);
});

test('anchors survive a restart in one sealed private file that holds only each group\'s newest message',async t=>{
  const b=await bench(t,{phone:[past(10,'Land')],snapshot:{...snapshotOf(),catchups:[]}});
  b.catchup.seen(groupPacket(past(9,'an earlier synthetic body'),plain));
  b.catchup.seen(groupPacket(past(1,'Land',{sender:'447700900200',name:'Synthetic Sender'}),plain));
  b.catchup.seen(groupPacket(past(4,'other group synthetic body',{group:OTHER}),plain));
  await b.catchup.flush();
  const dir=path.join(b.root,'catchup'), sealed=await fs.readFile(path.join(dir,'auth.sealed.json'),'utf8');
  for(const secret of ['Land','synthetic','447700900200','Synthetic Sender',GROUP])assert(!sealed.includes(secret));
  assert.equal((await fs.stat(path.join(dir,'auth.sealed.json'))).mode&0o777,0o600);
  assert.deepEqual((await fs.readdir(dir)).sort(),['auth.sealed.json','storage.key']);
  const {openAuthState}=require('./socket-auth.cjs');
  const kept=(await openAuthState(dir,{BufferJSON:{},initAuthCreds:()=>({})})).state.creds.anchors;
  assert.deepEqual(Object.keys(kept).sort(),[GROUP,OTHER].sort());assert.equal(kept[GROUP].id,'H1');assert.equal(kept[GROUP].body,'Land');
  assert(!JSON.stringify(kept).includes('an earlier synthetic body'));
  // After a restart the kept anchor is enough to read back, and its own text is still answered.
  const again=createCatchup({client:b.client,root:b.root,spool:b.spool,current:()=>snapshotOf(),now:()=>b.state.clock,sleep:async()=>{},
    phoneOf:async id=>id.split('@')[0],limits:{pageTimeout:20}});
  b.state.receiver=again;await again.tick();
  assert.deepEqual(b.state.requests[0],{groupId:GROUP,before:{id:'H1',fromMe:false,timestamp:T0-60},seconds:false});
  assert.deepEqual((await b.submitted()).map(e=>[e.text,e.senderName]).sort(),[['Land',''],['Land','Synthetic Sender']]);
  // Only the newest groups are kept.
  const many=await bench(t,{limits:{anchors:2}});
  for(const n of [1,2,3])many.catchup.seen(groupPacket(past(n,'x',{group:'90000000'+n+'@g.us'}),plain));
  await many.catchup.flush();
  const few=(await openAuthState(path.join(many.root,'catchup'),{BufferJSON:{},initAuthCreds:()=>({})})).state.creds.anchors;
  assert.deepEqual(Object.keys(few).sort(),['900000001@g.us','900000002@g.us']);
});

test('the outbox posts controls first, then requests, then each catch-up group\'s report; an older server cannot block it',()=>{
  const actions=['catchups/'+ID+'/progress','events','opt-outs','catchup-events'];
  assert.deepEqual(actions.sort((a,b)=>spoolOrder(a)-spoolOrder(b)),['opt-outs','events','catchup-events','catchups/'+ID+'/progress']);
  for(const status of [400,409,413])assert.equal(setAside(status,'events'),true);
  // A server from before catch-up has neither action: its 404 sets them aside, while any other failure is retried.
  assert.equal(setAside(404,'catchup-events'),true);assert.equal(setAside(404,'catchups/'+ID+'/progress'),true);
  assert.equal(setAside(404,'events'),false);assert.equal(setAside(503,'catchup-events'),false);assert.equal(setAside(undefined,'catchup-events'),false);
});

test('a first answer that comes back empty is asked for once more with the time in seconds, and the trace shows only shapes',async t=>{
  // A phone that honours the request only when the stored message's time is in seconds: the milliseconds form gets an
  // empty "nothing earlier" answer, as the first live run did.
  const phone=[past(20,'Land',{name:'Fixture Asker'}),past(30,'chat'),past(49*60,'old')];
  const b=await bench(t,{phone,trace:true,answer:(older,requestId,seconds)=>seconds
    ?{messages:older,chats:[],sessionId:null}:{messages:[],chats:[{id:GROUP,endOfHistoryTransferType:1}],sessionId:requestId}});
  b.catchup.seen(groupPacket(past(1,'hello',{sender:'447700900200'}),plain));
  await b.catchup.tick();
  assert.deepEqual(b.state.requests.slice(0,2).map(r=>r.seconds),[false,true]);
  assert.ok(b.state.requests.slice(1).every(r=>r.seconds),'once the seconds form is in use it stays in use');
  assert.deepEqual((await b.submitted()).map(v=>v.senderName),['Fixture Asker']);
  const report=await b.report();
  assert.equal(report.status,'scanned');assert.equal(report.reachedSince,true);assert.equal(report.matched,1);
  const traced=b.state.notes.filter(n=>n.startsWith('Catch-up trace: '));
  assert.ok(traced.some(n=>/asked page=0 unit=milliseconds/.test(n))&&traced.some(n=>/unit=seconds/.test(n)));
  assert.ok(traced.some(n=>/answer chats=1 messages=0 forThisGroup=0 otherChats=0 groupListed=true end=1 session=match/.test(n)));
  assert.ok(traced.some(n=>/first answer empty; asking again/.test(n)));
  // Shapes only: no message text, sender name, phone number, group ID or message ID ever reaches the log.
  for(const line of b.state.notes)assert.doesNotMatch(line,/Land|Fixture|4477009|123456789|H20|REQ/);
});

test('a phone that has nothing earlier in either form ends the group after the second empty answer, and without the trace nothing extra is logged',async t=>{
  const b=await bench(t,{phone:[],answer:(older,requestId)=>({messages:[],chats:[],sessionId:requestId})});
  b.catchup.seen(groupPacket(past(1,'hello',{sender:'447700900200'}),plain));
  await b.catchup.tick();
  assert.deepEqual(b.state.requests.map(r=>r.seconds),[false,true]);
  const report=await b.report();
  assert.equal(report.status,'scanned');assert.equal(report.reachedSince,false);assert.equal(report.scanned,1);
  assert.ok(b.state.notes.every(n=>!n.startsWith('Catch-up trace: ')));
});
