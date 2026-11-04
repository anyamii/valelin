(function () {
  'use strict';
  const label = document.getElementById('studyHomeSummary'), count = document.getElementById('studyHomeCount');
  const layout = document.querySelector('.home-layout'), panel = document.getElementById('menuPanel'), nav = document.getElementById('tabbar');
  if (!label || !count || !layout || !window.StudyCore) { console.warn('[study] HOME entry or StudyCore missing'); return; }
  const C = window.StudyCore, root = document.documentElement;
  const word = document.getElementById('studyHomeWord'), phonetic = document.getElementById('studyHomePhonetic');
  const measure = document.createElement('canvas').getContext('2d');
  function setText(node, value) { if (node.textContent !== value) node.textContent = value; }
  function refreshStreak() {
    let streak = 0;
    try {
      const history = JSON.parse(localStorage.getItem('valelin_study_review_days_v1') || '[]');
      if (!Array.isArray(history)) throw new Error('Review days must be an array');
      const cursor = new Date(); cursor.setHours(12,0,0,0);
      const today = C.dayKey(cursor), oldest = new Date(cursor); oldest.setDate(oldest.getDate() - 399);
      const cutoff = C.dayKey(oldest);
      const days = new Set(history.filter(day => typeof day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(day) && day >= cutoff && day <= today && C.dayKey(new Date(day + 'T12:00:00')) === day));
      if (!days.has(today)) cursor.setDate(cursor.getDate() - 1);
      while (streak < 400 && days.has(C.dayKey(cursor))) { streak++; cursor.setDate(cursor.getDate() - 1); }
    } catch (_) { streak = null; console.warn('[study] Review day history unreadable; streak unavailable'); }
    setText(document.getElementById('studyHomeStreak'),'NO. ' + (streak === null ? '—' : String(streak).padStart(2,'0')));
  }
  function fitWord() {
    const available = word.clientWidth;
    if (!available || !word.textContent) return;
    const style = getComputedStyle(word), maximum = 34, minimum = 12;
    measure.font = style.fontStyle + ' ' + style.fontWeight + ' ' + maximum + 'px ' + style.fontFamily;
    const width = measure.measureText(word.textContent).width;
    const size = Math.max(minimum,Math.min(maximum,Math.floor(maximum * available / Math.max(width,1) * 10) / 10));
    if (word.style.fontSize !== size + 'px') word.style.fontSize = size + 'px';
  }
  function cardPhonetic(card) {
    if (!card) return '';
    const raw = String(card.phonetic).replace(/\p{Script=Han}/gu,'').trim();
    const variants = [...raw.matchAll(/\b(US|UK)\s*:?\s*(\/[^/]+\/|\[[^\]]+\])/gi)];
    return variants.length ? variants.find(match=>match[1].toUpperCase() === C.settings().accent)?.[2] || '' : raw;
  }
  function showWord(card) {
    const next = card ? String(card.front).replace(/\p{Script=Han}/gu,'').trim() : '';
    if (word.textContent !== next) { setText(word,next); word.title = next; fitWord(); }
    setText(phonetic,cardPhonetic(card));
  }
  const music = document.getElementById('homeMusic');
  async function loadMusic() {
    music.hidden=false;music.removeAttribute('href');music.removeAttribute('target');music.setAttribute('role','button');music.tabIndex=0;
    setText(document.getElementById('homeMusicTitle'),'音乐');setText(document.getElementById('homeMusicArtist'),'轻松片刻');setText(document.getElementById('homeMusicNote'),'DEMO');
  }
  const musicHint=e=>{e.preventDefault();e.stopImmediatePropagation();showToast('演示版没有接入音乐',2600);};
  music.addEventListener('click',musicHint,true);
  music.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')musicHint(e);},true);
  let homeActive = false, albumPending = false, albumLoadedAt = 0, inkVersion = 0, inkImage = null, inkSource = '';
  function refreshHomeData() {
    if (!homeVisible() || document.hidden) return;
    refreshMiniState(); loadTrace();
  }
  function refresh() {
    refreshStreak();
    try {
      const {cards} = C.readCards(), queue = C.queue(cards, C.settings().newLimit, C.daily()), due = queue.length;
      const latest = due ? queue[0] : cards.reduce((a,b) => !a || Date.parse(b.created_at) > Date.parse(a.created_at) ? b : a,null);
      showWord(latest);
      setText(count,String(due)); count.hidden = !due;
      setText(label,due ? due + ' CARDS DUE TODAY' : 'ALL CLEAR');
      // daily().seen tracks shown new cards, not completed reviews. grade() stamps this field.
      const reviewed = cards.filter(card => card.review.last_reviewed_at && C.dayKey(new Date(card.review.last_reviewed_at)) === C.dayKey()).length;
      const progress = document.getElementById('studyHomeProgress'), fill = document.getElementById('studyHomeCompleted');
      const total = reviewed + due, percentage = total ? reviewed / total * 100 : 0;
      progress.setAttribute('role','progressbar'); progress.setAttribute('aria-label','Today’s reviewed cards');
      progress.setAttribute('aria-valuemin','0'); progress.setAttribute('aria-valuemax',String(total || 1)); progress.setAttribute('aria-valuenow',String(reviewed));
      progress.setAttribute('aria-valuetext',reviewed + ' reviewed, ' + due + ' due');
      if (fill.style.width !== percentage + '%') fill.style.width = percentage + '%';
    } catch (_) {
      showWord(null); count.hidden = true; setText(label,'OPEN TO CHECK');
      const progress = document.getElementById('studyHomeProgress');
      progress.removeAttribute('role'); ['aria-valuemin','aria-valuemax','aria-valuenow','aria-valuetext'].forEach(name=>progress.removeAttribute(name));
      document.getElementById('studyHomeCompleted').style.width = '0%';
    }
  }
  function homeVisible() { return root.classList.contains('logged-in') && panel.classList.contains('open') && nav.querySelector('[aria-selected="true"]')?.dataset.page === 'pageHome'; }
  const image = document.getElementById('homeAlbumImage'), tile = document.getElementById('homeAlbumCard');
  function clearAlbumImage() { image.hidden = true; image.removeAttribute('src'); tile.classList.remove('has-image'); }
  image.addEventListener('error', clearAlbumImage);
  image.addEventListener('load', () => { if (image.naturalWidth) { image.hidden = false; tile.classList.add('has-image'); } });
  function albumTime(value) {
    const date = new Date(value), now = new Date(); if (!Number.isFinite(date.getTime())) return '';
    return date.toDateString() === now.toDateString() ? String(date.getHours()).padStart(2,'0') + ':' + String(date.getMinutes()).padStart(2,'0') + ' · TODAY' : date.toLocaleDateString('en-US',{month:'short',day:'numeric'}).toUpperCase();
  }
  async function loadAlbum() {
    if (!homeVisible() || albumPending || Date.now() - albumLoadedAt < 60000) return;
    albumPending = true;
    try {
      const response = await fetch(API_BASE + '/app/keeps?view=album&saved_by=all&limit=500', {headers:authHeaders(),cache:'no-store'});
      if (!response.ok) throw new Error('album-unavailable');
      const data = await response.json();
      if (!Array.isArray(data.items)) throw new Error('album-unavailable');
      const items = data.items.filter(item => item && item.media?.items?.some(media => media?.kind === 'image' && media.url));
      items.sort((a,b) => (new Date(b.saved_at || b.message_at).getTime() || 0) - (new Date(a.saved_at || a.message_at).getTime() || 0) || Number(b.id) - Number(a.id));
      const latest = items[0]; clearAlbumImage();
      document.getElementById('homeAlbumText').textContent = latest ? String(latest.display_text || latest.text || '').trim() || 'A saved moment.' : 'Your saved moments.';
      document.getElementById('homeAlbumTime').textContent = latest ? albumTime(latest.message_at || latest.saved_at) : '';
      if (latest) image.src = attUrl(latest.media.items.find(media => media?.kind === 'image' && media.url).url);
      albumLoadedAt = Date.now();
    } catch (_) { clearAlbumImage(); }
    finally { albumPending = false; }
  }
  function openKeepEntry(id) { window.setTab('pageUs'); document.getElementById(id).click(); }
  tile.addEventListener('click', () => openKeepEntry('keepAlbum'));
  const todayOpen = document.getElementById('homeTodayOpen'), todaySheet = document.getElementById('homeTodaySheet');
  const todayList = document.getElementById('homeTodayList'), todayBody = todaySheet.querySelector('.home-today-body');
  const sourceList = document.getElementById('traceList');
  let savedScroll = null, historyPending = false, backdropStart = false;
  function syncTodaySheet() {
    const rows = [...sourceList.children].map(row => {
      const copy = row.cloneNode(true);
      [copy,...copy.querySelectorAll('[data-readable-ink],[style]')].forEach(node => {
        delete node.dataset.readableInk; node.style.removeProperty('color');
      });
      return copy;
    });
    todayList.replaceChildren(...rows);
    setText(document.getElementById('homeTodayDate'),document.getElementById('todayDate').textContent);
  }
  function openTodaySheet(fromHistory = false) {
    if (todaySheet.open || historyPending || !homeVisible()) return;
    syncTodaySheet();
    savedScroll = {home:layout.scrollTop,trace:traceScroll.scrollTop,overflow:layout.style.overflow};
    layout.style.overflow = 'hidden';
    todaySheet.showModal(); todayBody.scrollTop = 0;
    layout.scrollTop = savedScroll.home; traceScroll.scrollTop = savedScroll.trace;
    todayOpen.setAttribute('aria-expanded','true');
    if (!fromHistory) {
      try { history.pushState({...history.state,homeToday:true},''); }
      catch (_) { console.warn('[home] TODAY browser-back entry unavailable'); }
    }
  }
  function closeTodaySheet(fromHistory = false, finish = false) {
    if (!todaySheet.open) return;
    if (!finish) { window.ValelinMotion.closeSheet(todaySheet); return; }
    todaySheet.close(); todaySheet.style.removeProperty('transform');
    todayOpen.setAttribute('aria-expanded','false');
    if (savedScroll) {
      layout.style.overflow = savedScroll.overflow;
      todayOpen.focus({preventScroll:true});
      layout.scrollTop = savedScroll.home; traceScroll.scrollTop = savedScroll.trace;
      savedScroll = null;
    }
    if (!fromHistory && history.state?.homeToday) { historyPending = true; history.back(); }
  }
  todayOpen.addEventListener('click',() => openTodaySheet());
  todaySheet.addEventListener('cancel',event => { event.preventDefault(); closeTodaySheet(); });
  // Capture Escape before the existing HOME handler can close the page underneath.
  window.addEventListener('keydown',event => {
    if (event.key === 'Escape' && todaySheet.open) { event.preventDefault(); event.stopImmediatePropagation(); closeTodaySheet(); }
  },true);
  const outsideSheet = event => { const rect = todaySheet.getBoundingClientRect(); return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom; };
  todaySheet.addEventListener('pointerdown',event => { backdropStart = event.target === todaySheet && outsideSheet(event); });
  todaySheet.addEventListener('click',event => { if (backdropStart && event.target === todaySheet && outsideSheet(event)) closeTodaySheet(); backdropStart = false; });
  window.addEventListener('popstate',event => {
    historyPending = false;
    if (event.state?.homeToday) openTodaySheet(true); else closeTodaySheet(true);
  });
  window.ValelinMotion.bindSheet(todaySheet,{history:false,handles:'.home-today-grip',close:()=>closeTodaySheet(false,true)});
  new MutationObserver(() => { if (todaySheet.open) syncTodaySheet(); }).observe(sourceList,{childList:true,subtree:true,characterData:true});
  const traceScroll = document.getElementById('traceScroll');
  function refreshTraceFade() {
    const room = traceScroll.scrollHeight - traceScroll.clientHeight;
    const top = traceScroll.scrollTop > 1, bottom = room - traceScroll.scrollTop > 1;
    const state = !traceScroll.clientHeight || room <= 1 ? 'none' : top ? bottom ? 'both' : 'top' : bottom ? 'bottom' : 'none';
    if (traceScroll.dataset.homeFade !== state) traceScroll.dataset.homeFade = state;
  }
  traceScroll.addEventListener('scroll',refreshTraceFade,{passive:true});
  new MutationObserver(refreshTraceFade).observe(document.getElementById('traceList'),{childList:true,subtree:true,characterData:true});
  const resized = new ResizeObserver(() => { fitWord(); refreshTraceFade(); });
  resized.observe(word); resized.observe(traceScroll);
  document.fonts.ready.then(fitWord); document.fonts.addEventListener('loadingdone',fitWord);
  function readableInk() {
    const nodes = [...layout.querySelectorAll('.home-bare,.home-brand,.menu-days,.home-now,.trace-head,.trace-list li'),document.getElementById('menuFoot'),nav];
    const copy = '.sig,.num,.lab,.today-clock,.dt,.wx,.corner,.by,.ms-txt,.ms-sub,.card-tt,.trace-empty,.trace-list .w,.trace-list .t,.jot-label,.home-music-title,.home-music-artist,.home-music-note,.home-music-wave';
    nodes.forEach(node => delete node.dataset.readableInk);
    layout.querySelectorAll('.home-bare '+copy.split(',').join(',.home-bare ')).forEach(node => node.style.removeProperty('color'));
    if (!inkImage?.complete || !inkImage.naturalWidth || root.dataset.sitewideBackground !== 'true') return;
    try {
      const style = getComputedStyle(root), parse = value => value.trim().startsWith('#') ? [1,3,5].map(at => parseInt(value.trim().slice(at,at+2),16)) : (value.match(/[\d.]+/g)||[]).map(Number);
      const paper = parse(style.getPropertyValue('--bg')), ink = parse(style.getPropertyValue('--text'));
      const lum = values => values.slice(0,3).reduce((sum,value,i) => { const c=value/255;return sum+(c<=.04045?c/12.92:((c+.055)/1.055)**2.4)*[.2126,.7152,.0722][i]; },0);
      const ratio = (a,b) => (Math.max(a,b)+.05)/(Math.min(a,b)+.05), L=[lum(ink),lum(paper)];
      const canvas = document.createElement('canvas'); canvas.width=32;canvas.height=32;
      const context=canvas.getContext('2d',{willReadFrequently:true}), W=innerWidth,H=innerHeight;
      const scale=Math.max(W/inkImage.naturalWidth,H/inkImage.naturalHeight), sw=W/scale,sh=H/scale,sx=(inkImage.naturalWidth-sw)/2,sy=(inkImage.naturalHeight-sh)/2;
      context.fillStyle=style.getPropertyValue('--bg');context.fillRect(0,0,32,32);context.globalAlpha=1-Number(style.getPropertyValue('--chat-background-dim')||0);
      context.drawImage(inkImage,sx,sy,sw,sh,0,0,32,32);
      const pixels=context.getImageData(0,0,32,32).data;
      for(const node of nodes){let rect=node.getBoundingClientRect();
        const content=node.matches('.trace-list li')?node.querySelector('.w'):null;
        if(content){const range=document.createRange();range.selectNodeContents(content);const textRect=range.getBoundingClientRect();if(textRect.width&&textRect.height)rect=textRect;}
        if(!rect.width||!rect.height)continue;const samples=[];
        for(let y=Math.max(0,Math.floor(rect.top/H*32));y<Math.min(32,Math.ceil(rect.bottom/H*32));y++)for(let x=Math.max(0,Math.floor(rect.left/W*32));x<Math.min(32,Math.ceil(rect.right/W*32));x++){const at=(y*32+x)*4;samples.push(lum(Array.from(pixels.slice(at,at+3))));}
        if(!samples.length)continue;
        let surfaces=samples;
        if(node===nav){const swatch=document.createElement('canvas');swatch.width=swatch.height=1;const coat=swatch.getContext('2d',{willReadFrequently:true});coat.fillStyle=getComputedStyle(nav).backgroundColor;coat.fillRect(0,0,1,1);const color=coat.getImageData(0,0,1,1).data,alpha=color[3]/255;
          // Composite the existing navigation coat over each wallpaper sample.
          const opaque=lum(Array.from(color.slice(0,3)));surfaces=samples.map(surface=>surface*(1-alpha)+opaque*alpha);
        }
        const average=surfaces.reduce((sum,surface)=>sum+surface,0)/surfaces.length;
        const scores=L.map(candidate=>ratio(candidate,average));
        node.dataset.readableInk=scores[0]>=scores[1]?'text':'bg';
        if(node!==nav)for(const text of node.querySelectorAll(copy))text.style.setProperty('color','var(--'+node.dataset.readableInk+')','important');
      }
    } catch (_) { /* Same fallback as StudyAppearance: retain existing theme tokens. */ }
  }
  function refreshInk() {
    const style=getComputedStyle(root),swatch=document.createElement('canvas');swatch.width=swatch.height=1;const ctx=swatch.getContext('2d',{willReadFrequently:true});
    const brightness=token=>{ctx.fillStyle=style.getPropertyValue(token);ctx.fillRect(0,0,1,1);const p=ctx.getImageData(0,0,1,1).data;return p[0]+p[1]+p[2];};
    tile.dataset.albumShade=brightness('--bg')<brightness('--text')?'bg':'text';
    const source = String(typeof chatBackgroundUrl === 'string' ? chatBackgroundUrl : '');
    if (!source || root.dataset.sitewideBackground !== 'true') { inkSource='';inkImage=null;readableInk();return; }
    if (source === inkSource) { readableInk();return; }
    inkSource=source;const current=++inkVersion,img=new Image();inkImage=img;
    img.onload=()=>{if(current===inkVersion)readableInk();};img.onerror=()=>{if(current===inkVersion){inkImage=null;readableInk();}};
    img.src=source;
  }
  function refreshLayout() {
    const visible=homeVisible();nav.dataset.homeSurface=String(visible);
    window.refreshHomeEntrance();
    if(!visible){homeActive=false;return;}
    if(!homeActive){albumLoadedAt=0;loadMusic();}homeActive=true;
    refresh();refreshInk();loadAlbum();refreshTraceFade();
  }
  window.refreshHomeLayout=refreshLayout;window.refreshHomeInk=refreshInk;
  window.addEventListener('home-data-ready',refreshLayout);
  new MutationObserver(refreshLayout).observe(panel,{attributes:true,attributeFilter:['class']});
  new MutationObserver(refreshLayout).observe(nav,{subtree:true,attributes:true,attributeFilter:['aria-selected']});
  new MutationObserver(refreshInk).observe(root,{attributes:true,attributeFilter:['data-theme','data-sitewide-background']});
  new MutationObserver(readableInk).observe(document.getElementById('traceList'),{childList:true});
  window.addEventListener('storage',refreshLayout);window.addEventListener('pageshow',refreshLayout);
  window.addEventListener('resize',readableInk);layout.addEventListener('scroll',readableInk,{passive:true});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshLayout();refreshHomeData();}});
  setInterval(()=>{refreshLayout();refreshHomeData();},60000);refresh();refreshLayout();
})();
