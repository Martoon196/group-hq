const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');

// One encrypted atomic snapshot. Never silently replace a missing/corrupt session.
async function openAuthState(directory, provider) {
  await fs.mkdir(directory,{recursive:true,mode:0o700});
  const keyFile=path.join(directory,'storage.key'), file=path.join(directory,'auth.sealed.json');
  let key;
  try {key=await fs.readFile(keyFile);}
  catch(error) {
    if(error.code!=='ENOENT')throw error;
    if(await fs.stat(file).catch(e=>{if(e.code==='ENOENT')return null;throw e;}))throw Error('Session key is missing');
    key=crypto.randomBytes(32);await fs.writeFile(keyFile,key,{flag:'wx',mode:0o600});
  }
  if(key.length!==32)throw Error('Invalid session key');
  let data;
  try {
    const envelope=JSON.parse(await fs.readFile(file,'utf8'));
    if(envelope.version!==1)throw Error('Unsupported session snapshot');
    const decipher=crypto.createDecipheriv('aes-256-gcm',key,Buffer.from(envelope.iv,'base64'));
    decipher.setAuthTag(Buffer.from(envelope.tag,'base64'));
    data=JSON.parse(Buffer.concat([decipher.update(Buffer.from(envelope.body,'base64')),decipher.final()]).toString('utf8'),provider.BufferJSON.reviver);
  } catch(error) {if(error.code!=='ENOENT')throw error;data={creds:provider.initAuthCreds(),keys:{}};}
  let pending=Promise.resolve(), failed=false;
  const persist=()=>{
    if(failed)return Promise.reject(Error('Session persistence needs attention'));
    const snapshot=JSON.stringify(data,provider.BufferJSON.replacer);
    const next=pending.then(async()=>{
      const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);
      const body=Buffer.concat([cipher.update(snapshot,'utf8'),cipher.final()]);
      const envelope={version:1,iv:iv.toString('base64'),tag:cipher.getAuthTag().toString('base64'),body:body.toString('base64')};
      const temp=file+'.'+crypto.randomUUID()+'.tmp';
      const handle=await fs.open(temp,'wx',0o600);
      try {await handle.writeFile(JSON.stringify(envelope));await handle.sync();}finally{await handle.close();}
      await fs.rename(temp,file);
    });
    pending=next.catch(()=>{failed=true;});
    return next;
  };
  const state={creds:data.creds,keys:{
    get:async(type,ids)=>{
      if(failed)throw Error('Session persistence needs attention');
      const result={};
      for(const id of ids){
        let value=data.keys[type]?.[id]??null;
        if(type==='app-state-sync-key'&&value)value=provider.proto.Message.AppStateSyncKeyData.fromObject(value);
        result[id]=value;
      }
      return result;
    },
    set:async(update)=>{
      for(const [type,values] of Object.entries(update)) {
        data.keys[type]??={};
        for(const [id,value] of Object.entries(values)) {
          if(value==null)delete data.keys[type][id];else data.keys[type][id]=value;
        }
      }
      await persist();
    },
  }};
  return {state,saveCreds:persist,flush:async()=>{await pending;if(failed)throw Error('Session persistence needs attention');}};
}
module.exports={openAuthState};
