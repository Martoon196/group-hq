'use strict';
// Search only rendered Messenger UI. Discovery returns candidate metadata;
// no conversation bodies enter Growth HQ until the owner selects that thread.
function readSearch() {
  const root=document.querySelector('[aria-label="Thread list"]');
  if(!root)throw Error('search_not_ready');
  const rows=[...root.querySelectorAll('a[href*="/messages/"]')].filter(e=>e.innerText.trim()).map(e=>{
    const url=new URL(e.href), match=url.pathname.match(/^\/messages\/(e2ee\/)?t\/(\d+)\/?$/);
    return match&&url.origin==='https://www.facebook.com'?{threadId:match[2],name:e.innerText.split('\n')[0].trim(),
      url:url.href,encrypted:!!match[1]}:null;
  }).filter(Boolean);
  return {rows,restoring:root.innerText.includes('Syncing chat history'),loading:root.innerText.includes('Loading...')};
}
function readPeer(name){
  const logs=[...document.querySelectorAll('[role="log"]')].filter(e=>e.getAttribute('aria-label')===`Messages in conversation with ${name}`);
  const ids=[...document.querySelectorAll('a[href]')].filter(e=>e.innerText.trim().startsWith(name)).map(e=>{
    try{const u=new URL(e.href);return u.origin==='https://www.facebook.com'?u.pathname.match(/^\/(\d+)\/?$/)?.[1]:null;}catch{return null;}
  }).filter(Boolean);
  return logs.length===1&&ids.length&&new Set(ids).size===1?ids[0]:null;
}
async function discover(context,query){
  if(typeof query!=='string'||query.trim().length<2||query.length>100)throw Error('invalid_search');
  const page=await context.newPage(),found=new Map();let restoring=false;
  try{
    await page.goto('https://www.facebook.com/messages/',{waitUntil:'domcontentloaded',timeout:30000});
    await page.getByRole('combobox',{name:'Search Messenger',exact:true}).fill(query);
    await page.getByRole('link',{name:`Search messages for ${query}`,exact:true}).click();
    await page.getByRole('textbox',{name:'Search',exact:true}).waitFor();
    let stable=0;
    for(let scan=0;scan<12&&stable<3&&found.size<100;scan++){
      const state=await page.evaluate(readSearch),before=found.size;restoring||=state.restoring;
      for(const row of state.rows)if(found.size<100)found.set(row.threadId,row);
      stable=found.size===before?stable+1:0;
      const links=page.locator('[aria-label="Thread list"] a[href*="/messages/"]').filter({visible:true});
      if(await links.count())await links.last().scrollIntoViewIfNeeded();
      await page.waitForTimeout(700);
    }
    const peerPage=await context.newPage();
    try{
      for(const row of found.values()){
        row.participantId='';row.textOnly=true;row.reason='';
        if(!row.encrypted){row.reason='This Messenger conversation format needs support.';continue;}
        try{
          await peerPage.goto(row.url,{waitUntil:'domcontentloaded',timeout:20000});
          await peerPage.waitForFunction(readPeer,row.name,{timeout:10000});
          row.participantId=await peerPage.evaluate(readPeer,row.name);
        }catch{row.reason='Messenger has not supplied a verified participant yet. Search again after history restores.';}
      }
    }finally{await peerPage.close();}
    return {candidates:[...found.values()].map(({url,encrypted,...row})=>row),complete:false,
      note:restoring?'Messenger is still restoring history. Search again for more results.':'Available search window checked; older or unloaded chats may still be missing.'};
  }finally{await page.close();}
}
function validateTargets(rows){
  if(!Array.isArray(rows)||rows.length>100)throw Error('invalid_selection');
  const seen=new Set();
  for(const t of rows){if(!/^\d+$/.test(t.threadId)||!/^\d+$/.test(t.participantId)||typeof t.name!=='string'||!t.name.trim()||t.name.length>500||seen.has(t.threadId))throw Error('invalid_selection');seen.add(t.threadId);}
  return rows;
}
module.exports={discover,readSearch,readPeer,validateTargets};
