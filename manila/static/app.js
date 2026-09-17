/* Shell: the project picker, folder tabs, and the four fixed page tabs.
 *
 * Nothing here creates content on its own. Projects, folders and entries exist
 * only because they were typed in. */

import { api, toast } from "./api.js";
import { Section, el } from "./table.js";
import { Repository } from "./repository.js";
import { Review } from "./review.js";
import { Sources } from "./sources.js";
import { atRest, describe as describeSave, onStatus } from "./status.js";

/* The sketch's fourth tab was System Prompt: instructions about where a model
 * should look. That belongs to Claude Desktop, and the notes it would have
 * kept are just Repository notes -- so the slot went to the other half of the
 * same idea. Review is where what a model found waits for you to decide,
 * because the useful thing was never telling it where to look; it was keeping
 * what it concluded out of the tables until you had read it. */
const PAGES = [
  { key: "dashboard",  label: "Dashboard" },
  { key: "contacts",   label: "Contact List" },
  { key: "repository", label: "Repository" },
  { key: "review",     label: "Review" },
];

const state = {
  schema: null,
  projects: [],
  projectId: null,
  folders: [],
  folderId: null,
  page: remembered(localStorage.getItem("manila.page")),
};

/** The tab last open, if it still exists. */
function remembered(key) {
  return PAGES.some((page) => page.key === key) ? key : "dashboard";
}

const view = document.getElementById("view");
const folderBar = document.getElementById("folder-bar");
const folderTabs = document.getElementById("folder-tabs");
const pageTabs = document.getElementById("page-tabs");
const emptyPanel = document.getElementById("empty");
const picker = document.getElementById("picker");
const pickerList = document.getElementById("picker-list");
const projectChip = document.getElementById("project-chip");

// --- boot --------------------------------------------------------------------

async function boot() {
  watchSaves();
  state.schema = await api.get("/api/schema");
  document.getElementById("new-folder").onclick = newFolder;
  document.getElementById("empty-new").onclick = newFolder;
  document.getElementById("picker-new").onclick = newProject;
  document.getElementById("picker-import").onclick = importProject;
  // Switching project is the chip's job and it happens at once. Delaying it to
  // watch for a second click made the one thing it does feel broken.
  projectChip.onclick = () => show(null);
  window.addEventListener("hashchange", () => show(hashProject(), false));
  await loadProjects();
  await show(hashProject() || localStorage.getItem("manila.project"));
}

/* Manila has no Save button, so it says instead -- continuously -- what has
 * been written and what has not. A failure stays on screen; it does not fade
 * like the toast that announced it. */
function watchSaves() {
  const label = document.getElementById("save-status");
  onStatus((status) => {
    const { text, tone, title } = describeSave(status);
    label.textContent = text;
    label.title = title;
    label.dataset.tone = tone;
  });

  // Typing in the repository is written a beat after you stop. Leaving the tab
  // writes it immediately rather than trusting the beat to finish.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") state.repo?.flushAll();
  });
  window.addEventListener("beforeunload", (event) => {
    state.repo?.flushAll();
    if (atRest()) return;
    event.preventDefault();
    event.returnValue = "";       // some changes are still being saved
  });
}

/* The project lives in the URL, so a bookmark or a link reopens exactly the
 * project it was made in -- and the back button steps between them. */
