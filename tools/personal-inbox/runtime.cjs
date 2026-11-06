const {openAuthState}=require('../whatsapp-keywords/socket-auth.cjs');
const {EngineClient,GrowthHqClient,snapshot,channel}=require('./client.cjs');
async function openState(directory){return openAuthState(directory,{BufferJSON:{},initAuthCreds:()=>({token:null,deliveries:{}})});}
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));

async function dispatch(engine,cloud,state,replyId){
  const saved=state.state.creds.deliveries;
  if(saved[replyId])return;
  const job=await cloud.request('replies/'+replyId+'/begin',{});
  if(job.dispatch!==true)return;
  const binding={account_id:job.account_id,user_id:job.user_id,channel:job.channel};
  // Persist intent BEFORE the non-idempotent desktop send. Never repeat a begun send.
  saved[replyId]={state:'begun',binding,chatId:job.thread_id};await state.saveCreds();
  try{
    const pendingId=await engine.send(binding,job.thread_id,job.body);
    saved[replyId]={state:'pending',binding,chatId:job.thread_id,pendingId};await state.saveCreds();
    await cloud.request('replies/'+replyId+'/settle',{state:'bridge_accepted',provider_message_id:pendingId});
  }catch(error){
    const entry=saved[replyId];
    if(entry.state==='pending')return; // We have an acknowledgement; retry settlement/status, never POST again.
    entry.state='unknown';await state.saveCreds();
    await cloud.request('replies/'+replyId+'/settle',{state:'outcome_unknown'}).catch(()=>{});
  }
}
async function settlePending(engine,cloud,state){
  for(const [id,entry] of Object.entries(state.state.creds.deliveries)){
    if(entry.state==='settled')continue;
    if(entry.state==='settling'){
      await cloud.request('replies/'+id+'/settle',entry.result);entry.state='settled';await state.saveCreds();continue;
    }
    if(entry.state==='begun'||entry.state==='unknown'){
      await cloud.request('replies/'+id+'/settle',{state:'outcome_unknown'});entry.state='settled';await state.saveCreds();continue;
    }
    if(entry.state!=='pending')continue;
    await cloud.request('replies/'+id+'/settle',{state:'bridge_accepted',provider_message_id:entry.pendingId});
    let outcome;try{outcome=await engine.outcome(entry.binding,entry.chatId,entry.pendingId);}catch{continue;}
    if(!['SUCCESS','FAIL_PERMANENT'].includes(outcome.status))continue;
    const result={state:outcome.status==='SUCCESS'?'accepted':'rejected',provider_message_id:outcome.id,previous_message_id:entry.pendingId};
    // Persist the final result before settling. A lost response must replay this
    // exact result, never regress the cloud receipt to the temporary message ID.
    entry.state='settling';entry.result=result;await state.saveCreds();
    await cloud.request('replies/'+id+'/settle',result);
    entry.state='settled';await state.saveCreds();
  }
}

async function cycle(engine,cloud,state){
  const accounts=await engine.accounts();
  const configured=await cloud.request('accounts',{accounts:accounts.map(a=>({account_id:a.accountID,user_id:a.user.id,channel:channel(a),
    engine_provider:a.bridge.provider,status:a.status,label:a.user.fullName||a.network||channel(a)}))});
  await settlePending(engine,cloud,state);
  const queue=await cloud.request('pending',undefined,'GET');
  for(const id of queue.reply_ids??[])await dispatch(engine,cloud,state,id);
  for(const binding of configured.accounts??[]){
    if(binding.enabled!==true)continue;
    const page=await engine.chats(binding);
    for(const c of page.items){
      if(c.type!=='single'||c.merge)continue;
      try{const messages=await engine.messages(binding,c);await cloud.request('threads',snapshot(binding,c,messages));}
      catch(error){if(/cross_account|unbound/.test(error.message))throw error;}
      // Long initial histories must not starve replies queued in Growth HQ.
      const waiting=await cloud.request('pending',undefined,'GET');
      for(const id of waiting.reply_ids??[])await dispatch(engine,cloud,state,id);
    }
  }
}
async function run(){
  const directory=process.env.GHQ_PERSONAL_DATA_DIR;if(!directory)throw Error('missing_private_data_directory');
  const state=await openState(directory);
  const token=state.state.creds.token;if(!token)throw Error('connect_the_local_engine_first');
  const engine=new EngineClient(token),cloud=new GrowthHqClient(process.env.GHQ_INBOX_PERSONAL_SECRET);
  let stopped=false;process.on('SIGINT',()=>{stopped=true;});process.on('SIGTERM',()=>{stopped=true;});
  while(!stopped){try{await cycle(engine,cloud,state);console.log('Personal inbox sync completed.');}
    catch{console.log('Personal inbox connection needs attention; no send was retried.');}
    if(!stopped)await delay(20000);
  }
  await state.flush();
}
module.exports={openState,dispatch,settlePending,cycle};
if(require.main===module)run().catch(()=>{console.error('Personal inbox setup needs attention.');process.exitCode=1;});
