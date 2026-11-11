const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {EventEmitter}=require('node:events');
const {SocketClient,groupPacket,incomingMessage}=require('./socket-client.cjs');
const {createMissed}=require('./missed.cjs');
const {setAside,spoolOrder}=require('./catchup.cjs');

// Synthetic fixtures only: no WhatsApp connection, no Growth HQ request, no real number.
const GROUP='123456789@g.us', OTHER='555000111@g.us', T0=1800000000, plain={normalizeMessageContent:x=>x};
const rule={id:'rule-1',enabled:true,groupIds:[GROUP],keywords:['Land'],matchMode:'exact'};
const late=(n,text,{sender='4477009'+String(10000+n),group=GROUP,fromMe=false,name='Fixture '+n}={})=>({
  key:{remoteJid:group,fromMe,id:'L'+n,...(fromMe?{}:{participant:sender+'@s.whatsapp.net'})},messageTimestamp:T0-n*60,message:{conversation:text},pushName:name});

async function bench(t,options={}) {
  const spool=await fs.mkdtemp(path.join(os.tmpdir(),'ghq-missed-fixture-'));
  t.after(()=>fs.rm(spool,{recursive:true,force:true}));
  const notes=[], learned=[];
  const missed=createMissed({client:{remember:(a,b)=>learned.push([a,b])},spool,note:m=>notes.push(m),now:()=>T0*1000,
    phoneOf:async id=>/^[1-9][0-9]{6,14}@c\.us$/.test(id)?id.split('@')[0]:null,...options});
  const outbox=async()=>Promise.all((await fs.readdir(spool)).sort().map(async n=>JSON.parse(await fs.readFile(path.join(spool,n),'utf8'))));
  return {missed,spool,notes,outbox};
}

test('a request delivered late waits for the rules, then goes to the outbox once with its own event ID',async t=>{
  const b=await bench(t);
  const packet=late(90,'LAND!');
  b.missed.seen(groupPacket(packet,plain));
  b.missed.seen(groupPacket(late(80,'just chatting'),plain));
  b.missed.seen(groupPacket(late(70,'Land',{group:OTHER}),plain));
  b.missed.seen(groupPacket(late(60,'Land',{fromMe:true}),plain));
  assert.equal(b.missed.waiting(),3);assert.deepEqual(await b.outbox(),[]);
  assert.equal(await b.missed.submit({revision:1,rules:[]}),0,'nothing is covered by a rule yet, and nothing is kept');
  assert.equal(b.missed.waiting(),0);
  b.missed.seen(groupPacket(packet,plain));
  assert.equal(await b.missed.submit({revision:2,rules:[rule]}),1);
  const [entry]=await b.outbox();
  assert.equal(entry.action,'missed-events');
  assert.deepEqual(entry.value,{eventId:groupPacket(packet,plain).eventId,listenerId:'personal-7510',groupId:GROUP,senderId:'447700910090@c.us',
    senderPhone:'447700910090',senderName:'Fixture 90',text:'LAND!',observedAt:new Date((T0-90*60)*1000).toISOString()});
  // The same message is the same event however it arrives: its ID is the one the live path would have used.
  assert.equal(entry.value.eventId,incomingMessage({...packet,messageTimestamp:Math.floor(Date.now()/1000)},Date.now()-1000,plain).id._serialized);
  b.missed.seen(groupPacket(packet,plain));
  assert.equal(await b.missed.submit({revision:2,rules:[rule]}),0,'already in the outbox');
  assert.equal((await b.outbox()).length,1);
  for(const line of b.notes)assert.doesNotMatch(line,/LAND|Fixture|4477009|123456789/);
});

test('too old, from the future, without a resolvable number, or in pairing-only mode: never submitted',async t=>{
  const b=await bench(t);
  b.missed.seen(groupPacket(late(73*60,'Land'),plain));                       // older than the 72 hours kept
  b.missed.seen(groupPacket(late(-5,'Land'),plain));                          // a clock ahead of ours
  assert.equal(b.missed.waiting(),0);
  const hidden=late(10,'Land');hidden.key.participant='987654321@lid';
  b.missed.seen(groupPacket(hidden,plain));
  assert.equal(await b.missed.submit({rules:[rule]}),0);assert.deepEqual(await b.outbox(),[]);
  assert.ok(b.notes.some(n=>/phone identity review/.test(n)));
  assert.equal(await b.missed.submit({rules:[{...rule,enabled:false}]}),0);
  const p=await bench(t,{pairOnly:true});
  p.missed.seen(groupPacket(late(10,'Land'),plain));
  assert.equal(p.missed.waiting(),0);assert.equal(await p.missed.submit({rules:[rule]}),0);
  const capped=await bench(t,{limits:{waiting:2}});
  for(const n of [30,20,10])capped.missed.seen(groupPacket(late(n,'Land'),plain));
  assert.equal(capped.missed.waiting(),2);
});

test('the socket offers what WhatsApp held back while offline, and never to the live path',async()=>{
  // The real connect() handlers over a stand-in provider: no connection is opened.
  const sockets=[],auth={state:{creds:{},keys:{}},saveCreds:async()=>{},flush:async()=>{}};
  const provider={normalizeMessageContent:x=>x,Browsers:{ubuntu:()=>['Ubuntu','Chrome','22'],windows:()=>['Windows','Desktop','10']},
    DisconnectReason:{restartRequired:515,loggedOut:401,badSession:500},proto:{HistorySync:{HistorySyncType:{ON_DEMAND:6}}},getHistoryMsg:()=>null,
    default:options=>{const socket={options,ev:new EventEmitter(),user:{id:'447700900001:2@s.whatsapp.net'},end:()=>{},
      signalRepository:{lidMapping:{getPNForLID:async()=>null}}};sockets.push(socket);return socket;}};
  const client=new SocketClient({dataPath:'unused',expectedNumber:'447700900001',loadProvider:async()=>provider,authFactory:async()=>auth,loggerFactory:()=>({})});
  const missed=[],live=[];
  client.on('group_missed',m=>missed.push(m.id));client.on('message',m=>live.push(m.body));
  await client.initialize();const socket=sockets[0];
  socket.ev.emit('messages.upsert',{type:'append',messages:[late(30,'Land')]});
  assert.deepEqual(missed,[],'nothing is read before the account is confirmed');
  socket.ev.emit('connection.update',{connection:'open'});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(client.ready,true);
  const now=Math.floor(Date.now()/1000), at=(id,seconds,text,extra={})=>({...late(1,text,extra),key:{...late(1,text,extra).key,id},messageTimestamp:now-seconds});
  socket.ev.emit('messages.upsert',{type:'append',messages:[at('HELD',3600,'Land'),at('MINE',3000,'Land',{fromMe:true}),{...at('PIC',2000,''),message:{imageMessage:{}}}]});
  socket.ev.emit('messages.upsert',{type:'notify',messages:[at('LATE',600,'Land')]});          // delivered as new, but typed before we connected
  socket.ev.emit('messages.upsert',{type:'append',requestId:'R1',messages:[at('ASKED',900,'Land')]});   // an answer to a request, not a held-back message
  socket.ev.emit('messages.upsert',{type:'notify',messages:[at('LIVE',0,'Land')]});            // genuinely live: the live path's, not ours
  assert.deepEqual(missed,['HELD','LATE']);assert.deepEqual(live,['Land']);
  await client.destroy();
  // An older server answers 404 to the new action; that must not block the live requests queued behind it.
  assert.equal(setAside(404,'missed-events'),true);assert.equal(setAside(404,'events'),false);assert.equal(spoolOrder('missed-events'),1);
});