function hashProject() {
  return decodeURIComponent(location.hash.replace(/^#\/?/, "")) || null;
}

async function loadProjects() {
  state.projects = (await api.get("/api/projects")).projects ?? [];
}

/* The single entry point: an id opens that project, null shows the picker. */
async function show(projectId, updateHash = true) {
  const found = state.projects.find((p) => p.id === projectId);
  state.projectId = found ? found.id : null;
  api.project = state.projectId;

  if (updateHash) location.hash = found ? `#${found.id}` : "";
  if (found) localStorage.setItem("manila.project", found.id);

  if (!found) {
    if (projectId) toast(`No project "${projectId}".`);
    drawPicker();
    return;
  }
  applyAccent(found);
  state.folders = [];
  state.folderId = null;
  await loadFolders();
  draw();
}

/* Each project takes its own accent, so the window looks different depending on
 * which one is open -- worth having before typing a note into the wrong one. */
function applyAccent(project) {
  const root = document.documentElement.style;
  if (project?.color) root.setProperty("--accent", project.color);
  else root.removeProperty("--accent");
  document.title = project ? `Manila - ${project.name}` : "Manila";
}

// --- the picker --------------------------------------------------------------

function drawPicker() {
  applyAccent(null);
  picker.hidden = false;
  for (const node of [folderBar, pageTabs, view, emptyPanel]) node.hidden = true;

  pickerList.innerHTML = "";
  document.getElementById("picker-hint").textContent = state.projects.length
    ? "Projects share nothing but this port - separate directories, separate history."
    : "A project is a directory of its own. Nothing is created until you name one "
      + "- and a project copied from another machine is opened, not imported: "
      + "point Manila at its folder and it reads it where it lies.";

  for (const project of state.projects) {
    const item = el("li", "project-card");
    const strip = el("span", "tab-strip");
    strip.style.background = project.color || "var(--accent)";
    item.append(strip);

    const open = el("button", "open");
    const name = el("span", "name", project.name);
    open.append(name);
    open.append(el("span", "meta", describe(project)));
    open.onclick = () => show(project.id);
    open.disabled = project.status === "unavailable";
    if (project.status === "unavailable") open.title = project.detail || "";
    item.append(open);

    const tools = el("div", "tools");
    for (const [label, run] of [
      ["Rename", () => renameProject(project, name)],
      ["Color", () => recolorProject(project)],
      ["Remove", () => forgetProject(project)],
    ]) {
      const button = el("button", "ghost", label);
      button.onclick = run;
      tools.append(button);
    }
    item.append(tools);
    makeProjectDraggable(item, project);
    pickerList.append(item);
  }
}

function describe(project) {
  if (project.status === "unavailable") return `unavailable - ${project.path}`;
  const count = project.folders === 1 ? "1 folder" : `${project.folders} folders`;
  return `${count} - ${project.path}`;
}

function makeProjectDraggable(item, project) {
  item.draggable = true;
  item.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", project.id);
    item.classList.add("dragging");
  });
  item.addEventListener("dragend", () => item.classList.remove("dragging"));
  item.addEventListener("dragover", (event) => event.preventDefault());
  item.addEventListener("drop", async (event) => {
    event.preventDefault();
    const moved = event.dataTransfer.getData("text/plain");
    if (!moved || moved === project.id) return;
    const order = state.projects.map((p) => p.id);
    const after = order.indexOf(moved) > order.indexOf(project.id) && order[0] === project.id
      ? null : project.id;
    await api.post(`/api/projects/${moved}/move`, { after_id: after });
    await loadProjects();
    drawPicker();
  });
}

async function newProject() {
  const name = prompt("Project name")?.trim();
  if (!name) return;
  const parent = await chooseLocation(name);
  if (parent === null) return;              // cancelled
  try {
    const { project } = await api.post("/api/projects", { name, parent: parent || null });
    await loadProjects();
    await show(project.id);
  } catch (error) { toast(error.message); }
}

/* A project that already exists: this machine is new, the work is not.
 *
 * A project is a self-contained folder -- its database, and the documents
 * copied into it named relative to that folder -- so moving PCs is copying the
 * folder across and pointing Manila at it here. Nothing is copied, moved or
 * written by this: the folder stays where you put it. */
async function importProject() {
  const path = await chooseLocation(null, { importing: true });
  if (path === null) return;                // cancelled
  try {
    const { project } = await api.post("/api/projects/import", { path });
    await loadProjects();
    await show(project.id);
  } catch (error) { toast(error.message); }
}

/* Pick where a project's data lives by walking the machine's own directories.
 * Typed paths work too, because on Windows you often already know the share
 * you want and browsing to it is the slow way. Resolves to a directory to
 * create the project inside, "" for the default, or null if cancelled. */
