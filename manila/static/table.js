/* The table component.
 *
 * Written once and reused by Actions, Dates and Milestones, and the Contact
 * List -- they differ only in their column definitions. Everything about how a
 * row behaves (inline editing, per-cell history, drag to reorder, archive and
 * trash) lives here. */

import { api, toast } from "./api.js";
import * as fmt from "./format.js";
import * as icons from "./icons.js";
import { renderHistory } from "./history.js";

const GRIP = "1.7rem";
const ACTIONS = "6.4rem";   // three icons and room around them
const SCOPES = [["active", "Active"], ["archived", "Archived"]];
const ORDERS = [["manual", "My order"], ["due", "By date"]];

export class Section {
  constructor(host, { folderId, entity, title, columns }) {
    Object.assign(this, { host, folderId, entity, title, columns });
    this.scope = "active";
    this.rows = [];
    this.openCell = null;   // {rowId, field} whose history is expanded
    this.siblings = [];     // the other sections on this page
    this.editing = null;    // the cell with an open editor, if any
    this.stale = false;     // a refresh that arrived while one was open
    // A table with a deadline can be read soonest-first. Which way you last
    // read it is remembered per table, across folders: it is how you like to
    // look at Actions, not a property of one folder's actions.
    this.dateColumn = columns.find((c) => c.deadline) ?? null;
    this.order = this.dateColumn && localStorage.getItem(this.orderKey()) === "due"
      ? "due" : "manual";
  }

  orderKey() { return `manila.order.${this.entity}`; }

  async load() {
    const path = api.p(`/folders/${this.folderId}/rows/${this.entity}?scope=${this.scope}`);
    this.rows = this.arrange((await api.get(path)).rows);
    await this.loadHints();
  }

  /* Soonest first, overdue included; anything with no date sinks below
   * everything that has one. Rows arrive in your own order and the sort is
   * stable, so whatever the dates cannot tell apart keeps that order -- and
   * switching back and forth never shuffles it. */
  arrange(rows) {
    if (this.order !== "due") return rows;
    const { key, type } = this.dateColumn;
    const dateOf = (row) => (type === "datenote" ? row.fields[key]?.date : row.fields[key]) || "";
    return [...rows].sort((a, b) => {
      const x = dateOf(a), y = dateOf(b);
      if (x === y) return 0;
      if (!x || !y) return x ? -1 : 1;
      return x < y ? -1 : 1;
    });
  }

  /* What has already been typed into the suggest columns of this folder, so
   * the same person gets spelled the same way the second time. Failing to
   * fetch them costs a dropdown, never an edit -- so failures stay quiet. */
  /* One section's rows can be another's pick list: a deliverable added to
   * Dates and Milestones is immediately something an action can resolve by.
   * Without this the picker stays empty until the page is reloaded, which
   * reads as the feature not being there at all. */
  announce() {
    for (const other of this.siblings) {
      if (other !== this) other.linksChanged(this.entity);
    }
  }

  /* Reload the rows, not just the pick list.
   *
   * A borrowed date is resolved on the server and arrives inside the row, so
   * a deliverable that moved changes the *rows* of every section following it.
   * Refreshing only the options left the old date on screen until a reload --
   * the propagation was working and invisible. */
  async linksChanged(entity) {
    if (!this.columns.some((col) => col.link?.entity === entity)) return;
    /* Never redraw a table out from under someone typing in it. Rebuilding the
     * rows destroys the open editor, which takes the focus and the half-typed
     * value with it. The refresh waits its turn. */
    if (this.editing) { this.stale = true; return; }
    await this.refresh();
  }

  async loadHints() {
    this.hints = {};
    this.links = {};
    const suggest = this.columns.filter((c) => c.suggest);
    const borrows = this.columns.filter((c) => c.link);
    await Promise.all([
      ...suggest.map(async (col) => {
        try {
          const { values } = await api.get(
            api.p(`/folders/${this.folderId}/suggest/${this.entity}/${col.key}`));
          this.hints[col.key] = values ?? [];
        } catch { this.hints[col.key] = []; }
      }),
      // What a borrowed date can be taken from, so the picker is current the
      // moment a deliverable is added.
      ...borrows.map(async (col) => {
        try {
          const { options } = await api.get(
            api.p(`/folders/${this.folderId}/links/${this.entity}/${col.key}`));
          this.links[col.key] = options ?? [];
        } catch { this.links[col.key] = []; }
      }),
    ]);
  }

