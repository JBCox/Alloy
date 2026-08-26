"""Boots ui/index.html's inline script for real, in Node, against a stub DOM.

Every other suite here reads the UI as TEXT. That cannot see the failure mode
this file exists for: the whole UI is ONE inline <script>, so a single throw at
top level silently kills everything after it — no seats, no `pywebviewready`
listener, and every model/thinking menu permanently blank, with a window that
otherwise looks fine. That is exactly what a `syncPermissionNote()` call placed
above its own `let activeId` declaration did on 2026-08-21 (TDZ ReferenceError).

So this suite EXECUTES the script and asserts the boot path actually produced
populated menus, and — because a harness that quietly stops detecting anything
is worse than no harness — re-injects that original bug into a copy and asserts
it is still caught.

Token-free, like the rest of tests/. Skips cleanly where node is unavailable;
node is already a hard dependency of the relay (the codex/gemini npm shims).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")
NODE = shutil.which("node")

# What app.Api.get_config would hand the UI. Deliberately uneven counts so an
# assertion cannot pass on a coincidence.
STUB_CONFIG = {
    "claude_models": [{"id": "claude-opus-5", "label": "Opus 5"},
                      {"id": "claude-haiku-4-5", "label": "Haiku 4.5"}],
    "claude_default_model": "claude-opus-5",
    "claude_default_effort": "high",
    "gpt_models": [{"id": "gpt-5.6-sol", "label": "Sol",
                    "levels": ["low", "medium", "high"],
                    "default_level": "medium"}],
    "gpt_default_model": "gpt-5.6-sol",
    "gpt_default_effort": "high",
    "gemini_families": [{"base": "gemini-3.7-flash", "label": "Gemini 3.7 Flash",
                         "levels": ["high", "medium", "low"]}],
    "gemini_default_family": "gemini-3.7-flash",
    "ox_models": [
        {"id": "opencode/x-preview-f-free", "label": "Ox Alpha",
         "levels": ["low", "high", "max"], "default_level": "high"},
        {"id": "opencode/big-pickle", "label": "Big Pickle",
         "levels": [], "default_level": ""},
        {"id": "opencode/nemotron-3-ultra-free", "label": "Nemotron 3 Ultra",
         "levels": [], "default_level": ""},
    ],
    "ox_default_model": "opencode/x-preview-f-free",
    "ox_default_effort": "high",
    "gemini_default_level": "high",
    "dictation": {"available": True, "engine": "whisper", "model": "base.en",
                  "label": "Whisper (local)", "cached": True, "reason": ""},
}

DOM_JS = r"""// Minimal DOM stub: enough to BOOT ui/index.html's one inline script.
// Deliberately permissive — unknown members answer with no-op functions so a
// missing stub method can never masquerade as a UI bug.
'use strict';

// A real browser reflects these HTML attributes onto their IDL properties,
// including when they appear BARE (`<button hidden>`). A stub that dropped the
// bare form reported every such element as visible/enabled at boot — the exact
// lie that let "clear button ships hidden" fail while looking like UI breakage
// (2026-08-23).
const BOOL_ATTRS = {hidden: true, disabled: true, checked: true, selected: true};

function applyAttrs(el, attrs) {
  const are = /([^\s=]+)(?:\s*=\s*"([^"]*)")?/g;
  let a;
  while ((a = are.exec(attrs))) {
    const k = (a[1] || '').toLowerCase();
    if (!k || k === '/') continue;
    if (a[2] === undefined) {
      el.setAttribute(k, '');                    // bare boolean attribute
      if (BOOL_ATTRS[k]) el[k] = true;
      continue;
    }
    if (k === 'class') el.className = a[2];
    else if (k === 'id') el.id = a[2];
    else el.setAttribute(k, a[2]);
  }
}

