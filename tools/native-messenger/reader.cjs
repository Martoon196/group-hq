'use strict';
const {createHash}=require('node:crypto');
// This function reads rendered Messenger DOM only. No cookies, storage, React
// internals, undocumented network endpoints or encryption keys are accessed.
function readConversation(expected) {
  const url = new URL(location.href);
  const match = url.pathname.match(expected.threadKind==='page'?/^\/messages\/t\/(\d+)\/?$/:/^\/messages\/e2ee\/t\/(\d+)\/?$/);
  if (url.origin !== 'https://www.facebook.com' || !match || match[1] !== expected.threadId) throw Error('wrong_thread');
  const logs = [...document.querySelectorAll('[role="log"]')].filter(e => e.getAttribute('aria-label') === `Messages in conversation with ${expected.name}`);
  if (logs.length !== 1) throw Error('conversation_not_ready');
  const log = logs[0];
  const profiles = [...document.querySelectorAll('a[href]')].filter(e => e.textContent.trim().startsWith(expected.name))
    .map(e => { try {const u=new URL(e.href);return u.origin==='https://www.facebook.com' ? u.pathname.match(/^\/(\d+)\/?$/)?.[1] : null;} catch{return null;} }).filter(Boolean);
  if (!profiles.length || profiles.some(id => id !== expected.participantId)) throw Error('wrong_participant');
  const contextDates = new Map();
  if(expected.archive===true){
    let date='';
    for(const node of log.querySelectorAll('[role="heading"],[role="row"],h3,time,[data-message-id]')){
      if(node.hasAttribute('data-message-id')){contextDates.set(node,date);continue;}
      if(node.closest('[data-message-id]')||node.querySelector('[data-message-id]'))continue;
      const text=(node.innerText||node.textContent||'').trim();
      if(text!==expected.name&&text.length<100){
        if(/^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?(?:(?:\d{1,2}\s+[A-Za-z]+|[A-Za-z]+\s+\d{1,2}),?\s+\d{4}|\d{1,2}\/\d{1,2}\/\d{4})$/i.test(text))date=text;
        // A later unsupported/no-year date heading must clear the previous
        // context. Never attach an old day's clock-only rows to that date.
        else if(/^(Today|Yesterday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|\d{1,2}\s+[A-Za-z]+|[A-Za-z]+\s+\d{1,2})(?:\b|,)/i.test(text))date='';
      }
    }
  }
  const messages = [...log.querySelectorAll('[data-message-id]')].map(e => ({
    id: e.getAttribute('data-message-id'), label: e.getAttribute('aria-label'), heading: e.querySelector('h4')?.textContent?.trim() ?? '',
    ...(expected.archive===true?{context_date:contextDates.get(e)||''}:{})
  }));
  let historyState={};
  if(expected.archive===true){
    let scroller=log;
    while(scroller.parentElement&&scroller.scrollHeight<=scroller.clientHeight+1)scroller=scroller.parentElement;
    const reverse=getComputedStyle(scroller).flexDirection==='column-reverse'||scroller.scrollTop<0;
    const edge=reverse?-(scroller.scrollHeight-scroller.clientHeight):0;
    const notices=[...document.querySelectorAll('[role="dialog"],[role="alert"]')]
      .filter(e=>e.getClientRects().length).map(e=>(e.innerText||'').trim());
    historyState={at_oldest_scroll:Math.abs(scroller.scrollTop-edge)<3,
      restoring:notices.some(t=>/syncing chat history|restoring.*history/i.test(t)),
      restore_required:notices.some(t=>/enter (?:your )?pin|restore (?:your )?(?:chat|message) history/i.test(t)),
      scroll_top:scroller.scrollTop,scroll_height:scroller.scrollHeight,viewport_height:scroller.clientHeight};
  }
  return {url:url.href,threadId:match[1],participantId:expected.participantId,name:expected.name,
    observedAt:new Date().toISOString(), messages,
    // This signals the visible introduction, not a promise about deleted or
    // unavailable historical messages. Virtualised windows remain incomplete.
    introductionVisible:[...log.querySelectorAll('h3')].some(e=>e.textContent.trim()===expected.name),
    virtualised:!!log.querySelector('[data-virtualized="true"]'),
    composerReady:[...document.querySelectorAll('[contenteditable="true"]')].some(e=>e.getAttribute('aria-label')===`Write to ${expected.name}`),...historyState};
}