function chooseLocation(name, { importing = false } = {}) {
  return new Promise((resolve) => {
    const backdrop = el("div", "modal-backdrop");
    const box = el("div", "modal");
    backdrop.append(box);
    box.append(el("h3", null, importing
      ? "Which folder holds the project?"
      : `Where should "${name}" live?`));
    box.append(el("p", "hint", importing
      ? "The folder a project was copied into -- the one with manila.db in it. "
        + "It is read where it lies: nothing is moved, copied or changed."
      : "Everything for this project - its database and every document you import "
        + "- goes in one folder here. Choose somewhere that gets backed up."));

    const shortcuts = el("div", "places");
    const crumbs = el("div", "crumbs");
    const listing = el("ul", "dirs");
    const typed = Object.assign(document.createElement("input"),
                                { type: "text", placeholder: "or type a full path" });
    const chosen = el("p", "chosen");
    // Hidden until asked for: most projects go in a folder that already exists.
    const maker = el("div", "maker");
    const folderName = Object.assign(document.createElement("input"),
                                     { type: "text", placeholder: "New folder name" });
    const makeIt = el("button", null, "Create folder");
    maker.append(folderName, makeIt);
    maker.hidden = true;
    box.append(shortcuts, crumbs, maker, listing, typed, chosen);

    const actions = el("div", "modal-actions");
    const useDefault = el("button", "ghost", "Use the default location");
    const cancel = el("button", "ghost", "Cancel");
    const confirm = el("button", "primary", importing ? "Open this project" : "Create here");
    // Importing goes to a folder that is already there, so there is no default
    // to fall back to and nothing to create.
    useDefault.hidden = importing;
    maker.hidden = true;
    actions.append(useDefault, el("span", "spacer"), cancel, confirm);
    box.append(actions);

    let here = null;
    let sep = "/";

    const close = (value) => { backdrop.remove(); resolve(value); };
    cancel.onclick = () => close(null);
    useDefault.onclick = () => close("");
    confirm.onclick = () => close(typed.value.trim() || here || (importing ? null : ""));
    backdrop.onclick = (event) => { if (event.target === backdrop) close(null); };
    /* Enter on a typed path opens it here rather than taking it on trust: you
     * see what is in it, and -- when opening an existing project -- whether it
     * is one at all, before committing to it. */
    typed.onkeydown = (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const said = typed.value.trim();
      if (said) go(said).then(() => { typed.value = ""; typed.oninput(); });
    };
    typed.oninput = () => {
      const said = typed.value.trim();
      if (importing) {
        chosen.textContent = said ? `Will open: ${said}` : preview(here);
        confirm.disabled = !said && !isProject;
        return;
      }
      chosen.textContent = said ? `${said}${sep}...` : preview(here);
    };

    let isProject = false;
    const preview = (dir) => {
      if (!dir) return "";
      if (!importing) return `Will be created in: ${dir}`;
      return isProject
        ? `Will open: ${dir}`
        : `${dir} is not a project. Open the folder that holds manila.db.`;
    };

    /* Make a folder where the project would have gone, and step into it, so the
     * one just named is the one it lands in without a second click. A typed
     * path wins over the browsed one here for the same reason it wins at
     * "Create here": it is the more recent thing said. */
    const makeFolder = async () => {
      const folder = folderName.value.trim();
      if (!folder) return;
      try {
        const parent = typed.value.trim() || here;
        const made = await api.post("/api/browse", { parent, name: folder });
        folderName.value = "";
        typed.value = "";              // else it would outrank the new folder
        await go(made.path);
      } catch (error) { toast(error.message); }
    };
    makeIt.onclick = makeFolder;
    folderName.onkeydown = (event) => {
      if (event.key === "Enter") { event.preventDefault(); makeFolder(); }
      if (event.key === "Escape") maker.hidden = true;
    };

    const go = async (path) => {
      try {
        const view = await api.get(
          `/api/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`);
        here = view.path;
        sep = view.sep;
        crumbs.innerHTML = "";
        if (view.parent) {
          const up = el("button", "ghost", "\u2191 Up");
          up.onclick = () => go(view.parent);
          crumbs.append(up);
        }
        crumbs.append(el("span", "path", view.path));
        if (!view.writable) crumbs.append(el("span", "warn", "not writable"));
        maker.hidden = true;
        isProject = Boolean(view.project);
        if (view.writable && !importing) {
          const add = el("button", "ghost", "New folder");
          add.onclick = () => {
            maker.hidden = !maker.hidden;
            if (!maker.hidden) folderName.focus();
          };
          crumbs.append(add);
        }

        listing.innerHTML = "";
        for (const entry of view.entries) {
          const item = el("li");
          const button = el("button", null, entry.name);
          button.onclick = () => go(entry.path);
          item.append(button);
          // Which of these is a project, so importing is a matter of seeing it
          // rather than remembering which folder it was.
          if (entry.project) item.append(el("span", "is-project", "project"));
          listing.append(item);
        }
        if (!view.entries.length) listing.append(el("li", "nothing", "No sub-folders."));
        chosen.textContent = typed.value.trim() && importing
          ? `Will open: ${typed.value.trim()}`
          : preview(here);
        confirm.disabled = importing
          ? !isProject && !typed.value.trim()
          : !view.writable;
      } catch (error) { toast(error.message); }
    };

    api.get("/api/places").then(({ places, default: fallback }) => {
      for (const place of places) {
        const button = el("button", "ghost", place.label);
        button.title = place.path;
        button.onclick = () => go(place.path);
        shortcuts.append(button);
      }
      useDefault.title = fallback;
      go(null);
    }).catch((error) => toast(error.message));

    document.body.append(backdrop);
    typed.focus();
  });
}

