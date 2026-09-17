/* Display formatting.
 *
 * These are the only computed values in Manila, and they only reformat what
 * was typed -- a relative phrase is rendered fresh on every paint so it never
 * goes stale, while storage and history always keep the literal date. */

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

export function absolute(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return `${String(d).padStart(2, "0")}-${MONTHS[m - 1]}-${y}`;
}

function daysFromToday(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return null;
  const then = Date.UTC(y, m - 1, d);
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((then - today) / 86400000);
}

/** True when a date is strictly before today -- a deadline that has passed. */
export function isPast(iso) {
  const days = daysFromToday(iso);
  return days !== null && days < 0;
}

export function relative(iso) {
  const days = daysFromToday(iso);
  if (days === null) return "";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  return days > 0 ? `in ${days} days` : `${-days} days ago`;
}

/** Timestamps in the history log: absolute, never relative. */
export function stamp(iso) {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.valueOf())) return iso;
  return `${String(at.getDate()).padStart(2, "0")}-${MONTHS[at.getMonth()]}`;
}

export const LINK_MARK = " \u21b3 ";

/** Render a stored history value back into something readable.
 *
 * Takes the column, not just its type: how many fields a composite has is
 * declared by its storage list, which is also the only safe way to split a
 * value whose free-text half may itself contain a pipe. */
export function historyValue(col, raw) {
  const type = typeof col === "string" ? col : col.type;
  const slots = typeof col === "string" ? 0 : (col.storage?.length ?? 0);
  if (raw === "") return "(empty)";
  if (type === "datenote") {
    // The last field absorbs any remaining separators, so a note reading
    // "re: a|b" survives the round trip intact.
    const count = slots || (raw.split("|").length > 2 ? 3 : 2);
    const bits = raw.split("|");
    const fields = bits.slice(0, count - 1).concat(bits.slice(count - 1).join("|"));
    // A borrowed date carries where it came from, appended to the date itself.
    const [date, source] = fields[0].split(LINK_MARK);
    const shown = [absolute(date), ...fields.slice(1)].filter(Boolean).join(" · ");
    const from = source ? `${LINK_MARK}${source}` : "";
    return (shown + from) || "(empty)";
  }
  if (type === "date") return absolute(raw);
  if (type === "check") return raw === "1" ? "done" : "not done";
  return raw;
}