  async refresh() {
    await this.load();
    this.render();
  }

  // --- rendering ------------------------------------------------------------

  render() {
    closeAllCombos();
    this.host.innerHTML = "";
    const section = el("section", "section");

    const head = el("div", "section-head");
    head.append(el("div", "section-title", this.title));
    const scopes = el("div", "scopes");
    for (const [key, label] of SCOPES) {
      const button = el("button", `scope${this.scope === key ? " on" : ""}`, label);
      button.onclick = () => { this.scope = key; this.openCell = null; this.refresh(); };
      scopes.append(button);
    }
    head.append(scopes);
    if (this.dateColumn) head.append(this.orderPills());
    head.append(el("span", "count", `${this.rows.length}`));
    section.append(head);

    const table = el("div", "table");
    table.style.setProperty(
      "--cols",
      [GRIP, ...this.columns.map((c) => c.width), ACTIONS].join(" ")
    );
    table.append(this.headRow());
    for (const row of this.rows) table.append(this.group(row));
    if (this.scope === "active") table.append(this.newRow());

    // One delegated listener rather than one per cell: cells are repainted
    // constantly, the table is not.
    table.addEventListener("click", (event) => {
      const cell = event.target.closest(".cell.editable");
      if (cell && table.contains(cell)) this.beginEdit(cell);
    });

    this.table = table;
    section.append(table);
    this.host.append(section);
  }

  orderPills() {
    const pills = el("div", "scopes orders");
    for (const [key, label] of ORDERS) {
      const button = el("button", `scope${this.order === key ? " on" : ""}`, label);
      button.title = key === "due"
        ? `Soonest ${this.dateColumn.label} first; no date goes last`
        : "The order you dragged them into";
      button.onclick = () => {
        if (this.order === key) return;
        this.order = key;
        localStorage.setItem(this.orderKey(), key);
        this.refresh();
      };
      pills.append(button);
    }
    return pills;
  }

  headRow() {
    const row = el("div", "row head");
    row.append(el("div", "cell"));
    for (const col of this.columns) row.append(el("div", "cell", col.label));
    row.append(el("div", "cell"));
    return row;
  }

  group(rowData) {
    const group = el("div", "group");
    group.dataset.id = rowData.id;
    if (rowData.fields.done) group.classList.add("done");

    const row = el("div", "row");
    row.append(this.gripCell(group, rowData));
    for (const col of this.columns) row.append(this.cell(rowData, col));
    row.append(this.actionsCell(rowData));
    group.append(row);

    if (this.openCell?.rowId === rowData.id) {
      const panel = el("div", "history");
      group.append(panel);
      renderHistory(panel, {
        entity: this.entity,
        row: rowData,
        columns: this.columns.filter((c) => c.type !== "check"),
        field: this.openCell.field,
        // Switching reading is a filter on the same log, not a different one.
        onSelect: (field) => { this.openCell = { rowId: rowData.id, field }; this.render(); },
        // Deleting an entry changes a chip count, so the row is repainted from
        // the count the server reports back.
        onChange: (field, remaining) => {
          if (field) rowData.history_counts[field] = remaining;
          if (!this.rowHasHistory(rowData)) this.openCell = null;
          this.render();
        },
      });
    }
    this.wireDrag(group);
    return group;
  }

  cell(rowData, col) {
    const cell = el("div", "cell");
    cell.dataset.row = rowData.id;
    cell.dataset.field = col.key;
    cell.dataset.type = col.type;

    if (col.type === "check") {
      cell.classList.add("check-cell");
      const box = Object.assign(document.createElement("input"), {
        type: "checkbox",
        checked: !!rowData.fields.done,
      });
      box.onchange = () => this.save(rowData.id, col, box.checked);
      cell.append(box);
      return cell;
    }

    cell.classList.add("editable");
    cell.append(this.display(rowData.fields[col.key], col));

    const count = rowData.history_counts?.[col.key] || 0;
    if (count > 0) cell.append(this.chip(rowData.id, col, count));
    return cell;
  }