/* Renamed in place, not through a dialog.
 *
 * A browser starts refusing prompt() once a page has thrown a few, and a
 * refusal is indistinguishable from nothing happening. A text box cannot be
 * suppressed, does not block the page, and behaves like every other edit here:
 * Enter commits, Escape cancels, clicking away commits. */
function renameProject(project, nameNode) {
  if (!nameNode) return;
  const box = Object.assign(document.createElement("input"),
                            { type: "text", value: project.name,
                              className: "project-rename" });
  nameNode.replaceWith(box);
  box.focus();
  box.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const wanted = box.value.trim();
    if (!save || !wanted || wanted === project.name) { drawPicker(); return; }
    try {
      await api.patch(`/api/projects/${project.id}`, { name: wanted });
      await loadProjects();
      if (state.projectId === project.id) applyAccent(current());
      redraw();
    } catch (error) { toast(error.message); drawPicker(); }
  };
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); finish(true); }
    else if (event.key === "Escape") { event.preventDefault(); finish(false); }
  });
  box.addEventListener("blur", () => finish(true));
  box.addEventListener("click", (event) => event.stopPropagation());
}

async function recolorProject(project) {
  const color = prompt("Accent color as hex, e.g. #3d6b8a", project.color || "")?.trim();
  if (!color) return;
  try {
    await api.patch(`/api/projects/${project.id}`, { color });
    await loadProjects();
    if (state.projectId === project.id) applyAccent(current());
    redraw();
  } catch (error) { toast(error.message); }
}

/* Removing takes a project off this list and nothing else. Manila never deletes
 * a project's files -- if you want them gone, delete the directory yourself. */
async function forgetProject(project) {
  if (!confirm(`Remove "${project.name}" from this list?\n\n`
      + `Its data stays on disk at ${project.path}. Nothing is deleted.`)) return;
  try {
    await api.post(`/api/projects/${project.id}/forget`);
    await loadProjects();
    if (state.projectId === project.id) await show(null);
    else drawPicker();
    toast(`Removed from the list. Data kept at ${project.path}`);
  } catch (error) { toast(error.message); }
}

function current() {
  return state.projects.find((p) => p.id === state.projectId) || null;
}

/* Redraw whatever is actually on screen.
 *
 * Renaming happens on the picker, but the header carries the project's name
 * too, and only the picker was ever told. Which view is showing is a fact
 * about the page, not something each caller should have to remember. */
