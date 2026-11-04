/* Browser-only transport for the original Valelin renderer. */
(() => {
  'use strict';
  const scenes = [{"id": "command_push", "prompt": "帮我开个二十五分钟的番茄钟，我先整理笔记。", "keywords": ["番茄", "专注"], "tool": "command_push", "thinking": "先明确这次专注的目标和时长。", "reply": "番茄钟已经开始了。先把这一小段做好，结束后休息一下。\n\n这二十五分钟只处理一件事：把学习笔记整理成几个清楚的要点。\n\n结束后回看最有收获的一点，再安排下一轮。"}, {"id": "run_check", "prompt": "帮我检查一下这个项目，运行前先问我。", "keywords": ["检查", "审批"], "tool": "run_check", "thinking": "先说明将进行的检查，等你同意。", "reply": "检查好了，没有发现语法问题。\n\n文件没有改动，你可以继续整理项目。"}, {"id": "file", "prompt": "把这份演示说明发给我吧。", "keywords": ["说明", "文件卡"], "tool": "file_send", "thinking": "先准备一份可以下载的说明文件。", "reply": "说明整理好了，你可以直接下载。\n\n文件包含演示入口、场景说明和使用方法，方便随时对照。\n\n先看看内容；如果需要整理自己的笔记，也可以选择上传场景。"}, {"id": "voice", "prompt": "用语音提醒我，慢慢来就好。", "keywords": ["语音", "录音"], "tool": "voice", "thinking": "录音素材待授权确认，本次只回放文字转写。", "reply": "[voice] Take one small step, then check your progress.\n[zh] 先迈出一小步，再检查自己的进展。"}, {"id": "calendar", "prompt": "在日历里帮我记一下今天的项目回顾。", "keywords": ["日历", "回顾"], "tool": "calendar_add", "thinking": "确认日期，把项目回顾记到本地日历。", "reply": "记下来了。US 页的今天可以看到项目回顾。\n\n日期和事项已经整理清楚，可以从日历检查记录。\n\n回顾时先看完成了什么，再记下下一步。"}, {"id": "write_scene", "prompt": "把今天学习时的印象记下来吧。", "keywords": ["印象", "记忆"], "tool": "write_scene", "thinking": "回看今天的学习过程，记住可复用的做法。", "reply": "今天的日印象已经记下，Mind 里多了一张卡。\n\n记录重点是可复用的学习方法，不是对个人关系的推测。\n\n下次遇到类似任务，可以从这张卡片继续。"}, {"id": "codex_dispatch", "prompt": "帮我把整理项目说明这件事交给 Codex。", "keywords": ["codex", "派活"], "tool": "codex_dispatch", "thinking": "把目标和验收条件整理好，再回放排队、领走与报告。", "reply": "Codex 做好了。 文档已整理。 检查通过。 退出码 0。\n\n任务已按目标和验收条件投递，随后查询了执行状态。\n\n回放结果：文档已整理，检查通过，退出码为 0。"}, {"id": "upload", "prompt": "这份笔记有点乱，帮我整理一下文件。", "keywords": ["整理一下文件", "改文件"], "tool": "file_edit", "thinking": "读取本地附件，把内容整理成一个可下载的副本。", "reply": "我帮你改好了，你看看。\n\n我保留了原始内容，并在整理副本里补上下一步清单。\n\n你可以下载对照，确认后再保存到自己的目录。"}, {"id": "keep", "prompt": "把这句话收藏起来：先做一个小步骤，再验证结果。", "keywords": ["收藏", "收起来"], "tool": "keep_save", "thinking": "把这句学习方法保存下来，保留原消息入口。", "reply": "收好了。\n\n以后想找这句话，点下面的收藏记录就行。\n\n先做一小步，再看看结果。"}, {"id": "letter", "prompt": "给我写封信，鼓励我继续学习。", "keywords": ["写封信", "写一封信"], "tool": "letter_write", "thinking": "把今天的学习过程写成一封中性的鼓励信。", "reply": "信已经写好了，放进了本地信箱。\n\n信里回顾的是今天的学习步骤和可以继续尝试的方法。\n\n如果卡住，就把问题缩小到一个能验证的小例子。", "menu": false}, {"id": "default", "prompt": "", "keywords": "", "tool": "replay", "thinking": "想想怎样把下一步说清楚。", "reply": "我们可以先把目标拆成一个小步骤，做完再一起检查。\n\n先说明你希望得到什么结果，再选一个能在几分钟内完成的动作。\n\n做完检查结果，把有效的方法记下来，再继续下一步。"}];
  const base = new URL('.', document.currentScript.src);
  const nativeFetch = window.fetch.bind(window);
  const streams = new Set(), history = [], jobs = [], pending = new Map();
  const daily = {}, memories = [], keeps = [];
  let nextId = 1, command = null;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const json = value => new Response(JSON.stringify(value), {headers:{'Content-Type':'application/json'}});
  const asset = name => new URL(name, base).href;
  const emit = value => streams.forEach(s => s.dispatchEvent(new MessageEvent('message', {data:JSON.stringify(value)})));
  class ReplayStream extends EventTarget {
    constructor() { super(); this.readyState=0; streams.add(this); setTimeout(() => {if(this.readyState===2)return;this.readyState=1;this.dispatchEvent(new Event('open'));},20); }
    set onopen(fn) {this.addEventListener('open',fn);}
    set onmessage(fn) {this.addEventListener('message',fn);}
    set onerror(fn) {this.addEventListener('error',fn);}
    close() {this.readyState=2;streams.delete(this);}
  }
  Object.assign(ReplayStream,{CONNECTING:0,OPEN:1,CLOSED:2});
  window.EventSource=ReplayStream;
  window.WebSocket=class extends EventTarget {constructor(){super();this.readyState=3;}send(){}close(){}};
  navigator.sendBeacon=()=>false;
  const save = (from,kind,text,meta={}) => {
    const m={id:nextId++,ts:new Date().toISOString(),from,kind,text,meta};history.push(m);emit(m);return m;
  };
  async function play(msg) {
    const scene=scenes.find(s=>s.prompt&&s.prompt===msg.text)||scenes.find(s=>Array.isArray(s.keywords)&&s.keywords.some(k=>msg.text.toLowerCase().includes(k.toLowerCase())))||scenes.at(-1);
    const run='replay-'+msg.id, common={stream_id:run,source_id:String(msg.id),api_session:msg.meta.api_session||''};
    let sequence=0;
    const frame=(type,data={})=>emit({type,...common,...data});
    const event=(type,data={})=>frame('agent_event',{event:{runId:'sdk:'+run,eventId:run+'-'+(++sequence),sequence,turnId:String(msg.id),type,timestamp:Date.now(),...data}});
    await sleep(300);event('run_state',{status:'running'});
    event('block_start',{blockId:'thought',kind:'thinking'});
    event('thinking_delta',{blockId:'thought',text:scene.thinking});
    frame('thinking_delta',{text:scene.thinking,done:false});await sleep(500);
    event('block_stop',{blockId:'thought'});
    save('ai','thinking',scene.thinking,common);
    const tool='tool-'+msg.id;
    event('block_start',{blockId:tool,kind:'tool',tool:{id:tool,name:scene.tool,source:'replay'}});
    let allowed=true;
    const segments=[], replyId=nextId++;
    const paragraph=async text=>{const seq=segments.length;segments.push({seq,kind:'text',text});frame('text_msg',{msg_id:replyId,seq,text,ts:msg.ts});await sleep(400);};
    if(scene.id==='run_check') {
      await paragraph('我先检查项目里的 Python 文件，看看有没有语法问题。你允许后我再开始。');
      event('tool_state',{toolUseId:tool,status:'preparing',input:{command:'python -m compileall backend'}});
      event('permission_required',{toolUseId:tool});
      const decision=new Promise(resolve=>pending.set(run,resolve));
      frame('sdk_permission_request',{permission_id:run,session_key:'demo',tool_name:scene.tool,title:'允许检查项目语法吗？',description:'只检查 Python 语法，不修改文件。',input:{command:'python -m compileall backend'}});
      allowed=await decision;
    }
    event('tool_state',{toolUseId:tool,status:allowed?'running':'canceled',input:{scene:scene.id}});
    let job;
    if(allowed&&scene.id==='codex_dispatch') {job={id:run,name:'整理项目说明',state:'queued',submitted:Date.now(),timeline:['投出','领走','完成'],summary:['文档已整理。','检查通过。','退出码 0。']};jobs.push(job);await sleep(600);Object.assign(job,{state:'running',start:Date.now()});}
    await sleep(500);
    if(allowed) {
      if(scene.id==='calendar'){const key=new Date().toISOString().slice(0,10);const notes=daily.valelin_notes||{};(notes[key]||=[]).push({id:run,t:'演示项目回顾',author:'model',ts:Date.now()});daily.valelin_notes=notes;}
      if(scene.id==='write_scene')memories.push({id:run,name:'演示日印象',text:'Lin 把复杂问题拆成三个小步骤，逐个验证。',tags:['学习'],comments:[],importance:3});
      if(scene.id==='keep')keeps.push({id:msg.id,text:msg.text,user_text:msg.text,saved_by:'Vale',created_at:msg.ts,tags:[],media:{items:[]}});
      if(scene.id==='command_push'){command={id:run,text:'专注整理学习笔记',minutes:25,started_at:new Date().toISOString(),deadline_at:new Date(Date.now()+25*60000).toISOString(),status:'active'};
        save('ai','reply','[指令 · 专注整理学习笔记 · 25 min → 已推给她]',{command_card_id:run,command_card_status:'active',command_card_finished_at:null});
      }
      if(job)Object.assign(job,{state:'done',duration:108,code:0});
      event('tool_result',{toolUseId:tool,outputPreview:'预设剧本回放完成；没有执行真实工具。',error:false});
      save('ai','act','用了 '+scene.tool,{...common,glyph:'memory',steps:[{tool:scene.tool,cmd:scene.prompt,result:'预设剧本回放完成。'}]});
    }
    const reply=allowed?scene.reply:'好，不运行。等你想检查时再告诉我。';
    for(const text of reply.split('\n\n'))await paragraph(text);
    if(allowed&&['file','upload'].includes(scene.id)) {
      const name=scene.id==='file'?'demo-guide.md':'edited.md', seq=segments.length;
      const attachment={kind:'file',name,url:asset('media/'+name),download_url:asset('media/'+name),mime:'text/markdown',size:200};
      segments.push({seq,...attachment});frame('file_msg',{msg_id:replyId,seq,...attachment,attachment,ts:msg.ts});
    }
    const result={id:replyId,ts:msg.ts,from:'ai',kind:'reply',text:reply,meta:{...common,segments}};
    history.push(result);emit(result);
    if(allowed&&scene.id==='keep')save('ai','keep_activity','Vale 收了你一条：先做一个小步骤，再验证结果。',{keep_activity:{action:'save',actor:'Vale',keep_id:msg.id,keep_ids:[msg.id],count:1,last_at:new Date().toISOString(),turn_id:msg.id,after_seg:segments.length}});
    event('agent_done');frame('typing',{active:false});
  }
  window.fetch=async(input,init={})=>{
    const url=new URL(typeof input==='string'||input instanceof URL?input:input.url,location.href);
    if(url.origin!==base.origin)return json({detail:'静态演示不连接外部服务'});
    let path=url.pathname;
    if(path.startsWith(base.pathname))path='/'+path.slice(base.pathname.length);
    let body={};try{body=JSON.parse(init.body||'{}');}catch{}
    if(path==='/demo/scenes')return json(scenes.filter(s=>s.id!=='default'&&s.menu!==false));
    if(path==='/demo/jobs')return json(jobs);
    if(path==='/app/send') {
      const msg=save('human','user',body.text,{api_session:body.api_session||'',client_token:body.client_token,attachments:body.attachments||[]});
      void play(msg);return json({id:msg.id});
    }
    if(path==='/app/history'){
      let rows=history.filter(m=>m.id>Number(url.searchParams.get('since')||0));
      if(url.searchParams.has('before_id'))rows=rows.filter(m=>m.id<Number(url.searchParams.get('before_id')));
      return json({messages:rows});
    }
    if(path==='/app/sdk/permission'){pending.get(body.permission_id)?.(!!body.allow);pending.delete(body.permission_id);return json({ok:true});}
    if(path==='/app/upload'){const blob=init.body;return json({url:URL.createObjectURL(blob),name:url.searchParams.get('name'),size:blob.size,mime:blob.type,kind:'file'});}
    if(path==='/app/sessions')return json({sessions:[],active_session:''});
    if(path==='/app/daily')return json({documents:Object.fromEntries(Object.entries(daily).map(([key,data])=>[key,{data,updated_at:new Date().toISOString()}]))});
    if(path.startsWith('/app/daily/')){const key=decodeURIComponent(path.split('/').pop());if(init.method&&init.method!=='GET')daily[key]=body.data;return json({key,data:daily[key]||(key==='valelin_anniv'?[]:{}),updated_at:new Date().toISOString()});}
    if(path==='/app/ob/buckets'||path==='/app/ob/search')return json(memories);
    if(path==='/app/keeps')return json({items:keeps});
    if(path==='/app/command-card')return json({active:command,latest:command});
    if(path.includes('/command-card/')&&path.endsWith('/result')){if(command)command.status=body.outcome;const latest=command;command=null;return json({command:latest});}
    if(path==='/app/ledger')return json({entries:[],items:[]});
    if(path==='/app/notes')return json({notes:[{id:'demo-md',title:'项目笔记.md',name:'项目笔记.md',format:'md',size:90}]});
    if(path.startsWith('/app/notes/'))return json({id:'demo-md',title:'学习笔记',content:'先理解问题。\n写一个最小例子。\n验证后记录结果。',markdown:'先理解问题。\n写一个最小例子。\n验证后记录结果。',format:'md'});
    if(path==='/demo/weather')return json({current_condition:[{temp_C:'20',weatherDesc:[{value:'晴'}]}]});
    if(path==='/previews/catalogue.json')return json({groups:[{title:'预览',items:[{title:'阅读',file:'reading.html'},{title:'专注',file:'focus.html'},{title:'主题',file:'theme.html'}]}]});
    if(path.startsWith('/app/')||path.startsWith('/api/')||path==='/healthz')return json({ok:true,connected:true,enabled:true,provider:'replay',items:[],events:[],sessions:[],models:[],letters:[],messages:[],tools:[],windows:{},count:0});
    // Only same-origin packaged static resources can reach native fetch.
    if(/\.(?:css|js|html|svg|woff2?|ttf|md|txt)$/.test(path))return nativeFetch(new URL(path.slice(1),base),init);
    return json({ok:true,items:[]});
  };
})();
