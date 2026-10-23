/* =============================================================================
   Arca — renderer
   Plain DOM, no framework: the whole UI is one state object and a few renders.
   ========================================================================== */
(() => {
'use strict';

/* ------------------------------------------------------------------ helpers */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const icon = (id, cls = '') => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
function fmtSize(n) {
  n = Number(n) || 0;
  let i = 0;
  while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i += 1; }
  return `${i === 0 ? Math.round(n) : n.toFixed(n >= 100 ? 0 : 1)} ${UNITS[i]}`;
}
const fmtSpeed = (n) => (n > 1 ? `${fmtSize(n)}/s` : '');
function fmtEta(s) {
  if (s === 0) return '即将完成';
  if (!s || s < 0) return '';
  if (s < 60) return `剩余 ${Math.max(1, Math.round(s))} 秒`;
  if (s < 3600) return `剩余 ${Math.round(s / 60)} 分`;
  return `剩余 ${(s / 3600).toFixed(1)} 小时`;
}
const fmtDate = (ts) => {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};
const baseName = (p) => (p || '').replace(/\/+$/, '').split('/').pop() || p;
const dirName = (p) => {
  const t = (p || '').replace(/\/+$/, '');
  const i = t.lastIndexOf('/');
  return i <= 0 ? '/' : t.slice(0, i);
};
const HOME = '/Users';
let homeDir = '';
function shortPath(p) {
  if (!p) return '';
  if (homeDir && p.startsWith(homeDir)) return '~' + p.slice(homeDir.length);
  return p;
}
const join = (a, b) => (a.replace(/\/+$/, '') + '/' + b.replace(/^\/+/, ''));

/** Shorten from the middle so both the anchor and the file name stay readable. */
function midEllipsis(text, max = 46) {
  const t = String(text || '');
  if (t.length <= max) return t;
  const head = Math.max(6, Math.ceil((max - 1) / 3));
  const tail = max - 1 - head;
  return t.slice(0, head) + '…' + t.slice(-tail);
}

function debounce(fn, ms) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* -------------------------------------------------------------------- boot */
const BOOT = window.__ARCA_BOOT || {};
const TOKEN = BOOT.token || new URLSearchParams(location.search).get('t') || '';
const NATIVE = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.arca);

const pending = new Map();
let seq = 0;
window.__arcaReply = (id, result) => {
  const resolve = pending.get(String(id));
  if (resolve) { pending.delete(String(id)); resolve(result || {}); }
};
function native(method, params = {}) {
  if (!NATIVE) return Promise.resolve(null);
  const id = 'r' + (++seq);
  return new Promise((resolve) => {
    pending.set(id, resolve);
    window.webkit.messageHandlers.arca.postMessage(JSON.stringify({ id, method, params }));
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); resolve(null); } }, 180000);
  });
}
async function pick(kind, extra = {}) {
  const res = await native('pick', Object.assign({ kind }, extra));
  return (res && res.paths) || [];
}

/* --------------------------------------------------------------------- api */
async function api(path, body) {
  const res = await fetch('/api' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'X-Arca-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok || !data || data.ok === false) {
    const err = new Error((data && data.error) || `请求失败 (${res.status})`);
    // Callers that need a decision (e.g. "folder already exists") read the
    // payload instead of parsing the message.
    err.data = data || {};
    throw err;
  }
  return data;
}

/* ------------------------------------------------------------------- toast */
function toast(message, kind = 'info', iconId = null) {
  const box = $('#toasts');
  const existing = [...box.children].find((item) => item.dataset.message === message && item.dataset.kind === kind && !item.classList.contains('out'));
  if (existing) {
    const count = Number(existing.dataset.count || 1) + 1;
    existing.dataset.count = String(count);
    const label = existing.querySelector('.toast-text');
    if (label) label.textContent = `${message} ×${count}`;
    return;
  }
  while (box.children.length >= 3) box.firstElementChild.remove();
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.dataset.message = message;
  el.dataset.kind = kind;
  el.dataset.count = '1';
  const ic = iconId || (kind === 'ok' ? 'i-check-circle' : kind === 'err' ? 'i-alert' : 'i-info');
  el.innerHTML = `${icon(ic, 'ic')}<span class="toast-text">${esc(message)}</span>${kind === 'err' ? '<button class="toast-close" aria-label="关闭">×</button>' : ''}`;
  box.appendChild(el);
  if (kind === 'err') el.querySelector('.toast-close').addEventListener('click', () => el.remove());
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 260);
  }, Math.min(6000, 2600 + message.length * 35));
}

/* ------------------------------------------------------------------- state */
const state = {
  meta: null,
  settings: {},
  jobs: [],
  history: [],
  view: 'compress',
  c: {
    items: [], format: '7z', level: 3, method: 'auto',
    splitId: 'none', splitBytes: 0, splitCustom: '',
    pw: '', encryptNames: true, excludeMeta: true, solid: true,
    par2: false, par2Pct: 5, par2Single: false,
    dest: '', name: '', touchedName: false,
    target: '', exists: false, busy: false,
  },
  x: { path: '', info: null, pw: '', remember: false, dest: '', autoFolder: true,
       overwrite: 'rename', loading: false, error: '', expanded: false },
  jobStatus: {},
  pollTimer: 0,
  update: null,        // last engine-update check
  updateTimer: 0,
  componentInstall: {},  // id -> installer snapshot while a component updates
  integration: null,     // Finder services + default handler (read on demand)
};

const VIEW_META = {
  compress: { title: '压缩', sub: '把文件收进一个整洁的归档里' },
  extract: { title: '解压', sub: '预览并取出归档里的内容' },
  queue: { title: '任务', sub: '进度、速度与历史' },
};

/* -------------------------------------------------------------------- theme */
function systemDark() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}
function applyTheme(mode) {
  const dark = mode === 'dark' || (mode !== 'light' && systemDark());
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('#btn-theme').innerHTML = icon(dark ? 'i-sun' : 'i-moon');
  $('#btn-theme').title = dark ? '切换到浅色' : '切换到深色';
  $('#btn-theme').setAttribute('aria-pressed', String(dark));
  native('appearance', { mode: dark ? 'dark' : 'light' });
  // A theme swap can nudge font metrics, so re-measure any open segmented
  // control instead of leaving its pill on stale coordinates.
  syncAllSegs();
}

/** Flip the appearance and keep state in sync immediately — persisting is a
 *  round trip, and anything reading state.settings before it lands (opening
 *  the settings sheet right after a toggle) would otherwise see the old value
 *  and show the wrong option as selected. */
function setAppearance(mode) {
  state.settings.appearance = mode;
  applyTheme(mode);
  patchSettings({ appearance: mode });
}

/* ---------------------------------------------------------------- segmented */
function buildSeg(container, items, activeId, onPick, onUnavailable) {
  // Re-rendering must not rebuild. A fresh pill is a *new* element starting at
  // translateX(0), so syncSeg then animates it from the first option to the
  // real one -- a full-width slide every time an unrelated toggle re-renders
  // the row. Rebuilding also destroyed focus, which killed keyboard arrow
  // navigation after a single step.
  const sig = items.map((it) => [it.id, it.label, it.tag || '',
                                 it.disabled ? 1 : 0, it.ariaDisabled ? 1 : 0,
                                 it.title || ''].join('\u001f')).join('\u001e');
  const reuse = container.dataset.sig === sig && !!container.querySelector('button[data-id]');
  if (!reuse) {
    container.innerHTML = '<div class="seg-pill"></div>' + items.map((it) => `
      <button role="tab" data-id="${esc(it.id)}" aria-selected="false"
        ${it.disabled ? 'disabled' : ''} ${it.ariaDisabled ? 'aria-disabled="true"' : ''}
        title="${esc(it.title || '')}">
        ${esc(it.label)}${it.tag ? `<span class="ext">${esc(it.tag)}</span>` : ''}
      </button>`).join('');
    container.dataset.sig = sig;
  }

  // Selection is reflected here, not by the caller: a caller that only patches
  // its state in place (the settings sheet does) would otherwise leave the pill
  // parked on the old option and the control would look dead.
  const select = (btn, fire) => {
    $$('button[data-id]', container).forEach((b) => {
      b.setAttribute('aria-selected', String(b === btn));
    });
    syncSeg(container);
    if (fire) onPick(btn.dataset.id);
  };

  container.onclick = (e) => {
    const btn = e.target.closest('button[data-id]');
    if (!btn || btn.disabled) return;
    // A capability that only lacks setup is not "disabled": clicking it must
    // lead somewhere (the setting that unlocks it) instead of doing nothing.
    if (btn.getAttribute('aria-disabled') === 'true') {
      if (onUnavailable) onUnavailable(btn.dataset.id);
      return;
    }
    if (btn.getAttribute('aria-selected') === 'true') return;
    select(btn, true);
  };
  container.onkeydown = (e) => {
    const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
    if (step === undefined && e.key !== 'Home' && e.key !== 'End') return;
    const opts = $$('button[data-id]', container)
      .filter((b) => !b.disabled && b.getAttribute('aria-disabled') !== 'true');
    if (!opts.length) return;
    e.preventDefault();
    const at = opts.findIndex((b) => b.getAttribute('aria-selected') === 'true');
    let next;
    if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = opts.length - 1;
    else next = Math.min(opts.length - 1, Math.max(0, (at < 0 ? 0 : at) + step));
    opts[next].focus();
    if (next !== at) select(opts[next], true);
  };

  // Selection is state, not DOM history: reflect it before measuring, even
  // when the buttons were reused and their attributes still say otherwise.
  const opts = $$('button[data-id]', container);
  const wanted = opts.find((b) => b.dataset.id === activeId);
  opts.forEach((b) => b.setAttribute('aria-selected', String(b === wanted)));
  let active = wanted;
  if (!active) {
    // Nothing selected (stale or unknown value)? Fall back to the first usable
    // option so the pill always has somewhere to sit.
    active = container.querySelector('button[data-id]:not([disabled]):not([aria-disabled="true"])');
    if (active) active.setAttribute('aria-selected', 'true');
  }
  // A rebuilt pill has to appear parked, not slide in from the left edge.
  syncSeg(container, { instant: !reuse });
}

function syncSeg(container, opts) {
  const pill = container.querySelector('.seg-pill');
  const active = container.querySelector('button[aria-selected="true"]');
  if (!pill || !active) return;
  // offsetLeft/offsetWidth are layout units, so an ancestor's entrance
  // transform (the sheet scales in from 0.985) can't shrink the measurement —
  // getBoundingClientRect() here left the pill permanently a hair too small.
  const padL = parseFloat(getComputedStyle(container).paddingLeft) || 0;
  const place = () => {
    pill.style.left = `${padL}px`;
    pill.style.width = `${active.offsetWidth}px`;
    pill.style.transform = `translateX(${active.offsetLeft - padL}px)`;
  };
  if (opts && opts.instant) {
    // Paint the destination with transitions off, then restore them: the pill
    // is parked, and a later real selection still slides.
    pill.classList.add('no-anim');
    place();
    void pill.offsetWidth;
    pill.classList.remove('no-anim');
    return;
  }
  place();
}

function syncAllSegs() {
  $$('.seg').forEach((seg) => { if (seg.querySelector('.seg-pill')) syncSeg(seg, { instant: true }); });
}

/* --------------------------------------------------------------- formatting */
const LEVEL_NAMES = ['仅存储', '快速', '较快', '标准', '较高', '极限'];
const LEVEL_HINTS = ['不压缩，只打包', '最快速度', '偏向速度', '速度与体积平衡', '体积更小', '最省空间'];

function pwStrength(pw) {
  if (!pw) return 0;
  let score = 0;
  if (pw.length >= 6) score += 1;
  if (pw.length >= 10) score += 1;
  if (pw.length >= 14) score += 1;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score += 1;
  if (/\d/.test(pw)) score += 1;
  if (/[^A-Za-z0-9]/.test(pw)) score += 1;
  return Math.min(4, Math.max(1, Math.round(score * 4 / 6)));
}
const STRENGTH_TEXT = ['', '偏弱', '一般', '较强', '很强'];

function refreshCompressPasswordHint() {
  const { pw, format } = state.c;
  const s = pwStrength(pw);
  $('#c-pw-strength').dataset.s = String(s);
  $('#c-pw-text').textContent = format === 'zip' && /[^\x00-\x7f]/.test(pw)
    ? 'ZIP 格式暂不支持含中文等非 ASCII 字符的密码，请改用 7z 格式（同时会加密文件名，更安全）'
    : (pw ? STRENGTH_TEXT[s] : '');
}

function genPassword(len = 16) {
  const alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*-_=+';
  const buf = new Uint32Array(len);
  crypto.getRandomValues(buf);
  return Array.from(buf, (v) => alphabet[v % alphabet.length]).join('');
}

/* =============================================================== compress UI */
const cEls = {};

function cTotalSize() {
  if (!state.c.items.length) return 0;
  if (state.c.items.some((i) => i.isDir)) return 0;
  return state.c.items.reduce((s, i) => s + (i.size || 0), 0);
}

function cFormatExt() {
  const fmt = (state.meta?.formats || []).find((f) => f.id === state.c.format);
  return fmt ? fmt.ext : '.7z';
}

