const test=require('node:test');const assert=require('node:assert/strict');const crypto=require('node:crypto');
const fs=require('node:fs/promises');const os=require('node:os');const path=require('node:path');
const {EngineClient,GrowthHqClient,snapshot,ENGINE}=require('./client.cjs');
const {dispatch,settlePending,openState}=require('./runtime.cjs');const {createWizard}=require('./connect.cjs');
const binding={key:'personal-test',account_id:'account-1',user_id:'me',channel:'facebook'};
const account={accountID:'account-1',user:{id:'me'},bridge:{type:'facebook',provider:'local'},status:'connected'};
const chat={id:'native-chat',accountID:'account-1',type:'single',title:'Durham contact',participants:{items:[{id:'me',isSelf:true},{id:'peer'}],hasMore:false}};
const json=value=>new Response(JSON.stringify(value),{status:200});
const journal=()=>({state:{creds:{deliveries:{}}},saveCreds:async()=>{}});
test('a wrong-account or merged conversation cannot be sent to',async()=>{
  for(const invalid of [{...chat,accountID:'other'},{...chat,merge:{}}]){
    let writes=0;const engine=new EngineClient('private',async(url,opts)=>{if(opts.method==='POST')writes++;return json(url.endsWith('/accounts')?[account]:invalid);});
    await assert.rejects(engine.send(binding,chat.id,'reply'),/unbound_chat/);assert.equal(writes,0);
  }
});
test('provider timeout performs one POST and is never automatically retried',async()=>{
  let writes=0;const engine=new EngineClient('private',async(url,opts)=>{if(opts.method==='POST'){writes++;throw Error('timeout');}return json(url.endsWith('/accounts')?[account]:chat);});
  const state=journal(),cloud={request:async(action)=>action.endsWith('/begin')?{dispatch:true,...binding,thread_id:chat.id,body:'reply'}:{}};
  await dispatch(engine,cloud,state,'id');await dispatch(engine,cloud,state,'id');await settlePending(engine,cloud,state);
  assert.equal(writes,1);assert.equal(state.state.creds.deliveries.id.state,'settled');
});
test('crash after intent is durable never calls the provider again',async()=>{
  const state=journal();state.state.creds.deliveries.id={state:'begun'};
  let writes=0;const engine={send:async()=>writes++},cloud={request:async()=>({})};
  await dispatch(engine,cloud,state,'id');await settlePending(engine,cloud,state);assert.equal(writes,0);
});
test('lost final settlement response retries the final receipt, not the pending receipt or send',async()=>{
  const state=journal();state.state.creds.deliveries.id={state:'pending',binding,chatId:chat.id,pendingId:'pending-1'};
  const calls=[];let first=true;const cloud={request:async(action,result)=>{calls.push(result);if(result.state==='accepted'&&first){first=false;throw Error('lost');}}};
  const engine={outcome:async()=>({id:'native-1',status:'SUCCESS'})};
  await assert.rejects(settlePending(engine,cloud,state));assert.equal(state.state.creds.deliveries.id.state,'settling');
  await settlePending(engine,cloud,state);assert.deepEqual(calls.map(c=>c.state),['bridge_accepted','accepted','accepted']);
  assert.equal(calls.at(-1).previous_message_id,'pending-1');assert.equal(state.state.creds.deliveries.id.state,'settled');
});
test('private snapshots retain exact account and participant IDs without asserting consent',()=>{
  const row={id:'m1',chatID:chat.id,accountID:binding.account_id,senderID:'peer',timestamp:'2026-11-06T12:00:00Z',text:'Durham',isSender:false};
  const value=snapshot(binding,chat,{items:[row],hasMore:true});assert.equal(value.thread.participant_id,'peer');assert.equal(value.thread.messages[0].direction,'incoming');assert.equal(value.thread.has_more,true);assert.equal(value.consent,undefined);
  assert.throws(()=>snapshot(binding,chat,{items:[{...row,accountID:'other'}]}),/cross_account/);
});
test('cloud request is path-bound and contains no engine bearer token',async()=>{
  const key='s'.repeat(32);const cloud=new GrowthHqClient(key,async(url,options)=>{
    const p=new URL(url).pathname,stamp=options.headers['x-ghq-timestamp'];
    assert.equal(options.headers.Authorization,undefined);assert.equal(options.headers['x-ghq-signature'],crypto.createHmac('sha256',key).update(`${stamp}\nPOST\n${p}\n`).update(options.body).digest('hex'));return json({ok:true});
  });await cloud.request('accounts',{accounts:[]});await assert.rejects(cloud.request('../escape',{}),/invalid_action/);
});
test('OAuth handoff uses PKCE, rejects changed state and encrypts the token locally',async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'ghq-inbox-test-'));let redirect,challenge,verifier;
  const wizard=await createWizard(directory,{fetcher:async(url,options)=>{
    if(url.endsWith('oauth-authorization-server'))return json({authorization_endpoint:ENGINE+'/oauth/authorize',token_endpoint:ENGINE+'/oauth/token',registration_endpoint:ENGINE+'/oauth/register'});
    if(url.endsWith('/register')){redirect=JSON.parse(options.body).redirect_uris[0];return json({client_id:'test-public-client'});}
    verifier=new URLSearchParams(options.body).get('code_verifier');return json({access_token:'private-engine-token',token_type:'Bearer'});
  }});
  try{
    const page=await (await fetch(wizard.url)).text(),href=page.match(/href="([^"]+)"/)[1].replaceAll('&amp;','&'),auth=new URL(href);challenge=auth.searchParams.get('code_challenge');
    assert.equal(auth.searchParams.get('scope'),'read write');assert.equal((await fetch(redirect+'?state=wrong&code=code')).status,400);
    const callback=redirect+'?'+new URLSearchParams({state:auth.searchParams.get('state'),code:'code'});
    assert.equal((await fetch(callback)).status,200);assert.equal(wizard.connected(),true);
    assert.equal(crypto.createHash('sha256').update(verifier).digest('base64url'),challenge);
    assert.equal((await fetch(callback)).status,400);assert.equal((await fs.readFile(path.join(directory,'auth.sealed.json'),'utf8')).includes('private-engine-token'),false);
    assert.equal((await openState(directory)).state.creds.token,'private-engine-token');
  }finally{await new Promise(resolve=>wizard.server.close(resolve));await fs.rm(directory,{recursive:true,force:true});}
});
