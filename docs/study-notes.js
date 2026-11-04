(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  function inline(text) {
    // Input is already escaped. Only our own fixed markup may be emitted.
    let html = '', at = 0;
    while (at < text.length) {
      const marker = text[at] === '`' ? '`' : text.startsWith('**', at) ? '**' : null;
      const end = marker ? text.indexOf(marker, at + marker.length) : -1;
      if (end > at + marker?.length) {
        const content = text.slice(at + marker.length, end);
        html += marker === '`' ? '<code>' + content + '</code>' : '<strong>' + inline(content) + '</strong>';
        at = end + marker.length;
      } else { html += text[at++]; }
    }
    return html;
  }
  function cells(line) {
    line = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    const result = []; let value = '', code = false;
    for (let i = 0; i < line.length; i++) {
      const char = line[i];
      if (char === '\\' && line[i + 1] === '|') { value += '|'; i++; }
      else if (char === '`') { code = !code; value += char; }
      else if (char === '|' && !code) { result.push(value.trim()); value = ''; }
      else value += char;
    }
    result.push(value.trim()); return result;
  }
  function tableAt(lines, at) {
    if (!lines[at]?.includes('|') || !lines[at + 1]?.includes('|')) return null;
    const head = cells(lines[at]), rule = cells(lines[at + 1]);
    return head.length === rule.length && rule.every(cell => /^:?-{3,}:?$/.test(cell)) ? rule : null;
  }
  function blocks(lines) {
    let html = '', at = 0;
    while (at < lines.length) {
      const line = lines[at];
      if (!line.trim()) { at++; continue; }
      const fence = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
      if (fence) {
        const code = [], marker = fence[1][0], size = fence[1].length; at++;
        while (at < lines.length && !new RegExp('^\\s*' + marker + '{' + size + ',}\\s*$').test(lines[at])) code.push(lines[at++]);
        if (at < lines.length) at++;
        html += '<div class="code-card"><div class="code-card-head">' + (fence[2].trim() || 'code') + '</div><pre><code>' + code.join('\n') + '</code></pre></div>'; continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) { html += '<h' + heading[1].length + '>' + inline(heading[2]) + '</h' + heading[1].length + '>'; at++; continue; }
      if (/^\s*&gt;\s?/.test(line)) {
        const quote = [];
        while (at < lines.length && /^\s*&gt;\s?/.test(lines[at])) quote.push(lines[at++].replace(/^\s*&gt;\s?/, ''));
        html += '<blockquote>' + blocks(quote) + '</blockquote>'; continue;
      }
      const rule = tableAt(lines, at);
      if (rule) {
        const align = rule.map(cell => cell.endsWith(':') ? cell.startsWith(':') ? 'table-align-center' : 'table-align-right' : '');
        const row = (values, tag) => '<tr>' + rule.map((_, i) => '<' + tag + ' class="' + align[i] + '">' + inline(values[i] || '') + '</' + tag + '>').join('') + '</tr>';
        html += '<div class="table-card" tabindex="0" role="region" aria-label="Table, scroll horizontally"><table><thead>' + row(cells(line), 'th') + '</thead><tbody>'; at += 2;
        while (at < lines.length && lines[at].trim() && lines[at].includes('|')) html += row(cells(lines[at++]), 'td');
        html += '</tbody></table></div>'; continue;
      }
      const list = line.match(/^( *)([-+*]|\d+\.)\s+(.+)$/);
      if (list) {
        const ordered = /\d/.test(list[2]), tag = ordered ? 'ol' : 'ul', indent = list[1].length;
        html += '<' + tag + '>';
        while (at < lines.length) {
          const item = lines[at].match(/^( *)([-+*]|\d+\.)\s+(.+)$/);
          if (!item || item[1].length !== indent || /\d/.test(item[2]) !== ordered) break;
          html += '<li>' + inline(item[3]); at++;
          const nested = [];
          while (at < lines.length && lines[at].trim() && (lines[at].match(/^ */)[0].length > indent)) nested.push(lines[at++].slice(indent + 2));
          if (nested.length) html += blocks(nested);
          html += '</li>';
        }
        html += '</' + tag + '>'; continue;
      }
      const paragraph = [line]; at++;
      while (at < lines.length && lines[at].trim() && !/^(#{1,6}\s|\s*(`{3,}|~{3,}|&gt;|[-+*]\s|\d+\.\s))/.test(lines[at]) && !tableAt(lines, at)) paragraph.push(lines[at++]);
      html += '<p>' + paragraph.map(inline).join('<br>') + '</p>';
    }
    return html;
  }
  function markdown(raw) { return blocks(escape(raw).replace(/\r\n?/g, '\n').split('\n')); }
  function updated(value) {
    const date = new Date(value), minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    if (!Number.isFinite(minutes)) return '';
    return '更新于 ' + (minutes < 1 ? '刚刚' : minutes < 60 ? minutes + ' 分钟前' : date.toLocaleString('zh-CN', {year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}));
  }
  let pending = null, version = 0;
  async function request(path, status, apply) {
    const current = ++version; pending?.abort();
    const controller = new AbortController(); pending = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    status.textContent = '正在读取…';
    try {
      const secret = 'demo-local';
      if (!secret) { status.textContent = '请先在 HOME 登录，再回来阅读。'; return; }
      const response = await fetch(path, {headers:{Authorization:'Bearer ' + secret}, cache:'no-store', redirect:'error', signal:controller.signal});
      if (!response.ok) {
        status.textContent = response.status === 401 ? '登录已失效，请回 HOME 登录。' : response.status === 404 ? '笔记暂不可用，请稍后刷新。' : '暂时无法读取笔记，请重试。'; return;
      }
      const data = await response.json();
      if (current !== version) return;
      apply(data); status.textContent = '';
    } catch (_) { if (current === version) status.textContent = '读取未完成，请检查网络后刷新。'; }
    finally { clearTimeout(timeout); if (pending === controller) pending = null; }
  }
  function pullRefresh(refresh) {
    let start = null;
    document.addEventListener('touchstart', event => { start = scrollY <= 0 && event.touches.length === 1 ? {x:event.touches[0].clientX,y:event.touches[0].clientY} : null; }, {passive:true});
    document.addEventListener('touchend', event => {
      if (start && event.changedTouches.length) { const touch = event.changedTouches[0]; if (touch.clientY - start.y > 70 && Math.abs(touch.clientX - start.x) < 40 && scrollY <= 0) refresh(); }
      start = null;
    }, {passive:true});
    document.addEventListener('touchcancel', () => { start = null; }, {passive:true});
  }
  const selectionKey = 'valelin_study_section';
  function remember(value) { try { sessionStorage.setItem(selectionKey, value); } catch (_) {} }
  if ($('notesPanel')) {
    let active = false;
    function load() {
      if (!active) return;
      const list = $('notesList'); list.replaceChildren();
      request('/app/notes', $('notesStatus'), data => {
        for (const note of data.notes || []) {
          const link = document.createElement('a'); link.className = 'card note-link'; link.href = 'study-note.html?id=' + encodeURIComponent(note.id);
          const title = document.createElement('h2'); title.textContent = note.title;
          const meta = document.createElement('p'); meta.className = 'hint'; meta.textContent = updated(note.modified_at) + ' · ' + note.word_count + ' 字';
          link.append(title, meta); list.append(link);
        }
        if (!list.children.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '还没有可读笔记。'; list.append(empty); }
        window.ValelinMotion.cascade(list.children);
      });
    }
    function select(notes) {
      active = notes; remember(notes ? 'notes' : 'cards');
      $('notesPanel').hidden = !notes; $('cardList').hidden = notes; $('addCard').hidden = notes; $('toggleSearch').hidden = notes;
      $('firstHint').hidden = notes || localStorage.getItem('valelin_study_hint_dismissed_v1') === 'true';
      if (notes) { $('searchArea').hidden = true; $('typeTabs').hidden = false; }
      for (const tab of document.querySelectorAll('#typeTabs button')) {
        const selected = notes ? tab === $('notesTab') : tab.dataset.type === $('cardList').dataset.type;
        tab.setAttribute('aria-selected', String(selected)); tab.setAttribute('aria-pressed', String(selected));
      }
      if (notes) load(); else { version++; pending?.abort(); }
    }
    $('notesTab').addEventListener('click', () => select(true));
    document.querySelectorAll('#typeTabs [data-type]').forEach(tab => tab.addEventListener('click', () => select(false)));
    window.addEventListener('pageshow', load);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
    window.addEventListener('storage', event => { if (event.key === null || event.key === 'companion_secret') load(); });
    pullRefresh(load);
    try { if (sessionStorage.getItem(selectionKey) === 'notes') select(true); } catch (_) {}
  }
  if ($('noteBody')) {
    const id = new URL(location.href).searchParams.get('id');
    remember('notes');
    function load() {
      $('noteBody').replaceChildren(); $('noteTitle').textContent = ''; $('noteModified').textContent = '';
      if (!id) { $('noteStatus').textContent = '未找到笔记。'; return; }
      request('/app/notes/' + encodeURIComponent(id), $('noteStatus'), note => {
        $('noteTitle').textContent = note.title; document.title = note.title + ' · NOTES';
        $('noteModified').textContent = updated(note.modified_at);
        $('noteBody').innerHTML = markdown(note.markdown);
        window.ValelinMotion.cascade([$('noteBody')]);
      });
    }
    $('notesBack').addEventListener('click', event => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      try { const ref = new URL(document.referrer); if (ref.origin === location.origin && ref.pathname === 'study.html' && history.length > 1) { event.preventDefault(); history.back(); } } catch (_) {}
    });
    window.addEventListener('pageshow', load);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
    window.addEventListener('storage', event => { if (event.key === null || event.key === 'companion_secret') load(); });
    pullRefresh(load);
  }
})();
