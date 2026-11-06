const http=require('node:http');
const crypto=require('node:crypto');
const fs=require('node:fs/promises');
const path=require('node:path');

async function createPairingPage(directory,{renderQr}={}) {
  const render=renderQr||((value)=>require('qrcode').toString(value,{type:'svg',margin:4,width:440}));
  await fs.mkdir(directory,{recursive:true,mode:0o700});
  const pointer=path.join(directory,'pairing-url.txt');
  let previous;
  try {
    previous=new URL((await fs.readFile(pointer,'utf8')).trim());
    if(previous.protocol!=='http:'||previous.hostname!=='127.0.0.1'||previous.username||previous.password||previous.search||previous.hash||!/^\/[A-Za-z0-9_-]{32}$/.test(previous.pathname)||Number(previous.port)<1024)throw Error('Invalid private pairing address');
  }catch(error){if(error.code!=='ENOENT')throw error;}
  const route=previous?.pathname||'/'+crypto.randomBytes(24).toString('base64url');
  let svg='',expires=0,phase='connecting',generation=0;
  const labels={connecting:'Connecting to WhatsApp…',pairing:'Scan this QR with your phone',
    connected:'WhatsApp linked. Choose your groups and keywords in Growth HQ.',
    reconnecting:'Reconnecting to WhatsApp…',error:'The connection needs checking. No messages will be sent.'};
  const server=http.createServer((req,res)=>{
    if(req.method!=='GET'||req.url!==route){res.writeHead(404);res.end();return;}
    const fresh=phase==='pairing'&&Date.now()<expires;
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store',
      'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'",'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer'});
    res.end(`<!doctype html><html><head><meta http-equiv="refresh" content="5"><title>Link WhatsApp to Growth HQ</title><style>body{font:20px system-ui;max-width:650px;margin:28px auto;padding:16px;color:#142019}h1{font-size:30px}svg{display:block;max-width:100%;height:auto}p{line-height:1.5}a{color:#142019}</style></head><body><h1>Link WhatsApp to Growth HQ</h1><p>${phase==='pairing'&&!fresh?'Refreshing the QR…':labels[phase]}</p>${fresh?'<p>On your phone: WhatsApp → Settings → Linked devices → Link a device.</p>'+svg:''}<p>Keep this page private.</p><p><a href="https://group-hq.onrender.com/crm/channels/whatsapp/keywords">Open Growth HQ keywords</a></p></body></html>`);
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(previous?Number(previous.port):0,'127.0.0.1',resolve);});
  const url=`http://127.0.0.1:${server.address().port}${route}`;
  await fs.writeFile(pointer,url,{mode:0o600});
  return {url,
    showQr:async value=>{const at=++generation;const image=await render(value);if(at!==generation)return;svg=image;expires=Date.now()+60000;phase='pairing';},
    setStatus:value=>{if(!Object.hasOwn(labels,value))throw Error('Invalid connection state');++generation;phase=value;svg='';expires=0;},
    status:()=>({phase,qrReady:phase==='pairing'&&Date.now()<expires}),
    close:()=>new Promise(resolve=>server.close(resolve)),
  };
}
module.exports={createPairingPage};
