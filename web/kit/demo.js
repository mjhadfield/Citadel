import { initKit } from './kit.js';

const kit = initKit({ origins: [location.origin], storagePrefix: 'ck-demo' });
const chip = document.getElementById('state-chip');
const syncChip = s => { chip.textContent = `${s.mode} · ${s.theme} · ${s.style}`; };
syncChip(kit.getState());
document.addEventListener('kit:change', e => syncChip(e.detail));
document.getElementById('toggle-mode').addEventListener('click', () => kit.toggleMode());
const dlg = document.getElementById('demo-dlg');
document.getElementById('open-dlg').addEventListener('click', () => dlg.showModal());
dlg.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => dlg.close()));
