const test=require('node:test');
const assert=require('node:assert/strict');
const {createSetup}=require('./linkedin-setup.cjs');
const bridge={id:'local-linkedin',type:'linkedin',provider:'local',status:'available'};
const first={bridgeID:bridge.id,loginSessionID:'one',status:'waiting_for_input',currentStep:{
  stepID:'login',type:'user_input',fields:[{id:'identifier'},{id:'password'}]}};
const display={...first,status:'waiting_for_display',currentStep:{stepID:'approve',type:'display_and_wait',
  instructions:'Approve this sign-in in LinkedIn.',display:{type:'code',data:'SYNTHETIC'}}};

test('slow credentials return an immediate status page, confirm native challenge on timeout and never replay', async t=>{
 let rejectStep, result=first, credentialPosts=0, displayPosts=0;
 const engine={accounts:async()=>[],request:async(path,opt)=>{
   if(path.endsWith('login-flows'))return {items:[{id:'password'}]};
   if(path.endsWith('local-linkedin'))return bridge;
   if(path.endsWith('/login-sessions'))return first;
   if(path.endsWith('/steps/login')){credentialPosts++;return new Promise((_,reject)=>{rejectStep=reject;});}
   if(path.endsWith('/steps/approve')){displayPosts++;return {...first,status:'complete',currentStep:undefined};}
   return result;
 }};
 const {server,url}=await createSetup(engine,{port:0});t.after(()=>server.close());
 async function post(action,other={}) {
   const html=await (await fetch(url)).text();
   const token=html.match(/name="token" value="([^"]+)"/)[1];
   const response=await fetch(url,{method:'POST',redirect:'manual',headers:{Origin:new URL(url).origin},
     body:new URLSearchParams({token,action,...other}),signal:AbortSignal.timeout(2000)});
   assert.equal(response.status,303);
 }
 async function state(){return (await fetch(url+'/status')).json();}
 async function settled(){for(let i=0;i<30;i++){if((await state()).phase!=='checking')return;await new Promise(r=>setTimeout(r,10));}assert.fail('still waiting');}
 await post('start');await settled();
 await post('submit',{f0:'fixture@example.test',f1:'private-fixture'});
 assert.equal((await state()).phase,'checking');
 const wait=await (await fetch(url)).text();assert.match(wait,/http-equiv="refresh"/);assert.doesNotMatch(wait,/private-fixture/);
 result=display;rejectStep(Error('timeout'));await settled();assert.equal(credentialPosts,1);
 const next=await (await fetch(url)).text();assert.match(next,/I completed the LinkedIn verification/);assert.match(next,/SYNTHETIC/);
 await post('display');await settled();assert.equal(displayPosts,1);assert.equal(credentialPosts,1);
});
