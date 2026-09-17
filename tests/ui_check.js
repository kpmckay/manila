/* Rendering checks: the app driven through a small fake DOM.
 *
 * Run with: gjs -m tests/ui_check.js
 *
 * These exist because every recent visual bug lived in the render path -- a
 * view that was never told something changed -- and none of them were
 * reachable by testing pure functions.
 */

import { makeDocument, makeClock, settle } from "./fake_dom.js";

const IDS = ["folder-bar", "folder-tabs", "page-tabs", "view", "empty",
             "picker", "picker-list", "picker-new", "picker-import", "picker-hint",
             "project-chip", "new-folder", "empty-new", "save-status"];

const doc = makeDocument(IDS);
const clock = makeClock();

globalThis.document = doc;
// gjs already defines a read-only `window`; node and deno do not.
if (typeof window === "undefined") globalThis.window = globalThis;
/* A real location fires hashchange when the hash is assigned, which is how the
 * app ends up running show() twice for one navigation. */
let hash = "";
globalThis.location = {
  get hash() { return hash; },
  set hash(value) {
    const next = String(value);
    if (next === hash) return;
    hash = next;
    doc.dispatch("hashchange");
  },
};
globalThis.setTimeout = clock.setTimeout;
globalThis.clearTimeout = clock.clearTimeout;
globalThis.addEventListener = (type, fn) => doc.addEventListener(type, fn);
globalThis.ResizeObserver = undefined;

// As a returning visitor: a project was open last time.
const store = { "manila.project": "work" };
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};

let projectName = "Bob";
let asked = 0;
globalThis.prompt = (_message, _value) => { asked += 1; return "Renamed"; };
globalThis.confirm = () => true;

