"""Suggestions: proposed changes, waiting for you to decide.

Manila is written by you and by nothing else. An assistant reading your mail
and your chats can see plenty that belongs in a folder -- an answer that has
arrived, a date that has moved, a name on a thread who is not in the contact
list -- but what it sees is inference, and inference belongs in a queue rather
than in the table.

So the agent side of the API can do exactly two things: read, and propose.
Everything it proposes lands here, in a table of its own, where it changes
nothing at all. Accepting one applies it through history.update_field like an
edit you typed yourself -- same transaction, same history entry, same chip --
so the folder's log records what the cell became, not that something suggested
it. Rejecting one leaves no trace in the folder whatsoever.

Two things make the queue worth trusting rather than worth clicking through:

`seen` is what the cell said at the moment the suggestion was made, captured
here rather than sent by the proposer. If you have since typed something else,
the suggestion is shown as stale instead of quietly reverting you.

`ref` is the mail or chat the suggestion came from. A second sync over the same
inbox proposes the same things again; matching on ref means those arrive as
duplicates and are dropped, so re-running a sync is safe rather than noisy.

An accepted suggestion's ref is also written against the entry it changed, in
`entry_ref`. The queue's own dedupe answers "has this been proposed here?"; the
entry's refs answer "what is this entry already made of?", which is the
question a folder read has to be able to answer if the next sync is to skip
what it has already been told without a second round trip -- and it survives
the queue being cleared, which the first one does not.
"""

import json
from datetime import datetime, timezone

from . import db, history, schema

KINDS = ("update", "create", "archive", "tick")

# One sync run, not one mailbox. A batch is a thing you sit down and read, and
# a hundred proposals is not that -- it is a sign the agent has been told to
# reconcile everything rather than to report what changed.
MAX_BATCH = 50

STATES = ("pending", "accepted", "rejected")


class Refused(ValueError):
    """A batch declined whole, carrying the reason for each part that failed.

    All or nothing on purpose. A batch that lands half-recorded leaves the
    proposer with no way to tell what it still owes you, and leaves you reading
    a sync run with holes in it that nothing explains.
    """

    def __init__(self, problems):
        super().__init__("; ".join(f"[{p['index']}] {p['error']}" for p in problems))
        self.problems = problems


# --- naming things -----------------------------------------------------------

def label_column(entity):
    """The column that says which entry this is: Item, Deliverable, Name."""
    for col in schema.columns(entity):
        if col["type"] in ("text", "longtext"):
            return col
    return schema.columns(entity)[0]


def label_of(entity, values):
    """A short name for one entry, for a queue that is read away from the table."""
    key = label_column(entity)["key"]
    text = str(values.get(key) or "").strip().splitlines()
    first = text[0] if text else ""
    return (first[:77] + "...") if len(first) > 80 else first


def _stamp(raw):
    """One watermark: a full timestamp with its timezone, stored in UTC.

    A date is refused. Read to "the 16th" means either rereading the whole of
    the 16th on every run or losing the end of it, and the proposer knows the
    instant it read to. A time with no zone is refused for the same reason: it
    names a different instant on every machine that reads it.

    Stored in UTC so that the furthest point is also the greatest string, and
    two sources' watermarks can be compared as written.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    wrong = ValueError("covered_through must be a full ISO-8601 timestamp with a "
                       f"timezone, e.g. 2026-09-16T23:50:00Z -- not {text!r}")
    try:
        # A trailing Z is what most APIs send; not every Python reads it.
        when = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in "Zz" else text)
    except ValueError:
        raise wrong from None
    if len(text) <= 10 or when.tzinfo is None:
        raise wrong
    return when.astimezone(timezone.utc).isoformat()


def _coverage(meta):
    """{source: watermark} for one run, checked. Empty if it says nothing.

    Keyed by source because one run can read two places unevenly: Outlook read
    through tonight while the Teams scan stopped early on a rate limit. A
    single value would overstate Teams and silently skip whatever fell in the
    gap.

    The keys are the only thing that names a source. A run's own `source` is a
    label -- "both", "Outlook + Teams" -- and taking it as a key is how a label
    became a third source whose watermark competed with the real two.
    """
    raw = meta.get("covered_through")
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError('covered_through must be an object of {source: timestamp}, '
                         'e.g. {"Outlook": "2026-09-16T23:50:00Z"}')
    out = {}
    for source, stamp in raw.items():
        name = str(source).strip()
        if not name:
            raise ValueError("covered_through has a source with no name")
        if name in out:
            raise ValueError(f"covered_through names {name!r} twice")
        if not str(stamp or "").strip():
            raise ValueError(f"covered_through for {name!r} is empty; leave "
                             "that source out instead")
        out[name] = _stamp(stamp)
    return out


def _when(text):
    """A stored watermark as an instant, for comparing. Runs recorded before
    timestamps were required may hold a bare date: that is read as the start
    of the day, the reading that can only make a run reread, never skip."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def tick_column(entity):
    for col in schema.columns(entity):
        if col["type"] == "check":
            return col
    raise ValueError(f"{entity} has no tick box")


