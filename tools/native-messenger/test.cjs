const {test}=require('node:test'),assert=require('node:assert/strict');
const {normalise,displayedTime}=require('./reader.cjs');
const {validateAction,allowedUrl,client}=require('./login.cjs');
const expected={threadId:'12345',participantId:'98765',name:'Example Buyer'};
const binding={key:'native-example',account_id:'facebook-web:123',user_id:'123',channel:'facebook',timeZone:'Europe/London'};
const {validateTargets,readSearch,readPeer}=require('./discovery.cjs');
test('owner-selected conversations are no longer limited to three; selection identity remains exact',()=>{
  const many=Array.from({length:20},(_,i)=>({...expected,threadId:String(1000+i)}));
  assert.equal(validateTargets(many).length,20);assert.deepEqual(validateTargets([]),[]);
  assert.throws(()=>validateTargets([...many,many[0]]),/invalid_selection/);
  assert.throws(()=>validateTargets([{...expected,participantId:'not-a-native-id'}]),/invalid_selection/);
  assert.throws(()=>validateTargets(Array(101).fill(expected)),/invalid_selection/);
});
test('discovery extracts only visible Facebook thread links and reports restoration',()=>{
  const vm=require('node:vm');
  const root={innerText:'Syncing chat history. Loading...',querySelectorAll:()=>[
    {href:'https://www.facebook.com/messages/e2ee/t/12345/',innerText:'Example Buyer\nDurham'},
    {href:'https://www.facebook.com/messages/t/12346/',innerText:'Another Buyer\nDurham'},
    {href:'https://www.facebook.com/messages/e2ee/t/999/',innerText:''},
    {href:'https://evil.test/messages/e2ee/t/12345/',innerText:'Wrong origin'}]};
  const value=vm.runInNewContext('('+readSearch.toString()+')()',{URL,document:{querySelector:()=>root}});
  assert.equal(value.rows.length,2);assert.equal(value.rows[0].threadId,'12345');assert.equal(value.rows[0].name,'Example Buyer');
  assert.equal(value.rows[1].encrypted,false);assert.equal(value.restoring,true);
});
test('candidate identity requires one exact conversation log and unanimous native profile ID',()=>{
  const vm=require('node:vm');let ids=['98765'];
  const document={querySelectorAll:selector=>selector==='[role="log"]'?[{getAttribute:()=> 'Messages in conversation with Example Buyer'}]:
    ids.map(id=>({innerText:'Example Buyer',href:'https://www.facebook.com/'+id}))};
  const check=()=>vm.runInNewContext('('+readPeer.toString()+')("Example Buyer")',{URL,document});
  assert.equal(check(),'98765');ids.push('111');assert.equal(check(),null);ids=[];assert.equal(check(),null);
});
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
test('explicit text-only window retains text and marks omitted attachment/edited rows incomplete',()=>{
  const observed=fixture();observed.messages.push({id:'98765@msgr.123456791',label:null,heading:'Example'},{id:'98765@msgr.123456792',label:'At Today 18:00, Example: Edited text',heading:'ExampleEdited'});
  const packet=normalise(observed,{...expected,textOnly:true},binding);
  assert.equal(packet.thread.messages.length,2);assert.equal(packet.thread.has_more,true);assert.equal(packet.evidence.omitted_rows,2);
  assert.throws(()=>normalise(observed,expected,binding));
});
test('all-omitted text-only window is skipped instead of replacing history',()=>{
  assert.equal(normalise({...fixture(),messages:[{id:'98765@msgr.123456791',label:null,heading:'Example'}]},{...expected,textOnly:true},binding),null);
});
test('text-only filtering never hides a foreign message identity',()=>{
  const observed=fixture();observed.messages.push({id:'11111@msgr.123456791',label:null,heading:'Example'});
  assert.throws(()=>normalise(observed,{...expected,textOnly:true},binding),/invalid_message_identity/);
});
test('browser navigation restricted to real Facebook HTTPS hosts',()=>{assert.equal(allowedUrl('https://www.facebook.com/messages/'),true);for(const url of ['http://www.facebook.com/','https://facebook.com.evil.test/','file:///tmp/a','https://127.0.0.1/'])assert.equal(allowedUrl(url),false);});
test('remote controls bounded and no arbitrary command/evaluate surface',()=>{assert.equal(validateAction({action:'key',key:'Enter'}).key,'Enter');for(const v of [{action:'eval',text:'x'},{action:'click',x:-1,y:0},{action:'type',text:'a'.repeat(2001)},{action:'key',key:'Control+L'},{action:'navigate',url:'https://evil.test'}])assert.throws(()=>validateAction(v));});
test('connection page JavaScript parses',()=>{new (require('node:vm').Script)(client);});

const {dispatch,settle}=require('./runtime.cjs');
test('profile utility links without an ID do not change identity; different and missing IDs fail',async()=>{
  const {MessengerAdapter}=require('./runtime.cjs');
  let links=[];
  const page={goto:async()=>{},locator:()=>({filter:()=>({first:()=>({waitFor:async()=>{}})}),evaluateAll:async fn=>fn(links)})};
  const adapter=new MessengerAdapter({newPage:async()=>page},{userId:'123'},[]);
  links=[{href:'https://www.facebook.com/profile.php?fb_profile_edit_entry_point=1&id=123'},{href:'https://www.facebook.com/profile.php?fb_profile_edit_entry_point=1'}];
  await adapter.verifyIdentity();
  links.push({href:'https://www.facebook.com/profile.php?fb_profile_edit_entry_point=1&id=456'});
  await assert.rejects(adapter.verifyIdentity(),/account_changed/);
  links=[{href:'https://www.facebook.com/profile.php?fb_profile_edit_entry_point=1'}];
  await assert.rejects(adapter.verifyIdentity(),/account_changed/);
});
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
