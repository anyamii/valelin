/* Scheduled letters share the existing paper; only delivered mail is browsable. */
(function(){
  'use strict';
  const queue = [], queued = new Set();
  let active = null, busy = false, closing = false;
  const overlay = document.getElementById('letterOverlay');
  if (!overlay){ console.warn('[letters] #letterOverlay missing'); return; }
  const body = document.getElementById('lBody'), date = document.getElementById('lDate');
  const stamp = overlay.querySelector('.l-stamp'), close = document.getElementById('lClose');
  const day = (value = new Date()) => new Intl.DateTimeFormat('sv-SE', {
    timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
  }).format(new Date(value));
  const ready = () => !document.hidden && document.documentElement.classList.contains('logged-in')
    && typeof secret === 'string' && !!secret && !(typeof USE_MOCK !== 'undefined' && USE_MOCK);
  async function request(path, method = 'GET'){
    // /app is the existing authenticated public nginx route; /api aliases stay private.
    const response = await fetch(`${API_BASE}/app/letters${path}`, {
      method, headers:{ Authorization:`Bearer ${secret}` }, cache:'no-store',
    });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }
  function seenKey(occasion){ return occasion === 'period' ? 'valelin_letter_seen_period' : 'valelin_letter_seen'; }
  function seen(occasion){ try { return localStorage.getItem(seenKey(occasion)) === day(); } catch { return false; } }
  function remember(occasion){ if (occasion) try { localStorage.setItem(seenKey(occasion), day()); } catch {} }
  function render(letter, template = false){
    const value = letter.delivered_at || letter.deliver_at || new Date();
    const [year, month, number] = day(value).split('-');
    const monthName = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][Number(month)-1];
    // Keep one date in the existing stamp. The old right-aligned date occupied the same space.
    date.textContent = day(value); date.hidden = true;
    stamp.replaceChildren();
    for (const text of [monthName + ' ' + Number(number), year]){
      const line = document.createElement('span'); line.textContent = text; stamp.appendChild(line);
    }
    body.replaceChildren();
    if (template) body.innerHTML = letter.body.replace(/\n/g, '<br>'); // existing, authored templates only
    else String(letter.text).split(/\r?\n/).forEach((line, index) => {
      if (index) body.appendChild(document.createElement('br'));
      body.appendChild(document.createTextNode(line));
    });
    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden', 'false');
    close.focus({ preventScroll:true });
  }
  async function next(){
    if (active || busy || !ready() || overlay.classList.contains('open')) return;
    busy = true;
    try {
      while (queue.length && ready()){
        const letter = queue[0];
        let result;
        try { result = await request(`/${encodeURIComponent(letter.id)}/delivered`, 'POST'); }
        catch (error){
          if (String(error.message) !== '404') throw error;
          queue.shift(); queued.delete(letter.id); continue; // cancelled after the due lookup
        }
        queue.shift(); queued.delete(letter.id);
        if (result.letter.status === 'read') continue; // another tab already finished it
        if (!ready()) break; // delivered-but-unread is offered again at next foreground check
        active = result.letter;
        remember(active.occasion);
        render(active);
        break;
      }
    } catch (error){
      console.warn('[letters] delivery acknowledgement failed');
    } finally { busy = false; }
  }
  async function hide(){
    if (closing) return;
    closing = true; close.disabled = true;
    try {
      if (active && active.status !== 'read') await request(`/${encodeURIComponent(active.id)}/read`, 'POST');
      active = null;
      overlay.classList.remove('open'); overlay.setAttribute('aria-hidden', 'true');
    } catch { console.warn('[letters] read acknowledgement failed; close again to retry'); }
    finally { closing = false; close.disabled = false; }
    if (!active) { await next(); if (!active) await special(); }
  }
  async function open(letter){
    if (!ready() || active || overlay.classList.contains('open') || busy) return;
    active = letter;
    render(letter);
  }
  async function special(){
    if (!ready() || active || queue.length || busy || overlay.classList.contains('open')) return;
    // Wait for the existing daily-document sync before checking an actual period start.
    if (typeof window.dailySyncNow === 'function') await window.dailySyncNow();
    if (!ready() || active || queue.length || overlay.classList.contains('open')) return;
    const today = new Date(day() + 'T12:00:00');
    const template = typeof window.valelinLetterTemplate === 'function' ? window.valelinLetterTemplate(today) : null;
    const period = typeof window.pdRead === 'function' && window.pdRead().logs.some((log) => log.start === day());
    const occasion = period && !seen('period') ? 'period' : template && !seen('anniversary') ? 'anniversary' : null;
    if (!occasion) return;
    let letters = [];
    try { ({ letters } = await request(`?occasion=${occasion}`)); }
    catch { console.warn('[letters] occasion lookup unavailable; using existing template'); }
    if (active || busy || queue.length || !ready() || overlay.classList.contains('open')) return;
    if (letters.length){ active = letters[0]; render(active); }
    else if (occasion === 'anniversary') render(template, true);
    else if (window.valelinPeriodLetterTemplate) render({body:window.valelinPeriodLetterTemplate()}, true);
    else return;
    remember(occasion);
  }
  let checking = false;
  async function check(){
    if (!ready() || checking) return;
    checking = true;
    try {
      const { letters } = await request('/due');
      for (const letter of letters){
        if (letter.id !== active?.id && !queued.has(letter.id)) { queue.push(letter); queued.add(letter.id); }
      }
      queue.sort((a,b) => a.deliver_at.localeCompare(b.deliver_at) || a.id.localeCompare(b.id));
      await next();
      await special();
    } catch { console.warn('[letters] due lookup unavailable'); await special(); }
    finally { checking = false; }
  }
  close.addEventListener('click', hide);
  overlay.addEventListener('click', (event) => { if (event.target === overlay) hide(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && overlay.classList.contains('open')) { event.preventDefault(); hide(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  window.addEventListener('online', check);
  new MutationObserver(check).observe(document.documentElement, { attributes:true, attributeFilter:['class'] });
  setInterval(check, 60000);
  window.ValelinLetters = { check, open, close:hide, day };

  // This markup exists only in index-next.html. No sealed mail/count is requested.
  const entry = document.getElementById('lettersEntry'), list = document.getElementById('lettersList');
  if (entry && list){
    entry.addEventListener('click', async () => {
      list.hidden = !list.hidden;
      if (list.hidden || !ready()) return;
      list.textContent = '读取中…';
      try {
        const { letters } = await request('');
        list.replaceChildren();
        if (!letters.length) list.textContent = '还没有收到信。';
        for (const letter of letters){
          const row = document.createElement('li'), button = document.createElement('button');
          button.type = 'button'; button.className = 'add';
          button.textContent = day(letter.delivered_at) + ' · ' + (letter.title || letter.text.split(/\r?\n/)[0]);
          button.addEventListener('click', () => open(letter)); row.appendChild(button); list.appendChild(row);
        }
      } catch { list.textContent = '暂时读不到，点信箱重试。'; }
    });
  }
  if (window.location?.hash === '#preview-letter') render({body:'这里会推开一封信。'}, true);
  check();
})();
