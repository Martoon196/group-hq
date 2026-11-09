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
function readPeer(threadId){
  const route=location.pathname.match(/^\/messages\/(?:e2ee\/)?t\/(\d+)\/?$/);
  if(!route||route[1]!==threadId)return null;
  const prefix='Messages in conversation with ';
  const logs=[...document.querySelectorAll('[role="log"]')].filter(e=>e.getAttribute('aria-label')?.startsWith(prefix));
  if(logs.length!==1)return null;
  const name=logs[0].getAttribute('aria-label').slice(prefix.length).trim();
  if(!name)return null;
  const ids=[...document.querySelectorAll('a[href]')].filter(e=>e.innerText.trim()===name).map(e=>{
    try{const u=new URL(e.href);return u.origin==='https://www.facebook.com'?u.pathname.match(/^\/(\d+)\/?$/)?.[1]:null;}catch{return null;}
  }).filter(Boolean);
  return ids.length&&new Set(ids).size===1?{name,participantId:ids[0]}:null;
}
function searchReady(state,query,value){return !state.loading&&value.trim()===query.trim();}
function readRecentSentPreviews(query) {
  const list = document.querySelector('[aria-label="Thread list"]');
  if (!list) return [];
  const normalise = value => value.normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim();
  const phrase = normalise(query);
  const matches = [];
  for (const link of list.querySelectorAll('a[href*="/messages/"]')) {
    if (!link.getClientRects().length) continue;
    const url = new URL(link.href);
    const route = url.pathname.match(/^\/messages\/(e2ee\/)?t\/(\d+)\/?$/);
    if (!route || url.origin !== 'https://www.facebook.com') continue;
    const lines = link.innerText.split('\n').map(line => line.trim()).filter(Boolean);
    // Only an explicitly self-authored preview beneath the name is evidence.
    if (!lines.slice(1).some(line => /^You:\s*\S/i.test(line) && normalise(line.slice(4)).includes(phrase))) continue;
    matches.push({threadId: route[2], url: url.origin + url.pathname, encrypted: Boolean(route[1]), name: ''});
  }
  return matches;
}

function readAvailable() {
  const root = document.querySelector('[aria-label="Thread list"]');
  if (!root) throw Error('inbox_not_ready');
  const found = new Map();
  for (const link of root.querySelectorAll('a[href*="/messages/"]')) {
    if (!link.getClientRects().length || !link.innerText.trim()) continue;
    const url = new URL(link.href), route = url.pathname.match(/^\/messages\/(e2ee\/)?t\/(\d+)\/?$/);
    if (!route || url.origin !== 'https://www.facebook.com') continue;
    found.set(route[2], {threadId: route[2], name: link.innerText.split('\n')[0].trim(),
      url: url.origin + url.pathname, encrypted: Boolean(route[1])});
  }
  return {rows: [...found.values()], restoring: root.innerText.includes('Syncing chat history'),
    loading: root.innerText.includes('Loading...')};
}

