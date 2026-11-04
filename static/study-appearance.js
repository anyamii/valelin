(function () {
  'use strict';
  const root = document.documentElement;
  const keys = ['companion_theme', 'companion_bubble_texture', 'companion_chat_background_blur', 'companion_glass_card_opacity', 'companion_water_card_opacity', 'companion_water_refraction_strength'];
  let request = null, version = 0, appliedUrl = '', appliedConfig = false, serial = 0;
  const history = [];
  function integer(value, fallback, min, max) {
    const number = Number.parseInt(value, 10);
    return Number.isFinite(number) ? Math.max(min, Math.min(max, number)) : fallback;
  }
  function readableInk() {
    delete root.dataset.studyWallInk; delete root.dataset.studyCardInk;
    const image = document.getElementById('studyWallImage');
    if (!root.dataset.sitewideBackground || !image.complete || !image.naturalWidth) return;
    try {
      const style = getComputedStyle(root), rgba = value => {
        const source = value.trim();
        return source.startsWith('#') ? [1, 3, 5].map(at => Number.parseInt(source.slice(at, at + 2), 16)) : (source.match(/[\d.]+/g) || []).map(Number);
      };
      const paper = rgba(style.getPropertyValue('--bg')), ink = rgba(style.getPropertyValue('--text'));
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 16;
      const context = canvas.getContext('2d', {willReadFrequently: true});
      context.fillStyle = style.getPropertyValue('--bg'); context.fillRect(0, 0, 16, 16);
      context.globalAlpha = 1 - Number(style.getPropertyValue('--chat-background-dim') || 0); context.drawImage(image, 0, 0, 16, 16);
      const pixels = context.getImageData(0, 0, 16, 16).data, wall = [0, 0, 0];
      for (let i = 0; i < pixels.length; i += 4) for (let channel = 0; channel < 3; channel++) wall[channel] += pixels[i + channel] / 256;
      const luminance = rgb => rgb.slice(0, 3).map(value => { const c = value / 255; return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4; }).reduce((sum, value, i) => sum + value * [.2126, .7152, .0722][i], 0);
      const contrast = (a, b) => (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
      const choose = surface => contrast(luminance(ink), luminance(surface)) >= contrast(luminance(paper), luminance(surface)) ? 'text' : 'bg';
      let coat, alpha;
      if (root.dataset.bubbleTexture === 'default') { coat = rgba(style.getPropertyValue('--card-bg')); alpha = coat[3] ?? 1; }
      else {
        const a = rgba(style.getPropertyValue('--bubble-ai-bg')), b = rgba(style.getPropertyValue('--composer-bg'));
        coat = a.map((value, i) => (value + b[i]) / 2);
        alpha = Math.min(1, Number.parseFloat(style.getPropertyValue('--card-glass-opacity')) / 100 + (root.dataset.bubbleTexture === 'water' ? 0 : .05));
      }
      root.dataset.studyWallInk = choose(wall);
      root.dataset.studyCardInk = choose(wall.map((value, i) => value * (1 - alpha) + coat[i] * alpha));
    } catch (_) { /* Cross-origin images without canvas permission keep the existing theme ink. */ }
  }
  function localAppearance() {
    try {
      const theme = localStorage.getItem(keys[0]);
      if (['warm', 'night'].includes(theme)) root.dataset.theme = theme;
      else delete root.dataset.theme;
      const saved = localStorage.getItem(keys[1]);
      const selected = ['glass', 'water', 'liquid'].includes(saved) ? saved : 'default';
      // Liquid is not finalized in HOME; this task explicitly selects the exact glass preset.
      const texture = selected === 'liquid' ? 'glass' : selected;
      root.dataset.studyRequestedMaterial = selected; root.dataset.bubbleTexture = texture;
      const water = texture === 'water';
      root.style.setProperty('--card-glass-opacity', integer(localStorage.getItem(water ? keys[4] : keys[3]), water ? 12 : 40, water ? 0 : 30, 90) + '%');
      const displacement = document.getElementById('waterGlassCardDisplacement');
      if (displacement) displacement.setAttribute('scale', String(integer(localStorage.getItem(keys[5]), 60, 0, 80) / 4));
      else if (water) console.warn('[study-appearance] waterGlassCardDisplacement missing');
      const blur = integer(localStorage.getItem(keys[2]), 0, 0, 30), image = document.getElementById('studyWallImage');
      image.style.filter = 'blur(' + blur + 'px)'; image.style.inset = '-' + blur * 2 + 'px';
      image.style.width = 'calc(100% + ' + blur * 4 + 'px)'; image.style.height = 'calc(100% + ' + blur * 4 + 'px)';
      readableInk();
    } catch (_) { /* The card UI reports unavailable local storage. */ }
  }
  function clearWall() {
    delete root.dataset.sitewideBackground; document.getElementById('studyWallImage').removeAttribute('src');
    appliedUrl = ''; appliedConfig = false;
    delete root.dataset.studyWallInk; delete root.dataset.studyCardInk;
  }
  function loadImage(url, signal) {
    return new Promise((resolve, reject) => {
      const image = new Image(), timer = setTimeout(() => finish(new Error('image-timeout')), 10000);
      function finish(error) { clearTimeout(timer); signal.removeEventListener('abort', aborted); image.onload = image.onerror = null; error ? reject(error) : resolve(); }
      function aborted() { finish(new Error('aborted')); }
      signal.addEventListener('abort', aborted, {once: true});
      if (signal.aborted) { aborted(); return; }
      image.onload = () => finish(); image.onerror = () => finish(new Error('image-error')); image.src = url;
    });
  }
  function background(trigger) {
    const output = document.getElementById('appearanceNotice');
    let credential;
    try { credential = localStorage.getItem('companion_secret'); } catch (_) { return Promise.resolve(); }
    // Coalesce lifecycle notifications; only a changed login may supersede a live request.
    if (request && request.credential === credential) { request.record.triggers.push(trigger); return request.promise; }
    const current = ++version;
    if (request) request.controller.abort('superseded');
    if (!credential) { request = null; clearWall(); output.textContent = 'Sign in to HOME in this browser to load your background.'; return Promise.resolve(); }
    const controller = new AbortController(), record = {id: ++serial, triggers: [trigger], status: null, outcome: 'pending'};
    history.push(record); if (history.length > 40) history.shift();
    const entry = {controller, credential, record, promise: null}; request = entry;
    entry.promise = (async () => {
      const timeout = setTimeout(() => controller.abort('timeout'), 10000);
      try {
        const response = await fetch('/config', {headers: {Authorization: 'Bearer ' + credential}, cache: 'no-store', redirect: 'error', signal: controller.signal});
        record.status = response.status;
        if (!response.ok) throw new Error('http-' + response.status);
        const config = await response.json(); clearTimeout(timeout);
        if (current !== version) { record.outcome = 'superseded'; return; }
        const value = String(config.chat_background || '').trim();
        if (!value) { clearWall(); appliedConfig = true; output.textContent = ''; record.outcome = 'applied'; return; }
        const url = new URL(value, location.origin);
        if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('invalid-background');
        if (url.href !== appliedUrl) await loadImage(url.href, controller.signal);
        if (current !== version) { record.outcome = 'superseded'; return; }
        const image = document.getElementById('studyWallImage');
        if (image.src !== url.href) image.src = url.href;
        root.style.setProperty('--chat-background-dim', String((100 - integer(config.chat_background_opacity, 100, 0, 100)) / 100));
        root.dataset.sitewideBackground = 'true'; appliedUrl = url.href; appliedConfig = true;
        readableInk();
        output.textContent = ''; record.outcome = 'applied';
      } catch (error) {
        record.reason = controller.signal.aborted ? String(controller.signal.reason) : /^(http-\d{3}|image-error|image-timeout|invalid-background|aborted)$/.test(error.message) ? error.message : 'network-or-config-error';
        if (current !== version || record.reason === 'superseded') { record.outcome = 'superseded'; return; }
        record.outcome = appliedConfig ? 'retained' : 'failed';
        if (appliedConfig) output.textContent = '';
        else output.textContent = record.status === 401 ? 'Sign in to HOME, then return to load your background.' : 'HOME background settings are unavailable. Your cards still work.';
      } finally { clearTimeout(timeout); if (request === entry) request = null; }
    })();
    return entry.promise;
  }
  // Volatile request metadata only: never credentials, URLs, settings payloads, or card data.
  window.StudyAppearance = {diagnostics: () => ({requested: root.dataset.studyRequestedMaterial, effective: root.dataset.bubbleTexture, hasBackground: !!appliedUrl, ink: {wall: root.dataset.studyWallInk, card: root.dataset.studyCardInk}, requests: structuredClone(history)})};
  document.getElementById('studyWallImage').addEventListener('load', readableInk);
  window.addEventListener('storage', event => {
    if (event.key === null || keys.includes(event.key)) localAppearance();
    if (event.key === null || event.key === 'companion_secret') background('storage');
  });
  window.addEventListener('pageshow', () => { localAppearance(); background('pageshow'); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { localAppearance(); background('visibilitychange'); } });
  localAppearance();
})();
