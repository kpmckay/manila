/* Whether your edits actually landed.
 *
 * Manila saves as you go, with no Save button to press, so the only honest
 * substitute is to say plainly what has been written and what has not. Every
 * write goes through tracked() -- api.js wraps post, patch and upload in it --
 * and the header reports the total.
 *
 * Two different states get counted:
 *   pending   a request is in flight
 *   unsaved   text typed but not yet sent (the repository's debounce)
 *   failed    a write came back an error and the value is NOT stored
 */

const state = { pending: 0, unsaved: 0, failed: 0, savedAt: null, lastError: "" };
const listeners = new Set();

function notify() {
  for (const listener of listeners) listener({ ...state });
}

export function onStatus(listener) {
  listeners.add(listener);
  listener({ ...state });
  return () => listeners.delete(listener);
}

/** Wrap a write. Resolves to its result; rethrows so callers still handle it. */
export async function tracked(run) {
  state.pending += 1;
  notify();
  try {
    const result = await run();
    state.savedAt = new Date();
    state.failed = 0;
    state.lastError = "";
    return result;
  } catch (error) {
    state.failed += 1;
    state.lastError = error.message;
    throw error;
  } finally {
    state.pending -= 1;
    notify();
  }
}

/** Typed but not yet sent. Kept separate: nothing has been written yet. */
export function markUnsaved(count) {
  state.unsaved = Math.max(0, count);
  notify();
}

export function atRest() {
  return state.pending === 0 && state.unsaved === 0;
}

export function describe({ pending, unsaved, failed, savedAt, lastError }) {
  if (failed) {
    return { text: `${failed} not saved`, tone: "bad",
             title: lastError || "The last change was rejected and is not stored." };
  }
  if (pending || unsaved) {
    return { text: "Saving...", tone: "busy",
             title: "A change is on its way to disk." };
  }
  if (savedAt) {
    const at = savedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit",
                                                second: "2-digit" });
    return { text: `Saved ${at}`, tone: "ok",
             title: `Your last change was written at ${savedAt.toLocaleString()}.` };
  }
  return { text: "No changes yet", tone: "idle",
           title: "Nothing has been edited since this page loaded." };
}