  display(value, col) {
    const wrap = el("div", "value");
    // A deadline that has passed is the one thing in the table that should
    // catch the eye on its own.
    const late = (iso) => col.deadline && fmt.isPast(iso) ? " overdue" : "";
    if (col.type === "date") {
      wrap.textContent = fmt.absolute(value);
      if (value) wrap.append(el("span", `rel${late(value)}`, fmt.relative(value)));
    } else if (col.type === "datenote") {
      wrap.textContent = [value.via, value.note].filter(Boolean).join(" - ");
      if (value.date) {
        wrap.prepend(el("span", `rel${late(value.date)}`,
                        `${fmt.relative(value.date)} · ${fmt.absolute(value.date)}`));
      }
      // Where a borrowed date came from, so a slipped deliverable is traceable
      // from the action that is waiting on it.
      if (value.link_label) {
        wrap.append(el("span", "borrowed-from", `\u21B3 ${value.link_label}`));
      }
    } else {
      wrap.textContent = value;
    }
    return wrap;
  }

  /** Whether anything at all has happened to this entry. */
  rowHasHistory(rowData) {
    return Object.values(rowData.history_counts || {}).some((n) => n > 0);
  }

  chip(rowId, col, count) {
    const open = this.openCell?.rowId === rowId && this.openCell?.field === col.key;
    const button = el("button", `chip${open ? " on" : ""}`);
    button.innerHTML = `${icons.chip}<span>${count}</span>`;
    button.title = `${count} earlier ${count === 1 ? "value" : "values"} - click to replay`;
    button.onclick = (event) => {
      event.stopPropagation();
      this.openCell = open ? null : { rowId, field: col.key };
      this.render();
    };
    return button;
  }

