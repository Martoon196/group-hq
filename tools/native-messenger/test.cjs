const {test}=require('node:test'),assert=require('node:assert/strict');
const {normalise,displayedTime}=require('./reader.cjs');
const {validateAction,allowedUrl,client}=require('./login.cjs');
const expected={threadId:'12345',participantId:'98765',name:'Example Buyer'};
const binding={key:'native-example',account_id:'facebook-web:123',user_id:'123',channel:'facebook',timeZone:'Europe/London'};
const fixture=()=>({threadId:'12345',participantId:'98765',name:'Example Buyer',observedAt:'2026-11-06T19:00:00Z',introductionVisible:true,virtualised:false,composerReady:true,
  messages:[{id:'98765@msgr.123456789',label:'At Wednesday 13:32, You: Is Durham the area you want?',heading:'You sent'},
    {id:'98765@msgr.123456790',label:'At Wednesday 21:21, Example: Yes, Durham please.',heading:'Example'}]});
test('native identity and message direction preserved; exact repeat stable',()=>{const a=normalise(fixture(),expected,binding),b=normalise(fixture(),expected,binding);assert.deepEqual(a,b);assert.equal(a.thread.messages[1].id,'98765@msgr.123456790');assert.equal(a.thread.messages[0].direction,'outgoing');assert.equal(a.thread.messages[1].direction,'incoming');assert.equal(a.thread.messages[0].occurred_at,'2026-11-04T13:32:00.000Z');assert.equal(a.evidence.time_precision,'minute');});
test('wrong thread/participant/account rejected',()=>{for(const key of ['threadId','participantId','name'])assert.throws(()=>normalise({...fixture(),[key]:'wrong'},expected,binding));assert.throws(()=>normalise(fixture(),expected,{...binding,account_id:'facebook-web:456'}));});
test('duplicate and foreign message identities rejected',()=>{const a=fixture();a.messages.push({...a.messages[0]});assert.throws(()=>normalise(a,expected,binding));a.messages.pop();a.messages[0].id='different@msgr.123';assert.throws(()=>normalise(a,expected,binding));});
test('sender mismatch rejected instead of guessing direction',()=>{const a=fixture();a.messages[1].heading='Someone';assert.throws(()=>normalise(a,expected,binding));});
test('unsupported dates and DST ambiguity rejected',()=>{assert.throws(()=>displayedTime('4 Nov at tea time','2026-11-06T19:00:00Z','Europe/London'));assert.throws(()=>displayedTime('Sunday 01:30','2026-10-25T12:00:00Z','Europe/London'));assert.throws(()=>displayedTime('Today 23:59','2026-11-06T19:00:00Z','Europe/London'));});
test('virtualised or absent introduction remains a partial window',()=>{assert.equal(normalise({...fixture(),virtualised:true},expected,binding).thread.has_more,true);assert.equal(normalise({...fixture(),introductionVisible:false},expected,binding).thread.has_more,true);assert.equal(normalise(fixture(),expected,binding).thread.has_more,false);});
test('empty or oversized windows cannot overwrite historic evidence',()=>{assert.throws(()=>normalise({...fixture(),messages:[]},expected,binding));assert.throws(()=>normalise({...fixture(),messages:Array(101).fill(fixture().messages[0])},expected,binding));});
test('browser navigation restricted to real Facebook HTTPS hosts',()=>{assert.equal(allowedUrl('https://www.facebook.com/messages/'),true);for(const url of ['http://www.facebook.com/','https://facebook.com.evil.test/','file:///tmp/a','https://127.0.0.1/'])assert.equal(allowedUrl(url),false);});
test('remote controls bounded and no arbitrary command/evaluate surface',()=>{assert.equal(validateAction({action:'key',key:'Enter'}).key,'Enter');for(const v of [{action:'eval',text:'x'},{action:'click',x:-1,y:0},{action:'type',text:'a'.repeat(2001)},{action:'key',key:'Control+L'},{action:'navigate',url:'https://evil.test'}])assert.throws(()=>validateAction(v));});
test('connection page JavaScript parses',()=>{new (require('node:vm').Script)(client);});

const {dispatch,settle}=require('./runtime.cjs');
test('native send intent persisted before send; accepted result replays without second send',async()=>{
  const data={deliveries:{}},order=[];let sends=0,settleAttempts=0;
  const state={state:{creds:data},saveCreds:async()=>order.push(JSON.parse(JSON.stringify(data)))};
  const cloud={request:async(action,payload)=>{if(action.endsWith('/begin'))return {dispatch:true,thread_id:'12345'};
    settleAttempts++;if(settleAttempts===1)throw Error('lost_ack');return {state:payload.state};}};
  const adapter={observe:async()=>({thread:{messages:[]}}),sendOnce:async()=>{assert.equal(data.deliveries.job.state,'begun');assert.ok(order.length);sends++;return 'native-id';}};
  await assert.rejects(dispatch(adapter,cloud,state,binding,'job'));assert.equal(data.deliveries.job.state,'settling');
  await settle(cloud,state);await dispatch(adapter,cloud,state,binding,'job');assert.equal(sends,1);assert.equal(data.deliveries.job.state,'settled');
});
test('crash after begun is unknown; no automatic resend',async()=>{
  const state={state:{creds:{deliveries:{job:{state:'begun'}}}},saveCreds:async()=>{}};let result;
  await settle({request:async(a,p)=>{result=p;}},state);assert.equal(result.state,'outcome_unknown');assert.equal(result.connector_id,'native-messenger');
});
test('pause before dispatch leaves the cloud queue unclaimed',async()=>{
  let calls=0;await dispatch({}, {request:async()=>{calls++;}}, {state:{creds:{deliveries:{}}}},binding,'job',()=>true);assert.equal(calls,0);
});
test('wrong account and unapproved thread rejected before browser actions',async()=>{
  const {MessengerAdapter}=require('./runtime.cjs');const adapter=new MessengerAdapter({}, {userId:'123'},[expected]);
  await assert.rejects(adapter.sendOnce({account_id:'other',user_id:'123',channel:'facebook',thread_id:'12345'},binding,new Set()),/wrong_send_account/);
  await assert.rejects(adapter.sendOnce({account_id:binding.account_id,user_id:'123',channel:'facebook',thread_id:'other'},binding,new Set()),/thread_not_authorised/);
});
