'use strict';
const http=require('node:http'),crypto=require('node:crypto'),fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('playwright-core');
const allowedUrl=value=>{try{const u=new URL(value);return u.protocol==='https:'&&['www.facebook.com','facebook.com','m.facebook.com'].includes(u.hostname);}catch{return false;}};
function validateAction(data){
  if(!data||typeof data!=='object')throw Error('invalid_action');
  if(data.action==='click'&&Number.isFinite(data.x)&&Number.isFinite(data.y)&&data.x>=0&&data.x<1200&&data.y>=0&&data.y<850)return data;
  if(data.action==='type'&&typeof data.text==='string'&&data.text.length>0&&data.text.length<=2000)return data;
  if(data.action==='key'&&['Tab','Shift+Tab','Enter','Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Escape','Control+A'].includes(data.key))return data;
  if(data.action==='scroll'&&Number.isFinite(data.y)&&Math.abs(data.y)<=850)return data;
  if(['done','resume','pause'].includes(data.action))return data;
  throw Error('invalid_action');
}
async function body(req){let bytes=0,chunks=[];for await(const c of req){bytes+=c.length;if(bytes>10000)throw Error('too_large');chunks.push(c);}return JSON.parse(Buffer.concat(chunks).toString());}
const html=`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Connect Messenger to Growth HQ</title>
<style>body{font:17px system-ui;background:#f6f4ef;color:#17251e;margin:20px}main{max-width:1220px;margin:auto}h1{margin-bottom:8px}button{background:#e8a33d;border:0;padding:12px 18px;border-radius:8px;font-size:16px;cursor:pointer}input{font-size:17px;padding:12px;min-width:300px}#view{width:100%;max-width:1200px;border:1px solid #bbb;background:white;cursor:pointer;outline:3px solid transparent}#view:focus{outline-color:#e8a33d}.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0}#notice{min-height:25px;font-weight:600}</style>
<main><h1>Connect Messenger to Growth HQ</h1><p>This is your separate Facebook browser, running on this PC. Your normal browser stays untouched.</p>
<p>Click a field in the Facebook screen, then type normally. For pasting a password or PIN, use the private text box below and press <b>Enter into selected field</b>. Only Facebook receives the entry; Growth HQ does not store it.</p>
<div class="bar"><input id="entry" type="password" autocomplete="off" aria-label="Private text for selected Facebook field"><button id="insert">Enter into selected field</button><button id="tab">Next field</button><button id="enter">Enter</button><button id="done">Done signing in</button><button id="pause">Pause connector</button><button id="resume">Resume connector</button></div>
<div id="notice" role="status">Loading your isolated browser…</div><p id="address"></p><img id="view" tabindex="0" aria-label="Your isolated Facebook browser" alt="Your isolated Facebook browser"><p>Complete Facebook checks yourself. You can return here if Facebook needs your attention. Keep this PC awake for syncing.</p></main>
<script src="./client.js"></script></html>`;
const client=`const base=location.pathname.replace(/\\/$/,'');const view=document.querySelector('#view'),notice=document.querySelector('#notice');let refreshing=false;async function refresh(){if(refreshing)return;refreshing=true;try{const s=await fetch(base+'/status',{cache:'no-store'}).then(r=>r.json());notice.textContent=s.message;document.querySelector('#address').textContent=s.location;const r=await fetch(base+'/frame',{cache:'no-store'});if(r.ok){const old=view.src;view.src=URL.createObjectURL(await r.blob());if(old.startsWith('blob:'))URL.revokeObjectURL(old);}}catch{notice.textContent='Connector stopped. Reopen it before continuing.';}finally{refreshing=false;}}let chain=Promise.resolve();function act(data){chain=chain.then(async()=>{const r=await fetch(base+'/action',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});if(!r.ok)notice.textContent='That action could not be completed. Check the Facebook screen.';await refresh();}).catch(()=>{notice.textContent='Connection interrupted.';});return chain;}view.onclick=e=>{const r=view.getBoundingClientRect();act({action:'click',x:(e.clientX-r.left)*1200/r.width,y:(e.clientY-r.top)*850/r.height});view.focus();};view.onkeydown=e=>{if(e.ctrlKey||e.metaKey){if(e.key.toLowerCase()==='a'){e.preventDefault();act({action:'key',key:'Control+A'});}return;}if(e.key.length===1){e.preventDefault();act({action:'type',text:e.key});}else if(['Tab','Enter','Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Escape'].includes(e.key)){e.preventDefault();act({action:'key',key:e.shiftKey&&e.key==='Tab'?'Shift+Tab':e.key});}};view.onwheel=e=>{e.preventDefault();act({action:'scroll',y:Math.max(-850,Math.min(850,e.deltaY))});};document.querySelector('#insert').onclick=()=>{const input=document.querySelector('#entry');const text=input.value;input.value='';if(text)act({action:'type',text});};document.querySelector('#tab').onclick=()=>act({action:'key',key:'Tab'});document.querySelector('#enter').onclick=()=>act({action:'key',key:'Enter'});document.querySelector('#done').onclick=()=>act({action:'done'});document.querySelector('#pause').onclick=()=>act({action:'pause'});document.querySelector('#resume').onclick=()=>act({action:'resume'});document.addEventListener('paste',e=>{if(document.activeElement===view){const text=e.clipboardData.getData('text/plain');if(text){e.preventDefault();act({action:'type',text});}}});setInterval(refresh,1500);refresh();`;

