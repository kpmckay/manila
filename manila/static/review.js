/* The Review tab: what an assistant has proposed, and what you decide.
 *
 * Nothing reaches a folder's tables through this file. A suggestion sits here
 * until you accept it, and accepting it sends one request that applies it the
 * way you would have typed it -- same write path, same history entry, same
 * chip on the cell. Rejecting leaves no trace in the folder at all.
 *
 * A batch is one sync run, read as a batch, because that is how it was made:
 * an assistant reads a morning of mail and says nine things about it, and
 * those nine things make sense together and are usually right or wrong
 * together. Hence Accept all -- with the stale ones held back, since those are
 * exactly the ones that deserve a second look.
 *
 * Two facts are shown on every card, and they are the ones that make the queue
 * worth reading rather than worth clicking through:
 *
 *   what the cell says NOW, beside what is proposed -- so the decision is a
 *   comparison rather than an act of faith -- shown inside the whole entry,
 *   so a change to one cell is read with the rest of the row around it; and
 *
 *   the mail or chat it rests on -- so a suggestion you disagree with can be
 *   traced back to the sentence that produced it.
 *
 * The rest of the entry is editable from the card as well. A suggestion about
 * one cell is often what reminds you another is wrong, and that edit is yours,
 * not the suggestion's: it is saved straight away, the same write the table
 * makes, and stays whether the suggestion is then accepted or rejected.
 */

import { api, toast } from "./api.js";
import * as fmt from "./format.js";
import * as icons from "./icons.js";
import { buildEditor, closeAllCombos, el } from "./table.js";

const SCOPES = [["pending", "Waiting"], ["decided", "Decided"], ["all", "All"]];

const KIND_WORDS = {
  update: "Change",
  create: "Add",
  archive: "Archive",
  tick: "Tick off",
};

const ENTITY_WORDS = {
  action_item: "action",
  milestone: "deliverable",
  contact: "contact",
};

export class Review {
  constructor(host, { folderId, columns, onDecided }) {
    Object.assign(this, { host, folderId, columns, onDecided });
    this.scope = "pending";
    this.batches = [];
    this.pending = 0;
    this.editing = null;     // id of the suggestion with an open editor
    this.links = [];         // deliverables, for a borrowed date
    this.hints = {};         // what has been typed into the suggest columns
  }

  async load() {
    const path = api.p(`/folders/${this.folderId}/suggestions?scope=${this.scope}`);
    const payload = await api.get(path);
    this.batches = payload.batches ?? [];
    this.pending = payload.pending ?? 0;
    await this.loadHints();
  }

  /* The same pick lists the table itself offers, so editing a suggestion is
   * editing a cell rather than retyping one. Failing to fetch them costs a
   * dropdown, never a decision, so failures stay quiet. */
  async loadHints() {
    // Which columns offer a list, and which borrow a date, is the schema's to
    // say -- the same question the table asks, asked the same way.
    const suggests = [];
    const borrows = [];
    for (const [entity, columns] of Object.entries(this.columns)) {
      for (const col of columns) {
        if (col.suggest) suggests.push([entity, col.key]);
        if (col.link) borrows.push([entity, col.key]);
      }
    }
    this.hints = {};
    this.links = [];
    await Promise.all([
      ...suggests.map(async ([entity, key]) => {
        try {
          const { values } = await api.get(
            api.p(`/folders/${this.folderId}/suggest/${entity}/${key}`));
          this.hints[`${entity}.${key}`] = values ?? [];
        } catch { this.hints[`${entity}.${key}`] = []; }
      }),
      /* Every borrowing column in Manila borrows from the same place -- the
       * folder's deliverables -- so one list serves them all. Asked per column
       * anyway, so that stops being true the day the schema says otherwise
       * rather than the day someone notices. */
      ...borrows.map(async ([entity, key]) => {
        try {
          const { options } = await api.get(
            api.p(`/folders/${this.folderId}/links/${entity}/${key}`));
          this.links = [...this.links, ...(options ?? [])];
        } catch { /* a missing pick list costs a dropdown, never a decision */ }
      }),
    ]);
  }