function renderCompress() {
  const c = state.c;
  const has = c.items.length > 0;
  $('#drop-c').classList.toggle('hidden', has);
  $('#c-files').classList.toggle('hidden', !has);

  if (has) {
    const total = cTotalSize();
    const dirs = c.items.filter((i) => i.isDir).length;
    const files = c.items.length - dirs;
    // 项数、文件数、文件夹数、总大小一并给出（§5.1），大小未知时落"—"。
    $('#c-meta').textContent = `${c.items.length} 项 · ${files} 个文件 · ${dirs} 个文件夹`
      + (total ? ` · ${fmtSize(total)}` : c.items.every((i) => !i.isDir) ? '' : ' · 大小待扫描');
    $('#c-list').innerHTML = c.items.map((it, idx) => `
      <div class="file-row" data-idx="${idx}" style="animation-delay:${Math.min(idx, 6) * 18}ms">
        <div class="file-ico ${it.isDir ? 'folder' : ''}">${icon(it.isDir ? 'i-folder' : 'i-file')}</div>
        <div class="file-main">
          <b title="${esc(it.name)}">${esc(midEllipsis(it.name, 52))}</b>
          <small>${esc(midEllipsis(shortPath(it.dir), 44))}</small>
        </div>
        <span class="file-size">${it.isDir ? '文件夹' : fmtSize(it.size)}</span>
        <button class="file-x" data-remove="${idx}" title="从列表移除"
                aria-label="从列表移除 ${esc(it.name)}，不删除磁盘文件">${icon('i-x')}</button>
      </div>`).join('');
  } else {
    // Leaving the old rows behind made the list reachable to anything still
    // querying the DOM after "clear" — visible state said empty, the tree didn't.
    $('#c-list').innerHTML = '';
  }

  // format segmented
  const formats = (state.meta?.formats || []).map((f) => {
    const unavailable = !f.create;
    const needsSetup = unavailable && f.id === 'rar';
    return {
      id: f.id, label: f.label,
      // "需配置" is honest: this option becomes available by pointing at
      // WinRAR, so it stays focusable and clicking it leads to that setting.
      tag: unavailable ? (needsSetup ? '需配置' : '只读') : '',
      disabled: unavailable && !needsSetup,
      ariaDisabled: needsSetup,
      title: f.create ? f.hint
        : needsSetup
          ? '创建 RAR 需要 WinRAR 命令行（5.0 及以上）。点按前往设置指定。'
          : '该格式在当前环境中不可用',
    };
  });
  if (formats.length) buildSeg($('#c-format'), formats, c.format, (id) => {
    c.format = id;
    c.method = 'auto';
    renderCompress();
    refreshTarget();
  }, (id) => {
    // RAR only lacks a path to WinRAR: take the user straight to that row.
    if (id === 'rar') openSettings('#set-rar');
  });

  const fmt = (state.meta?.formats || []).find((f) => f.id === c.format);
  $('#c-format-hint').textContent = fmt ? fmt.hint : '';
  // RAR's own sentence can never reach the hint slot via selection -- the
  // option cannot be selected -- so surface it while the tab is hovered or
  // focused. Assigned as properties so a re-render reuses one listener.
  const rarBtn = $('#c-format').querySelector('button[aria-disabled="true"]');
  if (rarBtn) {
    const rarFmt = (state.meta?.formats || []).find((f) => f.id === 'rar');
    const rarHint = (rarFmt && rarFmt.hint) || '';
    const show = () => { if (rarHint) $('#c-format-hint').textContent = rarHint; };
    const hide = () => { $('#c-format-hint').textContent = fmt ? fmt.hint : ''; };
    rarBtn.onmouseenter = show;
    rarBtn.onmouseleave = hide;
    rarBtn.onfocus = show;
    rarBtn.onblur = hide;
  }
  // Formats differ in what they can actually do (zstd is a stream: no password,
  // no volumes), so rows are hidden rather than shown-and-ignored.
  const canEncrypt = !fmt || fmt.encrypt !== false;
  const canSplit = !fmt || fmt.split !== false;
  const canMethod = !fmt || fmt.method !== false;
  const canSolid = (!fmt || fmt.solid !== false) && c.format === '7z';
  $('#c-pw-row').classList.toggle('hidden', !canEncrypt);
  $('#c-split-row').classList.toggle('hidden', !canSplit);
  $('#c-method-row').classList.toggle('hidden', !canMethod);
  $('#c-solid-row').classList.toggle('hidden', !canSolid);
  // Filename encryption only exists where it can actually work, and only
  // matters once a password is set — so reveal the row progressively.
  const namesRow = $('#c-names-row');
  const showNames = canEncrypt && c.format !== 'zip' && c.pw.length > 0;
  namesRow.classList.toggle('hidden', !showNames);

  // level
  $('#c-level').value = String(c.level);
  $('#c-level-name').textContent = LEVEL_NAMES[c.level];
  $('#c-level-hint').textContent = LEVEL_HINTS[c.level];

  // split
  const presets = state.meta?.splitPresets || [];
  if (!$('#c-split').options.length) {
    $('#c-split').innerHTML = presets.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join('');
  }
  $('#c-split').value = c.splitId;
  $('#c-split-custom').classList.toggle('hidden', c.splitId !== 'custom');
  const total = cTotalSize();
  const bytes = c.splitId === 'custom'
    ? parseSizeInput(c.splitCustom)
    : (presets.find((p) => p.id === c.splitId)?.bytes || 0);
  c.splitBytes = bytes;
  let hint = '';
  if (c.splitId === 'custom' && !bytes && c.splitCustom) hint = '格式示例：120m、1.5g';
  else if (bytes > 0 && total) hint = `约 ${Math.ceil(total / bytes)} 个分卷`;
  else if (bytes > 0) hint = '分卷大小 ' + fmtSize(bytes);
  $('#c-split-hint').textContent = hint;

  // password
  refreshCompressPasswordHint();
  $('#c-names').setAttribute('aria-checked', String(c.encryptNames));
  $('#c-names-hint').textContent = '';

  // advanced
  $('#c-exclude').setAttribute('aria-checked', String(c.excludeMeta));
  $('#c-solid').setAttribute('aria-checked', String(c.solid));
  $('#c-solid').disabled = c.format !== '7z';
  const methods = state.meta?.methods || [];
  if (!$('#c-method').options.length) {
    $('#c-method').innerHTML = methods.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
  }
  $('#c-method').disabled = c.format === 'zip';
  $('#c-method').value = c.method;

  // recovery records: only offered when the par2 binary is actually present
  const par2 = state.meta?.par2 || {};
  const par2Row = $('#c-par2-row');
  if (par2Row) {
    par2Row.classList.toggle('hidden', !par2.available);
    if (par2.available) {
      const pctSel = $('#c-par2-pct');
      if (!pctSel.options.length) {
        pctSel.innerHTML = (par2.percents || [3, 5, 10, 15])
          .map((p) => `<option value="${p}">${p}%</option>`).join('');
      }
      pctSel.value = String(c.par2Pct);
      pctSel.classList.toggle('hidden', !c.par2);
      $('#c-par2').setAttribute('aria-checked', String(c.par2));
      const singleRow = $('#c-par2-single-row');
      if (singleRow) singleRow.classList.toggle('hidden', !c.par2);
      if (!c.par2) {
        const note = $('#c-par2-single-note');
        if (note) note.classList.add('hidden');
        const help = $('#c-par2-single-help');
        if (help) help.setAttribute('aria-expanded', 'false');
      }
      const singleBtn = $('#c-par2-single');
      if (singleBtn) singleBtn.setAttribute('aria-checked', String(!!c.par2Single));
      const bytes = cTotalSize();
      $('#c-par2-hint').textContent = par2HintText(bytes);
    }
  }

  // name & dest
  if (!$('#c-name').value || !c.touchedName) {
    $('#c-name').value = c.name || defaultArchiveName();
  }
  $('#c-dest-text').textContent = c.dest ? midEllipsis(shortPath(c.dest), 34) : '与源文件相同';
  $('#c-start').disabled = !has || c.busy;
  $('#c-outhint').textContent = c.busy ? '正在创建…'
    : !has ? '选择文件后即可开始'
      : buildOutHint();
}

/** Status line under the PAR2 row. It reports the current state and nothing
 * else -- the single-volume explanation lives behind #c-par2-single-help. */
function par2HintText(bytes) {
  const c = state.c;
  if (!c.par2) return '压缩后追加冗余数据，占用额外空间';
  return bytes
    ? `约 ${fmtSize(Math.round(bytes * c.par2Pct / 100))} 冗余 + 元数据${c.par2Single ? ' · 单卷' : ''}`
    : `冗余 ${c.par2Pct}%${c.par2Single ? ' · 单卷' : ''}`;
}

function buildOutHint() {
  const c = state.c;
  const fmt = (state.meta?.formats || []).find((f) => f.id === c.format);
  const bits = [fmt ? fmt.label : c.format.toUpperCase()];
  bits.push(LEVEL_NAMES[c.level]);
  if (c.pw && (!fmt || fmt.encrypt !== false)) {
    bits.push(c.format === 'zip' ? 'AES-256 加密' : '加密' + (c.encryptNames ? ' + 隐藏文件名' : ''));
  }
  if (c.splitBytes > 0 && (!fmt || fmt.split !== false)) bits.push('分卷 ' + fmtSize(c.splitBytes));
  if (c.par2) bits.push(`恢复记录 ${c.par2Pct}%${c.par2Single ? ' · 单卷' : ''}`);
  if (c.exists) bits.push('同名文件将被自动改名');
  return bits.join(' · ');
}

function defaultArchiveName() {
  const first = state.c.items[0];
  if (!first) return '';
  if (state.c.items.length > 1) return '归档';
  let n = first.name;
  if (!first.isDir) n = n.replace(/\.[^.]+$/, '');
  return n;
}

function parseSizeInput(text) {
  const m = String(text || '').trim().toLowerCase().match(/^([\d.]+)\s*([kmgt]?)b?$/);
  if (!m) return 0;
  const mult = { '': 1, k: 1024, m: 1024 ** 2, g: 1024 ** 3, t: 1024 ** 4 }[m[2]];
  return Math.round(parseFloat(m[1]) * mult);
}

const refreshTarget = debounce(async () => {
  const c = state.c;
  if (!c.items.length) { c.target = ''; $('#c-outpath').textContent = '—'; return; }
  const dest = c.dest || dirName(c.items[0].path);
  const name = ($('#c-name').value || defaultArchiveName());
  try {
    const r = await api('/preflight', { destDir: dest, name, format: c.format });
    c.target = r.target;
    c.exists = r.exists;
    $('#c-outpath').textContent = midEllipsis(shortPath(r.target), 56);
    $('#c-outhint').textContent = buildOutHint();
  } catch (err) {
    c.target = '';
    $('#c-outpath').textContent = midEllipsis(shortPath(join(dest, name + cFormatExt())), 56);
  }
}, 220);

/* ---------------------------------------------------------------- extract UI */
function overHintText(mode) {
  if (mode === 'overwrite') return '同名旧文件会被替换';
  if (mode === 'skip') return '保留旧文件，对应的新文件不写入';
  return '保留旧文件，新解出的文件自动换名写入';
}

function renderExtract() {
  const x = state.x;
  const panel = $('#x-panel');
  const has = !!x.path;
  $('#drop-x').classList.toggle('hidden', has);
  panel.classList.toggle('hidden', !has);
  // Mark the empty state so the view can centre its drop target instead of
  // leaving a screenful of dead air below it.
  $('#view-extract').classList.toggle('is-empty', !has);
  $('#x-error').classList.toggle('hidden', !x.error);
  $('#x-show-all').classList.add('hidden');
  if (!has || x.loading || x.error) x.expanded = false;

  if (!has) {
    $('#x-start').disabled = true;
    return;
  }
  const info = x.info || {};
  const locked = !!info.needsPassword && !(info.entries || []).length;
  $('#x-name').textContent = baseName(x.path);
  $('#x-path').textContent = midEllipsis(shortPath(dirName(x.path)), 40);
  $('#x-fmt').textContent = info.label || '—';
  $('#x-size').textContent = info.size ? fmtSize(info.size) : '—';
  $('#x-raw').textContent = info.uncompressed ? fmtSize(info.uncompressed) : '—';
  $('#x-count').textContent = locked ? '—' : (info.counts ? `${info.counts.files}` : '—');
  $('#x-chip-lock').classList.toggle('hidden', !info.encrypted);
  $('#x-chip-split').classList.toggle('hidden', !info.isSplit);
  $('#x-chip-par2').classList.toggle('hidden', !info.par2Files);
  if (info.par2Files) $('#x-chip-par2-t').textContent = `恢复记录 ${info.par2Files}`;
  $('#x-ratio').textContent = info.ratio ? `压缩率 ${Math.round(100 / Math.max(1, info.ratio))}%` : '';
  $('#x-pw-row').classList.toggle('hidden', !(info.needsPassword || info.encrypted));
  $('#x-remember').setAttribute('aria-checked', String(x.remember));
  $('#x-pw-status').textContent = x.error && (x.pw || info.savedPasswordUsed)
    ? '密码可能不正确，可在设置中清除后重新保存'
    : x.remember ? '密码可能不正确；保存后可在设置中删除重存'
      : info.savedPasswordUsed ? '已使用记住的密码' : '';

  if (x.loading) {
    $('#x-table').innerHTML = `<tr><td colspan="3" style="padding:22px 16px;color:var(--ink-3)">正在读取目录…</td></tr>`;
    $('#x-more').textContent = '';
    $('#x-outhint').textContent = '读取中…';
    $('#x-start').disabled = true;
    return;
  }

  if (locked) {
    $('#x-table').innerHTML = `
      <tr><td colspan="3" style="padding:26px 16px;text-align:center;color:var(--ink-3)">
        <div style="display:flex;align-items:center;justify-content:center;gap:8px">
          ${icon('i-lock', 'lock-ic')} 目录已加密 — 输入密码后可查看内容
        </div>
      </td></tr>`;
    $('#x-more').textContent = '文件列表已加密';
    // Starting a doomed job just to learn the password is wrong is noise:
    // gate the button on a non-empty password field instead.
    $('#x-start').disabled = !x.pw;
    $('#x-dest-text').textContent = x.dest ? midEllipsis(shortPath(x.dest), 34) : '与压缩包同目录';
    $('#x-folder').setAttribute('aria-checked', String(x.autoFolder));
    $('#x-over').value = x.overwrite;
    $('#x-over-hint').textContent = overHintText(x.overwrite);
    $('#x-outpath').textContent = midEllipsis(shortPath(finalDest()), 56);
    $('#x-outhint').textContent = x.pw ? '将用输入的密码解压' : '先输入密码，再开始解压';
    return;
  }

  const rows = (info.entries || []).filter((e) => !e.isDir);
  const dirs = (info.entries || []).filter((e) => e.isDir);
  const entries = x.error ? [] : [...dirs, ...rows];
  const show = x.expanded ? entries : entries.slice(0, 200);
  const more = $('#x-show-all');
  more.classList.toggle('hidden', entries.length <= show.length);
  more.textContent = `显示全部 ${entries.length} 条`;
  $('#x-table').innerHTML = show.length ? show.map((e) => `
    <tr data-entry="${esc(e.path)}">
      <td class="name ${e.isDir ? 'dir' : ''}">${icon(e.isDir ? 'i-folder' : 'i-file', 'lock-ic')} ${esc(e.path)}</td>
      <td class="num">${e.isDir ? '—' : fmtSize(e.size)}</td>
      <td>${esc((e.modified || '').slice(5).replace('T', ' ')) || '—'}</td>
    </tr>`).join('') : `<tr><td colspan="3" style="padding:22px 16px;color:var(--ink-3)">${x.error ? '无法读取目录' : '空归档'}</td></tr>`;

  const truncated = info.entriesTruncated || 0;
  $('#x-more').textContent = x.error ? '目录读取失败'
    : `${rows.length} 个文件 · ${dirs.length} 个文件夹`
      + (show.length < entries.length ? ` · 已显示 ${show.length} 条` : '')
      + (truncated > 0 ? ` · 另有 ${truncated} 条未返回（后端列表上限）` : '');
  $('#x-dest-text').textContent = x.dest ? midEllipsis(shortPath(x.dest), 34) : '与压缩包同目录';
  $('#x-folder').setAttribute('aria-checked', String(x.autoFolder));
  $('#x-over').value = x.overwrite;
  $('#x-over-hint').textContent = overHintText(x.overwrite);
  $('#x-start').disabled = !!x.error;
  const dest = finalDest('display');
  $('#x-outpath').textContent = midEllipsis(shortPath(dest), 56);
  $('#x-outhint').textContent = x.error ? '无法解压该文件'
    : `${info.needsPassword ? '需要密码' : '无需密码'} · 重名 ${({ rename: '自动改名', overwrite: '覆盖', skip: '跳过' })[x.overwrite]}`;
}

function finalDest() {
  const x = state.x;
  const root = x.dest || dirName(x.path);
  if (!x.autoFolder) return root;
  let stem = baseName(x.path)
    .replace(/\.(zip|7z|rar|tar|gz|bz2|xz)\.\d{3}$/i, '')
    .replace(/\.(zip|7z|rar|tar|gz|bz2|xz)$/i, '')
    .replace(/\.part\d+$/i, '');
  return join(root, stem || '解压结果');
}

/* ------------------------------------------------------------------ queue UI */
const KIND_ICON = { compress: 'i-compress', extract: 'i-extract', test: 'i-check-circle' };

function jobSub(job) {
  if (job.status === 'running') {
    const bits = [job.phase || '处理中'];
    if (job.item) bits.push(job.item);
    if (fmtSpeed(job.speed)) bits.push(fmtSpeed(job.speed));
    const eta = job.eta != null && job.percent > 2 ? fmtEta(job.eta) : '';
    if (eta) bits.push(eta);
    return bits.map((b) => esc(b)).join('<span class="sep">·</span>');
  }
  if (job.status === 'queued') return '等待中 · 前面还有任务';
  if (job.status === 'done') {
    const bits = [];
    if (job.result && job.result.parts > 1) bits.push(`${job.result.parts} 个分卷`);
    if (job.sizeText) bits.push(job.sizeText);
    if (job.result && job.result.ratio > 1) bits.push(`压缩比 ${job.result.ratio}:1`);
    if (job.result && job.result.par2) {
      bits.push(`恢复记录 ${job.result.par2.sizeText}`
        + (job.result.par2.single ? '（单卷）' : `（${job.result.par2.files} 个文件）`));
    }
    if (job.result && job.result.par2Error) bits.push('恢复记录未生成');
    if (job.result && job.result.repaired) bits.push('已修复');
    if (job.result && job.result.recoveryFiles) bits.push(`${job.result.recoveryFiles} 个恢复文件`);
    if (job.destPath) bits.push(midEllipsis(shortPath(job.destPath), 46));
    return bits.map((b) => esc(b)).join('<span class="sep">·</span>');
  }
  if (job.status === 'error') {
    // 一句可理解的原因放主行；长错误收进可聚焦的「详情」，不铺满卡片。
    const raw = String(job.error || '失败');
    const short = raw.length > 72 ? raw.slice(0, 72) + '…' : raw;
    const detail = raw.length > 72 || job.srcPath || job.destPath
      ? `<details class="job-err"><summary tabindex="0" aria-label="展开任务详情">详情</summary>`
        + `<div>${esc(raw)}${job.srcPath ? `<br>来源：${esc(job.srcPath)}` : ''}`
        + `${job.destPath ? `<br>输出：${esc(job.destPath)}` : ''}</div></details>`
      : '';
    return `<span style="color:var(--err)">${esc(short)}</span>${detail}`;
  }
  return '已取消';
}

