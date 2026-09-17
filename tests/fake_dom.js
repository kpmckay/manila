/* A DOM small enough to read, real enough to render into.
 *
 * Manila's browser code has had a run of bugs that live only in the render
 * path -- a header nobody told, a picker that never redrew, a table that
 * repainted from rows it had not refetched. None of them could be caught by
 * testing pure functions, because none of them are in one.
 *
 * This is not a browser. It keeps a tree, dispatches events, and answers the
 * handful of questions the app actually asks. That is enough to run boot(),
 * click things, and see what the page ends up saying.
 */

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.nodeType = 1;
    this.attributes = {};
    this.listeners = {};
    this.style = new Style();
    this.dataset = {};
    this._text = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.classList = new ClassList(this);
  }

  get className() { return this.attributes.class || ""; }
  set className(v) { this.attributes.class = v; }

  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }

  append(...nodes) {
    for (const node of nodes) {
      if (node === undefined || node === null) continue;
      const child = typeof node === "string" ? text(node) : node;
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }
  appendChild(node) { this.append(node); return node; }
  prepend(node) { node.parentNode = this; this.childNodes.unshift(node); }
  remove() {
    const kin = this.parentNode?.childNodes;
    if (kin) kin.splice(kin.indexOf(this), 1);
    this.parentNode = null;
  }
  replaceWith(node) {
    const kin = this.parentNode?.childNodes;
    if (!kin) return;
    kin.splice(kin.indexOf(this), 1, node);
    node.parentNode = this.parentNode;
  }

  /* Setting textContent makes a text node, exactly as the browser does. It
   * used to be kept off to the side, which meant appending anything -- a count
   * badge onto a tab, say -- silently threw the element's own words away, and
   * the test read back "" for a button the app had labelled. */
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(v) {
    const written = String(v ?? "");
    this.childNodes = written ? [text(written)] : [];
  }

  get innerHTML() { return this._html || ""; }
  set innerHTML(v) { this._html = String(v ?? ""); this.childNodes = []; }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }

  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  dispatch(type, event = {}) {
    const detail = { type, target: this, preventDefault() {}, stopPropagation() {},
                     ...event };
    for (const fn of this.listeners[type] || []) fn(detail);
    const inline = this[`on${type}`];
    if (typeof inline === "function") inline(detail);
  }
  click() { this.dispatch("click"); }
  focus() { this.ownerDocument.activeElement = this; }
  select() {}
  insertBefore(node, before) {
    const at = this.childNodes.indexOf(before);
    node.parentNode = this;
    this.childNodes.splice(at < 0 ? this.childNodes.length : at, 0, node);
    return node;
  }
  contains(node) {
    if (node === this) return true;
    return this.childNodes.some((c) => c.contains?.(node));
  }
  closest(selector) {
    let here = this;
    while (here) {
      if (here.matches?.(selector)) return here;
      here = here.parentNode;
    }
    return null;
  }
  matches(selector) {
    return selector.split(",").map((s) => s.trim()).some((one) => {
      if (one.startsWith(".")) return this.classList.contains(one.slice(1));
      return this.tagName === one.toUpperCase();
    });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === 1) {
          if (child.matches(selector.replace(/\[.*/, "").trim() || "*")) found.push(child);
          walk(child);
        }
      }
    };
    walk(this);
    return found;
  }
}

class ClassList {
  constructor(node) { this.node = node; }
  get list() { return (this.node.className || "").split(/\s+/).filter(Boolean); }
  set list(v) { this.node.className = v.join(" "); }
  add(...names) { this.list = [...new Set([...this.list, ...names])]; }
  remove(...names) { this.list = this.list.filter((n) => !names.includes(n)); }
  contains(name) { return this.list.includes(name); }
  toggle(name, on) {
    const want = on === undefined ? !this.contains(name) : on;
    want ? this.add(name) : this.remove(name);
  }
}

class Style {
  setProperty(name, value) { this[name] = value; }
  removeProperty(name) { delete this[name]; }
}

function text(value) {
  return { nodeType: 3, textContent: String(value), childNodes: [], contains: () => false };
}

/** A document with the elements index.html provides, and nothing else. */
export function makeDocument(ids) {
  const doc = {
    createElement: (tag) => {
      const node = new Node(tag);
      node.ownerDocument = doc;
      return node;
    },
    body: null,
    documentElement: null,
    activeElement: null,
    title: "",
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    dispatch(type, event = {}) {
      for (const fn of this.listeners[type] || []) fn({ type, ...event });
    },
    getSelection: () => null,
    querySelector: (s) => doc.body.querySelector(s),
    querySelectorAll: (s) => doc.body.querySelectorAll(s),
    getElementById: (id) => doc.known[id] || null,
    execCommand: () => true,
  };
  doc.body = doc.createElement("body");
  doc.documentElement = doc.createElement("html");
  doc.known = {};
  for (const id of ids) {
    const node = doc.createElement("div");
    node.id = id;
    doc.known[id] = node;
    doc.body.append(node);
  }
  return doc;
}

/** Timers you can run on purpose, so a test never waits on a clock. */
export function makeClock() {
  let queue = [];
  let id = 0;
  return {
    setTimeout: (fn, delay = 0) => { queue.push({ id: ++id, fn, delay }); return id; },
    clearTimeout: (which) => { queue = queue.filter((t) => t.id !== which); },
    /** Fire everything due, repeatedly, until the queue is quiet. */
    async run(rounds = 20) {
      for (let i = 0; i < rounds && queue.length; i++) {
        const due = queue;
        queue = [];
        for (const timer of due) timer.fn();
        await settle();
      }
    },
    pending: () => queue.length,
  };
}

/** Let every outstanding promise resolve. */
export async function settle(turns = 60) {
  for (let i = 0; i < turns; i++) await Promise.resolve();
}
