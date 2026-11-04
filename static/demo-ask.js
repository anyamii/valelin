// Local replay: no choices or replies are sent to a server.
(() => {
 const intro='我来整理一份周报，先帮我选一下格式、内容和补充要求。';
 const questions=[
  {header:'格式',question:'周报用什么格式？',options:[{label:'简洁要点',description:'用短句列出重点。'},{label:'表格',description:'按项目、进展和下一步整理。'}],multiSelect:false,allowOther:true},
  {header:'内容',question:'要包含哪几项？',options:[{label:'本周进展'},{label:'遇到的问题'},{label:'下周计划'}],multiSelect:true,allowOther:true},
  {header:'补充',question:'还有什么补充要求？',options:[],multiSelect:false,allowOther:true}
 ];
 const pending=new Map();
 const storageKey='valelin_demo_ask_replays';
 let local=[];
 try{local=JSON.parse(sessionStorage.getItem(storageKey)||'[]');if(!Array.isArray(local))local=[];}catch(_){}
 const remember=(message)=>{
  const index=local.findIndex(m=>m.id===message.id);if(index<0)local.push(message);else local[index]=message;
  local=local.slice(-100);
  try{sessionStorage.setItem(storageKey,JSON.stringify(local));}catch(_){}
  return message;
 };
 // The normal startup history is server-backed. Merge this browser's local replay
 // after startup too, so a late history response cannot replace the local turn.
 const originalStartChat=startChat;
 startChat=function(messages,options){
  const merged=new Map((messages||[]).map(m=>[m.id,m]));
  for(const m of local){
   if(m.meta?.demo_ask?.status==='waiting'&&!pending.has(m.id)){
    const state={status:'canceled',reply:'好，那我先按默认来'};
    m.meta.demo_ask=state;m.text=intro+'\n\n'+state.reply;m.meta.segments=[segment(intro,0),segment(state.reply,1)];remember(m);
   }
   if(msgInActiveSession(m))merged.set(m.id,m);
  }
  return originalStartChat([...merged.values()],options);
 };
 const segment=(text,seq)=>({kind:'text',text,seq,tw_at:text.length,tw_done:true});
 window.demoAskMarker=(m,host,bubbles)=>{
  const state=m.meta.demo_ask;
  const marker=document.createElement('div');marker.className='meta';marker.style.order='0';marker.dataset.demoAskState=state.status;
  if(state.status==='waiting'){
   for(const [label,action] of [['回答问题','open'],['取消','cancel']]){
    const button=document.createElement('button');button.type='button';button.className='thought-open';button.textContent=label;button.onclick=()=>pending.get(m.id)?.[action]();marker.append(button);
   }
  }else marker.textContent=state.status==='canceled'?'已取消':'已回答';
  if(bubbles && bubbles.length){host.insertBefore(marker,bubbles[1]||null);}
  else {
   const body=host.querySelector('.msg-body'),template=body?.querySelector('.bubble');
   if(template){
    const before=template.cloneNode(false);before.innerHTML='<div class="txt">'+renderBubbleText(intro)+'</div>';body.replaceChildren(before,marker);
    if(state.reply){const after=template.cloneNode(false);after.innerHTML='<div class="txt">'+renderBubbleText(state.reply)+'</div>';body.append(after);}
   }else host.append(marker);
  }
 };
 window.playDemoAsk=async()=>{
  flushTypewriters();
  const token=window.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  const stamp=new Date().toISOString(),id='demo-ask-'+token,meta={api_session:effectiveApiSession()||''};
  const human=remember({id:'demo-question-'+token,ts:stamp,from:'human',kind:'user',text:'让它问你',status:'sent',meta});
  setMessage(human,{incremental:true,stickToBottom:true});
  let state={status:'waiting'};
  const update=()=>{
   setMessage(human,{render:false});
   setMessage(remember({id,ts:stamp,from:'ai',kind:'reply',text:intro+(state.reply?'\n\n'+state.reply:''),status:'sent',meta:{...meta,demo_ask:state,segments:[segment(intro,0),...(state.reply?[segment(state.reply,1)]:[])]}}),{stickToBottom:true});
   pendingRowRefreshKeys.add(msgKey(id));scheduleRender({stickToBottom:true});
  };
  update();
  let cancel,opened=false,settled=false,cancelRequested=false;
  const canceled=new Promise(resolve=>cancel=()=>{cancelRequested=true;const sheet=document.getElementById('askSheet');if(sheet&&!sheet.hidden&&sheet._askClose)sheet._askClose();resolve({allow:false});});
  let opening;
  const open=()=>{if(cancelRequested)return Promise.resolve({allow:false});if(opened)return opening;opened=true;return opening=queueAskSheet(()=>settled?{allow:false}:openAskSheet(questions));};
  pending.set(id,{open,cancel});
  try{
   await new Promise(resolve=>setTimeout(resolve,550));
   const result=await Promise.race([open(),canceled]);settled=true;
   if(result&&result.allow){
    const answers=result.answers;
    const summary=questions.map(q=>{const value=answers[q.question];return q.header+'：'+(Array.isArray(value)?value.join('、'):value);}).join('；');
    state={status:'answered',answers,reply:'好，我按你的选择整理周报：'+summary+'。'};
   }else state={status:'canceled',reply:'好，那我先按默认来'};
   update();
  }finally{pending.delete(id);}
 };
})();
