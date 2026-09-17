/* The Repository: a per-folder canvas of manila-folder frames.
 *
 * Three kinds, all positioned by hand -- side by side, stacked, anywhere:
 *   text   a titled, scrollable, resizable note
 *   files  a consolidated list of documents, each a link that opens elsewhere
 *   image  one picture, shown at its own size
 *
 * Manila does not display documents. It keeps a copy and hands them off to
 * whatever the machine already uses to open them. */

import { api, toast } from "./api.js";
import { el } from "./table.js";
import { markUnsaved } from "./status.js";
import { richEditor } from "./editor.js";

const MIN_W = 200;
const MIN_H = 120;
const MIN_CANVAS = 360;
const CANVAS_PAD = 40;
const SAVE_DELAY = 600;

const KINDS = {
  text:  { label: "Text", w: 360, h: 260 },
  files: { label: "Documents", w: 320, h: 240 },
  image: { label: "Picture", w: 320, h: 280 },
};

/* Lists in a plain text box.
 *
 * The body stays plain text on purpose -- it is what a person reads in a file
 * manager, what grep finds, and what a model will be handed as context later.
 * So bullets are the markers you would type anyway; what is added is that the
 * box keeps typing them for you. */
const LIST_LINE = /^(\s*)([-*+\u2022]|\d+[.)])[ \t]+(\[[ xX]\][ \t]+)?(.*)$/;

function lineAt(text, at) {
  const from = text.lastIndexOf("\n", at - 1) + 1;
  return { from, line: text.slice(from, at) };
}

/** Whether the cursor sits on a list item. */
export function onListLine(text, at) {
  return LIST_LINE.test(lineAt(text, at).line);
}

/** Enter on a list item: the next item, already marked. */
export function listContinuation(text, at) {
  const { from, line } = lineAt(text, at);
  const found = line.match(LIST_LINE);
  if (!found) return null;
  const [, indent, marker, box, content] = found;

  // Enter on an empty item ends the list rather than making another empty one.
  if (!content.trim()) {
    return { text: text.slice(0, from) + indent + text.slice(at),
             cursor: from + indent.length };
  }
  const numbered = /^\d+[.)]$/.test(marker);
  const next = numbered
    ? `${parseInt(marker, 10) + 1}${marker.slice(-1)}`
    : marker;
  const prefix = `\n${indent}${next} ${box ? "[ ] " : ""}`;
  return { text: text.slice(0, at) + prefix + text.slice(at), cursor: at + prefix.length };
}

/** Tab and Shift+Tab across whatever lines the selection touches. */
export function shiftIndent(text, from, to, outdent) {
  const start = text.lastIndexOf("\n", from - 1) + 1;
  let end = text.indexOf("\n", to);
  if (end === -1) end = text.length;
  const shifted = text.slice(start, end).split("\n")
    .map((line) => outdent ? line.replace(/^(\t| {1,2})/, "") : `  ${line}`)
    .join("\n");
  return { text: text.slice(0, start) + shifted + text.slice(end),
           from: start, to: start + shifted.length };
}

/* Pasting a list in from somewhere else.
 *
 * Notes arrive from OneNote, Word, Teams and web pages, and their plain-text
 * flavour is a mess: bullet glyphs instead of markers, tabs and non-breaking
 * spaces instead of indentation, and nesting that is only implied. Where the
 * clipboard also carries HTML, the real structure is in there and is worth
 * reading rather than guessing at.
 *
 * The result is still plain text -- markers you could have typed yourself. */

// Bullet glyphs other programs use. Deliberately not "o" or "-": one is a real
// word and the other already means what it should.
//
// The \uF0xx range is Word and Outlook writing Symbol-font characters straight
// out as private-use code points -- F0B7 is their round bullet, F06F the hollow
// one, F0A7 the square. Nothing legitimate ever uses that range, so matching it
// is safe.
const GLYPHS = "\u2022\u00b7\u25e6\u25aa\u25ab\u25cf\u25cb\u2023\u2043\u2219"
             + "\uf0b7\uf06f\uf0a7\uf0d8\uf0fc\uf095\uf0a4";
