"use strict";
// A small DOM, enough to run the frontend (src/datoolkit/web/app.js) in Node with no packages.
// The page is built from the real index.html, so an element app.js looks up that the page
// lacks comes back null, as it would in a browser. Timers, fetch, WebSockets and the page's
// libraries (xterm.js, Guacamole, marked, DOMPurify) are stand-ins the tests drive.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const WEB = path.join(__dirname, "..", "..", "src", "datoolkit", "web");
const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
const RAW_TEXT = new Set(["script", "style", "textarea"]);

// ------------------------------------------------------------------ events

class Event {
  constructor(type, init = {}) {
    Object.assign(this, init);
    this.type = type;
    this.bubbles = init.bubbles !== false;
    this.defaultPrevented = false;
    this.target = null;
    this.currentTarget = null;
    this._stopped = false;
    this._stoppedNow = false;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stopped = true; }
  stopImmediatePropagation() { this._stopped = this._stoppedNow = true; }
}

// The links between nodes, and what a node keeps for itself, aren't enumerable: assert's messages
// print what is (custom inspectors aside), deepEqual walks it, and through those links that would
// be the whole page and its window, enough to run a machine out of memory. Each node's enumerable
// nodeId keeps two different elements from ever being deep-equal.
function internal(obj, props) {
  for (const [k, v] of Object.entries(props)) Object.defineProperty(obj, k, { value: v, writable: true, configurable: true });
}
let serial = 0;

const captureOf = (opts) => (typeof opts === "boolean" ? opts : !!opts?.capture);

class Target {
  constructor() { internal(this, { _listeners: [] }); }
  addEventListener(type, fn, opts) {
    const capture = captureOf(opts);
    if (!fn || this._listeners.some((l) => l.type === type && l.fn === fn && l.capture === capture)) return;
    this._listeners.push({ type, fn, capture, once: !!opts?.once });
  }
  removeEventListener(type, fn, opts) {
    const capture = captureOf(opts);
    this._listeners = this._listeners.filter((l) => !(l.type === type && l.fn === fn && l.capture === capture));
  }
  _invoke(ev, capture) {
    for (const l of [...this._listeners]) {
      if (l.type !== ev.type || l.capture !== capture) continue;
      if (l.once) this.removeEventListener(l.type, l.fn, l.capture);
      ev.currentTarget = this;
      typeof l.fn === "function" ? l.fn.call(this, ev) : l.fn.handleEvent(ev);
      if (ev._stoppedNow) return;
    }
  }
}

// Capture from the window down, both phases at the target, then bubble back up.
function dispatch(target, ev, win) {
  ev.target = target;
  const path = [];
  for (let n = target.parentNode; n; n = n.parentNode) path.push(n);
  if (win && (path.at(-1) || target) instanceof Document) path.push(win);
  for (let i = path.length - 1; i >= 0 && !ev._stopped; i--) path[i]._invoke(ev, true);
  if (!ev._stopped) target._invoke(ev, true);
  if (!ev._stopped) target._invoke(ev, false);
  for (let i = 0; ev.bubbles && i < path.length && !ev._stopped; i++) path[i]._invoke(ev, false);
  ev.currentTarget = null;
  return !ev.defaultPrevented;
}

// ------------------------------------------------------------------ nodes