# --- validation --------------------------------------------------------------

def _row(conn, entity, folder_id, row_id):
    """One row of this folder, or a refusal naming what was not found.

    Scoped to the folder deliberately: a proposal is made against a folder you
    pointed the agent at, and a row id from somewhere else is a mistake however
    it arose.
    """
    try:
        wanted = int(row_id)
    except (TypeError, ValueError):
        raise ValueError(f"row must be the id of a {entity}, not {row_id!r}")
    row = conn.execute(
        f"SELECT * FROM {schema.TABLES[entity]} WHERE id = ? AND folder_id = ?",
        (wanted, folder_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"no {entity} with id {wanted} in this folder")
    return row


def _value(conn, entity, folder_id, field, raw):
    """Coerce one incoming value, and check anything it points at really exists."""
    col = schema.column(entity, field)
    if col["type"] == "check":
        raise ValueError(f"{field} is a tick box: propose kind 'tick' instead")
    value = history.coerce(col, raw)
    spec = col.get("link")
    if spec and isinstance(value, dict) and value.get("link"):
        # Borrowing a date is a real proposal -- "this is due when that ships"
        # -- so the deliverable it names has to be one of this folder's.
        _row(conn, spec["entity"], folder_id, value["link"])
    return col, value


def check(conn, folder_id, item):
    """Validate one proposal into the row that would store it.

    Everything a proposal can get wrong is caught here, while it is still just
    JSON: an unknown field, a row in another folder, a date that isn't one, a
    change that changes nothing. What reaches the queue is something that could
    actually be applied, because a queue full of proposals that fail on accept
    is worse than no queue.
    """
    kind = str(item.get("kind") or "update").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")

    entity = item.get("entity") or item.get("entity_type")
    if entity not in schema.TABLES:
        raise ValueError(f"entity must be one of {', '.join(schema.TABLES)}, not {entity!r}")

    out = {
        "kind": kind,
        "entity_type": entity,
        "entity_id": None,
        "field": "",
        "seen": "",
        "value": "null",
        "evidence": str(item.get("evidence") or "").strip(),
        "reason": str(item.get("reason") or "").strip(),
        "ref": str(item.get("ref") or "").strip(),
    }

    if kind == "create":
        fields = item.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("a create needs a fields object, e.g. "
                             '{"item": "Chase the supplier quote"}')
        built = {}
        for key, raw in fields.items():
            col, value = _value(conn, entity, folder_id, key, raw)
            if history.is_blank(col, value):
                continue
            built[key] = value
        if not built:
            raise ValueError("a create with nothing in it would make an empty row")
        out["value"] = json.dumps(built)
        return out

    row = _row(conn, entity, folder_id, item.get("row") or item.get("entity_id"))
    out["entity_id"] = row["id"]

    if kind == "update":
        field = item.get("field")
        if not field:
            raise ValueError("an update needs the field to change")
        col, value = _value(conn, entity, folder_id, field, item.get("value"))
        was = history.read_value(col, row)
        if was == value:
            raise ValueError(f"{field} already says that")
        out["field"] = col["key"]
        out["seen"] = json.dumps(was)
        out["value"] = json.dumps(value)
        return out

    if kind == "archive":
        if row["archived_at"]:
            raise ValueError("that entry is already archived")
        return out

    col = tick_column(entity)              # kind == "tick"
    if row[col["key"]]:
        raise ValueError("that entry is already ticked")
    out["field"] = col["key"]
    return out


# --- recording ---------------------------------------------------------------

def _duplicate(conn, folder_id, entry):
    """Whether this exact proposal, from this exact mail, has been seen before.

    Decided ones count. Having rejected something once, being offered it again
    on every sync is the behaviour that makes a review queue get ignored.
    """
    if not entry["ref"]:
        return False
    # `IS` rather than `=` so that a create, whose target does not exist yet and
    # whose entity_id is therefore NULL, compares equal to another create from
    # the same mail instead of comparing to nothing at all.
    return conn.execute(
        "SELECT 1 FROM suggestion WHERE folder_id = ? AND ref = ? AND kind = ?"
        " AND entity_type = ? AND field = ? AND entity_id IS ? LIMIT 1",
        (folder_id, entry["ref"], entry["kind"], entry["entity_type"],
         entry["field"], entry["entity_id"]),
    ).fetchone() is not None


def _key(entry):
    return (entry["ref"], entry["kind"], entry["entity_type"],
            entry["entity_id"], entry["field"])


def record(conn, folder_id, meta, items):
    """Store one sync run's proposals. Returns what was kept and what was not.

    A run that found nothing new is still a run: if it says how far it read,
    that is recorded as a batch with no suggestions in it, which the review
    queue never shows. Otherwise a quiet week leaves the watermark where the
    last busy one put it, and every run after re-reads from there.
    """
    items = [] if items is None else items
    if not isinstance(items, list):
        raise ValueError("suggestions must be a list")
    if len(items) > MAX_BATCH:
        raise ValueError(
            f"{len(items)} suggestions in one batch; {MAX_BATCH} is the most that "
            "can be reviewed in a sitting. Propose what changed, not everything.")

    # Checked before anything else: a batch whose watermark is nonsense is
    # refused whole, rather than recorded with the one field that the next run
    # will steer by quietly dropped.
    covered = _coverage(meta)
    rebaseline = bool(meta.get("rebaseline"))
    if rebaseline and not covered:
        raise ValueError("rebaseline needs covered_through: it says where the "
                         "watermark starts again")
    if not items and not covered:
        raise ValueError("nothing to record: send at least one suggestion, or "
                         "covered_through to say how far a quiet run read")

    checked, problems = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append({"index": index, "error": "each suggestion must be an object"})
            continue
        try:
            checked.append(check(conn, folder_id, item))
        except KeyError as exc:               # schema.column on an unknown field
            problems.append({"index": index, "error": str(exc.args[0])})
        except ValueError as exc:
            problems.append({"index": index, "error": str(exc)})
    if problems:
        raise Refused(problems)

    fresh, duplicates, seen_here = [], 0, set()
    for index, entry in enumerate(checked):
        if entry["ref"] and (_key(entry) in seen_here or _duplicate(conn, folder_id, entry)):
            duplicates += 1
            continue
        seen_here.add(_key(entry))
        fresh.append(entry)

    if not fresh and not covered:
        # Every one of them had already been proposed. That is the ordinary
        # result of re-running a sync, so it is reported rather than refused.
        # With no watermark either, there is nothing worth keeping.
        return {"batch_id": None, "recorded": 0, "duplicates": duplicates}

    stamp = history.now()
    conn.execute("BEGIN")
    try:
        source = str(meta.get("source") or "").strip()
        batch = conn.execute(
            "INSERT INTO suggestion_batch (folder_id, source, summary, agent,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (folder_id, source, str(meta.get("summary") or "").strip(),
             str(meta.get("agent") or "").strip(), stamp),
        ).lastrowid
        # Only the sources the run vouches for are advanced. A run that
        # vouches for none is kept on the batch and moves nothing.
        for name, through in covered.items():
            conn.execute(
                "INSERT INTO batch_coverage (batch_id, source, covered_through,"
                " rebaseline) VALUES (?, ?, ?, ?)",
                (batch, name, through, int(rebaseline)))
        for entry in fresh:
            conn.execute(
                "INSERT INTO suggestion (batch_id, folder_id, kind, entity_type,"
                " entity_id, field, seen, value, evidence, reason, ref, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch, folder_id, entry["kind"], entry["entity_type"],
                 entry["entity_id"], entry["field"], entry["seen"], entry["value"],
                 entry["evidence"], entry["reason"], entry["ref"], stamp),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    touched = set(covered)
    return {"batch_id": batch, "recorded": len(fresh), "duplicates": duplicates,
            # What each source's watermark is now, which is not always what was
            # sent: an earlier point than one already recorded does not win.
            "last_runs": [run for run in last_runs(conn, folder_id)
                          if run["source"] in touched]}