const GLYPH_LINE = new RegExp(`^([ \\t]*)[${GLYPHS}][ \\t]+(.*)$`);

/** Tidy pasted plain text into markers this box already understands. */
export function normalizeList(text) {
  if (!text) return "";
  return text
    .replace(/\r\n?/g, "\n")
    .replace(/\u00a0/g, " ")             // Word pads with non-breaking spaces
    .split("\n")
    .map((line) => {
      const indented = line.replace(/^\t+/, (tabs) => "  ".repeat(tabs.length));
      const bullet = indented.match(GLYPH_LINE);
      // Word pads between a marker and its text with a run of spacer spaces;
      // one space is what a marker actually needs.
      const marked = bullet
        ? `${bullet[1]}- ${bullet[2]}`
        // Word's sub-levels are lettered and roman as well as numbered, and it
        // pads after all of them. Only runs are collapsed, so prose that merely
        // opens with "A. Smith" is left exactly as written.
        : indented.replace(/^([ \t]*)((?:\d+|[A-Za-z]+)[.)])[ \t]{2,}/, "$1$2 ");
      return marked.replace(/\s+$/, "");
    })
    .join("\n")
    .replace(/\n{3,}/g, "\n\n");        // however many blank lines, at most one gap
}

const BLOCK = new Set(["P", "DIV", "H1", "H2", "H3", "H4", "H5", "H6",
                       "SECTION", "ARTICLE", "BLOCKQUOTE", "PRE", "TR"]);

/* Word and Outlook do not write real <ul> lists. They write ordinary
 * paragraphs with the bullet as literal text, and record the nesting only in a
 * style property:
 *
 *     <p style='mso-list:l0 level2 lfo1'><span>\u00b7  </span>the item</p>
 *
 * Without reading that, every level of an Outlook list arrives flattened
 * against the left margin. */
