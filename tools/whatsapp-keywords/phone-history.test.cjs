const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {createPhoneHistory, archiveMessage,openSpool}=require('./phone-history.cjs');

const self='447700900110@c.us', peer='447700900120@c.us', device='wa-device:'+'a'.repeat(64);
const raw=(i, room=peer)=>({key:{id:'m'+i,remoteJid:room,fromMe:i%2===0},messageTimestamp:Date.UTC(2015,i,1)/1000,message:{conversation:'fixture-'+i}});
const provider={normalizeMessageContent:x=>x};

test('linked-phone history resumes from archive ACK, requests older pages only from the same phone, and never sends a chat reply',async()=>{
 const journal={state:{creds:{}},saveCreds:async()=>{}};
 const files=new Map(),spool={append:async p=>files.set(p.chunk_id,p),list:async()=>[...files.keys()],
   read:async key=>files.get(key),remove:async key=>files.delete(key)};
 const requests=[], arch=[], client={ready:true,socket:{fetchMessageHistory:async(...args)=>requests.push(args)},
   sendMessage:()=>assert.fail('Historical messages must never send a reply')};
 let enable=false,lost=true;
 const history=createPhoneHistory({client,device,self,root:'unused',storage:async()=>journal,
   enabled:()=>enable,spoolFactory:async()=>spool,call:async(action,p)=>{
     assert.equal(action,'history');arch.push(p);
     if(lost){lost=false;throw Error('fixture lost ACK');}
     return {ok:true,chunk_id:p.chunk_id,stored:p.thread.messages.length,reached_start:p.reached_start};
   }});
 const bundle={messages:[...Array.from({length:121},(_,i)=>raw(i)),raw(1,'123456789@g.us'),raw(2,self)],chats:[{id:peer}]};
 await history.accept(bundle,provider);assert.equal(files.size,0);
 enable=true;await history.accept(bundle,provider);assert.equal(files.size,2);
 await assert.rejects(history.tick());assert.equal(requests.length,0);
 assert.equal(journal.state.creds.historyPeers,undefined);
 await history.tick();assert.equal(arch[0].chunk_id,arch[1].chunk_id);
 await history.tick();assert.deepEqual(requests,[[100,{remoteJid:peer.replace('@c.us','@s.whatsapp.net'),fromMe:true,id:'m0'},Date.UTC(2015,0,1)]]);
 assert(arch.every(p=>p.thread.id===peer && !p.reached_start));
 await history.tick();assert.equal(requests.length,1);
 await history.accept({messages:[],chats:[{id:peer,endOfHistoryTransferType:1}]},provider);
 await history.tick();await history.tick();assert.equal(arch.at(-1).reached_start,true);assert.equal(requests.length,1);
 assert.equal(archiveMessage({...raw(0),message:{imageMessage:{caption:'Old caption'}}},provider,self).message.body,'Old caption');
});

test('historical chunks survive a process restart encrypted until the server acknowledges them',async t=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'wa-history-fixture-'));
 t.after(()=>fs.rm(dir,{recursive:true,force:true}));
 const journal={state:{creds:{}},saveCreds:async()=>{}}, packet={chunk_id:'1'.repeat(64),reached_start:false,private:'synthetic sensitive fixture'};
 const store=await openSpool(dir,journal);await store.append(packet);
 const names=await store.list();assert.equal(names.length,1);
 assert(!(await fs.readFile(path.join(dir,names[0]))).includes(packet.private));
 const resumed=await openSpool(dir,journal);assert.deepEqual(await resumed.read(names[0]),packet);
 await resumed.remove(names[0]);assert.deepEqual(await resumed.list(),[]);
});
