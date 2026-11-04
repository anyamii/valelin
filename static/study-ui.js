(function () {
  'use strict';
  const C = window.StudyCore;
  const $ = id => document.getElementById(id);
  let state = {raw: null, cards: []}, usable = false, type = 'word';
  let editor = null, deletion = null, importPlan = null, readVersion = 0;
  let reviewId = null, reviewRaw = null, reviewed = 0, flipped = false, audio = null;
  const expanded = new Map(), swallowUntil = new WeakMap();
  const PAGE_TITLE = 'STUDY';
  const HINT_KEY = 'valelin_study_hint_dismissed_v1';
  let detailId = null, press = null, releasedLongPress = null;
  $('studyTitle').textContent = PAGE_TITLE;
  function message(error) {
    if (error.name === 'QuotaExceededError') return 'Browser storage is full. Export a backup before freeing space.';
    if (error.name === 'SecurityError') return 'Local storage is blocked. Allow this site to store data and try again.';
    return error.message || String(error);
  }
  function text(tag, value, className) {
    const node = document.createElement(tag); node.textContent = value;
    if (className) node.className = className;
    return node;
  }
  function front(node, card) {
    node.replaceChildren();
    const focus = card.type === 'sentence' ? card.focus.trim() : '';
    if (!focus) { node.textContent = card.front; return; }
    const lower = card.front.toLowerCase(), needle = focus.toLowerCase();
    let cursor = 0, at;
    while ((at = lower.indexOf(needle, cursor)) >= 0) {
      node.append(document.createTextNode(card.front.slice(cursor, at)), text('mark', card.front.slice(at, at + focus.length)));
      cursor = at + focus.length;
    }
    node.append(document.createTextNode(card.front.slice(cursor)));
  }
  function stopAudio() { if (audio) { audio.pause(); audio = null; } if (window.speechSynthesis) window.speechSynthesis.cancel(); }
  function play(card, output = $('notice')) {
    stopAudio(); output.textContent = '';
    if (card.audio_url) {
      try { audio = new Audio(card.audio_url); audio.play().catch(() => { output.textContent = 'Audio is unavailable. Try again later.'; }); } catch (error) { output.textContent = message(error); }
      return;
    }
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) { output.textContent = 'System speech is unavailable in this browser.'; return; }
    const utterance = new SpeechSynthesisUtterance(card.front);
    utterance.lang = C.settings().accent === 'UK' ? 'en-GB' : 'en-US';
    const voice = speechSynthesis.getVoices().find(item => item.lang.toLowerCase() === utterance.lang.toLowerCase());
    if (voice) utterance.voice = voice;
    utterance.onerror = event => { if (!['canceled', 'interrupted'].includes(event.error)) output.textContent = 'System speech failed. Check your English voice pack.'; };
    speechSynthesis.speak(utterance);
  }
  function open(dialog) { window.ValelinMotion.showSheet(dialog); }
  function render(enter = false) {
    const cards = state.cards;
    const settings = C.settings();
    $('wordCount').textContent = cards.filter(card => card.type === 'word').length;
    $('sentenceCount').textContent = cards.filter(card => card.type === 'sentence').length;
    const queue = C.queue(cards, settings.newLimit, C.daily());
    $('dueSummary').textContent = cards.length ? queue.length + (queue.length === 1 ? ' card due' : ' cards due') : 'No cards yet';
    $('newSummary').textContent = queue.filter(card => card.review.last_reviewed_at).length + ' overdue · ' + queue.filter(card => !card.review.last_reviewed_at).length + ' new';
    $('startReview').disabled = !usable || queue.length === 0;
    const query = $('search').value.trim().toLowerCase();
    const matches = cards.filter(card => card.type === type && [card.front, card.meaning, card.source, card.note, card.focus, card.phonetic].some(value => value.toLowerCase().includes(query)));
    const list = $('cardList'); list.replaceChildren(); list.dataset.type = type;
    $('cardList').setAttribute('aria-labelledby', type + 'Tab');
    if (!matches.length) {
      list.append(text('p', query ? 'No matches. Try another word.' : type === 'word' ? 'Save your first word.' : 'Keep a sentence you love.', 'empty'));
      return;
    }
    for (const card of matches) {
      const article = document.createElement('article'); article.className = 'study-card'; article.dataset.id = card.id;
      const content = document.createElement('div'); content.className = 'card card-content'; content.tabIndex = 0;
      content.setAttribute('role', 'group'); content.setAttribute('aria-label', 'Tap blank space or Enter for meaning. Hold or Shift+Enter for details.');
      const english = text('h2', '', 'card-front'); english.lang = 'en';
      const word = text('span', '', 'speakable'); word.tabIndex = 0; word.setAttribute('role', 'button'); word.dataset.action = 'play';
      word.setAttribute('aria-label', 'Read aloud: ' + card.front); front(word, card); english.append(word); content.append(english);
      if (card.phonetic) content.append(text('p', card.phonetic, 'phonetic'));
      const details = document.createElement('div'); details.className = 'card-details';
      details.hidden = !(expanded.get(card.id) ?? settings.showMeanings);
      details.append(text('p', card.meaning || 'To add', card.meaning ? 'meaning' : 'meaning hint'));
      if (card.note) details.append(text('p', card.note, 'card-note'));
      if (card.source) details.append(text('p', card.source, 'source'));
      const meta = !card.review.last_reviewed_at ? 'New card' : 'Box ' + card.review.box + ' · ' + new Date(card.review.due_at).toLocaleDateString('en-US', {month:'short',day:'numeric'}) + ' due';
      details.append(text('p', meta + (card.added_by === 'Vale' ? ' · Added by Vale' : ''), 'hint')); content.append(details);
      article.append(content); list.append(article);
    }
    if (enter) window.ValelinMotion.cascade(list.children);
  }
  function load(enter = false) {
    try {
      state = C.readCards(); C.settings(); C.daily(); usable = true;
      for (const id of ['addCard', 'openImport', 'exportJson']) $(id).disabled = false;
      render(enter === true); renderHint();
      if ($('detailDialog').open) detail(detailId, false);
    } catch (error) {
      usable = false;
      $('notice').textContent = 'Local data could not be read. Your saved data was not overwritten. ' + message(error);
      for (const id of ['addCard', 'openImport', 'exportJson', 'startReview']) $(id).disabled = true;
    }
  }
  function commit(cards, expectedRaw) {
    C.writeCards(cards, expectedRaw);
    load();
    if (!usable) throw new Error('Saved data could not be read again. Keep this page open and export browser data.');
  }
  function updateTypeFields() {
    const sentence = $('cardType').value === 'sentence';
    $('phoneticField').hidden = sentence; $('focusField').hidden = !sentence;
    $('frontLabel').textContent = sentence ? 'English sentence' : 'Word / phrase';
    $('meaningLabel').textContent = sentence ? 'Meaning (optional)' : 'Meaning (optional)';
  }
  function edit(card) {
    load(); if (!usable) return;
    if (card) {
      card = state.cards.find(item => item.id === card.id);
      if (!card) { $('notice').textContent = 'This card was deleted in another page.'; return; }
    }
    editor = {card: card || null, raw: state.raw};
    const form = $('editorForm'); form.reset(); $('cardType').value = card ? card.type : type;
    for (const field of ['front', 'phonetic', 'meaning', 'focus', 'note', 'source']) form.elements[field].value = card ? card[field] : '';
    $('editorTitle').textContent = card ? 'EDIT CARD' : 'ADD CARD';
    $('editorError').textContent = ''; updateTypeFields();
    open($('editorDialog'));
  }
  $('addCard').addEventListener('click', () => edit(null));
  $('cardType').addEventListener('change', updateTypeFields);
  $('editorForm').addEventListener('submit', event => {
    event.preventDefault();
    try {
      const form = event.currentTarget, values = Object.fromEntries(new FormData(form)), nextType = values.type;
      const card = editor.card ? {...editor.card, type: nextType, front: values.front.trim(), phonetic: nextType === 'word' ? values.phonetic.trim() : '', meaning: values.meaning.trim(), focus: nextType === 'sentence' ? values.focus.trim() : '', source: values.source.trim(), note: values.note.trim(), updated_at: new Date().toISOString()} : C.makeCard(nextType, values);
      C.validateCard(card);
      if (state.cards.some(item => item.id !== card.id && C.identity(item) === C.identity(card))) throw new Error('A card with the same type and English already exists.');
      const cards = editor.card ? state.cards.map(item => item.id === card.id ? card : item) : state.cards.concat(card);
      commit(cards, editor.raw); $('editorDialog').close(); $('notice').textContent = 'Card saved.';
    } catch (error) { $('editorError').textContent = message(error); }
  });
  function detail(id, show = true, source = null) {
    const card = state.cards.find(item => item.id === id);
    if (!card) { if ($('detailDialog').open) $('detailDialog').close(); $('notice').textContent = 'This card was deleted in another page.'; return; }
    detailId = id; front($('detailFront'), card); $('detailFront').setAttribute('aria-label', 'Read aloud: ' + card.front);
    $('detailPhonetic').textContent = card.phonetic; $('detailMeaning').textContent = card.meaning || 'To add'; $('detailNote').textContent = card.note || '—';
    $('detailReview').textContent = !card.review.last_reviewed_at ? 'New · not reviewed yet' : 'Box ' + card.review.box + ' · Next · ' + new Date(card.review.due_at).toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'}) + ' · Missed · ' + card.review.lapses;
    const related = C.relatedSentences(card, state.cards); $('detailSentences').hidden = !related.length; $('relatedSentences').replaceChildren();
    for (const sentence of related) { const node = text('article', '', 'card related-sentence'), en = text('p', '', 'card-front'); en.lang = 'en'; front(en, sentence); node.append(en); if (sentence.source) node.append(text('p', sentence.source, 'source')); $('relatedSentences').append(node); }
    $('detailError').textContent = ''; if (show) open($('detailDialog'));
  }
  function showDetails(row) { const id=row.dataset.id; load(); if (usable) detail(id,true,Array.from($('cardList').children).find(node=>node.dataset.id===id)?.firstElementChild); }
  function deleteCard(card) {
    load(); if (!usable) return; card = state.cards.find(item => item.id === card.id); if (!card) return;
    deletion = {card, raw: state.raw}; $('deleteFront').textContent = card.front; $('deleteError').textContent = ''; open($('deleteDialog'));
  }
  $('detailFront').addEventListener('click', () => { const card = state.cards.find(item => item.id === detailId); if (card) play(card, $('detailError')); });
  $('detailFront').addEventListener('keydown', event => { if (['Enter',' '].includes(event.key)) { event.preventDefault(); $('detailFront').click(); } });
  $('editDetail').addEventListener('click', () => { const card = state.cards.find(item => item.id === detailId); $('detailDialog').close(); if (card) edit(card); });
  $('deleteDetail').addEventListener('click', () => { const card = state.cards.find(item => item.id === detailId); $('detailDialog').close(); if (card) deleteCard(card); });
  function activateList(event) {
    const row = event.target.closest('article'); if (!row) return;
    if (Date.now() < (swallowUntil.get(row) || 0)) { event.preventDefault(); return; }
    const card = state.cards.find(item => item.id === row.dataset.id); if (!card) return;
    if (event.target.closest('[data-action="play"]')) { play(card); return; }
    const details = row.querySelector('.card-details'); details.hidden = !details.hidden; expanded.set(card.id, !details.hidden);
  }
  $('cardList').addEventListener('click', activateList);
  $('cardList').addEventListener('keydown', event => {
    const row = event.target.closest('article'); if (!row) return;
    if ((event.shiftKey && event.key === 'Enter') || event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) { event.preventDefault(); showDetails(row); }
    else if (['Enter',' '].includes(event.key)) { event.preventDefault(); activateList(event); }
  });
  function cancelPress(suppress = false) {
    if (!press) return; clearTimeout(press.timer);
    if (suppress || press.fired || press.moved) swallowUntil.set(press.row, Date.now() + 600);
    press = null;
  }
  $('cardList').addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0) { cancelPress(true); return; }
    const row = event.target.closest('article'); if (!row) return; cancelPress();
    const current = {row, x:event.clientX, y:event.clientY, id:event.pointerId, fired:false, moved:false}; press = current;
    current.timer = setTimeout(() => { if (press !== current) return; current.fired = true; swallowUntil.set(row, Date.now() + 800); showDetails(row); }, 500);
  });
  document.addEventListener('pointermove', event => {
    if (!press || event.pointerId !== press.id) return;
    if (Math.hypot(event.clientX - press.x, event.clientY - press.y) > 8) { press.moved = true; clearTimeout(press.timer); }
  }, {passive:true});
  document.addEventListener('pointerdown', () => { releasedLongPress = null; }, true);
  document.addEventListener('pointerup', event => {
    if (press?.fired) releasedLongPress = {id: event.pointerId, until: Date.now() + 400};
    cancelPress();
  });
  // A modal can retarget the release click to its backdrop or a detail button.
  document.addEventListener('click', event => {
    const released = releasedLongPress; releasedLongPress = null;
    if (released && event.detail > 0 && Date.now() < released.until && (event.pointerId === undefined || event.pointerId === released.id)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  document.addEventListener('pointercancel', () => cancelPress(true));
  $('cardList').addEventListener('contextmenu', event => { const row = event.target.closest('article'); if (row) { event.preventDefault(); if (!$('detailDialog').open) showDetails(row); } });
  function renderHint() { $('firstHint').hidden = localStorage.getItem(HINT_KEY) === 'true'; }
  $('dismissHint').addEventListener('click', () => { try { localStorage.setItem(HINT_KEY, 'true'); renderHint(); } catch (error) { $('notice').textContent = message(error); } });
  function applySearchMode(show) {
    $('searchArea').hidden = !show; $('typeTabs').hidden = show; $('toggleSearch').setAttribute('aria-expanded', String(show)); $('toggleSearch').setAttribute('aria-label', show ? 'Close search' : 'Open search');
    if (show) $('search').focus(); else { $('search').value = ''; if (usable) render(); $('toggleSearch').focus(); }
  }
  function searchMode(show) { return window.ValelinMotion.morph(show ? $('toggleSearch') : $('searchArea'),show ? $('searchArea') : $('toggleSearch'),()=>applySearchMode(show)); }
  $('toggleSearch').addEventListener('click', () => searchMode($('searchArea').hidden));
  $('search').addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); searchMode(false); } });
  $('confirmDelete').addEventListener('click', () => {
    try { commit(state.cards.filter(card => card.id !== deletion.card.id), deletion.raw); $('deleteDialog').close(); $('notice').textContent = 'Card deleted.'; } catch (error) { $('deleteError').textContent = message(error); }
  });
  document.querySelectorAll('.tabs [data-type]').forEach(button => button.addEventListener('click', () => {
    const changed = type !== button.dataset.type;
    type = button.dataset.type;
    document.querySelectorAll('.tabs [data-type]').forEach(tab => { tab.setAttribute('aria-selected', String(tab === button)); tab.setAttribute('aria-pressed', String(tab === button)); });
    if (usable) render(changed);
  }));
  $('search').addEventListener('input', () => { if (usable) render(); });
  $('exportJson').addEventListener('click', () => {
    try {
      const cards = C.readCards().cards;
      const url = URL.createObjectURL(new Blob([JSON.stringify(cards, null, 2)], {type: 'application/json;charset=utf-8'}));
      const link = document.createElement('a'); link.href = url; link.download = 'valelin-study-' + C.dayKey() + '.json'; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000); $('backupNotice').textContent = 'Exported ' + cards.length + ' cards, including review state.';
    } catch (error) { $('notice').textContent = message(error); }
  });
  function resetPreview() { readVersion++; importPlan = null; $('importPreview').hidden = true; $('importError').textContent = ''; $('importFile').value = ''; $('fileName').textContent = ''; }
  function importFormat() {
    resetPreview(); const json = $('importFormat').value === 'json';
    $('importTypeField').hidden = json; $('importFile').accept = json ? '.json,application/json' : '.txt,text/plain';
    $('importHint').textContent = json ? 'Import a card array. Matching IDs update the whole card, including review state. Duplicate English with different IDs is skipped. Export a backup first.' : 'One card per line: Tab, spaced |, or ｜. Words also accept the first comma; sentences keep commas. Use UTF-8 TXT.';
  }
  $('openImport').addEventListener('click', () => { $('settingsDialog').close(); importFormat(); open($('importDialog')); });
  $('importFormat').addEventListener('change', importFormat);
  $('importType').addEventListener('change', resetPreview);
  $('chooseFile').addEventListener('click', () => $('importFile').click());
  $('importFile').addEventListener('change', async event => {
    const file = event.target.files[0]; resetPreview(); if (!file) return;
    const version = readVersion, format = $('importFormat').value, selectedType = $('importType').value;
    $('fileName').textContent = file.name;
    try {
      let content;
      try { content = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer()); } catch (_) { throw new Error('Invalid UTF-8. Save the file as UTF-8 and import again.'); }
      if (content.includes('\uFFFD')) throw new Error('Replacement characters found. Check file encoding before importing.');
      if (version !== readVersion || !$('importDialog').open) return;
      const snapshot = C.readCards();
      let rows, stats;
      if (format === 'json') {
        const plan = C.planJson(JSON.parse(content.replace(/^\uFEFF/, '')), snapshot.cards);
        importPlan = {format, raw: snapshot.raw, cards: plan.cards}; rows = plan.rows;
        stats = 'Added ' + plan.added + ' · Updated ' + plan.updated + ' · Duplicates ' + plan.duplicates + ' · Total ' + plan.cards.length;
      } else {
        const plan = C.parseTxt(content, selectedType, snapshot.cards);
        importPlan = {format, raw: snapshot.raw, rows: plan.rows, existing: snapshot.cards}; rows = plan.rows;
        stats = plan.totalLines + ' lines · ' + rows.length + ' cards · Skipped ' + plan.skipped + ' · Duplicates ' + plan.duplicates;
      }
      $('importStats').textContent = stats; $('previewRows').replaceChildren();
      for (const row of rows.slice(0, 20)) {
        const li = document.createElement('li'); const en = text('p', row.front); en.lang = 'en'; li.append(en, text('p', row.meaning || 'To add', 'hint')); $('previewRows').append(li);
      }
      $('confirmImport').disabled = rows.length === 0; $('importPreview').hidden = false;
    } catch (error) { if (version === readVersion) { importPlan = null; $('importError').textContent = message(error); } }
  });
  $('confirmImport').addEventListener('click', () => {
    try {
      if (!importPlan) throw new Error('Choose the file again.');
      const cards = importPlan.format === 'json' ? importPlan.cards : importPlan.existing.concat(importPlan.rows.map(row => C.makeCard(row.type, row)));
      commit(cards, importPlan.raw); $('importDialog').close(); $('notice').textContent = 'Imported. ' + cards.length + ' cards in total.';
    } catch (error) { $('importError').textContent = message(error); }
  });
  function nextReview() {
    state = C.readCards(); const list = C.queue(state.cards, C.settings().newLimit, C.daily());
    $('reviewProgress').textContent = 'Reviewed ' + reviewed + ' · Due ' + list.length;
    $('reviewContent').hidden = list.length === 0; $('reviewDone').hidden = list.length !== 0;
    $('reviewError').textContent = ''; reviewId = null;
    if (!list.length) { render(); return; }
    const card = list[0]; C.markShown(card); reviewId = card.id; reviewRaw = state.raw; flipped = false;
    $('reviewKind').textContent = card.type === 'word' ? 'WORD' : 'SENTENCE';
    front($('reviewFront'), card); $('reviewPhonetic').textContent = card.phonetic;
    $('reviewBack').replaceChildren(text('span', card.meaning || 'To add', 'review-meaning'));
    if (card.note) $('reviewBack').append(text('span', card.note, 'hint'));
    $('reviewBack').hidden = true; $('flipHint').hidden = false; $('gradeButtons').hidden = true;
    $('flipCard').setAttribute('aria-label', 'Show meaning');
    $('reviewSource').textContent = card.source; $('reviewFront').setAttribute('aria-label', 'Read aloud: ' + card.front);
    render();
  }
  $('startReview').addEventListener('click', () => {
    try { reviewed = 0; nextReview(); open($('reviewDialog')); } catch (error) { $('notice').textContent = message(error); }
  });
  function flip() {
    flipped = !flipped; $('reviewBack').hidden = !flipped; $('flipHint').hidden = flipped; $('gradeButtons').hidden = !flipped;
    $('flipCard').setAttribute('aria-label', flipped ? 'Show English' : 'Show meaning');
  }
  function reviewSpeak(event) {
    event.stopPropagation(); const card = state.cards.find(item => item.id === reviewId); if (card) play(card, $('reviewError'));
  }
  $('flipCard').addEventListener('click', flip);
  $('reviewFront').addEventListener('click', reviewSpeak);
  $('reviewFront').addEventListener('keydown', event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); reviewSpeak(event); } });
  $('flipCard').addEventListener('keydown', event => { if (event.target === $('flipCard') && ['Enter', ' '].includes(event.key)) { event.preventDefault(); flip(); } });
  function recordReviewDay(date) {
    try {
      const key = 'valelin_study_review_days_v1', history = JSON.parse(localStorage.getItem(key) || '[]');
      if (!Array.isArray(history)) throw new Error('Review days must be an array');
      const now = new Date(); now.setHours(12,0,0,0);
      const today = C.dayKey(now), oldest = new Date(now); oldest.setDate(oldest.getDate() - 399);
      const cutoff = C.dayKey(oldest), day = C.dayKey(date);
      if (day < cutoff || day > today || !Number.isFinite(date.getTime())) throw new Error('Invalid rating day');
      const days = history.filter(value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && value >= cutoff && value <= today && C.dayKey(new Date(value + 'T12:00:00')) === value);
      localStorage.setItem(key,JSON.stringify([...new Set(days.concat(day))].sort()));
    } catch (_) { console.warn('[study] Review saved; day history could not be recorded'); }
  }
  function grade(remembered) {
    try {
      if (!reviewId || !flipped) return;
      const card = state.cards.find(item => item.id === reviewId);
      if (!card) throw new Error('This card changed. Restart the review.');
      commit(state.cards.map(item => item.id === card.id ? C.grade(card, remembered) : item), reviewRaw);
      recordReviewDay(new Date(state.cards.find(item => item.id === card.id).review.last_reviewed_at));
      stopAudio(); reviewed++; nextReview();
    } catch (error) { $('reviewError').textContent = message(error); }
  }
  $('rememberedCard').addEventListener('click', () => grade(true)); $('forgotCard').addEventListener('click', () => grade(false));
  $('openSettings').addEventListener('click', () => {
    try { const settings = C.settings(); $('newLimit').value = settings.newLimit; $('showMeanings').checked = settings.showMeanings; $('accent').value = settings.accent; $('backupNotice').textContent = ''; $('settingsError').textContent = ''; open($('settingsDialog')); } catch (error) { $('notice').textContent = message(error); }
  });
  $('settingsForm').addEventListener('submit', event => {
    event.preventDefault();
    try {
      const newLimit = Number($('newLimit').value);
      if (!Number.isInteger(newLimit) || newLimit < 0 || newLimit > 200) throw new Error('Enter a whole number from 0 to 200.');
      localStorage.setItem(C.SETTINGS_KEY, JSON.stringify({newLimit, showMeanings: $('showMeanings').checked, accent: $('accent').value})); expanded.clear(); render(); $('settingsDialog').close();
    } catch (error) { $('settingsError').textContent = message(error); }
  });
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach(dialog => {
    dialog.addEventListener('close', () => { stopAudio(); if (dialog.id === 'importDialog') resetPreview(); });
    dialog.addEventListener('click', event => { if (event.target === dialog) { const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close(); } });
  });
  window.addEventListener('storage', event => { if ([C.CARDS_KEY, C.SETTINGS_KEY, C.DAILY_KEY, HINT_KEY, null].includes(event.key)) { load(); if ($('reviewDialog').open) { reviewId = null; $('gradeButtons').hidden = true; $('reviewError').textContent = 'Another page changed the data. Close this panel and restart review.'; } } });
  window.addEventListener('pageshow', () => load(true));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); else { cancelPress(true); stopAudio(); } });
  window.addEventListener('pagehide', () => { cancelPress(true); stopAudio(); });
  setInterval(() => { if (usable && !document.querySelector('dialog[open]')) load(); }, 60000);
  load();
})();
