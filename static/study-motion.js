/* One motion layer for index-next and STUDY. No model calls or card-data writes. */
(function () {
  'use strict';
  const reduced = matchMedia('(prefers-reduced-motion:reduce)');
  const active = new Map();
  const excluded = '.home-top,.head,.menu-hero,.today-row,.msg,.message,.bubble';
  const clickable = 'button,a[href],[role="button"],.card-content,.keep-card,.home-tile,.settings-card[tabindex],.cal-card[tabindex]';
  const token = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue('--motion-' + name).trim() || fallback;
  const ms = (name, fallback) => parseFloat(token(name, fallback));
  function settle(panel, offset) {
    const duration=ms('sheet','360'),mass=ms('spring-mass','1'),stiffness=ms('spring-stiffness','320'),damping=ms('spring-damping','30');
    const omega=Math.sqrt(stiffness/mass),decay=damping/(2*mass),frequency=Math.sqrt(Math.max(0,omega*omega-decay*decay));
    const frames=Array.from({length:25},(_,i)=>{
      const time=i/24*duration/1000;
      const displacement=frequency>0?Math.exp(-decay*time)*(Math.cos(frequency*time)+decay/frequency*Math.sin(frequency*time)):Math.exp(-omega*time)*(1+omega*time);
      return {transform:'translateY('+(i===24?0:offset*displacement)+'px)'};
    });
    return animate(panel,frames,{duration,easing:'linear'});
  }
  function animate(node, frames, options = {}) {
    if (!node || reduced.matches || typeof node.animate !== 'function') return null;
    active.get(node)?.cancel();
    node.classList.add('motion-running');
    const animation = node.animate(frames, {duration:ms('duration', '500'),easing:token('ease','ease-out'),...options});
    active.set(node, animation);
    const done = () => { if (active.get(node) === animation) { active.delete(node); node.classList.remove('motion-running'); } };
    animation.finished.then(done, done);
    return animation;
  }
  let press = null;
  function release(cancel = false) {
    if (!press) return;
    const {node} = press; press = null; node.classList.remove('motion-pressed');
    active.get(node)?.cancel();
    if (!cancel) animate(node,[{scale:token('scale','.96')},{scale:token('overshoot','1.015'),offset:.72},{scale:'1'}],{duration:ms('release','340')});
  }
  document.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0 || reduced.matches) return;
    const node = event.target.closest(clickable);
    if (!node || node.matches(':disabled,[aria-disabled="true"]') || node.closest(excluded)) return;
    release(true); press = {node,x:event.clientX,y:event.clientY,id:event.pointerId};
    node.classList.add('motion-pressed');
    animate(node,[{scale:'1'},{scale:token('scale','.96')}],{duration:ms('press','120'),easing:'ease-out'});
  },{passive:true});
  document.addEventListener('pointermove', event => { if (press && Math.hypot(event.clientX-press.x,event.clientY-press.y)>10) release(true); },{passive:true});
  document.addEventListener('pointerup', () => release(),true);
  document.addEventListener('pointercancel', () => release(true),true);
  window.addEventListener('blur', () => release(true));
  reduced.addEventListener('change', () => { if(reduced.matches) { release(true); for(const animation of active.values()) animation.cancel(); } });
  const cascading = new WeakMap();
  function cascade(nodes, options = {}) {
    let index = 0;
    for (const node of nodes) {
      if (index >= (options.limit ?? 12)) break;
      if (node.hidden || node.closest(excluded) || !node.getClientRects().length || getComputedStyle(node).display === 'none') continue;
      node.classList.add('motion-cascade');
      const animation = animate(node,[{opacity:0,transform:'translateY('+(options.distance ?? 28)+'px)'},{opacity:1,transform:'translateY(0)'}],{duration:options.duration ?? ms('cascade','700'),delay:index++ * (options.gap ?? ms('cascade-gap','70')),easing:token('settle','ease-out')});
      cascading.set(node,animation);
      const clear = () => { if (cascading.get(node) === animation) { cascading.delete(node);node.classList.remove('motion-cascade'); } };
      if (animation) animation.finished.then(clear,clear); else clear();
    }
  }
  const sheets = new Map();
  let sheetOrder = 0, historyQueued = false, historyClosing = false, sheetDrag = null, dragClickUntil = 0;
  document.addEventListener('click',event=>{if(performance.now()<dragClickUntil){event.preventDefault();event.stopImmediatePropagation();dragClickUntil=0;}},true);
  const topSheet = () => [...sheets.values()].filter(item => item.open && !item.closing).sort((a,b)=>b.order-a.order)[0];
  function syncHistory() {
    if (historyQueued) return; historyQueued = true;
    queueMicrotask(() => {
      historyQueued = false; if (historyClosing) return;
      const top = topSheet(), id = top?.history ? top.panel.id : null;
      if (id) {
        if (history.state?.motionSheet === id) return;
        const state = {...history.state,motionSheet:id};
        try { if (history.state?.motionSheet) history.replaceState(state,''); else history.pushState(state,''); }
        catch (_) { console.warn('[motion] Sheet browser-back entry unavailable'); }
      } else if (history.state?.motionSheet) { historyClosing = true; history.back(); }
    });
  }
  function bindSheet(panel, options = {}) {
    if (!panel || sheets.has(panel)) return sheets.get(panel);
    const host = options.host || panel;
    const nativeClose = panel instanceof HTMLDialogElement ? panel.close.bind(panel) : null;
    const item = {panel,host,history:options.history !== false,open:false,closing:false,finishing:false,order:0,
      handles:options.handles || '.handle,.sheet-heading,.detail-heading',
      isOpen:options.isOpen || (()=>panel.open),close:options.close || (()=>nativeClose(...(item.closeArgs || []))),focus:null,originalHeight:panel.style.height,
      dismiss:options.dismiss || '[data-close]'};
    sheets.set(panel,item);
    if (nativeClose) panel.close = (...args) => {
      if (item.finishing || !panel.open) return nativeClose(...args);
      item.closeArgs = args; requestClose(item);
    };
    const refresh = () => {
      const open = !!item.isOpen(); if (item.open === open) return;
      item.open = open; item.closing = false;
      if (open) {
        item.order = ++sheetOrder; item.focus ||= document.activeElement;
        panel.classList.add('motion-sheet-open'); panel.dataset.motionSnap = 'auto';
        if(!panel.classList.contains('motion-morph-target'))animate(panel,[{transform:'translateY(100%)'},{transform:'translateY(0)'}],{duration:ms('sheet-open','520'),easing:token('settle','ease-out')});
      } else {
        item.exitAnimation?.cancel();item.exitAnimation = null;
        active.get(panel)?.cancel();panel.classList.remove('motion-sheet-open','motion-dragging');
        panel.style.height = item.originalHeight;panel.style.removeProperty('translate');
        item.focus?.focus?.({preventScroll:true});item.focus = null;
        item.closeArgs = null;
      }
      syncHistory();
    };
    new MutationObserver(refresh).observe(host,{attributes:true,attributeFilter:['class','open','hidden']});
    if (host !== panel) new MutationObserver(refresh).observe(panel,{attributes:true,attributeFilter:['class','hidden']});
    panel.addEventListener('close', refresh);
    panel.addEventListener('cancel',event=>{event.preventDefault();requestClose(item);});
    item.refresh = refresh;refresh();return item;
  }
  function requestClose(item, offset) {
    if (!item || item.closing || !item.open) return;
    item.closing = true;
    let finished = false;
    const finish = () => {
      if (finished || !item.closing) return; finished = true; item.finishing = true;
      try { item.close(); item.refresh(); } finally { item.finishing = false; }
    };
    const start = offset === undefined ? getComputedStyle(item.panel).transform : 'translateY('+offset+'px)';
    const end = offset > item.panel.getBoundingClientRect().height ? 'translateY('+offset+'px)' : 'translateY(100%)';
    const animation = animate(item.panel,[{transform:start === 'none' ? 'translateY(0)' : start},{transform:end}],{duration:ms('sheet-close','380'),easing:'cubic-bezier(.4,0,1,1)',fill:'forwards'});
    item.exitAnimation = animation;
    if (animation) animation.finished.then(finish,finish); else finish();
  }
  function showSheet(panel) {
    const item = bindSheet(panel); item.focus = document.activeElement;
    panel.showModal();item.refresh();
  }
  window.addEventListener('popstate', event => {
    historyClosing = true;
    for (const item of sheets.values()) if (item.open && item.history && event.state?.motionSheet !== item.panel.id) requestClose(item);
    queueMicrotask(()=>{historyClosing=false;});
  });
  window.addEventListener('keydown', event => {
    const top = topSheet();
    if (event.key === 'Escape' && top) { event.preventDefault();event.stopImmediatePropagation();requestClose(top); }
  },true);
  document.addEventListener('click', event => {
    const item = [...sheets.values()].filter(item=>item.open).sort((a,b)=>b.order-a.order)[0];
    if (!item || item.finishing) return;
    const target = event.target;
    const rect = item.panel.getBoundingClientRect();
    const outside = target === item.panel && item.panel instanceof HTMLDialogElement &&
      (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom);
    if (outside || target.closest(item.dismiss) && (item.panel.contains(target) || item.scrim?.contains(target))) {
      event.preventDefault();event.stopImmediatePropagation();requestClose(item);
    } else if (item.scrim?.contains(target)) {
      event.preventDefault();event.stopImmediatePropagation();requestClose(item);
    }
  },true);
  document.addEventListener('pointerdown', event => {
    const item = topSheet(); if (!item || !event.isPrimary || event.button !== 0 || event.target.closest('button,input,textarea,select,a')) return;
    const handle = event.target.closest(item.handles);if(!handle || !item.panel.contains(handle)) return;
    event.preventDefault();event.stopImmediatePropagation();release(true);active.get(item.panel)?.cancel();
    sheetDrag = {item,id:event.pointerId,x:event.clientX,y:event.clientY,lastY:event.clientY,lastAt:performance.now(),velocity:0,height:item.panel.getBoundingClientRect().height,dy:0};
    item.panel.classList.add('motion-dragging','motion-running');item.panel.setPointerCapture(event.pointerId);
  },true);
  document.addEventListener('pointermove', event => {
    const drag = sheetDrag;if(!drag || drag.id!==event.pointerId)return;
    event.preventDefault();event.stopImmediatePropagation();
    const now = performance.now();drag.velocity=(event.clientY-drag.lastY)/Math.max(1,now-drag.lastAt);drag.lastY=event.clientY;drag.lastAt=now;
    drag.dy=event.clientY-drag.y;
    const full=(visualViewport?.height || innerHeight)*.85, upper=drag.height-full;
    const offset=drag.dy<upper?upper+(drag.dy-upper)*.18:drag.dy;
    if(!reduced.matches)drag.item.panel.style.translate='0 '+offset+'px';
  },{capture:true,passive:false});
  function endSheetDrag(event) {
    const drag=sheetDrag;if(!drag || drag.id!==event.pointerId)return;
    event.preventDefault();event.stopImmediatePropagation();sheetDrag=null;
    const {item}=drag,panel=item.panel;if(Math.abs(drag.dy)>8)dragClickUntil=performance.now()+550;panel.classList.remove('motion-dragging','motion-running');
    if(panel.hasPointerCapture(event.pointerId))panel.releasePointerCapture(event.pointerId);
    const offset=parseFloat(panel.style.translate.split(' ')[1])||0;panel.style.removeProperty('translate');
    if(event.type!=='pointerup'){settle(panel,offset);return;}
    const full=(visualViewport?.height || innerHeight)*.85,half=(visualViewport?.height || innerHeight)*.5;
    const adaptive=drag.height<half-20,velocity=performance.now()-drag.lastAt<100&&Math.abs(drag.dy)>55?drag.velocity:0;
    const projected=drag.height-drag.dy-velocity*180;
    const anchors=adaptive?[0,drag.height]:[0,half,full];
    const target=Math.abs(drag.dy)<55?drag.height:anchors.reduce((best,value)=>Math.abs(value-projected)<Math.abs(best-projected)?value:best);
    if(target===0){requestClose(item,offset);return;}
    // Layout changes once at the settled anchor; only transform interpolates.
    panel.style.height=target+'px';panel.dataset.motionSnap=adaptive?'auto':Math.abs(target-half)<1?'half':'full';
    settle(panel,offset+target-drag.height);
  }
  for(const name of ['pointerup','pointercancel','lostpointercapture']) document.addEventListener(name,endSheetDrag,true);
  function scanSheets() {
    document.querySelectorAll('dialog.sheet').forEach(panel=>bindSheet(panel));
    const adapters = [
      ['#subPage .subpage-card','#subPage','.subpage-handle,.subpage-top','#subPageScrim',false],
      ['#keepFilterSheet',null,'.keep-sheet-grab,.keep-sheet-head','#keepScrim'],
      ['#keepTagSheet',null,'.keep-sheet-grab,.keep-sheet-head','#keepScrim'],
      ['#dpSheet','#dpOverlay','.dp-head','#dpClose'],
      ['#thoughtSheet',null,'.thought-sheet-head','#thoughtScrim'],
      ['#askSheet',null,'.thought-sheet-head','#askScrim']
    ];
    for(const [selector,hostSelector,handles,closeSelector,history] of adapters){
      const panel=document.querySelector(selector);if(!panel || sheets.has(panel))continue;
      const host=hostSelector?document.querySelector(hostSelector):panel;
      const hiddenBased=panel.matches('.thought-sheet');
      const item = bindSheet(panel,{host,handles,history,
        dismiss: selector.startsWith('#keep') ? '[data-keep-close]' : selector === '#dpSheet' ? '#dpClose' : selector.startsWith('#subPage') ? '#subPageBack' : '[data-close]',
        isOpen:()=>hiddenBased?!panel.hidden:host.classList.contains('open'),close:()=>{
        if(selector.startsWith('#subPage') && window.closeSubPage){window.closeSubPage();if(globalThis.history.state?.subpage)globalThis.history.back();}
        else document.querySelector(closeSelector)?.click();
      }});
      item.scrim = document.querySelector(selector === '#dpSheet' ? '#dpScrim' : closeSelector);
    }
    document.querySelectorAll('#keepScrim,#subPageScrim,#thoughtScrim,#askScrim,#ledgerScrim').forEach(node=>node.classList.add('motion-scrim'));
  }
  let transition = null;
  function geometry(from,to) {
    const root=document.documentElement;
    for(const [key,value] of Object.entries({'from-x':from.x+'px','from-y':from.y+'px','from-sx':from.width/to.width,'from-sy':from.height/to.height,'to-x':to.x+'px','to-y':to.y+'px'}))root.style.setProperty('--motion-'+key,value);
  }
  async function morph(source,target,update,opts) {
    if(opts && (opts.hideSource || opts.fit === 'cover' || opts.crossfade === false)) return morphWithOptions(source,target,update,opts);
    if(!source || !target || reduced.matches){await update();return;}
    if(transition){const previous=transition;previous.skipTransition();await previous.finished.catch(()=>{});}
    active.get(source)?.cancel();
    const from=source.getBoundingClientRect();if(!from.width || !from.height){await update();return;}
    const root=document.documentElement,oldSourceName=source.style.viewTransitionName,oldTargetName=target.style.viewTransitionName;
    target.classList.add('motion-morph-target');
    const cleanup=()=>{source.style.viewTransitionName=oldSourceName;target.style.viewTransitionName=oldTargetName;target.classList.remove('motion-morph-target');root.classList.remove('motion-transition');};
    if(typeof document.startViewTransition==='function') {
      source.style.viewTransitionName='motion-element';root.classList.add('motion-transition');
      let updated=false;
      const apply=async()=>{updated=true;await update();source.style.viewTransitionName=oldSourceName;target.style.viewTransitionName='motion-element';geometry(from,target.getBoundingClientRect());};
      try {
        const current=document.startViewTransition(apply);transition=current;current.ready.catch(()=>{});
        await current.finished; if(transition===current)transition=null;
      } catch (_) { if(!updated)await update(); }
      finally {cleanup();}
    } else {
      try {
        await update();await new Promise(resolve=>requestAnimationFrame(resolve));
        const to=target.getBoundingClientRect();if(!to.width || !to.height)return;
        const origin=target.style.transformOrigin;target.style.transformOrigin='0 0';
        const animation=animate(target,[{transform:'translate('+(from.x-to.x)+'px,'+(from.y-to.y)+'px) scale('+(from.width/to.width)+','+(from.height/to.height)+')',opacity:.5},{transform:'none',opacity:1}],{duration:ms('duration','500')});
        if(animation)await animation.finished.catch(()=>{});target.style.transformOrigin=origin;
      } finally {cleanup();}
    }
  }
  // Opt-in snapshot policy; the legacy three-argument path above is unchanged.
  async function morphWithOptions(source,target,update,opts) {
    if(!source || !target || reduced.matches){await update();return;}
    if(transition){const previous=transition;previous.skipTransition();await previous.finished.catch(()=>{});}
    active.get(source)?.cancel();
    const from=source.getBoundingClientRect();if(!from.width || !from.height){await update();return;}
    const root=document.documentElement,name='motion-element-options';
    const saved={sourceName:source.style.viewTransitionName,targetName:target.style.viewTransitionName,opacity:source.style.opacity,origin:target.style.transformOrigin};
    const classes=['motion-transition'];
    if(opts.fit === 'cover')classes.push('motion-morph-cover');
    if(opts.hideSource || opts.crossfade === false)classes.push(opts.snapshot === 'old'?'motion-morph-single-old':'motion-morph-single-new');
    target.classList.add('motion-morph-target');
    let current=null,updated=false;
    const apply=async()=>{
      updated=true;await update();
      source.style.viewTransitionName=saved.sourceName;
      if(opts.hideSource)source.style.opacity='0';
      target.style.viewTransitionName=name;
      geometry(from,target.getBoundingClientRect());
    };
    try{
      if(typeof document.startViewTransition === 'function'){
        source.style.viewTransitionName=name;root.classList.add(...classes);
        current=document.startViewTransition(apply);transition=current;current.ready.catch(()=>{});
        await current.finished;
      }else{
        await apply();await new Promise(resolve=>requestAnimationFrame(resolve));
        const to=target.getBoundingClientRect();if(!to.width || !to.height)return;
        const sx=from.width/to.width,sy=from.height/to.height,scale=opts.fit === 'cover'?Math.max(sx,sy):null;
        const x=scale===null?from.x-to.x:from.x+from.width/2-to.x-to.width*scale/2;
        const y=scale===null?from.y-to.y:from.y+from.height/2-to.y-to.height*scale/2;
        target.style.transformOrigin='0 0';
        const animation=animate(target,[{transform:'translate('+x+'px,'+y+'px) scale('+(scale??sx)+','+(scale??sy)+')',opacity:1},{transform:'none',opacity:1}],{duration:ms('duration','500')});
        if(animation)await animation.finished.catch(()=>{});
      }
    }catch(_){if(!updated)await update();}
    finally{
      if(transition===current)transition=null;
      source.style.viewTransitionName=saved.sourceName;target.style.viewTransitionName=saved.targetName;
      source.style.opacity=saved.opacity;target.style.transformOrigin=saved.origin;
      target.classList.remove('motion-morph-target');root.classList.remove(...classes);
    }
  }
  // Only these two documents opt in. Unsupported browsers retain ordinary navigation.
  const studySurface=()=>document.querySelector('#studyHomeCard,.review-summary');
  let clearStudySurface=()=>{};
  function nameStudySurface(node,until) {
    clearStudySurface();
    const previous=node.style.viewTransitionName;
    const clear=()=>{
      if(clearStudySurface!==clear)return;
      node.style.viewTransitionName=previous;clearStudySurface=()=>{};
    };
    clearStudySurface=clear;node.style.viewTransitionName='valelin-study';
    until.then(clear,clear);
  }
  window.addEventListener('pageswap',event=>{
    clearStudySurface();
    if(!event.viewTransition)return;
    event.viewTransition.ready.catch(()=>{});
    if(reduced.matches){event.viewTransition.skipTransition();return;}
    try{
      const destination=new URL(event.activation.entry.url).pathname;
      if(!['/study.html','/index-next.html'].includes(destination)){event.viewTransition.skipTransition();return;}
      const node=studySurface(),rect=node?.getBoundingClientRect();
      if(!rect?.width || !rect.height){event.viewTransition.skipTransition();return;}
      sessionStorage.setItem('valelin-motion-study',JSON.stringify({x:rect.x,y:rect.y,width:rect.width,height:rect.height,at:Date.now(),destination}));
      nameStudySurface(node,event.viewTransition.finished);
    }catch(_){event.viewTransition.skipTransition();}
  });
  window.addEventListener('pagereveal',event=>{
    // A bfcache document can resume before its outgoing finished handler runs.
    clearStudySurface();
    if(!event.viewTransition)return;
    event.viewTransition.ready.catch(()=>{});
    try{
      const saved=JSON.parse(sessionStorage.getItem('valelin-motion-study')||'null');sessionStorage.removeItem('valelin-motion-study');
      const node=studySurface(),to=node?.getBoundingClientRect();
      if(reduced.matches || !saved || Date.now()-saved.at>8000 || saved.destination!==location.pathname || !to?.width){event.viewTransition.skipTransition();return;}
      nameStudySurface(node,event.viewTransition.ready);
      geometry(saved,to);document.documentElement.classList.add('motion-cross');
      const clear=()=>document.documentElement.classList.remove('motion-cross');
      event.viewTransition.finished.then(clear,clear);
    }catch(_){event.viewTransition.skipTransition();}
  });
  document.addEventListener('DOMContentLoaded', () => {
    scanSheets();new MutationObserver(records=>{if(records.some(record=>[...record.addedNodes].some(node=>node.nodeType===1 && node.matches('#thoughtSheet,#askSheet'))))scanSheets();}).observe(document.body,{childList:true});
    for (const [selector,items] of [['#favList','.keep-card'],['#albumList','.keep-grid-cell'],['#profilePanel','.view.on .card,.view.on .settings-card'],['#mindPage','.view.on .card,.view.on > .pulse,.view.on > .search,.view.on > .domains,.view.on > .filters,.view.on > .list'],['#calPage','.us-view.on .card,.us-view.on .cal-card,.us-view.on .td-card,.us-view.on .pd-card,.us-view.on .an-card']]) {
      const root = document.querySelector(selector); if (!root) continue;
      let queued = false, view = null;
      const refresh = () => {
        const showing = root.getBoundingClientRect().width > 0 && getComputedStyle(root).visibility !== 'hidden' && !root.closest('.hidden') && (selector !== '#profilePanel' || root.classList.contains('open'));
        const next = showing ? ['#profilePanel','#mindPage','#calPage'].includes(selector) ? root.querySelector('.view.on,.us-view.on') : root : null;
        if (next && next !== view) cascade(root.querySelectorAll(items));
        view = next;
      };
      new MutationObserver(() => {
        if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; refresh(); }); }
      }).observe(root.closest('.keep-sub') || root,{childList:true,subtree:true,attributes:true,attributeFilter:['class']});
      refresh();
    }
  });
  window.ValelinMotion = {animate,cascade,morph,reduced,token,ms,bindSheet,showSheet,closeSheet:panel=>requestClose(sheets.get(panel)),sheetInventory:()=>[...sheets.values()].map(item=>({id:item.panel.id||item.host.id,history:item.history,open:item.open}))};
})();