  gripCell(group, rowData) {
    const cell = el("div", "cell grip-cell");
    cell.innerHTML = icons.grip;
    // Sorted by date, a drag would move a row somewhere it is not shown, so
    // your own order is only rearranged while you are looking at it.
    const sorted = this.order === "due";
    cell.draggable = this.scope === "active" && !sorted;
    cell.title = sorted ? "Sorted by date - switch to My order to drag" : "Drag to reorder";
    if (sorted) cell.classList.add("locked");
    cell.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", String(rowData.id));
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setDragImage(group, 20, 14);
      group.classList.add("dragging");
      this.dragging = rowData.id;
    });
    cell.addEventListener("dragend", () => {
      group.classList.remove("dragging");
      this.dragging = null;
    });
    return cell;
  }

  actionsCell(rowData) {
    const cell = el("div", "cell actions-cell");
    const buttons = this.scope === "archived"
      ? [["unarchive", icons.unarchive, "Unarchive"], ["delete", icons.trash, "Delete permanently"]]
      : [["archive", icons.archive, "Archive"], ["delete", icons.trash, "Delete permanently"]];

    /* The whole entry's story, which is usually the one worth reading. A chip
     * narrows the same panel to one cell.
     *
     * An entry with no past yet still takes the slot, invisibly: without that,
     * rows with and without history hold different numbers of icons and
     * nothing lines up down the column. */
    const told = this.rowHasHistory(rowData);
    const open = told && this.openCell?.rowId === rowData.id && !this.openCell?.field;
    const log = el("button", `icon log${open ? " on" : ""}${told ? "" : " vacant"}`);
    log.innerHTML = icons.log;
    if (told) {
      log.title = "Replay everything that has happened to this entry";
      log.onclick = () => {
        this.openCell = open ? null : { rowId: rowData.id, field: null };
        this.render();
      };
    } else {
      log.tabIndex = -1;
      log.setAttribute("aria-hidden", "true");
    }
    cell.append(log);

    for (const [action, glyph, label] of buttons) {
      const button = el("button", `icon${action === "delete" ? " trash" : ""}`);
      button.innerHTML = glyph;
      button.title = label;
      button.onclick = () => this.act(rowData.id, action, label);
      cell.append(button);
    }
    return cell;
  }

  newRow() {
    const row = el("div", "row new");
    row.append(el("div", "cell"));
    for (const col of this.columns) {
      if (col.type === "check") { row.append(el("div", "cell check-cell")); continue; }
      const cell = el("div", "cell editable");
      cell.dataset.row = "new";
      cell.dataset.field = col.key;
      cell.dataset.type = col.type;
      cell.append(el("div", "value", ""));
      row.append(cell);
    }
    row.append(el("div", "cell"));
    return row;
  }

  // --- editing --------------------------------------------------------------

  /** Every cell you can type into, in visual order -- the Tab order. */
  editableCells() {
    return [...this.table.querySelectorAll(".cell.editable")];
  }

  beginEdit(cell) {
    if (cell.querySelector("input, textarea")) return;
    // Anything that would redraw this table now waits until you are done.
    this.editing = cell;
    const col = this.columns.find((c) => c.key === cell.dataset.field);
    const rowId = cell.dataset.row;
    const current = rowId === "new"
      ? (col.type === "datenote" ? { date: "", note: "" } : "")
      : this.rows.find((r) => r.id === Number(rowId)).fields[col.key];

    const editor = buildEditor(col, current, this.hints?.[col.key],
                               this.links?.[col.key]);
    cell.innerHTML = "";
    cell.append(editor.node);
    editor.focus();

    let settled = false;
    const finish = async (save, move) => {
      if (settled) return;
      settled = true;
      this.editing = null;
      editor.destroy?.();          // take any open dropdown off the page
      // Resolve where focus is going before anything repaints and invalidates
      // the current DOM node.
      const next = move ? this.neighbour(cell, move) : null;
      let created = null;
      if (save) created = await this.commit(cell, col, editor.read());
      else this.repaint(cell, col);
      // Tabbing out of the new row commits it into a real entry, so carry
      // focus into that entry rather than the blank row that replaces it.
      if (next) this.focusCell(next.row === "new" && created ? created : next.row, next.field);
      // A background change that arrived mid-edit was held back; take it now,
      // unless the cursor has already moved into the next cell.
      if (this.stale && !this.editing) { this.stale = false; this.refresh(); }
    };

    // Enter and Tab both mean "done here, move on". A line break inside a cell
    // is the deliberate act, so it takes Alt+Enter.
    const navigate = (direction) => {
      if (editor.handleNav?.(direction)) return;
      finish(true, direction);
    };

    editor.node.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      } else if (event.key === "Tab") {
        event.preventDefault();
        navigate(event.shiftKey ? -1 : 1);
      } else if (event.key === "Enter" && (event.altKey || event.shiftKey)) {
        if (editor.insertNewline) { event.preventDefault(); editor.insertNewline(); }
      } else if (event.key === "Enter") {
        event.preventDefault();
        navigate(1);
      }
    });
    /* Commit only when focus has really left the cell.
     *
     * relatedTarget alone is not enough to decide that: a picker, a scrollbar
     * drag, or a window losing focus all report null while the cursor is still
     * in the cell. Committing then writes a revision for a half-typed value.
     * So the question is asked a tick later, of the document itself. */
    editor.node.addEventListener("focusout", () => {
      setTimeout(() => {
        if (settled) return;
        const active = editor.node.ownerDocument.activeElement;
        if (editor.node.contains(active)) return;
        if (active === editor.node.ownerDocument.body && !editor.node.isConnected) return;
        finish(true);
      }, 0);
    });
  }

  /** Identity of the next/previous editable cell, resolved before any repaint. */
  neighbour(cell, direction) {
    const cells = this.editableCells();
    const target = cells[cells.indexOf(cell) + direction];
    return target ? { row: target.dataset.row, field: target.dataset.field } : null;
  }

  focusCell(rowRef, field) {
    const cell = this.table?.querySelector(
      `.cell.editable[data-row="${rowRef}"][data-field="${field}"]`
    );
    if (cell) this.beginEdit(cell);
  }

  async commit(cell, col, value) {
    const rowId = cell.dataset.row;
    if (rowId === "new") {
      if (isBlank(col, value)) { this.repaint(cell, col); return null; }
      try {
        const { row } = await api.post(
          api.p(`/folders/${this.folderId}/rows/${this.entity}`),
          { field: col.key, value }
        );
        await this.refresh();
        this.announce();
        return row.id;
      } catch (error) { toast(error.message); return null; }
    }
    await this.save(Number(rowId), col, value, cell);
    return null;
  }

  async save(rowId, col, value, cell) {
    mark(cell, "saving");
    try {
      const result = await api.patch(
        api.p(`/rows/${this.entity}/${rowId}`), { field: col.key, value });
      const row = this.rows.find((r) => r.id === rowId);
      row.fields[col.key] = result.value;
      // A value that just appeared in a suggest column has to reach the other
      // rows' pick lists straight away, or the second use retypes it by hand.
      if (col.suggest) await this.loadHints();
      if (col.key === "done") { await this.refresh(); return; }
      row.history_counts[col.key] = result.history_count;
      // A new date can move the row. Not while you are still at work, though:
      // a row jumping away mid-Tab would take the cell you were heading for
      // with it. It takes its place once you are done, like any held refresh.
      if (col === this.dateColumn &&
          this.arrange(this.rows).some((r, i) => r !== this.rows[i])) {
        this.stale = true;
      }
      // Repaint just this cell so the rest of the table -- and any open
      // history panel -- stays exactly where it was.
      if (cell) this.repaint(cell, col);
      else this.render();
      mark(cell, "saved");
      this.announce();
    } catch (error) {
      toast(error.message);
      // The row still holds the old value, so repainting shows what is really
      // stored. The cell keeps a failed marker until it saves for real.
      if (cell) this.repaint(cell, col);
      mark(cell, "failed", error.message);
    }
  }

  repaint(cell, col) {
    const rowId = cell.dataset.row;
    cell.innerHTML = "";
    if (rowId === "new") { cell.append(el("div", "value", "")); return; }
    const row = this.rows.find((r) => r.id === Number(rowId));
    cell.append(this.display(row.fields[col.key], col));
    const count = row.history_counts?.[col.key] || 0;
    if (count > 0) cell.append(this.chip(row.id, col, count));
  }

  // --- row actions ----------------------------------------------------------

  async act(rowId, action, label) {
    if (action === "delete" &&
        !confirm("Delete this entry and its history permanently? This cannot be undone.")) return;
    try {
      await api.post(api.p(`/rows/${this.entity}/${rowId}/${action}`));
      if (this.openCell?.rowId === rowId) this.openCell = null;
      await this.refresh();
      // Archiving or deleting a deliverable changes what can be borrowed from.
      this.announce();
    } catch (error) { toast(`${label} failed: ${error.message}`); }
  }

  // --- drag to reorder ------------------------------------------------------

  wireDrag(group) {
    group.addEventListener("dragover", (event) => {
      if (this.dragging == null || Number(group.dataset.id) === this.dragging) return;
      event.preventDefault();
      group.classList.add("drop-target");
    });
    group.addEventListener("dragleave", () => group.classList.remove("drop-target"));
    group.addEventListener("drop", async (event) => {
      event.preventDefault();
      group.classList.remove("drop-target");
      const moved = this.dragging;
      const onto = Number(group.dataset.id);
      if (moved == null || moved === onto) return;
      // Dropping on a row places the dragged row directly after it, unless you
      // drop on the very first row while moving upward -- then it goes first.
      const order = this.rows.map((r) => r.id);
      const after = order.indexOf(moved) > order.indexOf(onto) && order[0] === onto
        ? null : onto;
      try {
        await api.post(api.p(`/rows/${this.entity}/${moved}/move`), { after_id: after });
        await this.refresh();
      } catch (error) { toast(error.message); }
    });
  }
}

