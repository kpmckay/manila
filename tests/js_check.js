/* Parse-and-load check for the browser modules, plus real tests for
 * format.js -- the only place Manila computes anything.
 *
 * Runs under any ES-module JS runtime. On Fedora: gjs -m tests/js_check.js
 * (Also works with node or deno if you have them.)
 */

// Minimal DOM stubs so the modules can be imported outside a browser.
const noop = () => {};
const fakeNode = () => new Proxy({}, {
  get: (_t, key) => (key === "style" || key === "dataset" || key === "classList")
    ? fakeNode() : (key === "textContent" || key === "className") ? "" : noop,
  set: () => true,
});
globalThis.document = {
  createElement: fakeNode, querySelector: () => null, querySelectorAll: () => [],
  getElementById: fakeNode, addEventListener: noop, body: fakeNode(),
  documentElement: fakeNode(), title: "",
};
globalThis.localStorage = { getItem: () => null, setItem: noop };
globalThis.location = { hash: "", replace: noop };
globalThis.addEventListener = noop;
// gjs already defines a read-only `window`; node and deno do not.
if (typeof window === "undefined") globalThis.window = globalThis;
globalThis.prompt = () => null;
globalThis.confirm = () => false;
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
globalThis.setTimeout = globalThis.setTimeout || noop;
globalThis.clearTimeout = globalThis.clearTimeout || noop;

let failures = 0;
const check = (label, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) { failures++; console.log(`FAIL  ${label}\n        got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`); }
  else console.log(`ok    ${label}`);
};

const throws = (label, fn) => {
  try { fn(); failures++; console.log(`FAIL  ${label}\n        expected a throw`); }
  catch { console.log(`ok    ${label}`); }
};

