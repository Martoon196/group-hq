const test = require('node:test');
const assert = require('node:assert/strict');
const {GrowthHqClient} = require('./client.cjs');
const {archiveCycle} = require('./history.cjs');

test('archive requests use the signed personal history route', async () => {
  let target;
  const cloud = new GrowthHqClient('s'.repeat(32), async (url, options) => {
    target = new URL(url);
    assert.equal(options.method, 'POST');
    assert.match(options.headers['x-ghq-signature'], /^[a-f0-9]{64}$/);
    return {ok:true,text:async()=>'{"ok":true}'};
  });
  assert.deepEqual(await cloud.request('history', {chunk_id:'x'}), {ok:true});
  assert.equal(target.pathname, '/api/personal-inbox/v1/history');
});

test('paged chat discovery and years of available Beeper messages survive a lost archive acknowledgement',async()=>{
  const binding={key:'li-fixture',channel:'linkedin',account_id:'native-a',user_id:'self'};
  const chat=id=>({id,accountID:'native-a',type:'single',title:'Fixture',participants:{items:[{id:'peer'}]}});
  const message=(c,i)=>({id:'m-'+i,chatID:c.id,accountID:'native-a',text:'Fixture '+i,
    timestamp:new Date(Date.UTC(2016,i,1)).toISOString()});
  let now=Date.UTC(2026,0,1), fail=true;
  const engine={
    chats:async(_,cur)=>cur ? {items:[chat('b')],hasMore:false} : {items:[chat('a')],hasMore:true,oldestCursor:'older-chats'},
    chat:async(_,id)=>chat(id),
    messages:async(_,c,cur)=>cur ? {items:[message(c,0)],hasMore:false,oldestCursor:'end'} :
      {items:Array.from({length:120},(_,i)=>message(c,120-i)),hasMore:true,oldestCursor:'older-messages'}
  };
  const state={state:{creds:{}},saveCreds:async()=>{}}, ids=new Set(), retries=[], oldest=[];
  const cloud={request:async(action,p)=>{
    assert.equal(action,'history');assert.equal(p.connector_id,'beeper');
    assert(p.thread.messages.length<=100);
    for(const m of p.thread.messages)ids.add(p.thread.id+'/'+m.id);
    if(p.thread.id==='a' && !p.reached_start)retries.push(p.chunk_id);
    if(fail){fail=false;throw Error('lost ACK');}
    if(p.reached_start)oldest.push(p.thread.id);
    return {ok:true,chunk_id:p.chunk_id,stored:p.thread.messages.length,reached_start:p.reached_start};
  }};
  for(let i=0;i<6;i++){await archiveCycle(engine,cloud,state,binding,now);now+=61000;}
  assert.equal(retries[0],retries[1]);assert.deepEqual(oldest.sort(),['a','b']);
  assert.equal(ids.size,242);
});