  async refresh() {
    await this.load();
    this.render();
    this.onDecided?.();
  }

  column(entity, field) {
    return (this.columns[entity] ?? []).find((c) => c.key === field) || null;
  }

  // --- rendering -------------------------------------------------------------

  render() {
    closeAllCombos(false);
    this.host.innerHTML = "";
    const section = el("section", "section review");

    const head = el("div", "section-head");
    head.append(el("div", "section-title", "Review"));
    const scopes = el("div", "scopes");
    for (const [key, label] of SCOPES) {
      const button = el("button", `scope${this.scope === key ? " on" : ""}`, label);
      button.onclick = () => { this.scope = key; this.editing = null; this.refresh(); };
      scopes.append(button);
    }
    head.append(scopes, el("span", "count", `${this.pending}`));
    section.append(head);

    if (!this.batches.length) {
      section.append(this.nothing());
      this.host.append(section);
      return;
    }
    for (const batch of this.batches) section.append(this.batchCard(batch));
    this.host.append(section);
  }

  /* An empty queue is the normal state, not a failure -- so it says what would
   * fill it rather than apologising for being empty. */
  nothing() {
    const box = el("div", "review-empty");
    box.append(el("p", null, this.scope === "pending"
      ? "Nothing is waiting to be reviewed."
      : "Nothing has been decided in this folder yet."));
    box.append(el("p", "hint",
      "Suggestions appear here when an assistant with the Manila MCP server "
      + "reads your mail or chats and finds something this folder does not know. "
      + "Nothing it proposes changes anything until you accept it."));
    return box;
  }

  batchCard(batch) {
    const card = el("div", "batch");
    const head = el("div", "batch-head");

    const title = el("div", "batch-title");
    title.append(el("span", "batch-source", batch.source || "Suggestions"));
    if (batch.summary) title.append(el("span", "batch-summary", batch.summary));
    head.append(title);

    const when = el("span", "batch-when", fmt.stamp(batch.created_at));
    when.title = `${batch.agent || "Proposed"} - `
      + `${new Date(batch.created_at).toLocaleString()}`;
    head.append(when, el("span", "spacer"));

    if (batch.pending > 0) {
      const all = el("button", "ghost", `Accept all ${batch.pending}`);
      all.title = "Accept everything still waiting in this run. "
        + "Anything overtaken by your own edits is left for you to look at.";
      all.onclick = () => this.decideBatch(batch, "accept");
      const none = el("button", "ghost", "Reject all");
      none.onclick = () => this.decideBatch(batch, "reject");
      head.append(all, none);
    } else {
      const clear = el("button", "ghost", "Clear");
      clear.title = "Remove this reviewed run from the list. "
        + "What you accepted stays in the folder.";
      clear.onclick = () => this.discard(batch);
      head.append(clear);
    }
    card.append(head);

    for (const group of byEntry(batch.suggestions)) {
      card.append(group.length > 1 ? this.entryCard(group) : this.card(group[0]));
    }
    return card;
  }

