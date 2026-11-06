const {test}=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const {normalise,findTrigger}=require('./matcher.cjs');
const {makeApi,findRule}=require('./adapter.cjs');
test('reused matcher accepts exact punctuation but not incidental income',()=>{
 const rules=[{keywords:['Income'],message:'hello'}];
 for(const value of ['Income','income!','INCOME 🙌']) assert.ok(findTrigger(value,rules));
 assert.equal(findTrigger('my income dropped',rules),null);
 assert.ok(findTrigger('my income dropped',rules,'word'));
 assert.equal(normalise('  FIRST—Door! '),'first door');
});
test('empty groups never means all groups; paused rules stay off',()=>{
 const r={enabled:true,groupIds:[],keywords:['income'],matchMode:'exact'};
 const s={revision:1,listenerId:'personal-7510',rules:[r]};
 assert.equal(findRule(s,'12345@g.us','income'),null);
 r.groupIds=['12345@g.us'];assert.equal(findRule(s,'12345@g.us','income'),r);
 r.enabled=false;assert.equal(findRule(s,'12345@g.us','income'),null);
});
test('API signs exact method path bytes and never follows redirects',async()=>{
 const secret='x'.repeat(40);let call;
 const api=makeApi({origin:'https://group-hq.onrender.com',secret,now:()=>1700000000000,
  fetchFn:async(url,options)=>{call={url,options};return new Response('{"ok":true}');}});
 assert.deepEqual(await api('POST','events',{a:'é'}),{ok:true});
 assert.equal(call.url,'https://group-hq.onrender.com/api/whatsapp-keywords/v1/events');
 assert.equal(call.options.redirect,'error');
 assert.equal(call.options.headers['X-GHQ-Signature'],crypto.createHmac('sha256',secret).update('1700000000\nPOST\n/api/whatsapp-keywords/v1/events\n{"a":"é"}').digest('hex'));
});
test('reject insecure connections and provider failures without reflection',async()=>{
 assert.throws(()=>makeApi({origin:'http://group-hq.onrender.com',secret:'x'.repeat(40)}));
 const api=makeApi({origin:'https://group-hq.onrender.com',secret:'x'.repeat(40),fetchFn:async()=>new Response('secret-private-error',{status:409})});
 await assert.rejects(()=>api('GET','rules'),e=>e.status===409&&!e.message.includes('secret-private'));
});
