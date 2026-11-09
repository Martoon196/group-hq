const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {EventEmitter}=require('node:events');
const {SocketClient,toTransportJid,privateSocketJid,incomingMessage}=require('./socket-client.cjs');
const {openAuthState}=require('./socket-auth.cjs');
const {createPairingPage}=require('./pairing-page.cjs');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const codec={BufferJSON:{replacer:(_k,v)=>v,reviver:(_k,v)=>v},initAuthCreds:()=>({registered:false}),proto:{Message:{AppStateSyncKeyData:{fromObject:x=>({...x,restored:true})}}}};

test('encrypted atomic auth survives restart and concurrent key writes; tampering is refused',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'ghq-auth-proof-'));
 const auth=await openAuthState(root,codec);
 await Promise.all([auth.state.keys.set({session:{one:{secret:'private-session-one'}}}),auth.state.keys.set({session:{two:{secret:'private-session-two'}}})]);
 auth.state.creds.registered=true;await auth.saveCreds();await auth.flush();
 const disk=await fs.readFile(path.join(root,'auth.sealed.json'),'utf8');
 assert(!disk.includes('private-session'));assert(!disk.includes('registered'));
 const restored=await openAuthState(root,codec);assert.equal(restored.state.creds.registered,true);
 assert.deepEqual(await restored.state.keys.get('session',['one','two']),{one:{secret:'private-session-one'},two:{secret:'private-session-two'}});
 await restored.state.keys.set({session:{one:null}});
 assert.equal((await (await openAuthState(root,codec)).state.keys.get('session',['one'])).one,null);
 const envelope=JSON.parse(await fs.readFile(path.join(root,'auth.sealed.json'),'utf8'));
 envelope.tag=Buffer.alloc(16).toString('base64');await fs.writeFile(path.join(root,'auth.sealed.json'),JSON.stringify(envelope));
 await assert.rejects(openAuthState(root,codec));
});

test('JIDs preserve PN and LID distinctions and sending refuses group/broadcast recipients',()=>{
 assert.equal(toTransportJid('447700900001:2@s.whatsapp.net'),'447700900001@c.us');
 assert.equal(toTransportJid('123456789:2@lid'),'123456789@lid');
 assert.equal(toTransportJid('447700900001@evil.invalid'),null);
 assert.equal(privateSocketJid('447700900001@c.us'),'447700900001@s.whatsapp.net');
 assert.equal(privateSocketJid('123456789@lid'),'123456789@lid');
 for(const value of ['123456789@g.us','status@broadcast','invalid'])assert.throws(()=>privateSocketJid(value));
});

function fixture(number='447700900001') {
 const sockets=[],calls=[],auth={state:{creds:{},keys:{}},saveCreds:async()=>{},flush:async()=>{}};
 const provider={normalizeMessageContent:x=>x,Browsers:{ubuntu:()=>['Ubuntu','Chrome','22'],windows:()=>['Windows','Desktop','10']},DisconnectReason:{restartRequired:515,loggedOut:401,badSession:500},
  default:options=>{const socket={options,ev:new EventEmitter(),user:{id:number+':2@s.whatsapp.net'},end:()=>calls.push('end'),
   groupFetchAllParticipating:async()=>{calls.push('groups');return {one:{id:'123456789@g.us',subject:'Test group'}};},
   signalRepository:{lidMapping:{getPNForLID:async()=>null}},
   sendMessage:async(jid,content)=>{calls.push({jid,content});return {key:{id:'provider-test-id'}};}};
   sockets.push(socket);return socket;}};
 const client=new SocketClient({dataPath:'unused',expectedNumber:'447700900001',loadProvider:async()=>provider,authFactory:async()=>auth,loggerFactory:()=>({})});
 return {client,sockets,calls,provider,auth};
}
function packet(changes={}) {return {key:{id:'message-one',remoteJid:'123456789@g.us',participant:'987654321@lid',participantAlt:'447700900002@s.whatsapp.net',fromMe:false},messageTimestamp:Math.floor(Date.now()/1000),message:{conversation:'Income'},pushName:'Test person',...changes};}