// Fixed two discovery pages. The list stays at its current rendered position
// between batches; it does not require a phrase or inspect private APIs.
class AvailableDiscovery {
  constructor(context) {this.context=context;this.page=null;this.peerPage=null;this.seen=new Set();this.stable=0;this.nextCycleAt=0;}
  async close() {
    for (const page of [this.page,this.peerPage]) if(page&&!page.isClosed()) await page.close();
    this.page=null;this.peerPage=null;this.seen.clear();this.stable=0;
  }
  async scan(known=[], limit=10) {
    if (!Number.isInteger(limit)||limit<1||limit>20) throw Error('invalid_discovery_batch');
    if (Date.now()<this.nextCycleAt) return null;
    if (!this.page||this.page.isClosed()) {
      this.page=await this.context.newPage();this.seen.clear();this.stable=0;
      await this.page.goto('https://www.facebook.com/messages/',{waitUntil:'domcontentloaded',timeout:30000});
      await this.page.locator('[aria-label="Thread list"]').waitFor({timeout:15000});
    }
    if(!this.peerPage||this.peerPage.isClosed())this.peerPage=await this.context.newPage();
    const verified=new Set(known.map(t=>t.threadId)), candidates=[];let restoring=false;
    for(let scan=0;scan<6&&candidates.length<limit&&this.stable<3;scan++) {
      const state=await this.page.evaluate(readAvailable);restoring||=state.restoring;
      if(state.loading){await this.page.waitForTimeout(500);continue;}
      let fresh=0, deferred=false;
      for(const found of state.rows) {
        if(this.seen.has(found.threadId))continue;
        // Do not scroll away from unprocessed rows when this batch is full.
        if(candidates.length>=limit){deferred=true;break;}
        this.seen.add(found.threadId);fresh++;
        if(verified.has(found.threadId))continue;
        const row={threadId:found.threadId,name:found.name,participantId:'',textOnly:true,
          threadKind:found.encrypted?'e2ee':'page',reason:''};
        try {
          await this.peerPage.goto(found.url,{waitUntil:'domcontentloaded',timeout:20000});
          await this.peerPage.waitForFunction(readPeer,found.threadId,{timeout:8000});
          const identity=await this.peerPage.evaluate(readPeer,found.threadId);
          if(!identity)throw Error('unresolved_identity');
          row.name=identity.name;row.participantId=identity.participantId;
          if(row.threadKind==='page'&&row.participantId!==row.threadId)row.reason='This non-Page conversation format needs support.';
        }catch{row.reason='Messenger has not supplied a verified participant yet. It will be checked again in a later scan.';}
        candidates.push(row);
      }
      this.stable=fresh?0:this.stable+1;
      if(deferred)break;
      const links=this.page.locator('[aria-label="Thread list"] a[href*="/messages/"]').filter({visible:true});
      if(await links.count())await links.last().scrollIntoViewIfNeeded();
      await this.page.waitForTimeout(500);
    }
    const note=restoring?'Available rendered chats checked in batches; Messenger is still restoring history.'
      :'Available rendered chats checked in batches. Older or unloaded chats may still be missing.';
    if(this.stable>=3){await this.close();this.nextCycleAt=Date.now()+60000;}
    return {candidates,complete:false,note};
  }
}
async function discover(context,query,onBatch=null,isPaused=()=>false){
  if(typeof query!=='string'||query.trim().length<2||query.length>100)throw Error('invalid_search');
  const page=await context.newPage(),found=new Map();let restoring=false;
  try{
    await page.goto('https://www.facebook.com/messages/',{waitUntil:'domcontentloaded',timeout:30000});
    const search = page.getByRole('combobox',{name:'Search Messenger',exact:true});
    await search.waitFor();
    const recentLinks = page.locator('[aria-label="Thread list"] a[href*="/messages/"]').filter({visible:true});
    const recentReady = await recentLinks.first().waitFor({timeout:3000}).then(() => true, () => false);
    const recent = recentReady ? await page.evaluate(readRecentSentPreviews, query) : [];
    let searchUnavailable = false;
    try {
      await search.fill(query);
      await page.getByRole('link',{name:`Search messages for ${query}`,exact:true}).click();
      await page.getByRole('textbox',{name:'Search',exact:true}).waitFor();
      let stable=0;
      while(stable<3){
        if(isPaused())throw Error('search_paused');
        const state=await page.evaluate(readSearch),before=found.size;restoring||=state.restoring;
        if(!searchReady(state,query,await page.getByRole('textbox',{name:'Search',exact:true}).inputValue())){
          stable=0;await page.waitForTimeout(700);continue;
        }
        for(const row of state.rows)found.set(row.threadId,row);
        stable=found.size===before?stable+1:0;
        const links=page.locator('[aria-label="Thread list"] a[href*="/messages/"]').filter({visible:true});
        if(await links.count())await links.last().scrollIntoViewIfNeeded();
        await page.waitForTimeout(700);
      }
    } catch (error) {
      if (!recent.length) throw error;
      searchUnavailable = true;
    }
    for (const row of recent) {
      if (!found.has(row.threadId)) found.set(row.threadId, row);
    }
    const peerPage=await context.newPage(),verified=[];
    try{
      for(const row of found.values()){
        if(isPaused())throw Error('search_paused');
        row.participantId='';row.textOnly=true;row.reason='';row.threadKind=row.encrypted?'e2ee':'page';
        try{
          await peerPage.goto(row.url,{waitUntil:'domcontentloaded',timeout:20000});
          await peerPage.waitForFunction(readPeer,row.threadId,{timeout:10000});
          const identity=await peerPage.evaluate(readPeer,row.threadId);
          if(!identity)throw Error('unresolved_identity');
          row.name=identity.name;row.participantId=identity.participantId;
          if(row.threadKind==='page'&&row.participantId!==row.threadId)row.reason='This non-Page conversation format needs support.';
        }catch{row.reason='Messenger has not supplied a verified participant yet. Search again after history restores.';}
        if(row.name){const {url,encrypted,...candidate}=row;verified.push(candidate);}
        if(onBatch&&verified.length>=100){await onBatch({candidates:verified.splice(0),complete:false,
          note:'Available search results are being verified and streamed in batches.'},false);}
      }
    }finally{await peerPage.close();}
    const note = searchUnavailable ? 'Messenger search was unavailable. Only visible matching sent previews were checked.'
      : restoring ? 'Messenger is restoring history. Available results and visible sent previews were checked.'
      : 'Available results and visible sent previews were checked. Older or unloaded chats may still be missing.';
    if(onBatch){await onBatch({candidates:verified,complete:false,note},true);return {streamed:true,complete:false,note};}
    return {candidates:verified,complete:false,note};
  }finally{await page.close();}
}
function validateTargets(rows){
  if(!Array.isArray(rows))throw Error('invalid_selection');
  const seen=new Set();
  for(const t of rows){if(!/^\d+$/.test(t.threadId)||!/^\d+$/.test(t.participantId)||!['e2ee','page'].includes(t.threadKind??'e2ee')||
    (t.threadKind==='page'&&t.participantId!==t.threadId)||typeof t.name!=='string'||!t.name.trim()||t.name.length>500||seen.has(t.threadId))throw Error('invalid_selection');seen.add(t.threadId);}
  return rows;
}
module.exports={discover,readSearch,readPeer,readRecentSentPreviews,readAvailable,AvailableDiscovery,searchReady,validateTargets};
