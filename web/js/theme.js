// Themes. A device picks two things, independently, from the header:
//   - the mode, Day or Night (one button), and
//   - the theme (a small menu): the built-in Citadel or LCARS, or a custom theme (Settings → Appearance).
// Every theme has both modes. Both choices are this device's own, kept in localStorage along with a ready-to-apply copy
// of a custom theme's colours (theme-init.js applies that copy before first paint, so nothing flashes the wrong look).
//
// On the page: Day is html[data-theme="light"] (the name every stylesheet already keys off) and the LCARS theme is
// html[data-skin="lcars"] (web/css/lcars.css). A custom theme is built on Citadel or LCARS and sets, separately for Day
// and for Night, every colour token (palette.js: four Basic colours, plus any Advanced overrides) and can add a
// background picture behind everything.

import { TOKEN_NAMES, resolve } from './palette.js';

const MODE_KEY = 'mode';           // 'day' | 'night'
const LOOK_KEY = 'look';           // 'citadel' | 'lcars' | 'borg' | 'c:<custom theme id>'
const SPEC_KEY = 'look-spec';      // the chosen custom theme's computed CSS for both modes, for theme-init.js
const BG_VARS = ['--bg-image', '--bg-dim', '--bg-size', '--bg-repeat', '--bg-pos'];
const VAR_NAMES = [...TOKEN_NAMES, ...BG_VARS];

export const THEMES = [
  { id: 'citadel', name: 'Citadel', skin: 'citadel' },
  { id: 'lcars', name: 'LCARS', skin: 'lcars' },
  { id: 'borg', name: 'Borg', skin: 'borg' },
];
const BUILTIN_SKINS = new Set(['citadel', 'lcars', 'borg']);
const normSkin = s => (BUILTIN_SKINS.has(s) ? s : 'citadel');

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* private window: this device just won't remember */ } },
};

const FIT = { cover: ['cover', 'no-repeat', 'center'], contain: ['contain', 'no-repeat', 'center'], tile: ['auto', 'repeat', 'top left'], center: ['auto', 'no-repeat', 'center'] };

/** One mode's half of a custom theme -> { vars, bg, glow }: every token from its Basic colours, its Advanced overrides
 * on top (palette.js), then the background picture. */
function half(h = {}, m, skin) {
  const vars = resolve(h, m, skin);
  const bg = h.background || {};
  if (bg.file) {
    const [size, repeat, pos] = FIT[bg.fit] || FIT.cover;
    Object.assign(vars, { '--bg-image': `url("/backgrounds/${bg.file}")`, '--bg-dim': `${bg.dim ?? 40}%`, '--bg-size': size, '--bg-repeat': repeat, '--bg-pos': pos });
  }
  return { vars, bg: !!bg.file, glow: bg.glow !== false };
}

/** A custom theme -> { skin, day, night } ready to apply. */
export function specOf(t) {
  const skin = normSkin(t.skin);
  return { skin, day: half(t.day, 'day', skin), night: half(t.night, 'night', skin) };
}

/** This device's mode: 'day' or 'night'. */
export function mode() { return store.get(MODE_KEY) === 'day' ? 'day' : 'night'; }

/** This device's chosen theme id: 'citadel', 'lcars', 'borg' or a custom theme's id. */
export function look() {
  const v = store.get(LOOK_KEY);
  return BUILTIN_SKINS.has(v) ? v : v?.startsWith('c:') ? v.slice(2) : 'citadel';
}

function storedSpec() {
  if (!store.get(LOOK_KEY)?.startsWith('c:')) return null;
  try { return JSON.parse(store.get(SPEC_KEY) || 'null'); } catch { return null; }
}

/** Put a mode + theme on the page. With no arguments: this device's own choice. Exported for the editor's live preview. */
export function applySpec(spec = storedSpec(), m = mode(), skin = spec ? normSkin(spec.skin) : normSkin(look())) {
  const root = document.documentElement;
  if (m === 'day') root.dataset.theme = 'light'; else delete root.dataset.theme;
  if (skin === 'lcars' || skin === 'borg') root.dataset.skin = skin; else delete root.dataset.skin;
  VAR_NAMES.forEach(n => root.style.removeProperty(n));
  root.classList.remove('has-bg', 'no-glow');
  const h = spec?.[m];
  if (!h) return;
  Object.entries(h.vars).forEach(([k, v]) => root.style.setProperty(k, v));
  root.classList.toggle('has-bg', h.bg);
  root.classList.toggle('no-glow', !h.glow);
}

const changed = () => document.dispatchEvent(new CustomEvent('theme:change', { detail: { base: currentTheme(), skin: currentSkin() } }));

// The same device's other pages follow a change at once: the dashboard and the Settings page it frames (and any other
// open tab). The browser tells every other page of this origin when localStorage changes.
addEventListener('storage', e => {
  if (e.key !== null && ![MODE_KEY, LOOK_KEY, SPEC_KEY].includes(e.key)) return;
  applySpec();
  changed();
});

/** Switch this device between Day and Night (whatever theme it uses). */
export function setMode(m) {
  store.set(MODE_KEY, m === 'day' ? 'day' : 'night');
  applySpec();
  changed();
}

/** Use a theme on this device. `customs` is the list of custom themes (from the server). */
export function setLook(id, customs = []) {
  const t = customs.find(x => x.id === id);
  if (t) { store.set(LOOK_KEY, `c:${id}`); store.set(SPEC_KEY, JSON.stringify(specOf(t))); }
  else { store.set(LOOK_KEY, normSkin(id)); store.set(SPEC_KEY, null); }
  applySpec();
  changed();
}

/** After the server's list arrives: re-apply this device's theme (it may have been edited elsewhere, or deleted). */
export function syncThemes(customs = []) {
  const id = look();
  if (BUILTIN_SKINS.has(id)) return;
  const t = customs.find(x => x.id === id);
  const fresh = t ? JSON.stringify(specOf(t)) : null;
  if (fresh !== store.get(SPEC_KEY) || !t) setLook(t ? id : 'citadel', customs);
}

/** What an embedded page (Music Companion, Music DB Explorer, Odysseus, Home Improvements) needs to look the same:
 * the mode as dark/light, a custom theme's colours for that mode (they use the same variable names; skin-specific
 * tokens stay here), and whether Citadel is drawing a background picture behind them — if so they make their own
 * page backdrop see-through and the picture shows through the frame. */
export function currentLook() {
  const h = storedSpec()?.[mode()];
  const vars = h ? Object.fromEntries(Object.entries(h.vars).filter(([k]) => !BG_VARS.includes(k) && !k.startsWith('--lc-') && !k.startsWith('--brg-') && !k.startsWith('--series-'))) : null;
  return { type: 'citadel-theme', theme: currentTheme(), vars, bg: !!h?.bg, glow: h ? h.glow : true };
}

/** 'dark' or 'light' — the mode, in the words embedded pages know. */
export function currentTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

/** 'citadel', 'lcars' or 'borg' — the built-in theme this device's look is built on. */
export function currentSkin() {
  return normSkin(document.documentElement.dataset.skin);
}
