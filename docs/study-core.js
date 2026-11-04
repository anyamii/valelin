/* Shared local card format. No network or model calls. */
(function (root) {
  'use strict';
  const CARDS_KEY = 'valelin_study_cards_v1';
  const SETTINGS_KEY = 'valelin_study_settings_v1';
  const DAILY_KEY = 'valelin_study_daily_v1';
  const INTERVALS = Object.freeze([1, 2, 4, 7, 15, 30]);
  const fields = ['front', 'phonetic', 'meaning', 'focus', 'note', 'source', 'audio_url'];
  const identity = card => card.type + '\u0000' + card.front.trim().toLowerCase();
  function dayKey(date = new Date()) {
    return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
  }
  function dueAfter(days, date = new Date()) {
    const next = new Date(date);
    next.setDate(next.getDate() + days);
    next.setHours(0, 0, 0, 0);
    return next.toISOString();
  }
  function validDate(value) { return typeof value === 'string' && value.trim() !== '' && Number.isFinite(Date.parse(value)); }
  function validateCard(card) {
    if (!card || typeof card !== 'object' || Array.isArray(card)) throw new Error('Card must be an object');
    if (typeof card.id !== 'string' || !card.id.trim()) throw new Error('Card is missing a stable ID');
    if (!['word', 'sentence'].includes(card.type)) throw new Error('Invalid card type');
    for (const field of fields) if (typeof card[field] !== 'string') throw new Error('Missing text field: ' + field);
    if (!card.front.trim()) throw new Error('English cannot be empty');
    if (!['Lin', 'Vale'].includes(card.added_by)) throw new Error('Invalid added_by');
    if (!validDate(card.created_at) || !validDate(card.updated_at)) throw new Error('Invalid creation or update time');
    const r = card.review;
    if (!r || !Number.isInteger(r.box) || r.box < 0 || r.box > 6 || !Number.isInteger(r.lapses) || r.lapses < 0 || !validDate(r.due_at) || !(r.last_reviewed_at === null || r.last_reviewed_at === '' || validDate(r.last_reviewed_at))) throw new Error('Invalid review state');
    if (card.audio_url) {
      const url = new URL(card.audio_url, 'https://study.local/');
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid audio URL');
    }
    return JSON.parse(JSON.stringify(card));
  }
  function validateArray(value) {
    if (!Array.isArray(value)) throw new Error('JSON must contain a card array');
    const ids = new Set();
    return value.map((card, i) => {
      let result;
      try { result = validateCard(card); } catch (error) { throw new Error('Card ' + (i + 1) + ': ' + error.message); }
      if (ids.has(result.id)) throw new Error('Duplicate ID in JSON: card ' + (i + 1));
      ids.add(result.id);
      return result;
    });
  }
  function readCards(storage = root.localStorage) {
    const raw = storage.getItem(CARDS_KEY);
    return {raw, cards: raw === null ? [] : validateArray(JSON.parse(raw))};
  }
  function writeCards(cards, expectedRaw, storage = root.localStorage) {
    const clean = validateArray(cards);
    if (storage.getItem(CARDS_KEY) !== expectedRaw) throw new Error('Another page changed the cards. Reopen this operation.');
    const raw = JSON.stringify(clean);
    storage.setItem(CARDS_KEY, raw); // Quota/security failures leave the old value intact.
    return raw;
  }
  function settings(storage = root.localStorage) {
    const value = JSON.parse(storage.getItem(SETTINGS_KEY) || '{}');
    return {newLimit: Number.isInteger(value.newLimit) && value.newLimit >= 0 && value.newLimit <= 200 ? value.newLimit : 20, showMeanings: value.showMeanings === true, accent: value.accent === 'UK' ? 'UK' : 'US'};
  }
  function daily(date = new Date(), storage = root.localStorage) {
    const value = JSON.parse(storage.getItem(DAILY_KEY) || '{}');
    return value.day === dayKey(date) && Array.isArray(value.seen) ? {day: value.day, seen: value.seen.filter(id => typeof id === 'string')} : {day: dayKey(date), seen: []};
  }
  function queue(cards, limit = 20, ledger = {seen: []}, date = new Date()) {
    const end = new Date(date); end.setHours(23, 59, 59, 999);
    const due = cards.filter(card => Date.parse(card.review.due_at) <= end.getTime());
    const old = due.filter(card => !!card.review.last_reviewed_at).sort((a, b) => Date.parse(a.review.due_at) - Date.parse(b.review.due_at));
    const fresh = due.filter(card => !card.review.last_reviewed_at).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
    const seen = new Set(ledger.seen);
    const remaining = Math.max(0, limit - seen.size);
    return old.concat(fresh.filter(card => seen.has(card.id)), fresh.filter(card => !seen.has(card.id)).slice(0, remaining));
  }
  function markShown(card, date = new Date(), storage = root.localStorage) {
    if (card.review.last_reviewed_at) return;
    const value = daily(date, storage);
    if (!value.seen.includes(card.id)) { value.seen.push(card.id); storage.setItem(DAILY_KEY, JSON.stringify(value)); }
  }
  function makeCard(type, values, date = new Date()) {
    const now = date.toISOString();
    if (!root.crypto || typeof root.crypto.randomUUID !== 'function') throw new Error('Open STUDY over HTTPS to create stable card IDs');
    return validateCard({id: root.crypto.randomUUID(), type,
      front: (values.front || '').trim(), phonetic: type === 'word' ? (values.phonetic || '').trim() : '',
      meaning: (values.meaning || '').trim(), focus: type === 'sentence' ? (values.focus || '').trim() : '',
      note: (values.note || '').trim(), source: (values.source || '').trim(), added_by: 'Lin', audio_url: '',
      created_at: now, updated_at: now, review: {box: 0, due_at: dueAfter(0, date), last_reviewed_at: null, lapses: 0}});
  }
  function grade(card, remembered, date = new Date()) {
    const copy = validateCard(card);
    const box = remembered ? Math.min(6, copy.review.box + 1) : 1;
    copy.review = {box, due_at: dueAfter(INTERVALS[box - 1], date), last_reviewed_at: date.toISOString(), lapses: copy.review.lapses + (remembered ? 0 : 1)};
    copy.updated_at = date.toISOString();
    return copy;
  }
  function parseTxt(text, type, existing = []) {
    if (!['word', 'sentence'].includes(type)) throw new Error('Select the import type');
    if (text.includes('\uFFFD') || /[\u0000-\u0008\u000B\u000C\u000E-\u001F]/.test(text)) throw new Error('Invalid text encoding or binary content. Save as UTF-8 TXT and import again.');
    const seen = new Set(existing.map(identity));
    const result = {rows: [], skipped: 0, duplicates: 0, totalLines: 0};
    const lines = text.replace(/^\uFEFF/, '').split(/\r\n|\n|\r/);
    if (lines[lines.length - 1] === '') lines.pop();
    for (const raw of lines) {
      result.totalLines++;
      const line = raw.trim();
      if (!line || line.startsWith('#')) { result.skipped++; continue; }
      const delimiter = raw.includes('\t') ? '\t' : raw.includes(' | ') ? ' | ' : raw.includes('｜') ? '｜' : type === 'sentence' && raw.includes('|') ? '|' : type === 'word' && raw.includes(',') ? ',' : null;
      const at = delimiter ? raw.indexOf(delimiter) : -1;
      const row = {type, front: (at < 0 ? raw : raw.slice(0, at)).trim(), meaning: at < 0 ? '' : raw.slice(at + delimiter.length).trim()};
      if (!row.front) { result.skipped++; continue; }
      const key = identity(row);
      if (seen.has(key)) { result.duplicates++; continue; }
      seen.add(key); result.rows.push(row);
    }
    return result;
  }
  function planJson(incoming, existing) {
    const cards = validateArray(incoming);
    const byId = new Map(existing.map(card => [card.id, card]));
    const byFront = new Map(existing.map(card => [identity(card), card.id]));
    const result = {cards: existing.slice(), rows: [], added: 0, updated: 0, duplicates: 0};
    // Same stable id replaces the whole card only after the visible import confirmation.
    for (const card of cards) {
      const collision = byFront.get(identity(card));
      if (collision && collision !== card.id) { result.duplicates++; continue; }
      if (byId.has(card.id)) {
        const before = byId.get(card.id);
        if (JSON.stringify(before) === JSON.stringify(card)) { result.duplicates++; continue; }
        byFront.delete(identity(before));
        result.cards[result.cards.findIndex(item => item.id === card.id)] = card;
        result.updated++;
      } else { result.cards.push(card); result.added++; }
      byId.set(card.id, card); byFront.set(identity(card), card.id); result.rows.push(card);
    }
    return result;
  }
  function relatedSentences(word, cards) {
    if (word.type !== 'word' || !word.front.trim()) return [];
    const needle = word.front.trim().replace(/[.*+?^\x24{}()|[\]\\]/g, '\\$&');
    const match = new RegExp('(^|[^\\p{L}\\p{N}_])' + needle + '(?=$|[^\\p{L}\\p{N}_])', 'iu');
    return cards.filter(card => card.type === 'sentence' && (match.test(card.focus) || match.test(card.front))).slice(0, 3);
  }
  const api = {CARDS_KEY, SETTINGS_KEY, DAILY_KEY, INTERVALS, identity, dayKey, dueAfter, validateCard, validateArray, readCards, writeCards, settings, daily, queue, markShown, makeCard, grade, parseTxt, planJson, relatedSentences};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.StudyCore = api;
  if (root.document) {
    try { const theme = root.localStorage.getItem('companion_theme'); if (['warm', 'night'].includes(theme)) root.document.documentElement.dataset.theme = theme; } catch (_) { /* Study UI reports storage failures. */ }
  }
})(typeof window === 'undefined' ? globalThis : window);