/* The queue re-renders on every poll (450 ms while something runs).  Replacing
   innerHTML there threw away keyboard focus and any text the user was selecting
   -- which is exactly what you want to do with a long error message.  So the
   list is built once per job and then patched in place. */
const QNODES = new Map();          // job id -> card element

function jobActions(job) {
  const actions = [];
  if (job.status === 'running' || job.status === 'queued') {
    actions.push(`<button class="icon-btn" data-cancel="${job.id}" title="取消任务（已写出的文件会保留）" aria-label="取消任务，已写出的文件会保留">${icon('i-stop')}</button>`);
  }
  if (job.status === 'error') {
    actions.push(`<button class="btn ghost" data-retry="${job.id}" aria-label="重试这个任务">重试</button>`);
  }
  if (job.status === 'done') {
    actions.push(`<button class="icon-btn" data-reveal="${esc(job.outputs[0] || job.destPath || '')}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon('i-reveal')}</button>`);
    if (job.kind !== 'test' && job.outputs[0]) {
      actions.push(`<button class="icon-btn" data-open="${esc(job.outputs[0])}" title="打开" aria-label="打开">${icon('i-external')}</button>`);
    }
  }
  if (job.status !== 'running' && job.status !== 'queued') {
    actions.push(`<button class="icon-btn" data-remove="${job.id}" title="从列表移除" aria-label="从列表移除，不删除磁盘文件">${icon('i-x')}</button>`);
  }
  return actions;
}

function makeJobEl(job) {
  const el = document.createElement('div');
  el.className = `job ${job.status}`;
  el.dataset.job = job.id;
  el.innerHTML = `
    <div class="job-ico"></div>
    <div class="job-main">
      <div class="job-top"><b></b><span class="pct"></span></div>
      <div class="job-sub"></div>
      <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i></i></div>
    </div>
    <div class="job-actions"></div>`;
  return el;
}

function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

/** True when the user is selecting text inside `el` -- skip content swaps then. */
function hasSelectionIn(el) {
  const sel = window.getSelection && window.getSelection();
  if (!sel || !sel.rangeCount || sel.isCollapsed) return false;
  return el.contains(sel.anchorNode) || el.contains(sel.focusNode);
}

function patchJobEl(el, job) {
  el.className = `job ${job.status}`;
  const pct = job.status === 'done' ? 100 : job.percent;
  const pctText = job.status === 'running' ? Math.round(pct) + '%'
    : job.status === 'done' ? '已完成'
      : job.status === 'error' ? '失败' : '已取消';
  setText(el.querySelector('.job-top b'), job.title);
  // Percentage belongs beside the title; a final state belongs with the action
  // buttons so the label and the icon share one vertical centre.
  setText(el.querySelector('.job-top .pct'), job.status === 'running' ? pctText : '');

  const ico = el.querySelector('.job-ico');
  const iconId = KIND_ICON[job.kind] || 'i-box';
  if (ico.dataset.icon !== iconId) {
    ico.innerHTML = icon(iconId);
    ico.dataset.icon = iconId;
  }

  const sub = el.querySelector('.job-sub');
  const subHtml = jobSub(job);
  if (sub.dataset.html !== subHtml && !hasSelectionIn(sub)) {
    sub.innerHTML = subHtml;
    sub.dataset.html = subHtml;
  }

  const bar = el.querySelector('.bar');
  // 结束态收起贯穿整行的进度条：完成不再是满宽绿条，失败/取消同理，
  // 状态由文字与图标承载（详见任务书 §5.3「收起长进度条」）。
  const finished = job.status === 'done' || job.status === 'error' || job.status === 'canceled';
  bar.classList.toggle('hidden', finished);
  if (!finished) {
    bar.classList.toggle('indet', job.status === 'running' && pct < 1);
    bar.querySelector('i').style.width = `${pct}%`;
    bar.setAttribute('aria-valuenow', String(Math.round(pct)));
    bar.setAttribute('aria-valuetext', pctText);
  }
  bar.setAttribute('aria-label', job.title);

  const box = el.querySelector('.job-actions');
  const statusHtml = job.status === 'running' ? '' : `<span class="pct">${esc(pctText)}</span>`;
  const sig = statusHtml + jobActions(job).join('');
  if (box.dataset.sig !== sig) {
    box.innerHTML = sig;
    box.dataset.sig = sig;
  }
}

function historyPath(item) {
  return (item.outputs && item.outputs[0]) || item.destPath || item.srcPath || '';
}

function historyLabel(item) {
  return item.status === 'done' ? '已完成' : item.status === 'error' ? '失败' : '已取消';
}

