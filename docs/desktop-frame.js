/* Keep fixed-position UI inside a real phone viewport on desktop. */
(() => {
  if (window.top !== window.self) return;
  const wide = matchMedia('(min-width: 601px)');
  const phoneLandscape = matchMedia('(pointer: coarse) and (max-height: 600px)');
  const route = () => {
    if (!wide.matches || phoneLandscape.matches) return;
    const shell = new URL('desktop-frame.html', document.currentScript?.src || scriptURL);
    shell.searchParams.set('page', location.href);
    location.replace(shell.href);
  };
  const scriptURL = document.currentScript.src;
  wide.addEventListener('change', route);
  phoneLandscape.addEventListener('change', route);
  route();
})();