  /* Everything one run proposed for one entry, as one card.
   *
   * A sync reads a thread and proposes each cell it touches as its own
   * suggestion -- which is right for storage and for history, and wrong for
   * reading: the same entry shown three times, whole each time. Here it is
   * shown once, every proposed change marked in place and the rest of the
   * entry around them, and decided with one action. */
  entryCard(group) {
    const key = `entry:${group[0].entity}:${group[0].entity_id}:${group[0].batch_id}`;
    const pending = group.filter((s) => s.state === "pending");
    const context = group.find((s) => s.row) || group[0];
    const entry = {
      entity: context.entity, entity_id: context.entity_id, row: context.row,
      state: pending.length ? "pending" : "decided",
      gone: !context.row,
    };
    const updates = group.filter((s) => s.kind === "update");
    const others = group.filter((s) => s.kind !== "update");
    const stale = pending.some((s) => s.stale);
    const editing = this.editing === key;
    const editors = new Map();

    // One source for the whole card is the usual case; then it is said once,
    // at the foot, rather than under every cell.
    const sources = new Set(group.map((s) => s.evidence).filter(Boolean));
    const shared = sources.size <= 1;

    const card = el("div", `suggestion entry ${entry.state}`);
    if (stale) card.classList.add("stale");

    const head = el("div", "suggestion-head");
    // Named in the order the entry itself reads, so the head and the list
    // below it agree about which cell comes first.
    const order = (this.columns[entry.entity] ?? []).map((c) => c.key);
    const labels = [...new Set(updates
      .slice()
      .sort((a, b) => order.indexOf(a.field) - order.indexOf(b.field))
      .map((s) => this.column(s.entity, s.field)?.label || s.field))];
    for (const s of others) labels.push(KIND_WORDS[s.kind]);
    head.append(el("span", "what", labels.join(", ")));
    if (context.title) head.append(el("span", "about", context.title));
    if (stale) head.append(el("span", "flag", "changed since"));
    card.append(head);

    const body = el("div", "suggestion-body");
    for (const s of others) {
      const line = el("div", "plain-proposal", s.kind === "archive"
        ? "Archive this entry. It can be brought back from the Archived pill."
        : "Tick this entry as done.");
      body.append(line, this.changeNotes(s, { shared, drop: pending.length > 1 }));
    }

    const fill = (col, dd) => {
      const mine = updates.filter((s) => s.field === col.key);
      if (!mine.length) return false;
      const live = mine.filter((s) => s.state === "pending");
      const latest = live.at(-1);
      dd.append(this.side("now", this.readable(mine.at(-1), mine.at(-1).now)));
      for (const s of mine) {
        const change = el("div", "change");
        if (editing && s === latest) {
          const built = buildEditor(col, this.blank(col, s.value),
                                   this.hints[`${s.entity}.${col.key}`], this.links);
          change.append(built.node);
          editors.set(s.id, built);
        } else {
          const line = this.side("proposed", this.readable(s, s.value), true);
          // An earlier proposal for a cell a later one also proposes: shown,
          // but it is the later one that accepting applies.
          if (s.state === "pending" && s !== latest) {
            line.classList.add("superseded");
            line.title = "A later suggestion in this run replaces this one";
          }
          change.append(line);
        }
        change.append(this.changeNotes(s, { shared, drop: pending.length > 1 }));
        dd.append(change);
      }
      return true;
    };
    if (entry.row) {
      body.append(this.rowList(entry, fill));
    } else {
      const list = el("dl", "fields");
      for (const s of updates) {
        const col = this.column(s.entity, s.field);
        list.append(el("dt", "changed", col?.label || s.field));
        const dd = el("dd", "changed");
        fill(col || { key: s.field }, dd);
        list.append(dd);
      }
      body.append(list);
    }
    card.append(body);

    if (shared && sources.size) {
      const from = el("div", "evidence");
      from.append(el("span", "quote", [...sources][0]));
      card.append(from);
    }
    if (stale) {
      card.append(el("div", "warn-line",
        "You have changed a marked cell since this was suggested. Accepting "
        + "replaces what is there now."));
    }
    if (entry.gone && pending.length) {
      card.append(el("div", "warn-line", "The entry this was about is no longer here."));
    }

    if (editing) {
      const actions = el("div", "editor-actions entry-editor-actions");
      const save = el("button", "primary", "Accept with these changes");
      save.onclick = () => {
        // Only what was actually retyped. A value sent back unchanged is still
        // recorded as an edit, and the card would then say it was edited from
        // itself.
        const values = Object.fromEntries([...editors]
          .map(([id, built]) => [id, built.read()])
          .filter(([id, read]) => JSON.stringify(read)
            !== JSON.stringify(pending.find((s) => s.id === id)?.value)));
        for (const built of editors.values()) built.destroy?.();
        this.decideMany(pending, "accept", values);
      };
      const cancel = el("button", "ghost", "Cancel");
      cancel.onclick = () => {
        for (const built of editors.values()) built.destroy?.();
        this.editing = null;
        this.render();
      };
      actions.append(save, cancel);
      card.append(actions);
      setTimeout(() => editors.values().next().value?.focus());
    }

    if (pending.length) {
      card.append(this.entryControls(key, pending, entry.gone));
    } else {
      const last = group.map((s) => s.decided_at).filter(Boolean).sort().at(-1);
      card.append(el("div", "decided", `decided ${fmt.stamp(last)}`));
    }
    return card;
  }