function renderHistory(list) {
  const history = state.history.slice(0, 20);
  const sig = history.map((item) => `${item.id}:${item.status}:${item.finished || ''}`).join('|');
  if (list.dataset.historySig === sig) return;
  list.dataset.historySig = sig;
  list.innerHTML = history.length ? `${history.map((item) => {
    const path = historyPath(item);
    // The right-hand column already says 已完成/失败/已取消, so the subtitle must
    // not repeat it: fall back to the archive type instead.
    const result = item.result && item.result.parts > 1 ? `${item.result.parts} 个分卷`
      : item.sizeText || item.format || '—';
    const reveal = path ? `<button class="icon-btn" data-reveal="${esc(path)}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon('i-reveal')}</button>` : '';
    return `<div class="job history-item" data-history="${esc(item.id)}">
      <div class="job-ico ${esc(item.status || 'done')}">${icon(KIND_ICON[item.kind] || 'i-box')}</div>
      <div class="job-main"><div class="job-top"><b>${esc(item.title || '历史任务')}</b></div>
      <div class="job-sub">${esc(fmtDate(item.finished))}<span class="sep">·</span>${esc(result)}</div></div>
      <div class="job-actions"><span class="pct">${esc(historyLabel(item))}</span>${reveal}</div>
    </div>`;
  }).join('')}` : '';
}

function renderQueue() {
  const list = $('#q-list');
  const jobs = state.jobs;
  const showHistory = jobs.length === 0 && state.history.length > 0;
  $('#q-empty').classList.toggle('hidden', jobs.length > 0 || showHistory);
  $('#q-meta').textContent = jobs.length ? `${jobs.length} 项` : showHistory ? `${Math.min(state.history.length, 20)} 条历史` : '';
  const active = jobs.filter((j) => j.status === 'running').length;
  // The panel title has to match what is actually listed: "进行中与最近" read
  // wrong once the list was pure history.
  $('#q-title').textContent = active ? '进行中'
    : (jobs.length || state.history.length) ? '最近任务' : '任务';
  // Clearing also drops persisted history, so name the action after the rows on
  // screen. An armed confirm label must survive the next poll repaint.
  const clearBtn = $('#q-clear');
  if (!clearBtn.dataset.armed) {
    clearBtn.textContent = (!jobs.length && state.history.length) ? '清除历史' : '清理已完成';
  }
  $('#nav-count').classList.toggle('hidden', active === 0);
  $('#nav-count').textContent = String(active);

  if (showHistory) {
    [...QNODES.values()].forEach((el) => { if (el.parentNode) el.remove(); });
    QNODES.clear();
    renderHistory(list);
    return;
  }
  if (list.dataset.historySig) {
    list.innerHTML = '';
    list.dataset.historySig = '';
  }
  const seen = new Set();
  // 面板秩序（§5.3）：进行中、等待在最上，失败其次，完成沉底——已完成的
  // 任务不再抢占注意力。稳定排序保持同组内的队列顺序。
  const RANK = { running: 0, queued: 1, error: 2, canceled: 3, done: 4 };
  queueNodeSync([...jobs].sort((a, b) => (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9)), list, seen);
}

function queueNodeSync(jobs, list, seen) {
  jobs.forEach((job, i) => {
    let el = QNODES.get(job.id);
    if (!el) {
      el = makeJobEl(job);
      QNODES.set(job.id, el);
    }
    patchJobEl(el, job);
    seen.add(job.id);
    if (list.children[i] !== el) list.insertBefore(el, list.children[i] || null);
  });
  [...QNODES.keys()].forEach((id) => {
    if (!seen.has(id)) {
      const el = QNODES.get(id);
      if (el && el.parentNode) el.remove();
      QNODES.delete(id);
    }
  });
}

/** Sidebar "recent": the last three finished jobs. The rail's lower half used to
 *  be a blank column (53% empty at 764px, 64% at 982px), so it now carries the
 *  only thing worth putting there -- what just happened. */
function renderRecent() {
  const box = $('#rail-recent');
  const list = $('#rail-recent-list');
  if (!box || !list) return;
  const current = state.jobs
    .filter((j) => j.status === 'done' || j.status === 'error' || j.status === 'canceled')
    .slice(0, 3);
  const done = current.length ? current : state.history.slice(0, 3);
  box.classList.toggle('hidden', done.length === 0);
  const sig = done.map((j) => `${j.id}:${j.status}:${j.finished || ''}`).join('|');
  if (list.dataset.sig === sig) return;
  list.dataset.sig = sig;
  list.innerHTML = done.map((j) => {
    const status = historyLabel(j);
    const name = String(j.title || '').replace(/^(压缩|解压|校验)\s*/, '');
    return `<button class="recent-item" data-recent="${esc(j.id)}"
              title="${esc(j.title)} · ${status}">
      <span class="recent-ico ${esc(j.status || 'done')}">${icon(KIND_ICON[j.kind] || 'i-box')}</span>
      <span class="recent-name">${esc(name)}</span>
    </button>`;
  }).join('');
}

/* ------------------------------------------------------------------ polling */
function schedulePoll(fast) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(pollJobs, fast ? 450 : 1600);
}
async function pollJobs() {
  try {
    const data = await api('/jobs');
    const jobs = data.jobs || [];
    if (jobs.length === 0) {
      const history = await api('/history');
      state.history = history.history || [];
    }
    const liveIds = new Set(jobs.map((j) => j.id));
    Object.keys(state.jobStatus).forEach((id) => {
      if (!liveIds.has(id)) delete state.jobStatus[id];
    });
    jobs.forEach((j) => {
      const prev = state.jobStatus[j.id];
      if (prev && prev !== j.status) {
        if (j.status === 'done') {
          const extra = j.result && j.result.parts > 1 ? ` · ${j.result.parts} 个分卷` : '';
          toast(`${j.kind === 'test' ? '校验通过' : '完成'} · ${j.title.replace(/^(压缩|解压|校验) /, '')}${extra}`, 'ok');
          native('attention');
        } else if (j.status === 'error') {
          toast(j.error || '任务失败', 'err');
          native('attention');
        }
      }
      state.jobStatus[j.id] = j.status;
    });
    state.jobs = jobs;
    // The rail must follow the queue from any view -- renderQueue() only runs on
    // the task page, so calling it from there left "最近任务" stale while
    // compressing, which is exactly when it matters.
    renderRecent();
    if (state.view === 'queue') renderQueue();
    else {
      const running = jobs.filter((j) => j.status === 'running').length;
      $('#nav-count').classList.toggle('hidden', running === 0);
      $('#nav-count').textContent = String(running);
    }
    const busy = jobs.some((j) => j.status === 'running' || j.status === 'queued');
    schedulePoll(busy);
  } catch (err) {
    schedulePoll(false);
  }
}
async function refreshJobsNow() {
  clearTimeout(state.pollTimer);
  await pollJobs();
}

/* -------------------------------------------------------------------- views */
function setView(name, { instant = false } = {}) {
  state.view = name;
  $$('.view').forEach((v) => {
    const on = v.id === 'view-' + name;
    v.classList.toggle('is-active', on);
    // Keyboard-driven switches (⌘1/2/3) happen dozens of times a day; a
    // 220ms rise there reads as lag rather than polish.
    if (on) v.classList.toggle('is-instant', instant);
  });
  $$('.nav-item[data-nav]').forEach((b) => b.classList.toggle('is-active', b.dataset.nav === name));
  const m = VIEW_META[name];
  $('#view-title').textContent = m.title;
  $('#view-sub').textContent = m.sub;
  $('#scroll').scrollTop = 0;
  if (name === 'queue') renderQueue();
  if (name === 'compress') { renderCompress(); requestAnimationFrame(() => syncSeg($('#c-format'), { instant: true })); }
  if (name === 'extract') renderExtract();
}

/* ---------------------------------------------------------------- add inputs */
async function statPath(path) {
  try {
    const r = await api('/stat', { path });
    return { path, name: baseName(path), dir: dirName(path), size: r.size, isDir: r.isDir };
  } catch (_) {
    return null;
  }
}

async function addToCompress(paths) {
  if (!paths.length) return;
  const existing = new Set(state.c.items.map((i) => i.path));
  const fresh = paths.filter((p) => !existing.has(p));
  const dup = paths.length - fresh.length;
  // 重复添加不再静默（§5.1）：明确告诉用户哪些没进来、为什么。
  if (dup > 0 && !fresh.length) {
    toast(`${paths.length === 1 ? '这个文件' : `这 ${dup} 个文件`}已在列表中`, 'info');
    return;
  }
  const stats = (await Promise.all(fresh.map(statPath))).filter(Boolean);
  if (!stats.length) return;
  state.c.items.push(...stats);
  if (dup > 0) toast(`已添加 ${stats.length} 项 · ${dup} 项已在列表中`, 'ok');
  if (!state.c.touchedName) state.c.name = defaultArchiveName();
  if (!state.c.dest) {
    const dirs = new Set(stats.map((s) => s.dir));
    state.c.dest = dirs.size === 1 ? stats[0].dir : '';
  }
  setView('compress');
  renderCompress();
  refreshTarget();
  toast(`已添加 ${stats.length} 项`, 'info', 'i-plus');
}

const ARCHIVE_RE = /\.(zip|7z|rar|tar|gz|tgz|bz2|xz|zst)(\.\d{3}|\.part\d+\.rar|\.r\d{2}|\.z\d{2})?$/i;

async function loadArchive(path) {
  state.x.path = path;
  state.x.info = null;
  state.x.error = '';
  state.x.loading = true;
  state.x.expanded = false;
  state.x.dest = dirName(path);
  state.x.pw = '';
  state.x.remember = false;
  $('#x-pw').value = '';
  $('#x-pw').type = 'password';
  setView('extract');
  renderExtract();
  try {
    const r = await api('/inspect', { path });
    state.x.info = r.info;
    if (r.info.needsPassword) {
      state.x.error = r.info.savedPasswordUsed && !(r.info.entries || []).length
        ? '记住的密码未能解锁，请在设置中清除后重新保存' : '';
      setTimeout(() => $('#x-pw').focus(), 60);
    }
  } catch (err) {
    state.x.error = err.message;
    state.x.info = null;
  }
  state.x.loading = false;
  renderExtract();
}

/* ------------------------------------------------------------- drag and drop */
let dragDepth = 0;
let dragCache = null;
let peekAt = 0;

// While a drag hovers, ask the native side what the system drag pasteboard
// holds. WebKit owns the drag session, so this is the only way to learn the
// real file paths — the DOM event itself is stripped of them.
async function peekDrag(force) {
  if (!NATIVE) return null;
  const now = Date.now();
  if (!force && now - peekAt < 250) return dragCache;   // dragover fires ~60x/s
  peekAt = now;
  try {
    const r = await native('dragPaths', {});
    const paths = r && Array.isArray(r.paths) ? r.paths : [];
    if (paths.length) dragCache = paths;
  } catch (_) { /* keep whatever we had */ }
  return dragCache;
}

function dragPaths(dt) {
  const out = [];
  const raw = (dt.getData('text/uri-list') || '') + '\n' + (dt.getData('text/plain') || '');
  raw.split(/\r?\n/).forEach((line) => {
    const l = line.trim();
    if (!l || l.startsWith('#')) return;
    if (l.startsWith('file://')) {
      let p = l.replace(/^file:\/\/(localhost)?/, '');
      try { p = decodeURIComponent(p); } catch (_) { /* keep */ }
      if (p && out.indexOf(p) < 0) out.push(p);
    }
  });
  return out;
}
function wireDragDrop() {
  const overlay = $('#drag-overlay');
  const show = (n) => {
    overlay.classList.add('on');
    $('#drag-count').textContent = n ? `${n} 个文件将加入当前任务` : '文件将加入当前任务';
  };
  const hide = () => { dragDepth = 0; dragCache = null; overlay.classList.remove('on'); };

  // Peek through the bridge and keep the overlay count honest — WebKit strips
  // the file list from the DOM event, so its own count is usually zero.
  const peek = async (force) => {
    const paths = await peekDrag(force);
    if (paths && paths.length && dragDepth) show(paths.length);
    return paths;
  };

  // Drop is handled natively: WebKit never hands local file paths to an
  // http:// page, so AppKit collects them and calls these hooks instead.
  window.__arcaDragEnter = (n) => { dragDepth = 1; show(Number(n) || 0); };
  window.__arcaDragLeave = () => hide();
  window.__arcaDrop = (paths) => {
    hide();
    if (Array.isArray(paths) && paths.length) ingest(paths);
  };

  // Fallback for when the page runs in a browser (development, screenshots).
  window.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes('Files')) return;
    e.preventDefault();
    dragDepth += 1;
    show(e.dataTransfer.items ? e.dataTransfer.items.length : 0);
    peek(true);
  });
  window.addEventListener('dragover', (e) => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes('Files')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    peek();
  });
  window.addEventListener('dragleave', (e) => {
    if (e.relatedTarget === null) hide();
    else { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) hide(); }
  });
  window.addEventListener('drop', async (e) => {
    e.preventDefault();
    const own = dragPaths(e.dataTransfer);
    const paths = own.length ? own : (dragCache || await peekDrag(true) || []);
    hide();
    if (!paths.length) {
      toast('拖入的文件无法读取，请改用「选择文件」按钮', 'err');
      return;
    }
    await ingest(paths);
  });
}

let lastIngest = { key: '', at: 0 };
async function ingest(paths) {
  if (!paths || !paths.length) return;
  // Native shim and the DOM fallback can both report the same drop.
  const key = paths.join('\u0000');
  const now = Date.now();
  if (key === lastIngest.key && now - lastIngest.at < 800) return;
  lastIngest = { key, at: now };
  if (state.view === 'extract') {
    const arch = paths.find((p) => ARCHIVE_RE.test(p)) || paths[0];
    return loadArchive(arch);
  }
  if (state.view === 'queue') setView('compress');
  return addToCompress(paths);
}

/* ------------------------------------------------------------------- actions */
async function startCompress() {
  const c = state.c;
  if (!c.items.length || c.busy) return;
  const name = ($('#c-name').value || defaultArchiveName()).trim();
  if (!name) { toast('请填写归档名称', 'err'); return; }
  c.busy = true;
  $('#c-start').disabled = true;
  $('#c-outhint').textContent = '正在创建…';
  try {
    const r = await api('/compress', {
      inputs: c.items.map((i) => i.path),
      name,
      destDir: c.dest || dirName(c.items[0].path),
      format: c.format,
      level: c.level,
      method: c.method,
      splitBytes: c.splitBytes,
      password: c.pw,
      encryptNames: c.encryptNames,
      excludeMeta: c.excludeMeta,
      solid: c.solid,
      par2Percent: c.par2 ? c.par2Pct : 0,
      par2Single: c.par2 && !!c.par2Single,
    });
    toast(`开始压缩 · ${baseName(r.target)}`, 'ok', 'i-compress');
    setView('queue');
    refreshJobsNow();
  } catch (err) {
    toast(err.message, 'err');
    c.busy = false;
    renderCompress();
  }
  c.busy = false;
}

async function startExtract(onExists) {
  const x = state.x;
  if (!x.path || !x.info) return;
  try {
    const r = await api('/extract', {
      path: x.path,
      password: x.pw,
      remember: x.remember,
      info: x.info,
      destDir: x.dest || dirName(x.path),
      autoFolder: x.autoFolder,
      overwrite: x.overwrite,
      ...(onExists ? { onExists } : {}),
    });
    toast(`开始解压到 ${baseName(r.dest)}`, 'ok', 'i-extract');
    setView('queue');
    refreshJobsNow();
  } catch (err) {
    if (err.data && err.data.code === 'exists') {
      askExists(err.data);
      return;
    }
    toast(err.message, 'err');
  }
}

/** "That folder is already there": merge, make a copy, or stop.  The old flow
 * silently produced 包 2, 包 3 … and people never knew which copy was current. */
function askExists(data) {
  const destName = baseName(data.dest || '');
  askChoice({
    title: `「${destName}」已存在`,
    body: '目标位置已有同名文件夹。合并会把新文件解压进去（按当前覆盖规则处理重名）；'
      + '新建副本会解压到「' + (data.suggestion ? baseName(data.suggestion) : '新文件夹') + '」。',
    actions: [
      { label: '取消', kind: 'quiet', value: null },
      { label: '新建副本', kind: 'ghost', value: 'rename' },
      { label: '合并', kind: 'primary', value: 'merge' },
    ],
  }).then((choice) => { if (choice) startExtract(choice); });
}

async function testArchive() {
  const x = state.x;
  if (!x.path) return;
  try {
    await api('/test', { path: x.path, password: x.pw, remember: x.remember, info: x.info });
    setView('queue');
    refreshJobsNow();
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* ------------------------------------------------------- recovery records */
// PAR2 verification is a long job (it re-reads every byte), so it goes through
// the same queue as everything else instead of blocking the dialog.
async function runPar2(path, action) {
  if (!path) return;
  try {
    await api(`/par2/${action}`, { path });
    setView('queue');
    refreshJobsNow();
  } catch (err) {
    toast(err.message, 'err');
  }
}

// `count` is the number of .par2 files found next to the archive; zero means
// the action would only fail, so the whole group is hidden.
function par2Menu(path, count) {
  if (!count) return [];
  const verify = { label: '校验恢复记录', icon: 'i-check-circle',
    action: () => runPar2(path, 'verify') };
  if (!state.meta?.par2?.available) {
    verify.disabled = true;
    verify.title = '缺少 par2 组件';
  }
  return [
    verify,
    { label: '修复损坏的文件', icon: 'i-refresh', danger: true,
      disabled: !state.meta?.par2?.available,
      action: () => runPar2(path, 'repair') },
  ];
}

// Appends a separator plus the recovery items when the archive actually has
// them, so menus that do not offer recovery stay exactly as before.
function withPar2(items, path, count) {
  const extra = par2Menu(path, count);
  if (!extra.length) return items;
  if (items.length) items.push({ sep: true });
  items.push(...extra);
  return items;
}

/* -------------------------------------------------------------- window drag */
// The window has a full-size web view, so WebKit owns every mouse-down and the
// title bar can no longer move it. The native side therefore keeps an
// invisible strip over the header and needs two things from the page: where
// that header is, and which controls sit inside it (those must keep falling
// through to the page, or the header buttons go dead).
function reportDragRegion() {
  const header = document.querySelector('.topbar');
  if (!NATIVE || !header) return;
  // The strip spans the whole top band, not just the header: the empty space
  // above the sidebar is where a normal title bar would be, and that is what
  // people reach for. The brand sits in that band, so it is reported as a hole
  // and keeps behaving like a normal, non-draggable element.
  // A menu or the settings sheet can sit inside that band, and the native
  // strip would swallow clicks meant for them. Collapse it to nothing while
  // any overlay is up; the page reports again when it closes.
  const overlay = document.querySelector('.menu, .sheet-back');
  const band = overlay ? 0 : Math.round(header.getBoundingClientRect().bottom);
  const holes = [];
  document.querySelectorAll('.brand, .top-actions, button, input, a, [data-no-drag]')
    .forEach((el) => {
      const b = el.getBoundingClientRect();
      if (b.width > 0 && b.height > 0 && b.top < band && b.bottom > 0) {
        holes.push([Math.round(b.left), Math.round(b.top),
                    Math.round(b.width), Math.round(b.height)]);
      }
    });
  native('dragRegion', {
    rect: [0, 0, Math.round(window.innerWidth), band],
    holes,
  });
}

/* ------------------------------------------------------------ clipboard glue */
async function copyText(text) {
  if (NATIVE) {
    const r = await native('copy', { text });
    return !!(r && r.ok);
  }
  try { await navigator.clipboard.writeText(text); return true; }
  catch (_) { return false; }
}

async function copyPaths(paths) {
  const list = (paths || []).filter(Boolean);
  if (!list.length) return;
  const ok = await copyText(list.join('\n'));
  if (!ok) { toast('无法写入剪贴板', 'err'); return; }
  toast(list.length > 1 ? `已拷贝 ${list.length} 条路径` : '已拷贝路径', 'info', 'i-copy');
}

/** Files sitting on the general pasteboard — "paste" from Finder, in effect. */
async function clipboardFiles() {
  if (!NATIVE) return [];
  const r = await native('clipPaths', {});
  return (r && Array.isArray(r.paths)) ? r.paths : [];
}

// A menu must appear on the same tick as the click, so the pasteboard is read
// ahead of time and the cached count is what the item shows. The count is
// decoration only — the item stays enabled, because a stale cache must never
// stop the user from pasting what they copied a second ago.
let clipCache = null;
async function refreshClipCache() {
  try { clipCache = await clipboardFiles(); } catch (_) { clipCache = null; }
  return clipCache || [];
}

async function pasteFromClipboard() {
  const paths = await refreshClipCache();
  if (!paths.length) { toast('剪贴板里没有文件', 'info', 'i-clipboard'); return; }
  await addToCompress(paths);
}

/* --------------------------------------------------------------- list ops */
function clearCompressList() {
  if (!state.c.items.length) return;
  state.c.items = [];
  state.c.dest = '';
  state.c.name = '';
  state.c.touchedName = false;
  renderCompress();
  refreshTarget();
}

function removeCompressItem(idx) {
  if (!state.c.items[idx]) return;
  state.c.items.splice(idx, 1);
  renderCompress();
  refreshTarget();
}

async function cancelJob(id) {
  await api('/job/cancel', { id });
  refreshJobsNow();
}

async function removeJob(id) {
  await api('/job/remove', { id });
  refreshJobsNow();
}

/** Re-run a finished compression with the exact same inputs. */
async function recompress(job) {
  const inputs = (job.inputs || []).filter(Boolean);
  if (!inputs.length) { toast('原始文件列表已不可用', 'err'); return; }
  setView('compress');
  state.c.items = [];
  state.c.touchedName = false;
  await addToCompress(inputs);
}

/** Extract to a directory picked on the spot, then start right away. */
async function extractTo() {
  const x = state.x;
  if (!x.path || !x.info) return;
  const paths = await pick('dest', { title: '解压到…', dir: x.dest || dirName(x.path) });
  if (!paths.length) return;
  x.dest = paths[0];
  renderExtract();
  startExtract();
}

/* ------------------------------------------------------------- context menu */
function menuCompressRow(idx) {
  const it = state.c.items[idx];
  if (!it) return null;
  return [
    { label: '开始压缩', icon: 'i-compress', kbd: '⌘↵', action: startCompress },
    { label: '仅压缩此项', disabled: state.c.items.length === 1, action: () => {
        state.c.items = [it];
        state.c.name = '';
        state.c.touchedName = false;
        renderCompress();
        refreshTarget();
      } },
    { sep: true },
    { label: '在 Finder 中显示', icon: 'i-folder', action: () => native('reveal', { path: it.path }) },
    { label: '拷贝路径', icon: 'i-copy', action: () => copyPaths([it.path]) },
    { sep: true },
    { label: '从列表移除', icon: 'i-x', danger: true, action: () => removeCompressItem(idx) },
    { label: '清空列表', icon: 'i-trash', danger: true, action: clearCompressList },
  ];
}

/* ------------------------------------------------------- update shortcuts */
// Three menus offer the same update entry points; keeping them in one place
// stops the wording and the availability rules from drifting apart.
async function checkEngineNow() {
  const info = await checkEngineUpdate({ refresh: true, verbose: true });
  if (!info) return;
  if (!info.ok) { toast(info.error || '检查更新失败', 'err'); return; }
  if (info.available) openEngineUpdate(info);
  else toast(`已是最新版本 7-Zip ${info.latest || info.current}`, 'ok');
}

function updateItems() {
  const items = [{ label: '检查引擎更新…', icon: 'i-refresh', action: checkEngineNow }];
  const comps = (state.meta?.components || []).filter((c) => c.id !== '7zz');
  if (comps.length) {
    items.push({ label: '检查组件更新…', icon: 'i-layers',
      action: () => openSettings('#set-components') });
  }
  return items;
}

function menuCompressBlank() {
  const n = (clipCache || []).length;
  return [
    { label: '添加文件…', icon: 'i-plus', kbd: '⌘O', action: () => $('#c-pick').click() },
    { label: '添加文件夹…', icon: 'i-folder',
      action: async () => addToCompress(await pick('folder', { prompt: '添加' })) },
    { label: n ? `从剪贴板添加 ${n} 项` : '从剪贴板添加',
      icon: 'i-clipboard', action: pasteFromClipboard },
    { sep: true },
    { label: '清空列表', icon: 'i-trash', danger: true,
      disabled: !state.c.items.length, action: clearCompressList },
    ...updateItems(),
    { label: '设置…', icon: 'i-gear', action: openSettings },
  ];
}

function menuExtractRow(entry) {
  const x = state.x;
  return withPar2([
    { label: '拷贝条目名称', icon: 'i-copy', action: () => copyPaths([entry]) },
    { sep: true },
    { label: '在 Finder 中显示压缩包', icon: 'i-folder', action: () => native('reveal', { path: x.path }) },
    { label: '拷贝压缩包路径', icon: 'i-file', action: () => copyPaths([x.path]) },
    { sep: true },
    { label: '解压到…', icon: 'i-extract', disabled: !!x.error, action: extractTo },
    { label: '仅校验', icon: 'i-check-circle', action: testArchive },
  ], x.path, x.info && x.info.par2Files);
}

function menuExtractBlank() {
  const x = state.x;
  if (!x.path) {
    return [
      { label: '选择压缩包…', icon: 'i-plus', kbd: '⌘O', action: () => $('#x-pick').click() },
      { label: '压缩文件', icon: 'i-compress', action: () => setView('compress') },
      { sep: true },
      { label: '设置…', icon: 'i-gear', action: openSettings },
    ];
  }
  const ready = !!x.info && !x.error;
  return withPar2([
    { label: '开始解压', icon: 'i-extract', kbd: '⌘↵', disabled: !ready, action: startExtract },
    { label: '解压到…', icon: 'i-folder', disabled: !ready, action: extractTo },
    { label: '仅校验', icon: 'i-check-circle', disabled: !x.info, action: testArchive },
    { sep: true },
    { label: '在 Finder 中显示压缩包', icon: 'i-folder', action: () => native('reveal', { path: x.path }) },
    { label: '拷贝压缩包路径', icon: 'i-copy', action: () => copyPaths([x.path]) },
    { sep: true },
    { label: '更换压缩包…', icon: 'i-refresh', action: () => $('#x-change').click() },
  ], x.path, x.info && x.info.par2Files);
}

function menuJob(job) {
  if (!job || !job.id) return null;
  const live = job.status === 'running' || job.status === 'queued';
  const done = job.status === 'done';
  const target = (job.outputs && job.outputs[0]) || job.destPath || '';
  const items = [];

  if (live) {
    items.push({ label: '取消任务', icon: 'i-stop', danger: true, action: () => cancelJob(job.id) });
  }
  if (done && target) {
    items.push(
      { label: '在 Finder 中显示', icon: 'i-folder', action: () => native('reveal', { path: target }) },
      { label: job.kind === 'extract' ? '打开所在位置' : '打开归档', icon: 'i-external',
        action: () => native('open', { path: target }) },
      { label: '拷贝路径', icon: 'i-copy', action: () => copyPaths([target]) },
    );
  }
  if (job.status === 'error') {
    items.push({ label: '拷贝错误信息', icon: 'i-copy', action: () => copyPaths([job.error || '']) });
  }

  const redo = [];
  if (job.kind === 'compress' && (job.inputs || []).length) {
    redo.push({ label: '再次压缩这些文件', icon: 'i-refresh', action: () => recompress(job) });
  }
  if (job.kind === 'extract' && job.srcPath) {
    redo.push({ label: '再次解压', icon: 'i-refresh', action: () => loadArchive(job.srcPath) });
  }
  if ((job.kind === 'test' || job.kind === 'par2') && job.srcPath) {
    redo.push({ label: '再次校验', icon: 'i-refresh', action: async () => {
      try {
        await api(job.kind === 'par2' ? '/par2/verify' : '/test', { path: job.srcPath });
        setView('queue');
        refreshJobsNow();
      } catch (err) { toast(err.message, 'err'); }
    } });
  }
  if (redo.length) {
    if (items.length) items.push({ sep: true });
    items.push(...redo);
  }
  // Recovery actions belong to finished jobs that produced or used .par2 files,
  // otherwise the menu would offer a repair that has nothing to repair from.
  const hasRecov = done && target && (job.kind === 'par2'
    || (job.result && (job.result.par2 || job.result.recoveryFiles)));
  if (hasRecov) {
    if (items.length) items.push({ sep: true });
    items.push(...par2Menu(job.srcPath || target, 1));
  }
  if (!live) {
    if (items.length) items.push({ sep: true });
    items.push({ label: '从列表移除', icon: 'i-x', danger: true, action: () => removeJob(job.id) });
  }
  return items;
}

function menuQueueBlank() {
  const finished = state.jobs.filter((j) => j.status !== 'running' && j.status !== 'queued').length;
  // The endpoint clears the persisted history too, so count it — otherwise the
  // entry reads "disabled" while the list still shows history rows.
  const records = finished + (state.jobs.length ? 0 : state.history.length);
  return [
    { label: '刷新任务列表', icon: 'i-refresh', action: refreshJobsNow },
    { label: records ? `清理已完成（${records}）` : '清理已完成', icon: 'i-trash',
      disabled: !records,
      action: async () => { await api('/jobs/clear', {}); refreshJobsNow(); } },
    { sep: true },
    { label: '压缩文件', icon: 'i-compress', action: () => setView('compress') },
    ...updateItems(),
    { label: '设置…', icon: 'i-gear', action: openSettings },
  ];
}

function menuGlobal() {
  const mode = state.settings.appearance || 'system';
  return [
    { label: '压缩文件', icon: 'i-compress', action: () => setView('compress') },
    { label: '解压压缩包', icon: 'i-extract', action: () => setView('extract') },
    { sep: true },
    { label: '外观：自动', checked: mode === 'system', action: () => setAppearance('system') },
    { label: '外观：浅色', checked: mode === 'light', action: () => setAppearance('light') },
    { label: '外观：深色', checked: mode === 'dark', action: () => setAppearance('dark') },
    { sep: true },
    ...updateItems(),
    { label: '设置…', icon: 'i-gear', action: openSettings },
  ];
}

function wireContextMenu() {
  const MENU = window.ArcaMenu;
  if (!MENU) return;

  // Everything below runs synchronously: the menu has to be on screen in the
  // same frame as the click, so nothing in this path may await the bridge.
  document.addEventListener('contextmenu', (e) => {
    const t = e.target;
    if (!(t instanceof Element) || t.closest('.menu')) return;
    // Text fields keep the system menu: it is the only way to reach
    // Copy / Paste / Look Up inside the password and name inputs.
    if (t.closest('input, textarea, select, [contenteditable="true"]')) return;
    e.preventDefault();

    let items = null;
    const row = t.closest('#c-list .file-row');
    const jobEl = t.closest('.job[data-job]');
    const entryRow = t.closest('#x-table tr[data-entry]');

    if (row) items = menuCompressRow(Number(row.dataset.idx));
    else if (jobEl) items = menuJob(state.jobs.find((j) => j.id === jobEl.dataset.job));
    else if (entryRow) items = menuExtractRow(entryRow.dataset.entry);
    else if (t.closest('.rail, .topbar')) items = menuGlobal();
    else if (state.view === 'compress') items = menuCompressBlank();
    else if (state.view === 'extract') items = menuExtractBlank();
    else if (state.view === 'queue') items = menuQueueBlank();
    else items = menuGlobal();

    if (items && items.length) MENU.open({ x: e.clientX, y: e.clientY, items });
    // Keep the paste count current for the next open.
    refreshClipCache();
  });
}

/* ------------------------------------------------- Finder / 默认打开方式 */
/** What the Finder service an the 打开方式 association look like right now. */
async function loadIntegration() {
  try {
    const r = await api('/integration');
    state.integration = r;
    return r;
  } catch (err) {
    return null;
  }
}

/** Requests that arrive from outside the window: Finder 服务、打开方式、
 *  双击、拖到 Dock 图标。 它们先落在一个队列里（冷启动时页面还没加载完，
 *  直接 eval JS 会丢），这里定时取走并执行。 */
async function pollInbox() {
  if (pollInbox.busy) return;
  pollInbox.busy = true;
  try {
    const r = await api('/inbox');
    for (const item of (r.items || [])) await handleInbox(item);
  } catch (_) {
    /* shutting down, or a stale page during reload */
  }
  pollInbox.busy = false;
}
pollInbox.busy = false;

async function handleInbox(item) {
  const paths = (item.paths || []).filter(Boolean);
  if (!paths.length) return;
  if (item.action === 'compress') {
    const fmt = state.settings.serviceFormat;
    if (fmt && fmt !== state.c.format) {
      state.c.format = fmt;
      renderCompress();
    }
    await addToCompress(paths);
    if (state.settings.serviceAutoStart === false) {
      toast(`已加入压缩列表 · ${paths.length} 项`, 'info', 'i-plus');
      return;
    }
    await startCompress();
    return;
  }
  const arch = paths.filter((p) => ARCHIVE_RE.test(p));
  if (!arch.length) { toast('选中的项目里没有压缩包', 'err'); return; }
  if (arch.length === 1) {
    if (state.x.path === arch[0] && state.x.info) { setView('extract'); return; }
    await loadArchive(arch[0]);
    toast('来自 Finder · ' + baseName(arch[0]), 'ok', 'i-extract');
    return;
  }
  // 多个压缩包没有可预览的目标，直接各自解到旁边，结果都在任务页。
  let started = 0;
  for (const p of arch) {
    try {
      await api('/extract', { path: p, destDir: dirName(p), autoFolder: true,
                              overwrite: state.settings.overwrite || 'rename' });
      started += 1;
    } catch (err) {
      toast(`${baseName(p)}：${err.message}`, 'err');
    }
  }
  if (started) {
    setView('queue');
    refreshJobsNow();
    toast(`开始解压 ${started} 个压缩包`, 'ok', 'i-extract');
  }
}

window.arcaInbox = pollInbox;

/* -------------------------------------------------------------------- modals */
const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** A sheet is `aria-modal`, so Tab must stay inside it and the page behind it
 * must go inert -- otherwise the keyboard walks into controls the user cannot
 * see, and after Esc the focus is gone.  Returns the restore function. */
function modalize(back) {
  const shell = document.querySelector('.shell');
  const opener = document.activeElement;
  if (shell && 'inert' in shell) shell.inert = true;
  const onKey = (e) => {
    if (e.key !== 'Tab') return;
    const items = [...back.querySelectorAll(FOCUSABLE)]
      .filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  back.addEventListener('keydown', onKey);
  const firstFocus = back.querySelector(FOCUSABLE);
  (firstFocus || back).focus({ preventScroll: true });
  return () => {
    back.removeEventListener('keydown', onKey);
    if (shell && 'inert' in shell) shell.inert = false;
    if (opener && opener.isConnected && opener.focus) opener.focus({ preventScroll: true });
  };
}

/** Small decision dialog: resolves with the chosen value, or null on cancel. */
function askChoice({ title, body, actions }) {
  return new Promise((resolve) => {
    const back = document.createElement('div');
    back.className = 'sheet-back';
    back.innerHTML = `
      <div class="sheet sheet-ask" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="ask-body">
          <h2>${esc(title)}</h2>
          <p>${esc(body)}</p>
        </div>
        <div class="sheet-foot">
          ${actions.map((a, i) => `<button class="btn ${a.kind}" data-choice="${i}">${esc(a.label)}</button>`).join('')}
        </div>
      </div>`;
    document.body.appendChild(back);
    reportDragRegion();
    const restore = modalize(back);
    let done = false;
    const close = (value) => {
      if (done) return;
      done = true;
      restore();
      back.remove();
      reportDragRegion();
      resolve(value);
    };
    back.addEventListener('click', (e) => {
      if (e.target === back) { close(null); return; }
      const btn = e.target.closest('[data-choice]');
      if (btn) close(actions[Number(btn.dataset.choice)].value);
    });
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape') { document.removeEventListener('keydown', onKey); close(null); }
    });
  });
}

/* ------------------------------------------------------------------ settings */
async function openSettings(focus) {
  // `#btn-settings` is wired straight to this function, so a click event lands
  // in the first argument; only a selector string may be used below.
  if (typeof focus !== 'string') focus = '';
  // The Finder / association state lives outside the process, so it is read
  // fresh every time the sheet opens instead of being cached from /meta.
  await loadIntegration();
  const saved = (await api('/settings')).savedPasswords || { count: 0, files: [] };
  const s = state.settings;
  const meta = state.meta || {};
  const integ = state.integration || { types: [], services: [], ours: 0, total: 0 };
  const mainTypes = (integ.types || []).filter((t) => t.headline);
  const svcAuto = s.serviceAutoStart !== false;
  // One line replaces the old "引擎" + "版本" pair: the same three versions
  // used to be printed twice, in two sections, one scroll apart.
  const comps = (meta.components || []).filter((c) => c.id !== '7zz');
  // Component versions live on their own rows below, so the engine line stays
  // short instead of repeating them.
  const versionLine = [
    `${meta.engine?.engine || '7-Zip'} ${meta.engine?.engineVersion || '?'}`,
    meta.engine?.rarEncode ? 'RAR 可创建' : 'RAR 只读',
  ].join(' · ');
  const back = document.createElement('div');
  back.className = 'sheet-back';
  back.innerHTML = `
    <div class="sheet" role="dialog" aria-modal="true" aria-label="设置">
      <div class="sheet-head">
        <h2>设置</h2><span class="spacer"></span>
        <button class="icon-btn" data-close>${icon('i-x')}</button>
      </div>
      <div class="sheet-body">
      <div class="sheet-section">
        <h3>常规</h3>
        <div class="sheet-row">
          <label>主题<small>跟随系统或强制指定</small></label>
          <div class="seg" id="set-theme"></div>
        </div>
        <div class="sheet-row">
          <label>压缩格式</label>
          <select class="select" id="set-format" style="width:110px">
            ${(meta.formats || []).map((f) => `<option value="${f.id}" ${f.create ? '' : 'disabled'}>${f.label}</option>`).join('')}
          </select>
        </div>
        <div class="sheet-row">
          <label>压缩级别</label>
          <select class="select" id="set-level" style="width:110px">
            ${LEVEL_NAMES.map((n, i) => `<option value="${i}">${n}</option>`).join('')}
          </select>
        </div>
        <details class="sheet-fold">
          <summary>${icon('i-chevron')}<span>更多压缩选项</span><span class="fold-hint">提示音 · 系统文件 · WinRAR</span></summary>
          <div class="sheet-row">
            <label>完成提示音<small>压缩或解压完成后播放系统提示音</small></label>
            <button class="toggle" id="set-sound" role="switch" aria-checked="${s.suppressSound ? 'false' : 'true'}"></button>
          </div>
          <div class="sheet-row">
            <label>跳过系统文件<small>.DS_Store、__MACOSX、._* 等</small></label>
            <button class="toggle" id="set-meta" role="switch" aria-checked="${s.excludeMeta ? 'true' : 'false'}"></button>
          </div>
          <div class="sheet-row">
            <label>WinRAR 命令行<small>用于创建 .rar 归档（可选）</small></label>
            <button class="btn ghost" id="set-rar">${meta.engine?.rarEncode ? '更换…' : '选择…'}</button>
          </div>
        </details>
      </div>
      <div class="sheet-section" id="set-default">
        <h3>系统集成</h3>
        <div class="sheet-row">
          <label>解压 / 浏览压缩包<small>双击压缩包用哪个 App 打开</small></label>
          <span class="chip ${integ.ours ? 'ok' : ''}" id="set-def-chip">${integ.ours || 0}/${integ.total || 0} 项为 Arca</span>
          <div class="row-actions">
            <button class="btn primary" id="set-def-on" ${integ.canSetDefault ? '' : 'disabled'}>设为默认</button>
            <button class="btn quiet" id="set-def-off" ${integ.ours ? '' : 'disabled'}>恢复默认</button>
          </div>
        </div>
        <div class="sheet-row" id="set-finder">
          <label>用 Arca 压缩 / 解压<small>选中项目（可多选）→ 右键 → 快速操作 / 服务子菜单</small></label>
          <div class="row-actions">
            <span class="chip ${integ.quickActions ? 'ok' : ''}">${integ.quickActions ? '已安装' : '未安装'}</span>
            <button class="btn primary" id="set-qa-install">${integ.quickActions ? '重新安装' : '安装快速操作'}</button>
          </div>
        </div>
        <details class="sheet-fold" ${(integ.ours || 0) < (integ.total || 0) ? 'open' : ''} id="set-def-detail">
          <summary>${icon('i-chevron')}<span>逐项打开方式</span><span class="fold-hint">ZIP · 7Z · RAR · TAR · ZST</span></summary>
          ${mainTypes.map((t) => `
            <div class="sheet-row" data-def="${esc(t.ext)}">
              <label>${esc(t.label)}<small>${esc(t.uti || '未注册')}</small></label>
              <span class="chip ${t.ours ? 'ok' : ''}">${esc(t.handlerName || '系统默认')}</span>
            </div>`).join('')}
        </details>
        <details class="sheet-fold">
          <summary>${icon('i-chevron')}<span>右键菜单细节</span><span class="fold-hint">直接压缩 · 格式 · 入口</span></summary>
          <div class="sheet-row">
            <label>右键后直接开始压缩<small>默认关闭：文件进压缩页，选好格式与选项再按「开始压缩」</small></label>
            <button class="toggle" id="set-svc-auto" role="switch" aria-checked="${svcAuto ? 'true' : 'false'}"></button>
          </div>
          <div class="sheet-row">
            <label>右键压缩的格式<small>只影响 Finder 右键，不改上面的默认格式</small></label>
            <select class="select" id="set-svc-format" style="width:150px">
              ${(integ.creatable || [{ id: '', label: '跟随默认格式' }]).map((f) =>
                `<option value="${esc(f.id)}">${esc(f.label)}</option>`).join('')}
            </select>
          </div>
          <div class="sheet-row">
            <label>右键菜单看不到？<small>入口在「快速操作」或最底部「服务」子菜单；首次使用需在快捷键设置里勾选</small></label>
            <button class="btn ghost" id="set-svc-prefs">打开快捷键设置</button>
          </div>
        </details>
      </div>
      <div class="sheet-section">
        <h3>更新与密码</h3>
        <div class="sheet-row">
          <label>应用更新<small id="set-app-version">${s.appUpdateRepo ? 'Arca ' + (meta.version || '') : '未配置仓库，功能关闭'}</small></label>
          <div class="row-actions">
            <button class="btn ghost" id="set-app-check" ${s.appUpdateRepo ? '' : 'disabled'}>检查更新…</button>
            <button class="toggle" id="set-app-autocheck" role="switch" aria-label="启动时自动检查应用更新" aria-checked="${s.checkAppUpdates === false ? 'false' : 'true'}" title="启动时自动检查应用更新"></button>
          </div>
        </div>
        <div class="sheet-row">
          <label>内置引擎<small>${esc(versionLine)}${meta.engine?.engineSource === 'user' ? ' · 已使用应用外安装版本' : ''}</small></label>
          <div class="row-actions">
            <button class="btn ghost" id="set-updnow">检查更新…</button>
            <button class="toggle" id="set-updcheck" role="switch" aria-checked="${s.checkEngineUpdates === false ? 'false' : 'true'}" title="启动时自动检查 7-Zip 更新"></button>
          </div>
        </div>
        <div class="sheet-row">
          <label id="set-saved-count">加密包密码<small>已记住 ${saved.count} 个，仅存本机钥匙串</small></label>
          <div class="row-actions">
            <button class="toggle" id="set-password-auth" role="switch" aria-label="读取记住的密码前要求触控 ID 或登录密码" aria-checked="${s.passwordAuth === 'touchid' ? 'true' : 'false'}" title="读取记住的密码前要求触控 ID 或登录密码"></button>
            <button class="btn quiet" id="set-clear-passwords" ${saved.count ? '' : 'disabled'}>全部清除</button>
          </div>
        </div>
        <details class="sheet-fold">
          <summary>${icon('i-chevron')}<span>更新来源与隐私</span><span class="fold-hint">匿名统计 · GitHub · 组件</span></summary>
          <div class="sheet-row">
            <label>匿名使用统计<small>安装与每日首次启动各发一条记录：随机标识、版本与系统信息；不涉及文件内容与密码</small></label>
            <button class="toggle" id="set-stats" role="switch" aria-label="匿名使用统计" aria-checked="${s.anonymousStats === false ? 'false' : 'true'}"></button>
          </div>
          <details class="sheet-fold sn-fold">
            <summary>${icon('i-chevron')}<span>查看发送内容</span><span class="fold-hint">字段示例</span></summary>
            <div class="sheet-row">
              <label>每条记录包含<small>事件类型（安装或每日活跃）· 日期 · App 版本 · macOS 大版本 · 芯片架构 · 随机安装标识（形如 a3f9…c2，首次启动时在本机生成）</small></label>
            </div>
            <div class="sheet-row" style="border-bottom:0;padding-bottom:0">
              <label>不会发送<small>文件名与路径 · 归档内容 · 密码 · 压缩任务参数</small></label>
            </div>
          </details>
          <div class="sheet-row">
            <label>更新来源<small>owner/repo，留空关闭应用更新</small></label>
            <input class="input" id="set-app-repo" aria-label="应用更新 GitHub 仓库" value="${esc(s.appUpdateRepo || '')}" placeholder="owner/repo" style="width:180px">
            <button class="btn ghost" id="set-app-save">保存</button>
          </div>
          ${meta.engine?.engineSource === 'user' ? `
          <div class="sheet-row">
            <label>引擎来源<small>已使用应用外安装的版本</small></label>
            <button class="btn quiet" id="set-updreset">恢复内置</button>
          </div>` : ''}
          ${(meta.components || []).filter((c) => c.id !== '7zz').map((c) => `
          <div class="sheet-row" data-comp="${esc(c.id)}">
            <label>${esc(c.label)}<small>${esc(c.kind)} · ${c.version ? 'v' + esc(c.version) : '缺失'} · ${compSourceText(c.source)}</small></label>
            <div class="row-actions">
              <button class="btn ghost" data-comp-check="${esc(c.id)}">检查更新…</button>
              ${c.source === 'user' ? `<button class="btn quiet" data-comp-reset="${esc(c.id)}">恢复内置</button>` : ''}
            </div>
          </div>`).join('') || '<div class="sheet-row"><label>没有可用的组件</label></div>'}
        </details>
      </div>

      <div class="sheet-foot">
        <span class="foot-version">v${esc(meta.version || '1.0.0')}</span>
        <span class="spacer"></span>
        <button class="btn primary" data-close>完成</button>
      </div>
    </div>`;
  document.body.appendChild(back);
  reportDragRegion();
  const restoreModal = modalize(back);
  if (focus) {
    const target = back.querySelector(focus);
    if (target) {
      // A row inside a collapsed <details> cannot be scrolled to: open the
      // group first, then reveal it. Highlighting matters as much as scrolling
      // -- otherwise the jump lands on a sheet that looks unchanged.
      const fold = target.closest('details.sheet-fold');
      if (fold) fold.open = true;
      target.scrollIntoView({ block: 'center' });
      // Highlight the whole row: a lone outline on a small button is easy to
      // miss when the sheet is busy.
      const row = target.closest('.sheet-row') || target;
      row.classList.add('is-target');
      setTimeout(() => row.classList.remove('is-target'), 1800);
      if (target.matches('button, select, input, textarea')) {
        target.focus({ preventScroll: true });
      }
    }
  }

  const close = () => {
    document.removeEventListener('keydown', onKey);
    restoreModal();
    back.remove();
    reportDragRegion();
  };
  back.addEventListener('click', (e) => { if (e.target === back || e.target.closest('[data-close]')) close(); });
  back._close = close;   // 让内部动作（改默认打开方式）能干净地关掉再重开
  function onKey(e) {
    if (e.key === 'Escape') close();
  }
  document.addEventListener('keydown', onKey);

  buildSeg($('#set-theme', back), [
    { id: 'system', label: '自动' }, { id: 'light', label: '浅色' }, { id: 'dark', label: '深色' },
  ], s.appearance || 'system', (id) => {
    setAppearance(id);
  });
  $('#set-clear-passwords', back).onclick = async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await api('/settings', { clearSavedPasswords: true });
      $('#set-saved-count', back).textContent = '已记住 0 个加密包的密码';
      if (state.x.info) state.x.info.savedPasswordUsed = false;
      renderExtract();
      toast('已清除记住的密码', 'ok');
    } catch (err) {
      btn.disabled = false;
      toast(err.message, 'err');
    }
  };
  $('#set-format', back).value = s.format || '7z';
  $('#set-level', back).value = String(s.level ?? 3);
  $('#set-format', back).onchange = (e) => patchSettings({ format: e.target.value });
  $('#set-level', back).onchange = (e) => patchSettings({ level: Number(e.target.value) });
  $('#set-password-auth', back).onclick = async (e) => {
    const btn = e.currentTarget;
    const next = btn.getAttribute('aria-checked') !== 'true';
    btn.disabled = true;
    try {
      await patchSettings({ passwordAuth: next ? 'touchid' : 'off' });
      btn.setAttribute('aria-checked', String(state.settings.passwordAuth === 'touchid'));
    } finally {
      btn.disabled = false;
    }
  };
  $('#set-meta', back).onclick = (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    patchSettings({ excludeMeta: next });
  };
  $('#set-sound', back).onclick = (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    patchSettings({ suppressSound: !next });
  };
  $('#set-stats', back).onclick = (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    patchSettings({ anonymousStats: next });
  };
  // ---------------------------------------------------- Finder 集成
  const svcFormat = $('#set-svc-format', back);
  if (svcFormat) {
    svcFormat.value = s.serviceFormat || '';
    svcFormat.onchange = (e) => patchSettings({ serviceFormat: e.target.value });
  }
  const svcAutoBtn = $('#set-svc-auto', back);
  if (svcAutoBtn) svcAutoBtn.onclick = (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    patchSettings({ serviceAutoStart: next });
  };
  const svcPrefs = $('#set-svc-prefs', back);
  if (svcPrefs) svcPrefs.onclick = async () => {
    try {
      await api('/integration/services', {});
      toast('在「键盘快捷键」列表里勾选「用 Arca 压缩 / 解压」两项', 'info', 'i-gear');
    } catch (err) {
      toast(err.message, 'err');
    }
  };
  const qaInstall = $('#set-qa-install', back);
  if (qaInstall) qaInstall.onclick = async () => {
    qaInstall.disabled = true;
    const old = qaInstall.textContent;
    qaInstall.textContent = '正在安装…';
    try {
      const r = await api('/integration/quickactions', {});
      if (r.ok) {
        toast('已写入「快速操作」· 回 Finder 选中文件 → 右键 → 快速操作', 'ok', 'i-download');
        if (state.integration) state.integration.quickActions = true;
      } else {
        toast('安装失败：' + (r.failed || []).join(' '), 'err');
      }
    } catch (err) {
      toast(err.message, 'err');
    }
    qaInstall.disabled = false;
    qaInstall.textContent = old;
  };
  const defOn = $('#set-def-on', back);
  if (defOn) defOn.onclick = () => setDefaultHandler(true, back);
  const defOff = $('#set-def-off', back);
  if (defOff) defOff.onclick = () => setDefaultHandler(false, back);

  back.querySelectorAll('[data-comp-check]').forEach((btn) => {
    btn.onclick = () => openComponentUpdate(btn.dataset.compCheck, { refresh: true });
  });
  back.querySelectorAll('[data-comp-reset]').forEach((btn) => {
    btn.onclick = async () => {
      await resetComponent(btn.dataset.compReset);
      close();
      openSettings();
    };
  });
  $('#set-updcheck', back).onclick = (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    patchSettings({ checkEngineUpdates: next });
  };
  const appAuto = $('#set-app-autocheck', back);
  if (appAuto) appAuto.onclick = (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    patchSettings({ checkAppUpdates: next });
  };
  $('#set-updnow', back).onclick = async () => {
    close();
    const info = await checkEngineUpdate({ refresh: true, verbose: true });
    if (!info) return;
    if (!info.ok) { toast(info.error || '检查更新失败', 'err'); return; }
    if (info.available) openEngineUpdate(info);
    else toast(`已是最新版本 7-Zip ${info.latest || info.current}`, 'ok');
  };
  async function loadAppUpdate(refresh = false) {
    const info = (await api('/appupdate' + (refresh ? '?refresh=1' : ''))).update;
    const here = info.current || (state.meta && state.meta.version) || '?';
    $('#set-app-version', back).textContent = info.recovery || (info.enabled
      ? `Arca ${here}${info.available ? ' · ' + (info.skipped ? '已跳过 ' : '新版本 ') + info.latest : ''}` : `Arca ${here} · 应用更新已关闭`);
    const button = $('#set-app-check', back);
    button.disabled = !info.enabled;
    button.textContent = info.available ? `新版本 ${info.latest} · 下载并更新` : '检查应用更新…';
    return info;
  }
  // The repo row is visible now (no fold), so the version line loads on open.
  if (s.appUpdateRepo) loadAppUpdate().catch((err) => toast(err.message, 'err'));
  $('#set-app-save', back).onclick = async () => {
    try {
      await patchSettings({ appUpdateRepo: $('#set-app-repo', back).value.trim() });
      await loadAppUpdate();
    } catch (err) { toast(err.message, 'err'); }
  };
  $('#set-app-check', back).onclick = async () => {
    try {
      let info = await loadAppUpdate();
      const resuming = info.install && info.install.phase !== 'error';
      if (!resuming) info = await loadAppUpdate(true);
      if (!resuming && !info.ok) throw new Error(info.error || '检查应用更新失败');
      if (!info.available && !info.install) { toast('应用已是最新版本', 'ok'); return; }
      close();
      openUpdateSheet(Object.assign({}, info, {
        page: `https://github.com/${info.repo}/releases`,
        highlights: info.highlights ? [{ tag: 'official', label: '应用', text: info.highlights }] : [],
      }), {
        id: 'app', title: '应用更新', chip: 'Arca', poll: '/appupdate', apply: '/appupdate/apply',
        skip: true, dismiss: '/appupdate/dismiss', restart: true,
        doneText: () => '更新已下载并安装，重启后生效',
        toastText: () => '应用更新已就绪，点击「立即重启」生效',
        note: '重启会关闭当前窗口并打开新版本。新版本成功启动前保留旧应用以便恢复。',
      });
    } catch (err) { toast(err.message, 'err'); }
  };
  const reset = $('#set-updreset', back);
  if (reset) reset.onclick = async () => {
    try {
      const r = await api('/engine/reset', {});
      if (state.meta) state.meta.engine = r.engine;
      state.update = null;
      renderEngineBadge();
      close();
      toast(`已恢复内置引擎 ${r.engine.engineVersion}`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  };
  $('#set-rar', back).onclick = async () => {
    const paths = await pick('files', { multiple: false, title: '选择 rar 命令行工具（通常位于 WinRAR 安装目录）' });
    if (paths.length) {
      await patchSettings({ rarPath: paths[0] });
      const meta2 = await api('/meta');
      state.meta = meta2;
      close();
      renderCompress();   // the RAR tab flips to enabled, the setup link goes away
      toast('已设置 RAR 命令行', 'ok');
    }
  };
}

/** First-launch stats notice: equal-weight allow/deny; nothing is sent until
 *  a choice is made (see stats.maybe_ping's statsPrompted guard). */
function renderStatsNotice() {
  if (state.settings.statsPrompted !== false) return;
  if (document.getElementById('stats-notice')) return;
  const topbar = document.querySelector('.topbar');
  if (!topbar) return;
  const bar = document.createElement('div');
  bar.className = 'stats-notice';
  bar.id = 'stats-notice';
  bar.setAttribute('role', 'note');
  bar.setAttribute('aria-label', '匿名使用统计说明');
  bar.innerHTML = `
    <div class="sn-body">
      <b>匿名使用统计</b>
      <span>为估算安装量与活跃设备，Arca 会在首次启动与每日首次启动时各发送一条记录：本机保存的随机标识、App 版本、系统大版本与架构。不收集文件名、路径、压缩内容或密码。之后可随时在「设置 → 更新来源与隐私」中修改。</span>
    </div>
    <div class="sn-actions">
      <button class="btn ghost" id="sn-allow">允许</button>
      <button class="btn ghost" id="sn-deny">不允许</button>
    </div>`;
  topbar.after(bar);
  const settle = async (allow) => {
    for (const b of bar.querySelectorAll('button')) b.disabled = true;
    await patchSettings({ statsPrompted: true, anonymousStats: allow });
    bar.remove();
  };
  bar.querySelector('#sn-allow').onclick = () => settle(true);
  bar.querySelector('#sn-deny').onclick = () => settle(false);
}

async function patchSettings(patch) {
  try {
    const r = await api('/settings', { patch });
    state.settings = r.settings;
  } catch (err) {
    toast(err.message, 'err');
  }
}

/** Hand every archive type to Arca (or back to the system) and redraw. */
async function setDefaultHandler(on, back) {
  const btn = on ? $('#set-def-on', back) : $('#set-def-off', back);
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '正在设置…'; }
  try {
    const r = await api('/integration/default', { mode: on ? 'on' : 'reset' });
    if (state.integration) {
      state.integration.types = r.types || state.integration.types;
      state.integration.ours = r.ours || 0;
    }
    const failed = (r.failed || []).map((f) => f.ext.toUpperCase());
    if (failed.length) {
      toast(`部分类型没能切换：${failed.join(' ')} · 请把 Arca 放进「应用程序」再试`, 'err');
    } else {
      toast(on ? `已设为默认解压软件 · ${r.applied} 项` : '已还原系统默认打开方式', 'ok');
    }
    if (back && back._close) back._close(); else if (back) back.remove();
    openSettings('#set-default');
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    toast(err.message, 'err');
  }
}

