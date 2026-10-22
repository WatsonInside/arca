/* =============================================================================
   Arca — context menu
   Drawn in the page instead of using the native one: the app owns the design
   tokens, and the menu has to sit on the same surface as the rest of the UI.
   One instance at a time; every caller hands over a flat list of items.

   Item shapes
     { sep: true }
     { label, icon?, kbd?, danger?, disabled?, checked?, action() }
   ========================================================================== */
(() => {
'use strict';

const PAD = 8;
const icon = (id) => `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let root = null;
let model = [];

/* --------------------------------------------------------------- rendering */
function build(items) {
  const el = document.createElement('div');
  el.className = 'menu';
  el.setAttribute('role', 'menu');
  el.tabIndex = -1;
  el.innerHTML = items.map((it, i) => {
    if (it.sep) return '<div class="menu-sep" role="separator"></div>';
    const cls = ['menu-item'];
    if (it.danger) cls.push('danger');
    if (it.disabled) cls.push('is-disabled');
    const lead = it.checked ? icon('i-check') : (it.icon ? icon(it.icon) : '');
    return `<button class="${cls.join(' ')}" type="button" data-i="${i}"
      role="${it.checked === undefined ? 'menuitem' : 'menuitemradio'}"
      ${it.checked ? 'aria-checked="true"' : ''}
      ${it.disabled ? 'aria-disabled="true"' : ''}>
      <span class="menu-ic">${lead}</span>
      <span class="menu-label">${esc(it.label)}</span>
      ${it.kbd ? `<span class="menu-kbd">${esc(it.kbd)}</span>` : ''}
    </button>`;
  }).join('');
  return el;
}

/** Options the arrow keys may land on: visible, enabled items only. */
function stops() {
  return Array.from(root.querySelectorAll('.menu-item:not(.is-disabled)'));
}

function setActive(el) {
  stops().forEach((n) => n.classList.toggle('is-active', n === el));
  if (el) el.scrollIntoView({ block: 'nearest' });
}

function move(step) {
  const list = stops();
  if (!list.length) return;
  const at = list.indexOf(root.querySelector('.menu-item.is-active'));
  let next;
  if (at < 0) next = step > 0 ? 0 : list.length - 1;
  else next = Math.min(list.length - 1, Math.max(0, at + step));
  setActive(list[next]);
}

function fire(el) {
  const item = model[Number(el.dataset.i)];
  if (!item || item.disabled) return;
  close();
  try { item.action && item.action(); }
  catch (err) { console.error('[menu] action failed', err); }
}

/* ------------------------------------------------------------------ placing */
function place(x, y) {
  const box = root.getBoundingClientRect();
  const vw = document.documentElement.clientWidth || window.innerWidth;
  const vh = document.documentElement.clientHeight || window.innerHeight;
  let left = x;
  let top = y;
  let right = false;
  let up = false;
  if (left + box.width > vw - PAD) { left = Math.max(PAD, x - box.width); right = true; }
  if (top + box.height > vh - PAD) { top = Math.max(PAD, y - box.height); up = true; }
  root.style.left = `${Math.round(left)}px`;
  root.style.top = `${Math.round(top)}px`;
  root.style.transformOrigin = `${right ? 'right' : 'left'} ${up ? 'bottom' : 'top'}`;
}

/* ------------------------------------------------------------- open / close */
function announce() {
  document.dispatchEvent(new CustomEvent('arca:overlay'));
}

function close() {
  if (!root) return;
  document.removeEventListener('pointerdown', onPointerDown, true);
  document.removeEventListener('keydown', onKeyDown, true);
  document.removeEventListener('contextmenu', onDocMenu, true);
  window.removeEventListener('resize', close);
  window.removeEventListener('blur', close);
  document.removeEventListener('scroll', close, true);
  root.remove();
  root = null;
  model = [];
  announce();
}

function onPointerDown(e) {
  if (root && root.contains(e.target)) return;
  close();
}

function onDocMenu(e) {
  // Swallow the right-click that lands on the menu itself; otherwise it would
  // bubble to the app handler and replace this menu with the generic one.
  if (root && root.contains(e.target)) { e.preventDefault(); e.stopPropagation(); }
}

function onKeyDown(e) {
  if (!root) return;
  const k = e.key;
  if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
  if (k === 'ArrowDown' || k === 'ArrowUp') {
    e.preventDefault();
    e.stopPropagation();
    move(k === 'ArrowDown' ? 1 : -1);
    return;
  }
  if (k === 'Home' || k === 'End') {
    e.preventDefault();
    e.stopPropagation();
    const list = stops();
    if (list.length) setActive(k === 'Home' ? list[0] : list[list.length - 1]);
    return;
  }
  if (k === 'Enter' || k === ' ') {
    const cur = root.querySelector('.menu-item.is-active');
    if (!cur) return;                    // let the page keep its own Enter
    e.preventDefault();
    e.stopPropagation();
    fire(cur);
    return;
  }
  if (k === 'Tab') { close(); }
}

/**
 * open({ x, y, items })
 * Coordinates are viewport-relative (as handed over by a MouseEvent).
 */
function open({ x, y, items }) {
  close();
  model = (items || []).filter(Boolean);
  if (!model.some((it) => !it.sep)) return false;

  root = build(model);
  root.style.left = '-9999px';
  root.style.top = '0px';
  document.body.appendChild(root);
  place(x, y);
  root.classList.add('in');

  // Keyboard is opt-in: right-clicking should not steal focus from a field the
  // user was typing in, so the menu only responds once a key actually lands.
  document.addEventListener('pointerdown', onPointerDown, true);
  document.addEventListener('keydown', onKeyDown, true);
  document.addEventListener('contextmenu', onDocMenu, true);
  window.addEventListener('resize', close);
  window.addEventListener('blur', close);
  document.addEventListener('scroll', close, true);

  root.addEventListener('click', (e) => {
    const el = e.target.closest('.menu-item');
    if (el) fire(el);
  });
  root.addEventListener('contextmenu', (e) => e.preventDefault());
  announce();
  return true;
}

window.ArcaMenu = {
  open,
  close,
  isOpen: () => !!root,
  /** Number of items in the open menu — handy for diagnostics. */
  size: () => (root ? root.querySelectorAll('.menu-item').length : 0),
  element: () => root,
};
})();