function redraw() {
  if (picker.hidden) draw();
  else drawPicker();
}

// --- folders -----------------------------------------------------------------

async function loadFolders() {
  state.folders = (await api.get(api.p("/folders"))).folders ?? [];
  const remembered = Number(localStorage.getItem(`manila.folder.${state.projectId}`));
  if (!state.folders.some((f) => f.id === state.folderId)) {
    state.folderId = state.folders.some((f) => f.id === remembered)
      ? remembered
      : state.folders[0]?.id ?? null;
  }
}

function draw() {
  const project = current();
  // A header with no project behind it can only render a blank chip. If the
  // project has gone, the picker is the honest thing to show.
  if (!project) { drawPicker(); return; }

  picker.hidden = true;
  folderBar.hidden = false;
  projectChip.textContent = project.name;
  projectChip.style.background = project.color || "";
  projectChip.title = `${project.path} - click to switch project`;

  drawFolderTabs();
  drawPageTabs();
  const none = state.folders.length === 0;
  emptyPanel.hidden = !none;
  pageTabs.hidden = none;
  view.hidden = none;
  if (!none) drawView();
  if (state.folderId) {
    localStorage.setItem(`manila.folder.${state.projectId}`, state.folderId);
  }
  localStorage.setItem("manila.page", state.page);
}

function drawFolderTabs() {
  folderTabs.innerHTML = "";
  for (const folder of state.folders) {
    const active = folder.id === state.folderId;
    const tab = el("button", `tab${active ? " active" : ""}`);
    tab.append(el("span", "label", folder.name));
    tab.onclick = () => { state.folderId = folder.id; draw(); };
    tab.ondblclick = () => renameFolder(folder);
    if (active) {
      const dot = el("span", "dot", "⋮");
      dot.title = "Folder options";
      dot.onclick = (event) => { event.stopPropagation(); folderMenu(event, folder); };
      tab.append(dot);
    }
    makeTabDraggable(tab, folder);
    folderTabs.append(tab);
  }
}

function makeTabDraggable(tab, folder) {
  tab.draggable = true;
  tab.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", String(folder.id));
    tab.classList.add("dragging");
  });
  tab.addEventListener("dragend", () => tab.classList.remove("dragging"));
  tab.addEventListener("dragover", (event) => event.preventDefault());
  tab.addEventListener("drop", async (event) => {
    event.preventDefault();
    const moved = Number(event.dataTransfer.getData("text/plain"));
    if (!moved || moved === folder.id) return;
    const order = state.folders.map((f) => f.id);
    const after = order.indexOf(moved) > order.indexOf(folder.id) && order[0] === folder.id
      ? null : folder.id;
    await api.post(api.p(`/folders/${moved}/move`), { after_id: after });
    await loadFolders();
    draw();
  });
}

function folderMenu(event, folder) {
  document.querySelector(".menu")?.remove();
  const menu = el("div", "menu");
  const rect = event.target.getBoundingClientRect();
  Object.assign(menu.style, {
    position: "fixed", top: `${rect.bottom + 4}px`, left: `${rect.left - 90}px`, zIndex: 30,
  });
  // No Archive: with no shelf to find them in, an archived folder would be
  // gone with no way back. Rows still archive -- their tables have a scope
  // switch to see them again.
  for (const [label, run] of [
    ["Rename", () => renameFolder(folder)],
    ["Delete permanently", () => folderAction(folder, "delete")],
  ]) {
    const item = el("button", null, label);
    item.onclick = () => { menu.remove(); run(); };
    menu.append(item);
  }
  document.body.append(menu);
  setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }));
}

async function newFolder() {
  const name = prompt("Folder name")?.trim();
  if (!name) return;
  const created = await api.post(api.p("/folders"), { name });
  await loadFolders();
  state.folderId = created.id;
  draw();
}

async function renameFolder(folder) {
  const name = prompt("Folder name", folder.name)?.trim();
  if (!name || name === folder.name) return;
  await api.patch(api.p(`/folders/${folder.id}`), { name });
  await loadFolders();
  draw();
}