class Node extends Target {
  constructor() {
    super();
    this.nodeId = ++serial;
    internal(this, { parentNode: null, childNodes: [] });
  }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes.at(-1) || null; }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n instanceof Document;
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) {
    this.replaceChildren();
    const s = v === null || v === undefined ? "" : String(v);
    if (s) this.appendChild(new Text(s));
  }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, ref) {
    if (typeof node === "string") node = new Text(node);
    if (!(node instanceof Node)) throw new TypeError(`fakedom: not a node: ${node}`);
    for (let n = this; n; n = n.parentNode) if (n === node) throw new Error("fakedom: a node can't hold itself");
    node.remove();
    const at = ref ? this.childNodes.indexOf(ref) : -1;
    if (at < 0) this.childNodes.push(node); else this.childNodes.splice(at, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) { node.remove(); return node; }
  remove() {
    const p = this.parentNode;
    if (!p) return;
    p.childNodes.splice(p.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  append(...nodes) { for (const n of nodes) this.appendChild(typeof n === "string" ? new Text(n) : n); }
  prepend(...nodes) {
    const first = this.firstChild;
    for (const n of nodes) this.insertBefore(typeof n === "string" ? new Text(n) : n, first);
  }
  replaceChildren(...nodes) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  before(...nodes) { for (const n of nodes) this.parentNode.insertBefore(typeof n === "string" ? new Text(n) : n, this); }
  after(...nodes) {
    const next = this.parentNode.childNodes[this.parentNode.childNodes.indexOf(this) + 1] || null;
    for (const n of nodes) this.parentNode.insertBefore(typeof n === "string" ? new Text(n) : n, next);
  }
  replaceWith(...nodes) { this.before(...nodes); this.remove(); }
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  // a failed assertion prints this, not the whole page each node leads to
  [Symbol.for("nodejs.util.inspect.custom")]() {
    if (this instanceof Text) return `#text ${JSON.stringify(this.data.slice(0, 40))}`;
    if (this instanceof Document) return "#document";
    return `<${this.localName}${this.id ? `#${this.id}` : ""}${this.className ? `.${this.className.split(/\s+/).join(".")}` : ""}>`;
  }
}

class Text extends Node {
  constructor(data) {
    super();
    this.data = String(data);
  }
  get nodeType() { return 3; }
  get nodeValue() { return this.data; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = v === null || v === undefined ? "" : String(v); }
}

class ClassList {
  constructor(el) { this._el = el; }
  _get() { return this._el.className.split(/\s+/).filter(Boolean); }
  add(...names) {
    const l = this._get();
    for (const c of names) if (!l.includes(c)) l.push(c);
    this._el.className = l.join(" ");
  }
  remove(...names) { this._el.className = this._get().filter((c) => !names.includes(c)).join(" "); }
  toggle(name, force) {
    const want = force === undefined ? !this.contains(name) : !!force;
    if (want) this.add(name); else this.remove(name);
    return want;
  }
  contains(name) { return this._get().includes(name); }
  replace(a, b) {
    if (!this.contains(a)) return false;
    this._el.className = this._get().map((c) => (c === a ? b : c)).join(" ");
    return true;
  }
  get length() { return this._get().length; }
  [Symbol.iterator]() { return this._get()[Symbol.iterator](); }
}

function makeStyle() {
  const props = {
    setProperty(k, v) { this[k] = String(v); },
    removeProperty(k) { delete this[k]; },
    getPropertyValue(k) { return this[k] ?? ""; },
  };
  return new Proxy(props, { get: (t, k) => (k in t ? t[k] : typeof k === "string" ? "" : undefined) });
}

const dataAttr = (k) => `data-${k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
function makeDataset(el) {
  return new Proxy({}, {
    get: (t, k) => (typeof k === "string" ? el.getAttribute(dataAttr(k)) ?? undefined : undefined),
    set: (t, k, v) => { el.setAttribute(dataAttr(k), v); return true; },
    deleteProperty: (t, k) => { el.removeAttribute(dataAttr(k)); return true; },
    has: (t, k) => typeof k === "string" && el.hasAttribute(dataAttr(k)),
  });
}

function makeCanvasContext() {
  return new Proxy({}, {
    get: (t, k) => (k in t ? t[k] : () => undefined),
    set: (t, k, v) => { t[k] = v; return true; },
  });
}

class Element extends Node {
  constructor(tag, doc) {
    super();
    this.localName = tag.toLowerCase();
    this.tagName = tag.toUpperCase();
    internal(this, {
      _doc: doc, _attrs: new Map(), style: makeStyle(), classList: null, dataset: null,
      _value: undefined, _checked: undefined, _selected: undefined, _ctx: undefined,
      scrollTop: 0, scrollLeft: 0, scrollHeight: 0, scrollWidth: 0, clientHeight: 0, clientWidth: 0,
      offsetWidth: 0, offsetHeight: 0, selectionStart: 0, selectionEnd: 0,
    });
    this.classList = new ClassList(this);
    this.dataset = makeDataset(this);
  }
  get nodeType() { return 1; }
  get nodeName() { return this.tagName; }
  get ownerDocument() { return this._doc; }
  get children() { return this.childNodes.filter((c) => c instanceof Element); }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children.at(-1) || null; }
  get childElementCount() { return this.children.length; }
  get nextElementSibling() {
    const sib = this.parentNode?.children || [];
    return sib[sib.indexOf(this) + 1] || null;
  }
  get previousElementSibling() {
    const sib = this.parentNode?.children || [];
    return sib[sib.indexOf(this) - 1] || null;
  }

  setAttribute(k, v) { this._attrs.set(String(k).toLowerCase(), String(v)); }
  getAttribute(k) { return this._attrs.get(String(k).toLowerCase()) ?? null; }
  hasAttribute(k) { return this._attrs.has(String(k).toLowerCase()); }
  removeAttribute(k) { this._attrs.delete(String(k).toLowerCase()); }
  toggleAttribute(k, force) {
    const want = force === undefined ? !this.hasAttribute(k) : !!force;
    if (want) this.setAttribute(k, ""); else this.removeAttribute(k);
    return want;
  }
  get attributes() { return [...this._attrs].map(([name, value]) => ({ name, value })); }

  get id() { return this.getAttribute("id") || ""; }
  set id(v) { this.setAttribute("id", v); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get type() { return (this.getAttribute("type") || (this.localName === "input" ? "text" : "")).toLowerCase(); }
  set type(v) { this.setAttribute("type", v); }

  // checkedness and selectedness start from the attribute, then are the element's own
  get checked() { return this._checked ?? this.hasAttribute("checked"); }
  set checked(v) { this._checked = !!v; }
  get selected() { return this._selected ?? this.hasAttribute("selected"); }
  set selected(v) {
    const select = this.closest("select");
    if (v && select && !select.multiple) for (const o of select.options) o._selected = false;
    this._selected = !!v;
  }
  get options() { return this.querySelectorAll("option"); }
  get selectedIndex() { return this.options.findIndex((o) => o.selected); }
  get value() {
    if (this.localName === "select") {
      const o = this.options.find((x) => x.selected) || this.options[0];
      return o ? o.value : "";
    }
    if (this.localName === "option") return this.getAttribute("value") ?? this.textContent;
    if (this._value !== undefined) return this._value;
    if (this.localName === "textarea") return this.textContent;
    return this.getAttribute("value") ?? "";
  }
  set value(v) {
    if (this.localName === "select") {
      for (const o of this.options) o._selected = o.value === String(v);
      return;
    }
    if (this.localName === "option") return this.setAttribute("value", v);
    this._value = v === null || v === undefined ? "" : String(v);
    this.selectionStart = this.selectionEnd = this._value.length;
  }

  get innerHTML() { return this.childNodes.map(serialize).join(""); }
  set innerHTML(html) { this.replaceChildren(...parseHTML(String(html ?? ""), this._doc)); }
  get outerHTML() { return serialize(this); }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }

  querySelectorAll(sel) {
    const groups = parseSelector(sel);
    const out = [];
    const walk = (el) => {
      for (const c of el.children) {
        if (matches(c, groups)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  matches(sel) { return matches(this, parseSelector(sel)); }
  closest(sel) {
    const groups = parseSelector(sel);
    for (let n = this; n instanceof Element; n = n.parentNode) if (matches(n, groups)) return n;
    return null;
  }

  focus() { if (this._doc) this._doc.activeElement = this; }
  blur() { if (this._doc?.activeElement === this) this._doc.activeElement = this._doc.body; }
  select() {
    this.selectionStart = 0;
    this.selectionEnd = this.value.length;
  }
  setSelectionRange(a, b) {
    this.selectionStart = a;
    this.selectionEnd = b;
  }
  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }
  dispatchEvent(ev) { return dispatch(this, ev, this._doc?._win); }
  scrollIntoView() {}
  scrollTo() {}
  getBoundingClientRect() { return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  getClientRects() { return []; }
  getContext() { return (this._ctx ??= makeCanvasContext()); }
  toDataURL(type = "image/png") { return `data:${type};base64,RkFLRQ==`; }
}

// boolean attributes that the property reflects
for (const [prop, attr] of [["disabled", "disabled"], ["hidden", "hidden"], ["readOnly", "readonly"],
  ["multiple", "multiple"], ["required", "required"], ["open", "open"]]) {
  Object.defineProperty(Element.prototype, prop, {
    get() { return this.hasAttribute(attr); },
    set(v) { this.toggleAttribute(attr, !!v); },
  });
}
// string attributes that the property reflects
for (const attr of ["title", "href", "src", "placeholder", "name", "rel", "target", "alt", "accept", "role"]) {
  Object.defineProperty(Element.prototype, attr, {
    get() { return this.getAttribute(attr) ?? ""; },
    set(v) { this.setAttribute(attr, v); },
  });
}

class Document extends Node {
  constructor(html) {
    super();
    this.title = "";
    this.hidden = false;
    this.visibilityState = "visible";
    internal(this, { _win: null, documentElement: null, head: null, body: null, activeElement: null });
    for (const n of parseHTML(html, this)) if (n instanceof Element) this.appendChild(n);
    this.documentElement = this.childNodes.find((n) => n.localName === "html");
    this.head = this.documentElement.querySelector("head");
    this.body = this.documentElement.querySelector("body");
    this.activeElement = this.body;
  }
  get nodeType() { return 9; }
  get children() { return this.childNodes.filter((c) => c instanceof Element); }
  createElement(tag) { return new Element(tag, this); }
  createTextNode(text) { return new Text(text); }
  querySelectorAll(sel) {
    const groups = parseSelector(sel);
    return [...(matches(this.documentElement, groups) ? [this.documentElement] : []), ...this.documentElement.querySelectorAll(sel)];
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getElementById(id) {
    const walk = (el) => {
      if (el.id === id) return el;
      for (const c of el.children) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this.documentElement);
  }
  dispatchEvent(ev) { return dispatch(this, ev, this._win); }
  execCommand() { return true; }
  hasFocus() { return true; }
}

// ------------------------------------------------------------------ HTML in and out

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
const decode = (s) => s.replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) => (e[0] !== "#" ? ENTITIES[e] ?? m
  : String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10))));
const escText = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escAttr = (s) => escText(s).replace(/"/g, "&quot;");

// Well-formed HTML as index.html and the page's own fragments are: tags, attributes, text,
// comments, raw-text elements. No implied end tags.
function parseHTML(html, doc) {
  const holder = new Element("template", doc);
  let cur = holder;
  const tags = /<!--[\s\S]*?-->|<![^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*(\/?)>|([^<]+|<)/g;
  let m;
  while ((m = tags.exec(html))) {
    if (m[1]) {
      const tag = m[1].toUpperCase();
      for (let n = cur; n !== holder; n = n.parentNode) if (n.tagName === tag) { cur = n.parentNode; break; }
    } else if (m[2]) {
      const el = new Element(m[2], doc);
      for (const a of m[3].matchAll(/([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
        el.setAttribute(a[1], decode(a[2] ?? a[3] ?? a[4] ?? ""));
      }
      cur.appendChild(el);
      const tag = el.localName;
      if (RAW_TEXT.has(tag)) {
        const end = html.toLowerCase().indexOf(`</${tag}`, tags.lastIndex);
        const stop = end < 0 ? html.length : end;
        const text = html.slice(tags.lastIndex, stop);
        if (text) el.appendChild(new Text(tag === "textarea" ? decode(text) : text));
        tags.lastIndex = end < 0 ? html.length : html.indexOf(">", end) + 1;
      } else if (!VOID.has(tag) && !m[4]) {
        cur = el;
      }
    } else if (m[5] !== undefined) {
      cur.appendChild(new Text(decode(m[5])));
    }
  }
  const nodes = [...holder.childNodes];
  holder.replaceChildren();
  return nodes;
}

function serialize(n) {
  if (n instanceof Text) return escText(n.data);
  const attrs = [...n._attrs].map(([k, v]) => ` ${k}="${escAttr(v)}"`).join("");
  if (VOID.has(n.localName)) return `<${n.localName}${attrs}>`;
  return `<${n.localName}${attrs}>${n.childNodes.map(serialize).join("")}</${n.localName}>`;
}

// ------------------------------------------------------------------ selectors

const cssUnescape = (s) => s.replace(/\\([0-9a-fA-F]{1,6}\s?|[\s\S])/g, (m, e) => (/^[0-9a-fA-F]/.test(e) && e.length > 1
  ? String.fromCodePoint(parseInt(e, 16)) : e));
// CSS.escape as the page uses it (on keys like "search:7")
const cssEscape = (s) => String(s).replace(/^\d/, (d) => `\\3${d} `).replace(/[^\w-]/g, (c) => `\\${c}`);

// groups (a, b) of compounds joined by " " or ">"; compounds of tag, #id, .class, [attr],
// [attr=value], :checked, :disabled, :first-child, :last-child and :not(...)
function parseSelector(sel) {
  const groups = [];
  let group = [], comb = null, i = 0;
  const s = String(sel).trim();
  while (i < s.length) {
    const ch = s[i];
    if (ch === ",") {
      groups.push(group);
      group = [];
      comb = null;
      i++;
    } else if (/\s/.test(ch)) {
      if (group.length && !comb) comb = " ";
      i++;
    } else if (ch === ">") {
      comb = ">";
      i++;
    } else {
      const [parts, next] = parseCompound(s, i);
      group.push({ comb: group.length ? comb || " " : null, parts });
      comb = null;
      i = next;
    }
  }
  groups.push(group);
  if (groups.some((g) => !g.length)) throw new Error(`fakedom: can't read the selector ${sel}`);
  return groups;
}

function parseCompound(s, i) {
  const parts = [];
  while (i < s.length && !/[\s,>]/.test(s[i])) {
    const rest = s.slice(i);
    let m;
    if ((m = /^\*/.exec(rest))) parts.push({ t: "any" });
    else if ((m = /^[a-zA-Z][\w-]*/.exec(rest))) parts.push({ t: "tag", v: m[0].toUpperCase() });
    else if ((m = /^#((?:[\w-]|\\.)+)/.exec(rest))) parts.push({ t: "id", v: cssUnescape(m[1]) });
    else if ((m = /^\.((?:[\w-]|\\.)+)/.exec(rest))) parts.push({ t: "class", v: cssUnescape(m[1]) });
    else if ((m = /^\[\s*([\w-]+)\s*(?:=\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^\]\s]+)))?\s*\]/.exec(rest))) {
      const v = m[2] ?? m[3] ?? m[4];
      parts.push({ t: "attr", k: m[1], v: v === undefined ? null : cssUnescape(v) });
    } else if ((m = /^:not\(/.exec(rest))) {
      let depth = 0, j = i + 5;
      for (; j < s.length; j++) {
        if (s[j] === "(") depth++;
        else if (s[j] === ")" && depth-- === 0) break;
      }
      parts.push({ t: "not", groups: parseSelector(s.slice(i + 5, j)) });
      m = [s.slice(i, j + 1)];
    } else if ((m = /^:(checked|disabled|first-child|last-child)\b/.exec(rest))) parts.push({ t: m[1] });
    else throw new Error(`fakedom: can't read the selector ${s}`);
    i += m[0].length;
  }
  return [parts, i];
}

