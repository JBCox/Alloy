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

// ONE compound (no combinators). `matches` below layers the comma list
// and the descendant combinator on top.
function matchesCompound(el, p) {
    if (!p) return false;
    // COMPOUND class selectors (".react-btn.on") matched for real. Treating
    // the whole tail as one class name means such a selector NEVER matches,
    // and the UI then takes its own no-match branch -- which is how a probe
    // watched addMsg fall back to a default verdict and reported it as the
    // product's behaviour (2026-08-27).
    if (p.startsWith('.')) {
      // ".class[attr]" and ".class[attr=value]" matched for real. Dropping
      // the bracket made the whole selector never match, so a renderer that
      // finds its own card with `.plan-card[data-active-board]` built a NEW
      // one on every repaint and the harness watched cards accumulate --
      // the same shape as the attribute selectors on tag names below
      // (2026-08-27).
      const at = p.match(/\[([\w-]+)(?:\s*=\s*"?([^\]"]*)"?)?\]\s*$/);
      const classes = (at ? p.slice(0, at.index) : p).slice(1).split('.');
      if (classes.every(
            c => c && (' ' + el.className + ' ').includes(' ' + c + ' '))) {
        if (!at) return true;
        const have = el.getAttribute(at[1]);
        if (have !== undefined && have !== null
            && (at[2] === undefined || String(have) === at[2])) return true;
      }
    }
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
        if (have === undefined || have === null) return false;
        if (attr[2] !== undefined && String(have) !== attr[2]) return false;
      }
      if (/:checked\b/.test(p) && !el.checked) return false;
      if (/:disabled\b/.test(p) && !el.disabled) return false;
      const cls = p.match(/\.([\w-]+)/);
      if (!cls) return true;
      if ((' ' + el.className + ' ').includes(' ' + cls[1] + ' ')) return true;
  }
  return false;
}

// Comma lists AND the descendant combinator. The combinator used to be
// ignored outright: "#schedDays .sched-day" hit the `#` branch and
// compared el.id to the WHOLE string, so it matched nothing, silently --
// and the UI's own code then looked as though it found no checkboxes.
// ui/index.html has been using that form for a while ("#feed .msg",
// "#battleBar .vote-btn"), so this was three selectors returning [] in
// the harness while working perfectly in a browser: the same family as
// the no-op classList and the permissive Proxy (2026-08-27).
function matches(el, sel) {
  for (const part of String(sel).split(',')) {
    const compounds = part.trim().split(/\s+/).filter(Boolean);
    if (!compounds.length) continue;
    if (!matchesCompound(el, compounds.pop())) continue;
    let node = el.parentElement, ok = true;
    while (compounds.length) {
      const want = compounds.pop();
      while (node && !matchesCompound(node, want)) node = node.parentElement;
      if (!node) { ok = false; break; }
      node = node.parentElement;
    }
    if (ok) return true;
  }
  return false;
}

let byId = {};

function mkEl(tag) {
  const el = {
    tag, id: '', className: '', children: [], parent: null,
    _attrs: {}, _html: '', textContent: '', value: '', checked: false,
    hidden: false, disabled: false, selected: false, options: [],
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
    // RECORDED, not dropped. A no-op here makes every keyboard binding in
    // the page structurally untestable: the composer's Enter / Shift+Enter /
    // Ctrl+Enter split lives entirely in a keydown listener, so a probe could
    // only ever call the handler function directly -- which passes whether
    // the key is bound or not (caught by a RED pass, 2026-08-27).
    _listeners: null,
    addEventListener(type, fn) {
      if (typeof fn !== 'function') return;
      (this._listeners = this._listeners || {});
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      const fns = (this._listeners || {})[type] || [];
      const i = fns.indexOf(fn);
      if (i > -1) fns.splice(i, 1);
    },
    dispatchEvent(ev) {
      const type = (ev && ev.type) || '';
      ((this._listeners || {})[type] || []).slice().forEach(fn => fn(ev));
      return true;
    },
    focus() {}, blur() {}, select() {}, click() {}, scrollIntoView() {},
    getBoundingClientRect() { return {top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0}; },
    setPointerCapture() {}, releasePointerCapture() {},
    remove() {
      if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this);
    },
    appendChild(c) {
      // A real DOM MOVES an already-parented node; a stub that only pushed
      // duplicated it, which is exactly the shape the UI relies on when it
      // re-appends live indicators below a new message (typingEls/workingEls).
      // The duplicate then made one remove() look like it deleted two rows.
      if (c.parent) c.parent.children = c.parent.children.filter(x => x !== c);
      c.parent = this;
      this.children.push(c);
      if (this.tag === 'select' && c.tag === 'option') {
        this.options.push(c);
        if (c.selected || this.options.length === 1) this.value = c.value;
      }
      return c;
    },
    append(...cs) { cs.forEach(c => typeof c === 'object' && this.appendChild(c)); },
    // REAL, not the permissive Proxy's no-op. Every renderer in the UI that
    // rebuilds a list uses replaceChildren(), and a silent no-op meant the
    // old rows stayed: a probe then watched a list "grow" on every repaint
    // and read that as the product's behaviour. Same lesson as classList and
    // the attribute selectors -- a stub that lies is worse than one that
    // refuses (2026-08-27).
    replaceChildren(...cs) {
      this.children.forEach(c => { if (c.parent === this) c.parent = null; });
      this.children = [];
      this._html = '';
      if (this.tag === 'select') { this.options = []; this.value = ''; }
      cs.forEach(c => typeof c === 'object' && this.appendChild(c));
    },
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
  // A REFLECTING dataset, both ways, like a real one. The plain object it
  // used to be meant `el.dataset.activeBoard = "1"` set no attribute at all,
  // so `querySelector('.plan-card[data-active-board]')` could never find the
  // card a renderer had just built -- and the renderer then built a NEW one
  // on every repaint while the harness watched them pile up. The other
  // direction matters too: `data-` attributes written in index.html's markup
  // have to be readable as dataset properties.
  const dataKey = k => 'data-' + String(k).replace(/[A-Z]/g,
                                                  c => '-' + c.toLowerCase());
  el._dataset = new Proxy({}, {
    get(t, k) {
      if (typeof k !== 'string') return undefined;
      return k in t ? t[k] : el._attrs[dataKey(k)];
    },
    set(t, k, v) { t[k] = v; el._attrs[dataKey(k)] = String(v); return true; },
    deleteProperty(t, k) {
      delete t[k]; delete el._attrs[dataKey(k)]; return true;
    },
    has(t, k) { return k in t || dataKey(k) in el._attrs; },
  });
  Object.defineProperty(el, 'dataset', {get() { return el._dataset; }});
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) {
      this._html = String(v);
      this.children = [];
      if (this.tag === 'select') { this.options = []; this.value = ''; }
      parseFragment(this._html, mkEl).forEach(c => this.appendChild(c));
    },
  });
  // `parentElement` is what the page reaches for; the stub only had `parent`,
  // and the permissive Proxy answered the standard name with a no-op FUNCTION
  // — so `x.parentElement.children` was `undefined`, not an error anyone could
  // read.
  Object.defineProperty(el, 'parentElement', {get() { return this.parent || null; }});
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
  // byId holds the STATIC markup. Everything the script builds gets its id
  // by plain assignment (`d.id = "seat-" + seat.id`), which registers
  // nowhere — so a lookup-only stub could never find a seat card, and code
  // doing $('seat-' + n) looked broken to the harness while working
  // perfectly in a browser. That is the direction that makes a stub worse
  // than useless, so this searches the document the way the real one does
  // (2026-08-27; W1.4's seat readout is the first test to need it).
  getElementById: id => byId[id]
    || everyEl().find(e => e.id === id) || null,
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
// The FACTORY boot is one Claude seat now (Alloy as a solo harness); every
// probe below was written against the historical three-seat roster, so seed
// the saved default stage the product reads at boot -- which also drives the
// default-stage restore path for real instead of leaving it untested.
if (process.env.ALLOY_SAVED_STAGE) {
  store['defaultStage'] = process.env.ALLOY_SAVED_STAGE;
} else if (!process.env.ALLOY_FACTORY_BOOT) {
  store['defaultStage'] = JSON.stringify([
    {provider: 'claude', model: '', effort: ''},
    {provider: 'gpt', model: '', effort: ''},
    {provider: 'gemini', model: '', effort: ''},
  ]);
}

const api = new Proxy({}, {
  get(_, name) {
    if (name === 'then') return undefined;          // must not look thenable
    return (...args) => Promise.resolve(apiReply(String(name), args));
  },
});

const savedTabs = [], autoResumeNoted = [], continued = [], apiCalls = [];
const playbookCalls = [];    // W1.5: what set_playbook_rule was asked to do
const memCalls = [];         // Wave 3: what the memory bridge was asked to do
// what get_memory / save_memory / forget_memory hand back; the probe swaps it
let memReply = {scope: 'global', label: '', global_scope: 'global',
                truncated: false, error: null, entries: []};