test('socket confirms selected account before groups/messages; notify-only live input and PN mapping',async()=>{
 const f=fixture();await f.client.initialize();let messages=[];f.client.on('message',m=>messages.push(m));
 await assert.rejects(f.client.getChats());
 f.sockets[0].ev.emit('messages.upsert',{type:'notify',messages:[packet()]});assert.equal(messages.length,0);
 await f.client.connectionUpdate(f.sockets[0],{connection:'open'});
 assert.equal(f.client.info.wid._serialized,'447700900001@c.us');assert.equal((await f.client.getChats()).length,1);
 f.sockets[0].ev.emit('messages.upsert',{type:'append',messages:[packet()]});
 f.sockets[0].ev.emit('messages.upsert',{type:'notify',requestId:'history',messages:[packet()]});
 f.sockets[0].ev.emit('messages.upsert',{type:'notify',messages:[packet({messageTimestamp:Math.floor(Date.now()/1000)-100})]});
 f.sockets[0].ev.emit('messages.upsert',{type:'notify',messages:[packet({key:{...packet().key,fromMe:true}})]});
 assert.equal(messages.length,0);
 f.sockets[0].ev.emit('messages.upsert',{type:'notify',messages:[packet()]});assert.equal(messages.length,1);
 assert.equal(messages[0].author,'987654321@lid');assert.equal(messages[0].body,'Income');
 assert.deepEqual(await f.client.getContactLidAndPhone(['987654321@lid']),[{pn:'447700900002@c.us'}]);
 assert.equal((await f.client.sendMessage('447700900002@c.us','Requested plan')).id._serialized,'baileys:provider-test-id');
 await assert.rejects(f.client.sendMessage('123456789@g.us','No group sends'));
 assert.equal(f.calls.filter(c=>typeof c==='object').length,1);
 await f.client.destroy();
});

test('a paired session keeps its pairing browser when history is enabled, and a fresh pairing records its profile',async()=>{
 // Existing link paired as Chrome, before the profile was recorded: enabling history must not change identity (428).
 let f=fixture();f.auth.state.creds={me:{id:'447700900001:2@s.whatsapp.net'}};
 f.client.allowHistory=()=>true;f.client.prepareHistory=async()=>true;
 await f.client.initialize();
 assert.deepEqual(f.sockets[0].options.browser,['Ubuntu','Chrome','22']);assert(f.sockets[0].options.syncFullHistory);
 await f.client.connectionUpdate(f.sockets[0],{connection:'open'});assert.equal(f.auth.state.creds.ghqBrowser,'chrome');
 await f.client.destroy();
 // A fresh pairing with history uses the desktop profile and keeps it on later reconnects, even with history off.
 f=fixture();f.client.prepareHistory=async()=>true;await f.client.initialize();
 assert.deepEqual(f.sockets[0].options.browser,['Windows','Desktop','10']);
 f.auth.state.creds.me={id:'447700900001:2@s.whatsapp.net'};
 await f.client.connectionUpdate(f.sockets[0],{connection:'open'});assert.equal(f.auth.state.creds.ghqBrowser,'desktop');
 f.client.prepareHistory=async()=>false;await f.client.connect();
 assert.deepEqual(f.sockets[1].options.browser,['Windows','Desktop','10']);
 await f.client.destroy();
 // An explicit recovery override applies only to a paired session that has no recorded profile.
 f=fixture();f.auth.state.creds={me:{id:'447700900001:2@s.whatsapp.net'}};
 f.client.pairedProfile='desktop';await f.client.initialize();
 assert.deepEqual(f.sockets[0].options.browser,['Windows','Desktop','10']);await f.client.destroy();
 assert.throws(()=>new SocketClient({pairedProfile:'firefox'}));
});

test('the verified desktop history profile routes old and append packets only to the archive',async()=>{
 const f=fixture(), historic=[], live=[];
 f.client.allowHistory=()=>true;f.client.prepareHistory=async()=>true;
 f.client.on('direct_history',bundle=>historic.push(bundle));f.client.on('message',m=>live.push(m));
 await f.client.initialize();await f.client.connectionUpdate(f.sockets[0],{connection:'open'});
 const socket=f.sockets[0];assert(socket.options.syncFullHistory);assert.deepEqual(socket.options.browser,['Windows','Desktop','10']);
 const old=packet({messageTimestamp:Date.UTC(2016,1,2)/1000});
 socket.ev.emit('messages.upsert',{type:'append',messages:[old]});
 socket.ev.emit('messages.upsert',{type:'notify',messages:[old]});
 socket.ev.emit('messaging-history.set',{chats:[],messages:[old]});
 assert.equal(historic.length,3);assert.equal(live.length,0);assert.equal(f.calls.length,0);
 await f.client.destroy();
});