async function folderAction(folder, action) {
  if (action === "delete" &&
      !confirm(`Delete "${folder.name}", everything in it, and its history, permanently?`
               + " This cannot be undone.")) return;
  try {
    await api.post(api.p(`/folders/${folder.id}/${action}`));
    if (state.folderId === folder.id) state.folderId = null;
    await loadFolders();
    draw();
  } catch (error) { toast(error.message); }
}

// --- page tabs ---------------------------------------------------------------

function drawPageTabs() {
  pageTabs.innerHTML = "";
  const folder = state.folders.find((f) => f.id === state.folderId);
  for (const page of PAGES) {
    const tab = el("button", `tab${state.page === page.key ? " active" : ""}`, page.label);
    // How many suggestions are waiting, on the tab itself. Without it the queue
    // is a tab you have to remember to click, which is a queue that is not read.
    if (page.key === "review" && folder?.pending > 0) {
      tab.append(el("span", "pending", String(folder.pending)));
    }
    tab.onclick = () => { state.page = page.key; draw(); };
    pageTabs.append(tab);
  }
}

// --- the view ----------------------------------------------------------------

/* One navigation can start this twice -- assigning the hash fires hashchange,
 * which runs show() again -- so two passes are in flight over the same view,
 * each awaiting its own load. Whichever finishes last must be the one on
 * screen, and the other must stop rather than appending its half of a page
 * beside it: that is how the Review tab came to render twice, once for each
 * pass, under a single Sources panel.
 *
 * So every pass takes a ticket, and checks after each await that it is still
 * the current one. Clearing the view is not enough on its own -- a stale pass
 * that appends after the clear appends into the live view. */
let pass = 0;

async function drawView() {
  // Leaving the Repository must not strand text typed a moment ago.
  state.repo?.flushAll();
  state.repo = null;
  view.innerHTML = "";

  const ticket = ++pass;
  const mine = () => ticket === pass;

  if (state.page === "review") {
    // Where to look, above what came back. Both hosts are placed before
    // anything is awaited, so a pass that loses the race renders into nodes
    // that are no longer in the page rather than into the one that won.
    const sourceHost = el("div");
    const host = el("div");
    view.append(sourceHost, host);

    const panel = new Sources(sourceHost, {
      folderId: state.folderId,
      kinds: state.schema.source_kinds ?? {},
    });
    globalThis.__manilaSources = panel;
    // Failing to draw the sources must not cost you the queue, which is the
    // half with decisions waiting in it.
    try {
      await panel.load();
      if (!mine()) return;
      panel.render();
    } catch (error) { toast(error.message); }
    if (!mine()) return;

    const review = new Review(host, {
      folderId: state.folderId,
      columns: state.schema.columns,
      // Deciding something changes the count on the tab, and accepting one can
      // change a table the Dashboard is showing -- so the folder list is reread
      // and the tabs repainted rather than left saying what was true before.
      onDecided: async () => { await loadFolders(); drawPageTabs(); },
    });
    globalThis.__manilaReview = review;
    await review.load();
    if (!mine()) return;
    review.render();
    return;
  }
  if (state.page === "repository") {
    const host = el("div");
    view.append(host);
    state.repo = new Repository(host, { folderId: state.folderId });
    await state.repo.load();
    if (!mine()) return;
    state.repo.render();
    return;
  }

  const sections = state.schema.sections[state.page];
  if (!sections) return;
  // The sections on a page know about each other, so a deliverable added to
  // one is offered by the other without a reload.
  const live = [];
  for (const spec of sections) {
    const host = el("div");
    view.append(host);
    live.push(new Section(host, {
      folderId: state.folderId,
      entity: spec.entity,
      title: spec.title,
      columns: state.schema.columns[spec.entity],
    }));
  }
  for (const section of live) section.siblings = live;
  // Reachable by the render tests; unused by the app itself.
  globalThis.__manilaSections = live;
  for (const section of live) {
    await section.load();
    if (!mine()) return;
    section.render();
  }
}

boot().catch((error) => {
  toast(error.message);
  console.error(error);
});
