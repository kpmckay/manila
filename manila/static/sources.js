/* Sources: where this folder's news arrives.
 *
 * Pointers and nothing else -- a team and channel id, a chat id, an address, a
 * domain. Not keywords. An id changes about twice a year and tells you when it
 * has, because the sync comes back empty; a keyword list goes stale quietly and
 * keeps working on last quarter's vocabulary. The words worth searching for are
 * already in the folder's own Items and Waiting On cells, kept current by being
 * used, so that is where an assistant reads them from.
 *
 * It sits above the queue because it is the other half of the same question:
 * this says where to look, and the queue below says what came back.
 */

import { api, toast } from "./api.js";
import * as fmt from "./format.js";
import * as icons from "./icons.js";
import { el } from "./table.js";

export class Sources {
  constructor(host, { folderId, kinds }) {
    Object.assign(this, { host, folderId, kinds });
    this.sources = [];
    this.runs = [];
    this.adding = null;      // the kind being added, while the row is open
  }

  /* Remembered if you have ever said, and otherwise open exactly when the list
   * is empty. A folder with nothing listed is one where the panel has
   * something to teach; a folder already set up wants its queue at the top of
   * the screen, not a list of ids it will not touch again this quarter. */
  get open() {
    const choice = localStorage.getItem("manila.sources");
    if (choice === "open") return true;
    if (choice === "shut") return false;
    return this.sources.length === 0;
  }

  path(rest = "") {
    return api.p(`/folders/${this.folderId}/sources${rest}`);
  }

  async load() {
    const payload = await api.get(this.path());
    this.sources = payload.sources ?? [];
    this.runs = payload.last_runs ?? [];
  }

  async refresh() {
    await this.load();
    this.render();
  }

  spec(kind) {
    return this.kinds[kind] ?? { label: kind, ref: "id" };
  }

  // --- rendering ---------------------------------------------------------------

  render() {
    this.host.innerHTML = "";
    const section = el("section", "section sources");

    const head = el("div", "section-head");
    const shown = this.open;
    const toggle = el("button", "sources-toggle");
    toggle.append(el("span", "twist", shown ? "▾" : "▸"),
                  el("span", "section-title", "Sources"));
    toggle.onclick = () => {
      localStorage.setItem("manila.sources", shown ? "shut" : "open");
      this.render();
    };
    head.append(toggle);
    if (!shown) head.append(el("span", "sources-line", this.summary()));
    section.append(head);

    if (shown) section.append(this.panel());
    this.host.append(section);
  }

  /* Shut, it still has to be worth glancing at: what this folder listens to,
   * and whether anything has actually been read from it. */
  summary() {
    if (!this.sources.length) return "none set";
    const counts = new Map();
    for (const source of this.sources) {
      counts.set(source.kind, (counts.get(source.kind) ?? 0) + 1);
    }
    const parts = [...counts].map(([kind, n]) =>
      `${n} ${this.spec(kind).label}${n === 1 ? "" : "s"}`);
    return `${parts.join(", ")} · ${this.read()}`;
  }

  read() {
    if (!this.runs.length) return "never read";
    return this.runs.map((run) => {
      const point = run.covered_through || run.last_run;
      const word = run.covered_through ? "read to" : "last run";
      return `${run.source} ${word} ${fmt.stamp(point)}`;
    }).join(" · ");
  }

  panel() {
    const box = el("div", "sources-box");
    box.append(el("p", "hint",
      "Where an assistant should start looking for this folder's news: Teams "
      + "channels and chats by id, mail by the folder you file it in, or by "
      + "address or domain. Pointers only -- what "
      + "to search for is read off this folder's own items, which stay current "
      + "because you use them. A starting point, not a fence: internal mail and "
      + "places nobody has listed are still read, which is how a task this "
      + "folder does not mention yet reaches the queue at all."));

    if (this.sources.length) {
      const list = el("div", "source-list");
      for (const source of this.sources) list.append(this.row(source));
      box.append(list);
    }

    box.append(this.adding ? this.addCard() : this.addBar());
    box.append(this.coverage());
    return box;
  }

