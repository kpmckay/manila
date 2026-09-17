/* The history panel.
 *
 * One log, two readings. By default it shows the whole entry -- what happened
 * to this action item, in order -- because that is how work actually happens:
 * you chase someone, so Last Touched moves and Current State is rewritten in
 * the same breath, and those belong on one page. Narrowing to a single cell
 * answers the sharper question, "how has this date moved", and is a filter on
 * the same log rather than a different one.
 *
 * It expands in place, under the entry it belongs to, so the table above never
 * moves. Newest first: reading downward is reading backward in time. */

import { api, toast } from "./api.js";
import * as fmt from "./format.js";
import { el } from "./table.js";

export async function renderHistory(panel, options) {
  const { entity, row, columns, field, onSelect, onChange } = options;
  const column = field ? columns.find((c) => c.key === field) : null;

  panel.append(header(row, columns, field, onSelect));

  const list = el("ol");
  panel.append(list);

  // The current value belongs at the top of a single cell's story. Across a
  // whole entry there are six of them, and they are already on the row above.
  if (column) {
    const current = el("li", "current");
    current.append(el("span", "when", "now"));
    current.append(el("span", "what", currentText(row.fields[column.key], column)));
    list.append(current);
  }

  try {
    const path = field
      ? api.p(`/rows/${entity}/${row.id}/history/${field}`)
      : api.p(`/rows/${entity}/${row.id}/history`);
    const { entries } = await api.get(path);

    for (const entry of entries) {
      const of = column || columns.find((c) => c.key === entry.field);
      list.append(line(entry, of, !column, onChange));
    }
    if (!entries.length) {
      list.append(el("li", "nothing",
        column ? "no earlier values" : "nothing has changed yet"));
    }
  } catch (error) {
    toast(error.message);
  }
}

/* Which reading you are looking at. Every cell that has a past is offered, so
 * the panel is also a map of where the churn is. */
function header(row, columns, field, onSelect) {
  const bar = el("div", "history-head");
  bar.append(el("h4", null, field ? "History" : "Entry history"));

  const pills = el("div", "history-pills");
  const pill = (key, label, count) => {
    const button = el("button", `history-pill${field === key ? " on" : ""}`, label);
    if (count) button.append(el("span", "count", String(count)));
    button.onclick = (event) => { event.stopPropagation(); onSelect(key); };
    return button;
  };
  pills.append(pill(null, "Whole entry"));
  for (const col of columns) {
    const count = row.history_counts?.[col.key] || 0;
    if (count > 0) pills.append(pill(col.key, col.label || col.key, count));
  }
  bar.append(pills);
  return bar;
}

/* Which half of a change a line shows depends on the reading it is in.
 *
 * Filtered to one cell, the current value sits at the top as "now" and the
 * lines below are what it said *before* each change -- reading downward is
 * reading backward. Across a whole entry there is no "now" to read down from,
 * so each line says what the cell *became* at that moment, which is what a
 * chronology means. Getting this backwards makes an edit look like it logged
 * the text you replaced.
 */
export function half(entry, wholeEntry) {
  return wholeEntry
    ? { shown: entry.new_value, other: entry.old_value, word: "was" }
    : { shown: entry.old_value, other: entry.new_value, word: "became" };
}

function line(entry, column, showField, onChange) {
  const item = el("li", entry.inherited ? "inherited" : null);
  item.append(el("span", "when", fmt.stamp(entry.changed_at)));

  if (showField && column) {
    item.append(el("span", "which", column.label || column.key));
  }

  if (entry.inherited) {
    /* Not this cell's own revision: its source moved, and this cell moved with
     * it. Shown so the replay is complete, but stored once, on the source --
     * so it is not this panel's to delete. */
    item.append(el("span", "what",
      `${entry.source} moved to ${fmt.historyValue("date", entry.new_value)}`));
    item.append(el("span", "borrowed", "inherited"));
    item.title = `${entry.source} moved from `
      + `${fmt.historyValue("date", entry.old_value)} on `
      + `${new Date(entry.changed_at).toLocaleString()}`;
    return item;
  }

  const { shown, other, word } = half(entry, showField);
  item.append(el("span", "what", fmt.historyValue(column, shown)));
  item.title = `${word} ${fmt.historyValue(column, other)}\nchanged `
    + `${new Date(entry.changed_at).toLocaleString()}`;

  /* A mistyped value becomes a permanent revision the moment it is corrected.
   * This drops that line -- and only that line; the cell's current value is
   * not touched. */
  const drop = el("button", "forget", "×");
  drop.title = "Delete this entry from the history";
  drop.onclick = (event) => { event.stopPropagation(); forget(entry, column, onChange); };
  item.append(drop);
  return item;
}

async function forget(entry, column, onChange) {
  const shown = fmt.historyValue(column, entry.old_value);
  const label = column?.label || "this";
  if (!confirm(`Delete this entry from the ${label} history?\n\n`
      + `    ${fmt.stamp(entry.changed_at)}  ${shown}\n\n`
      + "The cell's current value is not affected. This cannot be undone.")) return;
  try {
    const result = await api.post(api.p(`/history/${entry.id}/delete`));
    onChange?.(column?.key, result.history_count);
  } catch (error) { toast(error.message); }
}

function currentText(value, col) {
  if (col.type === "datenote") {
    const parts = [fmt.absolute(value.date), value.via, value.note];
    const shown = parts.filter(Boolean).join(" · ") || "(empty)";
    return value.link_label ? `${shown}${fmt.LINK_MARK}${value.link_label}` : shown;
  }
  if (col.type === "date") return fmt.absolute(value) || "(empty)";
  return value || "(empty)";
}