/* -------------------------------------------------------- component updates */
function compSourceText(source) {
  return { user: '已更新', bundled: '应用内置', path: '系统路径', none: '缺失' }[source] || '未知';
}

async function checkComponent(id, opts = {}) {
  try {
    const r = await api('/components/check?id=' + encodeURIComponent(id)
      + (opts.refresh ? '&refresh=1' : ''));
    if (r.components && state.meta) state.meta.components = r.components;
    return r.update;
  } catch (err) {
    if (opts.verbose) toast(err.message, 'err');
    return { ok: false, error: err.message };
  }
}

/** Same dialog as the engine updater: a component is only ever built or
 *  downloaded after this click. */
async function openComponentUpdate(id, opts = {}) {
  const info = await checkComponent(id, { refresh: opts.refresh, verbose: true });
  if (!info) return;
  if (!info.ok) { toast(info.error || '检查更新失败', 'err'); return; }
  if (!info.available) {
    toast(`${info.label} ${info.currentVersion || ''} 已是最新`, 'ok');
    return;
  }
  openUpdateSheet(info, {
    id,
    title: `${info.label} 更新`,
    chip: info.label,
    poll: '/components/check?id=' + encodeURIComponent(id),
    apply: '/components/apply',
    skip: false,
    snooze: false,
    goLabel: '编译并安装',
    doneText: (v) => `已安装 ${v}，之后的压缩任务会直接使用`,
    toastText: (v) => `${info.label} 已更新到 ${v}`,
    note: `组件会编译到 ~/Library/Application Support/Arca/components/，不会修改应用本身；
           上游只发布源码，所以更新需要本机命令行工具（Xcode Command Line Tools），编译失败会保留原版本。`,
    afterDone: async () => {
      try {
        const m = await api('/meta');
        state.meta = m;
        renderEngineBadge();
        renderCompress();
      } catch (_) { /* the dialog already showed the result */ }
    },
  });
}