  row(source) {
    const spec = this.spec(source.kind);
    const row = el("div", "source-row");
    row.append(el("span", "source-kind", spec.label));
    row.append(this.field(source, "name", "what you call it", "source-name"));
    const ref = this.field(source, "ref", spec.ref, "source-ref");
    row.append(ref);
    if (spec.within) {
      row.append(this.field(source, "within", spec.within, "source-within"));
    } else {
      ref.classList.add("whole");
    }

    const drop = el("button", "icon trash");
    drop.innerHTML = icons.trash;
    drop.title = "Stop listening to this one";
    drop.setAttribute("aria-label", "Remove this source");
    drop.onclick = async () => {
      const what = source.name || source.ref;
      if (!confirm(`Stop reading ${what} into this folder?`)) return;
      try {
        await api.post(api.p(`/sources/${source.id}/delete`));
        await this.refresh();
      } catch (error) { toast(error.message); }
    };
    row.append(drop);
    return row;
  }

  /* Saved when you leave the box, not as you type: these are ids pasted in one
   * go, and a PATCH per keystroke would send a dozen half-ids. */
  field(source, key, placeholder, className) {
    const input = el("input", className);
    input.value = source[key] ?? "";
    input.placeholder = placeholder;
    input.spellcheck = false;
    input.onchange = async () => {
      const value = input.value.trim();
      if (value === (source[key] ?? "")) return;
      try {
        const { source: saved } = await api.patch(
          api.p(`/sources/${source.id}`), { [key]: value });
        Object.assign(source, saved);
        input.value = saved[key];
      } catch (error) {
        toast(error.message);
        input.value = source[key] ?? "";     // put back what is actually stored
      }
    };
    return input;
  }

  addBar() {
    const bar = el("div", "source-add");
    for (const [kind, spec] of Object.entries(this.kinds)) {
      const button = el("button", "ghost", `Add ${spec.label}`);
      button.onclick = () => {
        this.adding = { kind, ref: "" };
        this.render();
      };
      bar.append(button);
    }
    return bar;
  }

  /* One box, because there is one thing to paste. A channel link carries the
   * channel id, the team that holds it and the channel's name, and asking for
   * those three separately is asking someone to do a URL decode by hand.
   *
   * It is a card rather than a row in the list above: the list is five columns
   * wide and this is a question with an explanation and two answers, which is
   * not the same shape. The previous version tried to be a row, overflowed the
   * grid, and put Cancel somewhere you could not see it. */
  addCard() {
    const draft = this.adding;
    const spec = this.spec(draft.kind);
    const card = el("div", "source-new");
    card.append(el("div", "source-new-kind", spec.label));

    const input = el("input", "source-new-ref");
    input.placeholder = spec.add ?? spec.ref;
    input.spellcheck = false;
    input.oninput = () => { draft.ref = input.value; };
    input.onkeydown = (event) => {
      if (event.key === "Enter") save();
      if (event.key === "Escape") this.stopAdding();
    };
    card.append(input);
    if (spec.help) card.append(el("p", "hint", spec.help));

    const save = async () => {
      if (!(draft.ref ?? "").trim()) return input.focus();
      try {
        await api.post(this.path(), { kind: draft.kind, ref: draft.ref });
        this.adding = null;
        await this.refresh();
      } catch (error) { toast(error.message); }
    };

    const actions = el("div", "source-new-actions");
    const add = el("button", "primary", "Add");
    add.onclick = save;
    const cancel = el("button", "ghost", "Cancel");
    cancel.onclick = () => this.stopAdding();
    actions.append(add, cancel);
    card.append(actions);

    Promise.resolve().then(() => input.focus?.());
    return card;
  }

  stopAdding() {
    this.adding = null;
    this.render();
  }

  /* How far each source has actually been read, which is the fact that decides
   * how far back the next run goes. A source with no run at all is the one
   * worth seeing: a first pass over it has no floor, so it will surface a
   * backlog rather than a morning unless it is given a window. */
  coverage() {
    const box = el("div", "source-coverage");
    if (!this.runs.length) {
      box.append(el("span", "muted", "Nothing has been read into this folder yet."));
      return box;
    }
    for (const run of this.runs) {
      const line = el("div", "coverage-run");
      line.append(el("span", "coverage-source", run.source));
      if (run.covered_through) {
        line.append(el("span", "coverage-point",
          `read through ${fmt.stamp(run.covered_through)}`));
      } else {
        line.append(el("span", "coverage-point muted", "no watermark"));
      }
      const when = el("span", "coverage-when",
        `${run.runs} run${run.runs === 1 ? "" : "s"}, last ${fmt.stamp(run.last_run)}`);
      line.append(when);
      box.append(line);
    }
    return box;
  }
}