  /* What sits under one change on an entry card: why, from where if the card
   * has more than one source, what it was edited from, and -- when there is
   * more than one change waiting -- a way to turn down just this one. */
  changeNotes(s, { shared, drop }) {
    const notes = el("div", "change-notes");
    if (!shared && s.evidence) notes.append(el("span", "quote", s.evidence));
    if (s.reason) notes.append(el("span", "reason", s.reason));
    if (s.proposed !== null && s.proposed !== undefined) {
      notes.append(el("span", "reason edited",
                      `edited from: ${this.readable(s, s.proposed)}`));
    }
    if (s.stale) notes.append(el("span", "flag", "changed since"));
    if (s.state !== "pending") {
      notes.append(el("span", "change-state", s.state));
    } else if (drop) {
      const reject = el("button", "change-drop", "×");
      reject.title = "Reject just this change";
      reject.onclick = () => this.decide(s, "reject");
      notes.append(reject);
    }
    return notes;
  }

  entryControls(key, pending, gone) {
    const bar = el("div", "suggestion-actions");
    const accept = el("button", "icon accept");
    accept.innerHTML = icons.tick;
    accept.title = pending.some((s) => s.stale)
      ? "Accept every change here, replacing what the cells say now"
      : "Accept every change here, as edits you made";
    accept.onclick = () => this.decideMany(pending, "accept");
    if (gone) accept.disabled = true;

    const edit = el("button", "icon");
    edit.innerHTML = icons.pencil;
    edit.title = "Change these before accepting them";
    edit.onclick = () => {
      this.editing = this.editing === key ? null : key;
      this.render();
    };
    if (gone || !pending.some((s) => s.kind === "update")) edit.disabled = true;

    const reject = el("button", "icon reject");
    reject.innerHTML = icons.cross;
    reject.title = "Reject every change here: nothing is written, and none is "
      + "proposed again";
    reject.onclick = () => this.decideMany(pending, "reject");

    bar.append(accept, edit, reject);
    return bar;
  }

  card(item) {
    const card = el("div", `suggestion ${item.state}`);
    if (item.stale) card.classList.add("stale");
    if (item.gone) card.classList.add("gone");

    card.append(this.cardHead(item));
    card.append(this.body(item));

    if (item.evidence) {
      const from = el("div", "evidence");
      from.append(el("span", "quote", item.evidence));
      card.append(from);
    }
    if (item.reason) card.append(el("div", "reason", item.reason));

    if (item.stale) {
      card.append(el("div", "warn-line",
        "You have changed this cell since this was suggested. Accepting replaces "
        + "what is there now."));
    }
    if (item.gone && item.state === "pending") {
      card.append(el("div", "warn-line", item.kind === "update"
        ? "The entry this was about is no longer here."
        : "That has already been done."));
    }
    if (item.proposed !== null && item.proposed !== undefined) {
      const was = el("div", "reason edited");
      was.textContent = `edited from: ${this.readable(item, item.proposed)}`;
      card.append(was);
    }

    if (item.state === "pending") card.append(this.controls(item));
    else card.append(el("div", "decided", `${item.state} ${fmt.stamp(item.decided_at)}`));
    return card;
  }