const reactCalls = [];       // W1.7: what react_message was asked to do
let reactionsReply = {};     // what get_reactions hands a reopened chat
// undefined = leave the real inline editor alone; anything else is what
// openNoteEditor resolves to (null = cancelled)
let noteEditorAnswer = undefined;
let playbookReply = null;    // set to {error} to drive the refusal path
let interjectReply = null;   // set to {error} to drive the refusal path
const dockCalls = [];        // W2.1: what the queue dock asked the bridge to do
let prepareReply = null;     // what prepare_message hands back; null = an echo
// set to a chat id to have `interject` move the focus WHILE the dock awaits
// it — the real race sendQueued's pinning exists for
let interjectSwitchesChatTo = null;
let openSessionExtra = {};   // merged into the reopened chat's session summary
let jobsReply = {jobs: [], now: 1000};   // W2.3: what Api.jobs() hands back
const boardCalls = [];       // W2.2: what approve_board was asked to do
const schedCalls = [];       // W4: what the schedule bridge was asked to do
let schedReply = {ok: true, rooms: [], schedules: []};
let riskReply = {ok: true, grants: [], sentences: [], notes: []};
const hookSaves = [];        // what set_event_hooks was handed
// null = the bridge's real event list; the probe swaps in one carrying an
// event hookLabels does NOT know, which is the whole point of the check
let hooksReply = null;
function apiReply(name, args) {
  apiCalls.push(name);
  switch (name) {
    case 'get_config': return JSON.parse(process.env.STUB_CONFIG);
    case 'get_stats': return {ok: true};
    case 'react_message':
      reactCalls.push([args[0], args[1], args[3] === undefined ? null : args[3]]);
      return {ok: true};
    case 'get_reactions': return reactionsReply;
    case 'get_playbook': return {ok: true};
    case 'get_memory': memCalls.push([name, args[0]]); return memReply;
    case 'save_memory':
    case 'forget_memory': memCalls.push([name].concat(args)); return memReply;
    case 'set_playbook_rule':
      playbookCalls.push(args);
      return playbookReply || {ok: true, rules: []};
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
      // a spawned-team pair exercises the spawn-lineage tree (t6): Alloy
      // persists provenance as meta.parent on every child row, and the UI
      // derives the whole tree client-side from those hints.
      {id: 'sess-team-parent', title: 'Team Parent', project: '',
       participants: [{id: '0', provider: 'claude', name: 'Claude'}], run: {}},
      {id: 'sess-team-child', title: 'Child Squad', project: '',
       participants: [{id: '0', provider: 'claude', name: 'Claude'}], run: {},
       parent: {id: 'sess-team-parent', label: 'Claude'}},
    ];
    case 'get_tabs': return {
      open: [{id: 'sess-one', color: 'rose'}, {id: 'sess-two', color: ''}],
      active: 'sess-one',
    };
    case 'save_tabs': savedTabs.push(args && args[0]); return args && args[0];
    case 'interject':
      dockCalls.push([name].concat(args));
      if (interjectSwitchesChatTo) {
        vm.runInContext('activeId = ' + JSON.stringify(interjectSwitchesChatTo),
                        ctx);
      }
      return interjectReply || {ok: true, text: args && args[0]};
    case 'prepare_message':
      dockCalls.push([name].concat(args));
      return prepareReply || {ok: true, text: args && args[0], attached: 0};
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
                  meta: '', ts: '2026-08-23T08:00:00',
                  message_id: 'stub0'},
                 // a SEAT row too: replay paths that only exist on seat rows
                 // (reactions, notes, thumbs) have nothing to land on
                 // otherwise, and a probe would pass by not running at all
                 // delivered_to is what gives a row its dataset.messageId,
                 // and every per-row control hangs off that
                 {speaker: 0, provider: 'claude', name: 'Claude',
                  text: 'an earlier reply', meta: 'round 1', delivered_to: [],
                  ts: '2026-08-23T08:01:00', message_id: 'stub1'}],
      session: Object.assign(
        {id: args && args[0], title: 'Death Factory', participants: [],
         can_continue: true, interrupted: true, mode: 'round_robin',
         workspace: '', project: '', transcript: '', completion:
         {lifecycle: 'active', goal_verdict: 'unknown'}},
        openSessionExtra),
    };
    case 'jobs': return jobsReply;
    case 'get_schedules': return schedReply;
    case 'room_risk': return riskReply;
    case 'save_schedule':
    case 'delete_schedule':
    case 'set_schedule_enabled':
    case 'run_schedule_now':
      schedCalls.push([name].concat(args)); return {ok: true, started: true};
    case 'get_event_hooks': return hooksReply || {
      ok: true, events: ['question', 'checkin', 'done', 'gate_red',
                         'scheduled'], hooks: {}};
    case 'set_event_hooks': hookSaves.push(args && args[0]); return {ok: true};
    case 'approve_board': boardCalls.push(args.slice()); return {ok: true};
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
  // The roster the BOOT itself produced -- captured before any probe touches
  // the stage, because the solo probe tears seats down and a restore at the
  // end would mask what boot actually built (the default-stage tests read
  // this, not report()'s end-of-run snapshot).
  more.bootSeats = document.querySelectorAll('.seat').map(s => ({
    provider: (s.dataset && s.dataset.provider) || '',
    model: (s.querySelector('.model') || {}).value || null,
    effort: (s.querySelector('.effort') || {}).value || null,
  }));
  // Boot now REOPENS and RESUMES an interrupted chat, so the evidence must be
  // read before anything else touches the stage — and the remaining probes
  // need the unseated draft they have always assumed, hence newChat().
  more.resume = {
    noted: autoResumeNoted.slice(),
    continued: continued.map(c => ({session_id: c && c.session_id,
                                    opener: c && c.opener})),
    // the stub does NOT aggregate a parent's textContent from its children,
    // so gather it by hand rather than reading an empty string
    // NOT a short slice: this is a haystack for several assertions, and a
    // stub row added for an unrelated probe pushed the interesting text off
    // the end of a 600-char cut (2026-08-27).
    reopenedText: [...byId['feed'].children].map(deepText).join(' | ').slice(0, 2500),
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
    // the composer MODE PILL replaced the rail's card grid: boot paints the
    // current mode, opening the pill lists all five modes once each, and
    // picking Keep Improving from the popover must open the warning, not
    // silently arm the mode
    more.cont.pillLabelAtBoot = String(byId['modePickLabel'].textContent);
    byId['modePickBtn'].onclick({stopPropagation() {}});
    more.cont.menuOpenedByPill = !byId['modePickMenu'].hidden;
    const modeRows = byId['modeOptList'].children.slice();
    more.cont.menuRows = modeRows.length;
    more.cont.menuNames = modeRows.map(r => {
      const b = r.querySelector('b');
      return b ? b.textContent : null;
    });
    more.cont.menuSelectedCount =
      modeRows.filter(r => r.getAttribute('aria-selected') === 'true').length;
    const kiRow = modeRows.find(r =>
      r.getAttribute('data-mode') === 'keep_improving');
    kiRow.onclick();
    more.cont.pillAfterKeepPick = String(byId['modePickLabel'].textContent);
    more.cont.menuClosedAfterPick = !!byId['modePickMenu'].hidden;
    more.cont.openedByPill = (byId['contModal'].className || '').includes('show');
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
    // Cancel must put the mode back where it was, not re-open the warning
    // forever (the previous preset used to be read AFTER the selection had
    // already moved to keep_improving).
    ctx.closeContinuous();
    more.cont.presetAfterCancel = byId['presetSel'].value;
    more.cont.pillAfterCancel = String(byId['modePickLabel'].textContent);
    more.cont.closedAfterCancel = !(byId['contModal'].className || '').includes('show');
    more.cont.onAfterCancel = ctx.continuousCfg() !== null;
    // and it re-opens cleanly, through the pill again
    byId['modePickBtn'].onclick({stopPropagation() {}});
    byId['modeOptList'].children
      .find(r => r.getAttribute('data-mode') === 'keep_improving').onclick();
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

  // ---- desktop control: the acknowledgement, and the revert on refusal ----
  try {
    more.desk = {};
    const sel = byId['desktopMode'];
    more.desk.bootValue = String(sel.value);
    // a harmless rung needs no ceremony
    sel.value = 'ask'; sel.onchange();
    more.desk.askOpensNothing =
      !(byId['deskModal'].className || '').includes('show');
    more.desk.askNote = String(byId['desktopNote'].textContent || '');
    more.desk.appsHiddenForAsk = !!byId['desktopApps'].hidden;
    sel.value = 'allowlist'; sel.onchange();
    more.desk.appsShownForAllowlist = !byId['desktopApps'].hidden;
    // ...but the unattended rung must stop and ask
    sel.value = 'full'; sel.onchange();
    more.desk.fullOpensModal =
      (byId['deskModal'].className || '').includes('show');
    more.desk.okDisabledBeforeAck = !!byId['deskOk'].disabled;
    // cancelling is a REFUSAL: the picker goes back, it does not stay on full
    byId['deskCancel'].onclick();
    more.desk.valueAfterCancel = String(sel.value);
    more.desk.closedAfterCancel =
      !(byId['deskModal'].className || '').includes('show');
    // ticking the box is the only thing that unlocks OK
    sel.value = 'full'; sel.onchange();
    byId['deskAck'].checked = true; byId['deskAck'].onchange();
    more.desk.okAfterAck = !!byId['deskOk'].disabled;
    byId['deskOk'].onclick();
    more.desk.valueAfterOk = String(sel.value);
    more.desk.closedAfterOk =
      !(byId['deskModal'].className || '').includes('show');
    more.desk.fullNote = String(byId['desktopNote'].textContent || '');
    // the payload the engine actually receives
    byId['desktopAppList'].value = ' Notepad$ , , calc\\.exe ';
    more.desk.apps = ctx.desktopAppList();
    // a reopened chat shows what it RAN with, and an unknown value is off
    ctx.restoreDesktop('allowlist', ['Notepad$']);
    more.desk.restored = String(sel.value);
    more.desk.restoredApps = String(byId['desktopAppList'].value);
    ctx.restoreDesktop(undefined, undefined);
    more.desk.restoredLegacy = String(sel.value);
    ctx.restoreDesktop('sudo-everything', []);
    more.desk.restoredJunk = String(sel.value);
  } catch (e) { more.deskError = (e && e.stack) || String(e); }

  // ---- browser control: the same ceremony, its own state ------------------
  try {
    more.brws = {};
    const bsel = byId['browserMode'];
    more.brws.bootValue = String(bsel.value);
    more.brws.sitesHiddenWhenOff = !!byId['browserSites'].hidden;
    // the site list is offered at EVERY live rung, not only one: with no
    // sites Chrome reaches nothing, so it is the difference between a
    // working capability and a dead one at read, ask and full alike
    bsel.value = 'read'; bsel.onchange();
    more.brws.readOpensNothing =
      !(byId['brwsModal'].className || '').includes('show');
    more.brws.readNote = String(byId['browserNote'].textContent || '');
    more.brws.sitesShownForRead = !byId['browserSites'].hidden;
    bsel.value = 'ask'; bsel.onchange();
    more.brws.sitesShownForAsk = !byId['browserSites'].hidden;
    // ...and the unattended rung must stop and ask
    // whatever the desktop picker happens to hold, cancelling the BROWSER
    // modal must leave it there: separate prev-values, or one Cancel moves
    // two controls
    more.brws.desktopBefore = String(byId['desktopMode'].value);
    bsel.value = 'full'; bsel.onchange();
    more.brws.fullOpensModal =
      (byId['brwsModal'].className || '').includes('show');
    more.brws.okDisabledBeforeAck = !!byId['brwsOk'].disabled;
    byId['brwsCancel'].onclick();
    more.brws.valueAfterCancel = String(bsel.value);
    more.brws.closedAfterCancel =
      !(byId['brwsModal'].className || '').includes('show');
    more.brws.desktopAfter = String(byId['desktopMode'].value);
    bsel.value = 'full'; bsel.onchange();
    byId['brwsAck'].checked = true; byId['brwsAck'].onchange();
    more.brws.okAfterAck = !!byId['brwsOk'].disabled;
    byId['brwsOk'].onclick();
    more.brws.valueAfterOk = String(bsel.value);
    more.brws.fullNote = String(byId['browserNote'].textContent || '');
    // the payload the engine actually receives
    byId['browserSiteList'].value = ' https://a.test/* , , https://b.test/* ';
    more.brws.sites = ctx.browserSiteList();
    // a reopened chat shows what it RAN with; anything unknown is off
    ctx.restoreBrowser('read', ['https://a.test/*']);
    more.brws.restored = String(bsel.value);
    more.brws.restoredSites = String(byId['browserSiteList'].value);
    ctx.restoreBrowser(undefined, undefined);
    more.brws.restoredLegacy = String(bsel.value);
    ctx.restoreBrowser('browse-anywhere', []);
    more.brws.restoredJunk = String(bsel.value);
  } catch (e) { more.brwsError = (e && e.stack) || String(e); }

  // ---- the advisory ceiling: shown only where it is TRUE ------------------
  try {
    more.adv = {};
    const psel = byId['permissionMode'], bsel = byId['browserMode'];
    bsel.value = 'ask'; bsel.onchange();
    psel.value = 'ask'; ctx.syncPermissionNote();
    more.adv.hiddenWhenSupervised = !!byId['rungAdvisory'].hidden;
    psel.value = 'full'; ctx.syncPermissionNote();
    more.adv.shownAtFull = !byId['rungAdvisory'].hidden;
    more.adv.textAtFull = String(byId['rungAdvisory'].textContent || '');
    // The other end of the same honesty problem: at Read only the axes are
    // not weak, they are INERT -- claude refuses every MCP tool in plan mode,
    // so the engine registers nothing and the picker would otherwise keep
    // showing a rung that was never handed to anybody.
    psel.value = 'read_only'; ctx.syncPermissionNote();
    more.adv.shownAtReadOnly = !byId['rungAdvisory'].hidden;
    more.adv.textAtReadOnly = String(byId['rungAdvisory'].textContent || '');
    // and it disappears again when neither axis is on
    bsel.value = 'off'; bsel.onchange();
    byId['desktopMode'].value = 'off'; byId['desktopMode'].onchange();
    more.adv.hiddenWhenBothOff = !!byId['rungAdvisory'].hidden;
  } catch (e) { more.advError = (e && e.stack) || String(e); }

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
  // ---- memory: the modal, driven like a user -----------------------------
  // Wave 3. Three things only an executed page can see: that the row's OWN
  // scope (not the chat's) is what a Forget sends, that the first click only
  // ARMS, and that a note's text reaches the DOM as text rather than markup.
  try {
    const p = more.mem = {};
    const modal = byId['memModal'];
    p.present = !!modal;
    p.hiddenAtBoot = modal ? !modal.classList.contains('show') : null;
    memReply = {
      scope: 'proj-1234abcd', label: 'ai-chat', global_scope: 'global',
      truncated: false, error: null,
      entries: [
        {id: 'm1', kind: 'josh', who: 'Josh', when: '2026-08-27',
         scope: 'proj-1234abcd', text: 'The gate is <b>run_all</b>.'},
        {id: 'm2', kind: 'seat', who: null, when: null,
         scope: 'global', text: 'a global note'},
      ],
    };
    memCalls.length = 0;
    await ctx.openMemory();
    p.openCall = memCalls.slice();
    p.shown = modal.classList.contains('show');
    const rows = () => [...byId['memList'].children]
      .filter(c => String(c.className || '').split(/\s+/).includes('mem-row'));
    p.rowCount = rows().length;
    p.firstText = deepText(rows()[0]);
    // a note's text is TEXT: the store is hand-editable, so its content is
    // arbitrary and must never reach the page as markup. Read off the TEXT
    // NODE, not the row -- the row's _html is empty either way, so a probe
    // pointed there passes whether textContent or innerHTML was used.
    const cell = c => [...c.children].concat(
      [...c.children].flatMap(k => [...(k.children || [])]))
      .filter(k => String(k.className || '').split(/\s+/).includes('mem-text'))[0];
    p.firstTextProp = cell(rows()[0]).textContent || '';
    p.firstHtmlProp = cell(rows()[0])._html || '';
    p.scopeLine = deepText(byId['memScope']);
    // an unattributed seat note says who it was, never borrows the row above
    p.secondText = deepText(rows()[1]);
    // the "everywhere" tag only appears inside a PROJECT chat's list
    p.taggedRows = rows().filter(r => deepText(r).includes('everywhere')).length;
    p.everywhereOffered = !byId['memEverywhereLbl'].hidden;

    // Forget: first click ARMS, second sends -- and it sends the ROW's scope
    const del = rows()[1].querySelector('button');
    memCalls.length = 0;
    del.onclick();
    p.armedLabel = del.textContent;
    p.callsAfterArm = memCalls.slice();
    await del.onclick();
    p.callsAfterConfirm = memCalls.slice();

    // Adding: an empty box refuses without calling the bridge
    memCalls.length = 0;
    byId['memText'].value = '   ';
    await ctx.memAdd();
    p.emptyCalls = memCalls.slice();
    p.emptyNote = deepText(byId['memNote']);
    // a real note goes with the checkbox's value and the active chat id
    byId['memText'].value = 'remember this';
    byId['memEverywhere'].checked = true;
    memReply = Object.assign({}, memReply, {note: 'trimmed to 1000 characters'});
    await ctx.memAdd();
    p.addCalls = memCalls.slice();
    p.textCleared = byId['memText'].value === '';
    // a trim or an eviction is STATED, never silently applied
    p.addNote = deepText(byId['memNote']);

    // a global-scope chat does not offer the checkbox and tags nothing
    memReply = {scope: 'global', label: '', global_scope: 'global',
                truncated: true, error: null,
                entries: [{id: 'm3', kind: 'josh', who: 'Josh',
                           when: '2026-08-27', scope: 'global', text: 'g'}]};
    await ctx.openMemory();
    p.globalOffersEverywhere = !byId['memEverywhereLbl'].hidden;
    p.globalTagged = rows().filter(r => deepText(r).includes('everywhere')).length;
    p.globalScopeLine = deepText(byId['memScope']);

    // an error is shown instead of an empty list pretending nothing is stored
    memReply = {error: 'Memory could not be read: disk on fire'};
    await ctx.openMemory();
    p.errorNote = deepText(byId['memNote']);

    ctx.closeMemory();
    p.closed = !modal.classList.contains('show');
    // the two directives render as chips like every other trailing token --
    // md() peels them, so with no label they would read "remember"/"recall"
    p.chips = ['[[REMEMBER: the gate is run_all]]', '[[RECALL: gate]]']
      .map(d => ctx.md('body text ' + d));
  } catch (e) { more.memError = (e && e.stack) || String(e); }
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
  // ---- the live budget bar, driven through its REAL event path ----------
  // Engine `usage` events must light the strip with REPORTED truth only,
  // blank-reporting seats must land in an explicit "not reported" tooltip
  // group, zero burn must produce no projection, and a fresh stage clears it.
  try {
    const p = more.budget = {};
    const strip = byId['budgetStrip'];
    p.present = !!strip;
    p.hiddenAtBoot = !(strip.className || '').includes('show');
    // pure math first — edge cases need no DOM at all
    const noon = new Date(2026, 7, 25, 9, 0).getTime();   // local 09:00
    p.noCap = JSON.stringify(ctx.projectCapHit(0.42, 0, null, 18, noon));
    p.zeroBurn = JSON.stringify(ctx.projectCapHit(0, 0, 2, 18, noon));
    p.noTime = JSON.stringify(ctx.projectCapHit(0.42, 0, 2, 0, noon));
    p.overCap = JSON.stringify(ctx.projectCapHit(2.5, 0, 2, 10, noon));
    // $0.42 in 18 min ⇒ ~1.58 left at .0233/min ⇒ ~68 min ⇒ ~10:07
    p.proj = ctx.projectCapHit(0.42, 0, 2, 18, noon);
    p.textWithCap = String(ctx.budgetStripText({spend: 0.42, cap: 2,
      anchor: 0, elapsedMin: 18, nowMs: noon}));
    p.textNoCap = String(ctx.budgetStripText({spend: 0.05, cap: null,
      anchor: 0, elapsedMin: null, nowMs: noon}));
    p.textZeroBurn = String(ctx.budgetStripText({spend: 0, cap: 2,
      anchor: 0, elapsedMin: 30, nowMs: noon}));
    p.textOver = String(ctx.budgetStripText({spend: 2.5, cap: 2,
      anchor: 0, elapsedMin: 30, nowMs: noon}));
    p.textNothing = String(ctx.budgetStripText({spend: null, cap: null,
      anchor: 0, elapsedMin: null, nowMs: noon}));
    // live path: one seat reports, the other two stay honestly blank
    ctx.uiEvent({event: 'usage', payload: {total_cost_usd: 0.42,
      input_tokens: 100, output_tokens: 50, total_tokens: 150,
      by_seat: {'0': {cost_usd: 0.42, total_tokens: 150}}}});
    p.shownAfterUsage = (strip.className || '').includes('show');
    p.textAfterUsage = String(strip.textContent);
    p.tipAfterUsage = String(strip.title || '');
    // the payload carries ADDITIVE totals, so the second event replaces
    ctx.uiEvent({event: 'usage', payload: {total_cost_usd: 0.9,
      input_tokens: 200, output_tokens: 100, total_tokens: 300,
      by_seat: {'0': {cost_usd: 0.5, total_tokens: 150},
                '1': {cost_usd: 0.4, total_tokens: 150}}}});
    p.textAfterSecond = String(strip.textContent);
    p.tipAfterSecond = String(strip.title || '');
    // a fresh stage forgets the chat's budget entirely
    await ctx.newChat();
    p.hiddenAfterNewChat = !(byId['budgetStrip'].className || '').includes('show');
    p.textAfterNewChat = String(byId['budgetStrip'].textContent);
  } catch (e) { more.budgetError = (e && e.stack) || String(e); }

  // ---- @-mention hint + composer drag-and-drop, through their seams ------
  // The chip only MIRRORS engine routing (relay.parse_mention), so the probe
  // is that it appears for a seated label and stays hidden otherwise. Drop
  // planning is a pure core over plain descriptors; classification is driven
  // with fake DataTransferItemList items.
  try {
    const p = more.quickwins = {};
    await ctx.newChat();
    byId['say'].value = '@claude hello there';
    ctx.updateMentionHint();
    p.hintForClaude = !byId['mentionHint'].hidden
      ? byId['mentionHint'].textContent : null;
    byId['say'].value = '@nobody hello there';
    ctx.updateMentionHint();
    p.hintHiddenWhenNoMatch = !!byId['mentionHint'].hidden;
    byId['say'].value = 'plain text';
    ctx.updateMentionHint();
    p.hintHiddenForPlain = !!byId['mentionHint'].hidden;
    byId['say'].value = '';
    // files plan: everything attaches, nothing is refused
    const filesPlan = ctx.planDrop(
      [{name: 'a.png', isDir: false}, {name: 'b.txt', isDir: false}], false);
    p.filesAttach = filesPlan.attachCount;
    p.filesNoCue = filesPlan.cue === null && filesPlan.folderName === null;
    // folder while UNSEATED: detected and named, refused with the Choose hint
    const unseated = ctx.planDrop([{name: 'proj', isDir: true}], false);
    p.folderUnseatedCue = /choose/i.test(unseated.cue || '');
    p.folderNamed = unseated.folderName === 'proj' &&
      unseated.attachCount === 0;
    // folder while SEATED: locked, said in those words
    const seatedF = ctx.planDrop([{name: 'proj', isDir: true}], true);
    p.folderSeatedCue = /locked/i.test(seatedF.cue || '');
    // entry-vs-folder classification over fake drop items
    const items = [
      {kind: 'file', getAsFile() { return {name: 'x.png'}; },
       webkitGetAsEntry() { return null; }},
      {kind: 'file', getAsFile() { return null; },
       webkitGetAsEntry() { return {isDirectory: true, name: 'proj'}; }},
      {kind: 'string'},
    ];
    const cls = ctx.classifyDropEntries(items);
    p.classifiedFiles = cls.files.length;
    p.classifiedFolders = cls.folders.map(f => f.name);
    p.dropCueStartsHidden = !!byId['dropCue'].hidden;
  } catch (e) { more.quickwinsError = (e && e.stack) || String(e); }
  // ---- the relay's own "I am working" indicator --------------------------
  // Seats have typing indicators; everything that is NOT a seat used to run
  // in silence. Driven through the REAL uiEvent path, because the grace
  // period, the id pairing and the teardown are all top-level script.
  try {
    const p = more.working = {};
    await ctx.newChat();
    const feed = byId['feed'];
    const rows = () => feed.querySelectorAll('.working').length;
    const text = () => [...feed.querySelectorAll('.working')]
      .map(deepText).join(' | ');

    // 1. a side call that finishes inside the grace period paints NOTHING
    ctx.uiEvent({event: 'working', payload: {id: 'fast', phase: 'moderator',
      what: 'Choosing who speaks next', started: Date.now() / 1000}});
    p.nothingImmediately = rows() === 0;
    ctx.uiEvent({event: 'working', payload: {id: 'fast', done: true,
                                             elapsed: 0.04}});
    await new Promise(r => setTimeout(r, 600));
    p.fastCallNeverPainted = rows() === 0;

    // 2. a slow one paints, with its words and its detail
    ctx.uiEvent({event: 'working', payload: {id: 'slow', phase: 'plan',
      what: 'Supervisor is planning the work', detail: 'make this better',
      started: Date.now() / 1000}});
    await new Promise(r => setTimeout(r, 600));
    p.slowRows = rows();
    p.slowText = text();

    // 3. two at once are two rows (parallel seat threads, helper threads)
    ctx.uiEvent({event: 'working', payload: {id: 'slow2', phase: 'gate',
      what: 'Running the verification gate', detail: 'pytest',
      started: Date.now() / 1000}});
    await new Promise(r => setTimeout(r, 600));
    p.twoRows = rows();

    // 4. a message lands: the rows stay BELOW it, like typing indicators
    ctx.uiEvent({event: 'message', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', text: 'hello', round: 1}});
    const kids = [...feed.children].map(c => c.className || '');
    p.rowsStayLast = kids.slice(-2).every(c => c.includes('working'));

    // 5. closing by ID removes exactly that row
    ctx.uiEvent({event: 'working', payload: {id: 'slow', done: true,
                                             elapsed: 91.2}});
    p.afterFirstClose = rows();
    p.remainingIsGate = /verification gate/.test(text());

    // 6. the run ending clears whatever is left -- a spinner that outlives
    //    its run is worse than no spinner at all
    ctx.hideAllTyping();
    p.clearedOnFinish = rows() === 0;

    // 7. reopening a chat mid-plan replays it instead of rendering idle
    p.replayed = null;
    ctx.showWorking({id: 'replay', phase: 'plan', what: 'Planning the work',
                     started: Date.now() / 1000 - 42});
    await new Promise(r => setTimeout(r, 600));
    p.replayed = text();
    ctx.hideAllTyping();

    // 8. THE routing trap: the app's pre-flight row opens before the chat has
    //    an id and closes after `started` gave it one. A close routed like an
    //    ordinary event is dropped by the not-my-chat gate and the spinner
    //    never goes away.
    ctx.uiEvent({event: 'working', payload: {id: 'setup', phase: 'setup',
      what: 'Setting up the conversation', started: Date.now() / 1000}});
    await new Promise(r => setTimeout(r, 600));
    p.setupPainted = rows() === 1;
    ctx.uiEvent({event: 'working', payload: {id: 'setup', done: true,
      elapsed: 1.2, chat_id: 'a-chat-that-is-not-open'}});
    p.setupClosedAcrossChats = rows() === 0;

    // ...while an OPEN belonging to another chat is still not this
    //    transcript's business
    ctx.uiEvent({event: 'working', payload: {id: 'elsewhere', phase: 'plan',
      what: 'Planning the work', chat_id: 'a-chat-that-is-not-open',
      started: Date.now() / 1000}});
    await new Promise(r => setTimeout(r, 600));
    p.otherChatNotPainted = rows() === 0;
    ctx.hideAllTyping();
  } catch (e) { more.workingError = (e && e.stack) || String(e); }

  // ---- richer live narration (2026-08-26) --------------------------------
  // The seat log used to be a grey wall of file names: no commentary, no
  // outcomes, no sense of how much had happened. The line CONTENT is checked
  // through actLineHtml directly, because this stub parses nested innerHTML
  // into a flat list and cannot see a span inside a div; the DOM is used for
  // what it can answer honestly (classes, counts, the step badge).
  try {
    const p = more.narration = {};
    await ctx.newChat();
    const feed = byId['feed'];
    ctx.showTyping(0, 'claude', 'Claude', {started: Date.now() / 1000 - 300});
    const steps = [
      {kind: 'say', text: "I'll grep for needle, then edit."},
      {kind: 'search', text: 'searching in sample.txt: needle'},
      {kind: 'result', text: 'found 1'},
      {kind: 'command', text: '$ pytest -q'},
      {kind: 'result', text: 'failed (exit 1): AssertionError'},
    ];
    steps.forEach(s => ctx.uiEvent({event: 'activity', payload:
      Object.assign({speaker: 0, provider: 'claude', name: 'Claude'}, s)}));
    const typing = feed.querySelectorAll('.typing')[0];
    p.classes = [...typing.querySelectorAll('.act-line')]
      .map(l => String(l.className || ''));

    // ---- the renderer's own output, exactly as the browser gets it ----
    p.htmlSay = ctx.actLineHtml(steps[0]);
    p.htmlCommand = ctx.actLineHtml(steps[3]);
    p.htmlFail = ctx.actLineHtml(steps[4]);
    p.htmlOkResult = ctx.actLineHtml(steps[2]);
    p.htmlEscaped = ctx.actLineHtml(
      {kind: 'say', text: '<img src=x onerror=alert(1)>'});

    // a progress tick is a stopwatch, not a step: it must not inflate the
    // count, exactly as the engine's sink refuses to persist one
    p.stepsAfterFive = Number(typing.dataset.steps || 0);
    ctx.uiEvent({event: 'activity', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', kind: 'progress', text: '1,000 tokens'}});
    p.stepsAfterProgress = Number(typing.dataset.steps || 0);
    p.header = String(typing.querySelector('.trow').textContent || '') +
      String(typing.querySelector('.tsteps').textContent || '');
    // one progress LINE, replaced in place, however many ticks arrive
    ctx.uiEvent({event: 'activity', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', kind: 'progress', text: '2,000 tokens'}});
    p.progressLines = typing.querySelectorAll('.k-progress').length;

    // the SAME renderer paints a finished row's stored activity
    ctx.hideAllTyping();
    ctx.uiEvent({event: 'message', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', text: 'done', round: 1, activity: steps}});
    const row = [...feed.children].pop();
    p.storedText = deepText(row);
    p.storedClasses = [...row.querySelectorAll('.act-line')]
      .map(l => String(l.className || ''));
  } catch (e) { more.narrationError = (e && e.stack) || String(e); }

  // ---- spawn-lineage tree on rail rows (t6) -------------------------------
  // Alloy persists provenance as meta.parent on child rows; the UI derives
  // the whole parent→children chain client-side. Driven exactly the way a
  // user drives it: find the row, click its chip, click a listed child.
  try {
    const rows = [...byId['chatList'].querySelectorAll('.chat-row')];
    const tOf = r => ((r.querySelector('.t') || {}).textContent || '');
    const parentRow = rows.find(r => tOf(r) === 'Team Parent');
    const childRow = rows.find(r => tOf(r) === '\u21b3 Child Squad');
    const plainRow = rows.find(r => tOf(r) === 'Death Factory');
    const lin = parentRow && parentRow.querySelector('.lin');
    const opensBefore = apiCalls.filter(n => n === 'open_session').length;
    more.lineage = {
      childPrefixed: !!childRow,
      chipPresent: !!lin,
      chipVisible: !!lin && !lin.hidden,
      chipText: lin ? lin.textContent : null,
      noKidsChipHidden: !plainRow || plainRow.querySelector('.lin').hidden,
      popStartsHidden: !(parentRow && parentRow.querySelector('.lineage-pop')) ||
        parentRow.querySelector('.lineage-pop').hidden,
    };
    if (lin) {
      lin.onclick({stopPropagation() {}});
      const pop = parentRow.querySelector('.lineage-pop');
      more.lineage.popShownAfterClick = !!pop && !pop.hidden;
      more.lineage.headText = pop && pop.querySelector('.lineage-h')
        ? pop.querySelector('.lineage-h').textContent : null;
      more.lineage.items = pop
        ? [...pop.querySelectorAll('.lineage-item')]
            .map(i => i.querySelector('.lt').textContent) : [];
      if (pop && pop.querySelectorAll('.lineage-item').length) {
        pop.querySelector('.lineage-item').onclick({stopPropagation() {}});
        await new Promise(r => setTimeout(r, 40));
        more.lineage.itemOpensChat =
          apiCalls.filter(n => n === 'open_session').length > opensBefore;
      }
      lin.onclick({stopPropagation() {}});
      more.lineage.popHiddenAfterSecondClick = pop.hidden;
      more.lineage.chipOpenClassCleared =
        lin.className.includes('open') === false;
    }
  } catch (e) { more.lineageError = (e && e.stack) || String(e); }

  // ---- team-report caption link in the feed --------------------------------
  // A team report row IS the parent→child edge rendered as a message; the
  // jump is offered only when the referenced session still exists.
  try {
    const opensBefore = apiCalls.filter(n => n === 'open_session').length;
    ctx.addMsg('claude', 'Team report',
      "(Team finished. Report follows.) Full transcript: sessions/sess-two",
      'team report for Claude', '', '2026-08-26T10:00:00', null, null,
      {speaker: 'team-t1', message_id: 'probe-team-row'});
    // a report pointing at a DELETED child must render no link…
    ctx.addMsg('claude', 'Team report',
      "(Team failed.) Partial transcript: sessions/gone-child",
      'team report for Claude', '', '2026-08-26T10:01:00', null, null,
      {speaker: 'team-t2', message_id: 'probe-team-row-2'});
    // …and neither must an ordinary seat merely mentioning a session path
    ctx.addMsg('claude', 'Claude', 'mentioning sessions/sess-team-child in prose',
               '', '', '2026-08-26T10:02:00', null, null,
               {speaker: 's0', message_id: 'probe-seat-row'});
    const links = [...byId['feed'].querySelectorAll('.lineage-link')];
    more.teamLink = {
      count: links.length,   // exactly one: sess-two lives, gone-child does not
      titles: links.map(b => b.title),
    };
    if (links.length === 1) {
      links[0].onclick({stopPropagation() {}});
      await new Promise(r => setTimeout(r, 40));
      more.teamLink.opensChat =
        apiCalls.filter(n => n === 'open_session').length > opensBefore;
    }
  } catch (e) { more.teamLinkError = (e && e.stack) || String(e); }

  // ---- delivery-refusal pills (comms-design.md section 3.3, UI half) ------
  // The engine stamps refused deliveries into the envelope as rejected_to
  // [{seat, reason}] (+ narrowing_failed). Driven through the same addMsg
  // path live turns and replay share; malformed shapes must render nothing.
  try {
    ctx.addMsg('claude', 'Claude', 'a reply that could not reach everyone',
               '', '', '2026-08-26T11:00:00', null, null,
               {speaker: 's1', origin: 'seat',
                delivered_to: ['0'], audience: ['0', '9'],
                rejected_to: [{seat: '9',
                               reason: 'worker radio-silent until t3 settles'}]});
    // narrowing failure alone: every intended seat got it, but [[TO]] fell
    // back to broadcast and replay must say so
    ctx.addMsg('claude', 'Claude', 'TO fell back to broadcast',
               '', '', '2026-08-26T11:01:00', null, null,
               {speaker: 's1', origin: 'seat', delivered_to: ['0'],
                audience: '*', narrowing_failed: true});
    // garbage entries must not crash or invent a pill
    ctx.addMsg('claude', 'Claude', 'malformed refusals',
               '', '', '2026-08-26T11:02:00', null, null,
               {speaker: 's1', origin: 'seat', delivered_to: ['0'],
                audience: '*', rejected_to: [null, "", {"reason": "orphan"}]});
    const rpills = [...byId['feed'].querySelectorAll('.refusal-pill')];
    // Stub-DOM readers: a template-parsed leaf keeps its text in _html
    // (textContent stays ''), and attributes live in _attrs — so labels are
    // read through tag-stripped _html and titles through getAttribute.
    const ptext = p => String((p && p._html) || '').replace(/<[^>]*>/g, '').trim();
    const ptitle = p => {
      if (!p) return '';
      const t = typeof p.getAttribute === 'function' ? p.getAttribute('title') : null;
      return String(t || p.title || '');
    };
    const pattr = (p, a) => {
      if (!p || typeof p.getAttribute !== 'function') return '';
      return String(p.getAttribute(a) || '');
    };
    more.refusal = {
      total: rpills.length,                       // exactly 2 expected
      firstSeats: rpills.length ? pattr(rpills[0], 'data-seats') : null,
      firstTitle: rpills.length ? ptitle(rpills[0]) : null,
      secondText: rpills.length > 1
        ? pattr(rpills[1], 'data-seats') || '(narrowing only)' : null,
      secondTitle: rpills.length > 1 ? ptitle(rpills[1]) : null,
      secondMentionsBroadcast: rpills.length > 1 &&
        /broadcast/.test(ptitle(rpills[1])),
    };
    const before = rpills.length;
    ctx.addMsg('claude', 'Claude', 'plain reply, no refusals',
               '', '', '2026-08-26T11:03:00', null, null,
               {speaker: 's1', origin: 'seat', delivered_to: ['0'],
                audience: '*'});
    more.refusal.plainAddsNone =
      [...byId['feed'].querySelectorAll('.refusal-pill')].length === before;
  } catch (e) { more.refusalError = (e && e.stack) || String(e); }

  // ---- the default stage: factory fallback + Set as default ----------------
  // Before the solo teardown below: saveDefaultStage captures the LIVE stage,
  // and this is the last moment it is still the seeded three-seat roster.
  try {
    more.defstage = {};
    // the boot above consumed the seeded default; with it gone, the loader
    // must answer null so the FACTORY solo-Claude stage takes over
    delete store['defaultStage'];
    more.defstage.noSaved = ctx.loadDefaultStage();
    more.defstage.factory = vm.runInContext('FACTORY_STAGE', ctx);
    store['defaultStage'] = JSON.stringify([{provider: 'evil'}]);
    more.defstage.garbage = ctx.loadDefaultStage();
    store['defaultStage'] = 'not json';
    more.defstage.junk = ctx.loadDefaultStage();
    // the button's whole job: the stage on screen becomes the stored default
    document.querySelectorAll('.seat .switch').forEach(sw => { sw.checked = true; });
    ctx.saveDefaultStage();
    more.defstage.saved = JSON.parse(store['defaultStage'] || 'null');
    more.defstage.btnText = byId['saveDefaultBtn'] ? byId['saveDefaultBtn'].textContent : null;
  } catch (e) { more.defstage = {error: String((e && e.stack) || e)}; }

  // ---- ONE seat: Alloy as a harness for a single agent ---------------------
  // Deliberately the LAST probe: it removes seat cards, and every earlier
  // block assumes the three-seat boot roster.
  try {
    more.solo = {};
    // The Advanced drawer's two crowd-voiced option LABELS, read BEFORE the
    // roster is torn down. Capturing "many" by adding a seat back afterwards
    // would leave a different stage for every probe after this one.
    const optText = (id, v) =>
      [...byId[id].options].filter(o => o.value === v).map(o => o.textContent);
    const drawerWords = () => ({
      laps: optText('budgetUnitSel', 'laps'),
      participants: optText('completionSel', 'participants'),
      values: [...byId['budgetUnitSel'].options].map(o => o.value),
    });
    ctx.rosterChanged();
    more.solo.drawerMany = drawerWords();
    const cards = [...document.querySelectorAll('.seat')];
    // the rail's own remove handler, not a shortcut past it
    cards.slice(1).forEach(c => c.querySelector('.rm').onclick());
    more.solo.seatsLeft = document.querySelectorAll('.seat').length;
    more.solo.isSolo = ctx.soloStage();
    ctx.renderModePick();
    const rows = [...byId['modeOptList'].children].map(b => ({
      mode: b.getAttribute('data-mode'),
      name: b.querySelector('b').textContent,
      desc: b.querySelector('.mode-desc').textContent,
      off: !!b.disabled,
    }));
    more.solo.rows = rows;
    // clicking a refused row must change nothing
    const arena = [...byId['modeOptList'].children]
      .find(b => b.getAttribute('data-mode') === 'arena');
    const wasPreset = ctx.selectedPreset();
    if (arena) arena.onclick();
    more.solo.presetAfterRefusedClick = ctx.selectedPreset();
    more.solo.presetWas = wasPreset;
    ctx.normalizePolicyControls();
    more.solo.policyReason = byId['policyReason'].textContent;
    more.solo.modToggleHidden = !!byId['modToggleRow'].hidden;
    ctx.syncRoundsCtl();
    more.solo.roundsLabel = byId['roundsLabel'].textContent;
    ctx.renderEmptyRoster();
    more.solo.headline = byId['emptyH2'] ? byId['emptyH2'].textContent : null;
    const stopBtn = document.querySelectorAll('.seat-stop')[0];
    more.solo.stopTitle = stopBtn ? stopBtn.title : null;
    more.solo.pillLabel = byId['modePickLabel'].textContent;
    more.solo.refusals = {
      openDiscussion: ctx.presetSeatRefusal('open_discussion', 1),
      buildExecute: ctx.presetSeatRefusal('build_execute', 1),
      keepImproving: ctx.presetSeatRefusal('keep_improving', 1),
      arena: ctx.presetSeatRefusal('arena', 1),
      liveRoom: ctx.presetSeatRefusal('live_room', 1),
      panel: ctx.presetSeatRefusal('panel_review', 1),
      zero: ctx.presetSeatRefusal('open_discussion', 0),
      arenaAtTwo: ctx.presetSeatRefusal('arena', 2),
    };
    // a hand-tuned recipe reads as "custom" and has no preset rule of its
    // own: the live mode has to answer for it
    byId['modeSel'].value = 'free';
    more.solo.customFree = ctx.presetSeatRefusal('custom', 1);
    byId['modeSel'].value = 'battle';
    more.solo.customBattle = ctx.presetSeatRefusal('custom', 1);
    byId['modeSel'].value = 'free';
    more.solo.namedWhileFree = ctx.presetSeatRefusal('open_discussion', 1);
    byId['modeSel'].value = 'battle';
    more.solo.namedWhileBattle = ctx.presetSeatRefusal('build_execute', 1);
    byId['modeSel'].value = 'panel';
    more.solo.customPanel = ctx.presetSeatRefusal('custom', 1);
    byId['modeSel'].value = 'round_robin';
    more.solo.customRoundRobin = ctx.presetSeatRefusal('custom', 1);
    // a moderated solo room must keep the switch that turns it off, and the
    // drawer must say what it costs
    byId['floorSel'].value = 'moderated';
    ctx.normalizePolicyControls('floorSel');
    more.solo.modRowHiddenWhenModerated = !!byId['modToggleRow'].hidden;
    more.solo.moderatedReason = byId['policyReason'].textContent;
    // a reactive solo recipe must say it will not start
    byId['floorSel'].value = 'fair';
    ctx.normalizePolicyControls('floorSel');
    more.solo.reactiveReason = byId['policyReason'].textContent;
    // a roster change re-describes; it must not erase the badge explaining a
    // move the user was just shown
    ctx.applyPreset('open_discussion');
    byId['completionSel'].value = 'moderator';
    ctx.advancedPolicyChanged('completionSel');
    more.solo.badgeBefore = byId['policyChanges'].textContent;
    ctx.rosterChanged();
    more.solo.badgeAfterRoster = byId['policyChanges'].textContent;
    ctx.rosterChanged();
    more.solo.drawerSolo = drawerWords();
  } catch (e) { more.soloError = (e && e.stack) || String(e); }

  // ---- the memory modal follows the chat ---------------------------------
  // Ctrl+Tab / Ctrl+1-9 / Ctrl+T change the chat straight through an open
  // modal, and every action in it sends `activeId` at CALL time -- so a stale
  // header could name project A while memAdd wrote into project B.
  try {
    more.memFollow = {};
    memReply = {scope: 'proj-b', label: 'Project B', global_scope: 'global',
                truncated: false, error: null, entries: []};
    memCalls.length = 0;
    byId['memModal'].classList.add('show');
    byId['memText'].value = 'half a thought';
    await ctx.memoryFollowsTheChat();
    more.memFollow.asked = memCalls.slice();
    more.memFollow.header = deepText(byId['memScope']);
    more.memFollow.draftKept = byId['memText'].value;
    more.memFollow.note = byId['memNote'].textContent;
    // ...and with the modal CLOSED it must not call the bridge at all
    byId['memModal'].classList.remove('show');
    memCalls.length = 0;
    await ctx.memoryFollowsTheChat();
    more.memFollow.askedWhenClosed = memCalls.slice();
  } catch (e) { more.memFollowError = (e && e.stack) || String(e); }

  // ---- W1.1: produced-file chips, through the REAL message path ----------
  // The row's `artifacts` list has been stamped by the engine since it
  // shipped and the UI never read it. Driven as a `message` event, not by
  // calling artifactChips directly, because the field has to survive
  // addMsg's whole argument chain to be worth anything.
  try {
    const p = more.artifacts = {};
    await ctx.newChat();
    const feed = byId['feed'];
    const arts = n => {
      const rows = [...feed.children].filter(c => (c.className || '').includes('msg'));
      const row = rows[rows.length - 1 + (n || 0)];
      if (!row) return null;
      const strip = row.querySelector('.msg-arts');
      if (!strip) return null;
      // the stub does NOT derive a parent's textContent from its children,
      // so read the spans the renderer actually filled
      const span = (c, cls) => {
        const s = c.querySelector('.' + cls);
        return s ? String(s.textContent || '') : null;
      };
      return [...strip.children].map(c => ({
        cls: c.className || '',
        tag: c.tag || '',
        title: c.title || '',
        ico: span(c, 'art-ico'),
        name: span(c, 'art-name'),
        size: span(c, 'art-size'),
      }));
    };
    // 1. a reply that wrote nothing shows no strip at all
    ctx.uiEvent({event: 'message', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', text: 'just talking', round: 1, message_id: 'm0'}});
    p.silentWhenAbsent = arts() === null;
    // 2. a reply that wrote files gets one chip each
    ctx.uiEvent({event: 'message', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', text: 'done', round: 2, message_id: 'm1',
      artifacts: [
        {artifact_id: 'a1', path: 'notes\\\\plan.md', kind: 'text/markdown',
         size: 3288, producer: 0, source_message_id: 'm1',
         operation: 'created_or_modified'},
        {artifact_id: 'a2', path: 'chart.png', kind: 'image/png',
         size: 20480, producer: 0, source_message_id: 'm1',
         operation: 'created_or_modified'},
      ]}});
    p.chips = arts();
    // 3. the click routes by KIND: image to the lightbox, text to the pane
    const strip = [...feed.children].filter(
      c => (c.className || '').includes('msg')).pop().querySelector('.msg-arts');
    const calls = [];
    const realCode = ctx.openCode, realLb = ctx.openLightbox;
    ctx.openCode = (path, prov) => calls.push(['code', path, prov]);
    ctx.openLightbox = (path, name) => calls.push(['lightbox', path, name]);
    strip.children[0].onclick({stopPropagation() {}});
    strip.children[1].onclick({stopPropagation() {}});
    ctx.openCode = realCode; ctx.openLightbox = realLb;
    p.routed = calls;
    // 4. an empty list, a junk entry and a non-array are all silent
    ctx.uiEvent({event: 'message', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', text: 'x', round: 3, message_id: 'm2', artifacts: []}});
    p.silentWhenEmpty = arts() === null;
    ctx.uiEvent({event: 'message', payload: {speaker: 0, provider: 'claude',
      name: 'Claude', text: 'y', round: 4, message_id: 'm3',
      artifacts: [{artifact_id: 'z', kind: 'text/plain'}, null, 7]}});
    p.silentWhenPathless = arts() === null;
    // 5. replay must render identically — openChat feeds the same rows back
    //    through addMsg, so a chip that only appeared live would vanish on
    //    reopen (the failure mode typing indicators actually had)
    p.replay = null;
    ctx.addMsg('claude', 'Claude', 'replayed', 'round 5', null,
               '2026-08-27T10:00:00', null, null,
               {message_id: 'm4', delivered_to: [], speaker: 0,
                artifacts: [{artifact_id: 'r1', path: 'a\\\\b\\\\out.txt',
                             kind: 'text/plain', size: 12}]});
    p.replay = arts();
  } catch (e) { more.artifactsError = (e && e.stack) || String(e); }

  // ---- W1.6 folding + W1.7 the note on a reaction -------------------------
  try {
    const p = more.fold = {};
    await ctx.newChat();
    const feed = byId['feed'];
    const rows = () => [...feed.children].filter(
      c => String(c.className || '').indexOf('msg') >= 0);
    const last = () => rows()[rows().length - 1];
    const say = (text, extra, kind) => ctx.uiEvent({event: 'message', payload:
      Object.assign({speaker: 0, provider: kind || 'claude', name: 'Claude',
                     text: text, round: 1,
                     message_id: 'x' + rows().length}, extra || {})});
    const btn = (rowEl, cls) => rowEl.querySelector('.' + cls);
    const folded = rowEl => String(rowEl.className || '')
      .indexOf('msg-folded') >= 0;
    // 1. fold / unfold, and the one line a folded row keeps
    say('The first line of the reply.\nAnd a second one.', {message_id: 'f1'});
    let r1 = last();
    btn(r1, 'fold-btn').onclick({stopPropagation() {}});
    p.foldedClass = folded(r1);
    const peekEl = r1.querySelector('.msg-peek');
    p.peek = peekEl ? String(peekEl.textContent || '') : null;
    btn(r1, 'fold-btn').onclick({stopPropagation() {}});
    p.unfoldedClass = folded(r1);
    // 2. the peek is the row's own text, not its rendered markdown
    say('# A heading line\n\nbody', {message_id: 'f2'});
    btn(last(), 'fold-btn').onclick({stopPropagation() {}});
    p.peekMarkdown = String((last().querySelector('.msg-peek') || {})
      .textContent || '');
    // 3. a row that ends with a directive refuses, out loud
    say('Nothing left to add. [[WRAP]]', {message_id: 'f3'});
    const dirRow = last();
    btn(dirRow, 'fold-btn').onclick({stopPropagation() {}});
    p.directiveFolded = folded(dirRow);
    p.directiveTitle = String(btn(dirRow, 'fold-btn').title || '');
    // 4. alt-click folds every row from that speaker, and only that speaker
    await ctx.newChat();
    say('one', {message_id: 'a1', speaker: 0});
    say('two', {message_id: 'a2', speaker: 1});
    say('three', {message_id: 'a3', speaker: 0});
    btn(rows()[0], 'fold-btn').onclick({stopPropagation() {}, altKey: true});
    p.afterAltClick = rows().map(folded);
    await ctx.newChat();
    say('plain', {message_id: 'b1', speaker: 0});
    say('done here [[WRAP]]', {message_id: 'b2', speaker: 0});
    btn(rows()[0], 'fold-btn').onclick({stopPropagation() {}, altKey: true});
    p.altClickWithDirective = rows().map(folded);
    // 5. a system row has nothing worth folding
    await ctx.newChat();
    ctx.addMsg('system', 'relay', 'a note from the relay', '');
    p.systemHasFold = !!btn(last(), 'fold-btn');
    // 6. a find hit inside a folded row opens it before scrolling.
    //    The stub parses innerHTML into ELEMENTS only, so a rendered body
    //    holds no text nodes for collectFindHits to walk; a real one does.
    //    Appending one gives find something it can genuinely find, and the
    //    rest of the path -- findRun, paintFind, scrollFindCurrent -- is the
    //    shipping code.
    await ctx.newChat();
    say('the needle is in here somewhere', {message_id: 'n1'});
    const nrow = last();
    nrow.querySelector('.msg-body')
      .appendChild(document.createTextNode('the needle is in here'));
    btn(nrow, 'fold-btn').onclick({stopPropagation() {}});
    p.foldedBeforeFind = folded(nrow);
    ctx.findOpen();
    byId['findInput'].value = 'needle';
    ctx.findRun();
    p.stillFoldedAfterFind = folded(nrow);
    ctx.findClose();
    // ---- W1.7
    await ctx.newChat();
    playbookCalls.length = 0;
    reactCalls.length = 0;
    ctx.addMsg('claude', 'Claude', 'a reply worth noting', 'round 1', null,
               '2026-08-27T10:00:00', null, null,
               {message_id: 'm9', delivered_to: [], speaker: 0,
                _reaction: 'not_helpful',
                _reactionNote: 'It answered a different question.'});
    const nr = last();
    p.noteText = String((nr.querySelector('.msg-note') || {})
      .textContent || '');
    // The REAL inline editor, driven the way a person drives it: open it,
    // type into its textarea, click one of its own two buttons.
    const editNote = async (rowEl, text) => {
      btn(rowEl, 'note-btn').onclick({stopPropagation() {}});
      const box = rowEl.querySelector('.msg-note-edit');
      const ta = box.querySelector('textarea');
      const buttons = [...box.querySelectorAll('button')];
      if (text === null) buttons[1].onclick({stopPropagation() {}});
      else { ta.value = text; buttons[0].onclick({stopPropagation() {}}); }
      await new Promise(r => setTimeout(r, 0));
    };
    // cancel changes nothing
    await editNote(nr, null);
    p.cancelCalls = reactCalls.length;
    p.noteAfterCancel = String((nr.querySelector('.msg-note') || {})
      .textContent || '');
    // ...and saving carries the row's OWN verdict plus the typed words
    await editNote(nr, 'typed words');
    p.saveCall = reactCalls[0] || null;
    // a plain thumb click passes NO note at all
    reactCalls.length = 0;
    nr.querySelectorAll('.react-btn')[0].onclick({stopPropagation() {}});
    await new Promise(r => setTimeout(r, 0));
    p.thumbCall = reactCalls[0] || null;
    // removing the thumb clears the note from the row too
    reactCalls.length = 0;
    nr.querySelectorAll('.react-btn')[0].onclick({stopPropagation() {}});
    await new Promise(r => setTimeout(r, 0));
    p.noteAfterUnreact = String((nr.querySelector('.msg-note') || {})
      .textContent || '');
    // a note on a row with no thumb adopts the gentle reading
    reactCalls.length = 0;
    ctx.addMsg('claude', 'Claude', 'unmarked', 'round 2', null,
               '2026-08-27T10:01:00', null, null,
               {message_id: 'm7', delivered_to: [], speaker: 0});
    await editNote(last(), 'just saying');
    p.bareNoteCall = reactCalls[0] || null;
    // Replay repaints a stored note. Driven through the REAL openChat, not
    // by handing addMsg the field: the line that maps get_reactions onto
    // each row is the one that can be lost, and a probe that sets the field
    // itself would pass with or without it.
    reactionsReply = {stub1: {verdict: 'helpful', note: 'from the record'}};
    await ctx.openChat('some-chat');
    await new Promise(r => setTimeout(r, 0));
    p.replayNote = String((byId['feed'].querySelector('.msg-note') || {})
      .textContent || '');
    reactionsReply = {};
  } catch (e) { more.foldError = (e && e.stack) || String(e); }

  // ---- W1.5: stats + the playbook's first UI ------------------------------
  // Driven through the REAL `stats` and `playbook` events. The rule the
  // whole panel exists to hold: a number nobody reported is a BLANK, never
  // a zero -- and a token count withheld because it predates the telemetry
  // fix is STATED, not silently missing.
  try {
    const p = more.stats = {};
    // The stub parses innerHTML FLAT, so a <tr> has no child <td>s at all.
    // Read the table out of the element's own _html instead.
    const cells = label => {
      const html = String(byId['stTables']._html || '');
      for (const chunk of html.split('<tr')) {
        if (chunk.indexOf('>' + label + '</td>') < 0) continue;
        const tds = chunk.match(/<td[^>]*>[\s\S]*?<\/td>/g) || [];
        return tds.slice(1).map(td => td.replace(/<[^>]*>/g, ''));
      }
      return null;
    };
    const settle = () => new Promise(r => setTimeout(r, 0));
    ctx.uiEvent({event: 'stats', payload: {
      sessions_counted: 3, sessions_with_usage: 3,
      totals: {key: 'all', label: 'All seats', sessions: 3, turns: 13,
               cost_usd: 0.25, input_tokens: null, output_tokens: 42,
               cached_tokens: null, wall_ms: 1200, cache_hit: null,
               prompt_tokens: null, superseded_sessions: 1},
      providers: [
        {key: 'claude', label: 'claude', sessions: 2, turns: 9,
         cost_usd: 0.25, input_tokens: 10, output_tokens: 42,
         cached_tokens: 90, wall_ms: 1200, cache_hit: 0.9,
         prompt_tokens: 100, superseded_sessions: 0},
        {key: 'gemini', label: 'gemini', sessions: 1, turns: 4,
         cost_usd: null, input_tokens: null, output_tokens: null,
         cached_tokens: null, wall_ms: null, cache_hit: null,
         prompt_tokens: null, superseded_sessions: 0},
        {key: 'gpt', label: 'gpt', sessions: 1, turns: 3,
         cost_usd: null, input_tokens: 559310306, output_tokens: 12,
         cached_tokens: 4, wall_ms: null, cache_hit: 0,
         prompt_tokens: 4000, superseded_sessions: 1},
      ],
      models: []}});
    p.claudeCells = cells('claude');
    p.geminiCells = cells('gemini');
    p.zeroHit = (cells('gpt') || [])[5];
    p.caveatShown = !byId['stCaveat'].hidden;
    p.caveatText = String(byId['stCaveat'].textContent || '');
    p.caveatNumbers = 'half a billion';
    ctx.uiEvent({event: 'stats', payload: {
      sessions_counted: 1, sessions_with_usage: 1,
      totals: {key: 'all', label: 'All seats', sessions: 1, turns: 2,
               cost_usd: 0.1, superseded_sessions: 0},
      providers: [], models: []}});
    p.caveatAfterClean = !byId['stCaveat'].hidden;
    // ---- the playbook half
    const bookRows = () => [...byId['bookList'].children]
      .filter(r => String(r.className || '').indexOf('book-row') >= 0);
    ctx.uiEvent({event: 'playbook', payload: {
      summary: {sessions_counted: 7,
                rules: {total: 3, active: 2, pinned: 1, dismissed: 1}},
      rules: [
        {heuristic_id: 'a', directive: 'Do A', evidence_count: 3,
         source: 'human_reason', pinned: false, status: 'active',
         provenance_display: 'you tagged this'},
        {heuristic_id: 'b', directive: 'Do B', evidence_count: 2,
         source: 'inferred_pattern', pinned: true, status: 'active',
         provenance_display: 'seen twice'},
        {heuristic_id: 'c', directive: 'Old one', evidence_count: 1,
         source: 'inferred_pattern', pinned: false, status: 'dismissed',
         provenance_display: 'seen once'},
      ]}});
    p.bookRows = bookRows().map(r => {
      const d = r.querySelector('.book-directive');
      return d ? String(d.textContent || '') : null;
    });
    p.bookDismissed = bookRows().map(
      r => String(r.className || '').indexOf('dismissed') >= 0);
    p.bookActions = bookRows().map(r => [...r.querySelectorAll('button')]
      .map(b => String(b.textContent || '')));
    // clicking routes to the bridge with THAT rule's id and nothing else.
    // Re-seeded before each click because a successful call re-renders from
    // whatever `rules` the bridge answered with -- an empty list here.
    const seedOne = () => ctx.uiEvent({event: 'playbook', payload: {
      summary: {}, rules: [{heuristic_id: 'a', directive: 'Do A',
                            evidence_count: 1, pinned: false,
                            status: 'active'}]}});
    playbookCalls.length = 0;
    seedOne();
    bookRows()[0].querySelectorAll('button')[0].onclick();
    await settle();
    p.pinCall = playbookCalls[0] || null;
    playbookCalls.length = 0;
    seedOne();
    bookRows()[0].querySelectorAll('button')[1].onclick();
    await settle();
    p.dismissCall = playbookCalls[0] || null;
    // a bridge error is shown, not swallowed
    playbookReply = {error: 'no such rule'};
    seedOne();
    bookRows()[0].querySelectorAll('button')[0].onclick();
    await settle();
    p.noteAfterError = String(byId['stNote'].textContent || '');
    playbookReply = null;
    ctx.uiEvent({event: 'playbook', payload: {summary: {}, rules: []}});
    p.bookEmpty = String((byId['bookList'].querySelector('#bookEmpty') || {})
      .textContent || '');
    // tabs
    ctx.statsTab('book');
    const panes = () => [byId['stPane'].hidden, byId['bookPane'].hidden];
    p.panes = [panes()];
    ctx.statsTab('stats');
    p.panes.push(panes());
  } catch (e) { more.statsError = (e && e.stack) || String(e); }

  // ---- W1.2: the per-seat todo strip --------------------------------------
  // Driven through the REAL activity/message events. The named trap is a
  // rendering one: ACT_LOG_MAX removes log.firstChild, so a strip pinned
  // inside the activity log is deleted on exactly the long turns it exists
  // for. The DOM stub parses innerHTML FLAT and never derives a parent's
  // textContent, so content is read out of the element's own _html.
  try {
    const p = more.todo = {};
    const marks = h => (String(h).match(/todo-mark">([^<]*)</g) || [])
      .map(s => s.replace(/todo-mark">|</g, ''));
    const texts = h => (String(h).match(/<\/span> ([^<]*)</g) || [])
      .map(s => s.replace(/<\/span> |</g, ''));
    const head = h => {
      const m = String(h).match(/plan <b>([^<]*)<\/b>/);
      return m ? 'plan ' + m[1] : null;
    };
    await ctx.newChat();
    const feed = byId['feed'];
    const plan = (done, items) => ({items, done, total: items.length});
    const P = plan(1, [{text: 'write it', state: 'done'},
                       {text: 'test it', state: 'active'},
                       {text: 'ship it', state: 'pending'}]);
    const act = (extra) => ctx.uiEvent({event: 'activity', payload: Object.assign(
      {speaker: 0, provider: 'claude', name: 'Claude'}, extra)});
    ctx.uiEvent({event: 'thinking', payload: {speaker: 0, provider: 'claude',
                                              name: 'Claude'}});
    act({kind: 'command', text: '$ pytest'});
    act({kind: 'todo', text: 'plan 1\/3 \u00b7 test it', todo: P});
    const ind = [...feed.children].filter(
      c => (c.className || '').includes('typing')).pop();
    let strip = ind.querySelector('.act-todo');
    p.head = head(strip._html);
    p.marks = marks(strip._html);
    p.texts = texts(strip._html);
    p.classes = strip.querySelectorAll('.todo-item').map(e => e.className);
    // outside .act-log, whose trim removes firstChild
    p.stripOutsideLog = !!strip && !ind.querySelector('.act-log')
      .querySelector('.act-todo');
    // a step is a thing the seat DID; a checklist is a state
    p.stepsAfterTodo = String(ind.dataset.steps || '');
    // 40 more steps drives the real ACT_LOG_MAX trim
    for (let i = 0; i < 40; i++) act({kind: 'command', text: '$ step ' + i});
    act({kind: 'todo', text: 'plan 3\/3', todo: plan(3, [
      {text: 'write it', state: 'done'}, {text: 'test it', state: 'done'},
      {text: 'ship it', state: 'done'}])});
    strip = ind.querySelector('.act-todo');
    p.survivesTrim = !!strip;
    p.afterTrimHead = strip ? head(strip._html) : null;
    p.stripCount = ind.querySelectorAll('.act-todo').length;
    // the finished row: the plan is legible without expanding anything
    const rowHtml = () => {
      const rows = [...feed.children].filter(
        c => (c.className || '').includes('msg'));
      return rows.length ? String(rows[rows.length - 1]._html || '') : '';
    };
    const summaryOf = h => {
      const m = String(h).match(/<summary>([\s\S]*?)<\/summary>/);
      return m ? m[1] : null;
    };
    ctx.uiEvent({event: 'message', payload: {
      speaker: 0, provider: 'claude', name: 'Claude', text: 'done', round: 1,
      message_id: 'm1', activity: [
        {kind: 'command', text: '$ pytest'},
        {kind: 'read', text: 'reading a.py'},
        {kind: 'todo', text: 'plan 1\/3 \u00b7 test it', todo: P}]}});
    p.rowSummary = summaryOf(rowHtml());
    p.rowMarks = marks(rowHtml());
    ctx.uiEvent({event: 'message', payload: {
      speaker: 0, provider: 'claude', name: 'Claude', text: 'planned', round: 2,
      message_id: 'm2', activity: [
        {kind: 'todo', text: 'plan 1\/3', todo: P}]}});
    p.planOnlySummary = summaryOf(rowHtml());
    ctx.uiEvent({event: 'message', payload: {
      speaker: 0, provider: 'claude', name: 'Claude', text: 'x', round: 3,
      message_id: 'm3', activity: [{kind: 'command', text: '$ a'},
                                   {kind: 'command', text: '$ b'}]}});
    p.noPlanSummary = summaryOf(rowHtml());
    // replay: a strip that only appeared live would vanish on reopen
    ctx.addMsg('claude', 'Claude', 'replayed', 'round 4', null,
               '2026-08-27T10:00:00',
               [{kind: 'command', text: '$ pytest'},
                {kind: 'read', text: 'reading a.py'},
                {kind: 'todo', text: 'plan 1\/3', todo: P}], null,
               {message_id: 'm4', delivered_to: [], speaker: 0});
    p.replayMarks = marks(rowHtml());
    // the renderer itself: fallback, empty, escaping, unknown state
    p.fallbackHtml = ctx.actLineHtml({kind: 'todo', text: 'plan 1\/2 x'});
    p.emptyHtml = ctx.todoBlockHtml({items: [], done: 0, total: 0});
    p.escapedHtml = ctx.todoBlockHtml(plan(0, [
      {text: '<img src=x onerror=1>', state: 'pending'}]));
    p.unknownStateMark = marks(ctx.todoBlockHtml(plan(0, [
      {text: 'a', state: 'blocked'}])))[0];
    ctx.hideAllTyping();
  } catch (e) { more.todoError = (e && e.stack) || String(e); }

  // ---- W1.4: the honest context readout -----------------------------------
  // Driven through the REAL message event and the real seat cards. The DOM
  // stub parses innerHTML FLAT and never derives a parent's textContent, so
  // content is read out of the element's own _html.
  try {
    const p = more.ctx = {};
    const strip = h => {
      const html = String(h || '');
      const open = html.indexOf('>'), close = html.lastIndexOf('<');
      return open < 0 || close <= open ? html : html.slice(open + 1, close);
    };
    p.pillWithWindow = strip(ctx.contextPill(
      {context_used: 41616, context_window: 200000}));
    p.pillNoWindow = strip(ctx.contextPill({context_used: 14701}));
    p.pillNone = ctx.contextPill(null);
    p.pillZero = ctx.contextPill({context_used: 0, context_window: 200000});
    p.short = [842, 1234, 41616, 200000, 1500000, 12000000]
      .map(n => ctx.shortTokens(n));
    p.shortJunk = [ctx.shortTokens('lots'), ctx.shortTokens(-4),
                   ctx.shortTokens(null)];
    await ctx.newChat();
    const card = () => document.getElementById('seat-0');
    const box = () => card().querySelector('.seat-ctx');
    const cardText = () => {
      const b = box();
      if (!b || !(b.className || '').includes('show')) return '';
      const m = String(b._html || '').match(/ctx-text">([^<]*)</);
      return m ? m[1] : '';
    };
    const row = (n, extra) => ctx.uiEvent({event: 'message', payload:
      Object.assign({speaker: 0, provider: 'claude', name: 'Claude',
                     text: 'r' + n, round: n, message_id: 'c' + n}, extra)});
    row(1, {context: {context_used: 41616, context_window: 200000}});
    p.barWithWindow = String(box()._html || '').indexOf('ctx-fill') >= 0;
    p.textWithWindow = cardText();
    p.tightAtHalf = (box().className || '').includes('tight');
    row(2, {context: {context_used: 50000, context_window: 200000}});
    p.afterTwoRows = cardText();
    row(3, {context: {context_used: 180000, context_window: 200000}});
    p.tightAtNinety = (box().className || '').includes('tight');
    row(4, {context: {context_used: 900000, context_window: 200000}});
    p.overFullWidth = (String(box()._html || '')
      .match(/width:(\d+%)/) || [])[1] || null;
    row(5, {context: {context_used: 14701}});
    p.barNoWindow = String(box()._html || '').indexOf('ctx-fill') >= 0;
    p.textNoWindow = cardText();
    ctx.addMsg('claude', 'Claude', 'replayed', 'round 6', null,
               '2026-08-27T10:00:00', null, null,
               {message_id: 'c6', delivered_to: [], speaker: 0,
                context: {context_used: 33000, context_window: 200000}});
    p.afterReplay = cardText();
    // A masked blind-duel row must not repaint the card it just anonymised.
    // Driven through the REAL masking path (syncBattleUI + intent:"battle"),
    // because a hand-set row field sets no dataset.realName at all and the
    // probe would pass whether the guard existed or not.
    ctx.syncBattleUI({battle: {state: 'awaiting', slots: [0, 1]}});
    p.maskWorks = false;
    ctx.addMsg('claude', 'Claude', 'masked', 'round 7', null,
               '2026-08-27T10:01:00', null, null,
               {message_id: 'c7', delivered_to: [], speaker: 0,
                intent: 'battle',
                context: {context_used: 99000, context_window: 200000}});
    const masked = [...byId['feed'].children].filter(
      c => (c.className || '').includes('msg')).pop();
    p.maskWorks = (masked.className || '').includes('msg-masked');
    p.afterMasked = cardText();
    ctx.syncBattleUI({});
    // reopening a DIFFERENT chat must not leave this one's numbers behind:
    // the stub's open_session replies with rows that carry no context at
    // all, so nothing in the replay would overwrite them
    row(8, {context: {context_used: 60000, context_window: 200000}});
    p.beforeReopen = cardText();
    await ctx.openChat('some-other-chat');
    p.afterReopen = cardText();
    await ctx.newChat();
    p.afterNewChat = cardText();
  } catch (e) { more.ctxError = (e && e.stack) || String(e); }

  // ---- W1.3: the usage pill's clock ---------------------------------------
  // claude reports duration_ms, codex reports neither duration_ms nor cost —
  // so before wall_ms every GPT row showed no time at all.
  try {
    const p = more.usagePill = {};
    const pill = u => {
      const html = String(ctx.formatUsage(u) || '');
      const open = html.indexOf('>'), close = html.lastIndexOf('<');
      return open < 0 || close <= open ? html : html.slice(open + 1, close);
    };
    p.none = pill(null);
    p.claude = pill({cost_usd: 0.25512, input_tokens: 4,
                     output_tokens: 3893, total_tokens: 3897,
                     duration_ms: 60905, wall_ms: 61500});
    p.codex = pill({input_tokens: 18050, output_tokens: 6,
                    total_tokens: 18056, duration_ms: null, wall_ms: 4231});
    p.neitherClock = pill({input_tokens: 5, output_tokens: 1,
                           total_tokens: 6});
    p.nothingReported = pill({});
  } catch (e) { more.usagePillError = (e && e.stack) || String(e); }
  // ---- W2.2: the board-review switch is offered only where it works --------
  // A checkbox that silently does nothing in four of six modes is the shape
  // this repo calls "a control that does nothing"; the rule is to disable it
  // with a stated reason, never to hide it.
  try {
    const p = more.boardSwitch = {};
    await ctx.newChat();
    const box = byId['boardReview'];
    ctx.applyPreset('open_discussion');
    p.offInConversation = !!box.disabled;
    p.reasonInConversation = (box.parentElement || box).title;
    ctx.applyPreset('build_execute');   // the KEY; the label is "Build Together"
    p.onInBuildTogether = !box.disabled;
    p.reasonInBuildTogether = (box.parentElement || box).title;
    // ...but a switch that is ON is never taken away: that is how moderation
    // once got stuck on with no way to turn it off
    box.checked = true;
    ctx.applyPreset('open_discussion');
    p.stillEditableWhenOn = !box.disabled;
    box.checked = false;
    ctx.applyPreset('open_discussion');
    await ctx.newChat();
  } catch (e) { more.boardSwitchError = (e && e.stack) || String(e); }

  // ---- W2.2: the Supervisor board review -----------------------------------
  // A separate card, event and bridge call from Plan Mode's, sharing only the
  // stylesheet. What the card sends has to match what merge_board_edits
  // accepts, and what it OFFERS has to match what that whitelist allows.
  try {
    const p = more.board = {};
    await ctx.newChat();
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-board', title: 'Board', participants: [],
                mode: 'supervisor', can_continue: false},
      transcript: 'x/transcript.md', workspace: '', mode: 'supervisor'}});
    const BOARD = {
      id: 'board-1', revision: 1, phase: 'proposed', wave: 2,
      goal: 'make it better',
      seats: [{id: 0, name: 'Claude', writes: true},
              {id: 1, name: 'Gemini', writes: false}],
      tasks: [{id: 't1', owner: 0, brief: 'write a.py', files: ['a.py'],
               deps: [], status: 'pending', replans: 0},
              {id: 't2', owner: 1, brief: 'write b.py', files: [],
               deps: [], status: 'pending', replans: 0}],
    };
    ctx.uiEvent({event: 'board', payload: {...BOARD, chat_id: 'sess-board'}});
    // the gate's question routes to the CARD, never the generic modal — the
    // modal answers with a STRING and board_gate reads Josh's edits off a dict
    ctx.uiEvent({event: 'question', payload: {
      chat_id: 'sess-board', qid: 'bq1', kind: 'board', asker: 'Supervisor',
      question: 'Approve the board?', options: ['Approve & dispatch'],
      tasks: BOARD.tasks}});
    p.askModalOpened = byId['askModal'].classList.contains('show');
    const card = () => document.querySelector('.plan-card[data-active-board]');
    p.cardPresent = !!card();
    p.phase = card().dataset.phase;
    const rows = () => [...card().querySelector('.plan-tasks').children];
    p.rowCount = rows().length;
    // the owner picker offers the seats the ENGINE published, and says which
    // of them can actually deliver a file
    p.ownerOptions = [...rows()[0].querySelector('.plan-owner').options]
      .map(o => o.value + ':' + o.textContent);
    p.filesShown = deepText(rows()[0].querySelector('.task-state'));
    // ...and it offers no way to edit the file claims or the dependencies
    p.hasFileInput = !!card().querySelector('.plan-files');

    // drop t2, reword and reassign t1, then approve
    rows()[1].querySelector('.plan-include').checked = false;
    rows()[0].querySelector('.plan-title').value = 'write a.py properly';
    rows()[0].querySelector('.plan-owner').value = '1';
    boardCalls.length = 0;
    const acts = card().querySelector('.plan-actions');
    const approve = [...acts.children].filter(
      c => String(c.className || '').includes('plan-approve'))[0];
    await approve.onclick();
    await new Promise(r => setImmediate(r));
    p.approveCall = boardCalls[0];
    p.phaseAfterApprove = card().dataset.phase;

    // ...and a refusal asks WHY before it sends
    ctx.uiEvent({event: 'board', payload: {...BOARD, chat_id: 'sess-board'}});
    ctx.uiEvent({event: 'question', payload: {
      chat_id: 'sess-board', qid: 'bq2', kind: 'board', asker: 'Supervisor',
      question: 'Approve the board?', options: ['Approve & dispatch']}});
    boardCalls.length = 0;
    const back = [...card().querySelector('.plan-actions').children]
      .filter(c => deepText(c) === 'Send it back')[0];
    const pending = back.onclick();
    await new Promise(r => setImmediate(r));
    const note = card().querySelector('textarea');
    p.notePrompted = !!note;
    note.value = 'too broad';
    card().querySelectorAll('button')
      .filter(b => deepText(b) === 'Send back')[0].onclick();
    await pending;
    await new Promise(r => setImmediate(r));
    p.refuseCall = boardCalls[0];
    await ctx.newChat();
  } catch (e) { more.boardError = (e && e.stack) || String(e); }

  // ---- W2.3: the background jobs badge and popover -------------------------
  // The badge counts every chat this window holds, including the ones whose
  // rows never reach the transcript on screen — which is the whole point, and
  // the reason its repaint is hoisted above uiEvent's not-my-chat return.
  try {
    const p = more.jobs = {};
    await ctx.newChat();
    // Earlier probes left live runs in the shared chatRuns map, and the badge
    // counts every chat this window holds — which is exactly right in the
    // app and exactly wrong for an isolated assertion.
    vm.runInContext('chatRuns.clear()', ctx);
    ctx.renderJobsBadge();
    const bar = byId['jobsBar'], pop = byId['jobsPop'];
    p.hiddenWithNothingLive = !!bar.hidden;
    // a BACKGROUND chat starts: nothing of it reaches this transcript
    ctx.uiEvent({event: 'started', payload: {
      background: true,
      session: {id: 'sess-bgjob', title: 'From a script', participants: [],
                mode: 'round_robin', can_continue: false},
      transcript: 'y/transcript.md', workspace: '', mode: 'round_robin'}});
    p.shownForBackground = !bar.hidden;
    p.labelForBackground = deepText(byId['jobsLabel']);
    // ...and a question in it flips the badge to attention
    ctx.uiEvent({event: 'question', payload: {
      chat_id: 'sess-bgjob', qid: 'q1', question: 'which one?', options: []}});
    p.labelWhenWaiting = deepText(byId['jobsLabel']);
    p.attentionClass = byId['jobsBtn'].classList.contains('attention');
    ctx.uiEvent({event: 'question_done', payload: {chat_id: 'sess-bgjob', qid: 'q1'}});

    // the popover reads its clocks from the bridge, because a background
    // chat's `thinking` never reaches this transcript
    jobsReply = {now: 2000, jobs: [{
      chat_id: 'sess-bgjob', status: 'thinking', running: true,
      background: true, pending_ask: null, queued: 2,
      thinking: [
        // no duration bound: age only ("of X" here would be the 0:00 of
        // 15:00 lie)
        {speaker: 0, provider: 'claude', name: 'Claude',
         limit: null, idle: 300, started: 1900},
        // a real deadline: count to it
        {speaker: 1, provider: 'gemini', name: 'Gemini',
         limit: 600, idle: 600, started: 1880},
      ],
      working: [{id: 'w1', phase: 'plan', what: 'Planning the work',
                 detail: '', started: 1970}],
    }]};
    await ctx.toggleJobs();
    await new Promise(r => setImmediate(r));
    p.popShown = !pop.hidden;
    const rows = () => [...byId['jobsList'].children]
      .filter(c => String(c.className || '').split(/\s+/).includes('job-row'));
    p.rowCount = rows().length;
    p.rowText = deepText(rows()[0]);
    p.clocks = [...rows()[0].children]
      .filter(c => String(c.className || '').includes('job-line'))
      .map(deepText);
    // clicking a row opens that chat
    apiCalls.length = 0;
    rows()[0].onclick({stopPropagation() {}});
    await new Promise(r => setImmediate(r));
    p.openedOnClick = apiCalls.includes('open_session');
    p.popClosedAfterClick = !!pop.hidden;
    jobsReply = {jobs: [], now: 1000};
    // Starting a NEW chat must not hide it: the background one is still
    // running, which is the entire reason this bar exists.
    await ctx.newChat();
    p.stillShownAfterNewChat = !byId['jobsBar'].hidden;
    // ...and it goes away when that chat actually finishes
    ctx.uiEvent({event: 'done', payload: {
      chat_id: 'sess-bgjob', background: true, transcript: null,
      session: {id: 'sess-bgjob'}, can_continue: true}});
    p.hiddenOnceItFinished = !!byId['jobsBar'].hidden;
  } catch (e) { more.jobsError = (e && e.stack) || String(e); }

  // ---- W2.4: the Master goal chip shows the MANAGER's goal -----------------
  // `goal` on a session summary is meta["topic"] — the words Josh typed to
  // open the chat — so a supervised run showed its opening message as its
  // master goal for the whole conversation, and a Keep Improving run on its
  // third objective showed the first thing anyone ever said.
  try {
    const p = more.goalChip = {};
    await ctx.newChat();
    const chip = () => deepText(byId['supervisorGoal']);
    ctx.renderSupervisorOverview('', [], '');
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-goal', title: 'Goal', participants: [],
                mode: 'supervisor', can_continue: false,
                goal: 'the opening message Josh typed',
                supervisor_goal: 'what the manager is actually working on'},
      transcript: 'x/transcript.md', workspace: '', mode: 'supervisor'}});
    p.afterStarted = chip();
    // a trace entry carrying a goal repaints it live — that is how a
    // /objective re-target reaches the chip
    ctx.uiEvent({event: 'supervisor', payload: {
      chat_id: 'sess-goal',
      entry: {id: 'e1', type: 'objective_set', phase: 'objective', wave: 1,
              title: 'Josh set the objective', detail: 'ship the docs',
              goal: 'ship the docs'}}});
    p.afterRetarget = chip();
    // and a chat with no manager goal at all still falls back to the opener
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-goal2', title: 'Goal2', participants: [],
                mode: 'supervisor', can_continue: false,
                goal: 'only an opener here'},
      transcript: 'x/transcript.md', workspace: '', mode: 'supervisor'}});
    p.fallback = chip();
    // ...and the REOPEN path, which is a second call site: openChat passes
    // the summary's fields straight to renderSupervisorOverview, so fixing
    // only runFor left a reopened chat still showing its opener
    openSessionExtra = {mode: 'supervisor',
                        goal: 'the opening message Josh typed',
                        supervisor_goal: 'what the manager settled on'};
    await ctx.openChat('sess-goal-reopen');
    p.afterReopen = chip();
    openSessionExtra = {};
    await ctx.newChat();
  } catch (e) { more.goalChipError = (e && e.stack) || String(e); }

  // ---- W2.1: the queue dock, driven like a user ---------------------------
  // Things only an executed page can see: that Enter still SENDS while
  // Ctrl+Enter holds, that an edit to a held row is what actually goes out,
  // and that a drop happens in two steps and names what it cannot undo.
  try {
    const p = more.dock = {};
    await ctx.newChat();
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-dock', title: 'Dock', participants: [],
                mode: 'round_robin', can_continue: false},
      transcript: 'x/transcript.md', workspace: '', mode: 'round_robin'}});
    const dock = byId['queueDock'];
    p.hiddenWithNothingQueued = !!dock.hidden;
    p.buttonOfferedWhileRunning = !byId['queueBtn'].hidden;

    // plain Enter still delivers at once
    dockCalls.length = 0;
    byId['say'].value = 'send me now';
    await ctx.sendSay();
    p.enterCalls = dockCalls.map(c => c[0]);

    // the queue gesture holds instead — driven through the REAL keydown
    // binding, not by calling queueSay directly, which would pass whether the
    // key was bound at all
    const key = (el, ev) => {
      let stopped = false;
      const e = Object.assign({key: 'Enter', ctrlKey: false, metaKey: false,
                               shiftKey: false, type: 'keydown',
                               preventDefault() { stopped = true; }}, ev);
      ((el._listeners || {}).keydown || []).slice().forEach(fn => fn(e));
      return stopped;
    };
    dockCalls.length = 0;
    byId['say'].value = 'hold me';
    p.ctrlEnterPrevented = key(byId['say'], {ctrlKey: true});
    await new Promise(r => setImmediate(r));   // queueSay is async
    p.queueCalls = dockCalls.map(c => c[0]);
    // ...while a plain Enter on the same box still SENDS
    dockCalls.length = 0;
    byId['say'].value = 'plain enter';
    key(byId['say'], {});
    await new Promise(r => setImmediate(r));
    p.plainEnterCalls = dockCalls.map(c => c[0]);
    dockCalls.length = 0;
    p.sayClearedAfterQueue = byId['say'].value === '';
    const rows = () => [...byId['queueList'].children]
      .filter(c => String(c.className || '').split(/\s+/).includes('q-row'));
    p.rowCount = rows().length;
    p.shownOnceQueued = !dock.hidden;
    p.rowIsTextarea = (rows()[0].querySelector('.q-text') || {}).tag;

    // editing the row is what gets sent, not what was typed
    const ta = rows()[0].querySelector('.q-text');
    ta.value = 'hold me, edited';
    ta.oninput();
    dockCalls.length = 0;
    await ctx.sendQueued(rows()[0].dataset.qid);
    p.sentText = (dockCalls.find(c => c[0] === 'interject') || [])[1];
    p.emptyAfterSend = rows().length;
    p.hiddenAfterSend = !!dock.hidden;

    // a slash line is refused rather than queued
    dockCalls.length = 0;
    byId['say'].value = '/compact';
    await ctx.queueSay();
    p.slashCalls = dockCalls.map(c => c[0]);
    p.slashNote = deepText(byId['queueNote']);
    byId['say'].value = '';

    // dropping takes two clicks, and the arm names the files it leaves
    prepareReply = {ok: true, attached: 1,
                    text: 'with a file\n\n[Josh attached a file: C:\\w\\a.png]'};
    byId['say'].value = 'with a file';
    await ctx.queueSay();
    const row = rows()[0];
    p.attChips = [...((row.querySelector('.q-att') || {}).children || [])]
      .map(c => c.textContent);
    p.proseOnlyInBox = row.querySelector('.q-text').value;
    const del = row.querySelector('.q-del');
    del.onclick();
    p.armLabel = del.textContent;
    p.rowsAfterArm = rows().length;
    del.onclick();
    p.rowsAfterConfirm = rows().length;
    prepareReply = null;

    // reordering, and what SEND ALL then delivers
    dockCalls.length = 0;
    byId['say'].value = 'first';
    await ctx.queueSay();
    byId['say'].value = 'second';
    await ctx.queueSay();
    const twoRows = rows();
    p.orderBefore = twoRows.map(r => r.querySelector('.q-text').value);
    // the second row's "move up"
    twoRows[1].querySelector('.q-acts').children[0].onclick();
    p.orderAfter = rows().map(r => r.querySelector('.q-text').value);
    dockCalls.length = 0;
    await ctx.sendAllQueued();
    p.sendAllOrder = dockCalls.filter(c => c[0] === 'interject').map(c => c[1]);
    p.emptyAfterSendAll = rows().length;

    // ...and that the rows survive a reload with ids nothing will collide with
    byId['say'].value = 'kept across a reload';
    await ctx.queueSay();
    p.persisted = JSON.parse(sandbox.localStorage.getItem('alloyQueued') || '{}');
    rows().forEach(r => ctx.dropQueued(r.dataset.qid, 'sess-dock'));

    // Josh switches chats while the send is in flight: the row must leave the
    // chat it was SENT to, and the echo must not be painted into the other
    // one's transcript
    byId['say'].value = 'sent while switching away';
    await ctx.queueSay();
    const switching = rows()[0].dataset.qid;
    const feedWas = (byId['feed'].children || []).length;
    interjectSwitchesChatTo = 'sess-elsewhere';
    await ctx.sendQueued(switching);
    interjectSwitchesChatTo = null;
    p.switchedAwayLeftBehind =
      (vm.runInContext("(queued.get('sess-dock') || []).length", ctx));
    p.switchedAwayEchoed = (byId['feed'].children || []).length - feedWas;
    vm.runInContext("activeId = 'sess-dock'", ctx);
    ctx.renderQueueDock();

    byId['say'].value = 'for sess-dock';
    await ctx.queueSay();
    p.rowsBeforeSwitch = rows().length;
    await ctx.openChat('some-other-chat');
    p.rowsInOtherChat = rows().length;
    p.dockHiddenInOtherChat = !!dock.hidden;
    await ctx.newChat();
  } catch (e) { more.dockError = (e && e.stack) || String(e); }

  // ---- W2.0: a background chat must not yank the visible transcript -------
  // The webhook (and every scheduled room after it) starts a conversation
  // while Josh is reading something else. `started` used to do
  // `activeId = id; openTab(id)` unconditionally.
  try {
    const p = more.bg = {};
    await ctx.newChat();
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-mine', title: 'Mine', participants: [],
                mode: 'round_robin', can_continue: false},
      transcript: 'x/transcript.md', workspace: '', mode: 'round_robin'}});
    // top-level `let`s live in the context's LEXICAL scope, not on the
    // sandbox object, so `ctx.activeId` is undefined however healthy the page
    // is. Evaluating in the same context is the only honest read.
    const peek = expr => vm.runInContext(expr, ctx);
    p.activeAfterMine = peek('activeId');
    p.tabsAfterMine = peek('tabs.map(t => t.id)');
    const feedBefore = (byId['feed'].children || []).length;
    ctx.uiEvent({event: 'started', payload: {
      background: true,
      session: {id: 'sess-bg', title: 'From a script', participants: [],
                mode: 'round_robin', can_continue: false},
      transcript: 'y/transcript.md', workspace: '', mode: 'round_robin'}});
    p.activeAfterBg = peek('activeId');
    p.tabsAfterBg = peek('tabs.map(t => t.id)');
    // it still earns a rail row and a status of its own
    p.knownToTheRail = peek("sessions.some(s => s.id === 'sess-bg')");
    p.bgStatus = (ctx.runFor('sess-bg') || {}).status;
    p.mineStatus = (ctx.runFor('sess-mine') || {}).status;
    // ...and its transcript rows stay out of the one on screen
    ctx.uiEvent({event: 'message', payload: {
      chat_id: 'sess-bg', speaker: 0, provider: 'claude', name: 'Claude',
      text: 'work from the background chat', round: 1}});
    p.feedGrewFromBg = (byId['feed'].children || []).length - feedBefore;
    // an event from a background chat that has NO id yet (its setup runs
    // before the session dir exists) must be dropped, not painted here
    ctx.uiEvent({event: 'status', payload: {
      background: true, text: 'a background chat is setting up'}});
    p.feedGrewFromAnonymousBg =
      (byId['feed'].children || []).length - feedBefore;
    // the same event WITHOUT the stamp is the visible chat's own setup
    ctx.uiEvent({event: 'status', payload: {text: 'my own setup'}});
    p.feedGrewFromMyOwnStatus =
      (byId['feed'].children || []).length - feedBefore;
    await ctx.newChat();
  } catch (e) { more.bgError = (e && e.stack) || String(e); }

  // ---- what the adversarial pass found ------------------------------------
  try {
    const p = more.advPass = {};   // NOT `adv` — the rung-advisory probe owns that key
    await ctx.newChat();

    // (a) the jobs clock measures SILENCE against a real last-activity
    // stamp. Feeding it the turn's start makes quiet == the whole age, so a
    // healthy long turn renders red "quiet M:SS of Y". Driven through the
    // POPOVER, not through turnClockText: calling the shared function with
    // hand-picked arguments passes whichever argument renderJobsList sends.
    p.quietFromLastact = ctx.turnClockText(1000, 400, null, 300, 995).text;
    p.quietFromStart = ctx.turnClockText(1000, 400, null, 300, 400).text;
    vm.runInContext('chatRuns.clear()', ctx);   // only sess-clock may be live
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-clock', title: 'Clock', participants: [],
                mode: 'round_robin', can_continue: false},
      transcript: 'x/transcript.md', workspace: '', mode: 'round_robin'}});
    ctx.uiEvent({event: 'thinking', payload: {
      chat_id: 'sess-clock', speaker: 0, provider: 'claude', name: 'Claude'}});
    // 151 s into a turn with a 300 s idle window and NO deadline: past the
    // half-window threshold, but streaming the whole time
    jobsReply = {now: 2000, jobs: [{
      chat_id: 'sess-clock', status: 'thinking', running: true,
      background: false, pending_ask: null, queued: 0, working: [],
      thinking: [{speaker: 0, provider: 'claude', name: 'Claude',
                  limit: null, idle: 300, started: 1849, lastact: 1999}]}]};
    await ctx.toggleJobs();
    await new Promise(r => setImmediate(r));
    p.busySeatLine = deepText([...byId['jobsList'].children]
      .filter(c => String(c.className || '').includes('job-row'))[0]);
    // ...and one that really HAS gone quiet still says so
    jobsReply.jobs[0].thinking[0].lastact = 1849;
    await ctx.refreshJobs();
    p.quietSeatLine = deepText([...byId['jobsList'].children]
      .filter(c => String(c.className || '').includes('job-row'))[0]);
    ctx.closeJobs();
    jobsReply = {jobs: [], now: 1000};

    // (b) a queued row minted after a restore does not reuse an id
    ctx.queueRestore({'some-chat': [{id: 'q7', text: 'restored', atts: []}]});
    p.seqAfterRestore = vm.runInContext('queueSeq', ctx);

    // (c) an engine `board` event repaints the card on its own — without it
    // an expired gate leaves a live "Approve & dispatch" on screen
    ctx.uiEvent({event: 'started', payload: {
      session: {id: 'sess-adv', title: 'Adv', participants: [],
                mode: 'supervisor', can_continue: false},
      transcript: 'x/transcript.md', workspace: '', mode: 'supervisor'}});
    ctx.uiEvent({event: 'board', payload: {
      chat_id: 'sess-adv', id: 'b1', revision: 1, phase: 'proposed', wave: 1,
      goal: 'g', seats: [{id: 0, name: 'Claude', writes: true}],
      tasks: [{id: 't1', owner: 0, brief: 'do it', files: [], deps: [],
               status: 'pending'}]}});
    const card = () => document.querySelector('.plan-card[data-active-board]');
    p.cardFromEventPhase = card() ? card().dataset.phase : null;
    ctx.uiEvent({event: 'board', payload: {
      chat_id: 'sess-adv', id: 'b1', revision: 1, phase: 'declined', wave: 1,
      goal: 'g', seats: [], tasks: []}});
    p.cardAfterEngineDecline = card().dataset.phase;
    p.declinedNote = deepText(card().querySelector('.plan-note'));

    // (d) a REOPENED chat's board reaches renderBoard. Every run is created
    // first by renderChats from a rail row with no board, so a
    // constructor-only seed could never arrive.
    ctx.runFor('sess-adv2');                    // created bare, like the rail
    const run = ctx.runFor('sess-adv2', {board: {
      id: 'b9', revision: 1, phase: 'approved', wave: 2, goal: 'g', seats: [],
      tasks: [{id: 't9', owner: 0, brief: 'later', files: [], deps: [],
               status: 'done'}]}});
    p.reopenedBoardSeeded = !!(run && run.board);

    // (e) a fresh stage repaints the jobs badge. Asserting it stays SHOWN
    // passes whether or not anything repaints, because nothing else hides it
    // — so this finishes the chats first and asserts it goes AWAY.
    p.barBeforeNewChat = !byId['jobsBar'].hidden;
    vm.runInContext("chatRuns.forEach(r => { r.status = 'done'; "
                    + "r.question = null; })", ctx);
    await ctx.newChat();
    p.barAfterNewChat = !byId['jobsBar'].hidden;
  } catch (e) { more.advPassError = (e && e.stack) || String(e); }

  // ---- W4: scheduled rooms, driven through the REAL modal ----------------
  try {
    const p = more.sched = {};

    // (a) the hooks modal renders from the BRIDGE's event list and saves what
    // it rendered. `future_thing` is deliberately absent from hookLabels: the
    // old hookSave walked that table, so a row Python knew about displayed and
    // was silently dropped on Save.
    hooksReply = {ok: true, hooks: {},
                  events: ['question', 'checkin', 'done', 'gate_red',
                           'scheduled', 'future_thing']};
    await ctx.openHooks();
    p.hookRowIds = vm.runInContext('hookRowIds', ctx).slice();
    p.scheduledLabel = byId['hookRows'].children
      .map(r => (r.children[0] || {}).textContent).join('|');
    // `document.getElementById`, exactly as the page's own $() does:
    // dynamically built elements never enter byId (only setAttribute
    // registers there), and the stub's getElementById already walks
    // the live tree for them
    document.getElementById('hook-scheduled').value = 'notify-me';
    document.getElementById('hook-future_thing').value = 'later';
    await byId['hookSave'].onclick();
    p.hookSaved = hookSaves[hookSaves.length - 1];
    hooksReply = null;
    ctx.closeHooks();

    // (b) the schedule list
    schedReply = {ok: true, poll_seconds: 30, rooms: ['Quiet', 'Loud'],
                  schedules: [
      {id: 's1', name: 'Nightly', room: 'Quiet', prompt: 'go', turns: 6,
       kind: 'daily', at: '01:00', days: [], every_min: 0, start: '',
       enabled: true, next_run: '2026-08-28T01:00:00', last_run: '',
       last_result: '', runs: 0, misses: 0, ack: null,
       describe: 'Every day at 01:00', grants: [], ack_gap: [],
       ack_sentences: [], missing_room: false},
      {id: 's2', name: 'Loud one', room: 'Loud', prompt: 'go', turns: 6,
       kind: 'weekly', at: '02:00', days: [0, 4], every_min: 0, start: '',
       enabled: false, next_run: '', last_run: '2026-08-20T02:00:00',
       last_result: 'Missed 2026-08-20 02:00 — Alloy was not running.',
       runs: 1, misses: 2,
       ack: {grants: ['connectors'], at: '2026-08-01T00:00:00'},
       describe: 'Every Mon, Fri at 02:00',
       grants: ['permission_full', 'connectors'],
       ack_gap: ['permission_full'],
       ack_sentences: ['write, delete and run anything'],
       missing_room: false},
    ]};
    riskReply = {ok: true, grants: [], sentences: [], notes: []};
    await ctx.openSched();
    p.modalShown = byId['schedModal'].classList.contains('show');
    p.rowCount = byId['schedRows'].children.length;
    p.rowNames = byId['schedRows'].children
      .map(r => (r.children[0] || {}).textContent);
    p.roomOptions = byId['schedRoom'].options.map(o => o.value);
    p.emptyHidden = !!byId['schedEmpty'].hidden;
    p.pollText = byId['schedPoll'].textContent;
    // a PAUSED row must not be silently re-armed by an edit
    p.editEnabledBefore = vm.runInContext('schedEditEnabled', ctx);
    // the second row's warning: it is armed against a room that has widened
    p.warnText = byId['schedRows'].children[1].children
      .filter(c => (c.className || '').indexOf('sched-foot') >= 0)
      .map(c => c.textContent).join(' ');
    // a paused row is marked as such rather than reading as armed
    p.secondRowClass = byId['schedRows'].children[1].className;

    // (c) editing a row whose room WIDENED must not pre-tick the box
    ctx.schedEdit(schedReply.schedules[1]);
    p.ackAfterGap = !!byId['schedAck'].checked;
    p.editHeading = byId['schedEditH'].textContent;
    // the stub's selector engine really does descendants now; a 0 here
    // means schedDaysPicked() is reading nothing and every weekly
    // assertion below is vacuous
    p.dayBoxes = document.querySelectorAll('#schedDays .sched-day').length;
    p.daysAfterEdit = Array.from(
      byId['schedDays'].children.map(l => l.children[0]))
      .filter(cb => cb.checked).map(cb => cb.value);
    // ...and one whose ack still covers the room does
    const covered = Object.assign({}, schedReply.schedules[1],
                                  {ack_gap: []});
    ctx.schedEdit(covered);
    p.ackWhenCovered = !!byId['schedAck'].checked;
    p.editEnabledAfterPausedEdit = vm.runInContext('schedEditEnabled', ctx);
    // ...and the PAYLOAD is the artefact the bridge consumes: asserting
    // the variable alone cannot see a hardcoded `enabled: true` one line
    // further down
    const beforePaused = schedCalls.length;
    await byId['schedSave'].onclick();
    p.pausedEditPayload = schedCalls.length > beforePaused
      ? schedCalls[schedCalls.length - 1][1].enabled : 'not saved';

    // (d) the kind rows follow the picker
    ctx.schedClear();
    p.kindRows = {};
    for (const kind of ['daily', 'weekly', 'interval', 'once']) {
      byId['schedKind'].value = kind;
      byId['schedKind'].onchange();
      p.kindRows[kind] = [byId['schedAtRow'].hidden,
                          byId['schedDaysRow'].hidden,
                          byId['schedEveryRow'].hidden,
                          byId['schedOnceRow'].hidden].map(Boolean);
    }

    // (e) a risky room shows its grants and REFUSES a save with no tick
    byId['schedKind'].value = 'daily';
    byId['schedKind'].onchange();
    riskReply = {ok: true, grants: ['permission_full'],
                 sentences: ['write, delete and run anything on this machine'],
                 notes: ['Desktop control is set to Ask.']};
    byId['schedRoom'].value = 'Loud';
    await byId['schedRoom'].onchange();
    p.grantsHidden = !!byId['schedGrants'].hidden;
    p.grantLines = byId['schedGrantList'].children.map(li => li.textContent);
    p.ackText = byId['schedAckText'].textContent;
    p.noteLines = byId['schedNotes'].children.map(d => d.textContent);
    byId['schedName'].value = 'Nightly';
    byId['schedPrompt'].value = 'do the thing';
    const before = schedCalls.length;
    await byId['schedSave'].onclick();
    p.savedWithoutTick = schedCalls.length - before;
    p.refusal = byId['schedNote'].textContent;
    // ...and lets it through once ticked, carrying the ack
    byId['schedAck'].checked = true;
    await byId['schedSave'].onclick();
    const call = schedCalls[schedCalls.length - 1];
    p.savedEnabled = call[1].enabled;
    p.savedName = call[0];
    p.savedSpec = call[1];

    // (f) changing the recurrence AFTER ticking un-earns the tick: the
    // sentence is what he agreed to, and it no longer says the same
    // thing. Found live 2026-08-27 -- a box ticked against "every day
    // at 01:00" saved "every Mon, Fri at 02:30".
    // saveSched kicks off schedClear()/loadSched() without awaiting them,
    // so let those settle and paint once before ticking: the guard keys
    // on the sentence that was LAST PAINTED, and a form nobody has
    // painted yet has no sentence to differ from
    await new Promise(r => setTimeout(r, 5));
    ctx.schedPaintAck();
    byId['schedAck'].checked = true;
    p.ackTextBeforeChange = byId['schedAckText'].textContent;
    byId['schedAt'].value = '04:45';
    byId['schedAt'].oninput();
    p.ackAfterTimeChange = !!byId['schedAck'].checked;
    p.ackTextAfterChange = byId['schedAckText'].textContent;
    p.changeNote = byId['schedNote'].textContent;
    // ...and a repaint that changes NOTHING must not un-tick it
    byId['schedAck'].checked = true;
    byId['schedAt'].oninput();
    p.ackAfterNoChange = !!byId['schedAck'].checked;

    // (f) an innocuous room hides the block again
    riskReply = {ok: true, grants: [], sentences: [], notes: []};
    byId['schedRoom'].value = 'Quiet';
    await byId['schedRoom'].onchange();
    p.grantsHiddenAgain = !!byId['schedGrants'].hidden;

    ctx.closeSched();

    // (g) the `scheduled` event reaches the banner — the ONLY thing on screen
    // that ever mentions a run that did not happen
    ctx.uiEvent({event: 'scheduled', payload: {
      id: 's1', name: 'Nightly', room: 'Quiet', started: false,
      text: 'Nightly — Skipped: another conversation was still running.'}});
    p.bannerShown = byId['contBanner'].classList.contains('show');
    p.bannerText = deepText(byId['contBanner']);
  } catch (e) { more.schedError = (e && e.stack) || String(e); }

  // Put the boot roster back: report() reads the LIVE DOM, so leaving the
  // stage solo would hand every other test in this file a one-seat page.
  // (bootSeats above is the record of what boot itself built.)
  try { ctx.addSeat('gpt'); ctx.addSeat('gemini'); } catch (e) {}

  report({bootRan: fns.length > 0, bootError, more});
})();
"""


def boot(html_path, workdir, extra_env=None):
    """Run the UI script headlessly; return the harness's JSON report."""
    with open(os.path.join(workdir, "dom.js"), "w", encoding="utf-8") as f:
        f.write(DOM_JS)
    with open(os.path.join(workdir, "boot.js"), "w", encoding="utf-8") as f:
        f.write(BOOT_JS)
    env = dict(os.environ, STUB_CONFIG=json.dumps(STUB_CONFIG))
    env.update(extra_env or {})
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
class SoloStageUiTests(unittest.TestCase):
    """One agent on the stage, driven through the REAL script.

    Static source guards live in tests/test_solo.py; these are the ones only
    an executing page can answer -- that the rail really reaches one seat,
    that the mode menu repaints itself for that roster, and that a refused row
    cannot be clicked into the state anyway."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.solo = boot(UI, cls._tmp.name)["solo"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_rail_really_reaches_one_seat(self):
        self.assertEqual(self.solo["seatsLeft"], 1)
        self.assertTrue(self.solo["isSolo"])

    def test_the_modes_that_still_work_stay_offered(self):
        # panel included: draft -> self-critique -> synthesis is a real
        # technique, the engine runs it solo, and refusing it only in the UI
        # meant one Josh-facing string promised a run another one refused.
        by = {r["mode"]: r for r in self.solo["rows"]}
        for mode in ("open_discussion", "build_execute", "keep_improving",
                     "panel_review"):
            self.assertFalse(by[mode]["off"], mode)

    def test_the_modes_that_need_peers_say_why_and_cannot_be_picked(self):
        by = {r["mode"]: r for r in self.solo["rows"]}
        for mode in ("live_room", "arena"):
            self.assertTrue(by[mode]["off"], mode)
            self.assertIn("agents", by[mode]["desc"], mode)
        self.assertEqual(self.solo["presetAfterRefusedClick"],
                         self.solo["presetWas"],
                         "a refused row must not change the recipe")

    def test_the_offered_rows_read_for_one_agent(self):
        by = {r["mode"]: r for r in self.solo["rows"]}
        self.assertEqual(by["open_discussion"]["name"], "Work in Turns")
        self.assertEqual(by["build_execute"]["name"], "Plan and Build")
        self.assertEqual(by["panel_review"]["name"], "Draft, Critique, Finalise")
        self.assertEqual(self.solo["pillLabel"], "Work in Turns")

    def test_the_supporting_copy_stops_describing_a_crowd(self):
        self.assertIn("One agent", self.solo["policyReason"])
        self.assertNotIn("every AI", self.solo["policyReason"])
        self.assertIn("Turns", self.solo["roundsLabel"])
        self.assertNotIn("each seat speaks", self.solo["roundsLabel"])
        # NOT "every tool Alloy has": desktop, browser and connectors reach
        # claude seats only (relay.MCP_DELIVERING_PROVIDERS), so a headline
        # keyed on seat COUNT would over-claim for a solo GPT/Gemini/Ox stage.
        self.assertEqual(self.solo["headline"], "One agent. Your project.")
        # ...and says what stopping the ONLY agent actually does: the
        # sequential floor parks it and the run ends.
        self.assertEqual(
            self.solo["stopTitle"],
            "Stop — cancel this turn; with one agent that also pauses the run")

    def test_the_moderator_toggle_is_gone_at_one_seat(self):
        self.assertTrue(self.solo["modToggleHidden"])

    def test_the_refusal_table_agrees_with_the_menu(self):
        r = self.solo["refusals"]
        for key in ("openDiscussion", "buildExecute", "keepImproving",
                    "arenaAtTwo", "panel"):
            self.assertEqual(r[key], "", key)
        for key in ("arena", "liveRoom"):
            self.assertTrue(r[key], key)
        self.assertEqual(r["zero"], "Pick at least one participant.")

    def test_a_hand_tuned_custom_recipe_is_judged_by_its_mode(self):
        self.assertTrue(self.solo["customFree"])
        self.assertTrue(self.solo["customBattle"])
        self.assertEqual(self.solo["customRoundRobin"], "")
        # panel is NOT in the table: the engine runs it solo, so refusing it
        # here would make one Josh-facing string promise what another refuses
        self.assertEqual(self.solo["customPanel"], "")

    def test_a_named_preset_never_inherits_another_modes_rule(self):
        """The fallback is for "custom" only. Keying it on "has no rule"
        instead made every rule-free preset inherit the live mode's rule, so
        removing a seat while Talk Live was selected greyed out every row of
        the pill -- including the three solo exists for -- each labelled with
        Talk Live's reason, leaving no clickable way out."""
        for key in ("namedWhileFree", "namedWhileBattle"):
            self.assertEqual(self.solo[key], "", key)

    def test_a_moderated_solo_room_keeps_its_off_switch_and_says_the_cost(self):
        """Hiding the row hid the OFF switch without clearing the setting:
        moderation kept running, its provider picker stayed on screen, and the
        solo sentence denied the moderator existed."""
        self.assertFalse(self.solo["modRowHiddenWhenModerated"])
        self.assertIn("moderator", self.solo["moderatedReason"])
        self.assertIn("per turn", self.solo["moderatedReason"])

    def test_a_solo_reactive_recipe_says_it_will_not_start(self):
        self.assertIn("will not start", self.solo["reactiveReason"])

    def test_the_advanced_drawer_stops_describing_a_crowd_at_one_seat(self):
        """The solo work left these two deliberately and put the explanation
        in #policyReason above the drawer; this says it in the words. The
        stored VALUES may never move -- meta, replay, forks and saved rooms
        all read them, so a relabelled value is a silently different room."""
        solo = self.solo["drawerSolo"]
        many = self.solo["drawerMany"]
        self.assertEqual(solo["laps"], ["Laps — one reply per lap"])
        self.assertEqual(solo["participants"], ["The AI itself"])
        self.assertEqual(many["laps"],
                         ["Laps — everyone speaks once per lap"])
        self.assertEqual(many["participants"], ["The AIs themselves"])
        self.assertEqual(solo["values"], many["values"])
        self.assertEqual(
            solo["values"],
            ["laps", "turns", "phases", "waves", "ceiling"])


    def test_a_roster_change_keeps_the_badge_that_explains_a_move(self):
        """orchestrationCfg's own comment states the rule: an anchorless
        normalize wipes the badges that say what the app moved for you."""
        self.assertTrue(self.solo["badgeBefore"])
        self.assertEqual(self.solo["badgeAfterRoster"], self.solo["badgeBefore"])