const weekdays=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
function displayedTime(label, observedAt, timeZone) {
  // Deliberately bounded to the locale/timezone configured for this browser.
  // Unsupported/ambiguous date formats block import rather than fabricate time.
  const explicitLabel=/^\d{1,2}:\d{2}$/.test(label)?'Today '+label:label;
  const m=explicitLabel.match(/^(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Today|Yesterday) (\d{1,2}):(\d{2})$/);
  if(!m || +m[2]>23 || +m[3]>59) throw Error('unsupported_displayed_time');
  const observed=new Date(observedAt);if(!Number.isFinite(observed.getTime()))throw Error('invalid_observation');
  const parts=new Intl.DateTimeFormat('en-GB',{timeZone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(observed);
  const p=Object.fromEntries(parts.map(x=>[x.type,x.value]));
  const day=new Date(Date.UTC(+p.year,+p.month-1,+p.day));
  const back=m[1]==='Today'?0:m[1]==='Yesterday'?1:(day.getUTCDay()-weekdays.indexOf(m[1])+7)%7;
  day.setUTCDate(day.getUTCDate()-back);
  const wall=Date.UTC(day.getUTCFullYear(),day.getUTCMonth(),day.getUTCDate(),+m[2],+m[3]);
  // Resolve local wall-clock minute against timezone offsets; reject DST's
  // repeated/missing hour instead of silently choosing one.
  return wallTime(wall,observed,timeZone);
}
function wallTime(wall,observed,timeZone){
  const day=new Date(wall),hour=day.getUTCHours(),minute=day.getUTCMinutes(),candidates=[];
  for(let offset=-14*60;offset<=14*60;offset+=15){
    const instant=new Date(wall-offset*60000);
    const v=Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(instant).map(x=>[x.type,x.value]));
    if(+v.year===day.getUTCFullYear()&&+v.month===day.getUTCMonth()+1&&+v.day===day.getUTCDate()&&+v.hour===hour&&+v.minute===minute)candidates.push(instant);
  }
  if(candidates.length!==1 || candidates[0]>observed)throw Error('ambiguous_displayed_time');
  return candidates[0].toISOString();
}

function normalise(observed, expected, binding) {
  if(observed.threadId!==expected.threadId||observed.participantId!==expected.participantId||observed.name!==expected.name)throw Error('wrong_thread_binding');
  if(binding.channel!=='facebook'||!/^[0-9]+$/.test(binding.user_id)||binding.account_id!==`facebook-web:${binding.user_id}`)throw Error('wrong_account_binding');
  if(!Array.isArray(observed.messages)||observed.messages.length===0||observed.messages.length>100)throw Error('unsupported_message_window');
  const ids=new Set();
  for(const m of observed.messages){
    const valid=expected.threadKind==='page'?
      expected.threadId===expected.participantId&&/^mid\.\$[A-Za-z0-9_-]{1,200}$/.test(m.id):
      typeof m.id==='string'&&m.id.startsWith(expected.participantId+'@msgr.')&&/^\d+@msgr\.\d+$/.test(m.id);
    if(typeof m.id!=='string'||!valid||ids.has(m.id))throw Error('invalid_message_identity');
    ids.add(m.id);
  }
  const selected=expected.textOnly===true?observed.messages.filter(m=>{
    if(typeof m.label!=='string'||m.label.length>22000)return false;
    const parsed=m.label.match(/^At (.+?), ([^:]+): ([\s\S]+)$/);
    return !!parsed&&(m.heading==='You sent'?parsed[2]==='You':parsed[2]===m.heading&&expected.name.split(' ').includes(m.heading));
  }):observed.messages;
  // A text-only account may have only attachments in its current window.
  // Do not replace any cached history with an empty snapshot.
  if(!selected.length)return null;
  const omitted=observed.messages.length-selected.length;
  const messages=selected.map(m=>{
    if(typeof m.label!=='string'||m.label.length>22000)throw Error('invalid_message_label');
    const parsed=m.label.match(/^At (.+?), ([^:]+): ([\s\S]+)$/);
    if(!parsed)throw Error('unsupported_message');
    const outgoing=m.heading==='You sent';
    if(outgoing ? parsed[2]!=='You' : (parsed[2]!==m.heading || !expected.name.split(' ').includes(m.heading)))throw Error('unknown_sender');
    const occurredAt=displayedTime(parsed[1],observed.observedAt,binding.timeZone);
    const revision='dom:'+createHash('sha256').update(JSON.stringify([parsed[3],outgoing,occurredAt])).digest('hex');
    return {id:m.id,body:parsed[3],name:outgoing?'Martin Howard':expected.name,direction:outgoing?'outgoing':'incoming',
      occurred_at:occurredAt,revision,source_time:parsed[1],time_precision:'minute'};
  });
  if(messages.some((m,i)=>i&&m.occurred_at<messages[i-1].occurred_at))throw Error('nonchronological_window');
  return {account_key:binding.key,account_id:binding.account_id,user_id:binding.user_id,observed_at:observed.observedAt,
    thread:{id:expected.threadId,participant_id:expected.participantId,name:expected.name,preview:messages.at(-1).body,
      updated_at:messages.at(-1).occurred_at,is_group:false,is_merged:false,messages,has_more:omitted>0||observed.introductionVisible!==true||observed.virtualised===true},
    evidence:{transport:'growth-hq-browser',history:'rendered_window',omitted_rows:omitted,time_precision:'minute',composer_ready:observed.composerReady===true}};
}
module.exports={readConversation,normalise,displayedTime,wallTime};