# --- reading -----------------------------------------------------------------

def pending_count(conn, folder_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM suggestion"
        " WHERE folder_id = ? AND state = 'pending'", (folder_id,)
    ).fetchone()["n"]


def pending_counts(conn):
    """{folder id: how many are waiting}, for the tab that says so."""
    rows = conn.execute(
        "SELECT folder_id, COUNT(*) AS n FROM suggestion"
        " WHERE state = 'pending' GROUP BY folder_id").fetchall()
    return {r["folder_id"]: r["n"] for r in rows}


def _shape(conn, row):
    """One suggestion, with enough of the folder's current state to judge it."""
    entity = row["entity_type"]
    value = json.loads(row["value"] or "null")
    out = {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "kind": row["kind"],
        "entity": entity,
        "entity_id": row["entity_id"],
        "field": row["field"],
        "value": value,
        "proposed": json.loads(row["proposed"]) if row["proposed"] else None,
        "evidence": row["evidence"],
        "reason": row["reason"],
        "ref": row["ref"],
        "state": row["state"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "stale": False,
        "gone": False,
    }
    if row["kind"] == "create":
        out["title"] = label_of(entity, value)
        out["fields"] = value
        return out

    current = conn.execute(
        f"SELECT * FROM {schema.TABLES[entity]} WHERE id = ?", (row["entity_id"],)
    ).fetchone()
    if current is None:
        # The entry was deleted after the suggestion was made. It can still be
        # rejected -- there is simply nothing left to apply it to.
        out["gone"] = True
        out["title"] = ""
        return out
    # The whole entry, as it stands, so a change to one cell is read in the
    # context of the rest -- the same view a proposed new entry gets.
    out["row"] = history.row_to_dict(entity, current)["fields"]
    out["title"] = label_of(entity, out["row"])

    if row["kind"] == "update":
        col = schema.column(entity, row["field"])
        out["field_label"] = col["label"]
        out["type"] = col["type"]
        out["now"] = history.read_value(col, current)
        # Only pending ones can be overtaken; a decided one is a record of what
        # the cell said then, and "stale" would be a lie about it now.
        if row["state"] == "pending" and row["seen"]:
            out["seen"] = json.loads(row["seen"])
            out["stale"] = out["seen"] != out["now"]
    elif row["kind"] == "archive":
        out["gone"] = current["archived_at"] is not None
    elif row["kind"] == "tick":
        out["gone"] = bool(current[row["field"]])
    return out


def listing(conn, folder_id, scope="pending"):
    """The review queue for one folder, newest run first."""
    if scope not in ("pending", "decided", "all"):
        raise ValueError(f"unknown scope {scope!r}")
    where = {"pending": " AND s.state = 'pending'",
             "decided": " AND s.state <> 'pending'",
             "all": ""}[scope]
    rows = conn.execute(
        "SELECT s.* FROM suggestion s WHERE s.folder_id = ?" + where
        + " ORDER BY s.batch_id DESC, s.id", (folder_id,)).fetchall()

    grouped = {}
    for row in rows:
        grouped.setdefault(row["batch_id"], []).append(_shape(conn, row))

    coverage = {}
    for row in conn.execute(
            "SELECT c.* FROM batch_coverage c JOIN suggestion_batch b"
            " ON b.id = c.batch_id WHERE b.folder_id = ? AND c.covered_through <> ''",
            (folder_id,)):
        coverage.setdefault(row["batch_id"], {})[row["source"]] = row["covered_through"]

    batches = []
    for batch in conn.execute(
            "SELECT * FROM suggestion_batch WHERE folder_id = ? ORDER BY id DESC",
            (folder_id,)):
        held = grouped.get(batch["id"])
        if not held:
            continue
        batches.append({
            "id": batch["id"], "source": batch["source"], "summary": batch["summary"],
            "agent": batch["agent"], "created_at": batch["created_at"],
            "covered_through": coverage.get(batch["id"], {}),
            "pending": sum(1 for s in held if s["state"] == "pending"),
            "suggestions": held,
        })
    return {"batches": batches, "pending": pending_count(conn, folder_id)}


def last_runs(conn, folder_id):
    """How far each source has been read into this folder, and when.

    What an agent needs to know before it reads a mailbox: how far back to go.
    One row per source a run has vouched for in covered_through -- never per
    batch label, so there is exactly one row to read for Outlook and one for
    Teams. `covered_through` is where the next run starts; `last_run` is the
    last time a run advanced it; `runs` is how many have.

    A source with no row has no watermark here. That is the case worth
    noticing: a first pass over a source with no floor will surface a backlog,
    not a morning, so the window has to be chosen rather than continued.

    Monotonic: the furthest point any run has reached is the one kept, so a
    narrow or partial run cannot drag a source back. The exception is a run
    sent with `rebaseline`, which starts that source's watermark again from
    its own value -- for when the old one is known to be wrong.
    """
    rows = conn.execute(
        "SELECT c.source, c.covered_through, c.rebaseline, b.created_at"
        " FROM batch_coverage c JOIN suggestion_batch b ON b.id = c.batch_id"
        " WHERE b.folder_id = ? ORDER BY b.id", (folder_id,)).fetchall()
    runs = {}
    for row in rows:
        run = runs.setdefault(row["source"], {
            "source": row["source"] or "(unnamed)", "last_run": "",
            "covered_through": "", "runs": 0})
        run["runs"] += 1
        run["last_run"] = max(run["last_run"], row["created_at"])
        through = row["covered_through"]
        if row["rebaseline"]:
            run["covered_through"] = through
        elif not run["covered_through"] or _later(through, run["covered_through"]):
            run["covered_through"] = through
    return sorted(runs.values(), key=lambda r: r["last_run"], reverse=True)


def _later(a, b):
    first, second = _when(a), _when(b)
    if first is None or second is None:
        return first is not None
    return first > second


# --- deciding ----------------------------------------------------------------

def _suggestion(conn, suggestion_id):
    row = conn.execute("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None:
        raise KeyError(f"no suggestion with id {suggestion_id}")
    return row


def revise(conn, suggestion_id, value):
    """Change what a pending suggestion proposes, before accepting it.

    The original is kept the first time it is edited, so an accepted suggestion
    still says what was actually suggested -- which is the only way to tell
    later whether the agent was worth listening to.
    """
    row = _suggestion(conn, suggestion_id)
    if row["state"] != "pending":
        raise ValueError(f"that suggestion was already {row['state']}")
    entity = row["entity_type"]

    if row["kind"] == "create":
        if not isinstance(value, dict) or not value:
            raise ValueError("a create needs a fields object")
        built = {}
        for key, raw in value.items():
            col, coerced = _value(conn, entity, row["folder_id"], key, raw)
            if not history.is_blank(col, coerced):
                built[key] = coerced
        if not built:
            raise ValueError("a create with nothing in it would make an empty row")
        stored = built
    elif row["kind"] == "update":
        _, stored = _value(conn, entity, row["folder_id"], row["field"], value)
    else:
        raise ValueError(f"a {row['kind']} suggestion has no value to edit")

    conn.execute(
        "UPDATE suggestion SET value = ?, proposed = ? WHERE id = ?",
        (json.dumps(stored), row["proposed"] or row["value"], suggestion_id))
    return _shape(conn, _suggestion(conn, suggestion_id))


def _remember_ref(conn, row, entity_id):
    """Write an accepted suggestion's source message against the entry it built.

    Only on accept. Something still in the queue has not been read into
    anything, and something rejected was read and turned down -- neither is
    part of what the entry is made of.
    """
    if not row["ref"] or entity_id is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO entry_ref (entity_type, entity_id, ref, field, at)"
        " VALUES (?, ?, ?, ?, ?)",
        (row["entity_type"], entity_id, row["ref"], row["field"], history.now()))


def refs_for(conn, entity, ids):
    """{entry id: the messages it was built from}, oldest first.

    Handed back with a folder read so that the next sync can tell what an entry
    already reflects from what it does not, against data it has loaded anyway.
    """
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT entity_id, ref FROM entry_ref"
        f" WHERE entity_type = ? AND entity_id IN ({marks}) ORDER BY id",
        (entity, *ids)).fetchall()
    out = {}
    for row in rows:
        held = out.setdefault(row["entity_id"], [])
        if row["ref"] not in held:      # one message can build two of an entry's cells
            held.append(row["ref"])
    return out