test('wrong account never opens groups or sends; logged-out connection does not reconnect',async()=>{
 const wrong=fixture('447700900099');let failed=0;wrong.client.on('auth_failure',()=>failed++);
 await wrong.client.initialize();await wrong.client.connectionUpdate(wrong.sockets[0],{connection:'open'});
 assert.equal(failed,1);assert.equal(wrong.client.ready,false);await assert.rejects(wrong.client.getChats());
 assert(!wrong.calls.includes('groups'));
 const f=fixture();await f.client.initialize();await f.client.connectionUpdate(f.sockets[0],{connection:'close',lastDisconnect:{error:{output:{statusCode:401}}}});
 assert.equal(f.client.closed,true);assert.equal(f.sockets.length,1);await f.client.destroy();
});

test('pair-only mode cannot expose messages, read groups/contacts or send even after successful pairing',async()=>{
 const f=fixture();f.client.pairOnly=true;let messages=0;f.client.on('message',()=>messages++);
 await f.client.initialize();await f.client.connectionUpdate(f.sockets[0],{connection:'open'});
 assert.equal(f.client.ready,true);
 f.sockets[0].ev.emit('messages.upsert',{type:'notify',messages:[packet()]});
 assert.equal(messages,0);await assert.rejects(f.client.getChats(),/Pairing only/);
 await assert.rejects(f.client.getContactLidAndPhone(['987654321@lid']),/Pairing only/);
 await assert.rejects(f.client.sendMessage('447700900002@c.us','hello'),/Pairing only/);
 assert(!f.calls.includes('groups'));assert.equal(f.calls.filter(c=>typeof c==='object').length,0);
 await f.client.destroy();
});

test('pairing restart reuses auth, refuses stale socket events, and persistence failure closes sending',async()=>{
 const f=fixture();await f.client.initialize();const old=f.sockets[0];
 await f.client.connectionUpdate(old,{connection:'close',lastDisconnect:{error:{output:{statusCode:515}}}});
 await new Promise(resolve=>setTimeout(resolve,130));assert.equal(f.sockets.length,2);
 await f.client.connectionUpdate(old,{connection:'open'});assert.equal(f.client.ready,false);
 await f.client.connectionUpdate(f.sockets[1],{connection:'open'});assert.equal(f.client.ready,true);
 f.auth.flush=async()=>{throw Error('storage failed');};
 await assert.rejects(f.client.sendMessage('447700900002@c.us','hello'));
 assert.equal(f.calls.filter(c=>typeof c==='object').length,0);
 f.client.persistenceFailed();assert.equal(f.client.closed,true);await f.client.destroy().catch(()=>{});
});

test('pairing page serves private SVG with no-store; no QR data in URL/disk and readiness removes QR',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'ghq-pairing-proof-'));
 const page=await createPairingPage(root,{renderQr:async()=>'<svg aria-label="Synthetic pairing QR"></svg>'});
 try {
  assert.equal(new URL(page.url).hostname,'127.0.0.1');
  await page.showQr('synthetic-private-qr-not-for-logs');assert.equal(page.status().qrReady,true);
  const response=await fetch(page.url);const html=await response.text();
  assert.equal(response.headers.get('cache-control'),'no-store');assert(html.includes('<svg'));assert(!html.includes('synthetic-private-qr-not-for-logs'));
  assert.equal((await fs.readdir(root)).join(','),'pairing-url.txt');assert(!page.url.includes('synthetic'));
  page.setStatus('connected');assert.equal(page.status().qrReady,false);assert(!(await (await fetch(page.url)).text()).includes('<svg'));
 }finally{await page.close();}
 const restart=await createPairingPage(root,{renderQr:async()=>'<svg></svg>'});
 try {assert.equal(restart.url,page.url);assert.equal(restart.status().qrReady,false);}finally{await restart.close();}
});