async function resetComponent(id) {
  try {
    const r = await api('/components/reset', { id });
    if (r.components && state.meta) state.meta.components = r.components;
    if (r.engine && state.meta) { state.meta.engine = r.engine; renderEngineBadge(); }
    toast('已恢复内置版本', 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* ------------------------------------------------------------ engine updates */
function renderEngineBadge() {
  const info = (state.meta && state.meta.engine) || {};
  const ver = info.engineVersion || '?';
  const tag = info.engineSource === 'user' ? '已更新' : '内置';
  const el = $('#engine-ver');
  if (el) el.textContent = `v${ver} · ${tag}`;
  const card = $('#engine-card');
  if (!card) return;
  const stale = state.update && state.update.ok && state.update.available;
  card.classList.toggle('has-update', !!stale);
  const parts = [`7-Zip ${ver}（${tag}）`];
  if (info.rarEncode) parts.push('RAR 可创建');
  else parts.push('RAR 只读；创建 RAR 需安装 WinRAR 命令行');
  if (stale) parts.push(`有新版本 ${state.update.latest}，右键或设置里可更新`);
  card.title = parts.join('\n');
}

/** Ask the local service (which talks to 7-zip.org) whether a newer 7-Zip
 *  exists.  Never installs: that is the user's click in the dialog. */
async function checkEngineUpdate(opts = {}) {
  try {
    const r = await api('/engine/update' + (opts.refresh ? '?refresh=1' : ''));
    state.update = r.update;
    if (state.meta) state.meta.engine = r.engine;
    renderEngineBadge();
    return r.update;
  } catch (err) {
    if (opts.verbose) toast(err.message, 'err');
    return { ok: false, error: err.message };
  }
}

function updateNoteLine(note) {
  return `<li><span class="upd-tag ${esc(note.tag)}">${esc(note.label)}</span>
    <span>${esc(note.text)}</span></li>`;
}

function openEngineUpdate(info, opts) {
  return openUpdateSheet(info, Object.assign({
    id: '7zz',
    title: '引擎更新',
    chip: '7-Zip',
    poll: '/engine/update',
    apply: '/engine/update/apply',
    skip: true,
    snooze: true,
    doneText: (v) => `已更新到 ${v}，下一次压缩就会用新引擎`,
    toastText: (v) => `7-Zip 引擎已更新到 ${v}`,
    note: `引擎会下载到 ~/Library/Application Support/Arca/engines/，不会修改应用本身；
          更新后立即生效，随时可以在设置里恢复内置版本。「稍后」24 小时后再提醒。`,
  }, opts || {}));
}

/** Shared dialog for application, engine and component updates. */
/** Jobs write files; swapping the bundle or an engine under them leaves a
 *  half-written archive. The server refuses as well — this is the fast path. */
function jobsBusy() {
  return (state.jobs || []).some((j) => j.status === 'running' || j.status === 'queued');
}

function openUpdateSheet(info, opts) {
  if (!info) return;
  const cur = info.currentVersion || (typeof info.current === 'string' ? info.current : '') || '?';
  const latest = info.latest || '?';
  const rows = info.versions || [];
  const jump = rows.length;
  const sec = info.securityFixes || 0;
  const metaBits = [];
  if (info.released) metaBits.push(`发布于 ${info.released}`);
  metaBits.push(jump > 1 ? `跨越 ${jump} 个版本` : `较 ${cur} 更新`);
  if (sec) metaBits.push(`官方记录了 ${sec} 项安全修复`);
  if (info.license) metaBits.push(info.license);

  const highlights = (info.highlights || []).length
    ? `<ul class="upd-hl">${info.highlights.map(updateNoteLine).join('')}</ul>`
    : '';

  const detail = rows.map((row) => {
    let body;
    if (row.notes && row.notes.length) {
      const hasOfficial = row.notes.some((n) => n.tag === 'official');
      body = `<ul class="upd-notes${hasOfficial ? ' en' : ''}">${row.notes.map(updateNoteLine).join('')}</ul>`
        + (hasOfficial ? '<p class="upd-hint">该版本暂无中文摘要，以上为官方原文。</p>' : '');
    } else if (row.official && row.official.length) {
      body = `<ul class="upd-notes en">${row.official.slice(0, 6)
        .map((t) => `<li><span>${esc(t)}</span></li>`).join('')}</ul>
        <p class="upd-hint">该版本暂无中文摘要，以上为官方原文。</p>`;
    } else {
      body = '<p class="upd-hint">官方未列出具体条目。</p>';
    }
    return `<div class="upd-row">
      <div class="upd-row-head"><b>${esc(row.version)}</b>
        <span>${esc(row.date || '')}</span></div>${body}</div>`;
  }).join('');

  const back = document.createElement('div');
  back.className = 'sheet-back';
  back.innerHTML = `
    <div class="sheet upd" role="dialog" aria-modal="true" aria-label="${esc(opts.title)}">
      <div class="sheet-head">
        <h2>${esc(opts.title)}</h2><span class="spacer"></span>
        <span class="chip accent">${esc(opts.chip)}</span>
        <button class="icon-btn" data-close title="${opts.snooze ? '稍后' : '关闭'}">${icon('i-x')}</button>
      </div>
      <div class="sheet-body">
        <div class="upd-hero">
          <div class="upd-vers"><b>${esc(cur)}</b>
            <span class="upd-arrow">${icon('i-chevron')}</span>
            <b class="upd-new">${esc(latest)}</b></div>
          <div class="upd-meta">${esc(metaBits.join(' · '))}</div>
        </div>
        <div class="upd-progress hidden" id="upd-prog">
          <div class="bar indet"><i></i></div>
          <div class="upd-prog-row"><span id="upd-msg">准备中…</span><b id="upd-pct"></b></div>
        </div>
        ${highlights ? `<div class="upd-section"><h3>这次更新带来什么</h3>${highlights}</div>` : ''}
        <div class="upd-section">
          <h3>逐版本变更</h3>
          <div class="upd-list">${detail || '<p class="upd-hint">没有可显示的条目。</p>'}</div>
        </div>
        <p class="upd-hint">
          ${opts.note || ''}
          <a href="#" id="upd-page">查看官方页面</a>
        </p>
      </div>
      <div class="sheet-foot">
        <button class="btn quiet ${opts.skip ? '' : 'hidden'}" id="upd-skip">跳过此版本</button>
        <span style="flex:1"></span>
        <button class="btn ghost" id="upd-later" data-close>${opts.snooze ? '稍后' : '取消'}</button>
        <button class="btn primary" id="upd-go">${icon('i-download')}${opts.goLabel || '下载并更新'}</button>
      </div>
    </div>`;
  document.body.appendChild(back);
  reportDragRegion();
  const restoreModal = modalize(back);

  let stopped = false;
  let settled = false;      // installed or explicitly skipped
  let doneSeen = false;     // the sheet itself watched this install reach done
  let busy = false;
  const close = () => {
    stopped = true;
    clearTimeout(state.updateTimer);
    document.removeEventListener('keydown', onKey);
    restoreModal();
    back.remove();
    reportDragRegion();
    // "稍后" is not a decision: hold the prompt for a day so it does not
    // reappear every single launch.
    if (!settled && opts.snooze && info.latest) {
      api('/engine/update/snooze', { version: info.latest, hours: 24 }).catch(() => {});
    }
  };
  function onKey(e) {
    if (e.key === 'Escape' && !busy) close();
  }
  document.addEventListener('keydown', onKey);
  back.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]') || e.target === back) { close(); return; }
    if (e.target.closest('#upd-page')) {
      e.preventDefault();
      native('openUrl', { url: info.page || (state.update && state.update.page) || 'https://www.7-zip.org/download.html' });
    }
  });

  const prog = $('#upd-prog', back);
  const bar = $('.bar', back);
  const msg = $('#upd-msg', back);
  const pct = $('#upd-pct', back);
  const go = $('#upd-go', back);

  const paint = (st) => {
    if (!st) return;
    const phase = st.phase || '';
    if (['download', 'unpack', 'build', 'verify', 'install'].includes(phase)) {
      const first = prog.classList.contains('hidden');
      prog.classList.remove('hidden');
      // The strip sits under the summary now, but the sheet may already be
      // scrolled into the changelog: bring it back into view.
      if (first) prog.scrollIntoView({ block: 'nearest' });
      const skip = $('#upd-skip', back);
      if (skip) skip.disabled = true;
      const known = st.percent > 0 && phase === 'download';
      bar.classList.toggle('indet', !known);
      if (known) bar.firstElementChild.style.width = `${Math.max(2, st.percent)}%`;
      msg.textContent = st.message || (phase === 'build' ? '正在编译…' : '正在更新…');
      pct.textContent = known ? `${st.percent}%` : '';
      go.disabled = true;
      go.innerHTML = `${icon('i-download')}更新中…`;
      busy = true;
    } else if (phase === 'done') {
      prog.classList.remove('hidden');
      bar.classList.remove('indet');
      bar.firstElementChild.style.width = '100%';
      bar.classList.add('ok');
      msg.textContent = opts.doneText(st.version || '');
      pct.textContent = '100%';
      go.disabled = false;
      go.innerHTML = opts.restart
        ? `${icon('i-refresh')}立即重启`
        : `${icon('i-check')}完成`;
      if (!opts.restart) go.classList.remove('primary');
      busy = false;
      doneSeen = true;   // observed locally: global state may not know this install
      $('#upd-skip', back).classList.add('hidden');
      // Nothing left to postpone and nothing left to decide: one primary
      // button ("完成") closes the sheet, the header X still works.
      const later = $('#upd-later', back);
      if (later) later.classList.add('hidden');
      settled = true;
      toast(opts.toastText(st.version || ''), 'ok');
      if (opts.afterDone) opts.afterDone();
    } else if (phase === 'error') {
      prog.classList.remove('hidden');
      bar.classList.remove('indet');
      bar.firstElementChild.style.width = '100%';
      bar.classList.add('err');
      msg.textContent = st.error || '更新失败';
      pct.textContent = '';
      go.disabled = false;
      go.innerHTML = `${icon('i-download')}重试`;
      busy = false;
    }
  };

  async function poll() {
    if (stopped) return;
    let r;
    try {
      r = await api(opts.poll);
    } catch (_) {
      state.updateTimer = setTimeout(poll, 1200);
      return;
    }
    const inst = r.update && r.update.install;
    if (r.engine && state.meta) { state.meta.engine = r.engine; renderEngineBadge(); }
    if (r.components && state.meta) { state.meta.components = r.components; }
    paint(inst);
    if (inst && (inst.phase === 'done' || inst.phase === 'error')) {
      if (inst.phase === 'done') {
        if (opts.id === '7zz') { state.update = r.update; renderEngineBadge(); }
        if (opts.afterDone) opts.afterDone();
      }
      return;
    }
    state.updateTimer = setTimeout(poll, 400);
  }

  go.onclick = async () => {
    if (busy) return;
    if (jobsBusy()) { toast('有任务正在进行，完成后再更新', 'err'); return; }
    // The app sheet's install state is only visible to this sheet's own poll
    // loop; state.componentInstall never carries it, so the old lookup always
    // returned {} and the restart branch never fired.
    const inst = opts.id === '7zz'
      ? ((state.update && state.update.install) || {})
      : ((state.componentInstall && state.componentInstall[opts.id]) || {});
    const done = doneSeen || inst.phase === 'done';
    if (settled || done) {
      if (opts.restart && done) {
        go.disabled = true;
        go.innerHTML = `${icon('i-refresh')}正在重启…`;
        try {
          await api('/appupdate/restart', {});
          return;            // the process exits on its own
        } catch (err) {
          go.disabled = false;
          go.innerHTML = `${icon('i-refresh')}立即重启`;
          toast(err.message, 'err');
        }
        return;
      }
      close();
      return;
    }
    go.disabled = true;
    try {
      const r = await api(opts.apply, { version: latest, id: opts.id });
      paint(r.install);
      busy = true;
      poll();
    } catch (err) {
      go.disabled = false;
      toast(err.message, 'err');
    }
  };

  const skipBtn = $('#upd-skip', back);
  if (skipBtn && opts.skip) {
    skipBtn.onclick = async () => {
      try {
        if (opts.dismiss) await api(opts.dismiss, { version: latest });
        else await patchSettings({ engineSkipVersion: latest });
        settled = true;
        if (!opts.dismiss && state.update) state.update.skipped = true;
        close();
        toast(opts.id === 'app' ? `已跳过应用版本 ${latest}，可手动重新检查`
          : `已跳过 ${latest}，下次启动不再提示`, 'ok');
      } catch (err) { toast(err.message, 'err'); }
    };
  }

  prog.classList.add('hidden');
  if (opts.id === 'app' && info.install) {
    paint(info.install);
    if (!['done', 'error'].includes(info.install.phase)) poll();
  }
}

