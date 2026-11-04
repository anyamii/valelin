// Offline Valelin defaults; preferences belong to this browser only.
(() => {
  const visitDay='e1:'+new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(Date.now()-4*3600000));
  if(sessionStorage.getItem('valelin_demo_visit_day')!==visitDay){sessionStorage.clear();sessionStorage.setItem('valelin_demo_visit_day',visitDay);}
  const newVisit=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
  let visit=sessionStorage.getItem('valelin_demo_visit');
  if(!/^[a-f0-9]{32}$/.test(visit||'')){visit=newVisit();sessionStorage.setItem('valelin_demo_visit',visit);}
  window.DEMO_VISIT_ID=visit;
  const NativeEventSource=window.EventSource;
  window.EventSource=class extends NativeEventSource{constructor(input,options){const u=new URL(input,location.href);u.searchParams.set('demo_visit',visit);super(u.href,options);}};
  // Retire shared pre-isolation chat caches; preferences remain local to this browser.
  localStorage.removeItem('Lin_unsent_queue');localStorage.removeItem('valelin_agent_timeline_run_v1');
  try{indexedDB.deleteDatabase('companion-message-cache');}catch(_){}

  const epoch=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(Date.now()-4*3600000));
  const stamp='v2:'+epoch();
  if(localStorage.getItem('valelin_demo_epoch')!==stamp){localStorage.clear();localStorage.setItem('valelin_demo_epoch',stamp);}
  const defaults={companion_secret:'demo-local',segment_enabled:'on',voice_enabled:'on',waifu_on:'on',waifu_char_delay:'45',waifu_bubble_gap:'0',waifu_strip_punct:'off',Lin_split_on:'on',sticker_on:'on',stt_keyterms:'Vale\nValelin'};
  for(const [key,value] of Object.entries(defaults))if(localStorage.getItem(key)===null)localStorage.setItem(key,value);
  const checkDay=()=>{if(localStorage.getItem('valelin_demo_epoch')!=='v2:'+epoch())location.reload();};
  setInterval(checkDay,60000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkDay();});
  const holidays=[{id:'new-year',date:'2026-01-01',name:'元旦',yearly:true},{id:'spring',date:'2026-02-17',name:'春节',yearly:true},{id:'valentine',date:'2026-02-14',name:'情人节',yearly:true},{id:'midautumn',date:'2026-09-25',name:'中秋节',yearly:true}];
  if(localStorage.getItem('valelin_anniv')===null)localStorage.setItem('valelin_anniv',JSON.stringify(holidays));
  localStorage.setItem('valelin_anniv_seed_v1','1');localStorage.setItem('valelin_period_seed_v1','1');localStorage.removeItem('valelin_period');
  if(localStorage.getItem('valelin_study_cards_v1')===null){
    const rows=[['word','clarify','/ˈklærɪfaɪ/','澄清；阐明','Could you clarify the goal?'],['word','consistent','/kənˈsɪstənt/','一致的；持续的','Small, consistent steps make progress.'],['word','verify','/ˈverɪfaɪ/','核实；验证','Verify the result before you continue.'],['sentence','Break the task into small steps.','','把任务分成小步骤。','方法：先拆分，再逐项验证。'],['sentence','Let us check what we have learned.','','让我们检查一下学到了什么。','复习时先回想，再看答案。']];
    const now=new Date().toISOString();localStorage.setItem('valelin_study_cards_v1',JSON.stringify(rows.map(([type,front,phonetic,meaning,note],i)=>({id:'valelin-example-'+i,type,front,phonetic,meaning,note,focus:'',source:'通用英语示例',audio_url:'',added_by:'Vale',created_at:now,updated_at:now,review:{box:0,due_at:now,last_reviewed_at:null,lapses:0}}))));
  }
  const response=data=>Promise.resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
  const original=window.fetch.bind(window);
  const voice=()=>({ok:true,segment_mode:localStorage.getItem('segment_enabled')==='on',voice_mode:localStorage.getItem('voice_enabled')==='on',sticker_mode:localStorage.getItem('sticker_on')==='on',tts:Object.fromEntries(Object.keys(localStorage).filter(k=>k.startsWith('tts_')).map(k=>[k.slice(4),localStorage.getItem(k)])),stt_provider:localStorage.getItem('stt_provider')||'',stt_model:localStorage.getItem('stt_model')||'',stt_lang:localStorage.getItem('stt_lang')||'auto',stt_keyterms:localStorage.getItem('stt_keyterms')||''});
  const baseIdentity={assistant_name:'Vale',user_name:'Lin',assistant_avatar:'',user_avatar:'',chat_background:'',chat_background_opacity:100};
  for(const key of ['valelin_avatar','companion_avatar','avatar']){if(/demo-[vl]\.svg/.test(localStorage.getItem(key)||''))localStorage.removeItem(key);}
  const identity=()=>{const cfg={...baseIdentity,...JSON.parse(localStorage.getItem('valelin_identity')||'{}')};for(const key of ['assistant_avatar','user_avatar'])if(!cfg[key] || /demo-[vl]\.svg/.test(cfg[key]))cfg[key]=baseIdentity[key];return cfg;};
  const settings=new Set(['/app/config','/app/sdk/config','/app/history-n','/app/loop_config','/app/sdk/context','/app/brain']);
  window.fetch=async(input,init={})=>{
    const url=new URL(typeof input==='string'?input:input.url,location.href);
    if(url.origin!==location.origin)return response({detail:'Offline demo: external requests disabled'});
    const method=(init.method||(input instanceof Request?input.method:'GET')).toUpperCase();
    let body={};if(init.body&&typeof init.body==='string'){try{body=JSON.parse(init.body);}catch(_){}}
    if(['/app/voice_mode','/app/stt_config'].includes(url.pathname)){
      if(method!=='GET')for(const [key,value] of Object.entries(body)){
        const map={segment_enabled:'segment_enabled',voice_enabled:'voice_enabled',sticker_enabled:'sticker_on'};
        const target=map[key]|| (key.startsWith('stt_')?key:'tts_'+key);
        localStorage.setItem(target,typeof value==='boolean'?(value?'on':'off'):String(value));
      }
      return response(voice());
    }
    if(url.pathname==='/config'){
      const cfg=identity();if(method!=='GET'){Object.assign(cfg,body);localStorage.setItem('valelin_identity',JSON.stringify(cfg));return response({ok:true,config:cfg});}return response(cfg);
    }
    if(url.pathname==='/upload_avatar'){
      const blob=init.body; if(!(blob instanceof Blob))return response({ok:false});
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});
      const cfg=identity(),type=url.searchParams.get('type');cfg[type==='background'?'chat_background':type==='assistant'?'assistant_avatar':'user_avatar']=data;localStorage.setItem('valelin_identity',JSON.stringify(cfg));return response({ok:true,url:data,config:cfg});
    }
    if(settings.has(url.pathname)){
      const key='valelin_config:'+url.pathname;const cfg={provider:'replay',model:'replay',target:'replay',effort:'low',...JSON.parse(localStorage.getItem(key)||'{}')};
      if(method!=='GET'){Object.assign(cfg,body);localStorage.setItem(key,JSON.stringify(cfg));}return response({...cfg,ok:true});
    }
    const headers=new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    headers.set('X-Demo-Visit',visit);
    return original(input,{...init,headers});
  };
})();
