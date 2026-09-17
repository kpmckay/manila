/* The rich text editor for a note.
 *
 * Built on contenteditable rather than a library, because Manila ships with no
 * build step and no CDN -- it has to keep working on a locked-down laptop from
 * a folder of files. That means document.execCommand, which is deprecated on
 * paper and implemented everywhere in practice; there is no replacement for
 * it that does not weigh more than the rest of the app.
 *
 * What gets typed is HTML, and it is cleaned on the way out of the browser and
 * again on the server, which is the copy that counts.
 */

import { el } from "./table.js";

// Structure and emphasis only, matching what the server will keep.
const ALLOWED = new Set([
  "P", "BR", "HR", "STRONG", "EM", "U", "S", "CODE", "PRE",
  "UL", "OL", "LI", "H1", "H2", "H3", "H4", "BLOCKQUOTE", "A",
  "TABLE", "THEAD", "TBODY", "TFOOT", "TR", "TH", "TD",
]);
const RENAME = { B: "STRONG", I: "EM", STRIKE: "S", DEL: "S", INS: "U", DIV: "P" };
const DROP_WHOLE = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "SVG",
                            "MATH", "FORM", "INPUT", "BUTTON", "SELECT", "TEXTAREA",
                            "NOSCRIPT", "LINK", "META"]);
const VOID = new Set(["BR", "HR"]);
const SAFE_HREF = /^(https?:\/\/|mailto:|tel:)/i;

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const escapeHtml = (t) => String(t).replace(/[&<>"]/g, (c) => ESCAPES[c]);

/** Clean a DOM-shaped tree into the allowed subset. Testable without a browser. */
export function cleanNode(root) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType === 3) { out.push(escapeHtml(child.textContent || "")); continue; }
      if (child.nodeType !== 1) continue;
      const raw = (child.tagName || "").toUpperCase();
      if (DROP_WHOLE.has(raw)) continue;
      const tag = RENAME[raw] || raw;
      if (!ALLOWED.has(tag)) { walk(child); continue; }   // unwrap, keep the words
      if (VOID.has(tag)) { out.push(`<${tag.toLowerCase()}>`); continue; }
      if (tag === "A") {
        const href = (child.getAttribute?.("href") || "").trim();
        out.push(SAFE_HREF.test(href)
          ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">`
          : "<a>");
      } else {
        out.push(`<${tag.toLowerCase()}>`);
      }
      walk(child);
      out.push(`</${tag.toLowerCase()}>`);
    }
  };
  walk(root);
  return out.join("");
}

const TOOLS = [
  { label: "B", title: "Bold (Ctrl+B)", command: "bold", style: "font-weight:700" },
  { label: "I", title: "Italic (Ctrl+I)", command: "italic", style: "font-style:italic" },
  { label: "U", title: "Underline (Ctrl+U)", command: "underline",
    style: "text-decoration:underline" },
  { label: "S", title: "Strikethrough", command: "strikeThrough",
    style: "text-decoration:line-through" },
  { gap: true },
  { label: "H", title: "Heading", block: "H2" },
  { label: "¶", title: "Normal text", block: "P" },
  { label: "“", title: "Quote", block: "BLOCKQUOTE" },
  { gap: true },
  { label: "•", title: "Bulleted list", command: "insertUnorderedList" },
  { label: "1.", title: "Numbered list", command: "insertOrderedList" },
  { label: "→", title: "Indent", command: "indent" },
  { label: "←", title: "Outdent", command: "outdent" },
  { gap: true },
  { label: "⚭", title: "Add a link", link: true },
  { label: "✗", title: "Clear formatting", command: "removeFormat" },
];

/* A note being edited. `onChange` runs whenever the content changes. */
export function richEditor(html, onChange) {
  const wrap = el("div", "rich");
  const bar = el("div", "rich-bar");
  const page = el("div", "rich-page");
  page.contentEditable = "true";
  page.spellcheck = true;
  page.innerHTML = html || "<p><br></p>";

  const run = (fn) => {
    page.focus();
    fn();
    onChange?.(cleanNode(page));
  };

  for (const tool of TOOLS) {
    if (tool.gap) { bar.append(el("span", "rich-gap")); continue; }
    const button = el("button", "rich-tool", tool.label);
    button.type = "button";
    button.title = tool.title;
    button.tabIndex = -1;
    if (tool.style) button.setAttribute("style", tool.style);
    // pointerdown, not click: the selection must still exist when we act on it.
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      if (tool.link) return run(() => addLink(page));
      if (tool.block) return run(() => exec("formatBlock", `<${tool.block}>`));
      run(() => exec(tool.command));
    });
    bar.append(button);
  }

  page.addEventListener("input", () => onChange?.(cleanNode(page)));
  page.addEventListener("paste", (event) => {
    const clipboard = event.clipboardData;
    if (!clipboard) return;
    const markup = clipboard.getData("text/html");
    event.preventDefault();
    if (markup) {
      const parsed = new DOMParser().parseFromString(markup, "text/html");
      exec("insertHTML", cleanNode(parsed.body));
    } else {
      exec("insertText", clipboard.getData("text/plain"));
    }
    onChange?.(cleanNode(page));
  });

  wrap.append(bar, page);
  return { node: wrap, page, focus: () => page.focus(), read: () => cleanNode(page) };
}

function exec(command, value) {
  try {
    document.execCommand(command, false, value);
  } catch { /* an unsupported command is a no-op, not a broken note */ }
}

function addLink(page) {
  const selection = page.ownerDocument.getSelection?.();
  const chosen = selection ? String(selection) : "";
  const href = prompt("Link to:", "https://")?.trim();
  if (!href || !SAFE_HREF.test(href)) return;
  if (chosen) exec("createLink", href);
  else exec("insertHTML",
            `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">`
            + `${escapeHtml(href)}</a>`);
}