// --- editors ----------------------------------------------------------------

/* A picker that is also a text box.
 *
 * Two things this must survive, both learned the hard way:
 *
 * A native <datalist> renders its popup outside this element, so opening it
 * fires focusout with no relatedTarget, the cell commits mid-edit, and the
 * correction you make afterwards lands in the history as a revision you never
 * meant. So the list is built here instead.
 *
 * But the list cannot live inside the cell either: the table clips its
 * contents, so a dropdown on a lower row is cut off and unreachable. It is
 * attached to the body and positioned to the input, which escapes every
 * clipping ancestor -- the table, and the repository canvas too.
 *
 * Focus never actually moves to the list (pointerdown is prevented), so the
 * cell still knows the cursor is in it. The typed value always wins; the list
 * is a shortcut, never the whole set. */
/* Every dropdown currently on the page. Because the list lives on the body
 * rather than in its cell, a repaint that removes the cell would otherwise
 * leave the list -- and its scroll listeners -- behind. */
const openCombos = new Set();

export function closeAllCombos(keepFocused = true) {
  const here = document.activeElement;
  for (const entry of [...openCombos]) {
    if (keepFocused && entry.holds?.(here)) continue;
    entry.close();
  }
}

function combo(current, options, placeholder) {
  const node = el("div", "combo");
  const field = Object.assign(document.createElement("input"),
                              { type: "text", value: current || "",
                                placeholder: placeholder || "",
                                autocomplete: "off" });
  node.append(field);

  const values = options ?? [];
  if (!values.length) {
    return { node, input: field, isOpen: () => false, close: () => {}, destroy: () => {} };
  }

  const toggle = el("button", "combo-open");
  toggle.type = "button";
  toggle.tabIndex = -1;                       // Tab belongs to the text box
  toggle.title = "Show the list";
  node.append(toggle);

  const list = el("ul", "combo-list");
  list.hidden = true;
  let open = false;

  const pick = (value) => (event) => {
    event.preventDefault();                   // keeps focus in the text box
    field.value = value;
    show(false);
    field.focus();
  };

  const draw = () => {
    const typed = field.value.trim().toLowerCase();
    const matching = values.filter((v) => v.toLowerCase().includes(typed));
    list.innerHTML = "";
    for (const value of (matching.length ? matching : values)) {
      const item = el("li");
      const button = el("button", value === field.value ? "on" : null, value);
      button.type = "button";
      button.tabIndex = -1;
      button.addEventListener("pointerdown", pick(value));
      item.append(button);
      list.append(item);
    }
    // Clearing is a choice too, and the only way back out of a wrong pick.
    if (field.value) {
      const item = el("li");
      const clear = el("button", "combo-clear", "Clear");
      clear.type = "button";
      clear.tabIndex = -1;
      clear.addEventListener("pointerdown", pick(""));
      item.append(clear);
      list.append(item);
    }
  };

  /* Anchored to the input in viewport coordinates, and flipped above it when
   * the last row of a table would otherwise push it off the screen. */
  const place = () => {
    const at = field.getBoundingClientRect();
    const view = field.ownerDocument.defaultView;
    list.style.left = `${at.left}px`;
    list.style.width = `${at.width}px`;
    const wanted = Math.min(list.scrollHeight || 160, 200);
    const below = view.innerHeight - at.bottom;
    if (below < wanted + 10 && at.top > below) {
      list.style.top = "auto";
      list.style.bottom = `${view.innerHeight - at.top + 3}px`;
    } else {
      list.style.bottom = "auto";
      list.style.top = `${at.bottom + 3}px`;
    }
  };

  const follow = () => { if (open) place(); };
  const handle = { close: () => show(false), holds: (node) => node === field };

  const show = (next) => {
    if (next === open) { if (open) place(); return; }
    open = next;
    node.classList.toggle("open", open);
    if (open) {
      draw();
      field.ownerDocument.body.append(list);
      list.hidden = false;
      place();
      // Scrolling the page must not leave the list behind the cursor.
      field.ownerDocument.addEventListener("scroll", follow, true);
      field.ownerDocument.defaultView.addEventListener("resize", follow);
      openCombos.add(handle);
    } else {
      list.hidden = true;
      list.remove();
      field.ownerDocument.removeEventListener("scroll", follow, true);
      field.ownerDocument.defaultView.removeEventListener("resize", follow);
      openCombos.delete(handle);
    }
  };

  toggle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    show(!open);
    field.focus();
  });
  field.addEventListener("input", () => { if (open) { draw(); place(); } });
  // Opened by asking -- a click, the arrow, or Down. Not merely by tabbing
  // through, which would pop a list open on every cell you pass.
  field.addEventListener("pointerdown", () => show(true));
  field.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" && !open) { event.preventDefault(); show(true); }
    else if (event.key === "Escape" && open) { event.stopPropagation(); show(false); }
  });

  return { node, input: field, isOpen: () => open, close: () => show(false),
           // The list is not a child of the cell, so tearing down the editor
           // has to take it away explicitly or it is left hanging on the page.
           destroy: () => show(false) };
}

