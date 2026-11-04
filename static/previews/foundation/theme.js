/* VALELIN 设计样张 · 唯一的脚本
   只做这几件事：日/夜/跟随系统（存 localStorage）、气泡 A/B（?bubble=b）、侧边索引开合（?index=open 只给截图用）、
   滚过大标题后的窄条（默认一直在；?bar=peek = 她提的“往回滑才出现”）、文件卡长名字中间省略 + 长按看全名、
   打开样张里的弹窗 / 抽屉 / toast。不发任何网络请求。
   侧边索引只靠点击打开（页头大标题、窄条、聊天页的对方名字），不监听边缘滑动：安卓手势导航的左右边缘是系统“返回”。 */
(function () {
  var root = document.documentElement;
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  var q = new URLSearchParams(location.search);
  var dark = matchMedia('(prefers-color-scheme: dark)');

  // 主题偏好：day | night | system。页面实际主题 = 偏好落地后的 day / night
  var pref = q.get('theme') || get('design-theme') || 'system';
  if (pref !== 'day' && pref !== 'night') pref = 'system';
  function apply() {
    root.dataset.theme = pref === 'system' ? (dark.matches ? 'night' : 'day') : pref;
    var radios = document.querySelectorAll('[data-theme-set]');
    for (var i = 0; i < radios.length; i++) radios[i].checked = radios[i].getAttribute('data-theme-set') === pref;
  }
  apply();
  dark.addEventListener('change', function () { if (pref === 'system') apply(); });
  document.addEventListener('DOMContentLoaded', apply);

  var bubble = q.get('bubble') || get('design-bubble') || 'a';
  root.dataset.bubble = bubble === 'b' ? 'b' : 'a';

  // 侧边索引：点大标题 / 窄条 / 对方名字开；点遮罩、点任一项、点 CLOSE、按 Esc 关。焦点进面板，关时回到打开它的那个入口
  var opener = null;
  function setIndex(open, from) {
    root.classList.toggle('index-open', open);
    if (open) opener = from || null;
    var btns = document.querySelectorAll('[data-index-open]');
    for (var i = 0; i < btns.length; i++) btns[i].setAttribute('aria-expanded', open ? 'true' : 'false');
    var panel = document.getElementById('index');
    if (panel) {
      panel.setAttribute('aria-hidden', open ? 'false' : 'true');
      if (open) panel.focus({ preventScroll: true });
      else if (opener && panel.contains(document.activeElement)) opener.focus({ preventScroll: true });
    }
  }

  // 窄条：大标题滚出屏幕后出现。默认一直在（任何位置一步就能打开）；?bar=peek 时只在往回滑时出现、往下滚就收起
  document.addEventListener('DOMContentLoaded', function () {
    var bar = document.querySelector('.peekbar');
    var title = document.querySelector('.masthead .title-btn, .cover .title-btn');
    if (!bar || !title || !('IntersectionObserver' in window)) return;
    var peek = q.get('bar') === 'peek';
    var past = false, up = false, lastY = scrollY;
    function update() { bar.classList.toggle('is-on', past && (!peek || up)); }
    new IntersectionObserver(function (es) {
      past = !es[0].isIntersecting && es[0].boundingClientRect.top < 0;
      update();
    }).observe(title);
    if (peek) addEventListener('scroll', function () {
      var dy = scrollY - lastY;
      if (Math.abs(dy) < 6) return;   // 往回轻滑一点就够
      up = dy < 0; lastY = scrollY;
      update();
    }, { passive: true });
  });
  document.addEventListener('DOMContentLoaded', function () { setIndex(q.get('index') === 'open'); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && root.classList.contains('index-open')) setIndex(false); });

  document.addEventListener('change', function (e) {
    var t = e.target;
    if (t.hasAttribute && t.hasAttribute('data-theme-set') && t.checked) {
      pref = t.getAttribute('data-theme-set');
      set('design-theme', pref);
      apply();
    }
  });

  var toastTimer;
  function toast(text) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = text;
    el.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('is-on'); }, 1800);
  }

  // 文件卡：长名字按实际宽度做中间省略。保留开头一段和结尾（含扩展名），中间一个 …；窗口宽度变了重算
  function fitName(el) {
    var full = el.getAttribute('data-full');
    el.textContent = full;
    if (el.scrollWidth <= el.clientWidth) return;
    var chars = Array.from(full);                       // 按码点切，中英文混排都不会切坏
    var dot = full.lastIndexOf('.');
    var ext = dot > 0 ? Array.from(full.slice(dot)).length : 0;
    var lo = 0, hi = chars.length - 1, best = '…' + chars.slice(chars.length - ext).join('');
    while (lo <= hi) {                                  // 二分：最多能留几个字
      var k = (lo + hi) >> 1;
      var tail = Math.min(k, Math.max(ext, Math.floor(k * 0.45)));
      var text = chars.slice(0, k - tail).join('') + '…' + chars.slice(chars.length - tail).join('');
      el.textContent = text;
      if (el.scrollWidth <= el.clientWidth) { best = text; lo = k + 1; } else hi = k - 1;
    }
    el.textContent = best;
  }
  function fitAll() {
    var names = document.querySelectorAll('.filecard .file-name[data-full]');
    for (var i = 0; i < names.length; i++) fitName(names[i]);
  }
  document.addEventListener('DOMContentLoaded', function () {
    fitAll();
    if (document.fonts) document.fonts.ready.then(fitAll);
    var w = innerWidth;
    addEventListener('resize', function () { if (innerWidth !== w) { w = innerWidth; fitAll(); } });
  });

  // 长按文件卡（500ms）弹 toast 显示全名。真页面里点卡片是打开文件，所以全名放长按。手指一动就取消
  var press, pressAt;
  function cancelPress() { clearTimeout(press); press = null; }
  document.addEventListener('pointerdown', function (e) {
    var card = e.target.closest && e.target.closest('.filecard');
    if (!card) return;
    pressAt = [e.clientX, e.clientY];
    press = setTimeout(function () { press = null; toast(card.getAttribute('title')); }, 500);
  });
  document.addEventListener('pointermove', function (e) {
    if (press && Math.abs(e.clientX - pressAt[0]) + Math.abs(e.clientY - pressAt[1]) > 10) cancelPress();
  });
  document.addEventListener('pointerup', cancelPress);
  document.addEventListener('pointercancel', cancelPress);
  document.addEventListener('contextmenu', function (e) { if (e.target.closest && e.target.closest('.filecard')) e.preventDefault(); });

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-index-open],[data-index-close],.index-list a,[data-toggle-theme],[data-bubble-set],[data-open],[data-close],[data-toast],.scrim,.copy');
    if (!t) return;
    if (t.hasAttribute('data-index-open')) {
      setIndex(true, t);
    } else if (t.hasAttribute('data-index-close') || t.matches('.index-list a')) {
      setIndex(false);   // 点某一项：先关面板，再照常跳转
    } else if (t.hasAttribute('data-toggle-theme')) {
      pref = root.dataset.theme === 'night' ? 'day' : 'night';
      set('design-theme', pref);
      apply();
    } else if (t.hasAttribute('data-bubble-set')) {
      root.dataset.bubble = t.getAttribute('data-bubble-set');
      set('design-bubble', root.dataset.bubble);
    } else if (t.hasAttribute('data-open')) {
      document.getElementById(t.getAttribute('data-open')).classList.add('is-open');
    } else if (t.hasAttribute('data-close') || (t.classList.contains('scrim') && e.target === t)) {
      t.closest('.scrim').classList.remove('is-open');
    } else if (t.hasAttribute('data-toast') || t.classList.contains('copy')) {
      toast(t.getAttribute('data-toast') || 'Copied');
    }
  });
})();
