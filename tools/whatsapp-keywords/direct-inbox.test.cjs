const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {createDirectInbox, privateNotification} = require('./direct-inbox.cjs');

const number = '447700900110', self = number + '@c.us', peer = '447700900120@c.us';
const at = 1800000000000, id = crypto.randomUUID();
const device = 'wa-device:' + crypto.createHash('sha256').update(self).digest('hex');

test('only fresh private text enters the device inbox and mobile outgoing direction is preserved', () => {
  const packet = {key:{id:'A1',fromMe:false,remoteJid:peer},messageTimestamp:at/1000,
    message:{conversation:'Fixture text'},pushName:'Fixture'};
  const parse = (p) => privateNotification(p,at,{normalizeMessageContent:x=>x},self,at);
  assert.equal(parse(packet).direction,'incoming');
  assert.equal(parse({...packet,key:{...packet.key,fromMe:true}}).direction,'outgoing');
  for (const jid of [self,'123456789@g.us','status@broadcast','123456789@newsletter']) {
    assert.equal(parse({...packet,key:{...packet.key,remoteJid:jid}}),null);
  }
  assert.equal(parse({...packet,messageTimestamp:at/1000-30}),null);
  assert.equal(parse({...packet,message:{imageMessage:{caption:'Fixture'}}}),null);
});

test('collection and reply each need active policy and original-account claim; uncertain sends never replay', async () => {
  let allowed = {collect:false,reply:false,since:new Date(at-5000).toISOString()};
  let job, sends=0, failSettle=false;
  const events=[], outcomes=[], journal={state:{creds:{events:{},deliveries:{}}}, saveCreds:async()=>{}};
  const client = {ready:true,pairOnly:false,info:{wid:{_serialized:self}},sendMessage:async(jid,body)=>{
    assert.equal(jid,peer);assert.equal(body,'Fixture outgoing');sends++;return {id:{_serialized:'baileys:out1'}};
  }};
  const api=async(method,action,data)=>{
    assert.equal(data.device,device);assert.equal(data.listenerId,'personal-7510');
    if(action==='direct/status')return allowed;
    if(action==='direct/event'){events.push(data);return {stored:true};}
    if(action==='direct/pending')return {reply_ids:job?[id]:[]};
    if(action.endsWith('/begin')){const result=job;job=null;return result;}
    if(action.endsWith('/settle')){if(failSettle)throw Error('fixture interrupted');outcomes.push(data);return {};}
    assert.fail(action);
  };
  const direct=createDirectInbox({api,client,root:'test',expectedNumber:number,phoneOf:async()=>number,
    openJournal:async()=>journal,now:()=>at});
  const sample={id:'baileys:in1',peer,at:new Date(at).toISOString(),direction:'incoming',body:'Fixture incoming',phone:peer.split('@')[0]};
  await direct.tick(); await direct.accept(sample); assert.equal(Object.keys(journal.state.creds.events).length,0);
  allowed={...allowed,collect:true}; await direct.tick();await direct.accept(sample);await direct.accept(sample);
  await direct.tick(); assert.equal(events.length,1);
  const valid={dispatch:true,reply_id:id,account_key:'personal-whatsapp-device',account_id:device,user_id:device,
    channel:'whatsapp',thread_id:peer,body:'Fixture outgoing'};
  job={...valid,account_id:'different-device'}; await direct.tick();assert.equal(sends,0);assert.ok(job);
  allowed={...allowed,reply:true};await direct.tick(); assert.equal(outcomes.at(-1).state,'rejected'); assert.equal(sends,0);
  job=valid;failSettle=true;await assert.rejects(direct.tick());assert.equal(sends,1);
  failSettle=false;await direct.tick();assert.equal(sends,1);assert.equal(outcomes.at(-1).state,'bridge_accepted');
  assert.equal(events.at(-1).peer,peer);assert.equal(events.at(-1).direction,'outgoing');
  client.info.wid._serialized='different@c.us';assert.equal(direct.enabled(),false);
  client.info.wid._serialized=self;client.pairOnly=true;assert.equal(direct.enabled(),false);
});