export function wordListLevel(node) {
  const style = node.getAttribute?.("style") || "";
  const found = /mso-list\s*:[^;"']*\blevel(\d+)/i.exec(style);
  return found ? Number(found[1]) : 0;
}
const LIST = new Set(["UL", "OL"]);
const tagOf = (node) => (node.tagName || "").toUpperCase();
const isList = (node) => node.nodeType === 1 && LIST.has(tagOf(node));

/** Walk a DOM-ish tree into plain text, keeping list nesting and numbering. */
export function nodeToText(root) {
  const lines = [];
  let line = "";
  const flush = () => { lines.push(line.replace(/[\s\u00a0]+$/, "")); line = ""; };

  const walk = (node, depth) => {
    for (const child of node.childNodes || []) {
      // Ordinary whitespace collapses, as it does in a browser -- but not the
      // non-breaking kind, which is how Outlook and Word write indentation.
      // Collapsing those flattens an indented reply against the margin.
      if (child.nodeType === 3) {
        line += (child.textContent || "").replace(/[ \t\r\n\f\v]+/g, " ");
        continue;
      }
      if (child.nodeType !== 1) continue;
      const tag = tagOf(child);
      if (LIST.has(tag)) { if (line.trim()) flush(); walkList(child, depth + 1); continue; }
      if (tag === "BR") { flush(); continue; }
      if (tag === "TD" || tag === "TH") { walk(child, depth); line += "  "; continue; }
      if (BLOCK.has(tag)) {
        if (line.trim()) flush();
        const level = wordListLevel(child);
        if (level > 1) line = "  ".repeat(level - 1);
        walk(child, depth);
        if (line.trim()) flush();
        continue;
      }
      walk(child, depth);                 // spans, links, bold: text only
    }
  };

  const walkList = (list, depth) => {
    const ordered = tagOf(list) === "OL";
    let n = Number(list.getAttribute?.("start")) || 1;
    for (const item of list.childNodes || []) {
      if (item.nodeType !== 1 || tagOf(item) !== "LI") continue;
      line = `${"  ".repeat(depth - 1)}${ordered ? `${n++}.` : "-"} `;
      // The item's own text first; lists inside it are items in their own right.
      walk({ childNodes: [...(item.childNodes || [])].filter((c) => !isList(c)) }, depth);
      flush();
      for (const nested of item.childNodes || []) {
        if (isList(nested)) walkList(nested, depth + 1);
      }
    }
  };

  walk(root, 0);
  if (line.trim()) flush();
  return lines.join("\n");
}

/** A stored canvas height, or 0 if it is missing or not believable. */
export function usableHeight(raw) {
  const kept = Number(raw);
  return Number.isFinite(kept) && kept >= MIN_CANVAS ? kept : 0;
}

/** The height that shows every frame, never below the floor or a kept height. */
export function canvasHeight(frames, kept = 0) {
  const bottom = frames.reduce((low, f) => Math.max(low, f.y + f.h), 0);
  return Math.max(MIN_CANVAS, bottom + CANVAS_PAD, kept);
}

export class Repository {
  constructor(host, { folderId }) {
    this.host = host;
    this.folderId = folderId;
    this.frames = [];
    this.timers = new Map();
    this.appliedHeight = 0;
  }

  get heightKey() { return `manila.canvas.${this.folderId}`; }

  /** A height dragged by hand, if there is a believable one stored. */
  keptHeight() {
    try {
      return usableHeight(localStorage.getItem(this.heightKey));
    } catch { return 0; }
  }

  /* Watch for the canvas being dragged taller by its own corner.
   *
   * Only a real drag is recorded. The observer also fires for heights this
   * code just set, and once before the canvas is laid out at all -- where
   * clientHeight is 0. Storing either of those is what made the canvas come
   * back the wrong size. */
  watchSize() {
    if (typeof ResizeObserver !== "function") return;
    this.observer?.disconnect();
    this.observer = new ResizeObserver(() => {
      const height = Math.round(this.canvas.clientHeight);
      if (height < MIN_CANVAS) return;                     // not laid out yet
      if (Math.abs(height - this.appliedHeight) <= 2) return;  // our own doing
      localStorage.setItem(this.heightKey, height);
    });
    this.observer.observe(this.canvas);
  }

  async load() {
    this.frames = (await api.get(api.p(`/folders/${this.folderId}/frames`))).frames ?? [];
  }

  /* Framed like the Dashboard's sections, because it is one: a title tab cut
   * from the same card, its controls beside it, and the board itself joined to
   * the tab the way a table is. It used to be a loose toolbar above a loose
   * canvas, which made the Repository look like a different program. */
  render() {
    this.host.innerHTML = "";
    const section = el("section", "section");
    const head = el("div", "section-head");
    head.append(el("div", "section-title", "Repository"));
    head.append(this.toolbar());
    head.append(el("span", "count", String(this.frames.length)));
    section.append(head);

    this.canvas = el("div", "canvas");
    // In the DOM before the observer starts, so it never measures an unlaid-out
    // element and stores a zero.
    section.append(this.canvas);
    this.host.append(section);
    for (const frame of this.frames) this.canvas.append(this.frame(frame));
    this.fitCanvas();
    this.watchSize();

    if (!this.frames.length) {
      this.canvas.append(el("p", "canvas-empty",
        "Nothing here yet. Add a text frame, a documents frame, or a picture - "
        + "then drag them where you want them."));
    }
    this.acceptDrops();
  }

  toolbar() {
    const bar = el("div", "repo-bar");
    for (const [kind, spec] of Object.entries(KINDS)) {
      const button = el("button", "ghost", `+ ${spec.label}`);
      button.onclick = () => this.add(kind);
      bar.append(button);
    }
    bar.append(el("span", "spacer"));
    const tidy = el("button", "ghost", "Tidy");
    tidy.title = "Lay the frames out in a grid, left to right";
    tidy.onclick = () => this.tidy();
    bar.append(tidy);
    return bar;
  }

  /* --- creating ----------------------------------------------------------- */

  async add(kind, at) {
    const spec = KINDS[kind];
    const spot = at ?? this.freeSpot(spec.w, spec.h);
    try {
      const { frame } = await api.post(api.p(`/folders/${this.folderId}/frames`),
        { kind, title: "", ...spot, w: spec.w, h: spec.h });
      this.frames.push(frame);
      this.render();
      const node = this.node(frame.id);
      node?.querySelector(".frame-title")?.focus();
      if (kind === "image") this.pick(frame, "image/*");
      if (kind === "files") this.pick(frame);
      return frame;
    } catch (error) { toast(error.message); return null; }
  }

  /* Somewhere the new frame does not land on top of an existing one. */
  freeSpot(w, h) {
    const gap = 16;
    const width = this.canvas?.clientWidth || 1100;
    for (let y = gap; y < 6000; y += 40) {
      for (let x = gap; x + w < Math.max(width, w + 2 * gap); x += 40) {
        const clash = this.frames.some((f) =>
          x < f.x + f.w + gap && x + w + gap > f.x &&
          y < f.y + f.h + gap && y + h + gap > f.y);
        if (!clash) return { x, y };
      }
    }
    return { x: gap, y: gap };
  }

  async tidy() {
    const gap = 16;
    const width = this.canvas.clientWidth || 1100;
    let x = gap, y = gap, rowHeight = 0;
    for (const frame of [...this.frames].sort((a, b) => a.y - b.y || a.x - b.x)) {
      if (x + frame.w + gap > width && x > gap) { x = gap; y += rowHeight + gap; rowHeight = 0; }
      Object.assign(frame, { x, y });
      x += frame.w + gap;
      rowHeight = Math.max(rowHeight, frame.h);
      await this.save(frame, { x: frame.x, y: frame.y });
    }
    this.render();
  }

  /* --- one frame ---------------------------------------------------------- */

  node(id) { return this.canvas.querySelector(`[data-frame="${id}"]`); }

  frame(frame) {
    // Not `frame-${kind}`: those names belong to the sheet inside, and
    // `.frame-image` in particular is position: relative -- on the frame itself
    // it dropped pictures into the page flow, each one stacked under the last.
    const node = el("div", `frame kind-${frame.kind}`);
    node.dataset.frame = frame.id;
    this.place(node, frame);
    node.style.zIndex = String(Math.round(frame.z) + 1);
    node.addEventListener("pointerdown", () => this.raise(frame), true);

    // The whole top strip drags, not just the tab: a thin handle is hard to
    // find and harder to hit.
    const head = el("div", "frame-head");
    const tab = el("div", "frame-tab");
    tab.append(el("span", "frame-grab"));

    const title = el("input", "frame-title");
    title.value = frame.title;
    title.placeholder = KINDS[frame.kind].label;
    title.oninput = () => this.queue(frame, { title: title.value });
    title.onblur = () => this.flush(frame);
    tab.append(title);
    head.append(tab);

    if (frame.kind === "text") {
      const rich = frame.view === "rich";
      const toggle = el("button", `frame-view${rich ? " on" : ""}`,
                        rich ? "Rich" : "Plain");
      toggle.title = rich
        ? "A formatting editor. Click to go back to plain text."
        : "A plain text box. Click for bold, headings, lists and links.";
      toggle.onclick = () => this.setView(frame, rich ? "plain" : "rich");
      head.append(toggle);
    }

    const close = el("button", "frame-close", "×");
    close.title = "Delete this frame";
    close.onclick = () => this.remove(frame);
    head.append(close);

    this.dragBy(head, node, frame);
    node.append(head);

    const body = el("div", "frame-body");
    body.append(this.contents(frame));
    node.append(body);

    const grip = el("div", "frame-grip");
    grip.title = "Resize";
    this.resizeBy(grip, node, frame);
    node.append(grip);
    return node;
  }

  contents(frame) {
    if (frame.kind === "text") return this.textBody(frame);
    if (frame.kind === "image") return this.imageBody(frame);
    return this.filesBody(frame);
  }

  /* Two ways to write the same note.
   *
   * Plain is a text box holding text. Rich is a real editor -- a toolbar and a
   * page you format as you type. The frame stores whichever it is in, and
   * switching converts the note rather than starting a new one. */
  textBody(frame) {
    return frame.view === "rich" ? this.richBody(frame) : this.plainBody(frame);
  }

  richBody(frame) {
    const editor = richEditor(frame.body, (html) => this.queue(frame, { body: html }));
    editor.page.addEventListener("blur", () => {
      frame.body = editor.read();
      this.flush(frame);
    });
    return editor.node;
  }

  plainBody(frame) {
    const area = el("textarea", "frame-text");
    area.value = frame.body;
    area.placeholder = "Notes, minutes, anything.\n\n"
      + "Start a line with - or 1. for a list; Enter carries it on.\n"
      + "- [ ] makes a checkbox. Tab and Shift+Tab indent.\n\n"
      + "For bold, headings and clickable links, switch this note to Rich.";
    area.spellcheck = true;
    area.oninput = () => this.queue(frame, { body: area.value });
    area.onblur = () => this.flush(frame);
    area.addEventListener("keydown", (event) => this.typing(event, area, frame));
    area.addEventListener("paste", (event) => this.pasting(event, area, frame));
    return area;
  }

  /* List behaviour, applied only where it belongs: Tab still leaves the box
   * from ordinary prose, so the keyboard is never trapped in a note. */
  typing(event, area, frame) {
    const { selectionStart: from, selectionEnd: to, value } = area;
    const apply = (text, start, end) => {
      event.preventDefault();
      area.value = text;
      area.selectionStart = start;
      area.selectionEnd = end ?? start;
      this.queue(frame, { body: area.value });
    };

    if (event.key === "Enter" && !event.shiftKey && !event.altKey && from === to) {
      const next = listContinuation(value, from);
      if (next) apply(next.text, next.cursor);
      return;
    }
    if (event.key === "Tab" && (onListLine(value, from) || from !== to)) {
      const moved = shiftIndent(value, from, to, event.shiftKey);
      apply(moved.text, moved.from, moved.to);
    }
  }

  imageBody(frame) {
    const wrap = el("div", "frame-image");
    const picture = frame.files.find((f) => f.is_image);
    if (!picture) {
      const add = el("button", "frame-drop", "Choose a picture");
      add.onclick = () => this.pick(frame, "image/*");
      wrap.append(add);
      return wrap;
    }
    const img = el("img");
    img.src = picture.url;
    img.alt = picture.name;
    img.title = picture.name;
    wrap.append(img);
    const swap = el("button", "frame-swap", "Replace");
    swap.onclick = async () => {
      await this.dropFile(picture);
      this.pick(frame, "image/*");
    };
    wrap.append(swap);
    return wrap;
  }

  filesBody(frame) {
    const wrap = el("div", "frame-files");
    const list = el("ul");
    for (const file of frame.files) {
      const item = el("li");
      const line = el("div", "doc-line");
      const link = el("a", "doc", file.name);
      link.href = file.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.title = `${size(file.size)}\n${file.path}`;
      line.append(link);
      line.append(el("span", "doc-size", size(file.size)));
      const drop = el("button", "doc-drop", "×");
      drop.title = "Remove this document from the project";
      drop.onclick = () => this.dropFile(file, frame);
      line.append(drop);
      item.append(line, this.description(file));
      list.append(item);
    }
    if (!frame.files.length) list.append(el("li", "nothing", "No documents yet."));
    wrap.append(list);
    const add = el("button", "frame-drop", "+ Add documents");
    add.onclick = () => this.pick(frame);
    wrap.append(add);
    return wrap;
  }

  /* What a document is, in your words. Optional, so an empty one stays out of
   * the way until the row is hovered -- a filename is often enough, and a list
   * of blank prompts would be worse than no field at all. */
  description(file) {
    const shown = el("div", file.description ? "doc-note" : "doc-note empty");
    shown.textContent = file.description || "describe it";
    shown.title = "Click to describe this document";
    shown.onclick = () => this.editDescription(file, shown);
    return shown;
  }

  editDescription(file, shown) {
    const box = Object.assign(document.createElement("input"),
                              { type: "text", value: file.description || "",
                                className: "doc-note-edit",
                                placeholder: "what this is, in your words" });
    shown.replaceWith(box);
    box.focus();
    box.select();

    let done = false;
    const finish = (keep) => {
      if (done) return;
      done = true;
      if (keep) this.describeFile(file, box.value.trim());
      const fresh = this.description(file);
      box.replaceWith(fresh);
      if (keep) this.settle(`file:${file.id}`);   // written before it leaves the screen
    };
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      else if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    box.addEventListener("blur", () => finish(true));
  }

  /* --- files -------------------------------------------------------------- */

  pick(frame, accept) {
    const input = el("input");
    input.type = "file";
    input.multiple = accept !== "image/*";
    if (accept) input.accept = accept;
    input.onchange = () => this.upload(frame, [...input.files]);
    input.click();
  }

  async upload(frame, list) {
    if (!list.length) return;
    for (const file of list) {
      try {
        const { file: added } = await api.upload(
          api.p(`/frames/${frame.id}/files`), file);
        frame.files.push(added);
      } catch (error) { toast(`${file.name}: ${error.message}`); }
    }
    this.render();
  }

  async dropFile(file, frame) {
    if (frame && !confirm(`Remove "${file.name}" from this project?\n\n`
        + "This deletes Manila's copy. Whatever you imported it from is untouched.")) {
      return;
    }
    try {
      await api.post(api.p(`/files/${file.id}/delete`));
      for (const f of this.frames) f.files = f.files.filter((x) => x.id !== file.id);
      if (frame) this.render();
    } catch (error) { toast(error.message); }
  }

  /* Dropping documents on empty canvas makes a frame to hold them: pictures
   * get their own, everything else is consolidated into one. */
  acceptDrops() {
    const stop = (event) => { event.preventDefault(); event.stopPropagation(); };
    this.canvas.addEventListener("dragover", (event) => {
      stop(event);
      this.canvas.classList.add("dropping");
    });
    this.canvas.addEventListener("dragleave", () => this.canvas.classList.remove("dropping"));
    this.canvas.addEventListener("drop", async (event) => {
      stop(event);
      this.canvas.classList.remove("dropping");
      const dropped = [...(event.dataTransfer?.files ?? [])];
      if (!dropped.length) return;
      const box = this.canvas.getBoundingClientRect();
      const at = { x: Math.max(0, event.clientX - box.left - 40),
                   y: Math.max(0, event.clientY - box.top - 20) };

      const pictures = dropped.filter((f) => f.type.startsWith("image/"));
      const documents = dropped.filter((f) => !f.type.startsWith("image/"));
      for (const picture of pictures) {
        const frame = await this.add("image", at);
        if (frame) await this.upload(frame, [picture]);
      }
      if (documents.length) {
        const frame = await this.add("files", at);
        if (frame) await this.upload(frame, documents);
      }
    });
  }

  /* --- moving and sizing --------------------------------------------------- */

  place(node, frame) {
    Object.assign(node.style, {
      left: `${frame.x}px`, top: `${frame.y}px`,
      width: `${frame.w}px`, height: `${frame.h}px`,
    });
  }

  /* One gesture handler for both moving and resizing: they differ only in
   * which numbers the pointer delta is added to. */
  gesture(handle, node, frame, apply) {
    handle.addEventListener("pointerdown", (event) => {
      if (event.target.closest("input, textarea, button, a")) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const start = { x: event.clientX, y: event.clientY,
                      fx: frame.x, fy: frame.y, fw: frame.w, fh: frame.h };
      node.classList.add("moving");

      const move = (e) => {
        apply(frame, start, e.clientX - start.x, e.clientY - start.y);
        this.place(node, frame);
      };
      const done = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", done);
        handle.removeEventListener("pointercancel", done);
        node.classList.remove("moving");
        this.fitCanvas();
        this.save(frame, { x: frame.x, y: frame.y, w: frame.w, h: frame.h });
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", done);
      handle.addEventListener("pointercancel", done);
    });
  }

  dragBy(handle, node, frame) {
    handle.classList.add("draggable");
    this.gesture(handle, node, frame, (f, start, dx, dy) => {
      f.x = Math.max(0, start.fx + dx);
      f.y = Math.max(0, start.fy + dy);
    });
  }

  resizeBy(handle, node, frame) {
    this.gesture(handle, node, frame, (f, start, dx, dy) => {
      f.w = Math.max(MIN_W, start.fw + dx);
      f.h = Math.max(MIN_H, start.fh + dy);
    });
  }

  /* The canvas grows to hold whatever is on it, so dragging a frame down does
   * not put it somewhere you cannot scroll to. */
  /* Size the canvas to hold everything on it.
   *
   * Opening the tab should show the whole board, so the height that fits the
   * lowest frame is the floor -- a smaller height dragged earlier does not
   * hide content the next time you come back. A larger one is kept, because
   * that is room deliberately made for what comes next. */
  fitCanvas() {
    if (!this.canvas) return;
    const right = this.frames.reduce((m, f) => Math.max(m, f.x + f.w), 0);
    this.appliedHeight = canvasHeight(this.frames, this.keptHeight());
    this.canvas.style.height = `${this.appliedHeight}px`;
    this.canvas.style.minWidth = `${right + 40}px`;
  }

  async raise(frame) {
    const top = this.frames.reduce((m, f) => Math.max(m, f.z), 0);
    if (frame.z >= top) return;
    frame.z = top + 1;
    this.node(frame.id).style.zIndex = String(Math.round(frame.z) + 1);
    await this.save(frame, { z: frame.z });
  }

  /* --- saving -------------------------------------------------------------- */

  /* Typing is saved a beat after you stop. Everything still waiting is counted
   * as "unsaved" -- typed, but not yet written -- so the header can say so and
   * leaving the page can force it out first. Keyed by a string so frames and
   * the notes on individual documents share one mechanism. */
  defer(key, run) {
    const waiting = this.timers.get(key);
    if (waiting) clearTimeout(waiting.timer);
    this.timers.set(key, { run, timer: setTimeout(() => this.settle(key), SAVE_DELAY) });
    markUnsaved(this.timers.size);
  }

  settle(key) {
    const waiting = this.timers.get(key);
    if (!waiting) return;
    clearTimeout(waiting.timer);
    this.timers.delete(key);
    markUnsaved(this.timers.size);
    waiting.run();
  }

  /** Write everything still waiting, now. Used when the page is going away. */
  flushAll() {
    for (const key of [...this.timers.keys()]) this.settle(key);
  }

  queue(frame, patch) {
    Object.assign(frame, patch);
    // Sent at settle time, so what goes is what the box says then, not what it
    // said when the first keystroke landed.
    this.defer(`frame:${frame.id}`,
               () => this.save(frame, { title: frame.title, body: frame.body }));
  }

  flush(frame) {
    this.settle(`frame:${frame.id}`);
  }

  async save(frame, patch) {
    const node = this.node?.(frame.id);
    node?.classList.remove("frame-failed");
    try {
      await api.patch(api.p(`/frames/${frame.id}`), patch);
    } catch (error) {
      node?.classList.add("frame-failed");
      toast(error.message);
    }
  }

  /* Switching rewrites the note into the other form, on the server, so the
   * conversion happens once and the browser shows what was actually stored. */
  async setView(frame, view) {
    this.flush(frame);
    try {
      const { frame: updated } = await api.patch(api.p(`/frames/${frame.id}`), { view });
      Object.assign(frame, updated);
      this.render();
    } catch (error) { toast(error.message); }
  }

  async describeFile(file, text) {
    file.description = text;
    this.defer(`file:${file.id}`, async () => {
      try {
        await api.patch(api.p(`/files/${file.id}`), { description: file.description });
      } catch (error) { toast(error.message); }
    });
  }

  async remove(frame) {
    const held = frame.files.length;
    const warning = held
      ? `\n\nThe ${held} document(s) it holds are Manila's own copies and will be `
        + "deleted. Whatever you imported them from is untouched."
      : "";
    if (!confirm(`Delete "${frame.title || KINDS[frame.kind].label}"?${warning}`)) return;
    try {
      await api.post(api.p(`/frames/${frame.id}/delete`));
      this.frames = this.frames.filter((f) => f.id !== frame.id);
      this.render();
    } catch (error) { toast(error.message); }
  }
}

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