function matchesParts(el, parts) {
  for (const p of parts) {
    if (p.t === "tag" && el.tagName !== p.v) return false;
    if (p.t === "id" && el.id !== p.v) return false;
    if (p.t === "class" && !el.classList.contains(p.v)) return false;
    if (p.t === "attr" && (el.getAttribute(p.k) === null || (p.v !== null && el.getAttribute(p.k) !== p.v))) return false;
    if (p.t === "not" && matches(el, p.groups)) return false;
    if (p.t === "checked" && !el.checked) return false;
    if (p.t === "disabled" && !el.disabled) return false;
    if (p.t === "first-child" && el.parentNode?.children[0] !== el) return false;
    if (p.t === "last-child" && el.parentNode?.children.at(-1) !== el) return false;
  }
  return true;
}

function matches(el, groups) {
  const at = (node, group, k) => {
    if (!matchesParts(node, group[k].parts)) return false;
    if (k === 0) return true;
    if (group[k].comb === ">") return node.parentElement ? at(node.parentElement, group, k - 1) : false;
    for (let p = node.parentElement; p; p = p.parentElement) if (at(p, group, k - 1)) return true;
    return false;
  };
  return groups.some((g) => at(el, g, g.length - 1));
}

// ------------------------------------------------------------------ time

// Timers run only when a test moves the clock (tick), in the order they fall due, with the
// promises they start settled before the next one.
class Clock {
  constructor() {
    this.now = 0;
    this.seq = 0;
    this.timers = new Map();
    this.setTimeout = (fn, ms = 0, ...args) => this._add(fn, ms, args, false);
    this.setInterval = (fn, ms = 0, ...args) => this._add(fn, ms, args, true);
    this.clearTimeout = this.clearInterval = (id) => { this.timers.delete(id); };
  }
  _add(fn, ms, args, repeat) {
    const id = ++this.seq;
    const every = Math.max(repeat ? 1 : 0, Number(ms) || 0);
    this.timers.set(id, { at: this.now + every, every: repeat ? every : 0, fn, args });
    return id;
  }
  async tick(ms = 0) {
    const end = this.now + ms;
    for (let runs = 0; ; runs++) {
      // a timer that keeps setting itself again would hold the test here, growing, for ever
      if (runs > 10000) throw new Error("fakedom: over 10000 timers ran in one tick: something keeps scheduling itself");
      await settle();
      let next = null;
      for (const [id, t] of this.timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
      if (!next) break;
      const [id, t] = next;
      this.now = t.at;
      if (t.every) t.at += t.every; else this.timers.delete(id);
      t.fn(...t.args);
    }
    this.now = end;
    await settle();
  }
}

async function settle() {
  for (let i = 0; i < 3; i++) await new Promise((resolve) => setImmediate(resolve));
}

// ------------------------------------------------------------------ the page

function makeStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
    clear: () => data.clear(),
  };
}

