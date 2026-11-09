// A local UI for Beeper's documented on-device LinkedIn bridge login steps.
const http = require('node:http');
const crypto = require('node:crypto');
const {EngineClient} = require('./client.cjs');
const {openState} = require('./runtime.cjs');

const BRIDGE = 'local-linkedin';
const BASE = '/v1/bridges/' + BRIDGE;
const SETTINGS = 'https://group-hq.onrender.com/inbox/connect';
const PORT = 23765;
const nonce = () => crypto.randomBytes(32).toString('base64url');
const esc = value => String(value || '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function part(value) {
  if (typeof value !== 'string' || !value || value.length > 500 || value === '.' || value === '..') throw Error('invalid_step');
  return encodeURIComponent(value);
}
function html(body,refresh=false) {
  return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'+
    (refresh?'<meta http-equiv="refresh" content="2;url=/linkedin">':'')+
    '<title>Connect personal LinkedIn - Growth HQ</title><style>body{font:18px/1.55 system-ui;color:#18251d;background:#f7f6f1;max-width:38rem;margin:7vh auto;padding:1.5rem}h1{line-height:1.2}label{display:block;margin:1rem 0}input{display:block;padding:.65rem;width:calc(100% - 2rem);font:inherit}button,a{font:inherit}button{background:#164a36;color:white;border:0;padding:.65rem 1rem;border-radius:.3rem;cursor:pointer}a{color:#164a36}</style>'+
    '<main><h1>Connect personal LinkedIn</h1>'+body+'</main></html>';
}
async function readForm(req) {
  if (req.headers['content-type']?.split(';')[0] !== 'application/x-www-form-urlencoded') throw Error('invalid_form');
  const chunks=[]; let size=0;
  for await(const chunk of req) {
    size+=chunk.length;
    if(size>16000)throw Error('invalid_form');
    chunks.push(chunk);
  }
  return new URLSearchParams(Buffer.concat(chunks).toString('utf8'));
}
async function createSetup(engine, {port=PORT, discovered=async()=>{}}={}) {
  let origin, token=nonce(), session=null, busy=false, linked=false, registered=false;
  const path='/linkedin';
  let checkOnly=false, note='', phase='ready';
  const linkedAccounts=async()=> (await engine.accounts()).filter(a=>a.bridge?.id===BRIDGE &&
    a.bridge?.provider==='local' && ['connected','backfilling'].includes(a.status));
  function update(result) {
    if(result?.bridgeID!==BRIDGE || !result.loginSessionID ||
        (session && result.loginSessionID!==session.loginSessionID))throw Error('invalid_session');
    session=result;
  }
  function form(action, fields='') {
    return '<form action="'+path+'" method="post" autocomplete="off"><input type="hidden" name="token" value="'+token+'">'+
      '<input type="hidden" name="action" value="'+action+'">'+fields;
  }
  const button=label=>'<button>'+label+'</button></form>';
  function view(message=note) {
    if (busy) return html('<p role="status">Checking the sign-in with Beeper. This page refreshes while LinkedIn responds.</p><a href="/linkedin">Check status now</a>',true);
    let body=message?'<p role="alert">'+esc(message)+'</p>':'';
    if (checkOnly && !linked) return html(body+'<p>Check the existing sign-in before choosing to start again.</p>'+
      form('refresh')+button('Check existing sign-in')+form('restart')+button('Start a fresh sign-in'));
    if(linked) {
      body+='<p>LinkedIn is connected to Beeper on this computer.</p>';
      if(registered)body+='<p><a href="'+SETTINGS+'">Open Growth HQ and enable your LinkedIn inbox</a>. Choose the default business and save the account.</p>';
      else body+=form('refresh')+button('Register this connection with Growth HQ');
    } else if(!session || ['failed','cancelled'].includes(session.status)) {
      body+='<p>Use the LinkedIn connection in Beeper on this computer. Beeper will guide you through sign-in and any LinkedIn verification.</p>'+
        form('start')+button('Start on-device sign-in');
    } else if(session.currentStep?.type==='user_input' && session.status==='waiting_for_input') {
      const step=session.currentStep;
      body+='<p>These fields go directly through the local connector to the Beeper LinkedIn bridge on this PC. Growth HQ does not receive your sign-in details.</p>'+
        '<p>'+esc(step.instructions)+'</p>'+form('submit');
      for(const [i, field] of step.fields.entries()) {
        const name=[field.id,field.type,field.label].join(' ').toLowerCase();
        const label=/identifier|username|email|phone/.test(name)?'LinkedIn email or phone':
          /password/.test(name)?'LinkedIn password':/code|otp|verification/.test(name)?'Verification code':field.label||field.id;
        const sensitive=/password|secret|code|otp|verification/.test(name);
        body+='<label>'+esc(label)+'<input name="f'+i+'" type="'+(sensitive?'password':'text')+'" autocomplete="off" maxlength="2000"'+(field.optional?'':' required')+'></label>';
      }
      body+=button('Continue with Beeper');
    } else if(session.status==='waiting_for_display' && session.currentStep?.type==='display_and_wait') {
      const display=session.currentStep.display;
      body+='<p>'+esc(session.currentStep.instructions || 'Complete verification in LinkedIn, then continue here.')+'</p>';
      if(display?.type==='code')body+='<p><strong>'+esc(display.data)+'</strong></p>';
      body+=form('display')+button('I completed the LinkedIn verification');
    } else {
      body+='<p>'+esc(session.currentStep?.instructions || 'Complete the network verification, then check again.')+'</p>'+
        form('refresh')+button('Check connection');
    }
    return html(body);
  }
  async function reconcile() {
    linked=(await linkedAccounts()).length>0;
    if(linked){
      await discovered();
      registered=true;
      session=null;
    }
  }
  async function act(action, fields) {
    if(action==='restart') {
      if(!checkOnly)throw Error('stale_step');
      await reconcile();
      if(linked)return;
      session=null;checkOnly=false;action='start';
    }
    if(action==='start'){
      if(session && !['failed','cancelled'].includes(session.status))throw Error('stale_step');
      await reconcile();
      if(linked)return;
      const bridge=await engine.request(BASE);
      if(bridge.id!==BRIDGE || bridge.provider!=='local' || bridge.status!=='available')throw Error('bridge_unavailable');
      const flows=await engine.request(BASE+'/login-flows');
      if(!flows.items?.some(f=>f.id==='password'))throw Error('flow_unavailable');
      session=null;
      update(await engine.request(BASE+'/login-sessions',{method:'POST',body:{flowID:'password'}}));
    }else if(action==='submit'){
      const step=session?.currentStep;
      if(session?.status!=='waiting_for_input'||step?.type!=='user_input'||!Array.isArray(step.fields)||
         !step.fields.length||step.fields.length>8)throw Error('stale_step');
      const values=Object.create(null);
      for(const [i,field] of step.fields.entries()){
        const value=fields.get('f'+i);
        if(typeof field.id!=='string' || typeof value!=='string' || value.length>2000 ||
           (!field.optional&&!value))throw Error('invalid_form');
        values[field.id]=value;
      }
      update(await engine.request(BASE+'/login-sessions/'+part(session.loginSessionID)+'/steps/'+part(step.stepID),
        {method:'POST',timeoutMs:45000,body:{type:'user_input',source:'api',fields:values}}));
    }else if(action==='display'){
      const step=session?.currentStep;
      if(session?.status!=='waiting_for_display'||step?.type!=='display_and_wait')throw Error('stale_step');
      update(await engine.request(BASE+'/login-sessions/'+part(session.loginSessionID)+'/steps/'+part(step.stepID),
        {method:'POST',timeoutMs:45000,body:{type:'display_and_wait',source:'api'}}));
    }else if(action==='refresh'){
      const before=session?.currentStep?.stepID;
      if(session)update(await engine.request(BASE+'/login-sessions/'+part(session.loginSessionID)));
      if(session?.currentStep?.stepID!==before || ['failed','cancelled','complete'].includes(session?.status))checkOnly=false;
      await reconcile();
      return;
    }else throw Error('invalid_form');
    if(session?.status==='complete')await reconcile();
  }
  const server=http.createServer(async(req,res)=>{
    const reply=(status,body)=>{res.writeHead(status,{'content-type':'text/html; charset=utf-8','cache-control':'no-store',
      'content-security-policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
      'referrer-policy':'no-referrer','x-content-type-options':'nosniff'});res.end(body);};
    if(!origin||req.headers.host!==new URL(origin).host)return reply(404,html('<p>Page unavailable.</p>'));
    if(req.method==='GET' && req.url===path+'/status') {
      res.writeHead(200,{'content-type':'application/json','cache-control':'no-store','x-content-type-options':'nosniff'});
      return res.end(JSON.stringify({phase:busy?'checking':phase,connected:linked,registered,
        session:session?.status||null,step:session?.currentStep?.type||null,manualCheck:checkOnly}));
    }
    if(req.url!==path)return reply(404,html('<p>Page unavailable.</p>'));
    if(req.method==='GET')return reply(200,view());
    if(req.method!=='POST'||req.headers.origin!==origin)return reply(403,html('<p>Use the local setup tab.</p>'));
    let fields;
    try{fields=await readForm(req);}catch{return reply(400,html('<p>Invalid setup form.</p>'));}
    if(busy||fields.get('token')!==token)return reply(409,view('The step changed. Use the current form.'));
    const action=fields.get('action');
    if(!['start','submit','refresh','display','restart'].includes(action) || (checkOnly && !['refresh','restart'].includes(action)))
      return reply(409,view('Use the current sign-in step.'));
    busy=true;token=nonce();note='';phase='checking';
    // Respond before provider IO so the owner sees a bounded status page. Never replay a credentials POST.
    void (async()=>{
      try {
        await act(action,fields);phase=linked?'connected':'ready';
      } catch {
        phase='needs_attention';
        if (action==='submit'||action==='display') {
          checkOnly=true;
          try { await act('refresh',new URLSearchParams()); } catch { /* Use the explicit existing-session check. */ }
          if(checkOnly && !linked)note='Beeper has not confirmed this step. Check the existing sign-in to see whether LinkedIn accepted it.';
        } else {
          if(action==='refresh' && session)checkOnly=true;
          note='Beeper could not complete the check. You can check again.';
        }
      } finally {busy=false;}
    })();
    res.writeHead(303,{location:path,'cache-control':'no-store'});res.end();
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(port,'127.0.0.1',resolve);});
  origin='http://127.0.0.1:'+server.address().port;
  return {server,url:origin+path};
}