@unittest.skipUnless(NODE, "node not installed")
class DefaultStageUiTests(unittest.TestCase):
    """The boot roster: factory solo Claude, overridable by "Set as default".

    The harness seeds a three-seat saved default so every historical probe
    keeps its roster assumptions -- which means the seeded run here exercises
    the RESTORE path, and a second, unseeded boot proves the factory stage."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.report = boot(UI, cls._tmp.name)
        cls.d = cls.report.get("defstage") or {}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_probe_ran_clean(self):
        self.assertIsNone(self.d.get("error"), self.d.get("error"))

    def test_no_saved_default_means_factory_takes_over(self):
        self.assertIsNone(self.d["noSaved"])
        self.assertEqual(self.d["factory"],
                         [{"provider": "claude", "model": "", "effort": ""}])

    def test_garbage_in_storage_is_refused_never_half_applied(self):
        # an unknown provider and non-JSON both answer null -- the factory
        # stage takes over rather than a partially-believed roster
        self.assertIsNone(self.d["garbage"])
        self.assertIsNone(self.d["junk"])

    def test_set_as_default_captures_the_live_stage(self):
        saved = self.d["saved"]
        self.assertEqual([s["provider"] for s in saved],
                         ["claude", "gpt", "gemini"])
        by = {s["provider"]: s for s in saved}
        # models and levels ride along -- this IS the default-model setting
        self.assertEqual(by["claude"]["model"], "claude-opus-5")
        self.assertEqual(by["gpt"]["model"], "gpt-5.6-sol")
        # gemini stores the agy slug (family-level), the shape cfgFor sends
        # and applyDefaultSeat reads back
        self.assertTrue(by["gemini"]["model"].startswith("gemini-3.7-flash-"),
                        by["gemini"]["model"])
        self.assertEqual(by["gemini"]["effort"], "")
        # the button says what just happened rather than clicking silently
        self.assertIn("Saved", self.d.get("btnText") or "")

    def test_the_factory_boot_really_is_one_claude_seat(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = boot(UI, tmp, extra_env={"ALLOY_FACTORY_BOOT": "1"})
        self.assertIsNone(rep["topLevelError"])
        self.assertIsNone(rep["bootError"])
        self.assertEqual([s["provider"] for s in rep["bootSeats"]], ["claude"])
        # the solo stage boots with populated pickers on the provider default
        self.assertEqual(rep["bootSeats"][0]["model"], "claude-opus-5")

    def test_a_saved_default_model_lands_on_the_booted_card(self):
        """The default-MODEL half of the setting: what "Set as default"
        stored is what a new window's picker shows."""
        seed = json.dumps([
            {"provider": "claude", "model": "claude-haiku-4-5", "effort": "low"},
            {"provider": "claude", "model": "claude-ancient", "effort": "max"},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            rep = boot(UI, tmp, extra_env={"ALLOY_SAVED_STAGE": seed})
        self.assertIsNone(rep["bootError"])
        seats = rep["bootSeats"]
        self.assertEqual([s["provider"] for s in seats], ["claude", "claude"])
        self.assertEqual(seats[0]["model"], "claude-haiku-4-5")
        self.assertEqual(seats[0]["effort"], "low")
        # a model this install no longer offers is a preference that fell
        # through to the provider default -- never a resurrected option
        # (applySavedSeat's rule is for reopened chats, not defaults)
        self.assertEqual(seats[1]["model"], "claude-opus-5")
        self.assertEqual(seats[1]["effort"], "high")


@unittest.skipUnless(NODE, "node not installed")
class UiBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.report = boot(UI, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---- the relay's own "I am working" indicator ----------------------
    def _working(self):
        self.assertIsNone(self.report.get("workingError"))
        return self.report["working"]

    def test_a_fast_side_call_never_paints_anything(self):
        """A row that flashes on every moderator pick is noise, and noise is
        what makes the real 90-second plan easy to miss."""
        w = self._working()
        self.assertTrue(w["nothingImmediately"])
        self.assertTrue(w["fastCallNeverPainted"])

    def test_a_slow_side_call_says_what_it_is_doing(self):
        w = self._working()
        self.assertEqual(w["slowRows"], 1)
        self.assertIn("planning the work", w["slowText"])
        self.assertIn("make this better", w["slowText"])

    def test_concurrent_side_calls_get_their_own_rows(self):
        """Parallel seat threads and helper threads each open their own."""
        self.assertEqual(self._working()["twoRows"], 2)

    def test_rows_stay_below_new_messages(self):
        self.assertTrue(self._working()["rowsStayLast"])

    def test_closing_removes_exactly_the_row_that_closed(self):
        w = self._working()
        self.assertEqual(w["afterFirstClose"], 1)
        self.assertTrue(w["remainingIsGate"])

    def test_the_run_ending_clears_every_row(self):
        """A spinner that outlives its run is worse than no spinner."""
        self.assertTrue(self._working()["clearedOnFinish"])

    def test_a_chat_reopened_mid_side_call_replays_it(self):
        self.assertIn("Planning the work", self._working()["replayed"])

    def test_a_close_lands_even_when_the_chat_id_changed_under_it(self):
        """The app's pre-flight row opens before the chat has an id and closes
        after `started` gave it one — routed like an ordinary event, that
        close is dropped as "not my chat" and the spinner never goes away."""
        w = self._working()
        self.assertTrue(w["setupPainted"])
        self.assertTrue(w["setupClosedAcrossChats"])

    def test_another_chats_work_is_not_painted_here(self):
        self.assertTrue(self._working()["otherChatNotPainted"])

    # ---- richer live narration ----------------------------------------
    def _narration(self):
        self.assertIsNone(self.report.get("narrationError"))
        return self.report["narration"]

    def test_each_kind_of_step_is_rendered_as_its_own_kind(self):
        self.assertEqual(
            [c.replace("act-line ", "").split()[0]
             for c in self._narration()["classes"]],
            ["k-say", "k-search", "k-result", "k-command", "k-result"])

    def test_a_failed_step_is_marked_so_it_cannot_be_missed(self):
        n = self._narration()
        self.assertIn("k-result bad", n["htmlFail"])
        self.assertNotIn("bad", n["htmlOkResult"])

    def test_a_command_line_is_not_given_a_second_prompt_glyph(self):
        """The adapters already emit "$ ", so an icon in front of it reads
        as "> $ pytest"."""
        html = self._narration()["htmlCommand"]
        self.assertIn('<span class="act-icon"></span>', html)
        self.assertIn("$ pytest -q", html)

    def test_other_kinds_do_get_their_glyph(self):
        n = self._narration()
        for key in ("htmlSay", "htmlOkResult"):
            icon = n[key].split('act-icon">')[1].split("</span>")[0]
            self.assertTrue(icon.strip(), key)

    def test_the_step_counter_ignores_the_token_stopwatch(self):
        """A ticking counter is not work done — the same rule the engine's
        sink follows when it refuses to persist a progress act."""
        n = self._narration()
        self.assertEqual(n["stepsAfterFive"], 5)
        self.assertEqual(n["stepsAfterProgress"], 5)
        self.assertIn("5 steps", n["header"])
        self.assertIn("on this", n["header"])

    def test_the_token_counter_stays_one_line(self):
        self.assertEqual(self._narration()["progressLines"], 1)

    def test_a_finished_row_replays_through_the_same_renderer(self):
        """Live and stored narration diverging is how one of them silently
        stops matching the adapters."""
        n = self._narration()
        self.assertIn("worked through 5 steps", n["storedText"])
        self.assertEqual(
            [c.replace("act-line ", "").split()[0] for c in n["storedClasses"]],
            ["k-say", "k-search", "k-result", "k-command", "k-result"])

    def test_narration_is_escaped(self):
        """It is arbitrary text from a CLI stream."""
        html = self._narration()["htmlEscaped"]
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

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

    def test_the_mode_pill_replaces_the_card_grid(self):
        """The five preset cards left the seat rail for a compact composer
        pill; boot paints the current mode and the popover lists them all."""
        c = self.report.get("cont") or {}
        self.assertEqual(c.get("pillLabelAtBoot"), "Discuss in Turns")
        self.assertTrue(c.get("menuOpenedByPill"))
        # six modes since the Arena Duel landed (2026-08-25)
        self.assertEqual(c.get("menuRows"), 6)
        self.assertEqual(c.get("menuNames"),
                         ["Discuss in Turns", "Talk Live", "Compare & Decide",
                          "Build Together", "Keep Improving", "Arena Duel"])
        # exactly ONE row claims to be the current mode at boot
        self.assertEqual(c.get("menuSelectedCount"), 1)
        # picking a mode repaints the pill and closes the popover
        self.assertEqual(c.get("pillAfterKeepPick"), "Keep Improving")
        self.assertTrue(c.get("menuClosedAfterPick"))

    def test_keep_improving_is_gated_behind_its_warning(self):
        """The pill's Keep-Improving row opens the modal; only the
        acknowledgement arms the mode."""
        c = self.report.get("cont") or {}
        self.assertIsNone(self.report.get("contError"))
        self.assertTrue(c.get("hiddenAtBoot"))
        self.assertFalse(c.get("onAtBoot"))
        self.assertTrue(c.get("openedByPill"), "the pill row opens the warning")
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

    def test_desktop_control_is_off_until_explicitly_chosen(self):
        self.assertIsNone(self.report.get("deskError"),
                          self.report.get("deskError"))
        d = self.report.get("desk") or {}
        self.assertEqual(d.get("bootValue"), "off")
        # the harmless rungs need no ceremony, and say what they mean
        self.assertTrue(d.get("askOpensNothing"))
        self.assertIn("waits for you", d.get("askNote") or "")
        self.assertTrue(d.get("appsHiddenForAsk"))
        self.assertTrue(d.get("appsShownForAllowlist"))

    def test_the_unattended_rung_needs_an_acknowledgement(self):
        d = self.report.get("desk") or {}
        self.assertTrue(d.get("fullOpensModal"))
        self.assertTrue(d.get("okDisabledBeforeAck"),
                        "OK must be locked until the box is ticked")
        self.assertFalse(d.get("okAfterAck"), "ticking it unlocks OK")
        self.assertEqual(d.get("valueAfterOk"), "full")
        self.assertTrue(d.get("closedAfterOk"))
        self.assertIn("No prompts", d.get("fullNote") or "")

    def test_cancelling_unattended_desktop_reverts_the_picker(self):
        """Backing out is a REFUSAL. Leaving the control reading 'Unattended'
        after Cancel would be the app claiming a consent it never got — the
        same lesson the Keep Improving warning already taught."""
        d = self.report.get("desk") or {}
        self.assertEqual(d.get("valueAfterCancel"), "allowlist",
                         "cancel must restore the PREVIOUS rung")
        self.assertTrue(d.get("closedAfterCancel"))

    def test_the_browser_script_block_did_not_throw(self):
        self.assertIsNone(self.report.get("brwsError"))
        self.assertIsNone(self.report.get("advError"))

    def test_unattended_browsing_needs_an_acknowledgement(self):
        b = self.report.get("brws") or {}
        self.assertEqual(b.get("bootValue"), "off",
                         "a capability you did not ask for starts off")
        self.assertTrue(b.get("readOpensNothing"),
                        "look-only is harmless and needs no ceremony")
        self.assertTrue(b.get("fullOpensModal"))
        self.assertTrue(b.get("okDisabledBeforeAck"),
                        "OK must be locked until the box is ticked")
        self.assertFalse(b.get("okAfterAck"), "ticking it unlocks OK")
        self.assertEqual(b.get("valueAfterOk"), "full")
        self.assertIn("No prompts", b.get("fullNote") or "")

    def test_cancelling_unattended_browsing_reverts_only_its_own_picker(self):
        """Cancel is a refusal — and the two axes keep SEPARATE revert
        targets, or backing out of one modal silently moves the other
        control."""
        b = self.report.get("brws") or {}
        self.assertEqual(b.get("valueAfterCancel"), "ask",
                         "cancel must restore the PREVIOUS rung")
        self.assertTrue(b.get("closedAfterCancel"))
        self.assertEqual(b.get("desktopAfter"), b.get("desktopBefore"),
                         "the desktop picker must not move")

    def test_the_site_list_is_offered_at_every_live_rung(self):
        """With no sites Chrome reaches nothing, so the field is the
        difference between a working capability and a dead one — unlike the
        desktop allowlist, which only means something at one rung."""
        b = self.report.get("brws") or {}
        self.assertTrue(b.get("sitesHiddenWhenOff"))
        self.assertTrue(b.get("sitesShownForRead"))
        self.assertTrue(b.get("sitesShownForAsk"))

    def test_the_browser_payload_and_restore_are_honest(self):
        b = self.report.get("brws") or {}
        self.assertEqual(b.get("sites"),
                         ["https://a.test/*", "https://b.test/*"],
                         "blank entries dropped, each pattern trimmed")
        self.assertEqual(b.get("restored"), "read")
        self.assertEqual(b.get("restoredSites"), "https://a.test/*")
        # A chat saved before browser control existed, and a junk value, both
        # read as OFF — the one direction that must never happen by accident.
        self.assertEqual(b.get("restoredLegacy"), "off")
        self.assertEqual(b.get("restoredJunk"), "off")

    def test_the_advisory_ceiling_appears_only_where_it_is_true(self):
        """The rungs enforce at Read only and Ask first. At Workspace and Full
        access the seat holds a shell and can go around them, so the app says
        so rather than letting the picker imply otherwise."""
        a = self.report.get("adv") or {}
        self.assertTrue(a.get("hiddenWhenSupervised"))
        self.assertTrue(a.get("shownAtFull"))
        self.assertIn("guard against accident", a.get("textAtFull") or "")
        self.assertIn("enforced inside Chrome", a.get("textAtFull") or "")
        self.assertTrue(a.get("hiddenWhenBothOff"),
                        "no ladder on means nothing to caveat")

    def test_read_only_says_the_axes_are_inert_rather_than_weak(self):
        """Measured with a real seat: read_only emits --permission-mode plan
        and claude answers every MCP call with "Cannot call ... while in plan
        mode", so the engine registers nothing at all."""
        a = self.report.get("adv") or {}
        self.assertTrue(a.get("shownAtReadOnly"))
        text = a.get("textAtReadOnly") or ""
        self.assertIn("get none of this", text)
        self.assertIn("Raise the permission mode", text)

    def test_the_desktop_payload_and_restore_are_honest(self):
        d = self.report.get("desk") or {}
        self.assertEqual(d.get("apps"), ["Notepad$", "calc\\.exe"],
                         "blank entries dropped, each pattern trimmed")
        self.assertEqual(d.get("restored"), "allowlist")
        self.assertEqual(d.get("restoredApps"), "Notepad$")
        # a chat saved before this existed, and a value we do not recognise,
        # both read as OFF — never as a grant
        self.assertEqual(d.get("restoredLegacy"), "off")
        self.assertEqual(d.get("restoredJunk"), "off")

    def test_cancelling_the_warning_puts_the_mode_back(self):
        """Backing out is a refusal. It used to re-apply Keep Improving and
        re-open the modal, forever."""
        c = self.report.get("cont") or {}
        self.assertTrue(c.get("closedAfterCancel"))
        self.assertFalse(c.get("onAfterCancel"), "cancel never arms the mode")
        self.assertEqual(c.get("presetAfterCancel"), "open_discussion",
                         "back to where the user was, not keep_improving")
        # and the pill repaints with it — the revert must be visible
        self.assertEqual(c.get("pillAfterCancel"), "Discuss in Turns")

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

    # ---- what the adversarial pass found ------------------------------------
    def _adv(self):
        self.assertIsNone(self.report.get("advPassError"),
                          "adv probe threw: %s"
                          % self.report.get("advPassError"))
        return self.report["advPass"]

    def test_the_quiet_clock_needs_a_real_last_activity_stamp(self):
        """Feeding it the turn's START makes quiet equal the whole age, so
        every healthy long turn past half its idle window renders red — the
        0:00-of-15:00 lie from inside the function written to prevent it."""
        a = self._adv()
        self.assertEqual(a["quietFromLastact"], "10:00")
        self.assertIn("quiet", a["quietFromStart"])

    def test_a_busy_background_seat_is_not_reported_as_going_quiet(self):
        """Driven through the POPOVER: calling the shared function with
        hand-picked arguments passes whichever argument renderJobsList
        actually sends."""
        a = self._adv()
        self.assertIn("Claude · 2:31", a["busySeatLine"])
        self.assertNotIn("quiet", a["busySeatLine"])
        self.assertIn("quiet 2:31 of 5:00", a["quietSeatLine"])

    def test_a_restored_queue_does_not_mint_ids_it_already_holds(self):
        self.assertEqual(self._adv()["seqAfterRestore"], 7)

    def test_an_engine_board_event_repaints_the_card(self):
        """Without it the card was only ever built by the `question` handler,
        so an expired gate left a live "Approve & dispatch" on screen."""
        a = self._adv()
        self.assertEqual(a["cardFromEventPhase"], "awaiting")
        self.assertEqual(a["cardAfterEngineDecline"], "declined")

    def test_the_declined_card_does_not_promise_a_re_plan_it_cannot_know(self):
        self.assertEqual(self._adv()["declinedNote"], "Sent back.")

    def test_a_reopened_chats_board_is_seeded_onto_an_existing_run(self):
        """Every run is created FIRST by renderChats from a rail row carrying
        no board, so a constructor-only seed could never arrive."""
        self.assertTrue(self._adv()["reopenedBoardSeeded"])

    def test_a_fresh_stage_repaints_the_jobs_badge(self):
        a = self._adv()
        self.assertTrue(a["barBeforeNewChat"])
        self.assertFalse(a["barAfterNewChat"],
                         "a fresh stage never repainted the bar")

    # ---- W2.2: the board-review switch --------------------------------------
    def _bswitch(self):
        self.assertIsNone(self.report.get("boardSwitchError"),
                          "board-switch probe threw: %s"
                          % self.report.get("boardSwitchError"))
        return self.report["boardSwitch"]

    def test_the_board_switch_is_refused_with_a_reason_where_it_cannot_work(self):
        b = self._bswitch()
        self.assertTrue(b["offInConversation"])
        self.assertIn("Supervisor", b["reasonInConversation"])
        self.assertTrue(b["onInBuildTogether"])
        self.assertIn("dispatches", b["reasonInBuildTogether"])

    def test_a_switch_that_is_on_is_never_taken_away(self):
        """Hiding one that is ON is how moderation got stuck on with no way to
        turn it off."""
        self.assertTrue(self._bswitch()["stillEditableWhenOn"])

    # ---- W2.2: the Supervisor board review ----------------------------------
    def _board(self):
        self.assertIsNone(self.report.get("boardError"),
                          "board probe threw: %s" % self.report.get("boardError"))
        return self.report["board"]

    def test_the_gate_answers_in_the_card_not_the_generic_modal(self):
        """The modal answers with a STRING; board_gate reads Josh's edits off
        a dict."""
        b = self._board()
        self.assertFalse(b["askModalOpened"])
        self.assertTrue(b["cardPresent"])
        self.assertEqual(b["phase"], "awaiting")
        self.assertEqual(b["rowCount"], 2)

    def test_the_owner_picker_comes_from_the_engines_seat_list(self):
        """Reassigning to a seat that is not in this conversation makes
        assign_workstreams fail the task outright."""
        b = self._board()
        self.assertEqual(b["ownerOptions"],
                         ["0:Claude", "1:Gemini (cannot write files)"])

    def test_the_card_shows_the_file_claims_and_will_not_edit_them(self):
        b = self._board()
        self.assertEqual(b["filesShown"], "1 file")
        self.assertFalse(b["hasFileInput"])

    def test_an_approval_sends_exactly_what_the_whitelist_accepts(self):
        b = self._board()
        chat, board_id, payload = b["approveCall"]
        self.assertEqual(chat, "sess-board")
        self.assertEqual(board_id, "board-1")
        self.assertEqual(payload["approved"], True)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["tasks"], [
            {"id": "t1", "include": True, "brief": "write a.py properly",
             "owner": "1"},
            {"id": "t2", "include": False, "brief": "write b.py",
             "owner": "1"},
        ])
        self.assertEqual(b["phaseAfterApprove"], "approved")

    def test_sending_it_back_asks_why_first(self):
        """A refusal with no reason is the least useful thing Josh can hand a
        planner, and it is the only channel the feedback has."""
        b = self._board()
        self.assertTrue(b["notePrompted"])
        self.assertFalse(b["refuseCall"][2]["approved"])
        self.assertEqual(b["refuseCall"][2]["feedback"], "too broad")

    # ---- W2.3: the background jobs view -------------------------------------
    def _jobs(self):
        self.assertIsNone(self.report.get("jobsError"),
                          "jobs probe threw: %s" % self.report.get("jobsError"))
        return self.report["jobs"]

    def test_the_bar_is_hidden_with_nothing_live(self):
        j = self._jobs()
        self.assertTrue(j["hiddenWithNothingLive"])
        self.assertTrue(j["hiddenOnceItFinished"])

    def test_starting_a_new_chat_does_not_hide_a_running_one(self):
        """Walking away from a chat is what the bar is FOR."""
        self.assertTrue(self._jobs()["stillShownAfterNewChat"])

    def test_a_background_chat_moves_the_badge(self):
        """Its rows never reach the transcript on screen, so the repaint has
        to be hoisted above uiEvent's not-my-chat return."""
        j = self._jobs()
        self.assertTrue(j["shownForBackground"])
        self.assertEqual(j["labelForBackground"], "1 running")

    def test_a_question_turns_the_badge_into_a_summons(self):
        j = self._jobs()
        self.assertEqual(j["labelWhenWaiting"], "1 waiting on you")
        self.assertTrue(j["attentionClass"])

    def test_the_popover_uses_the_transcripts_clock_rule_verbatim(self):
        """`limit` is a real deadline and earns "of X"; `idle` is a silence
        window and does not. Three of four providers always have no limit in
        the desktop app, so conflating them is the 0:00 of 15:00 lie."""
        j = self._jobs()
        self.assertTrue(j["popShown"])
        self.assertEqual(j["rowCount"], 1)
        self.assertEqual(j["clocks"][0], "Claude · 1:40")
        self.assertEqual(j["clocks"][1], "Gemini · 2:00 of 10:00")

    def test_the_popover_shows_the_relays_own_work_and_the_queue(self):
        j = self._jobs()
        self.assertIn("Planning the work · 0:30", j["clocks"][2])
        self.assertIn("2 messages waiting for the next turn", j["rowText"])

    def test_clicking_a_job_opens_that_chat_and_closes_the_popover(self):
        j = self._jobs()
        self.assertTrue(j["openedOnClick"])
        self.assertTrue(j["popClosedAfterClick"])

    # ---- W2.4: the Master goal chip ----------------------------------------
    def _goal(self):
        self.assertIsNone(self.report.get("goalChipError"),
                          "goal probe threw: %s"
                          % self.report.get("goalChipError"))
        return self.report["goalChip"]

    def test_the_master_goal_is_the_managers_goal_not_the_opener(self):
        g = self._goal()
        self.assertEqual(g["afterStarted"],
                         "what the manager is actually working on")

    def test_a_retarget_repaints_the_chip_live(self):
        self.assertEqual(self._goal()["afterRetarget"], "ship the docs")

    def test_it_falls_back_to_the_opener_when_no_goal_was_ever_set(self):
        self.assertEqual(self._goal()["fallback"], "only an opener here")

    def test_a_reopened_chat_shows_the_managers_goal_too(self):
        """openChat is a SECOND call site; fixing only runFor left every
        reopened supervised chat showing its opening message."""
        self.assertEqual(self._goal()["afterReopen"],
                         "what the manager settled on")

    # ---- W2.1: the queue dock ----------------------------------------------
    def _dock(self):
        self.assertIsNone(self.report.get("dockError"),
                          "dock probe threw: %s" % self.report.get("dockError"))
        return self.report["dock"]

    def test_the_dock_is_hidden_until_something_is_queued(self):
        d = self._dock()
        self.assertTrue(d["hiddenWithNothingQueued"])
        self.assertTrue(d["buttonOfferedWhileRunning"])

    def test_enter_still_sends_and_the_queue_gesture_holds(self):
        """A hold is something you ask for, never the new default: a message
        typed in a hurry must not sit in a dock nobody looked at."""
        d = self._dock()
        self.assertEqual(d["enterCalls"], ["interject"])
        # driven through the real keydown binding: Ctrl+Enter holds...
        self.assertTrue(d["ctrlEnterPrevented"],
                        "Ctrl+Enter fell through to the browser default")
        self.assertEqual(d["queueCalls"], ["prepare_message"])
        # ...and plain Enter on the same box still delivers
        self.assertEqual(d["plainEnterCalls"], ["interject"])
        self.assertTrue(d["sayClearedAfterQueue"])
        self.assertEqual(d["rowCount"], 1)
        self.assertTrue(d["shownOnceQueued"])

    def test_a_queued_row_is_a_textarea(self):
        """An <input> collapses multi-line text, and a row with an attachment
        is always multi-line."""
        self.assertEqual(self._dock()["rowIsTextarea"], "textarea")

    def test_the_edited_text_is_what_gets_sent(self):
        d = self._dock()
        self.assertEqual(d["sentText"], "hold me, edited")
        self.assertEqual(d["emptyAfterSend"], 0)
        self.assertTrue(d["hiddenAfterSend"])

    def test_a_slash_line_is_refused_rather_than_queued(self):
        d = self._dock()
        self.assertEqual(d["slashCalls"], [])
        self.assertIn("Commands are not queued", d["slashNote"])

    def test_attachments_are_chips_and_the_box_holds_only_the_prose(self):
        d = self._dock()
        self.assertEqual(d["proseOnlyInBox"], "with a file")
        self.assertEqual(len(d["attChips"]), 1)
        self.assertIn("a.png", d["attChips"][0])

    def test_dropping_takes_two_clicks_and_names_the_files_it_leaves(self):
        """The bytes were written into the working folder when the row was
        queued, so a drop cannot unwrite them — and hiding that is the lie."""
        d = self._dock()
        self.assertEqual(d["rowsAfterArm"], 1)
        self.assertIn("keeps 1 file", d["armLabel"])
        self.assertEqual(d["rowsAfterConfirm"], 0)

    def test_a_queued_row_can_be_moved_and_send_all_follows_the_order(self):
        d = self._dock()
        self.assertEqual(d["orderBefore"], ["first", "second"])
        self.assertEqual(d["orderAfter"], ["second", "first"])
        self.assertEqual(d["sendAllOrder"], ["second", "first"])
        self.assertEqual(d["emptyAfterSendAll"], 0)

    def test_the_dock_is_written_where_a_reload_will_find_it(self):
        rows = self._dock()["persisted"].get("sess-dock") or []
        self.assertEqual([r["text"] for r in rows], ["kept across a reload"])
        self.assertTrue(rows[0]["id"])

    def test_a_send_that_outlives_a_chat_switch_lands_where_it_was_sent(self):
        """sendQueued pins the chat for the bridge call; the drop and the echo
        after the await used the unpinned global, so a switch mid-flight
        dropped a row out of the chat now on screen and painted its echo
        there."""
        d = self._dock()
        self.assertEqual(d["switchedAwayLeftBehind"], 0,
                         "the row stayed in the chat it was sent to")
        self.assertEqual(d["switchedAwayEchoed"], 0,
                         "the echo was painted into another chat's transcript")

    def test_the_dock_belongs_to_one_chat(self):
        d = self._dock()
        self.assertEqual(d["rowsBeforeSwitch"], 1)
        self.assertEqual(d["rowsInOtherChat"], 0)
        self.assertTrue(d["dockHiddenInOtherChat"])

    # ---- W2.0: background chats (run pinning) ------------------------------
    def _bg(self):
        self.assertIsNone(self.report.get("bgError"),
                          "background probe threw: %s" % self.report.get("bgError"))
        return self.report["bg"]

    def test_a_chat_started_from_this_stage_takes_the_focus(self):
        b = self._bg()
        self.assertEqual(b["activeAfterMine"], "sess-mine")
        self.assertIn("sess-mine", b["tabsAfterMine"])

    def test_a_background_start_does_not_steal_the_transcript(self):
        """`started` did `activeId = id; openTab(id)` unconditionally, so a
        webhook firing while Josh read another chat swapped it under him."""
        b = self._bg()
        self.assertEqual(b["activeAfterBg"], "sess-mine",
                         "a background chat took the screen")
        self.assertNotIn("sess-bg", b["tabsAfterBg"],
                         "a background chat opened a tab nobody asked for")

    def test_a_background_chat_still_earns_a_rail_row_and_a_status(self):
        """Not showing it is not the same as hiding it."""
        b = self._bg()
        self.assertTrue(b["knownToTheRail"])
        self.assertEqual(b["bgStatus"], "running")
        self.assertEqual(b["mineStatus"], "running")

    def test_a_background_chats_messages_stay_out_of_the_open_one(self):
        self.assertEqual(self._bg()["feedGrewFromBg"], 0)

    def test_a_background_event_with_no_chat_id_yet_is_dropped(self):
        """Its setup runs before the session dir exists, so those events carry
        chat_id null — which the routing gate reads as "the chat on screen"."""
        b = self._bg()
        self.assertEqual(b["feedGrewFromAnonymousBg"], 0)
        self.assertGreater(b["feedGrewFromMyOwnStatus"], 0,
                           "the visible chat's own status row was dropped too")

    # ---- memory modal (Wave 3) ---------------------------------------------
    def _mem(self):
        self.assertIsNone(self.report.get("memError"),
                          "memory probe threw: %s" % self.report.get("memError"))
        return self.report["mem"]

    def test_the_memory_modal_follows_the_chat(self):
        """Judged transient once and it is not: a modal here is not a focus
        trap, `Ctrl+Tab` changes `activeId` underneath it, and `memAdd` sends
        `activeId` at CALL time -- so the header could name project A while
        the note landed in project B."""
        self.assertIsNone(self.report.get("memFollowError"),
                          self.report.get("memFollowError"))
        mem = self.report["memFollow"]
        self.assertEqual([c[0] for c in mem["asked"]], ["get_memory"],
                         "it did not re-ask the bridge once for THIS chat")
        self.assertIn("Project B", mem["header"])
        self.assertEqual(mem["draftKept"], "half a thought",
                         "a chat switch discarded what Josh was typing")
        self.assertIn("switched chats", mem["note"])
        self.assertEqual(mem["askedWhenClosed"], [],
                         "a closed modal still called the bridge")

    def test_both_chat_switches_make_the_memory_modal_follow(self):
        """The probe above proves the function; this proves it is WIRED.
        `openChat` is every rail click, tab click, Ctrl+Tab and Ctrl+1-9;
        `newChat` is Ctrl+T. A helper nothing calls is the control that does
        nothing, and the harness cannot drive either one end to end."""
        src = open(UI, encoding="utf-8").read()
        for fn in ("async function openChat(", "async function newChat("):
            start = src.index(fn)
            body = src[start:src.index(chr(10) + "}" + chr(10), start)]
            self.assertIn("await memoryFollowsTheChat();", body, fn)

    def test_the_memory_modal_exists_and_starts_hidden(self):
        m = self._mem()
        self.assertTrue(m["present"])
        self.assertTrue(m["hiddenAtBoot"])
        self.assertTrue(m["shown"])
        self.assertTrue(m["closed"])

    def test_opening_it_asks_the_bridge_for_THIS_chats_memory(self):
        self.assertEqual(self._mem()["openCall"], [["get_memory", None]])

    def test_a_notes_text_reaches_the_page_as_text_not_markup(self):
        # the store is a hand-editable markdown file, so its content is
        # arbitrary and a note containing tags must render as those tags
        m = self._mem()
        self.assertIn("The gate is <b>run_all</b>.", m["firstText"])
        # the two halves together: the tags arrive as TEXT, and nothing was
        # ever handed to innerHTML. Asserting only the first passes either
        # way, because the stub's deepText reads both properties.
        self.assertEqual(m["firstTextProp"], "The gate is <b>run_all</b>.")
        self.assertEqual(m["firstHtmlProp"], "")

    def test_an_unattributed_note_says_who_rather_than_borrowing(self):
        self.assertIn("a seat", self._mem()["secondText"])
        self.assertIn("undated", self._mem()["secondText"])

    def test_a_project_chat_marks_which_notes_come_from_everywhere(self):
        m = self._mem()
        self.assertIn("ai-chat", m["scopeLine"])
        self.assertEqual(m["rowCount"], 2)
        self.assertEqual(m["taggedRows"], 1)
        self.assertTrue(m["everywhereOffered"])

    def test_a_global_chat_offers_no_scope_choice_and_tags_nothing(self):
        # with one scope in play the checkbox would be a control that does
        # nothing and the tag would be on every row
        m = self._mem()
        self.assertFalse(m["globalOffersEverywhere"])
        self.assertEqual(m["globalTagged"], 0)
        self.assertIn("no project folder", m["globalScopeLine"])

    def test_a_truncated_store_says_so_in_the_modal_too(self):
        self.assertIn("too large to read in full", self._mem()["globalScopeLine"])

    def test_forget_arms_before_it_sends_and_sends_the_ROWS_scope(self):
        # the list mixes two files; deleting by id alone would have to guess
        m = self._mem()
        self.assertEqual(m["armedLabel"], "Really forget?")
        self.assertEqual(m["callsAfterArm"], [])
        self.assertEqual(m["callsAfterConfirm"],
                         [["forget_memory", "m2", "global", None]])

    def test_an_empty_note_is_refused_without_calling_the_bridge(self):
        m = self._mem()
        self.assertEqual(m["emptyCalls"], [])
        self.assertIn("Type something", m["emptyNote"])

    def test_adding_sends_the_text_the_scope_choice_and_the_chat(self):
        m = self._mem()
        self.assertEqual(m["addCalls"],
                         [["save_memory", "remember this", True, None]])
        self.assertTrue(m["textCleared"])

    def test_a_trim_or_an_eviction_is_stated_rather_than_silent(self):
        self.assertIn("trimmed", self._mem()["addNote"])

    def test_an_unreadable_store_shows_the_error_not_an_empty_list(self):
        self.assertIn("disk on fire", self._mem()["errorNote"])

    def test_the_two_memory_directives_render_as_chips(self):
        chips = self._mem()["chips"]
        self.assertIn("dir-chip dir-remember", chips[0])
        self.assertIn("remembers: the gate is run_all", chips[0])
        self.assertIn("dir-chip dir-recall", chips[1])
        self.assertIn("looks up: gate", chips[1])
        # the body survives the peel; the directive is not left in it
        self.assertIn("body text", chips[0])
        self.assertNotIn("[[REMEMBER", chips[0].split("dir-chip")[0])

    #: Every modal's Escape closer. Hand-kept ON PURPOSE, and the test below
    #: fails LOUDLY on a missing entry rather than skipping it -- a map that
    #: silently ignores an id it does not know is the fourth-place-to-sync bug
    #: the `scheduled` hook event was written to avoid.
    MODAL_CLOSERS = {
        "acctModal": "closeAccounts(", "roleModal": "closeRole(",
        "askModal": "hideAsk(", "skillModal": "closeSkills(",
        "contModal": "closeContinuous(", "kbdModal": "closeKbd(",
        "hooksModal": "closeHooks(", "roomsModal": "closeRooms(",
        "schedModal": "closeSched(", "statsModal": "closeStats(",
        "memModal": "closeMemory(",
        # these two REVERT a picker, so Escape checks them by id
        "deskModal": '$("deskModal")', "brwsModal": '$("brwsModal")',
    }

    def test_every_modal_is_registered_in_all_three_places(self):
        """Miss one and it is invisible, or Escape leaves it open.

        Derived from the markup rather than named one at a time -- the same
        move `test_every_sidebar_button_is_in_the_shared_style_rule` makes,
        for the same reason: the previous version pinned the literal string
        "#statsModal, #memModal {" and broke the moment a THIRTEENTH modal
        made the selector wrap, which says nothing about whether the new
        modal was registered.
        """
        with open(UI, encoding="utf-8") as f:
            src = f.read()
        ids = re.findall(r'<div id="(\w+Modal)"', src)
        self.assertGreaterEqual(len(ids), 13, ids)
        # the SELECTOR LIST of each rule, sliced exactly -- both rules wrap
        # over several lines and will wrap again with the next modal
        marker = "position: fixed; inset: 0; z-index: 50; display: none;"
        i = src.index(marker)
        hidden = src[src.rindex("}", 0, i) + 1:i]
        j = src.index("#acctModal.show")
        shown = src[j:src.index("}", j) + 1]
        # the ONE global Escape handler -- anchored on the closer it is
        # known to hold, because the page has several keydown listeners
        # and an inline note editor with an Escape branch of its own
        k = src.index("closeAccounts(); closeRole();")
        escape = src[src.rindex('if (e.key === "Escape") {', 0, k):]
        escape = escape.split(chr(10) + "  }", 1)[0]
        for mid in ids:
            self.assertIn("#" + mid, hidden, mid + " is not in the display:none list")
            self.assertIn("#" + mid + ".show", shown, mid + " is not in the .show list")
            closer = self.MODAL_CLOSERS.get(mid)
            self.assertIsNotNone(
                closer, "add %s to MODAL_CLOSERS -- a new modal needs an "
                        "Escape closer" % mid)
            self.assertIn(closer, escape,
                          mid + " is not closed by the Escape listener")

    def test_every_sidebar_button_is_in_the_shared_style_rule(self):
        """Found in a real browser, invisible everywhere else.

        The app-nav buttons are styled by ONE id-list selector, and in their
        previous home (the seat-rail bottom stack) both #statsBtn (shipped
        2026-08-27) and #memBtn were left out of it -- so they rendered as
        raw browser defaults, Arial on white with a square 2px black border,
        among four styled siblings. No text-level test and no node harness
        can see that; only getComputedStyle on a real page can. This reads
        the ids straight out of #appNav so the NEXT button is caught by the
        same check.
        """
        with open(UI, encoding="utf-8") as f:
            src = f.read()
        nav = src.split('<nav id="appNav"', 1)[1].split("</nav>", 1)[0]
        ids = re.findall(r'<button id="(\w+)"', nav)
        self.assertIn("memBtn", ids)
        self.assertIn("statsBtn", ids)
        # keep the anchor IN the slice, or #acctBtn reads as missing from
        # the very rule it opens
        rule = "#acctBtn, " + src.split("#acctBtn, ", 1)[1].split("{", 1)[0]
        missing = [i for i in ids if "#" + i not in rule]
        self.assertEqual(missing, [], "app-nav buttons with no shared style")

    # ---- spawn-lineage tree (t6) -------------------------------------------
    def _lineage(self):
        self.assertIsNone(self.report.get("lineageError"),
                          "lineage probe threw: %s"
                          % self.report.get("lineageError"))
        return self.report["lineage"]

    def test_spawn_lineage_chip_marks_a_chat_that_spawned_children(self):
        """The ⌥ chip is the INDICATOR half: faintly visible on a parent row
        with its child count, absent on rows that spawned nothing."""
        lin = self._lineage()
        self.assertTrue(lin["childPrefixed"],
                        "the spawned child row lost its ↳ title prefix")
        self.assertTrue(lin["chipPresent"])
        self.assertTrue(lin["chipVisible"])
        self.assertEqual(lin["chipText"], "\u2325 1")
        self.assertTrue(lin["noKidsChipHidden"])

    def test_spawn_lineage_tree_lists_and_opens_children(self):
        """Click the chip → read-only popover listing each child; click a
        listed child → that chat opens; click again → closed, cleanly."""
        lin = self._lineage()
        self.assertTrue(lin["popStartsHidden"])
        self.assertTrue(lin["popShownAfterClick"])
        self.assertIn("1", lin["headText"] or "")
        self.assertEqual(lin["items"], ["\u21b3 Child Squad"])
        self.assertTrue(lin["itemOpensChat"])
        self.assertTrue(lin["popHiddenAfterSecondClick"])
        self.assertTrue(lin["chipOpenClassCleared"])

    def test_team_report_captions_link_only_to_living_children(self):
        """A team report row grows exactly one ⌥ jump — for the session that
        still exists. A deleted child and an ordinary seat merely mentioning
        a sessions/<id> path must render no link at all."""
        self.assertIsNone(self.report.get("teamLinkError"),
                          "team-link probe threw: %s"
                          % self.report.get("teamLinkError"))
        tl = self.report["teamLink"]
        self.assertEqual(tl["count"], 1)
        self.assertEqual(tl["titles"],
                         ["Open sub-conversation 'sess-two'"])
        self.assertTrue(tl["opensChat"])

    # ---- delivery-refusal pills (comms-design.md section 3.3, UI half) ------
    def _refusal(self):
        self.assertIsNone(self.report.get("refusalError"),
                          "refusal probe threw: %s"
                          % self.report.get("refusalError"))
        return self.report["refusal"]

    def test_refusal_pills_render_from_envelope_data(self):
        """rejected_to [{seat, reason}] becomes one dim dashed pill naming the
        refused seat, with the reason in its tooltip — visible rejection, not
        silent success. Rendered from data only: rows without refusals grow
        nothing, and malformed entries neither crash nor invent a pill."""
        r = self._refusal()
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["firstSeats"], "Seat 9")
        self.assertIn("not delivered to Seat 9", r["firstTitle"] or "")
        self.assertIn("worker radio-silent until t3 settles",
                      r["firstTitle"] or "")
        self.assertTrue(r["plainAddsNone"])

    def test_narrowing_failure_is_surfaced_even_when_all_seats_heard_it(self):
        """A [[TO:]] that mis-resolved broadcast instead: every seat was
        delivered, and the pill still says so — intent vs delivery must not
        quietly diverge in replay."""
        r = self._refusal()
        self.assertEqual(r["secondText"], "(narrowing only)")
        self.assertTrue(r["secondMentionsBroadcast"])

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

    def test_mention_hint_mirrors_engine_routing(self):
        """The chip shows for a seated label (case-insensitively), and an
        @name matching nobody stays literal text with no chip."""
        self.assertIsNone(self.report.get("quickwinsError"))
        q = self.report["quickwins"]
        self.assertEqual(q["hintForClaude"], "→ only Claude will receive this")
        self.assertTrue(q["hintHiddenWhenNoMatch"])
        self.assertTrue(q["hintHiddenForPlain"])

    def test_drop_planning_attaches_files_and_refuses_folders(self):
        """Files attach; a folder is DETECTED and named but refused — while
        unseated because WebView2 never exposes a dropped item's absolute
        path (so the cue points at Choose), and while seated because the
        working folder is locked once a conversation starts."""
        q = self.report["quickwins"]
        self.assertEqual(q["filesAttach"], 2)
        self.assertTrue(q["filesNoCue"])
        self.assertTrue(q["folderNamed"])
        self.assertTrue(q["folderUnseatedCue"], q.get("quickwinsError"))
        self.assertTrue(q["folderSeatedCue"])

    # ---- W4: scheduled rooms ------------------------------------------
    def _sched(self):
        self.assertIsNone(self.report.get("schedError"),
                          self.report.get("schedError"))
        return self.report["sched"]

    def test_the_hooks_modal_renders_the_bridges_event_list(self):
        """Three edits, and the third is the one nothing would notice."""
        p = self._sched()
        self.assertIn("scheduled", p["hookRowIds"])
        self.assertIn("Scheduled room", p["scheduledLabel"])

    def test_hook_save_sends_a_row_hook_labels_never_heard_of(self):
        """`future_thing` is in the bridge's event list and not in the UI's
        label table. The old Save walked the TABLE, so such a row rendered
        and was silently dropped."""
        p = self._sched()
        self.assertIn("future_thing", p["hookRowIds"])
        self.assertEqual(p["hookSaved"],
                         {"scheduled": "notify-me", "future_thing": "later"})

    def test_the_schedule_list_renders_and_names_its_rooms(self):
        p = self._sched()
        self.assertTrue(p["modalShown"])
        self.assertEqual(p["rowCount"], 2)
        self.assertEqual(p["rowNames"], ["Nightly", "Loud one"])
        self.assertEqual(p["roomOptions"], ["Quiet", "Loud"])
        self.assertTrue(p["emptyHidden"])

    def test_a_row_whose_room_widened_says_it_will_not_run(self):
        p = self._sched()
        self.assertIn("never acknowledged", p["warnText"])
        self.assertIn("will not run", p["warnText"])
        self.assertIn("Missed", p["warnText"])
        self.assertIn("off", p["secondRowClass"].split())

    def test_editing_a_widened_room_does_not_pre_tick_the_box(self):
        """The acknowledgement is for what the room grants NOW. A pre-ticked
        box would let one click re-consent to access nobody agreed to."""
        p = self._sched()
        self.assertFalse(p["ackAfterGap"])
        self.assertTrue(p["ackWhenCovered"])
        self.assertEqual(p["editHeading"], "Editing Loud one")
        self.assertEqual(p["daysAfterEdit"], ["0", "4"])

    def test_the_day_boxes_are_really_being_read(self):
        """A 0 here means schedDaysPicked() sees nothing and every weekly
        assertion in this file is vacuous -- the stub's selector engine used
        to ignore the descendant combinator outright."""
        self.assertEqual(self._sched()["dayBoxes"], 7)

    def test_the_form_shows_only_the_fields_its_recurrence_uses(self):
        rows = self._sched()["kindRows"]
        self.assertEqual(rows["daily"], [False, True, True, True])
        self.assertEqual(rows["weekly"], [False, False, True, True])
        self.assertEqual(rows["interval"], [True, True, False, True])
        self.assertEqual(rows["once"], [True, True, True, False])

    def test_a_risky_room_shows_its_grants_and_its_dead_controls(self):
        p = self._sched()
        self.assertFalse(p["grantsHidden"])
        self.assertEqual(p["grantLines"],
                         ["write, delete and run anything on this machine"])
        self.assertIn("Desktop control is set to Ask", p["noteLines"][0])
        # the tick box names the ROOM and the RECURRENCE, not just "this"
        self.assertIn("Loud", p["ackText"])
        self.assertIn("every day at 01:00", p["ackText"])
        self.assertIn("unattended", p["ackText"])
        self.assertTrue(p["grantsHiddenAgain"],
                        "the grants block stayed up for an innocuous room")

    def test_save_is_refused_until_the_acknowledgement_is_ticked(self):
        p = self._sched()
        self.assertEqual(p["savedWithoutTick"], 0,
                         "an unacknowledged schedule reached the bridge")
        self.assertIn("Tick the acknowledgement", p["refusal"])
        self.assertEqual(p["savedName"], "save_schedule")
        self.assertEqual(p["savedSpec"]["ack"], {"grants": ["permission_full"]})
        self.assertEqual(p["savedSpec"]["room"], "Loud")
        self.assertEqual(p["savedSpec"]["prompt"], "do the thing")

    def test_the_poll_interval_is_shown_rather_than_only_published(self):
        """`poll_seconds` had no reader at all: "Alloy has to be running"
        without its resolution is half the fact."""
        self.assertIn("every 30 seconds", self._sched()["pollText"])

    def test_editing_a_paused_schedule_does_not_silently_re_arm_it(self):
        """The payload used to hardcode `enabled: true`, so opening a paused
        row, changing one word and saving armed it — the opposite of what
        pausing means."""
        p = self._sched()
        self.assertTrue(p["editEnabledBefore"])
        self.assertFalse(p["editEnabledAfterPausedEdit"])
        self.assertIs(p["pausedEditPayload"], False,
                      "the saved payload re-armed a paused schedule")
        # ...and a fresh form still saves armed
        self.assertTrue(p["savedEnabled"])

    def test_changing_the_recurrence_un_earns_the_acknowledgement(self):
        """The sentence is what he agreed to. Found live: the room picker was
        the only thing that repainted it, so a box ticked against "every day
        at 01:00" saved "every Mon, Fri at 02:30"."""
        p = self._sched()
        self.assertIn("01:00", p["ackTextBeforeChange"])
        self.assertIn("04:45", p["ackTextAfterChange"])
        self.assertFalse(p["ackAfterTimeChange"],
                         "the tick survived a recurrence change")
        self.assertIn("read the access note", p["changeNote"])
        self.assertTrue(p["ackAfterNoChange"],
                        "an identical repaint un-ticked the box")

    def test_a_scheduled_event_reaches_the_banner(self):
        """The only thing on screen that ever mentions a run that did NOT
        happen. It carries no chat_id, so it must not be routed like one."""
        p = self._sched()
        self.assertTrue(p["bannerShown"])
        self.assertIn("Skipped", p["bannerText"])

    def test_drop_classification_splits_entries_from_files(self):
        """webkitGetAsEntry folders vs getAsFile files, string items skipped."""
        q = self.report["quickwins"]
        self.assertEqual(q["classifiedFiles"], 1)
        self.assertEqual(q["classifiedFolders"], ["proj"])
        self.assertTrue(q["dropCueStartsHidden"])

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