/** Launch-time check: quiet about failures, loud only when there is news. */
async function maybeCheckEngineUpdate() {
  if (state.settings.checkEngineUpdates === false) return;
  if (state.update && state.update.available && !state.update.skipped
      && !state.update.install) return openEngineUpdate(state.update);
  const info = await checkEngineUpdate();
  if (!info || !info.ok || !info.available || info.skipped || info.snoozed) return;
  if (info.install && ['download', 'unpack', 'verify', 'install'].includes(info.install.phase)) {
    return openEngineUpdate(info);      // finish showing an interrupted update
  }
  openEngineUpdate(info);
}

/** Same shape for the app itself; silent unless a newer release exists. */
async function maybeCheckAppUpdate() {
  if (state.settings.checkAppUpdates === false) return;
  if (!state.settings.appUpdateRepo) return;
  if ($('.sheet.upd')) return;          // the engine dialog already asked
  // One update prompt at a time: while the engine has news pending, the app
  // check waits for the next launch instead of stacking a second dialog.
  if (state.update && state.update.available && !state.update.skipped) return;
  let info;
  try {
    info = (await api('/appupdate')).update;
  } catch (_) {
    return;                             // offline or rate-limited: stay quiet
  }
  if (!info || !info.ok || info.skipped) return;
  const resuming = info.install && info.install.phase !== 'error';
  if (!info.available && !resuming) return;
  openUpdateSheet(Object.assign({}, info, {
    page: `https://github.com/${info.repo}/releases`,
    highlights: info.highlights ? [{ tag: 'official', label: '应用', text: info.highlights }] : [],
  }), {
    id: 'app', title: '应用更新', chip: 'Arca', poll: '/appupdate', apply: '/appupdate/apply',
    skip: true, dismiss: '/appupdate/dismiss', restart: true,
    doneText: () => '更新已下载并安装，重启后生效',
    toastText: () => '应用更新已就绪，点击「立即重启」生效',
    note: '重启会关闭当前窗口并打开新版本。新版本成功启动前保留旧应用以便恢复。',
  });
}