module.exports={createSetup,PORT};
if(require.main===module)(async()=>{
  const directory=process.env.GHQ_PERSONAL_DATA_DIR;
  if(!directory)throw Error('missing_directory');
  const state=await openState(directory);
  if(!state.state.creds.token)throw Error('first_approve_beeper');
  const engine=new EngineClient(state.state.creds.token);
  const {GrowthHqClient}=require('./client.cjs');
  const {discoverAccounts,cycle}=require('./runtime.cjs');
  const ghq=new GrowthHqClient(process.env.GHQ_INBOX_PERSONAL_SECRET);
  // Keep the LinkedIn outbox journal separate from the older account-wide connector.
  const journal=await openState(require('node:path').join(directory,'linkedin-deliveries'));
  let syncing=false, active=false;
  async function sync(){
    if(syncing || !active)return;
    syncing=true;
    try{await cycle(engine,ghq,journal,'linkedin');}
    catch{console.error('LinkedIn connection needs attention.');}
    finally{syncing=false;}
  }
  const setup=await createSetup(engine,{discovered:async()=>{
    await discoverAccounts(engine,ghq);
    active=true;
  }});
  active=(await engine.accounts()).some(a=>a.bridge?.id===BRIDGE && a.bridge?.provider==='local' &&
    ['connected','backfilling'].includes(a.status));
  const timer=setInterval(sync,20000);
  console.log('Private LinkedIn setup is ready on loopback.');
  const stop=()=>{clearInterval(timer);setup.server.close(()=>process.exit(0));};
  process.on('SIGINT',stop);process.on('SIGTERM',stop);
})().catch(()=>{console.error('LinkedIn setup unavailable. Check the local connector.');process.exitCode=1;});