def forget_refs(conn, entity, ids):
    """Drop what a deleted entry was made of, with the entry.

    Row ids are reused, and a new entry must not be born already believing it
    has read last quarter's mail.
    """
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute("DELETE FROM entry_ref"
                     f" WHERE entity_type = ? AND entity_id IN ({marks})",
                     (entity, *ids))


def _mark(state, suggestion_id):
    return ("UPDATE suggestion SET state = ?, decided_at = ? WHERE id = ?",
            (state, history.now(), suggestion_id))


def decide(conn, suggestion_id, verdict, value=None, resolve=None):
    """Accept or reject one suggestion.

    Accepting is the only place a suggestion touches a content table, and it
    does it the same way you would: through history.update_field for an edit,
    or as a row born with its values for a create. `resolve` is handed in by
    the server to read a borrowed date through to its source -- the one piece
    of this that belongs to routing rather than to storage.
    """
    if verdict not in ("accept", "reject"):
        raise ValueError(f"verdict must be accept or reject, not {verdict!r}")
    row = _suggestion(conn, suggestion_id)
    if row["state"] != "pending":
        raise ValueError(f"that suggestion was already {row['state']}")

    if verdict == "reject":
        conn.execute(*_mark("rejected", suggestion_id))
        return {"id": suggestion_id, "state": "rejected"}

    if value is not None:
        revise(conn, suggestion_id, value)
        row = _suggestion(conn, suggestion_id)

    entity, kind = row["entity_type"], row["kind"]
    stored = json.loads(row["value"] or "null")

    if kind == "update":
        col = schema.column(entity, row["field"])
        applied = resolve(entity, row["field"], stored) if resolve else stored
        try:
            changed, written = history.update_field(
                conn, entity, row["entity_id"], row["field"], applied)
        except KeyError:
            raise ValueError("the entry this was about has been deleted")
        # update_field owns its own transaction, so the mark lands just after
        # rather than within it. Accepting twice is guarded by the pending check
        # above, and the edit itself is a no-op the second time regardless.
        conn.execute(*_mark("accepted", suggestion_id))
        _remember_ref(conn, row, row["entity_id"])
        return {"id": suggestion_id, "state": "accepted",
                "entity": entity, "entity_id": row["entity_id"],
                "field": row["field"], "changed": changed, "value": written,
                "label": col["label"]}

    table = schema.TABLES[entity]
    conn.execute("BEGIN")
    try:
        if kind == "create":
            row_id = conn.execute(
                f"INSERT INTO {table} (folder_id, position, created_at) VALUES (?, ?, ?)",
                (row["folder_id"], db.next_position(conn, table, row["folder_id"]),
                 history.now()),
            ).lastrowid
            updates = {}
            for key, raw in (stored or {}).items():
                col = schema.column(entity, key)
                applied = resolve(entity, key, raw) if resolve else raw
                updates.update(history.to_storage(col, history.coerce(col, applied)))
            if updates:
                conn.execute(
                    f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in updates)}"
                    " WHERE id = ?", (*updates.values(), row_id))
            history.born(conn, entity, row_id, list(stored or {}))
            outcome = {"entity_id": row_id, "created": True}
        elif kind == "archive":
            conn.execute(f"UPDATE {table} SET archived_at = ? WHERE id = ?",
                         (history.now(), row["entity_id"]))
            outcome = {"entity_id": row["entity_id"], "archived": True}
        else:                                  # tick
            conn.execute(f"UPDATE {table} SET {row['field']} = 1 WHERE id = ?",
                         (row["entity_id"],))
            outcome = {"entity_id": row["entity_id"], "ticked": True}
        conn.execute(*_mark("accepted", suggestion_id))
        _remember_ref(conn, row, outcome["entity_id"])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # A row is born with its values rather than changed into them, so a create
    # writes no history -- the same rule the table itself follows when you type
    # into its bottom row.
    return {"id": suggestion_id, "state": "accepted", "entity": entity, **outcome}