/* --------------------------------------------------------------------- wire */
function wireEvents() {
  $$('.nav-item[data-nav]').forEach((btn) => btn.addEventListener('click', () => setView(btn.dataset.nav)));
  const railRecent = $('#rail-recent');
  if (railRecent) {
    railRecent.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-recent]');
      if (!btn) return;
      setView('queue');
      const card = $(`.job[data-job="${btn.dataset.recent}"], .history-item[data-history="${btn.dataset.recent}"]`);
      if (card) {
        card.scrollIntoView({ block: 'center' });
        card.classList.add('is-target');
        setTimeout(() => card.classList.remove('is-target'), 1600);
      } else {
        const item = state.history.find((entry) => entry.id === btn.dataset.recent);
        const path = item && historyPath(item);
        if (path) native('reveal', { path });
      }
    });
  }
  $('#btn-settings').addEventListener('click', openSettings);

  // The credit is a real <a> (copyable, announced as a link) but the webview
  // must never follow it: a navigation here would replace the app with a web
  // page and there is no back button. Same route as the release-notes link.
  const railCredit = $('#rail-credit');
  if (railCredit) {
    railCredit.addEventListener('click', (e) => {
      e.preventDefault();
      native('openUrl', { url: railCredit.href });
    });
  }

  $('#btn-theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme === 'dark';
    setAppearance(dark ? 'light' : 'dark');
  });

  // Pill geometry follows layout, not paint: re-measure when the window or the
  // font metrics change, otherwise a resized window leaves every pill behind.
  window.addEventListener('resize', debounce(syncAllSegs, 60));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncAllSegs);

  // compress
  $('#c-pick').addEventListener('click', async (e) => { e.stopPropagation(); await addToCompress(await pick('files', { multiple: true, prompt: '添加' })); });
  $('#c-pick-dir').addEventListener('click', async (e) => { e.stopPropagation(); await addToCompress(await pick('folder', { multiple: true, prompt: '添加' })); });
  $('#c-add').addEventListener('click', async () => addToCompress(await pick('files', { multiple: true, prompt: '添加' })));
  $('#drop-c').addEventListener('click', async () => addToCompress(await pick('files', { multiple: true, prompt: '添加' })));
  $('#drop-c').addEventListener('keydown', async (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); await addToCompress(await pick('files', { multiple: true })); }
  });
  $('#c-clear').addEventListener('click', clearCompressList);
  $('#c-list').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-remove]');
    if (!btn) return;
    removeCompressItem(Number(btn.dataset.remove));
  });
  $('#c-level').addEventListener('input', (e) => {
    state.c.level = Number(e.target.value);
    $('#c-level-name').textContent = LEVEL_NAMES[state.c.level];
    $('#c-level-hint').textContent = LEVEL_HINTS[state.c.level];
    $('#c-outhint').textContent = buildOutHint();
  });
  $('#c-split').addEventListener('change', (e) => {
    state.c.splitId = e.target.value;
    if (state.c.splitId === 'custom') { $('#c-split-custom').classList.remove('hidden'); setTimeout(() => $('#c-split-custom').focus(), 30); }
    else $('#c-split-custom').classList.add('hidden');
    renderCompress();
  });
  $('#c-split-custom').addEventListener('input', (e) => {
    state.c.splitCustom = e.target.value;
    state.c.splitBytes = parseSizeInput(e.target.value);
    const total = cTotalSize();
    $('#c-split-hint').textContent = state.c.splitBytes
      ? (total ? `约 ${Math.ceil(total / state.c.splitBytes)} 个分卷` : '分卷大小 ' + fmtSize(state.c.splitBytes))
      : '格式示例：120m、1.5g';
    $('#c-outhint').textContent = buildOutHint();
  });
  $('#c-pw').addEventListener('input', (e) => {
    state.c.pw = e.target.value;
    refreshCompressPasswordHint();
    $('#c-names-row').classList.toggle('hidden', !(state.c.pw && state.c.format !== 'zip'));
    $('#c-outhint').textContent = buildOutHint();
  });
  $('#c-pw-eye').addEventListener('click', () => {
    const input = $('#c-pw');
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    $('#c-pw-eye').innerHTML = icon(show ? 'i-eye-off' : 'i-eye');
    // 图标换了，读屏名称与提示也要跟着换（§5.2）。
    $('#c-pw-eye').setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    $('#c-pw-eye').title = show ? '隐藏密码' : '显示密码';
  });
  $('#c-pw-gen').addEventListener('click', () => {
    const pw = genPassword();
    $('#c-pw').value = pw;
    $('#c-pw').type = 'text';
    state.c.pw = pw;
    $('#c-pw-eye').innerHTML = icon('i-eye-off');
    $('#c-pw-eye').setAttribute('aria-label', '隐藏密码');
    $('#c-pw-eye').title = '隐藏密码';
    refreshCompressPasswordHint();
    $('#c-names-row').classList.toggle('hidden', !(state.c.pw && state.c.format !== 'zip'));
    $('#c-outhint').textContent = buildOutHint();
  });
  $('#c-names').addEventListener('click', (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    state.c.encryptNames = next;
    $('#c-outhint').textContent = buildOutHint();
  });
  $('#c-exclude').addEventListener('click', (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    state.c.excludeMeta = next;
  });
  $('#c-solid').addEventListener('click', (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    state.c.solid = next;
  });
  $('#c-method').addEventListener('change', (e) => { state.c.method = e.target.value; });
  $('#c-par2').addEventListener('click', (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    state.c.par2 = next;
    // No toast here: the row's own status line already states the consequence
    // and stays readable. A toast is for results, not for explaining an option
    // -- it vanished after ~3s, far from the control, with nothing to re-read.
    renderCompress();
  });
  $('#c-par2-pct').addEventListener('change', (e) => {
    state.c.par2Pct = parseInt(e.target.value, 10) || 5;
    renderCompress();
  });
  const singleRow = $('#c-par2-single-row');
  if (singleRow) {
    // The advice used to live on hover, which keyboard and touch users can never
    // reach, and which reflowed the card by a line every time the pointer
    // crossed the row. It is now a disclosure: same text, opened on purpose,
    // focusable, and it does not touch the status line.
    const help = $('#c-par2-single-help');
    if (help) {
      help.addEventListener('click', () => {
        const open = help.getAttribute('aria-expanded') === 'true';
        help.setAttribute('aria-expanded', String(!open));
        $('#c-par2-single-note').classList.toggle('hidden', open);
      });
    }
    $('#c-par2-single').addEventListener('click', (e) => {
      const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
      e.currentTarget.setAttribute('aria-checked', String(next));
      state.c.par2Single = next;
      renderCompress();
    });
  }
  $('#c-dest').addEventListener('click', async () => {
    const paths = await pick('dest', { title: '选择保存位置', dir: state.c.dest || (state.c.items[0] ? dirName(state.c.items[0].path) : homeDir) });
    if (paths.length) { state.c.dest = paths[0]; renderCompress(); refreshTarget(); }
  });
  $('#c-name').addEventListener('input', () => { state.c.touchedName = true; refreshTarget(); });
  $('#c-start').addEventListener('click', startCompress);
  $('#c-adv-toggle').addEventListener('click', (e) => {
    const open = e.currentTarget.getAttribute('aria-expanded') === 'true';
    e.currentTarget.setAttribute('aria-expanded', String(!open));
    $('#c-adv').classList.toggle('is-open', !open);
  });

  // extract
  $('#x-pick').addEventListener('click', async (e) => {
    e.stopPropagation();
    const paths = await pick('archive', { multiple: false, prompt: '打开' });
    if (paths.length) loadArchive(paths[0]);
  });
  $('#drop-x').addEventListener('click', async () => {
    const paths = await pick('archive', { multiple: false, prompt: '打开' });
    if (paths.length) loadArchive(paths[0]);
  });
  $('#drop-x').addEventListener('keydown', async (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); const p = await pick('archive', { multiple: false }); if (p.length) loadArchive(p[0]); }
  });
  $('#x-change').addEventListener('click', async () => {
    const paths = await pick('archive', { multiple: false, prompt: '打开' });
    if (paths.length) loadArchive(paths[0]);
  });
  $('#x-remember').addEventListener('click', () => {
    state.x.remember = !state.x.remember;
    renderExtract();
  });
  $('#x-pw').addEventListener('input', (e) => {
    state.x.pw = e.target.value;
    if (state.x.info) state.x.info.savedPasswordUsed = false;
    $('#x-pw-status').textContent = state.x.remember ? '仅在本次解压或校验时保存到系统钥匙串' : '';
    // Unlock the start button as soon as a password exists -- without this the
    // locked state asks for a password but keeps the button disabled-looking.
    const locked = !!(state.x.info && state.x.info.needsPassword
                      && !(state.x.info.entries || []).length);
    if (locked) {
      $('#x-start').disabled = !state.x.pw;
      $('#x-outhint').textContent = state.x.pw ? '将用输入的密码解压' : '先输入密码，再开始解压';
    }
  });
  $('#x-pw').addEventListener('keydown', async (e) => {
    if (e.key === 'Enter' && state.x.pw) await verifyPassword();
  });
  $('#x-pw-eye').addEventListener('click', () => {
    const input = $('#x-pw');
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    $('#x-pw-eye').innerHTML = icon(show ? 'i-eye-off' : 'i-eye');
    $('#x-pw-eye').setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    $('#x-pw-eye').title = show ? '隐藏密码' : '显示密码';
  });
  $('#x-verify').addEventListener('click', verifyPassword);
  $('#x-dest').addEventListener('click', async () => {
    const paths = await pick('dest', { title: '选择解压位置', dir: state.x.dest || dirName(state.x.path) });
    if (paths.length) { state.x.dest = paths[0]; renderExtract(); }
  });
  $('#x-folder').addEventListener('click', (e) => {
    const next = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(next));
    state.x.autoFolder = next;
    renderExtract();
  });
  $('#x-over').addEventListener('change', (e) => { state.x.overwrite = e.target.value; renderExtract(); });
  $('#x-show-all').addEventListener('click', () => {
    state.x.expanded = true;
    renderExtract();
  });
  $('#x-start').addEventListener('click', startExtract);
  $('#x-test').addEventListener('click', testArchive);

  // queue
  $('#q-list').addEventListener('click', async (e) => {
    const retry = e.target.closest('[data-retry]');
    if (retry) {
      retry.disabled = true;
      try {
        await api('/jobs/retry', { id: retry.dataset.retry });
        refreshJobsNow();
      } catch (err) {
        toast(err.message, 'err');
        retry.disabled = false;
      }
      return;
    }
    const cancel = e.target.closest('[data-cancel]');
    const remove = e.target.closest('[data-remove]');
    const reveal = e.target.closest('[data-reveal]');
    const open = e.target.closest('[data-open]');
    if (cancel) await cancelJob(cancel.dataset.cancel);
    if (remove) await removeJob(remove.dataset.remove);
    if (reveal && reveal.dataset.reveal) native('reveal', { path: reveal.dataset.reveal });
    if (open && open.dataset.open) native('open', { path: open.dataset.open });
  });
  // 清理动作先说清范围（§5.3）：清历史要二次确认并写明条数与「文件不受影响」；
  // 有已完成任务在屏时直接清卡片，但完成后用 toast 披露实际清了什么。
  const qClear = $('#q-clear');
  let qClearTimer = null;
  qClear.addEventListener('click', async () => {
    const historyOnly = !state.jobs.length && state.history.length > 0;
    if (historyOnly && qClear.dataset.armed !== '1') {
      qClear.dataset.armed = '1';
      qClear.dataset.label = qClear.textContent;
      qClear.textContent = `确认清除 ${state.history.length} 条历史？文件不受影响`;
      qClearTimer = setTimeout(() => {
        delete qClear.dataset.armed;
        qClear.textContent = qClear.dataset.label || '清除历史';
      }, 3500);
      return;
    }
    clearTimeout(qClearTimer);
    delete qClear.dataset.armed;
    const jobsOnScreen = state.jobs.filter((j) => j.status !== 'queued' && j.status !== 'running').length;
    const historyCount = state.history.length;
    await api('/jobs/clear', {});
    if (historyOnly) {
      toast(`已清除 ${historyCount} 条历史 · 输出文件未动`, 'ok');
    } else if (jobsOnScreen || historyCount) {
      const parts = [];
      if (jobsOnScreen) parts.push(`${jobsOnScreen} 个已完成任务`);
      if (historyCount) parts.push(`${historyCount} 条历史`);
      toast(`已清理 ${parts.join('与')} · 输出文件未动`, 'ok');
    }
    refreshJobsNow();
  });
  const qEmptyGo = $('#q-empty-go');
  if (qEmptyGo) qEmptyGo.addEventListener('click', () => setView('compress'));
  const qEmptyPick = $('#q-empty-pick');
  if (qEmptyPick) qEmptyPick.addEventListener('click', () => { setView('extract'); $('#x-pick').click(); });

  // keyboard
  document.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (!mod) return;
    if (e.key === '1') { e.preventDefault(); setView('compress', { instant: true }); }
    else if (e.key === '2') { e.preventDefault(); setView('extract', { instant: true }); }
    else if (e.key === '3') { e.preventDefault(); setView('queue', { instant: true }); }
    else if (e.key === 'o') {
      e.preventDefault();
      if (state.view === 'extract') $('#x-pick').click(); else $('#c-pick').click();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (state.view === 'extract') startExtract();
      else if (state.view === 'compress') startCompress();
    }
  });
}

async function verifyPassword() {
  const x = state.x;
  if (!x.pw || !x.path) return;
  const btn = $('#x-verify');
  btn.disabled = true;
  btn.textContent = '校验中';
  const fail = (message) => {
    state.x.error = message;
    renderExtract();
    // 密码错误后留在输入框里并全选，立刻重输不用再点一次（§5.2）。
    const box = $('#x-pw');
    box.focus();
    box.select();
  };
  try {
    const r = await api('/inspect', { path: x.path, password: x.pw });
    const info = r.info || {};
    // An encrypted archive answers a wrong password with the same
    // "needsPassword" payload as no password at all -- that is a failure,
    // not a success with an empty listing.
    if (info.needsPassword && !(info.entries || []).length) {
      fail('密码不正确，或该文件已损坏');
      return;
    }
    state.x.info = info;
    state.x.error = '';
    toast('密码正确', 'ok');
    renderExtract();
    $('#x-start').focus();
  } catch (err) {
    fail(err.message || '密码不正确，或该文件已损坏');
  } finally {
    btn.disabled = false;
    btn.textContent = '验证';
  }
}

/* ---------------------------------------------------------------------- init */
async function init() {
  try {
    const meta = await api('/meta');
    state.meta = meta;
    state.history = meta.history || [];
    state.settings = meta.settings || {};
    homeDir = meta.home || '';
    $('#engine-ver').textContent = `v${meta.engine.engineVersion} · 内置`;
    if (!meta.engine.rarEncode) {
      $('#engine-card').title = 'RAR 解压可用；创建 RAR 需安装 WinRAR 命令行';
    }
  } catch (err) {
    toast('无法连接到本地服务：' + err.message, 'err');
  }
  renderStatsNotice();

  applyTheme(state.settings.appearance || 'system');
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if ((state.settings.appearance || 'system') === 'system') applyTheme('system');
  });

  // restore defaults into the compress form
  const s = state.settings;
  state.c.format = s.format || '7z';
  state.c.level = Number(s.level ?? 3);
  state.c.excludeMeta = s.excludeMeta !== false;
  state.c.encryptNames = s.encryptNames !== false;
  state.c.splitId = s.splitId || 'none';
  state.c.overwrite = s.overwrite || 'rename';
  state.x.overwrite = state.c.overwrite;

  wireEvents();
  wireDragDrop();
  wireContextMenu();
  renderCompress();
  requestAnimationFrame(() => syncSeg($('#c-format')));
  window.addEventListener('resize', debounce(() => syncSeg($('#c-format'), { instant: true }), 120));

  // Hand the native drag strip the header geometry (and keep it in step when
  // the window is resized or the font finishes loading).
  reportDragRegion();
  window.addEventListener('resize', debounce(reportDragRegion, 120));
  // The menu is its own component; it announces when it appears or goes away.
  document.addEventListener('arca:overlay', reportDragRegion);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(reportDragRegion).catch(() => {});
  }

  // files passed on the command line (open -a Arca file.zip)
  if (BOOT.argv && BOOT.argv.length) {
    const arch = BOOT.argv.find((p) => ARCHIVE_RE.test(p));
    if (arch) loadArchive(arch);
    else addToCompress(BOOT.argv);
  }

  // Finder 服务 / 打开方式送来的请求：冷启动时它们早于页面就绪，所以是拉
  // 而不是推。 1.2s 的轮询跟任务轮询同量级，不值得再开一条事件通道。
  pollInbox();
  setInterval(pollInbox, 1200);

  pollJobs();

  // The pasteboard is outside our process, so keep its file list warm: on
  // boot and whenever the window comes back to the front.
  refreshClipCache();
  window.addEventListener('focus', () => refreshClipCache());

  renderEngineBadge();
  // Let the window settle first: the dialog should not race the first paint.
  setTimeout(maybeCheckEngineUpdate, 900);
  // An app update sits behind the same quiet check, but only after the engine
  // dialog had its chance to claim the screen.
  setTimeout(maybeCheckAppUpdate, 1600);
}

document.addEventListener('DOMContentLoaded', init);
})();
