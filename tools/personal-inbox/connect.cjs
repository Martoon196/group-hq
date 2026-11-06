// Local OAuth PKCE handoff. The engine token never leaves this computer.
const http=require('node:http');const crypto=require('node:crypto');const fs=require('node:fs/promises');
const {openState}=require('./runtime.cjs');
const {ENGINE}=require('./client.cjs');
const nonce=()=>crypto.randomBytes(32).toString('base64url');
async function engineJson(path,body,form=false,fetcher=fetch){
  const r=await fetcher(ENGINE+path,{method:body===undefined?'GET':'POST',redirect:'error',signal:AbortSignal.timeout(10000),
    headers:{'content-type':form?'application/x-www-form-urlencoded':'application/json'},
    body:body===undefined?undefined:form?new URLSearchParams(body).toString():JSON.stringify(body)});
  if(!r.ok)throw Error('engine_authorisation_failed');return r.json();
}
function page(message,link){return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Connect personal inboxes</title><style>body{font:20px system-ui;max-width:680px;margin:8vh auto;padding:24px;color:#142019;background:#fff9ef}a{display:inline-block;padding:16px;background:#e8a33d;color:#142019;border-radius:8px;font-weight:bold}</style><h1>Connect personal inboxes to Growth HQ</h1><p>'+message+'</p>'+(link?'<p><a href="'+link.replaceAll('&','&amp;')+'">Approve local connection</a></p>':'')+'<p>Your personal-account token stays encrypted on this PC. Keep Beeper and the Growth HQ connector running to read and reply from Growth HQ.</p></html>';}
async function createWizard(directory,{fetcher=fetch}={}){
  const state=await openState(directory),capability=nonce(),verifier=nonce(),oauthState=nonce();
  const challenge=crypto.createHash('sha256').update(verifier).digest('base64url');
  let origin,callback,authorise,clientId,claimed=false,complete=false;
  const server=http.createServer(async(req,res)=>{
    const reply=(status,html)=>{res.writeHead(status,{'content-type':'text/html; charset=utf-8','cache-control':'no-store',
      'content-security-policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",'referrer-policy':'no-referrer'});res.end(html);};
    if(req.headers.host!==new URL(origin).host||req.method!=='GET')return reply(400,page('Use the local connection page.'));
    const url=new URL(req.url,origin);
    if(url.pathname==='/'+capability){return reply(200,page(complete?'Connected. Return to Growth HQ → Inbox → Connect personal accounts.':'Sign into Beeper and connect your Facebook Messenger first. Then approve this local read/reply connection.',complete?null:authorise));}
    if(url.pathname!=='/callback')return reply(404,page('Page not found.'));
    if(claimed||url.searchParams.get('state')!==oauthState||!url.searchParams.get('code'))return reply(400,page('This authorisation was not completed. Reopen the connection page.'));
    claimed=true;
    try{
      const result=await engineJson('/oauth/token',{grant_type:'authorization_code',code:url.searchParams.get('code'),client_id:clientId,
        redirect_uri:callback,code_verifier:verifier},true,fetcher);
      if(typeof result.access_token!=='string'||result.token_type?.toLowerCase()!=='bearer'||result.access_token.length>16000)throw Error('invalid_token');
      state.state.creds.token=result.access_token;await state.saveCreds();complete=true;
      reply(200,page('Connected. Your local inbox engine is authorised. Return to Growth HQ → Inbox → Connect personal accounts.'));
    }catch{reply(502,page('The local engine could not complete authorisation. No token was displayed or sent to Growth HQ. Restart the connection helper to try again.'));}
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  origin='http://127.0.0.1:'+server.address().port;callback=origin+'/callback';
  try{
    const metadata=await engineJson('/.well-known/oauth-authorization-server',undefined,false,fetcher);
    for(const [key,path] of [['authorization_endpoint','/oauth/authorize'],['token_endpoint','/oauth/token'],['registration_endpoint','/oauth/register']]){
      const u=new URL(metadata[key]);if(!['localhost','127.0.0.1'].includes(u.hostname)||u.port!=='23373'||u.protocol!=='http:'||u.pathname!==path)throw Error('unexpected_engine_metadata');
    }
    const registration=await engineJson('/oauth/register',{client_name:'Growth HQ personal inbox',redirect_uris:[callback],
      grant_types:['authorization_code'],response_types:['code'],token_endpoint_auth_method:'none',scope:'read write'},false,fetcher);
    clientId=registration.client_id;if(typeof clientId!=='string'||clientId.length>1000)throw Error('invalid_client');
    const u=new URL(ENGINE+'/oauth/authorize');
    u.search=new URLSearchParams({response_type:'code',client_id:clientId,redirect_uri:callback,scope:'read write',state:oauthState,
      code_challenge:challenge,code_challenge_method:'S256'}).toString();authorise=u.toString();
  }catch(error){await new Promise(resolve=>server.close(resolve));throw error;}
  return {server,url:origin+'/'+capability,connected:()=>complete};
}
module.exports={createWizard,engineJson};
if(require.main===module)(async()=>{
  const directory=process.env.GHQ_PERSONAL_DATA_DIR;if(!directory)throw Error('missing_private_data_directory');
  const wizard=await createWizard(directory);const file=process.env.GHQ_PERSONAL_WIZARD_FILE;
  if(file)await fs.writeFile(file,JSON.stringify({url:wizard.url}),{mode:0o600});
  console.log('Growth HQ local connection page is ready.');
  const stop=()=>wizard.server.close(()=>process.exit(0));process.on('SIGINT',stop);process.on('SIGTERM',stop);
})().catch(()=>{console.error('Keep Beeper open, then restart the local connection helper.');process.exitCode=1;});