def decide_batch(conn, batch_id, verdict, resolve=None):
    """Decide everything still pending in one run.

    Accepting the lot is offered because a good sync is usually right about all
    of it, and clicking twelve times to say so is how a queue stops being read.
    Anything that has gone stale is left pending: those are exactly the ones
    worth a second look.
    """
    rows = conn.execute(
        "SELECT id FROM suggestion WHERE batch_id = ? AND state = 'pending' ORDER BY id",
        (batch_id,)).fetchall()
    if not rows:
        raise ValueError("nothing in that batch is still pending")
    done, held = [], []
    for row in rows:
        shaped = _shape(conn, _suggestion(conn, row["id"]))
        if verdict == "accept" and (shaped["stale"] or shaped["gone"]):
            held.append({"id": row["id"], "why": "gone" if shaped["gone"] else "stale"})
            continue
        try:
            done.append(decide(conn, row["id"], verdict, resolve=resolve))
        except ValueError as exc:
            held.append({"id": row["id"], "why": str(exc)})
    return {"batch_id": batch_id, "decided": len(done), "held": held}


# The order an entry's changes are applied in: its cells first, then ticking it
# off, and archiving last -- an entry is put away only once it says why.
APPLY_ORDER = {"update": 0, "create": 0, "tick": 1, "archive": 2}