async function start({directory,executablePath,onReady}={}){
  if(!directory||!path.isAbsolute(directory))throw Error('private_directory_required');
  await fs.mkdir(directory,{recursive:true});
  const context=await chromium.launchPersistentContext(path.join(directory,'profile'),{executablePath,channel:executablePath?undefined:'msedge',headless:true,chromiumSandbox:true,
    viewport:{width:1200,height:850},locale:'en-GB',timezoneId:'Europe/London',acceptDownloads:false});
  const page=context.pages()[0]||await context.newPage();
  // No stealth flags, security bypass, imported browser sessions or exposed CDP.
  await context.route('**/*',async route=>{
    const req=route.request();if(req.isNavigationRequest()&&!req.frame().parentFrame()&&!allowedUrl(req.url()))return route.abort();
    return route.continue();
  });
  let mode='login',message='Sign into Facebook in the screen below, then click Done signing in.',identity=null,busy=false,started=false;
  await page.goto('https://www.facebook.com/messages/',{waitUntil:'domcontentloaded',timeout:45000});
  const capability=crypto.randomBytes(32).toString('base64url');let origin,root;
  const server=http.createServer(async(req,res)=>{
    res.setHeader('Cache-Control','no-store');res.setHeader('Referrer-Policy','no-referrer');res.setHeader('X-Content-Type-Options','nosniff');
    res.setHeader('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'");
    if(req.headers.host!==new URL(origin).host||!req.url.startsWith(root)){res.writeHead(404).end();return;}
    const suffix=req.url.slice(root.length);try{
      if(req.method==='GET'&&['','/'].includes(suffix)){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);return;}
      if(req.method==='GET'&&suffix==='/client.js'){res.setHeader('Content-Type','application/javascript; charset=utf-8');res.end(client);return;}
      if(req.method==='GET'&&suffix==='/status'){res.setHeader('Content-Type','application/json');const u=new URL(page.url());res.end(JSON.stringify({mode,message,location:u.origin+u.pathname,accountVerified:!!identity}));return;}
      if(req.method==='GET'&&suffix==='/frame'){if(!allowedUrl(page.url()))throw Error('blocked_navigation');res.setHeader('Content-Type','image/jpeg');res.end(await page.screenshot({type:'jpeg',quality:75}));return;}
      if(req.method!=='POST'||suffix!=='/action'||req.headers.origin!==origin||!String(req.headers['content-type']).startsWith('application/json')){res.writeHead(403).end();return;}
      if(busy){res.writeHead(409).end();return;}
      const data=validateAction(await body(req));if(!allowedUrl(page.url()))throw Error('blocked_navigation');
      if(data.action==='pause'){mode='paused';message='Connector paused. An already-started send may finish; no subsequent send will start.';}
      else if(data.action==='resume'){if(!identity)throw Error('sign_in_first');mode='ready';message='Connector resumed.';}
      else if(data.action==='done'){
        busy=true;try{
          await page.goto('https://www.facebook.com/me/',{waitUntil:'domcontentloaded',timeout:30000});
          await page.getByRole('button',{name:'Edit profile',exact:true}).or(page.getByRole('link',{name:'Edit profile',exact:true})).waitFor({timeout:15000});
          const ids=await page.locator('a[href]').evaluateAll(links=>links.map(a=>{try{const u=new URL(a.href);return u.hostname==='www.facebook.com'&&u.searchParams.has('fb_profile_edit_entry_point')?u.searchParams.get('id'):null;}catch{return null;}}).filter(Boolean));
          if(new Set(ids).size!==1||!/^\d+$/.test(ids[0]))throw Error('identity_unconfirmed');
          identity={userId:ids[0],profileUrl:page.url()};await fs.writeFile(path.join(directory,'identity.json'),JSON.stringify(identity),{mode:0o600});
          await page.goto('https://www.facebook.com/messages/',{waitUntil:'domcontentloaded'});
          mode='ready';message='Facebook account verified. Complete any Messenger history PIN prompt shown below.';
          if(onReady&&!started){started=true;onReady({context,page,identity,setStatus:(m)=>{message=m;},isPaused:()=>mode==='paused'}).catch(()=>{mode='paused';message='Connector needs attention. Your Facebook session is preserved.';});}
        }catch{mode='login';message='Facebook sign-in or identity check is incomplete. Finish any Facebook check below, then try Done signing in again.';}finally{busy=false;}
      }else if(data.action==='click')await page.mouse.click(data.x,data.y);
      else if(data.action==='type')await page.keyboard.insertText(data.text);
      else if(data.action==='key')await page.keyboard.press(data.key);
      else if(data.action==='scroll')await page.mouse.wheel(0,data.y);
      res.setHeader('Content-Type','application/json');res.end('{"ok":true}');
    }catch{res.writeHead(400).end('{"error":"action_incomplete"}');}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));origin=`http://127.0.0.1:${server.address().port}`;root='/'+capability;
  await fs.writeFile(path.join(directory,'connection.json'),JSON.stringify({url:origin+root+'/',pid:process.pid}),{mode:0o600});
  return {context,page,server,close:async()=>{server.close();await context.close();}};
}
module.exports={start,validateAction,allowedUrl,client};
if(require.main===module)start({directory:process.env.GHQ_NATIVE_DATA_DIR,executablePath:process.env.GHQ_NATIVE_BROWSER_PATH,
  onReady:process.env.GHQ_NATIVE_CONFIG?async info=>{
    const config=JSON.parse(await fs.readFile(process.env.GHQ_NATIVE_CONFIG,'utf8'));
    if(info.identity.userId!==config.ownerUserId)throw Error('wrong_account');
    return require('./runtime.cjs').run({...info,directory:process.env.GHQ_NATIVE_DATA_DIR,secret:process.env.GHQ_INBOX_PERSONAL_SECRET,threads:config.threads});
  }:undefined}).then(instance=>{
  console.log('Growth HQ isolated Messenger login ready. Connection address saved privately.');
  for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>instance.close().finally(()=>process.exit()));
}).catch(()=>{console.error('Isolated Messenger browser could not start. No existing browser was touched.');process.exitCode=1;});
