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

function parseFragment(html, mk) {
  // Flat scan: every <tag ...> becomes a child element. Nesting is ignored on
  // purpose — querySelector below searches descendants flatly, which is all
  // the seat/modal builders need.
  const out = [];
  const re = /<([a-zA-Z][\w-]*)((?:\s+[^\s=>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>/g;
  let m;
  while ((m = re.exec(html))) {
    const el = mk(m[1].toLowerCase());
    const attrs = m[2] || '';
    const are = /([^\s=]+)\s*=\s*"([^"]*)"/g;
    let a;
    while ((a = are.exec(attrs))) {
      const k = a[1].toLowerCase();
      if (k === 'class') el.className = a[2];
      else if (k === 'id') el.id = a[2];
      else el.setAttribute(k, a[2]);
    }
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
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
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

module.exports = {mkEl, matches, byId, resetIds() { byId = {}; }, getById: id => byId[id] || null};
"""

BOOT_JS = r"""'use strict';
// Boots ui/index.html's inline script against the stub DOM and reports what
// happened as JSON on stdout. Any top-level throw is captured, not swallowed.
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const {mkEl, matches} = require(path.join(__dirname, 'dom.js'));

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
  const are = /([^\s=]+)\s*=\s*"([^"]*)"/g;
  let a;
  while ((a = are.exec(attrs))) {
    const k = a[1].toLowerCase();
    if (k === 'class') el.className = a[2];
    else if (k === 'id') el.id = a[2];
    else if (k === 'value') el.value = a[2];
    else el.setAttribute(k, a[2]);
  }
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
  addEventListener() {}, removeEventListener() {},
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
const listeners = Object.create(null);
const store = Object.create(null);

const api = new Proxy({}, {
  get(_, name) {
    if (name === 'then') return undefined;          // must not look thenable
    return (...args) => Promise.resolve(apiReply(String(name), args));
  },
});

const savedTabs = [];
function apiReply(name, args) {
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
    case 'list_workspace_files': return [];
    case 'list_runs': return {runs: []};
    case 'folder_exists': return false;
    case 'get_skills': return {skills: []};
    case 'get_mcp': return {ok: true};
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
  process.stdout.write(JSON.stringify(Object.assign({
    topLevelError,
    seats: seatInfo,
    permissionNote: byId['permissionNote'] ? byId['permissionNote'].textContent : null,
    modModelOptions: byId['modModel'] ? byId['modModel'].options.length : -1,
    bootRan: !!extra.bootRan,
    bootError: extra.bootError || null,
  }, extra.more || {}), null, 1));
}

if (topLevelError) {
  report({});
  process.exit(0);
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
  const more = {};
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