def decide_many(conn, ids, verdict, values=None, resolve=None):
    """Decide a chosen set together: everything one run proposed for one entry.

    The Review tab shows those as a single card, so they are accepted or
    rejected with one action. Unlike Accept all, a stale change is taken too:
    it was on the card, flagged, when the choice was made.

    Two proposals for the same cell cannot both be right. Accepting keeps the
    latest and rejects the earlier ones, which is what the card shows.
    `values` maps a suggestion id to the edited value to accept it with.
    """
    if verdict not in ("accept", "reject"):
        raise ValueError(f"verdict must be accept or reject, not {verdict!r}")
    values = {int(k): v for k, v in (values or {}).items()}
    rows = [_suggestion(conn, int(i)) for i in dict.fromkeys(ids)]
    if not rows:
        raise ValueError("no suggestions given")
    if any(r["state"] != "pending" for r in rows):
        raise ValueError("some of those were already decided")

    plan = [(r, verdict) for r in rows]
    if verdict == "accept":
        latest = {}
        for r in rows:
            if r["kind"] == "update":
                latest[(r["entity_type"], r["entity_id"], r["field"])] = max(
                    r["id"], latest.get((r["entity_type"], r["entity_id"], r["field"]), 0))
        plan = [(r, "accept" if r["kind"] != "update"
                 or latest[(r["entity_type"], r["entity_id"], r["field"])] == r["id"]
                 else "reject") for r in rows]
        plan.sort(key=lambda p: (APPLY_ORDER[p[0]["kind"]], p[0]["id"]))

    done, held = [], []
    for r, how in plan:
        try:
            value = values.get(r["id"]) if how == "accept" else None
            done.append(decide(conn, r["id"], how, value, resolve=resolve))
        except ValueError as exc:
            held.append({"id": r["id"], "why": str(exc)})
    return {"decided": len(done), "held": held, "results": done}


def discard_batch(conn, batch_id):
    """Clear a run you have finished reviewing. Decided ones only.

    What it removes is the record of what was suggested, never anything that
    was accepted -- that is in the folder now, and in its history, like any
    other edit.

    The batch itself stays, empty, because it also holds how far that run
    read. Clearing a reviewed run must not send the next one back to reread
    what this one already covered; an empty batch is not shown for review.
    """
    row = conn.execute("SELECT * FROM suggestion_batch WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise KeyError(f"no batch with id {batch_id}")
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM suggestion WHERE batch_id = ? AND state = 'pending'",
        (batch_id,)).fetchone()["n"]
    if left:
        raise ValueError(f"{left} suggestion(s) in that run are still undecided")
    conn.execute("DELETE FROM suggestion WHERE batch_id = ?", (batch_id,))
    return {"id": batch_id, "deleted": True}