/* Just enough server to render against. */
const ROUTES = [
  [/\/api\/schema$/, () => ({
    columns: {
      action_item: [
        { key: "item", label: "Item", type: "longtext", width: "1fr" },
        { key: "waiting_on", label: "Waiting On", type: "text", width: "1fr",
          suggest: true },
        { key: "resolve_by", label: "Resolve By", type: "datenote", width: "1fr",
          storage: ["resolve_by_at", "resolve_by_note"], deadline: true,
          link: { entity: "milestone", column: "resolve_by_milestone_id",
                  since: "resolve_by_linked_at", label: "deliverable",
                  date: "due_date" } },
        { key: "current_state", label: "Current State", type: "longtext", width: "1fr" },
      ],
      milestone: [
        { key: "deliverable", label: "Deliverable", type: "text", width: "1fr" },
      ],
      contact: [
        { key: "name", label: "Name", type: "text", width: "1fr" },
        { key: "email", label: "Email", type: "text", width: "1fr" },
      ],
    },
    sections: { dashboard: [{ entity: "action_item", title: "Actions" },
                            { entity: "milestone", title: "Dates and Milestones" }] },
    source_kinds: {
      teams_channel: { label: "Teams channel", ref: "channel id", within: "team id",
                       add: "paste the channel link",
                       help: "Right-click the channel -> Get link to channel." },
      teams_chat: { label: "Teams chat", ref: "chat id", add: "chat id" },
      email_domain: { label: "Email domain", ref: "domain", add: "example.com" },
    },
  })],
  [/\/api\/projects$/, () => ({ projects: [
    { id: "work", name: projectName, path: "/tmp/work", color: "#8a6a35",
      folders: 1, status: "ok" },
  ] })],
  [/\/api\/projects\/[\w-]+$/, (_p, body) => {
    if (body?.name) projectName = body.name;
    return { project: { id: "work", name: projectName, path: "/tmp/work" } };
  }],
  [/\/folders$/, () => ({ folders: [
    { id: 1, name: "ACME", position: 1, archived: false, pending: decided ? 1 : 2 },
  ] })],
  /* One sync run waiting to be reviewed: two changes to one entry -- which is
   * what a sync that read one thread proposes, and which belongs on one card --
   * and one about an entry that has since been deleted. */
  /* Two pointers, and a watermark for one source but not the other -- the
   * case the panel exists to make visible. */
  [/\/folders\/\d+\/sources$/, () => ({
    sources: [
      { id: 1, kind: "teams_channel", name: "Acme Rollout",
        ref: "19:abc@thread.tacv2", within: "team-123" },
      { id: 2, kind: "email_domain", name: "", ref: "acme.example.com", within: "" },
    ],
    last_runs: [
      { source: "Outlook", last_run: "2026-09-14T09:12:00.000000+00:00",
        covered_through: "2026-09-14T06:00:00+00:00", runs: 3 },
      { source: "Teams", last_run: "2026-09-15T09:12:00.000000+00:00",
        covered_through: "", runs: 1 },
    ],
  })],
  [/\/sources\/\d+\/delete$/, () => ({ id: 1, deleted: true })],
  [/\/folders\/\d+\/suggestions/, () => ({
    pending: decided ? 1 : 2,
    batches: decided ? [] : [{
      id: 3, source: "Outlook", summary: "2 threads since 10-Sep",
      agent: "Claude Desktop", created_at: "2026-09-14T09:12:00.000000+00:00",
      pending: 3,
      suggestions: [
        { id: 11, batch_id: 3, kind: "update", entity: "action_item",
          entity_id: 7, field: "waiting_on", field_label: "Waiting On",
          type: "text", now: "Alice", seen: "Alice", value: "Sam Rivera",
          proposed: null, evidence: "'Re: the quote' - Sam Rivera, 13-Sep",
          reason: "Alice handed it over", ref: "AAMk-1", state: "pending",
          stale: false, gone: false, title: "Supplier quote",
          row: { item: "Supplier quote", waiting_on: "Alice",
                 resolve_by: { date: "", note: "", link: 0 },
                 current_state: "Waiting on quote" },
          created_at: "2026-09-14T09:12:00.000000+00:00", decided_at: null },
        /* The same thread also moved the item's wording, and the cell has
         * been typed into since -- one entry, two changes, one of them
         * needing a second look. */
        { id: 13, batch_id: 3, kind: "update", entity: "action_item",
          entity_id: 7, field: "item", field_label: "Item", type: "longtext",
          now: "Supplier quote (edited by me)", seen: "Supplier quote",
          value: "Supplier quote, revised", proposed: null,
          evidence: "'Re: the quote' - Sam Rivera, 13-Sep", reason: "", ref: "AAMk-1",
          state: "pending", stale: true, gone: false, title: "Supplier quote",
          row: { item: "Supplier quote (edited by me)", waiting_on: "Alice",
                 resolve_by: { date: "", note: "", link: 0 },
                 current_state: "Waiting on quote" },
          created_at: "2026-09-14T09:12:00.000000+00:00", decided_at: null },
        { id: 12, batch_id: 3, kind: "update", entity: "action_item",
          entity_id: 8, field: "item", field_label: "Item", type: "longtext",
          now: "", seen: "Packaging", value: "Packaging budget", proposed: null,
          evidence: "'Re: the quote' - Sam Rivera, 13-Sep", reason: "", ref: "AAMk-1",
          state: "pending", stale: false, gone: true, title: "",
          created_at: "2026-09-14T09:12:00.000000+00:00", decided_at: null },
      ],
    }],
  })],
  [/\/rows\/action_item/, () => ({ rows: [{
    id: 7, position: 1, archived: false,
    fields: { item: "Supplier quote", waiting_on: "Alice",
              resolve_by: { date: "", note: "", link: 0 },
              current_state: "Waiting on quote" },
    history_counts: { item: 0, waiting_on: 1, resolve_by: 0, current_state: 0 },
  }] })],
  [/\/rows\/milestone/, () => ({ rows: [] })],
  [/\/suggest\//, () => ({ values: ["Alice"] })],
  [/\/suggestions\/\d+\/(accept|reject)$/, () => { decided = true; return { state: "accepted" }; }],
  [/\/suggestions\/(accept|reject)$/, (_p, body) => {
    decided = true;
    return { decided: body.ids.length, held: [], results: [] };
  }],
  [/\/links\//, () => ({ options: [] })],
  [/\/api\/places$/, () => ({ places: [{ label: "Home", path: "/data" }],
                              default: "/data/manila" })],
  /* A folder holding one project and one ordinary folder, which is what
   * opening an existing project has to tell apart. */
  [/\/api\/browse/, (path) => (
    path.includes("Acme")
      ? { path: "/data/Acme Rollout", parent: "/data", sep: "/", writable: true,
          project: true, entries: [] }
      : { path: "/data", parent: null, sep: "/", writable: true, project: false,
          entries: [{ name: "Acme Rollout", path: "/data/Acme Rollout", project: true },
                    { name: "Letters", path: "/data/Letters", project: false }] })],
  [/\/api\/projects\/import$/, (_p, body) => ({ project: {
    id: "acme-rollout", name: "Acme Rollout", path: body.path, color: "#3d6b8a",
    folders: 1, status: "ok" } })],
];

let decided = false;
const posted = [];

globalThis.fetch = async (path, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  if ((options.method || "GET") !== "GET") posted.push([options.method, path, body]);
  for (const [pattern, reply] of ROUTES) {
    if (pattern.test(path)) {
      return { ok: true, json: async () => reply(path, body) };
    }
  }
  return { ok: true, json: async () => ({}) };
};

// --- checks ------------------------------------------------------------------

let failures = 0;
const check = (label, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures++;
    console.log(`FAIL  ${label}\n        got ${JSON.stringify(actual)},`
                + ` want ${JSON.stringify(expected)}`);
  } else {
    console.log(`ok    ${label}`);
  }
};

const chip = doc.known["project-chip"];
const header = doc.known["folder-bar"];
const picker = doc.known["picker"];

await import("../manila/static/app.js");
await settle();
await clock.run();
await settle();

check("boot: the project opens from its remembered id", chip.textContent, "Bob");
check("boot: the header is showing", header.hidden, false);
check("boot: the picker is not", picker.hidden, true);

/* Switching project is the chip's only job, and it has to happen at once.
 * A delay here to watch for a second click made it feel broken. */
chip.dispatch("click");
check("chip: one click switches straight away, with no timer to wait on",
      [picker.hidden, clock.pending()], [false, 0]);
await settle();

/* Renaming happens in the card, in a text box -- no dialog to be suppressed,
 * and nothing that blocks the page. */
const card = doc.known["picker-list"].querySelectorAll("li")[0];
const nameBefore = card.querySelectorAll("span")
  .find((n) => n.classList.contains("name"));
check("picker: the card shows the current name", nameBefore.textContent, "Bob");

const renameButton = card.querySelectorAll("button")
  .find((b) => b.textContent === "Rename");
renameButton.dispatch("click");
await settle();

const box = card.querySelectorAll("input")[0];
check("rename: a text box replaces the name, not a dialog", Boolean(box), true);
check("rename: no dialog was asked for", asked, 0);
check("rename: it starts from the current name", box?.value, "Bob");

box.value = "Renamed";
box.dispatch("keydown", { key: "Enter" });
await settle();
await clock.run();
await settle();

check("rename: the picker shows the new name",
      doc.known["picker-list"].querySelectorAll("span")
        .filter((n) => n.classList.contains("name"))
        .map((n) => n.textContent), ["Renamed"]);

/* And the header, which carries the name too, is not left behind. */
doc.known["picker-list"].querySelectorAll("button")
  .find((b) => b.classList.contains("open")).dispatch("click");
await settle();
await clock.run();
await settle();

check("rename: the header shows it too, with no reload",
      chip.textContent, "Renamed");
check("rename: and we are back in the project",
      [header.hidden, picker.hidden], [false, true]);

/* Escape leaves the name alone. */
chip.dispatch("click");
await settle();
const again = doc.known["picker-list"].querySelectorAll("li")[0];
again.querySelectorAll("button").find((b) => b.textContent === "Rename").dispatch("click");
await settle();
const second = again.querySelectorAll("input")[0];
second.value = "Discard me";
second.dispatch("keydown", { key: "Escape" });
await settle();
await clock.run();
await settle();
check("rename: Escape keeps the name it had", projectName, "Renamed");


/* --- opening a project that already exists -------------------------------- */
//
// Moving to another PC is copying the project's folder across; this is the
// other half. The dialog must not offer to create anything, and must say which
// folder it would open -- and refuse a folder that is not a project at all.

doc.known["picker-import"].dispatch("click");
await settle();
await clock.run();
await settle();

const modal = doc.body.querySelector(".modal");
const modalButton = (label) =>
  modal.querySelectorAll("button").find((b) => b.textContent === label);
check("import: it asks which folder holds the project",
      modal?.querySelectorAll("h3")[0]?.textContent, "Which folder holds the project?");
check("import: there is nothing to create, so no default to fall back on",
      modalButton("Use the default location")?.hidden, true);
check("import: a folder that is not a project cannot be opened",
      modalButton("Open this project")?.disabled, true);
check("import: and it says what to look for instead",
      modal.querySelector(".chosen")?.textContent.includes("manila.db"), true);
check("import: the ones that are projects are marked",
      modal.querySelectorAll("span").filter((n) => n.classList.contains("is-project"))
        .length, 1);

modal.querySelectorAll("button").find((b) => b.textContent === "Acme Rollout")
  .dispatch("click");
await settle();
await clock.run();
await settle();
check("import: standing in a project, it can be opened",
      modalButton("Open this project")?.disabled, false);
modalButton("Open this project").dispatch("click");
await settle();
await clock.run();
await settle();

check("import: opening one registers the folder, and creates nothing",
      posted.at(-1)?.slice(0, 2), ["POST", "/api/projects/import"]);
check("import: naming the folder it was pointed at",
      posted.at(-1)?.[2], { path: "/data/Acme Rollout" });


/* Typing in a cell. The editor must stay put and keep focus until you leave
 * it on purpose -- nothing behind the scenes may snatch it away. */
chip.dispatch("click");           // back to the picker
await settle();
doc.known["picker-list"].querySelectorAll("button")
  .find((b) => b.classList.contains("open")).dispatch("click");
await settle();
await clock.run();
await settle();

const cells = doc.known["view"].querySelectorAll("div")
  .filter((n) => n.classList.contains("editable"));
check("typing: the table rendered editable cells", cells.length > 0, true);

const target = cells.find((c) => c.dataset.field === "waiting_on");
target.dispatch("click", { target });
await settle();
await clock.run();
await settle();

const editing = doc.activeElement;
check("typing: clicking a cell puts focus in an editor",
      Boolean(editing) && editing.tagName === "INPUT", true);

// Type, the way a keystroke arrives.
editing.value = "Alice and Bob";
editing.dispatch("input");
await settle();
await clock.run();
await settle();

check("typing: focus is still in the same editor after typing",
      doc.activeElement === editing, true);
check("typing: what was typed is still there", editing.value, "Alice and Bob");
check("typing: nothing was left pending that would yank it away",
      clock.pending(), 0);


/* The thing that must never happen: work in the background redrawing a table
 * while you are typing in it, which takes the editor, the focus and the
 * half-typed value with it. */
const { Section } = await import("../manila/static/table.js");
const sections = [];
for (const host of doc.known["view"].querySelectorAll("div")) {
  if (host.classList.contains("section")) sections.push(host);
}

// Reach the live Section objects the way the app wired them together.
const actions = globalThis.__manilaSections?.[0];
if (actions) {
  actions.editing = target;              // pretend a cell is open
  await actions.linksChanged("milestone");
  check("editing: a background refresh is held back, not applied",
        [actions.stale, doc.activeElement === editing], [true, true]);
  actions.editing = null;
  actions.stale = false;
} else {
  check("editing: a background refresh is held back, not applied", "no handle", "no handle");
}


/* The reported sequence, start to finish: rename the project, come back into
 * it, then use the page tabs. They stopped responding until a reload. */
const pageTabs = doc.known["page-tabs"];
const tabNamed = (label) => pageTabs.querySelectorAll("button")
  .find((b) => b.textContent.startsWith(label));

chip.dispatch("click");                       // out to the picker
await settle();
await clock.run();
await settle();

const renameAgain = doc.known["picker-list"].querySelectorAll("button")
  .find((b) => b.textContent === "Rename");
renameAgain.dispatch("click");
await settle();
const field = doc.known["picker-list"].querySelectorAll("input")[0];
field.value = "After Rename";
field.dispatch("keydown", { key: "Enter" });
await settle();
await clock.run();
await settle();

doc.known["picker-list"].querySelectorAll("button")
  .find((b) => b.classList.contains("open")).dispatch("click");
await settle();
await clock.run();
await settle();

check("after rename: the header carries the new name", chip.textContent, "After Rename");
check("after rename: the page tabs are on screen",
      [pageTabs.hidden, pageTabs.querySelectorAll("button").length], [false, 4]);

const contacts = tabNamed("Contact List");
check("after rename: the Contact List tab exists", Boolean(contacts), true);
contacts?.dispatch("click");
await settle();
await clock.run();
await settle();

check("after rename: clicking a page tab switches to it",
      tabNamed("Contact List")?.classList.contains("active"), true);
check("after rename: and the dashboard is no longer the active one",
      tabNamed("Dashboard")?.classList.contains("active"), false);
check("after rename: we are still in the project, not the picker",
      [header.hidden, picker.hidden], [false, true]);


/* The sketch's fourth tab is back, as the other half of the same idea: not
 * where a model is told to look, but where what it concluded waits. */
check("tabs: the sketch's fourth tab is Review",
      pageTabs.querySelectorAll("button").map((b) => b.textContent.replace(/\d+$/, "")),
      ["Dashboard", "Contact List", "Repository", "Review"]);
check("tabs: nothing claims to be unbuilt any more",
      doc.known["view"].querySelectorAll("div")
        .some((n) => n.classList.contains("placeholder")), false);


/* --- the Review tab ------------------------------------------------------- */

check("review: the tab says how many are waiting",
      tabNamed("Review")?.querySelector(".pending")?.textContent, "2");

tabNamed("Review").dispatch("click");
await settle();
await clock.run();
await settle();

const view = doc.known["view"];
const cards = view.querySelectorAll(".suggestion");
/* One card per entry, not per change: a run that touched three cells of one
 * entry used to show the whole entry three times over. */
check("review: one card per entry, not per proposed change", cards.length, 2);
check("review: the run says where it read from",
      view.querySelector(".batch-source")?.textContent, "Outlook");
check("review: and what it covered",
      view.querySelector(".batch-summary")?.textContent, "2 threads since 10-Sep");

/* One navigation can start drawView twice -- assigning the hash fires
 * hashchange, which runs show() again -- so two passes race over one view.
 * The loser used to append its Review section after the winner had cleared
 * and refilled the page, giving two identical queues under one Sources panel. */
tabNamed("Dashboard").dispatch("click");
tabNamed("Review").dispatch("click");
tabNamed("Review").dispatch("click");
await settle();
await clock.run();
await settle();

check("review: two passes over one view leave one queue",
      view.querySelectorAll(".review").length, 1);
check("review: and one Sources panel above it",
      view.querySelectorAll(".sources").length, 1);


/* --- sources: where this folder's news arrives ---------------------------- */
//
// A folder that already lists its sources opens with the panel shut, because
// the queue is what you came for; shut, it still has to say enough to notice
// that something has never been read.

check("sources: a folder that has them starts shut",
      view.querySelector(".sources-box"), null);
check("sources: shut, it still says what is listed and how far it is read",
      view.querySelector(".sources-line")?.textContent,
      "1 Teams channel, 1 Email domain \u00b7 Outlook read to 14-Sep \u00b7 Teams last run 15-Sep");

view.querySelector(".sources-toggle").dispatch("click");
await settle();

const rows = view.querySelectorAll(".source-row");
check("sources: open, one row per pointer", rows.length, 2);
check("sources: each says what kind of pointer it is",
      rows.map((r) => r.querySelector(".source-kind").textContent),
      ["Teams channel", "Email domain"]);
check("sources: the id is shown whole, not summarised",
      rows[0].querySelector(".source-ref").value, "19:abc@thread.tacv2");
check("sources: a channel also carries the team that holds it",
      rows[0].querySelector(".source-within").value, "team-123");
check("sources: a kind with no container has no box for one",
      rows[1].querySelector(".source-within"), null);

/* The fact that decides how far back the next run goes. A source that has run
 * but never said how far it read has no watermark, and saying "no watermark"
 * is the point -- "1 run" alone reads like it is covered. */
const coverage = view.querySelectorAll(".coverage-run");
check("sources: a source that said how far it read says so",
      coverage[0].querySelector(".coverage-point").textContent, "read through 14-Sep");
check("sources: one that did not is called out rather than assumed covered",
      coverage[1].querySelector(".coverage-point").textContent, "no watermark");

/* Editing a pointer saves the one field, to the source, not to the folder. */
const ref = rows[1].querySelector(".source-ref");
ref.value = "example.com";
ref.dispatch("change");
await settle();
check("sources: editing one patches that source alone",
      posted.at(-1)?.slice(0, 2), ["PATCH", "/api/p/work/sources/2"]);
check("sources: and sends only the field that changed",
      posted.at(-1)?.[2], { ref: "example.com" });
/* Every write reaches the header's save status, not only the table's. */
check("sources: the edit shows in the save status",
      doc.getElementById("save-status").textContent.startsWith("Saved"), true);

/* Fifty characters someone went and looked up in Graph. Removing one asks. */
const asks = [];
const realConfirm = globalThis.confirm;
globalThis.confirm = (message) => { asks.push(message); return false; };
const before = posted.length;
rows[0].querySelector(".trash").dispatch("click");
await settle();
globalThis.confirm = realConfirm;
check("sources: removing one asks before it does", asks.length, 1);
check("sources: and says no by doing nothing", posted.length, before);

/* Adding one asks for the link, not for two ids picked out of it by hand --
 * and, having asked, lets you leave again. The cancel used to be a bare cross
 * appended past the end of a five-column grid, which put it on a row of its
 * own under the kind label, where nobody found it. */
const addBar = () => view.querySelector(".source-add");
const addButton = (label) =>
  addBar().querySelectorAll("button").find((b) => b.textContent === label);
const cardButton = (label) => view.querySelector(".source-new-actions")
  .querySelectorAll("button").find((b) => b.textContent === label);

addButton("Add Teams channel").dispatch("click");
await settle();

const adding = view.querySelector(".source-new");
check("sources: adding a channel asks for one thing",
      adding.querySelectorAll(".source-new-ref").length, 1);
check("sources: and says where to get it",
      adding.querySelector(".source-new-ref").placeholder, "paste the channel link");
check("sources: with the two clicks that answer it in words",
      view.querySelector(".source-new-actions")
          .querySelectorAll("button").map((b) => b.textContent),
      ["Add", "Cancel"]);

const paste = adding.querySelector(".source-new-ref");
paste.value = "https://teams.microsoft.com/l/channel/19%3aabc%40thread.tacv2/FW?groupId=g-1";
paste.dispatch("input");
cardButton("Add").dispatch("click");
await settle();
check("sources: the link goes to the server whole, to be unpacked there",
      posted.at(-1)?.slice(0, 2), ["POST", "/api/p/work/folders/1/sources"]);
check("sources: as the one thing that was typed",
      posted.at(-1)?.[2],
      { kind: "teams_channel",
        ref: "https://teams.microsoft.com/l/channel/19%3aabc%40thread.tacv2/FW?groupId=g-1" });

addButton("Add Teams chat").dispatch("click");
await settle();
check("sources: the add card is open", view.querySelector(".source-new") !== null, true);
cardButton("Cancel").dispatch("click");
await settle();
check("sources: cancel backs out of it", view.querySelector(".source-new"), null);
check("sources: and puts the choices back",
      view.querySelector(".source-add") !== null, true);

view.querySelector(".sources-toggle").dispatch("click");
await settle();


/* The comparison is the whole point: what the cell says now has to be on
 * screen beside what is proposed, or accepting is an act of faith. */
const sides = cards[0].querySelectorAll(".side").map((n) => n.textContent);
check("review: now and proposed are shown together, for every changed cell",
      sides, ["nowSupplier quote (edited by me)", "proposedSupplier quote, revised",
              "nowAlice", "proposedSam Rivera"]);
/* Named in the order the entry reads, so the head and the list agree. */
check("review: the card names every cell the run would change",
      cards[0].querySelector(".what")?.textContent, "Item, Waiting On");
check("review: and which entry",
      cards[0].querySelector(".about")?.textContent, "Supplier quote");
check("review: the mail it rests on is shown",
      cards[0].querySelector(".evidence")?.textContent,
      "'Re: the quote' - Sam Rivera, 13-Sep");

/* A change to one cell is shown inside the whole entry, as a new entry is,
 * so it is read with the rest of the row around it. */
const entry = cards[0].querySelector(".fields");
check("review: an edit shows every column of the entry",
      entry?.querySelectorAll("dt").map((n) => n.textContent),
      ["Item", "Waiting On", "Resolve By", "Current State"]);
check("review: with every cell being changed marked among them",
      entry.querySelectorAll("dt").filter((n) => n.classList.contains("changed"))
        .map((n) => n.textContent), ["Item", "Waiting On"]);
const entryCells = entry.querySelectorAll("dd");
check("review: a cell nothing proposes reads as the table shows it, and is "
      + "there to be edited by hand",
      [entryCells[2].textContent, entryCells[2].classList.contains("editable")],
      ["(empty)", true]);
check("review: and an empty one says so quietly",
      entryCells[2].classList.contains("empty"), true);
/* Why each change was proposed belongs with that change, not in one heap at
 * the foot of a card carrying three of them. */
check("review: each change carries its own reason",
      entryCells[1].querySelector(".reason")?.textContent,
      "Alice handed it over");
check("review: one source for the whole card is said once",
      cards[0].querySelectorAll(".quote").map((n) => n.textContent),
      ["'Re: the quote' - Sam Rivera, 13-Sep"]);
/* One wrong change among three must not cost the other two. */
check("review: each waiting change can be turned down on its own",
      entry.querySelectorAll(".change-drop").length, 2);
check("review: the comparison sits in the changed cell",
      entryCells[1].querySelectorAll(".side").length, 2);
check("review: an entry that is gone falls back to the cell alone",
      cards[1].querySelector(".fields"), null);

/* The rest of the entry can be changed from the card, as your own edit. A
 * suggestion about one cell is often what reminds you another is wrong. */
const liveCells = () => view.querySelectorAll(".suggestion")[0].querySelectorAll("dd");
check("review: the entry's other cells can be edited from the card",
      liveCells().map((n) => n.classList.contains("editable")),
      [false, false, true, true]);
check("review: but not on a card whose entry is gone",
      view.querySelectorAll(".suggestion")[1].querySelectorAll(".editable").length, 0);

liveCells()[3].dispatch("click");
await settle();
const itemCell = liveCells()[3];
check("review: clicking one opens the table's own editor in place",
      itemCell.querySelector("textarea")?.value, "Waiting on quote");
itemCell.dispatch("click");
check("review: a click inside it does not open a second one",
      itemCell.querySelectorAll(".cell-editor").length, 1);
itemCell.querySelectorAll("button").find((b) => b.textContent === "Cancel")
  .dispatch("click");
check("review: cancel puts the value back",
      itemCell.textContent, "Waiting on quote");

liveCells()[3].dispatch("click");
await settle();
liveCells()[3].querySelector("textarea").value = "Quote in, waiting on approval";
const sentBefore = posted.length;
liveCells()[3].querySelectorAll("button").find((b) => b.textContent === "Save")
  .dispatch("click");
await settle();
check("review: saving writes that cell to the entry, now",
      posted.slice(sentBefore).map((p) => p.slice(0, 3)),
      [["PATCH", "/api/p/work/rows/action_item/7",
        { field: "current_state", value: "Quote in, waiting on approval" }]]);
check("review: and leaves the suggestion waiting",
      view.querySelectorAll(".suggestion")[0].classList.contains("pending"), true);

// The live card: `cards` was read before a later pass redrew the view.
view.querySelectorAll(".suggestion")[0].querySelector(".suggestion-actions")
  .querySelectorAll("button")[1].dispatch("click");
await settle();
const editingCard = view.querySelectorAll(".suggestion")[0];
const changedCell = editingCard.querySelectorAll("dd")
  .find((n) => n.classList.contains("changed"));
check("review: editing opens the editor in the changed cell, in the entry",
      changedCell?.querySelector("textarea") !== null, true);
check("review: without naming the cell a second time",
      changedCell.querySelector(".editor-label"), null);
editingCard.querySelector(".editor-actions").querySelectorAll("button")
  .find((b) => b.textContent === "Cancel").dispatch("click");
await settle();

/* A cell you have edited since must not be replaced without being told. */
check("review: a change overtaken by your own edit flags its card",
      cards[0].classList.contains("stale"), true);
check("review: and says so in words, not just in colour",
      cards[0].querySelector(".warn-line") !== null, true);
check("review: the stale one still shows what it would replace",
      cards[0].querySelectorAll(".side").map((n) => n.textContent).slice(0, 2),
      ["nowSupplier quote (edited by me)", "proposedSupplier quote, revised"]);
/* Which of several changes went stale, not just that one did. */
check("review: and which of the card's changes it is",
      cards[0].querySelectorAll("dd").map(
        (n) => n.querySelectorAll(".flag").length), [1, 0, 0, 0]);

/* Accepting is a decision, so it is one click on a control that is always
 * visible -- not one that appears on hover like a row's icons. */
const accept = view.querySelectorAll(".suggestion")[0].querySelector(".accept");
check("review: every card offers accept without hovering", Boolean(accept), true);
accept.dispatch("click");
await settle();
await clock.run();
await settle();

check("review: accepting posts the entry's changes together, not the row",
      posted.at(-1)?.slice(0, 2), ["POST", "/api/p/work/suggestions/accept"]);
check("review: naming each one it is deciding",
      posted.at(-1)?.[2]?.ids, [11, 13]);
check("review: and the tab count is reread rather than assumed",
      tabNamed("Review")?.querySelector(".pending")?.textContent, "1");

/* Editing before accepting is the common case -- a suggestion right but for
 * one word -- so it opens the cell's own editor rather than a plain box. */
tabNamed("Review").dispatch("click");
await settle();
await clock.run();
await settle();

/* --- ordering by date ----------------------------------------------------- */

/* Dates and Milestones, in the order they were dragged into: undated first,
 * then the later deadline, a tie, and the earliest last. */
ROUTES.unshift([/\/folders\/9\/rows\/milestone/, () => ({ rows: [
  { id: 21, position: 1, fields: { deliverable: "TBD", due_date: "" }, history_counts: {} },
  { id: 22, position: 2, fields: { deliverable: "Phase One", due_date: "2026-11-01" }, history_counts: {} },
  { id: 23, position: 3, fields: { deliverable: "Phase Two", due_date: "2026-10-01" }, history_counts: {} },
  { id: 24, position: 4, fields: { deliverable: "Rollout", due_date: "2026-10-01" }, history_counts: {} },
  { id: 25, position: 5, fields: { deliverable: "Kickoff", due_date: "2026-09-01" }, history_counts: {} },
] })]);

const dated = [
  { key: "deliverable", label: "Deliverable", type: "text", width: "1fr" },
  { key: "due_date", label: "Date", type: "date", width: "1fr", deadline: true },
];
const ordered = (s) => s.rows.map((r) => r.fields.deliverable);
const pill = (host, label) => host.querySelectorAll("button")
  .find((b) => b.classList.contains("scope") && b.textContent === label);
const grips = (host) => host.querySelectorAll("div")
  .filter((n) => n.classList.contains("grip-cell") && n.parentNode?.classList.contains("row")
          && !n.parentNode.classList.contains("head") && !n.parentNode.classList.contains("new"))
  .map((n) => n.draggable);

const sortHost = doc.createElement("div");
const milestones = new Section(sortHost, { folderId: 9, entity: "milestone",
                                           title: "Dates and Milestones", columns: dated });
await milestones.refresh();
check("order: a table starts in your own order", ordered(milestones),
      ["TBD", "Phase One", "Phase Two", "Rollout", "Kickoff"]);
check("order: a table with a deadline offers both orders",
      [Boolean(pill(sortHost, "My order")), Boolean(pill(sortHost, "By date"))], [true, true]);
check("order: My order is the one selected",
      pill(sortHost, "My order").classList.contains("on"), true);

pill(sortHost, "By date").dispatch("click");
await settle();
check("order: By date puts the soonest first, undated last, ties in your order",
      ordered(milestones), ["Kickoff", "Phase Two", "Rollout", "Phase One", "TBD"]);
check("order: the choice is remembered for the table", store["manila.order.milestone"], "due");
check("order: rows cannot be dragged while sorted by date",
      grips(sortHost).every((d) => d === false), true);

const reopened = new Section(doc.createElement("div"),
  { folderId: 9, entity: "milestone", title: "Dates and Milestones", columns: dated });
await reopened.load();
check("order: the next time the table is drawn, it is still by date",
      ordered(reopened), ["Kickoff", "Phase Two", "Rollout", "Phase One", "TBD"]);

pill(sortHost, "My order").dispatch("click");
await settle();
check("order: switching back restores your own order exactly",
      ordered(milestones), ["TBD", "Phase One", "Phase Two", "Rollout", "Kickoff"]);
check("order: and rows can be dragged again", grips(sortHost).every(Boolean), true);

/* Resolve By holds its date inside a composite value, borrowed or not. */
const byResolve = new Section(doc.createElement("div"), { folderId: 9, entity: "action_item",
  title: "Actions", columns: [{ key: "resolve_by", label: "Resolve By",
                                type: "datenote", deadline: true }] });
byResolve.order = "due";
check("order: Resolve By sorts on the date inside the cell",
      byResolve.arrange([
        { id: 1, fields: { resolve_by: { date: "", note: "ASAP", link: 0 } } },
        { id: 2, fields: { resolve_by: { date: "2026-12-01", note: "", link: 4 } } },
        { id: 3, fields: { resolve_by: { date: "2026-09-20", note: "EOM", link: 0 } } },
      ]).map((r) => r.id), [3, 2, 1]);

const contactsOnly = new Section(doc.createElement("div"), { folderId: 9, entity: "contact",
  title: "Contact List", columns: [{ key: "name", label: "Name", type: "text" }] });
check("order: a table with no deadline has nothing to sort by", contactsOnly.dateColumn, null);

/* Changing a date while sorted moves the row -- but only once you are done,
 * never out from under the cell you are tabbing into. */
pill(sortHost, "By date").dispatch("click");
await settle();
ROUTES.unshift([/\/rows\/milestone\/22$/, () => ({ changed: true, value: "2026-08-01",
                                                  history_count: 1 })]);
const phaseCell = sortHost.querySelectorAll("div")
  .find((n) => n.dataset?.row === "22" && n.dataset?.field === "due_date");
await milestones.save(22, dated[1], "2026-08-01", phaseCell);
check("order: a date that changes the order holds the move for later",
      [milestones.stale, ordered(milestones)[0]], [true, "Kickoff"]);
milestones.stale = false;

console.log(failures ? `\n${failures} failure(s)` : "\nall UI checks passed");