  cardHead(item) {
    const head = el("div", "suggestion-head");
    const col = item.kind === "update" ? this.column(item.entity, item.field) : null;
    const what = item.kind === "update"
      ? (col?.label || item.field)
      : `${KIND_WORDS[item.kind]} ${ENTITY_WORDS[item.entity] || item.entity}`;
    head.append(el("span", "what", what));
    if (item.title) head.append(el("span", "about", item.title));
    if (item.stale) head.append(el("span", "flag", "changed since"));
    return head;
  }

  /* Now beside proposed, in the same shape the cell renders them, so the
   * comparison is the one you would make looking at the table. */
  body(item) {
    const body = el("div", "suggestion-body");
    if (item.kind === "update") {
      const change = (dd) => {
        dd.append(this.side("now", this.readable(item, item.now)));
        if (this.editing === item.id) dd.append(this.editor(item, { labels: false }));
        else dd.append(this.side("proposed", this.readable(item, item.value), true));
      };
      if (item.row) {
        body.append(this.rowList(item, (col, dd) => {
          if (col.key !== item.field) return false;
          change(dd);
          return true;
        }));
      } else {
        change(body);    // the entry is gone; the cell is all there is to show
      }
      return body;
    }
    if (item.kind === "create") {
      if (this.editing === item.id) { body.append(this.editor(item)); return body; }
      const list = el("dl", "fields");
      for (const [key, value] of Object.entries(item.fields || {})) {
        const col = this.column(item.entity, key);
        list.append(el("dt", null, col?.label || key));
        list.append(el("dd", null, this.readableValue(col, value)));
      }
      body.append(list);
      return body;
    }
    body.append(el("div", "plain-proposal", item.kind === "archive"
      ? "Archive this entry. It can be brought back from the Archived pill."
      : "Tick this entry as done."));
    if (item.row) body.append(this.rowList(item, () => false));
    return body;
  }

  /* The entry a suggestion is about, every column in the table's order. `fill`
   * takes over the cell it claims -- the one being changed -- and the rest
   * read as the table would show them, and open for editing on a click while
   * the suggestion is still waiting. */
  rowList(item, fill) {
    const list = el("dl", "fields");
    const open = item.state === "pending" && !item.gone;
    for (const col of this.columns[item.entity] ?? []) {
      const dt = el("dt", null, col.label || col.key);
      const dd = el("dd");
      if (fill(col, dd)) {
        dt.classList.add("changed");
        dd.classList.add("changed");
      } else {
        this.paintCell(item, col, dd);
        if (open) {
          dd.classList.add("editable");
          dd.title = `Change ${col.label || col.key} -- saved as your own edit`;
          dd.onclick = () => this.cellEditor(item, col, dd);
        }
      }
      list.append(dt, dd);
    }
    return list;
  }

  paintCell(item, col, dd) {
    const text = this.readableValue(col, item.row[col.key]);
    dd.textContent = text;
    dd.classList.toggle("empty", text === "(empty)");
  }