// The frontend in a fresh page: `app.run(code)` evaluates in the page (its `const`s, such as S,
// are only reachable that way); `app.call(name, ...args)` calls one of its functions.
function loadApp({ token = "tok", port = 48599 } = {}) {
  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const source = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
  const document = new Document(html);
  const clock = new Clock();
  const app = {
    document, clock, calls: [], routes: {}, sockets: [], terminals: [], clipboard: "", objectUrls: [], revoked: [],
    opened: [], decodable: (src) => /^(data:image\/(png|jpeg|gif|webp)[;,]|blob:)/.test(src),
  };

  app.routes = {
    "GET /api/phone": () => ({ running: false, phones: [] }),
    "GET /api/cases": () => ({ cases: [] }),
    "GET /api/draft": () => ({ text: "", images: [] }),
    "POST /api/draft": () => ({ ok: true }),
    "POST /api/layout": () => ({ ok: true }),
  };
  // A route answers with a value, a promise of one (a held request) or reply(status, body).
  async function fetch(url, opts = {}) {
    const u = new URL(url, `http://127.0.0.1:${port}`);
    const method = (opts.method || "GET").toUpperCase();
    const call = { method, path: u.pathname, query: Object.fromEntries(u.searchParams),
      body: typeof opts.body === "string" ? JSON.parse(opts.body) : opts.body, headers: opts.headers || {} };
    app.calls.push(call);
    const route = app.routes[`${method} ${u.pathname}`];
    const out = await (route ? route(call) : { __status: 404, body: { detail: `no route ${method} ${u.pathname} in the test` } });
    const status = out?.__status ?? 200;
    const data = out?.__status ? out.body : out;
    return { ok: status < 400, status, statusText: status < 400 ? "OK" : "Error",
      json: async () => data, text: async () => JSON.stringify(data), blob: async () => new Blob([]) };
  }

  class FakeWebSocket extends Target {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      app.sockets.push(this);
    }
    send(data) { this.sent.push(data); }
    close() {
      this.readyState = 3;
      this.onclose?.(new Event("close"));
    }
    open() {
      this.readyState = 1;
      this.onopen?.(new Event("open"));
    }
    receive(data) { this.onmessage?.({ data: typeof data === "string" ? data : JSON.stringify(data) }); }
  }
  Object.assign(FakeWebSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });

  class FakeTerminal {
    constructor(options) {
      this.options = { ...options };
      this.cols = 80;
      this.rows = 24;
      this.pasted = [];
      this.written = [];
      this.markers = [];
      this.selection = "";
      const buffer = { type: "normal", length: 0, baseY: 0, cursorY: 0, viewportY: 0, getLine: () => undefined };
      this.buffer = { active: buffer, normal: buffer };
      app.terminals.push(this);
    }
    loadAddon() {}
    open(el) { this.element = el; }
    onData(fn) { this.dataHandler = fn; return { dispose() {} }; }
    onResize(fn) { this.resizeHandler = fn; return { dispose() {} }; }
    attachCustomKeyEventHandler(fn) { this.keyHandler = fn; }
    write(data) { this.written.push(data); }
    paste(text) { this.pasted.push(text); }
    hasSelection() { return !!this.selection; }
    getSelection() { return this.selection; }
    clearSelection() { this.selection = ""; }
    registerMarker() {
      const m = { line: 0, isDisposed: false, dispose() { this.isDisposed = true; }, onDispose() {} };
      this.markers.push(m);
      return m;
    }
    focus() {}
    blur() {}
    scrollToBottom() {}
    refresh() {}
    dispose() { this.disposed = true; }
  }

  class FakeImage extends Target {
    constructor() {
      super();
      this.naturalWidth = this.naturalHeight = this.width = this.height = 0;
    }
    get src() { return this._src; }
    set src(v) {
      this._src = v;
      Promise.resolve().then(() => {
        if (!app.decodable(v)) return this.onerror?.(new Event("error"));
        this.naturalWidth = this.width = 640;
        this.naturalHeight = this.height = 480;
        this.onload?.(new Event("load"));
      });
    }
  }

  class FakeURL extends URL {
    static createObjectURL() {
      const url = `blob:fake/${app.objectUrls.length + 1}`;
      app.objectUrls.push(url);
      return url;
    }
    static revokeObjectURL(url) { app.revoked.push(url); }
  }

  const unavailable = (what) => class { constructor() { throw new Error(`${what} isn't available in these tests`); } };
  const win = new Target();
  Object.assign(win, {
    console, Blob, TextDecoder, TextEncoder, AbortController, URLSearchParams, structuredClone, queueMicrotask,
    URL: FakeURL, Event, KeyboardEvent: Event, MouseEvent: Event, ClipboardEvent: Event,
    Node, Element, HTMLElement: Element, Text, Document,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    requestAnimationFrame: (fn) => clock.setTimeout(() => fn(clock.now), 16), cancelAnimationFrame: clock.clearTimeout,
    document,
    location: { search: `?t=${token}`, host: `127.0.0.1:${port}`, hostname: "127.0.0.1", port: String(port),
      protocol: "http:", origin: `http://127.0.0.1:${port}`, href: `http://127.0.0.1:${port}/?t=${token}`, reload() {} },
    history: { replaceState: (state, title, url) => { app.historyUrl = url; } },
    sessionStorage: makeStorage(),
    localStorage: makeStorage(),
    navigator: { userAgent: "fakedom", platform: "Linux x86_64",
      clipboard: { writeText: async (t) => { app.clipboard = t; }, readText: async () => app.clipboard } },
    fetch, WebSocket: FakeWebSocket, Image: FakeImage,
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
    innerWidth: 1600, innerHeight: 1000, devicePixelRatio: 1,
    getComputedStyle: (el) => el.style,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    getSelection: () => ({ toString: () => "", removeAllRanges() {} }),
    open: (url) => { app.opened.push(url); },
    CSS: { escape: cssEscape },
    Terminal: FakeTerminal,
    FitAddon: { FitAddon: class { fit() {} proposeDimensions() { return { cols: 80, rows: 24 }; } activate() {} dispose() {} } },
    marked: { parse: (text) => `<p>${escText(String(text))}</p>` },
    DOMPurify: { addHook() {}, sanitize: (html) => html },
    Guacamole: { Client: unavailable("Guacamole"), Keyboard: unavailable("Guacamole"), Mouse: unavailable("Guacamole"),
      WebSocketTunnel: unavailable("Guacamole"), StringReader: unavailable("Guacamole"), StringWriter: unavailable("Guacamole") },
  });
  win.window = win;
  win.self = win;
  document._win = win;
  vm.createContext(win);

  app.window = win;
  app.run = (code) => vm.runInContext(code, win);
  app.call = (name, ...args) => win[name](...args);
  app.$ = (sel) => document.querySelector(sel);
  app.events = () => app.sockets.find((s) => s.url.includes("/ws/events"));
  app.event = async (ev) => {
    app.events().receive(ev);
    await clock.tick(0);
  };
  app.posts = (p) => app.calls.filter((c) => c.method === "POST" && c.path === p);
  app.gets = (p) => app.calls.filter((c) => c.method === "GET" && c.path === p);
  app.toasts = () => document.querySelectorAll("#toasts .toast").map((t) => t.textContent);
  // a reply held until the test lets it go: route(call) waits for release(value)
  app.hold = (key, value) => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const calls = [];
    app.routes[key] = (call) => { calls.push(call); return gate.then(() => (typeof value === "function" ? value(call) : value)); };
    return { calls, release: async () => { release(); await clock.tick(0); } };
  };
  app.key = (target, init) => {
    const ev = new Event("keydown", init);
    target.dispatchEvent(ev);
    return ev;
  };
  app.type = async (el, text) => {
    el.value = text;
    el.dispatchEvent(new Event("input"));
    await clock.tick(0);
  };

  vm.runInContext(source, win, { filename: path.join(WEB, "app.js") });
  return app;
}

module.exports = { loadApp, Event, Element, Text, Document, parseHTML, parseSelector, settle, WEB };
