// Runs before first paint: puts this device's mode (Day/Night) and theme on the page straight away (see theme.js for
// the full story). A plain script, not a module, and a file rather than inline: the CSP drops inline scripts.
try {
  var r = document.documentElement, ls = localStorage;
  // One-time move from the old single key ('dark' | 'light' | 'c:<id>'): keep the mode, start on the Citadel theme.
  var old = ls.getItem('theme');
  if (old !== null) {
    if (ls.getItem('mode') === null) ls.setItem('mode', old === 'light' ? 'day' : 'night');
    ls.removeItem('theme'); ls.removeItem('theme-spec');
  }
  if (window.parent !== window) r.classList.add('in-frame');      // framed by the dashboard (Settings): no header of its own
  var m = ls.getItem('mode') === 'day' ? 'day' : 'night', l = ls.getItem('look') || 'citadel', s = null;
  if (m === 'day') r.dataset.theme = 'light';
  if (l.indexOf('c:') === 0) s = JSON.parse(ls.getItem('look-spec') || 'null');
  // LCARS and Borg boot from the first paint so nothing flashes before animating in; app.js clears the class once filled
  var sk = s ? s.skin : l;
  if (sk === 'lcars') { r.dataset.skin = 'lcars'; r.classList.add('lc-boot'); }
  else if (sk === 'lcars2') { r.dataset.skin = 'lcars'; r.dataset.lcarsVariant = 'bezel'; r.classList.add('lc-boot'); }
  else if (sk === 'borg') { r.dataset.skin = 'borg'; r.classList.add('brg-boot'); }
  var h = s && s[m];
  if (h) {
    for (var k in h.vars) r.style.setProperty(k, h.vars[k]);
    if (h.bg) r.classList.add('has-bg');
    if (!h.glow) r.classList.add('no-glow');
  }
  // LCARS screen overlay (Settings → Appearance → Overlay, this device's own; lcars.css): scanlines unless picked
  var ov = function () { var v = null; try { v = localStorage.getItem('lc-overlay'); } catch (e) {} if (v && v !== 'scan') r.dataset.lcOverlay = v; else delete r.dataset.lcOverlay; };
  ov();
  addEventListener('storage', function (e) { if (e.key === 'lc-overlay' || e.key === null) ov(); });
} catch (e) {}