  /* One of the entry's other cells, opened in place with the table's own
   * editor. Saving writes it to the entry now -- it does not ride along with
   * the suggestion -- and then rereads the queue, since the change can make
   * another suggestion about the same cell stale. */
  cellEditor(item, col, dd) {
    if (dd.editing) return;          // clicks inside the editor land here too
    dd.editing = true;
    dd.innerHTML = "";
    dd.classList.remove("empty");

    const current = item.row[col.key];
    const built = col.type === "check"
      ? checkEditor(current)
      : buildEditor(col, this.blank(col, current),
                    this.hints[`${item.entity}.${col.key}`], this.links);
    const box = el("div", "cell-editor");
    box.append(built.node);

    const close = () => {
      built.destroy?.();
      dd.editing = false;
      dd.innerHTML = "";
      this.paintCell(item, col, dd);
    };
    const save = async () => {
      const value = built.read();
      built.destroy?.();
      try {
        await api.patch(
          api.p(`/rows/${item.entity}/${item.entity_id}`), { field: col.key, value });
        await this.refresh();
      } catch (error) {
        toast(error.message);
        close();
      }
    };

    const actions = el("div", "editor-actions");
    const ok = el("button", "primary", "Save");
    ok.onclick = save;
    const cancel = el("button", "ghost", "Cancel");
    cancel.onclick = close;
    actions.append(ok, cancel);
    box.append(actions);

    // Enter saves and Escape backs out, as in the table -- except in a long
    // text, where Enter is how a line is ended.
    box.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.stopPropagation(); close(); }
      if (event.key === "Enter" && col.type !== "longtext") {
        event.preventDefault();
        save();
      }
    });

    dd.append(box);
    setTimeout(() => built.focus?.());
  }

  side(label, text, strong = false) {
    const line = el("div", `side${strong ? " proposed" : ""}`);
    line.append(el("span", "side-label", label));
    line.append(el("span", "side-value", text));
    return line;
  }

  readable(item, value) {
    if (item.kind === "create") {
      return Object.entries(value || {})
        .map(([key, held]) =>
          `${this.column(item.entity, key)?.label || key}: `
          + this.readableValue(this.column(item.entity, key), held))
        .join(" · ");
    }
    return this.readableValue(this.column(item.entity, item.field), value);
  }

  /* The same words the cell would show. A borrowed date names its deliverable,
   * which is read from the folder's own list rather than sent with the
   * suggestion -- the link is live, so the label has to be too. */
  readableValue(col, value) {
    if (value === null || value === undefined) return "(empty)";
    if (!col) return typeof value === "string" ? value : JSON.stringify(value);
    if (col.type === "datenote") {
      const parts = [fmt.absolute(value.date), value.via, value.note];
      const shown = parts.filter(Boolean).join(" · ") || "(empty)";
      const source = value.link
        && this.links.find((o) => String(o.id) === String(value.link));
      return source ? `${shown}${fmt.LINK_MARK}${source.label}` : shown;
    }
    if (col.type === "date") return fmt.absolute(value) || "(empty)";
    if (col.type === "check") return value ? "done" : "not done";
    return String(value || "") || "(empty)";
  }

  // --- editing before accepting ---------------------------------------------

  /* A suggestion that is right except for one word is the common case, and
   * retyping the whole value to fix it is how you end up rejecting it instead.
   * These are the table's own editors, so a date is a date picker and a
   * deadline can still be pointed at a deliverable. */
  editor(item, { labels = true } = {}) {
    const box = el("div", "suggestion-editor");
    const fields = item.kind === "create"
      ? Object.keys(item.fields || {})
      : [item.field];
    const editors = new Map();

    for (const key of fields) {
      const col = this.column(item.entity, key);
      if (!col) continue;
      const value = item.kind === "create" ? item.fields[key] : item.value;
      const line = el("div", "editor-line");
      // Inside the entry's own list the cell is already named beside it.
      if (labels) line.append(el("label", "editor-label", col.label || key));
      const built = buildEditor(col, this.blank(col, value),
                               this.hints[`${item.entity}.${key}`], this.links);
      line.append(built.node);
      box.append(line);
      editors.set(key, built);
    }

    const actions = el("div", "editor-actions");
    const save = el("button", "primary", "Accept with these changes");
    save.onclick = () => {
      const read = item.kind === "create"
        ? Object.fromEntries([...editors].map(([key, built]) => [key, built.read()]))
        : editors.get(item.field)?.read();
      for (const built of editors.values()) built.destroy?.();
      this.decide(item, "accept", read);
    };
    const cancel = el("button", "ghost", "Cancel");
    cancel.onclick = () => {
      for (const built of editors.values()) built.destroy?.();
      this.editing = null;
      this.render();
    };
    actions.append(save, cancel);
    box.append(actions);

    setTimeout(() => editors.values().next().value?.focus());
    return box;
  }

  /* The editors expect a cell's shape, and a composite cell is an object even
   * when the suggestion only filled part of it. */
  blank(col, value) {
    if (col.type !== "datenote") return value ?? "";
    const given = value && typeof value === "object" ? value : {};
    return { date: given.date || "", via: given.via || "",
             note: given.note || "", link: given.link || 0 };
  }

  controls(item) {
    const bar = el("div", "suggestion-actions");
    const accept = el("button", "icon accept");
    accept.innerHTML = icons.tick;
    accept.title = item.stale
      ? "Accept anyway, replacing what the cell says now"
      : "Accept: apply this as an edit you made";
    accept.onclick = () => this.decide(item, "accept");
    if (item.gone) accept.disabled = true;

    const edit = el("button", "icon");
    edit.innerHTML = icons.pencil;
    edit.title = "Change this before accepting it";
    edit.onclick = () => {
      this.editing = this.editing === item.id ? null : item.id;
      this.render();
    };
    if (item.gone || !["update", "create"].includes(item.kind)) edit.disabled = true;

    const reject = el("button", "icon reject");
    reject.innerHTML = icons.cross;
    reject.title = "Reject: nothing is written, and it is not proposed again";
    reject.onclick = () => this.decide(item, "reject");

    bar.append(accept, edit, reject);
    return bar;
  }

  // --- deciding --------------------------------------------------------------

  async decide(item, verdict, value) {
    try {
      const body = value === undefined ? {} : { value };
      await api.post(api.p(`/suggestions/${item.id}/${verdict}`), body);
      this.editing = null;
      await this.refresh();
    } catch (error) { toast(error.message); }
  }

  async decideMany(items, verdict, values) {
    try {
      const result = await api.post(api.p(`/suggestions/${verdict}`),
                                    { ids: items.map((s) => s.id), values: values ?? {} });
      this.editing = null;
      await this.refresh();
      if (result.held?.length) {
        toast(`${result.decided} decided. ${result.held.length} could not be: `
              + result.held.map((h) => h.why).join("; "));
      }
    } catch (error) { toast(error.message); }
  }

  async decideBatch(batch, verdict) {
    const what = verdict === "accept"
      ? `Accept all ${batch.pending} suggestion(s) in this run?`
      : `Reject all ${batch.pending} suggestion(s) in this run?\n\n`
        + "Nothing is written, and they are not proposed again.";
    if (!confirm(what)) return;
    try {
      const result = await api.post(api.p(`/batches/${batch.id}/${verdict}`));
      this.editing = null;
      await this.refresh();
      if (result.held?.length) {
        toast(`${result.decided} applied. ${result.held.length} left for you to `
              + "look at: the cell changed since it was suggested.");
      }
    } catch (error) { toast(error.message); }
  }

  async discard(batch) {
    try {
      await api.post(api.p(`/batches/${batch.id}/delete`));
      await this.refresh();
    } catch (error) { toast(error.message); }
  }
}

/** A run's suggestions, gathered per entry, in the order each entry first
 * appears. A proposed new entry is always its own. */
export function byEntry(suggestions) {
  const groups = new Map();
  for (const s of suggestions) {
    const key = s.kind === "create" ? `new:${s.id}` : `${s.entity}:${s.entity_id}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  return [...groups.values()];
}

/* Done is a tick box in the table, not something typed, so it gets one here. */
function checkEditor(value) {
  const node = document.createElement("input");
  node.type = "checkbox";
  node.checked = Boolean(value);
  return { node, focus: () => node.focus(), read: () => node.checked };
}
