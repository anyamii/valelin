(async () => {
 const $=s=>document.querySelector(s);
 document.querySelectorAll('.heart-link').forEach(x=>x.remove());
 document.querySelectorAll('#viewSeg button').forEach(x=>{if(!['brain','mind'].includes(x.dataset.v))x.remove();});
 document.querySelectorAll('#sitesViewSeg button').forEach(x=>{if(x.dataset.v!=='chat')x.remove();});
 document.querySelectorAll('#sitesViewMood,#moodEditView').forEach(x=>x.remove());
 document.querySelectorAll('#pdRing,#cardHist').forEach(x=>{const card=x.closest('.pd-card');if(card)card.remove();else x.remove();});
 document.querySelectorAll('[data-seg="period"],[data-pane="period"]').forEach(x=>x.remove());
 const b=$('[data-ledger-action="ledger"] span');if(b)b.textContent='番茄钟';
 const workButton=$('[data-ledger-action="window"]');workButton.querySelector('span').textContent='工作窗';
 const sheets=[];
 function sheet(title,id){
  const node=document.createElement('dialog');node.className='home-today-sheet';node.id=id;
  node.innerHTML='<div class="home-today-grip"><div class="handle"></div></div><header class="sheet-heading"><h2></h2><button class="home-today-close" aria-label="关闭">×</button></header><div class="home-today-body" tabindex="0"></div>';
  node.querySelector('h2').textContent=title;document.body.append(node);sheets.push(node);
  node.querySelector('button').onclick=()=>node.close();node.addEventListener('click',e=>{if(e.target===node)node.close();});
  let y;node.querySelector('.home-today-grip').onpointerdown=e=>y=e.clientY;node.querySelector('.home-today-grip').onpointerup=e=>{if(e.clientY-y>40)node.close();};return node;
 }
 const menu=sheet('DEMO · 演示清单','demoSceneSheet');const scenes=await (await fetch('/demo/scenes')).json();
 scenes.push({id:'ask_sheet',prompt:'让它问你'});
 const body=menu.querySelector('.home-today-body');
 let sceneBusy=false;
 const setSceneBusy=value=>{sceneBusy=value;body.querySelectorAll('button').forEach(button=>button.disabled=value);menu.setAttribute('aria-busy',String(value));};
 scenes.forEach(scene=>{const button=document.createElement('button');button.className='clip-mi';button.dataset.scene=scene.id;button.textContent=scene.prompt;button.onclick=async()=>{
  if(sceneBusy)return;
  const previousId=window.valelinDemoLastSent && window.valelinDemoLastSent.id;
  setSceneBusy(true);menu.close();
  try{
   if(scene.id==='ask_sheet'){await window.playDemoAsk();setSceneBusy(false);return;}
   if(scene.id==='upload')handleFiles([new File(['先学 Python\n再做例子\n最后检查结果'], 'learning-notes.md',{type:'text/markdown'})]);
   $('#input').value=scene.prompt;$('#input').dispatchEvent(new Event('input',{bubbles:true}));await doSend();if(mergeQueue.length)await flushMergeQueue();
   const receipt=window.valelinDemoLastSent;
   if(!receipt || receipt.id===previousId || receipt.text!==scene.prompt)throw Error('send not confirmed');
   const streamId='replay-'+receipt.id;
   const timer=setInterval(()=>{
    const reply=chatMessages.find(m=>m.from==='ai'&&m.kind==='reply'&&m.meta.stream_id===streamId&&m.status==='sent');
    const run=agentRuns.get('sdk:'+streamId);
    if(reply && run && run.runStatus==='completed' && (!Array.isArray(reply.meta.segments)||reply.meta.segments.every(s=>s.tw_done)) && !twPendingKeys.has(reply._key)){clearInterval(timer);setSceneBusy(false);}
   },100);
  }catch(error){setSceneBusy(false);showToast('没有发送成功，请再试一次');}
 };body.append(button);});
 const upload=document.createElement('button');upload.className='clip-mi';upload.textContent='选择自己的文件…';upload.onclick=()=>{menu.close();$('#clipInput').click();};body.append(upload);
 document.addEventListener('click',e=>{if(e.target.closest('#clipBtn')){e.preventDefault();e.stopImmediatePropagation();menu.showModal();}},true);
 const work=sheet('WORK · 工作窗','workWindow');
 work.querySelector('.home-today-close').remove();
 const input=$('#input'); input.placeholder='点左边回形针，选一个演示场景'; input.setAttribute('aria-description','点输入框左边的回形针，选一个演示场景');
 const hint=document.createElement('span');hint.id='demoComposerHint';hint.textContent=input.placeholder;hint.setAttribute('aria-hidden','true');input.parentElement.style.position='relative';input.parentElement.append(hint);
 const alignHint=()=>{hint.style.left=input.offsetLeft+'px';hint.style.width=input.offsetWidth+'px';hint.hidden=!!input.value;};
 input.addEventListener('input',alignHint);new ResizeObserver(alignHint).observe(input);alignHint();

 const now=Date.now();
 const baseline=[{name:'文档整理示例',state:'running',start:now-12000},{name:'打包示例',state:'queued',submitted:now-8000},{name:'回归检查示例',state:'queued',submitted:now-3000},{name:'只读冒烟测试',state:'done',duration:108,code:0,summary:['只读检查已完成。','三项资源正常。','退出码 0。']},{name:'浏览器检查示例',state:'error',step:'启动浏览器',reason:'示例阻塞：缺少浏览器，等待处理。'}];
 async function renderWork(){
  if(Date.now()-now>25000 && baseline[0].state==='running'){Object.assign(baseline[0],{state:'done',duration:37,code:0,summary:['文档已整理。','静态检查完成。','退出码 0。']});Object.assign(baseline[1],{state:'running',start:Date.now()});}
  const jobs=[...baseline,...await(await fetch('/demo/jobs')).json()];const target=work.querySelector('.home-today-body');const signature=JSON.stringify(jobs);
  if(target.dataset.signature===signature){target.querySelectorAll('[data-start]').forEach(n=>n.textContent='已开始 '+Math.floor((Date.now()-Number(n.dataset.start))/1000)+' 秒');return;}
  const expanded=new Set([...work.querySelectorAll('details[open]')].map(d=>d.dataset.key));
  target.dataset.signature=signature;target.replaceChildren();let rank=0;
  for(const job of jobs.filter(j=>j.state!=='done')){
   const row=document.createElement('article');row.className='trace-item';const title=document.createElement('b');title.textContent=({running:'在做',queued:'排队',error:'出问题'})[job.state]+' · '+job.name;row.append(title);
   const meta=document.createElement('p');meta.className='note';meta.textContent=job.state==='running'?'已开始 '+Math.floor((Date.now()-job.start)/1000)+' 秒':job.state==='queued'?'第 '+(++rank)+' 个 · 投于 '+new Date(job.submitted).toLocaleTimeString():job.step+' · '+job.reason;if(job.state==='running')meta.dataset.start=job.start;row.append(meta);target.append(row);
  }
  const done=document.createElement('details');done.dataset.key='completed';done.open=expanded.has('completed');const summary=document.createElement('summary');summary.textContent='已完成 · 最近 5 单';done.append(summary);
  for(const job of jobs.filter(j=>j.state==='done').slice(-5).reverse()){
   const detail=document.createElement('details');detail.dataset.key=job.id||job.name;detail.open=expanded.has(detail.dataset.key);const name=document.createElement('summary');name.textContent=job.name;detail.append(name);const p=document.createElement('p');p.textContent='耗时 '+job.duration+' 秒 · 退出码 '+job.code+'\n'+job.summary.join('\n')+'\n'+(job.timeline||[]).join(' → ');detail.append(p);done.append(detail);
  }target.append(done);
 }
 document.addEventListener('click',e=>{if(e.target.closest('[data-ledger-action="window"]')){e.stopImmediatePropagation();$('#ledgerPreviewMoreMenu').hidden=true;renderWork();work.showModal();}},true);
 setInterval(()=>{if(work.open)renderWork();},1000);
 // Reuse the original ledger panel for command history, with its existing cards.
 $('#ledgerTabs')?.remove();$('.lp-ratio')?.remove();$('#ledgerAdd')?.remove();$('#ledgerDue')?.remove();
 const things=$('#keepThings .keep-entry-title');if(things)things.textContent='预览';
})();