/* Exported because the Review tab edits cells too. A suggestion you want to
 * accept with one word changed has to be editable the same way the cell itself
 * is -- same date picker, same Email/Chat/Call list, same deliverable dropdown
 * -- or "edit" means retyping the value in a plain box. */
export function buildEditor(col, value, hints, sources) {
  if (col.type === "datenote") {
    const node = el("div", "datenote-editor");
    const date = Object.assign(document.createElement("input"),
                               { type: "date", value: value.date || "" });
    node.append(date);

    // Three-part cells carry a "how" between the date and the free text.
    let via = null;
    let viaCombo = null;
    if (col.choices) {
      viaCombo = combo(value.via, col.choices, "how - or type your own");
      viaCombo.node.classList.add("via");
      node.append(viaCombo.node);
      via = viaCombo.input;
    }

    /* Where the date comes from: typed here, or borrowed from a deliverable.
     * One control, because these are not two modes -- the date has a source,
     * and "my own" is one of the choices. */
    let source = null;
    if (col.link) {
      source = document.createElement("select");
      source.className = "link-pick";
      source.append(option("", "Own date"));
      for (const item of sources ?? []) {
        source.append(option(String(item.id),
                             `${item.label}${item.date ? ` - ${fmt.absolute(item.date)}` : ""}`));
      }
      // With nothing to borrow from, say so rather than showing a lone entry
      // that looks like the feature is missing.
      if (!sources?.length) {
        const none = option("", "(no deliverables in this folder yet)");
        none.disabled = true;
        source.append(none);
      }
      source.value = value.link ? String(value.link) : "";
      const follow = () => {
        const picked = sources.find((i) => String(i.id) === source.value);
        // Unlinking keeps the inherited date as your own, so changing your mind
        // about where a date came from never loses the date.
        if (picked) date.value = picked.date || "";
        date.readOnly = !!picked;
        date.classList.toggle("borrowed", !!picked);
      };
      source.addEventListener("change", follow);
      follow();
      node.append(source);
    }

    const note = Object.assign(document.createElement("input"),
                               { type: "text", value: value.note || "",
                                 placeholder: col.placeholder || "" });
    node.append(note);

    const order = [date, via, source, note].filter(Boolean);
    return {
      node,
      focus: () => date.focus(),
      read: () => {
        const out = { date: date.value, note: note.value };
        if (via) out.via = via.value;
        if (col.link) out.link = source ? Number(source.value || 0) : (value.link || 0);
        return out;
      },
      destroy: () => viaCombo?.destroy(),
      // Several inputs in one cell: step through them before leaving the cell.
      handleNav: (direction) => {
        viaCombo?.close();
        const at = order.indexOf(node.ownerDocument.activeElement);
        const next = order[at + direction];
        if (at === -1 || !next) return false;
        next.focus();
        next.select?.();
        return true;
      },
    };
  }
  if (col.type === "date") {
    const node = Object.assign(document.createElement("input"),
                               { type: "date", value: value || "" });
    return { node, focus: () => node.focus(), read: () => node.value };
  }
  if (col.type === "longtext") {
    const node = document.createElement("textarea");
    node.value = value || "";
    const grow = () => { node.style.height = "auto"; node.style.height = `${node.scrollHeight}px`; };
    node.addEventListener("input", grow);
    return {
      node,
      focus: () => { node.focus(); node.select(); grow(); },
      read: () => node.value,
      // Enter moves on, so a line break has to be asked for: Alt+Enter.
      insertNewline: () => {
        const { selectionStart: from, selectionEnd: to, value: text } = node;
        node.value = `${text.slice(0, from)}\n${text.slice(to)}`;
        node.selectionStart = node.selectionEnd = from + 1;
        grow();
      },
    };
  }
  const built = combo(value, col.suggest ? hints : null);
  return {
    node: built.node,
    focus: () => { built.input.focus(); built.input.select(); },
    read: () => built.input.value,
    destroy: () => built.destroy(),
  };
}

/* One visible state per cell: in flight, written, or refused. "saved" clears
 * itself; "failed" does not, because a change that was refused should stay
 * visible until it is actually stored. */
function mark(cell, state, why) {
  if (!cell) return;
  cell.classList.remove("cell-saving", "cell-saved", "cell-failed");
  if (state === "saving") { cell.classList.add("cell-saving"); return; }
  if (state === "failed") {
    cell.classList.add("cell-failed");
    cell.title = why || "This change was not saved.";
    return;
  }
  cell.removeAttribute("title");
  cell.classList.add("cell-saved");
  clearTimeout(cell.savedTimer);
  cell.savedTimer = setTimeout(() => cell.classList.remove("cell-saved"), 1400);
}

function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
}

function isBlank(col, value) {
  if (col.type === "datenote") return !value.date && !value.note && !value.link;
  return !String(value).trim();
}

// --- tiny DOM helper --------------------------------------------------------

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