const iso = (offsetDays) => {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

const modules = ["api.js", "format.js", "icons.js", "status.js", "editor.js",
                 "table.js", "history.js", "repository.js", "app.js"];
for (const name of modules) {
  try {
    await import(`../manila/static/${name}`);
    console.log(`ok    ${name} parses and loads`);
  } catch (error) {
    failures++;
    console.log(`FAIL  ${name}: ${error}`);
  }
}

const fmt = await import("../manila/static/format.js");

check("absolute date matches the sketch", fmt.absolute("2026-09-15"), "15-Sep-2026");
check("absolute of empty", fmt.absolute(""), "");
check("relative: today", fmt.relative(iso(0)), "today");
check("relative: tomorrow", fmt.relative(iso(1)), "tomorrow");
check("relative: yesterday", fmt.relative(iso(-1)), "yesterday");
check("relative: the sketch's countdown", fmt.relative(iso(15)), "in 15 days");
check("relative: the sketch's Last Touched", fmt.relative(iso(-3)), "3 days ago");
const twoPart = { type: "datenote", storage: ["a", "b"] };
const threePart = { type: "datenote", storage: ["a", "b", "c"] };

check("history: datenote splits on the pipe",
      fmt.historyValue(twoPart, "2026-08-20|in standup"), "20-Aug-2026 · in standup");
check("history: datenote with no note",
      fmt.historyValue(twoPart, "2026-08-20|"), "20-Aug-2026");
check("history: a date stays absolute, never relative",
      fmt.historyValue("date", "2026-09-15"), "15-Sep-2026");
check("history: empty reads as empty", fmt.historyValue("text", ""), "(empty)");
check("history: plain text passes through", fmt.historyValue("text", "Alice"), "Alice");

// Every folder and row URL is built by api.p(). If it silently produced a
// path with no project in it, one project's edits would land in another.
const { api } = await import("../manila/static/api.js");
throws("api.p refuses to build a URL with no project open", () => api.p("/folders"));
api.project = "acme-corp";
check("api.p scopes a folder URL", api.p("/folders"), "/api/p/acme-corp/folders");
check("api.p scopes a row URL", api.p("/rows/action_item/7"),
      "/api/p/acme-corp/rows/action_item/7");
api.project = null;

// A passed deadline is the one thing in the table that turns red.
check("isPast: yesterday", fmt.isPast(iso(-1)), true);
check("isPast: today is not late", fmt.isPast(iso(0)), false);
check("isPast: tomorrow", fmt.isPast(iso(1)), false);
check("isPast: no date", fmt.isPast(""), false);

// Two-part and three-part composites share one storage format.
check("history: date|note still reads",
      fmt.historyValue(twoPart, "2026-08-20|in standup"), "20-Aug-2026 · in standup");
check("history: date|via|note reads all three",
      fmt.historyValue(threePart, "2026-08-20|Call|left a message"),
      "20-Aug-2026 · Call · left a message");
check("history: a note containing a pipe stays whole",
      fmt.historyValue(threePart, "2026-08-20|Email|re: a|b"),
      "20-Aug-2026 · Email · re: a|b");
check("history: a two-part note containing a pipe stays whole",
      fmt.historyValue(twoPart, "2026-08-20|re: a|b"), "20-Aug-2026 · re: a|b");
check("history: three parts with an empty middle",
      fmt.historyValue(threePart, "2026-08-20||just a note"),
      "20-Aug-2026 · just a note");

// A borrowed date records where it came from, so the log still reads correctly
// after the deliverable it named is gone.
check("history: a borrowed date names its source",
      fmt.historyValue(twoPart, `2026-09-15${fmt.LINK_MARK}Phase One Signoff|ASAP`),
      `15-Sep-2026 · ASAP${fmt.LINK_MARK}Phase One Signoff`);
check("history: a borrowed date with no note",
      fmt.historyValue(twoPart, `2026-09-15${fmt.LINK_MARK}Phase One|`),
      `15-Sep-2026${fmt.LINK_MARK}Phase One`);
check("history: an older unlinked entry still reads",
      fmt.historyValue(twoPart, "2026-09-15|ASAP"), "15-Sep-2026 · ASAP");

// The save indicator is the only evidence an edit landed, so its states have
// to be right: a failure must never read as a success.
const status = await import("../manila/static/status.js");

check("status: nothing edited yet",
      status.describe({ pending: 0, unsaved: 0, failed: 0, savedAt: null }).tone, "idle");
check("status: a request in flight",
      status.describe({ pending: 1, unsaved: 0, failed: 0, savedAt: null }).tone, "busy");
check("status: typed but not yet sent",
      status.describe({ pending: 0, unsaved: 2, failed: 0, savedAt: null }).tone, "busy");
check("status: a failure outranks a past success",
      status.describe({ pending: 0, unsaved: 0, failed: 1, savedAt: new Date() }).tone, "bad");
check("status: a failure still outranks work in flight",
      status.describe({ pending: 3, unsaved: 0, failed: 1, savedAt: null }).tone, "bad");
check("status: written",
      status.describe({ pending: 0, unsaved: 0, failed: 0, savedAt: new Date() }).tone, "ok");

// tracked() must report both outcomes and never leave the counter stuck.
let seen = [];
status.onStatus((s) => seen.push(`${s.pending}/${s.failed}`));
await status.tracked(async () => "written");
check("tracked: a success ends at rest", status.atRest(), true);
try { await status.tracked(async () => { throw new Error("server said no"); }); }
catch { /* expected: tracked rethrows so the caller still handles it */ }
check("tracked: a failure ends at rest too", status.atRest(), true);
check("tracked: the failure is recorded",
      status.describe({ pending: 0, unsaved: 0, failed: 1, savedAt: new Date(),
                        lastError: "server said no" }).title, "server said no");
check("tracked: pending rose and fell", seen.includes("1/0"), true);

// The repository canvas opens showing everything on it. Two earlier bugs lived
// here: a stored zero height, and a kept height that hid content.
const repo = await import("../manila/static/repository.js");
const frames = [{ x: 0, y: 0, w: 300, h: 200 }, { x: 400, y: 620, w: 300, h: 240 }];

check("canvas: fits the lowest frame", repo.canvasHeight(frames), 900);
check("canvas: never below the floor", repo.canvasHeight([]), 360);
check("canvas: one small frame still gets the floor",
      repo.canvasHeight([{ x: 0, y: 0, w: 100, h: 50 }]), 360);
check("canvas: a larger kept height is room made on purpose",
      repo.canvasHeight(frames, 1200), 1200);
check("canvas: a smaller kept height never hides a frame",
      repo.canvasHeight(frames, 400), 900);

check("canvas: a stored zero is ignored", repo.usableHeight("0"), 0);
check("canvas: an unlaid-out measurement is ignored", repo.usableHeight(0), 0);
check("canvas: nothing stored", repo.usableHeight(null), 0);
check("canvas: junk stored", repo.usableHeight("tall"), 0);
check("canvas: a believable height is kept", repo.usableHeight("880"), 880);

// Lists in a repository note. The body stays plain text; the box just keeps
// typing the markers you would have typed anyway.
const cont = (text, at) => {
  const next = repo.listContinuation(text, at ?? text.length);
  return next && next.text;
};

check("list: a dash carries on", cont("- milk"), "- milk\n- ");
check("list: a star carries on", cont("* milk"), "* milk\n* ");
check("list: numbers increment", cont("1. first"), "1. first\n2. ");
check("list: a paren style is kept", cont("3) third"), "3) third\n4) ");
check("list: indentation is kept", cont("    - nested"), "    - nested\n    - ");
check("list: checkboxes come back empty",
      cont("- [x] done thing"), "- [x] done thing\n- [ ] ");
check("list: an empty item ends the list", cont("- one\n- "), "- one\n");
check("list: an empty indented item drops back to the indent",
      cont("  - "), "  ");
check("list: ordinary prose is left alone", cont("just a sentence"), null);
check("list: an empty document is left alone", cont(""), null);
check("list: only the line the cursor is on matters",
      cont("- one\nplain text"), null);
check("list: carrying on from mid-document",
      repo.listContinuation("- one\n- two\n- three", 11).text,
      "- one\n- two\n- \n- three");
// Enter part-way through an item splits it, and the remainder is an item too.
check("list: splitting an item marks the remainder",
      repo.listContinuation("- one two", 5).text, "- one\n-  two");

check("indent: one line in", repo.shiftIndent("- a", 1, 1, false).text, "  - a");
check("indent: and back out", repo.shiftIndent("  - a", 3, 3, true).text, "- a");
check("indent: outdenting an unindented line is harmless",
      repo.shiftIndent("- a", 1, 1, true).text, "- a");
check("indent: a whole selection moves together",
      repo.shiftIndent("- a\n- b\n- c", 0, 11, false).text, "  - a\n  - b\n  - c");
check("indent: a tab counts as indentation too",
      repo.shiftIndent("\t- a", 2, 2, true).text, "- a");

check("list: the cursor is on a list line", repo.onListLine("- milk", 6), true);
check("list: prose is not a list line", repo.onListLine("milk", 4), false);

// Pasting a list in from a note. The plain-text tidy-up is pure string work;
// the HTML walk needs only a DOM-shaped tree, so both are checkable here.
const B = "\u2022";        // the bullet Word and OneNote paste
const NBSP = "\u00a0";

check("paste: a bullet glyph becomes a marker",
      repo.normalizeList(`${B} milk`), "- milk");
check("paste: an indented glyph keeps its depth",
      repo.normalizeList(`  ${B} nested`), "  - nested");
check("paste: a tab indent becomes spaces",
      repo.normalizeList(`\t${B} nested`), "  - nested");
check("paste: hollow and square bullets too",
      repo.normalizeList("\u25e6 one\n\u25aa two"), "- one\n- two");
// Word separates its bullet from the text with a non-breaking space, so the
// marker is only recognisable once those become ordinary spaces.
check("paste: non-breaking spaces become ordinary ones",
      repo.normalizeList(`${B}${NBSP}spaced`), "- spaced");
check("paste: non-breaking spaces inside the text go too",
      repo.normalizeList(`a${NBSP}b`), "a b");
check("paste: numbering that came through is left alone",
      repo.normalizeList("1. first\n2. second"), "1. first\n2. second");
check("paste: a dash is already a marker",
      repo.normalizeList("- already fine"), "- already fine");
check("paste: prose starting with o is not a bullet",
      repo.normalizeList("o'clock somewhere"), "o'clock somewhere");
check("paste: windows line endings",
      repo.normalizeList("one\r\ntwo"), "one\ntwo");
check("paste: trailing spaces go",
      repo.normalizeList("padded   \nlines  "), "padded\nlines");
check("paste: a wall of blank lines collapses to one gap",
      repo.normalizeList("a\n\n\n\n\nb"), "a\n\nb");
check("paste: nothing pasted, nothing returned", repo.normalizeList(""), "");

// A DOM-shaped tree, built by hand.
const text = (s) => ({ nodeType: 3, textContent: s });
const tag = (name, children, attrs) => ({
  nodeType: 1, tagName: name, childNodes: children || [],
  getAttribute: (k) => (attrs || {})[k] ?? null,
});

check("paste: a flat bullet list",
      repo.nodeToText(tag("BODY", [
        tag("UL", [tag("LI", [text("milk")]), tag("LI", [text("bread")])]),
      ])), "- milk\n- bread");

check("paste: a numbered list is numbered",
      repo.nodeToText(tag("BODY", [
        tag("OL", [tag("LI", [text("first")]), tag("LI", [text("second")])]),
      ])), "1. first\n2. second");

check("paste: a numbered list that starts partway",
      repo.nodeToText(tag("BODY", [
        tag("OL", [tag("LI", [text("third")]), tag("LI", [text("fourth")])], { start: "3" }),
      ])), "3. third\n4. fourth");

check("paste: nesting is kept as indentation",
      repo.nodeToText(tag("BODY", [
        tag("UL", [
          tag("LI", [text("power"), tag("UL", [
            tag("LI", [text("supplier quote")]),
            tag("LI", [text("packaging")]),
          ])]),
          tag("LI", [text("shipping")]),
        ]),
      ])), "- power\n  - supplier quote\n  - packaging\n- shipping");

check("paste: a numbered list inside a bullet",
      repo.nodeToText(tag("BODY", [
        tag("UL", [tag("LI", [text("steps"), tag("OL", [
          tag("LI", [text("one")]), tag("LI", [text("two")]),
        ])])]),
      ])), "- steps\n  1. one\n  2. two");

check("paste: formatting inside an item is just text",
      repo.nodeToText(tag("BODY", [
        tag("UL", [tag("LI", [text("see "), tag("STRONG", [text("the spec")]),
                              text(" first")])]),
      ])), "- see the spec first");

check("paste: paragraphs become lines",
      repo.nodeToText(tag("BODY", [
        tag("P", [text("first para")]), tag("P", [text("second para")]),
      ])), "first para\nsecond para");

check("paste: a line break breaks the line",
      repo.nodeToText(tag("BODY", [
        tag("P", [text("one"), tag("BR"), text("two")]),
      ])), "one\ntwo");

check("paste: prose above a list keeps its place",
      repo.nodeToText(tag("BODY", [
        tag("P", [text("Open items:")]),
        tag("UL", [tag("LI", [text("power")])]),
      ])), "Open items:\n- power");

check("paste: an empty tree gives nothing", repo.nodeToText(tag("BODY", [])), "");

// Outlook and Word do not write real lists. They write paragraphs with the
// bullet as literal Symbol-font text and the nesting hidden in a style rule.
const WORD_BULLET = "\uf0b7";
const wordItem = (level, marker, body) => tag("P", [
  tag("SPAN", [text(`${marker}${NBSP}${NBSP}${NBSP}`)]),
  text(body),
], { style: `margin-left:.5in;mso-list:l0 level${level} lfo1` });

check("outlook: a symbol-font bullet becomes a marker",
      repo.normalizeList(repo.nodeToText(tag("BODY", [
        wordItem(1, WORD_BULLET, "chase the supplier quote"),
      ]))), "- chase the supplier quote");

check("outlook: nesting comes from the style rule, not the markup",
      repo.normalizeList(repo.nodeToText(tag("BODY", [
        wordItem(1, WORD_BULLET, "power"),
        wordItem(2, "\uf06f", "supplier quote"),
        wordItem(2, "\uf06f", "packaging"),
        wordItem(1, WORD_BULLET, "shipping"),
      ]))), "- power\n  - supplier quote\n  - packaging\n- shipping");

check("outlook: a numbered list keeps its numbers and its depth",
      repo.normalizeList(repo.nodeToText(tag("BODY", [
        wordItem(1, "1.", "first"),
        wordItem(2, "a.", "sub point"),
        wordItem(1, "2.", "second"),
      ]))), "1. first\n  a. sub point\n2. second");

check("outlook: prose paragraphs beside a list are untouched",
      repo.normalizeList(repo.nodeToText(tag("BODY", [
        tag("P", [text("Open items:")]),
        wordItem(1, WORD_BULLET, "power"),
        tag("P", [text("Thanks,")]),
      ]))), "Open items:\n- power\nThanks,");

check("outlook: the level-1 case needs no indent",
      repo.wordListLevel(tag("P", [], { style: "mso-list:l0 level1 lfo1" })), 1);
check("outlook: a plain paragraph has no level",
      repo.wordListLevel(tag("P", [], { style: "margin-left:.5in" })), 0);
check("outlook: no style attribute at all",
      repo.wordListLevel(tag("P", [])), 0);

// The private-use range is Word's alone, so matching it can never hit prose.
check("paste: a private-use square bullet",
      repo.normalizeList("\uf0a7 square item"), "- square item");
check("paste: spacer runs after a number collapse",
      repo.normalizeList("1.    padded"), "1. padded");
check("paste: spacer runs after a letter collapse too",
      repo.normalizeList("a.    padded"), "a. padded");
check("paste: a single space after a marker is left alone",
      repo.normalizeList("A. Smith reviewed it"), "A. Smith reviewed it");


// A thread shaped like the ones that get pasted in: a long bracketed subject,
// a link, an indented question, a plain answer. Invented, but with every
// feature that matters -- most pasted notes are prose, not lists, and the thing
// that must never happen is prose coming back altered.
const OUTLOOK_SAMPLE = "TRK-4471: [Acme][Order 44821 Rev B0011294]Delivery does not meet the agreed dates for lots 1.1/1.2/2.0.\n\nFor more information: https://example.com/share/folders/8fQ2xLmN4pRb7TvKzAe1?usp=sharing\n\n           Can it still ship under SPEC v3.1?\n\nSPEC v3.1 does not record any information related to the revised dates.\n";

check("outlook: real prose comes back byte for byte",
      repo.normalizeList(OUTLOOK_SAMPLE), OUTLOOK_SAMPLE);
check("outlook: the URL survives whole",
      repo.normalizeList(OUTLOOK_SAMPLE).includes(
        "https://example.com/share/folders/8fQ2xLmN4pRb7TvKzAe1?usp=sharing"), true);
check("outlook: an indented reply keeps its indent",
      repo.normalizeList(OUTLOOK_SAMPLE).split("\n")[4],
      "           Can it still ship under SPEC v3.1?");
check("outlook: version numbers are not mistaken for list markers",
      repo.normalizeList("SPEC v3.1 does not record"), "SPEC v3.1 does not record");

// The same thread as Outlook actually sends it: paragraphs, with the indent
// written as non-breaking spaces. Those must not collapse.
const outlookHtml = tag("BODY", OUTLOOK_SAMPLE.split("\n")
  .filter((l) => l.trim())
  .map((l) => tag("P", [text(l.replace(/^ +/, (pad) => NBSP.repeat(pad.length)))])));

check("outlook: the HTML flavour keeps the same indent",
      repo.normalizeList(repo.nodeToText(outlookHtml)).split("\n")[2],
      "           Can it still ship under SPEC v3.1?");
check("outlook: the HTML flavour keeps the URL whole",
      repo.normalizeList(repo.nodeToText(outlookHtml)).includes(
        "folders/8fQ2xLmN4pRb7TvKzAe1?usp=sharing"), true);

// What the editor keeps when a paste lands in it. The server cleans again on
// the way to disk -- this is the copy that stops the mess reaching the screen.
const ed = await import("../manila/static/editor.js");

check("editor: structure and emphasis are kept",
      ed.cleanNode(tag("BODY", [tag("P", [text("hi "), tag("B", [text("there")])])])),
      "<p>hi <strong>there</strong></p>");
check("editor: a list keeps its nesting",
      ed.cleanNode(tag("BODY", [tag("UL", [
        tag("LI", [text("power"), tag("UL", [tag("LI", [text("quote")])])]),
      ])])), "<ul><li>power<ul><li>quote</li></ul></li></ul>");
check("editor: old spellings are modernised",
      ed.cleanNode(tag("BODY", [tag("I", [text("a")]), tag("STRIKE", [text("b")])])),
      "<em>a</em><s>b</s>");
check("editor: a div becomes a paragraph",
      ed.cleanNode(tag("BODY", [tag("DIV", [text("line")])])), "<p>line</p>");
check("editor: an unknown tag is unwrapped, its words kept",
      ed.cleanNode(tag("BODY", [tag("FONT", [text("still here")])])), "still here");
check("editor: a table survives a paste",
      ed.cleanNode(tag("BODY", [tag("TABLE", [tag("TR", [
        tag("TD", [text("a")]), tag("TD", [text("b")])])])])),
      "<table><tr><td>a</td><td>b</td></tr></table>");

// The safety properties. These must never regress.
check("editor: a script is dropped whole",
      ed.cleanNode(tag("BODY", [tag("SCRIPT", [text("alert(1)")]), tag("P", [text("after")])])),
      "<p>after</p>");
check("editor: a style block is dropped whole",
      ed.cleanNode(tag("BODY", [tag("STYLE", [text("body{}")]), text("x")])), "x");
check("editor: an iframe is dropped whole",
      ed.cleanNode(tag("BODY", [tag("IFRAME", [text("no")])])), "");
check("editor: attributes do not survive",
      ed.cleanNode(tag("BODY", [tag("P", [text("x")], { onclick: "steal()",
                                                        style: "color:red" })])),
      "<p>x</p>");
check("editor: a javascript link keeps its words but gets no href",
      ed.cleanNode(tag("BODY", [tag("A", [text("click")],
                                    { href: "javascript:alert(1)" })])),
      "<a>click</a>");
check("editor: a data link gets no href either",
      ed.cleanNode(tag("BODY", [tag("A", [text("x")], { href: "data:text/html,y" })])),
      "<a>x</a>");
check("editor: a real link keeps its href and opens safely",
      ed.cleanNode(tag("BODY", [tag("A", [text("spec")],
                                    { href: "https://example.com/s?a=1&b=2" })])),
      '<a href="https://example.com/s?a=1&amp;b=2" target="_blank" '
      + 'rel="noopener noreferrer">spec</a>');
check("editor: angle brackets in text are escaped, not obeyed",
      ed.cleanNode(tag("BODY", [text("if a < b && c > d")])),
      "if a &lt; b &amp;&amp; c &gt; d");
check("editor: quotes in text cannot break an attribute",
      ed.cleanNode(tag("BODY", [text('say "hi"')])), "say &quot;hi&quot;");

// A borrowed date is resolved on the server and arrives inside the row, so a
// section following a deliverable has to reload its *rows* when that
// deliverable moves -- reloading only the pick list leaves the old date on
// screen and makes working propagation look broken.
const { Section } = await import("../manila/static/table.js");

const watcher = (columns) => {
  const calls = [];
  const section = Object.create(Section.prototype);
  section.columns = columns;
  section.refresh = async () => { calls.push("refresh"); };
  section.loadHints = async () => { calls.push("loadHints"); };
  section.render = () => { calls.push("render"); };
  return { section, calls };
};

const borrows = [{ key: "resolve_by", link: { entity: "milestone" } }];
let probe = watcher(borrows);
await probe.section.linksChanged("milestone");
check("links: a moved deliverable reloads the rows", probe.calls.join(","), "refresh");

probe = watcher(borrows);
await probe.section.linksChanged("contact");
check("links: a change it does not borrow from is ignored", probe.calls.length, 0);

probe = watcher([{ key: "waiting_on", suggest: true }]);
await probe.section.linksChanged("milestone");
check("links: a section that borrows nothing is left alone", probe.calls.length, 0);

// Sections tell each other, and never tell themselves.
const heard = [];
const speaker = Object.create(Section.prototype);
const listener = Object.create(Section.prototype);
speaker.entity = "milestone";
listener.linksChanged = (entity) => heard.push(entity);
speaker.siblings = [speaker, listener];
speaker.announce();
check("links: a section announces to its siblings", heard, ["milestone"]);

speaker.siblings = [speaker];
heard.length = 0;
speaker.announce();
check("links: and never to itself", heard.length, 0);

// Which half of a change a line shows depends on the reading it is in. Getting
// this backwards makes an edit look like it logged the text you replaced.
const hist = await import("../manila/static/history.js");
const change = { old_value: "test 3", new_value: "test 4" };

check("history: a whole-entry line says what the cell became",
      hist.half(change, true).shown, "test 4");
check("history: a single-cell line says what it said before",
      hist.half(change, false).shown, "test 3");
check("history: the other half is always the one it does not show",
      [hist.half(change, true).other, hist.half(change, false).other],
      ["test 3", "test 4"]);
check("history: and the tooltip names which is which",
      [hist.half(change, true).word, hist.half(change, false).word],
      ["was", "became"]);

console.log(failures ? `\n${failures} failure(s)` : "\nall JS checks passed");
