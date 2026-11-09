const test=require('node:test');
const assert=require('node:assert/strict');
const {createSetup}=require('./linkedin-setup.cjs');
const {cycle}=require('./runtime.cjs');

const bridge={id:'local-linkedin',provider:'local',type:'linkedin',status:'available'};
const ac={accountID:'linkedin-account',user:{id:'member-1'},bridge,status:'connected'};

test('owner completes native LinkedIn challenges on loopback and publishes only account discovery',async()=>{
  let posted=[], linked=false, registered=0;
  const session=(id,fields)=>({bridgeID:bridge.id,loginSessionID:'attempt',status:'waiting_for_input',
    currentStep:{stepID:id,type:'user_input',fields}});
  const engine={accounts:async()=>linked?[ac]:[],request:async(path,options)=>{
    if(path.endsWith('/login-flows'))return {items:[{id:'password'}]};
    if(path==='/v1/bridges/local-linkedin')return bridge;
    posted.push([path,options.body]);
    if(path.endsWith('/login-sessions'))return session('credentials',[{id:'identifier',label:'identifier'},{id:'password',label:'Password'}]);
    if(path.endsWith('/steps/credentials'))return session('challenge',[{id:'code',label:'Verification code'}]);
    assert.ok(path.endsWith('/steps/challenge'));linked=true;
    return {bridgeID:bridge.id,loginSessionID:'attempt',status:'complete'};
  }};
  const setup=await createSetup(engine,{port:0,discovered:async()=>{registered++;}});
  const origin=new URL(setup.url).origin;
  async function submit(action,values={},source=origin,stale){
    const page=await (await fetch(setup.url)).text(),token=stale||page.match(/name="token" value="([^"]+)"/)[1];
    return fetch(setup.url,{method:'POST',redirect:'manual',headers:{Origin:source},
      body:new URLSearchParams({action,token,...values})});
  }
  try {
    assert.equal((await submit('start',{},'https://other.example')).status,403);
    assert.equal((await submit('start',{},origin,'invalid')).status,409);
    assert.equal(posted.length,0);
    assert.equal((await submit('start')).status,303);
    let page=await (await fetch(setup.url)).text();
    assert.match(page,/type="password"/);assert.match(page,/LinkedIn email or phone/);
    assert.equal((await submit('submit',{f0:'example@example.test',f1:'fake-secret'})).status,303);
    page=await (await fetch(setup.url)).text();
    assert.match(page,/Verification code/);assert.doesNotMatch(page,/fake-secret|example@example/);
    assert.equal((await submit('submit',{f0:'000000'})).status,303);
    assert.equal(registered,1);
    assert.match(await (await fetch(setup.url)).text(),/enable your LinkedIn inbox/);
    assert.deepEqual(posted.map(x=>x[0]),[
      '/v1/bridges/local-linkedin/login-sessions','/v1/bridges/local-linkedin/login-sessions/attempt/steps/credentials',
      '/v1/bridges/local-linkedin/login-sessions/attempt/steps/challenge']);
  }finally{await new Promise(r=>setup.server.close(r));}
});

test('reuses the native LinkedIn binding from engine discovery and never drains the unscoped older-server queue',async()=>{
  const facebook={...ac,accountID:'other',bridge:{type:'facebookgo',provider:'cloud'},user:{id:'other-user'}};
  const read=[], actions=[];
  const engine={accounts:async()=>[ac,facebook],chats:async binding=>{
    read.push(binding);
    return {items:[]};
  }};
  const cloud={request:async(action,data)=>{
    actions.push([action,data]);
    if(action==='pending')return {reply_ids:['not-scoped']};
    assert.equal(action,'accounts');return {accounts:[{key:'li',account_id:ac.accountID,user_id:ac.user.id,enabled:true},
      {key:'fb',account_id:facebook.accountID,user_id:facebook.user.id,enabled:true}]};
  }};
  await cycle(engine,cloud,{state:{creds:{deliveries:{}}}},'linkedin');
  assert.equal(read.length,1);assert.equal(read[0].channel,'linkedin');assert.equal(read[0].key,'li');
  assert.deepEqual(actions.map(x=>x[0]),['accounts','pending']);
  assert.equal(actions[1][1].channel,'linkedin');
});
