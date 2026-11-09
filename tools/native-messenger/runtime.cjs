'use strict';
const fs=require('node:fs/promises'),path=require('node:path');
const {readConversation,normalise}=require('./reader.cjs');
const {GrowthHqClient}=require('../personal-inbox/client.cjs');
const {openAuthState}=require('../whatsapp-keywords/socket-auth.cjs');
const {discover,validateTargets,AvailableDiscovery}=require('./discovery.cjs');
const CONNECTOR='native-messenger';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

// Channel-independent boundary: verifyIdentity(), observe(thread), sendOnce(job).
// Other adapters must advertise their own actual history/receipt capabilities.
class MessengerAdapter {
  constructor(context,identity,threads){this.context=context;this.identity=identity;this.threads=threads;this.readPage=null;this.identityPage=null;}
  async verifyIdentity(){
    this.identityPage??=await this.context.newPage();
    await this.identityPage.goto('https://www.facebook.com/me/',{waitUntil:'domcontentloaded',timeout:30000});
    const edit=this.identityPage.locator('a[href*="fb_profile_edit_entry_point"]');
    await edit.filter({hasText:'Edit profile'}).first().waitFor({timeout:30000});
    const ids=await edit.evaluateAll(links=>links.map(e=>new URL(e.href).searchParams.get('id')).filter(Boolean));
    if(!ids.length||ids.some(id=>id!==this.identity.userId))throw Error('account_changed');
  }
  target(threadId){const t=this.threads.find(t=>t.threadId===threadId);if(!t)throw Error('thread_not_authorised');return t;}
  async page(threadId){
    const target=this.target(threadId);let page=this.readPage;
    if(!page||page.isClosed()){page=await this.context.newPage();this.readPage=page;}
    const expected=`https://www.facebook.com/messages/${target.threadKind==='page'?'':'e2ee/'}t/${target.threadId}`;
    if(page.url().replace(/\/$/,'')!==expected)await page.goto(expected,{waitUntil:'domcontentloaded',timeout:30000});
    // Messenger renders the log before its participant header on reload.
    // Wait for both native identity signals; the collector still validates
    // every profile ID and message instead of accepting a partial header.
    await page.waitForFunction(expected=>{
      const log=[...document.querySelectorAll('[role="log"]')].find(e=>e.getAttribute('aria-label')===`Messages in conversation with ${expected.name}`);
      const profile=[...document.querySelectorAll('a[href]')].some(e=>{
        if(!e.textContent.trim().startsWith(expected.name))return false;
        try{const u=new URL(e.href);return u.origin==='https://www.facebook.com'&&u.pathname.replace(/\/$/,'')==='/'+expected.participantId;}catch{return false;}
      });
      return !!(log&&profile&&log.querySelector('[data-message-id]'));
    },target,{timeout:30000});
    return page;
  }
  async observe(threadId,binding){const page=await this.page(threadId);return normalise(await page.evaluate(readConversation,this.target(threadId)),this.target(threadId),binding);}
  async sendOnce(job,binding,before){
    if(job.account_id!==binding.account_id||job.user_id!==binding.user_id||job.channel!=='facebook')throw Error('wrong_send_account');
    const target=this.target(job.thread_id);await this.verifyIdentity();
    const page=await this.page(job.thread_id);
    const current=await this.observe(job.thread_id,binding);
    if(!current.evidence.composer_ready)throw Error('composer_not_ready');
    const composer=page.getByRole('textbox',{name:`Write to ${target.name}`,exact:true});
    if((await composer.innerText()).trim())throw Error('existing_draft');
    // Exactly one explicit send. The caller has durably recorded begun first.
    await composer.fill(job.body);await composer.press('Enter');
    let found;
    for(let attempt=0;attempt<12;attempt++){
      await sleep(1000);let observed;try{observed=await this.observe(job.thread_id,binding);}catch{continue;}
      const matches=observed.thread.messages.filter(m=>!before.has(m.id)&&m.direction==='outgoing'&&m.body===job.body);
      if(matches.length>1)throw Error('send_outcome_unknown');
      if(matches.length===1){found=matches[0];break;}
    }
    if(!found)throw Error('send_outcome_unknown');
    // A reload must still show the exact native message. This is persistence
    // evidence, not a claim that the recipient has read or received it.
    await page.reload({waitUntil:'domcontentloaded'});await this.page(job.thread_id);
    const persisted=await this.observe(job.thread_id,binding);
    if(!persisted.thread.messages.some(m=>m.id===found.id&&m.body===job.body&&m.direction==='outgoing'))throw Error('send_outcome_unknown');
    return found.id;
  }
}