function parseFragment(html, mk) {
  // Flat scan: every <tag ...> becomes a child element. Nesting is ignored on
  // purpose — querySelector below searches descendants flatly, which is all
  // the seat/modal builders need.
  const out = [];
  const re = /<([a-zA-Z][\w-]*)((?:\s+[^\s=>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>/g;
  let m;
  while ((m = re.exec(html))) {
    const el = mk(m[1].toLowerCase());
    applyAttrs(el, m[2] || '');
    out.push(el);
  }
  return out;
}

function matches(el, sel) {
  sel = sel.trim();
  // only the selector forms the UI actually uses on built fragments
  for (const part of sel.split(',')) {
    const p = part.trim();
    if (!p) continue;
    if (p.startsWith('.') && (' ' + el.className + ' ').includes(' ' + p.slice(1) + ' ')) return true;
    if (p.startsWith('#') && el.id === p.slice(1)) return true;
    if (/^[a-zA-Z]/.test(p) && el.tag === p.toLowerCase().split(/[.#\s\[]/)[0]) {
      // [attr], [attr=value] and :checked are matched for REAL. Ignoring the
      // bracket and treating "input[name=x]" as bare "input" is far worse than
      // not supporting it: the UI's own bindings then land on EVERY input on
      // the page, and the suite reports the damage as a failure somewhere else
      // entirely (cost an hour on 2026-08-22).
      const attr = p.match(/\[([\w-]+)(?:\s*=\s*"?([^\]"]*)"?)?\]/);
      if (attr) {
        const have = el.getAttribute(attr[1]);
        if (have === undefined || have === null) continue;
        if (attr[2] !== undefined && String(have) !== attr[2]) continue;
      }
      if (/:checked\b/.test(p) && !el.checked) continue;
      if (/:disabled\b/.test(p) && !el.disabled) continue;
      const cls = p.match(/\.([\w-]+)/);
      if (!cls) return true;
      if ((' ' + el.className + ' ').includes(' ' + cls[1] + ' ')) return true;
    }
  }
  return false;
}

let byId = {};

function mkEl(tag) {
  const el = {
    tag, id: '', className: '', children: [], parent: null,
    _attrs: {}, _html: '', textContent: '', value: '', checked: false,
    hidden: false, disabled: false, selected: false, options: [],
    dataset: {},
    // records custom properties instead of swallowing them: a UI that paints
    // per-tab colour through setProperty('--tab', …) is otherwise untestable
    style: {_props: {}, display: '',
            setProperty(k, v) { this._props[k] = v; },
            removeProperty(k) { delete this._props[k]; },
            getPropertyValue(k) { return this._props[k] || ''; }},
    // A REAL classList. The no-op version silently lied in both directions:
    // code under test could not open a modal (`.add("show")` did nothing) and
    // could not read one back (`contains` always false), so a suite could
    // "pass" while the thing it drove never happened (2026-08-22).
    classList: {
      _own: null,
      _list() { return String(this._own.className || '').split(/\s+/).filter(Boolean); },
      _set(parts) { this._own.className = parts.join(' '); },
      add(...names) {
        const parts = this._list();
        names.forEach(n => { if (n && !parts.includes(n)) parts.push(n); });
        this._set(parts);
      },
      remove(...names) {
        this._set(this._list().filter(n => !names.includes(n)));
      },
      toggle(name, force) {
        const on = force === undefined ? !this.contains(name) : !!force;
        if (on) this.add(name); else this.remove(name);
        return on;
      },
      contains(name) { return this._list().includes(name); },
    },
    setAttribute(k, v) { this._attrs[k] = v; if (k === 'id') { this.id = v; byId[v] = this; } },
    getAttribute(k) { return this._attrs[k]; },
    removeAttribute(k) { delete this._attrs[k]; },
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    focus() {}, blur() {}, select() {}, click() {}, scrollIntoView() {},
    getBoundingClientRect() { return {top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0}; },
    setPointerCapture() {}, releasePointerCapture() {},
    remove() {
      if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this);
    },
    appendChild(c) {
      c.parent = this;
      this.children.push(c);
      if (this.tag === 'select' && c.tag === 'option') {
        this.options.push(c);
        if (c.selected || this.options.length === 1) this.value = c.value;
      }
      return c;
    },
    append(...cs) { cs.forEach(c => typeof c === 'object' && this.appendChild(c)); },
    insertBefore(c) { return this.appendChild(c); },
    prepend(c) { c.parent = this; this.children.unshift(c); return c; },
    querySelector(sel) { return this._all().find(e => matches(e, sel)) || null; },
    querySelectorAll(sel) { return this._all().filter(e => matches(e, sel)); },
    closest() { return null; },
    _all() {
      const out = [];
      const walk = n => n.children.forEach(c => { out.push(c); walk(c); });
      walk(this);
      return out;
    },
  };
  el.classList._own = el;
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) {
      this._html = String(v);
      this.children = [];
      if (this.tag === 'select') { this.options = []; this.value = ''; }
      parseFragment(this._html, mkEl).forEach(c => this.appendChild(c));
    },
  });
  Object.defineProperty(el, 'firstChild', {get() { return this.children[0] || null; }});
  Object.defineProperty(el, 'lastChild', {get() { return this.children[this.children.length - 1] || null; }});
  Object.defineProperty(el, 'childNodes', {get() { return this.children; }});
  Object.defineProperty(el, 'children_', {get() { return this.children; }});
  Object.defineProperty(el, 'scrollHeight', {get() { return 100; }});
  Object.defineProperty(el, 'clientHeight', {get() { return 100; }});
  Object.defineProperty(el, 'scrollTop', {get() { return 0; }, set() {}});
  // permissive fallback: unknown members answer as no-op functions
  return new Proxy(el, {
    get(t, k) {
      if (k in t) return t[k];
      if (typeof k === 'symbol') return undefined;
      return function () { return undefined; };
    },
    set(t, k, v) { t[k] = v; return true; },
    has() { return true; },
  });
}

module.exports = {mkEl, matches, applyAttrs, byId, resetIds() { byId = {}; }, getById: id => byId[id] || null};
"""

BOOT_JS = r"""'use strict';
// Boots ui/index.html's inline script against the stub DOM and reports what
// happened as JSON on stdout. Any top-level throw is captured, not swallowed.
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const {mkEl, matches, applyAttrs} = require(path.join(__dirname, 'dom.js'));

const HTML = fs.readFileSync(process.argv[2], 'utf8');
const script = HTML.match(/<script>([\s\S]*)<\/script>/)[1];
const markup = HTML.replace(/<script>[\s\S]*<\/script>/, '');

// ---- build the document from the REAL markup -------------------------------
const all = [];
const byId = Object.create(null);
const tagRe = /<([a-zA-Z][\w-]*)((?:\s+[^\s=>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>/g;
let m;
while ((m = tagRe.exec(markup))) {
  const el = mkEl(m[1].toLowerCase());
  const attrs = m[2] || '';
  applyAttrs(el, attrs);                       // shared with parseFragment: bare
  const vAttr = el.getAttribute('value');      // booleans reflected, not dropped
  if (vAttr !== undefined && vAttr !== null) el.value = vAttr;
  if (el.id) byId[el.id] = el;
  all.push(el);
  // give real <select id=...> its static options, so .value is truthful
  if (el.tag === 'select' && el.id) {
    const open = markup.indexOf(m[0], m.index);
    const close = markup.indexOf('</select>', open);
    if (close > -1) {
      const inner = markup.slice(open + m[0].length, close);
      const ore = /<option([^>]*)>([^<]*)</g;
      let o;
      while ((o = ore.exec(inner))) {
        const opt = mkEl('option');
        const v = o[1].match(/value\s*=\s*"([^"]*)"/);
        opt.value = v ? v[1] : o[2].trim();
        opt.textContent = o[2].trim();
        if (/\bselected\b/.test(o[1])) opt.selected = true;
        el.appendChild(opt);
      }
    }
  }
}

// KNOWN LIMITATION: markup is parsed into a FLAT list by a tag regex, so
// elements from index.html have no children. `el.querySelector('.thing')` on a
// STATIC element therefore finds nothing, while the same call on a fragment the
// script itself built works fine. Code that walks into static markup needs the
// same absence guard it would want in a browser anyway.
// static markup PLUS everything the script has appended since
function everyEl() {
  const out = [];
  const seen = new Set();
  const walk = n => {
    if (!n || seen.has(n)) return;
    seen.add(n);
    out.push(n);
    (n.children || []).forEach(walk);
  };
  all.forEach(walk);
  return out;
}

const documentEl = mkEl('html');
const body = byId['__body'] || mkEl('body');
const document = {
  getElementById: id => byId[id] || null,
  createElement: tag => mkEl(tag),
  createTextNode: t => { const e = mkEl('#text'); e.textContent = t; return e; },
  createDocumentFragment: () => mkEl('#fragment'),
  querySelector: sel => everyEl().find(e => matches(e, sel)) || null,
  querySelectorAll: sel => everyEl().filter(e => matches(e, sel)),
  // recorded, not dropped: the keyboard-shortcuts overlay closes through a
  // document keydown listener, and a no-op here made that path untestable
  addEventListener(ev, fn) { (docListeners[ev] = docListeners[ev] || []).push(fn); },
  removeEventListener(ev, fn) {
    const fns = docListeners[ev] || [];
    const i = fns.indexOf(fn);
    if (i > -1) fns.splice(i, 1);
  },
  documentElement: documentEl,
  body,
  head: mkEl('head'),
  readyState: 'complete',
  execCommand() { return true; },
  hasFocus() { return true; },
  activeElement: body,
};

// ---- window / globals ------------------------------------------------------
const errors = [];
// Async UI paths (openChat's replay, search rendering) fail as unhandled
// rejections — invisible in a report that only captures top-level throws.
// Record them so a test can demand a clean async path, not just a clean boot.
const asyncErrors = [];
process.on('unhandledRejection', e => {
  asyncErrors.push((e && e.stack) || String(e));
});
const listeners = Object.create(null);
const docListeners = Object.create(null);
const store = Object.create(null);

const api = new Proxy({}, {
  get(_, name) {
    if (name === 'then') return undefined;          // must not look thenable
    return (...args) => Promise.resolve(apiReply(String(name), args));
  },
});

const savedTabs = [], autoResumeNoted = [], continued = [], apiCalls = [];
let interjectReply = null;   // set to {error} to drive the refusal path
function apiReply(name, args) {
  apiCalls.push(name);
  switch (name) {
    case 'get_config': return JSON.parse(process.env.STUB_CONFIG);
    case 'get_auth_status': return {
      providers: [
        {provider: 'claude', label: 'Claude', state: 'signed_in', seatable: true, color: '#D97757', email: null, detail: '', install_hint: '', can_logout: true},
        {provider: 'gpt', label: 'GPT', state: 'signed_in', seatable: true, color: '#10A37F', email: null, detail: '', install_hint: '', can_logout: true},
        {provider: 'gemini', label: 'Gemini', state: 'signed_in', seatable: true, color: '#4285F4', email: null, detail: '', install_hint: '', can_logout: true},
        {provider: 'ox', label: 'OpenCode', state: 'signed_in', seatable: true, color: '#C084FC', email: null, detail: '', install_hint: '', can_logout: true},
      ], ready: true,
    };
    case 'list_sessions': return [
      {id: 'sess-one', title: 'Death Factory', project: '', participants: [], run: {}},
      {id: 'sess-two', title: 'Second Chat', project: '', participants: [], run: {}},
    ];
    case 'get_tabs': return {
      open: [{id: 'sess-one', color: 'rose'}, {id: 'sess-two', color: ''}],
      active: 'sess-one',
    };
    case 'save_tabs': savedTabs.push(args && args[0]); return args && args[0];
    case 'interject': return interjectReply || {ok: true, text: args && args[0]};
    // A chat whose process died mid-run: reopened AND resumed at boot.
    case 'restart_resume': return {
      session_id: 'sess-one', resume: true,
      reason: 'This conversation was still running when the app closed, so it '
              + 'is being picked up where it left off.',
    };
    case 'note_auto_resume': autoResumeNoted.push(args && args[0]);
      return {ok: true, count: 1};
    case 'continue_chat': continued.push(args && args[0]); return {ok: true};
    case 'open_session': return {
      ok: true, live: false, thinking: [],
      messages: [{speaker: 'josh', name: 'Josh', text: 'earlier message',
                  meta: '', ts: '2026-08-23T08:00:00'}],
      session: {id: args && args[0], title: 'Death Factory', participants: [],
                can_continue: true, interrupted: true, mode: 'round_robin',
                workspace: '', project: '', transcript: '', completion:
                {lifecycle: 'active', goal_verdict: 'unknown'}},
    };
    case 'list_workspace_files': return [];
    case 'list_runs': return {runs: []};
    case 'folder_exists': return false;
    case 'get_skills': return {skills: []};
    case 'get_mcp': return {ok: true};
    case 'search_sessions': return {
      query: args && args[0], truncated: false,
      chats: [
        {id: 'sess-two', title: 'Second Chat', project: 'Alloy',
         providers: ['claude'], updated: '2026-08-23T08:00:00',
         count: 3, title_match: true,
         snippets: [{name: 'Claude', ts: '2026-08-23T08:01:00',
                     excerpt: '…the Nile carries sediment to its delta…'}]},
      ],
    };
    default: return {ok: true};
  }
}

const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp,
  Error, TypeError, Map, Set, WeakMap, WeakSet, Symbol, Proxy, Reflect,
  parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent,
  btoa: s => Buffer.from(String(s), 'binary').toString('base64'),
  atob: s => Buffer.from(String(s), 'base64').toString('binary'),
  document,
  navigator: {clipboard: {writeText: () => Promise.resolve()}, userAgent: 'node', platform: 'Win32'},
  location: {href: 'file:///index.html', reload() {}},
  localStorage: {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  },
  requestAnimationFrame: fn => setTimeout(fn, 0),
  cancelAnimationFrame: id => clearTimeout(id),
  matchMedia: () => ({matches: false, addEventListener() {}, addListener() {}}),
  getComputedStyle: () => new Proxy({}, {get: () => ''}),
  MutationObserver: class { observe() {} disconnect() {} },
  ResizeObserver: class { observe() {} disconnect() {} unobserve() {} },
  IntersectionObserver: class { observe() {} disconnect() {} unobserve() {} },
  Image: class { set src(v) {} },
  FileReader: class { readAsDataURL() {} },
  Blob: class {}, URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
  fetch: () => Promise.resolve({ok: true, json: () => Promise.resolve({}), text: () => Promise.resolve('')}),
  addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
  removeEventListener() {},
  dispatchEvent() {},
  scrollTo() {}, alert() {}, prompt: () => null, confirm: () => true,
  innerWidth: 1220, innerHeight: 820, devicePixelRatio: 1,
  pywebview: {api},
  __errors: errors,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const ctx = vm.createContext(sandbox);

let topLevelError = null;
try {
  vm.runInContext(script, ctx, {filename: 'ui-inline.js'});
} catch (e) {
  topLevelError = (e && e.message) || String(e);
}

function report(extra) {
  const seats = document.querySelectorAll('.seat');
  const seatInfo = seats.map(s => {
    const model = s.querySelector('.model');
    const effort = s.querySelector('.effort');
    return {
      provider: (s.dataset && s.dataset.provider) || s.getAttribute('data-provider') || '',
      modelOptions: model ? model.options.length : -1,
      modelValue: model ? model.value : null,
      effortOptions: effort ? effort.options.length : -1,
    };
  });
  const payload = JSON.stringify(Object.assign({
    topLevelError,
    asyncErrors,
    seats: seatInfo,
    permissionNote: byId['permissionNote'] ? byId['permissionNote'].textContent : null,
    modModelOptions: byId['modModel'] ? byId['modModel'].options.length : -1,
    bootRan: !!extra.bootRan,
    bootError: extra.bootError || null,
  }, extra.more || {}), null, 1);
  // The page legitimately schedules 1-second tickers (the typing clock, seat
  // telemetry). Node will not exit while one is pending, so the harness has
  // to say when it is DONE — otherwise the first test to drive a `thinking`
  // event hangs the whole suite until its 60s timeout, and reports nothing
  // (2026-08-23). Exit from the write callback so stdout is flushed first.
  process.stdout.write(payload, () => process.exit(0));
}

if (topLevelError) {
  report({});          // report() exits once stdout has flushed
}

// ---- fire pywebviewready, the real boot path -------------------------------
(async () => {
  let bootError = null;
  const fns = listeners['pywebviewready'] || [];
  for (const fn of fns) {
    try { await fn({}); } catch (e) { bootError = (e && e.stack) || String(e); }
  }
  await new Promise(r => setTimeout(r, 30));

  // ---- drive one dictation event through the REAL uiEvent path -------------
  // The mic's only job is to land words in the composer, and that path is
  // pure top-level script: a throw anywhere in it is invisible to text-reading
  // suites.
  // _html too: message bodies are set through innerHTML, and the stub keeps
  // the raw string there rather than re-deriving a parent's textContent.
  const deepText = el => ((el.textContent || '') + ' ' + (el._html || '') + ' ' +
    (el.children || []).map(deepText).join(' ')).trim();
  const more = {};
  // Boot now REOPENS and RESUMES an interrupted chat, so the evidence must be
  // read before anything else touches the stage — and the remaining probes
  // need the unseated draft they have always assumed, hence newChat().
  more.resume = {
    noted: autoResumeNoted.slice(),
    continued: continued.map(c => ({session_id: c && c.session_id,
                                    opener: c && c.opener})),
    // the stub does NOT aggregate a parent's textContent from its children,
    // so gather it by hand rather than reading an empty string
    reopenedText: [...byId['feed'].children].map(deepText).join(' | ').slice(0, 600),
  };
  try { await ctx.newChat(); } catch (e) { more.newChatError = String(e); }
  const mic = byId['micBtn'];
  more.micHidden = mic ? !!mic.hidden : null;
  more.micDisabled = mic ? !!mic.disabled : null;
  more.micTitle = mic ? String(mic.title || '') : null;
  try {
    byId['say'].value = 'draft';
    ctx.uiEvent({event: 'dictation',
                 payload: {state: 'text', text: '  hello there  ', note: ''}});
    more.sayAfterDictation = byId['say'].value;
  } catch (e) {
    more.dictationError = (e && e.stack) || String(e);
  }
  // ---- drive the promoted moderator toggle through its REAL handler -------
  // The picker only appears when the room actually has a moderator, so the
  // toggle's whole job is to put floorSel there and reveal it. Text suites
  // can see the markup but not whether the handler works.
  try {
    more.modToggleHiddenAtBoot = byId['modToggleRow']
      ? !!byId['modToggleRow'].hidden : null;
    more.modCtlHiddenBefore = byId['modCtl'] ? !!byId['modCtl'].hidden : null;
    byId['modOn'].checked = true;
    byId['modOn'].onchange();
    more.floorAfterToggle = byId['floorSel'] ? byId['floorSel'].value : null;
    more.modCtlHiddenAfter = byId['modCtl'] ? !!byId['modCtl'].hidden : null;
    byId['modOn'].checked = false;
    byId['modOn'].onchange();
    more.floorAfterUntoggle = byId['floorSel'] ? byId['floorSel'].value : null;
  } catch (e) {
    more.modToggleError = (e && e.stack) || String(e);
  }
  // ---- a seat mid-turn has to LOOK like one --------------------------------
  // 14 minutes into a 15-minute window used to render identically to a dead
  // app. The clock is the whole fix, so drive it through the real event.
  try {
    more.think = {};
    ctx.uiEvent({event: 'thinking', payload: {chat_id: null, speaker: 0,
      provider: 'claude', name: 'Claude', limit: 900, round: 1, turns: 3,
      turn: 1}});
    const el = [...byId['feed'].children].find(c => (c.className||'').includes('typing'));
    more.think.rendered = !!el;
    const clock = el && el.querySelector('.tclock');
    more.think.clock = clock ? clock.textContent : null;
    more.think.limitKept = el ? String(el.dataset.limit) : null;
    // an OLD turn must not restart the clock at 0:00 on reopen
    if (typeof ctx.showTyping === 'function') {
      ctx.showTyping(1, 'gpt', 'GPT',
                     {started: (Date.now() / 1000) - 840, limit: 900});
      const old = [...byId['feed'].children]
        .filter(c => (c.className||'').includes('typing')).pop();
      const oc = old && old.querySelector('.tclock');
      more.think.replayed = oc ? oc.textContent : null;
      more.think.late = oc ? (oc.className||'').includes('late') : null;
    }
    // A seat under the IDLE watchdog has no deadline while it works. The
    // clock must not invent one -- "of 15:00" on a turn nothing will cut off
    // is the exact lie that made a healthy run read as a hung app.
    if (typeof ctx.showTyping === 'function' && typeof ctx.tickTyping === 'function') {
      ctx.showTyping(9, 'claude', 'Idle', {idle: 300});
      const iel = [...byId['feed'].children]
        .filter(c => (c.className||'').includes('typing')).pop();
      ctx.tickTyping();
      const ic = iel && iel.querySelector('.tclock');
      more.think.idleClock = ic ? ic.textContent : null;
      more.think.idleLate = ic ? (ic.className||'').includes('late') : null;
      // ...but once it actually goes quiet, say so, against the real window
      iel.dataset.lastact = (Date.now() / 1000) - 200;
      ctx.tickTyping();
      more.think.quietClock = ic ? ic.textContent : null;
      more.think.quietLate = ic ? (ic.className||'').includes('late') : null;
      // an activity event is proof of life and must reset the quiet clock
      ctx.uiEvent({event: 'activity', payload: {chat_id: null, speaker: 9,
        provider: 'claude', kind: 'tool', text: 'reading a file'}});
      ctx.tickTyping();
      more.think.afterActivity = ic ? ic.textContent : null;
    }
    ctx.hideAllTyping();
    more.think.clearedAfterHide = [...byId['feed'].children]
      .filter(c => (c.className||'').includes('typing')).length;
    more.think.timerLive = ctx.typingTimer !== null && ctx.typingTimer !== undefined;
    more.think.mapSize = ctx.typingEls ? ctx.typingEls.size : 'n/a';
  } catch (e) { more.thinkError = (e && e.stack) || String(e); }

  // ---- the Keep Improving warning modal, driven like a user ---------------
  // The acknowledgement checkbox is the ONLY thing that enables OK, and the
  // wording has to change when every limit is off. Neither is visible to a
  // suite that reads the file as text.
  try {
    more.cont = {};
    const modal = byId['contModal'];
    more.cont.hiddenAtBoot = !(modal.className || '').includes('show');
    more.cont.onAtBoot = ctx.continuousCfg() !== null;
    // selecting the card must open the warning, not silently arm the mode
    byId['presetGrid'];
    ctx.applyPreset('keep_improving');
    more.cont.openedByCard = (byId['contModal'].className || '').includes('show');
    more.cont.onBeforeOk = ctx.continuousCfg() !== null;
    more.cont.okDisabledBeforeAck = !!byId['contOk'].disabled;
    more.cont.ackDefault = byId['contAgreeText'].textContent;
    // every limit off => a different promise, so a different sentence
    byId['contSpendOn'].checked = false;
    byId['contHoursOn'].checked = false;
    byId['contWatchdogStop'].checked = false;
    byId['contSpendOn'].onchange();
    more.cont.ackNaked = byId['contAgreeText'].textContent;
    more.cont.warnNaked = byId['contAck'].textContent;
    more.cont.okStillDisabled = !!byId['contOk'].disabled;
    // ticking it is what unlocks OK
    byId['contAgree'].checked = true;
    byId['contAgree'].onchange();
    more.cont.okAfterAck = !!byId['contOk'].disabled;
    byId['contMinutes'].value = '2';       // below the floor
    byId['contMinutes'].onchange();
    more.cont.minutesClamped = String(byId['contMinutes'].value);
    // Cancel must put the cards back where they were, not re-open the
    // warning forever (the previous preset used to be read AFTER the cards
    // had already moved to keep_improving).
    ctx.closeContinuous();
    more.cont.presetAfterCancel = byId['presetSel'].value;
    more.cont.closedAfterCancel = !(byId['contModal'].className || '').includes('show');
    more.cont.onAfterCancel = ctx.continuousCfg() !== null;
    ctx.applyPreset('keep_improving');      // and it re-opens cleanly
    byId['contAgree'].checked = true;
    byId['contAgree'].onchange();
    byId['contOk'].onclick();
    more.cont.onAfterOk = ctx.continuousCfg() !== null;
    more.cont.closedAfterOk = !(byId['contModal'].className || '').includes('show');
    more.cont.cfg = ctx.continuousCfg ? ctx.continuousCfg() : 'missing';
    more.cont.preset = byId['presetSel'].value;
    // and switching away turns it off again
    ctx.applyPreset('open_discussion');
    more.cont.onAfterSwitch = ctx.continuousCfg() !== null;
  } catch (e) { more.contError = (e && e.stack) || String(e); }

  // ---- the rounds box, typed the way a user types it ---------------------
  // It used to be a <b> written with textContent; now it is a real input, so
  // the clamp and the focus guard are behaviour only an executing suite sees.
  try {
    const box = byId['rVal'];
    more.roundsTag = box ? box.tag : null;
    more.roundsAtBoot = box ? String(box.value) : null;
    box.value = '25'; box.onchange();
    more.roundsTyped = String(box.value);
    // top-level `let` is not a vm context property, so read the number
    // through the function that actually ships it to the engine
    more.roundsTypedCfg = (typeof ctx.orchestrationCfg === 'function')
      ? ctx.orchestrationCfg().budget.limit : 'orchestrationCfg missing';
    box.value = '999'; box.onchange();          // above the cap
    more.roundsClampedHigh = String(box.value);
    box.value = '0'; box.onchange();            // below the floor
    more.roundsClampedLow = String(box.value);
    box.value = 'abc'; box.onchange();          // garbage keeps the last good
    more.roundsGarbage = String(box.value);
    byId['untilDone'].checked = true;
    byId['untilDone'].onchange();
    more.ceilingLabel = byId['roundsLabel'].textContent;
    box.value = '4000'; box.onchange();         // the ceiling has its own cap
    more.ceilingClamped = String(box.value);
    byId['untilDone'].checked = false;
    byId['untilDone'].onchange();
    more.roundsAfterUntilDone = String(box.value);
  } catch (e) { more.roundsError = (e && e.stack) || String(e); }

  // ---- the provider pickers, as a user would read them ------------------
  try {
    more.seatProviders = byId['addSeatProvider'].options
      .map(o => o.value + '=' + o.textContent);
    more.modProviders = byId['modProv'].options
      .map(o => o.value + '=' + o.textContent);
    const prevProv = byId['modProv'].value;
    byId['modProv'].value = 'ox';
    byId['modProv'].onchange();
    more.oxModeratorModels = byId['modModel'].options
      .map(o => o.value + '=' + o.textContent);
    more.oxModeratorLevels = byId['modEffort'].options.map(o => o.value);
    // put it back: report() reads modModel below, and a probe that leaves the
    // page in a different state is measuring itself
    byId['modProv'].value = prevProv;
    byId['modProv'].onchange();
    // moderatorCfg() only answers when the room HAS a moderator, so turn it
    // on first - the same click a user makes before naming it.
    byId['modOn'].checked = true;
    byId['modOn'].onchange();
    more.modNamePlaceholder = byId['modName'] ? byId['modName'].placeholder : null;
    byId['modName'].value = 'Referee';
    more.moderatorCfgName = (typeof ctx.moderatorCfg === 'function')
      ? (ctx.moderatorCfg() || {}).name : 'moderatorCfg missing';
    byId['modName'].value = '';
    more.unnamedCfgName = (typeof ctx.moderatorCfg === 'function')
      ? (ctx.moderatorCfg() || {}).name : 'moderatorCfg missing';
    byId['modOn'].checked = false;
    byId['modOn'].onchange();
  } catch (e) { more.pickerError = (e && e.stack) || String(e); }

  // ---- the open-tab strip, driven the way a user drives it ---------------
  try {
    const strip = byId['tabStrip'];
    const read = () => [...byId['tabList'].children].map(el => ({
      title: el.querySelector('.tab-title').textContent,
      color: (el.style && el.style._props && el.style._props['--tab']) ||
             (el.style && el.style.getPropertyValue &&
              el.style.getPropertyValue('--tab')) || '',
      active: (el.className || '').includes('active'),
    }));
    more.tabsRestored = read();
    more.tabStripHidden = !!strip.hidden;
    // close the second tab exactly as its ✕ does
    ctx.closeTab('sess-two');
    more.tabsAfterClose = read();
    more.tabSaves = savedTabs.length;
    more.lastSavedIds = (savedTabs[savedTabs.length - 1] || {}).open;
    // recolour the survivor
    ctx.setTabColor('sess-one', 'teal');
    more.tabsAfterColor = read();
  } catch (e) { more.tabError = (e && e.stack) || String(e); }

  // ---- cross-chat search, driven the way a user drives it ---------------
  try {
    more.searchBoxPresent = !!byId['chatSearch'];
    more.searchClearHiddenAtBoot = !!byId['chatSearchClear'].hidden;
    byId['chatSearch'].value = 'nile';
    byId['chatSearch'].oninput();
    await new Promise(r => setTimeout(r, 260));   // clear the 200ms debounce
    const list = byId['chatList'];
    const head = list.querySelector('.chat-search-h');
    more.searchHeader = head ? head.textContent : null;
    more.searchRows = [...list.querySelectorAll('.search-row')].map(r => ({
      title: r.querySelector('.t').textContent,
      count: r.querySelector('.s-count').textContent,
      snippet: r.querySelector('.s-snippet').textContent,
      proj: r.querySelector('.s-proj').textContent,
    }));
    more.searchClearShown = !byId['chatSearchClear'].hidden;
    // clicking the hit opens that chat (openChat with the query attached)
    const row = list.querySelector('.search-row');
    if (row) { row.onclick(); await new Promise(r => setTimeout(r, 30)); }
    // the stub selector engine has no descendant combinators, so counting
    // messages goes through the feed element itself
    more.searchOpenMsgCount =
      [...byId['feed'].querySelectorAll('.msg')].length;
    // below the two-character floor the normal rail comes back
    byId['chatSearch'].value = 'x';
    byId['chatSearch'].oninput();
    await new Promise(r => setTimeout(r, 260));
    more.railRestoredBelowFloor =
      list.querySelectorAll('.search-row').length === 0 &&
      list.querySelectorAll('.chat-group').length > 0;
    more.clearHiddenBelowFloor = !!byId['chatSearchClear'].hidden;
    // and Escape clears outright
    byId['chatSearch'].value = 'nile';
    ctx.clearChatSearch();
    more.valueAfterEscape = byId['chatSearch'].value;
    more.railRestoredAfterEscape =
      list.querySelectorAll('.chat-group').length > 0;
  } catch (e) { more.searchError = (e && e.stack) || String(e); }

  // ---- a message typed into a FRESH tab must start a chat, not vanish ------
  // `running` is per-CHAT state. resetStage() cleared everything else and left
  // it true, so the first send after "+" took sendSay's interject branch and
  // the bridge answered "No conversation is running." for a chat the UI had
  // already walked away from - the message itself silently discarded, every
  // time (reported 2026-08-23 with two identical relay rows on an empty feed).
  try {
    const p = more.newTab = {};
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-live', title: 'Live one', participants: [],
                mode: 'round_robin', can_continue: false},
      transcript: 'x/transcript.md', workspace: '', mode: 'round_robin'}});
    p.placeholderWhileRunning = byId['say'].placeholder;
    p.stopShownWhileRunning = !byId['stopBtn'].hidden;
    // a REFUSED interjection must hand the words back, not just an excuse
    interjectReply = {error: 'No conversation is running.'};
    byId['say'].value = 'words worth keeping';
    await ctx.sendSay();
    p.sayAfterRefusal = byId['say'].value;
    interjectReply = null;
    await ctx.newChat();
    p.placeholderAfterNewTab = byId['say'].placeholder;
    p.stopHiddenAfterNewTab = !!byId['stopBtn'].hidden;
    apiCalls.length = 0;
    const before = (byId['feed'].children || []).length;
    byId['say'].value = 'a fresh opening message';
    await ctx.sendSay();
    p.calls = apiCalls.slice();
    p.sayAfterSend = byId['say'].value;
    p.relayRows = [...byId['feed'].children].slice(before)
      .filter(c => (c.className || '').includes('msg'))
      .map(deepText).filter(t => t.includes('No conversation is running'));
  } catch (e) { more.newTabError = (e && e.stack) || String(e); }
  // ---- the keyboard shortcuts cheat sheet, driven like a user -------------
  // The overlay's whole contract: exists, hidden until asked for, the toggle
  // opens AND closes it, and Escape closes it through the REAL document
  // keydown handler. Text suites see the markup but none of the behaviour.
  try {
    const p = more.kbd = {};
    const modal = byId['kbdModal'];
    p.present = !!modal;
    p.hiddenAtBoot = modal ? !modal.classList.contains('show') : null;
    ctx.toggleKbd();
    p.openAfterToggle = modal.classList.contains('show');
    ctx.toggleKbd();
    p.closedAfterSecondToggle = !modal.classList.contains('show');
    const fireEscape = () => (docListeners['keydown'] || [])
      .forEach(fn => fn({key: 'Escape', target: document.body,
                         preventDefault() {}}));
    ctx.toggleKbd();
    fireEscape();
    p.closedByEscape = !modal.classList.contains('show');
  } catch (e) { more.kbdError = (e && e.stack) || String(e); }
  // ---- transcript structure: day dividers, grouping, the history lens -----
  // All three are VALUE-tracked (lastDayKey/lastSpeakerKey), so every reset
  // and break below is asserted on what actually rendered, not on DOM walks.
  try {
    const p = more.tstruct = {};
    const iso = d => d.getFullYear() + '-' +
      String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0');
    const TODAY = iso(new Date());
    const STUB_DAY = '2026-08-23';        // the open_session stub's ts date
    const dividers = () => [...byId['feed'].children]
      .filter(c => String(c.className || '').split(/\s+/).includes('day-divider'));
    const msgs = () => [...byId['feed'].children]
      .filter(c => String(c.className || '').split(/\s+/).includes('msg'));
    await ctx.newChat();
    // Day one: two Claude rows in a row read as ONE run.
    ctx.addMsg('claude', 'Claude', 'first ever', '', '', '2020-01-01T10:00:00');
    ctx.addMsg('claude', 'Claude', 'still going', '', '', '2020-01-01T10:05:00');
    p.firstNotGrouped = !msgs()[0].classList.contains('cont');
    p.secondGrouped = msgs()[1].classList.contains('cont');
    p.dayOneLabel = dividers()[0] ? dividers()[0].textContent : null;
    // Josh breaks the run; same day opens no second divider.
    ctx.addMsg('josh', 'Josh', 'a human line', '', '', '2020-01-01T10:06:00');
    p.joshBreaksRun = !msgs()[2].classList.contains('cont');
    p.dividersAfterDayOne = dividers().length;
    // A new calendar day opens a divider AND ends any run across it.
    ctx.addMsg('claude', 'Claude', 'next morning', '', '', '2020-01-02T09:00:00');
    p.dayTwoLabel = dividers()[1] ? dividers()[1].textContent : null;
    p.dayBreakEndsRun = !msgs()[3].classList.contains('cont');
    // Tail the draft with TODAY rows so a broken reset is OBSERVABLE: if
    // lastDayKey leaked into the reopened chat, its replayed row would open
    // no divider at all.
    ctx.addMsg('claude', 'Claude', 'today one', '', '', TODAY + 'T11:00:00');
    ctx.addMsg('claude', 'Claude', 'today two', '', '', TODAY + 'T11:01:00');
    p.todayRunGroups = msgs()[msgs().length - 1].classList.contains('cont');
    ctx.addMsg('josh', 'Josh', 'draft tail josh', '', '', STUB_DAY + 'T11:30:00');
    // ---- reopen (openChat): counters must restart, not leak ---------------
    await ctx.openChat('sess-one');
    const firstReplay = msgs()[0];
    p.replayFirstIsJosh = firstReplay
      ? String(firstReplay.className || '').includes('josh') : null;
    p.replayJoshGroupedWithDraft = firstReplay
      ? firstReplay.classList.contains('cont') : null;
    p.replayDividers = dividers().length;
    // ---- new tab (resetStage): same contract for the empty stage ----------
    await ctx.newChat();
    ctx.addMsg('josh', 'Josh', 'post reset', '', '', STUB_DAY + 'T12:00:00');
    p.afterResetDividers = dividers().length;
    // ---- the lens hides a divider whose every message is hidden -----------
    ctx.addMsg('claude', 'Claude', 'for sY eyes', '', '',
               '2020-01-01T10:00:00', null, null,
               {speaker: 'sX', origin: 'seat', audience: '*', delivered_to: ['sY']});
    ctx.addMsg('gpt', 'GPT', 'quiet row', '', '',
               '2020-01-01T10:01:00', null, null,
               {speaker: 'sZ', origin: 'seat', audience: ['sZ'], delivered_to: []});
    ctx.addMsg('claude', 'Claude', 'live only', '', '', '2020-01-01T10:02:00');
    p.lensBarShown = !byId['historyLensBar'].hidden;
    let opts = [...new Set(byId['historyLens'].options.map(o => o.value))];
    p.lensOptions = opts;
    const v0 = opts.find(v => v.startsWith('seat:'));
    if (v0) {
      const id0 = v0.slice(5);
      // a row seat0 really received keeps its divider alive under the lens
      ctx.addMsg('gemini', 'Gemini', 'to seat zero', '', '',
                 '2020-01-01T10:03:00', null, null,
                 {speaker: 'other', origin: 'seat', audience: [id0],
                  delivered_to: [id0]});
      byId['historyLens'].value = 'all';
      byId['historyLens'].onchange();
      p.allShowsEverything =
        msgs().every(m => !m.classList.contains('lens-hidden')) &&
        dividers().every(d => !d.classList.contains('lens-hidden'));
      byId['historyLens'].value = v0;
      byId['historyLens'].onchange();
      p.underLensVisible = msgs().map(m => !m.classList.contains('lens-hidden'));
      p.dividerUnderLens = dividers()
        .map(d => d.classList.contains('lens-hidden'));
      byId['historyLens'].value = 'all';
      byId['historyLens'].onchange();
      p.lensRestoresBack =
        msgs().every(m => !m.classList.contains('lens-hidden')) &&
        dividers().every(d => !d.classList.contains('lens-hidden'));
    }
  } catch (e) { more.tstructError = (e && e.stack) || String(e); }
  report({bootRan: fns.length > 0, bootError, more});
})();
"""


def boot(html_path, workdir):
    """Run the UI script headlessly; return the harness's JSON report."""
    with open(os.path.join(workdir, "dom.js"), "w", encoding="utf-8") as f:
        f.write(DOM_JS)
    with open(os.path.join(workdir, "boot.js"), "w", encoding="utf-8") as f:
        f.write(BOOT_JS)
    env = dict(os.environ, STUB_CONFIG=json.dumps(STUB_CONFIG))
    out = subprocess.run(
        [NODE, os.path.join(workdir, "boot.js"), html_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, stdin=subprocess.DEVNULL, env=env)
    if out.returncode != 0 or not out.stdout.strip():
        raise AssertionError(
            "harness failed (rc=%s)\nstdout: %s\nstderr: %s"
            % (out.returncode, out.stdout[-2000:], out.stderr[-2000:]))
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not installed")
class UiBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.report = boot(UI, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_script_survives_top_level(self):
        """One throw at top level takes the entire UI down with it."""
        self.assertIsNone(self.report["topLevelError"],
                          "ui/index.html threw before finishing: %s"
                          % self.report["topLevelError"])

    def test_pywebviewready_handler_is_registered_and_runs_clean(self):
        # The listener is registered at the very END of the script, so it is
        # the first casualty of any earlier throw.
        self.assertTrue(self.report["bootRan"],
                        "no pywebviewready listener was ever registered")
        self.assertIsNone(self.report["bootError"])

    def test_every_seat_gets_populated_model_and_thinking_menus(self):
        """The user-visible symptom: blank pickers."""
        seats = self.report["seats"]
        self.assertEqual([s["provider"] for s in seats],
                         ["claude", "gpt", "gemini"])
        want_models = {"claude": 2, "gpt": 1, "gemini": 1}
        want_levels = {"claude": 5, "gpt": 3, "gemini": 3}
        for s in seats:
            self.assertEqual(s["modelOptions"], want_models[s["provider"]],
                             "%s model menu is wrong/blank" % s["provider"])
            self.assertEqual(s["effortOptions"], want_levels[s["provider"]],
                             "%s thinking menu is wrong/blank" % s["provider"])
        self.assertEqual(
            [s["modelValue"] for s in seats],
            ["claude-opus-5", "gpt-5.6-sol", "gemini-3.7-flash"])

    def test_moderator_picker_is_filled_too(self):
        self.assertEqual(self.report["modModelOptions"],
                         len(STUB_CONFIG["claude_models"]))

    def test_the_moderator_toggle_actually_turns_moderation_on(self):
        """The promoted control's whole job, exercised through its handler."""
        self.assertIsNone(self.report.get("modToggleError"))
        # visible on the default Discuss-in-Turns room, without opening Advanced
        self.assertFalse(self.report.get("modToggleHiddenAtBoot"),
                         "the moderator choice is hidden on a default room")
        # off by default, and the picker stays out of the way until asked for
        self.assertTrue(self.report.get("modCtlHiddenBefore"))
        self.assertEqual(self.report.get("floorAfterToggle"), "moderated")
        self.assertFalse(self.report.get("modCtlHiddenAfter"),
                         "picker never appeared after switching moderation on")
        self.assertEqual(self.report.get("floorAfterUntoggle"), "cyclic")

    def test_both_provider_pickers_use_the_registry_label(self):
        """A count-only guard let the hand-written "Ox" label survive forever,
        so the gateway read as one model instead of seven."""
        self.assertIsNone(self.report.get("pickerError"))
        for key in ("seatProviders", "modProviders"):
            self.assertIn("ox=OpenCode", self.report.get(key) or [], key)
            self.assertNotIn("ox=Ox", self.report.get(key) or [], key)

    def test_every_opencode_model_can_run_the_room(self):
        models = self.report.get("oxModeratorModels") or []
        self.assertEqual(len(models), 3, models)          # the whole stub catalog
        self.assertIn("opencode/nemotron-3-ultra-free=Nemotron 3 Ultra", models)
        # and it carries THAT model's levels, not a shared list
        self.assertEqual(self.report.get("oxModeratorLevels"),
                         ["low", "high", "max"])

    def test_the_moderator_can_be_named(self):
        """The name box is real, defaults to the role, and reaches the payload."""
        self.assertIsNone(self.report.get("pickerError"))
        # placeholder IS the default, same contract as a seat's name box
        self.assertIn(self.report.get("modNamePlaceholder"),
                      ("Moderator", "Supervisor"))
        self.assertEqual(self.report.get("moderatorCfgName"), "Referee")
        # blank means "use the role's own word", not an empty name
        self.assertIsNone(self.report.get("unnamedCfgName"))

    def test_the_rounds_box_can_be_typed_into(self):
        """It is a real input now, and the clamp is visible, not silent."""
        self.assertIsNone(self.report.get("roundsError"))
        self.assertEqual(self.report.get("roundsTag"), "input")
        # a typed number sticks, and reaches the payload the engine reads
        self.assertEqual(self.report.get("roundsTyped"), "25")
        self.assertEqual(self.report.get("roundsTypedCfg"), 25)
        # out of range is CLAMPED AND REPAINTED - never silently corrected
        self.assertEqual(self.report.get("roundsClampedHigh"), "50")
        self.assertEqual(self.report.get("roundsClampedLow"), "1")
        # garbage keeps the last good value rather than emptying the box
        self.assertEqual(self.report.get("roundsGarbage"), "1")

    def test_the_same_box_edits_the_until_done_ceiling(self):
        """One control, two numbers: the checkbox re-labels and re-scopes it."""
        self.assertIsNone(self.report.get("roundsError"))
        self.assertIn("Safety ceiling", self.report.get("ceilingLabel") or "")
        # the ceiling has its OWN cap, so typing 4000 lands on 500, not 50
        self.assertEqual(self.report.get("ceilingClamped"), "500")
        # and unticking restores the rounds number, not the ceiling
        self.assertEqual(self.report.get("roundsAfterUntilDone"), "1")

    def test_keep_improving_is_gated_behind_its_warning(self):
        """The card opens the modal; only the acknowledgement arms the mode."""
        c = self.report.get("cont") or {}
        self.assertIsNone(self.report.get("contError"))
        self.assertTrue(c.get("hiddenAtBoot"))
        self.assertFalse(c.get("onAtBoot"))
        self.assertTrue(c.get("openedByCard"), "the card opens the warning")
        self.assertFalse(c.get("onBeforeOk"),
                         "selecting the card alone must NOT arm the mode")
        self.assertTrue(c.get("okDisabledBeforeAck"))
        self.assertTrue(c.get("okStillDisabled"))
        self.assertFalse(c.get("okAfterAck"), "ticking it unlocks OK")
        self.assertTrue(c.get("onAfterOk"), "and OK is what arms it")
        self.assertTrue(c.get("closedAfterOk"))
        self.assertEqual(c.get("preset"), "keep_improving")

    def test_the_warning_changes_when_nothing_can_stop_the_run(self):
        c = self.report.get("cont") or {}
        self.assertIn("keeps spending", c.get("ackDefault") or "")
        self.assertIn("nothing but the Stop button", c.get("ackNaked") or "")
        self.assertIn("Nothing will end this run", c.get("warnNaked") or "")

    def test_the_modal_clamps_and_produces_the_engine_payload(self):
        c = self.report.get("cont") or {}
        self.assertEqual(c.get("minutesClamped"), "5", "5 is the floor")
        cfg = c.get("cfg") or {}
        self.assertTrue(cfg.get("on"))
        self.assertEqual(cfg.get("checkin", {}).get("minutes"), 5)
        limits = cfg.get("limits") or {}
        # null is the ONLY way to say "no limit"; 0 would mean stop at once
        self.assertIsNone(limits.get("spend_usd"))
        self.assertIsNone(limits.get("hours"))
        self.assertFalse(limits.get("watchdog_may_stop"))

    def test_cancelling_the_warning_puts_the_cards_back(self):
        """Backing out is a refusal. It used to re-apply Keep Improving and
        re-open the modal, forever."""
        c = self.report.get("cont") or {}
        self.assertTrue(c.get("closedAfterCancel"))
        self.assertFalse(c.get("onAfterCancel"), "cancel never arms the mode")
        self.assertEqual(c.get("presetAfterCancel"), "open_discussion",
                         "back to where the user was, not keep_improving")

    def test_hidden_rows_in_the_warning_are_actually_hidden(self):
        """CSS-only, so the executing harness cannot see it: an explicit
        `display` beats the hidden attribute, and `.cont-check` sets one."""
        css = open(UI, encoding="utf-8").read()
        self.assertIn(".cont-check[hidden]", css)
        self.assertRegex(
            css, r"\.cont-check\[hidden\][^{]*\{[^}]*display:\s*none")

    def test_a_thinking_seat_shows_how_long_it_has_and_how_long_it_gets(self):
        t = self.report.get("think") or {}
        self.assertIsNone(self.report.get("thinkError"))
        self.assertTrue(t.get("rendered"), "no typing indicator at all")
        self.assertEqual(t.get("limitKept"), "900")
        # a fresh turn starts at zero, against its real watchdog
        self.assertEqual(t.get("clock"), "0:00 of 15:00")

    def test_a_replayed_turn_shows_its_true_age_not_a_fresh_zero(self):
        """Reopening a chat must not make a 14-minute-old turn look new."""
        t = self.report.get("think") or {}
        self.assertEqual(t.get("replayed"), "14:00 of 15:00")
        self.assertTrue(t.get("late"), "past 60% of the window it warns")

    def test_an_idle_watchdog_seat_is_not_shown_a_deadline_it_does_not_have(self):
        """The turn runs until the work is done, so the clock shows AGE only."""
        t = self.report.get("think") or {}
        self.assertEqual(t.get("idleClock"), "0:00")
        self.assertFalse(t.get("idleLate"))

    def test_going_quiet_surfaces_the_window_that_can_actually_end_the_turn(self):
        t = self.report.get("think") or {}
        self.assertEqual(t.get("quietClock"), "0:00 · quiet 3:20 of 5:00")
        self.assertTrue(t.get("quietLate"))

    def test_activity_resets_the_quiet_clock(self):
        """Same evidence the engine counts: a talking seat is not going quiet."""
        t = self.report.get("think") or {}
        self.assertEqual(t.get("afterActivity"), "0:00")

    def test_hiding_the_indicators_leaves_nothing_ticking(self):
        self.assertEqual((self.report.get("think") or {}).get("clearedAfterHide"), 0)

    def test_a_chat_killed_mid_run_is_reopened_and_resumed_at_boot(self):
        """The literal complaint: relaunch the app, the conversation is dead."""
        r = self.report.get("resume") or {}
        self.assertEqual(r.get("noted"), ["sess-one"],
                         "the attempt must be recorded, or a crash loop bills "
                         "itself forever")
        self.assertEqual(r.get("continued"),
                         [{"session_id": "sess-one", "opener": ""}],
                         "resumed with no opener — it picks up, it does not "
                         "put words in Josh's mouth")

    def test_the_resume_says_why_it_happened(self):
        r = self.report.get("resume") or {}
        self.assertIn("still running when the app closed",
                      r.get("reopenedText") or "")

    def test_switching_preset_turns_keep_improving_off(self):
        self.assertFalse((self.report.get("cont") or {}).get("onAfterSwitch"))

    def test_open_tabs_are_restored_with_their_titles_and_colours(self):
        """Tabs are the working set; the rail stays the full history."""
        self.assertIsNone(self.report.get("tabError"))
        tabs = self.report.get("tabsRestored") or []
        self.assertEqual([t["title"] for t in tabs],
                         ["Death Factory", "Second Chat"])
        # the colour is what makes one identifiable at a glance
        self.assertIn("E0607E", (tabs[0]["color"] or "").upper())
        self.assertFalse(self.report.get("tabStripHidden"))

    def test_closing_a_tab_removes_it_and_persists(self):
        after = self.report.get("tabsAfterClose") or []
        self.assertEqual([t["title"] for t in after], ["Death Factory"])
        self.assertTrue((self.report.get("tabSaves") or 0) > 0,
                        "closing a tab never reached save_tabs")
        ids = [r["id"] for r in (self.report.get("lastSavedIds") or [])]
        self.assertEqual(ids, ["sess-one"])

    def test_recolouring_a_tab_takes_effect(self):
        after = self.report.get("tabsAfterColor") or []
        self.assertTrue(after, "no tabs left to recolour")
        self.assertIn("2DD4BF", (after[0]["color"] or "").upper())

    def test_the_tab_strip_spans_the_whole_window(self):
        """A sibling of <main>, not a child of the conversation column.

        Nested inside the column it stops at the rails and reads as part of
        the transcript; across the top it reads as what it is — the app's
        open conversations (Josh, 2026-08-22).
        """
        with open(UI, encoding="utf-8") as f:
            src = f.read()
        strip = src.index('id="tabStrip"')
        self.assertLess(src.index("</header>"), strip)
        self.assertLess(strip, src.index("<main>"))
        # and nothing may re-nest it inside the feed column
        self.assertLess(strip, src.index('<div class="feed-wrap">'))

    def test_permission_note_is_painted_at_startup(self):
        self.assertTrue((self.report["permissionNote"] or "").strip())

    def test_mic_button_is_enabled_when_the_probe_says_available(self):
        # The button ships `hidden` and is only revealed by the boot path, so
        # a still-hidden mic means applyDictationConfig never ran.
        self.assertFalse(self.report["micHidden"], "mic button was never revealed")
        self.assertFalse(self.report["micDisabled"],
                         "mic disabled despite an available probe")
        self.assertIn("dictate", (self.report["micTitle"] or "").lower())

    def test_a_dictation_event_lands_text_in_the_composer(self):
        """The mic's whole job. Pure top-level script — text suites can't see it."""
        self.assertIsNone(self.report.get("dictationError"))
        # inserted at the caret (end, under the stub), space-joined, trimmed
        self.assertEqual(self.report.get("sayAfterDictation"), "draft hello there")

    def test_search_box_renders_engine_results_in_the_rail(self):
        """Typing in the rail's search box queries the engine and repaints
        the rail as hit rows with a header, snippet and count badge."""
        self.assertIsNone(self.report.get("searchError"),
                          "search probe threw: %s" % self.report.get("searchError"))
        self.assertTrue(self.report.get("searchBoxPresent"))
        self.assertTrue(self.report.get("searchClearHiddenAtBoot"))
        self.assertEqual(self.report.get("searchHeader"),
                         '1 chat matching \u201cnile\u201d')
        rows = self.report.get("searchRows") or []
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Second Chat")
        self.assertEqual(rows[0]["count"], "3 hits")
        self.assertIn("Nile", rows[0]["snippet"])
        self.assertIn("Alloy", rows[0]["proj"])
        self.assertTrue(self.report.get("searchClearShown"))

    def test_clicking_a_search_hit_opens_that_chat(self):
        self.assertGreater(self.report.get("searchOpenMsgCount") or 0, 0,
                           "clicking a hit did not open the chat")

    def test_tiny_queries_restore_the_normal_rail(self):
        """Below the two-character floor the groups come back — a one-letter
        query must not leave the rail stuck on an empty result page."""
        self.assertIsNone(self.report.get("searchError"))
        self.assertTrue(self.report.get("railRestoredBelowFloor"))
        self.assertTrue(self.report.get("clearHiddenBelowFloor"))

    def test_escape_clears_the_search_outright(self):
        self.assertEqual(self.report.get("valueAfterEscape"), "")
        self.assertTrue(self.report.get("railRestoredAfterEscape"))

    def test_the_shortcuts_cheat_sheet_exists_and_starts_hidden(self):
        """The ? overlay: present in the page, not shown until asked for."""
        self.assertIsNone(self.report.get("kbdError"),
                          "shortcuts probe threw: %s" % self.report.get("kbdError"))
        k = self.report.get("kbd") or {}
        self.assertTrue(k.get("present"), "#kbdModal missing from the markup")
        self.assertTrue(k.get("hiddenAtBoot"),
                        "the cheat sheet was visible before anyone pressed ?")

    def test_the_shortcuts_toggle_opens_and_closes_it(self):
        """toggleKbd is a real toggle, not an opener."""
        k = self.report.get("kbd") or {}
        self.assertTrue(k.get("openAfterToggle"))
        self.assertTrue(k.get("closedAfterSecondToggle"))

    def test_escape_closes_the_shortcuts_cheat_sheet(self):
        """Driven through the REAL document keydown handler, like every other
        Escape-closable surface in the app."""
        k = self.report.get("kbd") or {}
        self.assertTrue(k.get("closedByEscape"))

    def test_the_cheat_sheet_is_a_fixed_overlay_that_starts_display_none(self):
        """Regression for 'stuck on the bottom of the screen' (Josh,
        2026-08-23): #kbdModal was left out of the shared modal rule that
        gives every other overlay position:fixed + display:none, so it
        rendered as an always-visible block in the page flow. It must live
        in THAT rule, not only in the .show list."""
        with open(UI, encoding="utf-8") as f:
            css = f.read()
        rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
        hits = [body for sel, body in rules if "#kbdModal" in sel]
        base = [b for b in hits if "display" in b and ".show" not in b]
        self.assertTrue(
            any("display: none" in b.replace("  ", " ") and
                "position: fixed" in b for b in base),
            "#kbdModal must share the other modals' fixed/hidden base rule; "
            "rules mentioning it: %r" % (hits,))

    def test_a_new_tab_forgets_that_the_old_chat_was_running(self):
        """The "+" button leaves a stage with no chat on it, so the composer
        must be an OPENING composer — not an interject box pointed at a chat
        this stage no longer shows."""
        self.assertIsNone(self.report.get("newTabError"))
        p = self.report["newTab"]
        self.assertTrue(p["stopShownWhileRunning"],
                        "the probe never got the UI into a running state")
        self.assertIn("opening", p["placeholderAfterNewTab"])
        self.assertTrue(p["stopHiddenAfterNewTab"],
                        "Stop still offered for a chat this tab does not show")

    def test_the_first_message_in_a_fresh_tab_starts_a_conversation(self):
        """Regression, reported 2026-08-23: every message typed into a fresh
        tab was silently swallowed and answered "No conversation is running.",
        because resetStage() cleared canContinue but not `running`."""
        p = self.report["newTab"]
        self.assertIn("start", p["calls"],
                      "a fresh tab's first message did not start a chat: %s"
                      % (p["calls"],))
        self.assertNotIn("interject", p["calls"],
                         "the message was interjected into a chat this tab "
                         "walked away from")
        self.assertEqual(p["relayRows"], [],
                         "the send was refused by the bridge: %s" % (p["relayRows"],))
        self.assertEqual(p["sayAfterSend"], "",
                         "the composer kept the text, so nothing was sent")

    def test_day_dividers_open_each_calendar_day_and_label_it(self):
        """A new calendar day in the transcript gets one labelled rule; the
        labels are relative (Today/Yesterday) or dated, never a raw ISO."""
        self.assertIsNone(self.report.get("tstructError"),
                          "transcript-structure probe threw: %s"
                          % self.report.get("tstructError"))
        t = self.report.get("tstruct") or {}
        self.assertEqual(t.get("dayOneLabel"), "Jan 1, 2020")
        self.assertEqual(t.get("dayTwoLabel"), "Jan 2, 2020")
        self.assertEqual(t.get("dividersAfterDayOne"), 1,
                         "same-day rows must not stack dividers")
        self.assertTrue(t.get("lensBarShown"),
                        "delivery-bearing rows never revealed the lens bar")

    def test_consecutive_speaker_grouping_breaks_on_speaker_and_day(self):
        """Two rows from one speaker read as a run; any other speaker, Josh
        included, and any day break ends it."""
        t = self.report.get("tstruct") or {}
        self.assertTrue(t.get("firstNotGrouped"))
        self.assertTrue(t.get("secondGrouped"))
        self.assertTrue(t.get("joshBreaksRun"))
        self.assertTrue(t.get("dayBreakEndsRun"))
        self.assertTrue(t.get("todayRunGroups"))

    def test_transcript_structure_resets_on_reopen(self):
        """openChat replays into a stage whose counters restarted: the replayed
        josh row must NOT group onto the draft's last josh row, and its row —
        same calendar day as the draft's tail — must still open a divider."""
        t = self.report.get("tstruct") or {}
        self.assertTrue(t.get("replayFirstIsJosh"))
        self.assertFalse(t.get("replayJoshGroupedWithDraft"),
                         "lastSpeakerKey leaked across openChat")
        self.assertEqual(t.get("replayDividers"), 1,
                         "lastDayKey leaked across openChat")
        # resetStage: the empty stage restarts them too
        self.assertEqual(t.get("afterResetDividers"), 1,
                         "lastDayKey leaked across resetStage")

    def test_history_lens_hides_dividers_that_lost_every_message(self):
        """Under 'What X saw', a divider survives only while some visible
        message sits under it; back to All, everything comes back."""
        t = self.report.get("tstruct") or {}
        self.assertIn("all", t.get("lensOptions") or [])
        self.assertTrue(any(str(v).startswith("seat:")
                            for v in t.get("lensOptions") or []),
                        "no per-seat lens options were built")
        self.assertTrue(t.get("allShowsEverything"))
        vis = t.get("underLensVisible")
        # five rows: the resetStage probe's josh row (live-only), the three
        # seeded rows, then seat 0's delivered one
        self.assertEqual(vis, [False, False, False, False, True],
                         "seat 0's lens shows only what seat 0 really got: %r"
                         % (vis,))
        self.assertEqual(t.get("dividerUnderLens"), [True, False],
                         "the day rule over hidden rows hides; the one still "
                         "holding a visible message stays")
        self.assertTrue(t.get("lensRestoresBack"))

    def test_a_refused_interjection_hands_the_words_back(self):
        """sendSay clears the box before the bridge answers, so a refusal used
        to cost Josh the message as well as the turn."""
        p = self.report["newTab"]
        self.assertEqual(p["sayAfterRefusal"], "words worth keeping")

    def test_harness_still_catches_the_original_regression(self):
        """RED guard: put the 2026-08-21 TDZ call back and demand a failure.

        Without this, a harness that quietly stopped executing the script would
        keep reporting success forever.
        """
        with open(UI, encoding="utf-8") as f:
            src = f.read()
        anchor = '$("permissionMode").onchange = syncPermissionNote;'
        self.assertIn(anchor, src)
        broken = src.replace(anchor, anchor + "\nsyncPermissionNote();", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "buggy.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(broken)
            report = boot(path, tmp)
        self.assertIn("activeId", report["topLevelError"] or "")
        self.assertEqual(report["seats"], [])
        self.assertFalse(report["bootRan"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