async function dispatch(adapter,cloud,state,binding,id,isPaused=()=>false){
  const journal=state.state.creds.deliveries;
  if(journal[id]||isPaused())return;
  const job=await cloud.request(`replies/${id}/begin`,{connector_id:CONNECTOR});
  if(job.dispatch!==true)return;
  journal[id]={state:'begun',threadId:job.thread_id};await state.saveCreds();
  try{
    const before=await adapter.observe(job.thread_id,binding);
    const nativeId=await adapter.sendOnce(job,binding,new Set(before.thread.messages.map(m=>m.id)));
    journal[id]={state:'settling',result:{connector_id:CONNECTOR,state:'accepted',provider_message_id:nativeId}};
  }catch{
    journal[id]={state:'settling',result:{connector_id:CONNECTOR,state:'outcome_unknown'}};
  }
  await state.saveCreds();await settle(cloud,state);
}
async function settle(cloud,state){
  for(const [id,entry] of Object.entries(state.state.creds.deliveries)){
    if(entry.state==='settled')continue;
    if(entry.state==='begun'){entry.state='settling';entry.result={connector_id:CONNECTOR,state:'outcome_unknown'};await state.saveCreds();}
    await cloud.request(`replies/${id}/settle`,entry.result);entry.state='settled';await state.saveCreds();
  }
}
function nextBatch(threads,offset,size=20){
  if(!Number.isInteger(size)||size<1||size>20)throw Error('invalid_sync_batch');
  if(!threads.length)return {threads:[],next:0};
  const start=offset%threads.length,count=Math.min(size,threads.length);
  return {threads:Array.from({length:count},(_,i)=>threads[(start+i)%threads.length]),next:(start+count)%threads.length};
}
async function seedExisting(cloud,identityPacket,threads){
  const count=Math.max(1,Math.ceil(threads.length/100));
  for(let batch=0;batch<count;batch++)await cloud.request('candidates',{...identityPacket,
    candidates:threads.slice(batch*100,(batch+1)*100),seed_existing:batch===count-1,
    query:'Existing imported conversations',complete:false,
    note:'Retained existing named conversations. Available-chat discovery is controlled by the owner in Growth HQ.'});
}
async function run({context,identity,directory,setStatus,isPaused,secret,threads}){
  validateTargets(threads);
  const adapter=new MessengerAdapter(context,identity,threads),cloud=new GrowthHqClient(secret);
  const available=new AvailableDiscovery(context);let syncOffset=0;
  const state=await openAuthState(path.join(directory,'journal'),{BufferJSON:{},initAuthCreds:()=>({deliveries:{}})});
  let stopped=false,searching=null;context.on('close',()=>{stopped=true;});
  const statusFile=path.join(directory,'runtime-status.json');
  while(!stopped){
    if(isPaused()){await sleep(2000);continue;}
    try{
      await adapter.verifyIdentity();
      const accountId=`facebook-web:${identity.userId}`;
      const configured=await cloud.request('accounts',{connector_id:CONNECTOR,accounts:[{account_id:accountId,user_id:identity.userId,
        channel:'facebook',engine_provider:'growth-hq-browser',status:'connected',label:'Martin Howard · Messenger',can_reply:true}]});
      const binding={...configured.accounts.find(a=>a.account_id===accountId),timeZone:'Europe/London'};
      if(!binding.key)throw Error('account_not_discovered');
      if(!binding.enabled){setStatus('Facebook connected. Choose Key Properties UK and enable this account in Growth HQ → Inbox → Connections.');await sleep(10000);continue;}
      await settle(cloud,state);
      const identityPacket={connector_id:CONNECTOR,account_key:binding.key,account_id:binding.account_id,user_id:binding.user_id};
      let selection=await cloud.request('selections',identityPacket);
      if(!selection.initialised){
        await seedExisting(cloud,identityPacket,threads);
        selection=await cloud.request('selections',identityPacket);
      }
      if(selection.sync_all===true&&!searching){
        const result=await available.scan(selection.threads);
        if(result){await cloud.request('candidates',{...identityPacket,...result,query:'All available private chats'});
          selection=await cloud.request('selections',identityPacket);}
      }else if(selection.sync_all!==true){await available.close();}
      adapter.threads=validateTargets(selection.threads);
      if(selection.request&&!selection.request.done&&!searching){
        const request=selection.request;
        searching=discover(context,request.query).then(result=>cloud.request('candidates',{...identityPacket,...result,query:request.query,request_id:request.id}))
          .catch(()=>cloud.request('candidates',{...identityPacket,candidates:[],query:request.query,request_id:request.id,complete:false,
            note:'Messenger search could not finish. Check the connection and try again.'}).catch(()=>{})).finally(()=>{searching=null;});
      }
      const counts=[];
      await adapter.verifyIdentity(); // Recheck the owner after discovery, before ingesting any body.
      const batch=nextBatch(adapter.threads,syncOffset);syncOffset=batch.next;
      // One unavailable conversation must not stop all other selected inboxes.
      for(const target of batch.threads){
        if(isPaused())break;
        try{const packet=await adapter.observe(target.threadId,binding);if(!packet){counts.push({threadId:target.threadId,count:0,skipped:'no_supported_text'});continue;}
          await cloud.request('threads',{connector_id:CONNECTOR,...packet});counts.push({threadId:target.threadId,count:packet.thread.messages.length,hasMore:packet.thread.has_more});}
        catch{counts.push({threadId:target.threadId,count:0,skipped:'conversation_needs_attention'});}
      }
      if(isPaused())continue;
      const pending=await cloud.request('pending',{connector_id:CONNECTOR});
      for(const id of pending.reply_ids??[]){if(isPaused())break;await dispatch(adapter,cloud,state,binding,id,isPaused);}
      const result={at:new Date().toISOString(),state:'connected',syncAll:selection.sync_all===true,
        selectedCount:adapter.threads.length,batchCount:batch.threads.length,counts};await fs.writeFile(statusFile,JSON.stringify(result),{mode:0o600});
      setStatus(selection.sync_all===true?'Connected. Available private one-to-one chats are being discovered and synced in batches. Personal chats stay personal.'
        :'Connected. Your selected conversations are syncing to Growth HQ. Replies use this Facebook account.');
    }catch(error){
      const reason=/^[a-z_]+$/.test(error.message)?error.message:'connection_needs_attention';
      await fs.writeFile(statusFile,JSON.stringify({at:new Date().toISOString(),state:'attention_required',reason}),{mode:0o600});
      await cloud.request('accounts',{connector_id:CONNECTOR,accounts:[{account_id:`facebook-web:${identity.userId}`,user_id:identity.userId,
        channel:'facebook',engine_provider:'growth-hq-browser',status:'attention_required',label:'Martin Howard · Messenger',can_reply:true}]}).catch(()=>{});
      setStatus('Connection needs attention. No unknown send will be retried. Check Facebook below or the Growth HQ connection settings.');
    }
    await sleep(20000);
  }
  await available.close();
  await state.flush();
}
module.exports={MessengerAdapter,dispatch,settle,nextBatch,seedExisting,run};
