"""The one write path for field values.

Every change to a cell goes through update_field(). It compares the old value
to the new one, writes the row and the history record in a single transaction,
and writes nothing at all when the value did not actually change. No route may
UPDATE a content column directly -- that rule is what makes the history log
trustworthy enough to replay.
"""

from datetime import datetime, timedelta, timezone

from . import schema


# How a borrowed source is written into the log. The date slot is an ISO date
# or empty, so a suffix on it can never be confused with the date itself.
LINK_MARK = " \u21b3 "


def now():
    # Microseconds, not seconds: a link made and a source moved within the same
    # second still have to be tellable apart, or one is attributed to the other.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def source_label(conn, col, link_id):
    """What a borrowed value's source was called, for the log.

    Written into the entry as text rather than as an id, because the log has to
    still read correctly years later -- including after the source is deleted.
    """
    spec = col.get("link")
    if not spec or not link_id:
        return ""
    row = conn.execute(
        f"SELECT {spec['label']} AS label FROM {schema.TABLES[spec['entity']]}"
        " WHERE id = ?", (link_id,)).fetchone()
    if row is None:
        return f"#{link_id}"
    return (row["label"] or "").strip() or f"#{link_id}"


def parts(col):
    """The named halves (or thirds) of a composite cell, in display order.

    A datenote is date + note; add a third storage column and it becomes
    date + via + note. The storage list is the single declaration of which.
    """
    return ("date", "via", "note") if len(schema.storage_columns(col)) == 3 \
        else ("date", "note")


# --- value <-> storage -------------------------------------------------------

def read_value(col, row):
    """Storage columns -> the value the API and browser exchange."""
    kind = col["type"]
    if kind == "check":
        return bool(row[col["key"]])
    if kind == "datenote":
        value = {name: row[column] or ""
                 for name, column in zip(parts(col), schema.storage_columns(col))}
        link = schema.link_column(col)
        if link:
            # 0 means "my own date". The stored date stays as the last effective
            # one, so unlinking -- or losing the source -- keeps a real date.
            value["link"] = row[link] or 0
        return value
    return row[col["key"]] or ""


def coerce(col, value):
    """Normalise an incoming value, rejecting shapes that don't fit the cell."""
    kind = col["type"]
    if kind == "check":
        return 1 if value else 0
    if kind == "datenote":
        if not isinstance(value, dict):
            named = ", ".join(parts(col))
            raise ValueError(f"{col['key']} expects an object with {named}")
        out = {name: str(value.get(name) or "").strip() for name in parts(col)}
        if schema.link_column(col):
            try:
                out["link"] = int(value.get("link") or 0)
            except (TypeError, ValueError):
                raise ValueError(f"{col['key']} link must be a row id")
        return out
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{col['key']} expects text")
    return value.strip() if kind != "longtext" else value.rstrip()


def to_storage(col, value):
    """Value -> {db column: db value}."""
    if col["type"] == "datenote":
        out = dict(zip(schema.storage_columns(col),
                       (value[name] for name in parts(col))))
        link = schema.link_column(col)
        if link:
            out[link] = value.get("link") or None
        return out
    return {col["key"]: value}


def to_history(col, value, label=""):
    """Canonical text for the log.

    Dates are stored ISO here on purpose: the log must still read correctly
    years later, so it records what was entered, never a relative phrase.

    A borrowed date is logged as the date itself, not as the link: the log
    answers "what did this cell say", and it said a date. Which milestone it
    came from is live state, and a milestone that later slips has that slip
    recorded once, in its own Date cell, rather than copied into every action
    waiting on it.
    """
    if col["type"] == "datenote":
        fields = [value[name] for name in parts(col)]
        # Linking and unlinking are real changes to the cell, so the log has to
        # show them. Without this, binding a date to a deliverable it already
        # matched wrote an entry whose two sides read identically.
        if schema.link_column(col) and value.get("link"):
            fields[0] = f"{fields[0]}{LINK_MARK}{label}" if label else fields[0]
        return "|".join(fields)
    if col["type"] == "check":
        return "1" if value else "0"
    return value


def _link_of(value):
    return value.get("link") or 0 if isinstance(value, dict) else 0


def is_blank(col, value):
    """True when a value carries nothing worth remembering."""
    if col["type"] == "datenote":
        return not any(value[name] for name in parts(col)) and not value.get("link")
    if col["type"] == "check":
        return False
    return not str(value).strip()


# --- the write path ----------------------------------------------------------

def update_field(conn, entity_type, entity_id, field, raw_value):
    """Commit one cell edit. Returns (changed, value)."""
    table = schema.TABLES[entity_type]
    col = schema.column(entity_type, field)
    value = coerce(col, raw_value)

    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise KeyError(f"no {entity_type} with id {entity_id}")

    old_value = read_value(col, row)
    if old_value == value:
        return False, value  # no-op edits write nothing, not even history

    updates = to_storage(col, value)
    # When a cell starts borrowing, note when. Everything the source did before
    # that belongs to the source alone.
    spec = col.get("link")
    if spec and _link_of(value) != _link_of(old_value):
        updates[spec["since"]] = now() if _link_of(value) else ""
    assignments = ", ".join(f"{c} = ?" for c in updates)

    conn.execute("BEGIN")
    try:
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?",
            (*updates.values(), entity_id),
        )
        if schema.tracks_history(col):
            _log(conn, entity_type, entity_id, col, old_value, value)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True, value


# --- settling ----------------------------------------------------------------
#
# Going straight back into a cell you just left -- the word you forgot, the
# typo you saw as focus moved on, the click that blurred it early -- is part of
# making one edit, not a second one. Edits to the same cell less than this many
# seconds apart are one revision: the entry keeps what the cell said before the
# first of them and takes the value after the last. The window restarts on each
# edit, so a cell settles once it has been left alone that long.

SETTLE_SECONDS = 10


def _elapsed(since, until):
    return (datetime.fromisoformat(until) - datetime.fromisoformat(since)).total_seconds()


def _log(conn, entity_type, entity_id, col, old_value, value):
    """Record one change to a tracked cell, folding it into a recent one."""
    field, stamp = col["key"], now()
    conn.execute("DELETE FROM settling_edit WHERE edited_at < ?",
                 (_ago(stamp, SETTLE_SECONDS),))
    recent = conn.execute(
        "SELECT base, history_id, edited_at FROM settling_edit"
        " WHERE entity_type = ? AND entity_id = ? AND field = ?",
        (entity_type, entity_id, field)).fetchone()
    if recent is not None and _elapsed(recent["edited_at"], stamp) >= SETTLE_SECONDS:
        recent = None

    if recent is not None:
        base, entry = recent["base"], recent["history_id"]
    else:
        # First fill is not a revision: an empty cell has no past entry worth
        # replaying, so typing into a blank cell leaves no chip behind. Only a
        # value that actually replaces something gets logged.
        entry = None
        base = None if is_blank(col, old_value) else \
            to_history(col, old_value, source_label(conn, col, _link_of(old_value)))

    new_text = to_history(col, value, source_label(conn, col, _link_of(value)))
    if base is None:
        entry = None
    elif base == new_text:
        # Put back the way it was: nothing happened, so nothing is logged.
        if entry is not None:
            conn.execute("DELETE FROM field_history WHERE id = ?", (entry,))
        entry = None
    else:
        if entry is not None and not conn.execute(
                "UPDATE field_history SET new_value = ?, changed_at = ? WHERE id = ?",
                (new_text, stamp, entry)).rowcount:
            entry = None
        # No entry yet -- or you forgot the one this burst wrote, and the cell
        # has changed again since, which is a revision in its own right.
        if entry is None:
            entry = conn.execute(
                "INSERT INTO field_history"
                " (entity_type, entity_id, field, old_value, new_value, changed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (entity_type, entity_id, field, base, new_text, stamp)).lastrowid
    _settle(conn, entity_type, entity_id, field, stamp, base, entry)


def _ago(stamp, seconds):
    return (datetime.fromisoformat(stamp) - timedelta(seconds=seconds)).isoformat(
        timespec="microseconds")


def _settle(conn, entity_type, entity_id, field, stamp, base, entry):
    conn.execute(
        "INSERT OR REPLACE INTO settling_edit"
        " (entity_type, entity_id, field, edited_at, base, history_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, field, stamp, base, entry))


def born(conn, entity_type, entity_id, fields):
    """A new row's values start settling too.

    What a row is created with is a first fill, so fixing it straight away is
    still part of making the row, not a revision of it.
    """
    stamp = now()
    for field in fields:
        if schema.tracks_history(schema.column(entity_type, field)):
            _settle(conn, entity_type, entity_id, field, stamp, None, None)


def unsettle(conn, entity_type, ids):
    """Forget recent edits to rows that are going away.

    Row ids can be reused, and a new row must not inherit a burst that belonged
    to the one deleted before it.
    """
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM settling_edit"
                     f" WHERE entity_type = ? AND entity_id IN ({marks})",
                     (entity_type, *ids))


# --- reading -----------------------------------------------------------------

def row_to_dict(entity_type, row, counts=None):
    out = {
        "id": row["id"],
        "position": row["position"],
        "archived": row["archived_at"] is not None,
        "fields": {},
        "history_counts": {},
    }
    for col in schema.columns(entity_type):
        out["fields"][col["key"]] = read_value(col, row)
        if counts is not None and schema.tracks_history(col):
            out["history_counts"][col["key"]] = counts.get((row["id"], col["key"]), 0)
    return out


def history_counts(conn, entity_type, ids):
    """{(entity_id, field): count} for every cell of the given rows."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT entity_id, field, COUNT(*) AS n FROM field_history"
        f" WHERE entity_type = ? AND entity_id IN ({marks})"
        " GROUP BY entity_id, field",
        (entity_type, *ids),
    ).fetchall()
    return {(r["entity_id"], r["field"]): r["n"] for r in rows}


def cell_history(conn, entity_type, entity_id, field):
    """Newest first. Each entry is what the cell said before that change."""
    rows = conn.execute(
        "SELECT id, old_value, new_value, changed_at FROM field_history"
        " WHERE entity_type = ? AND entity_id = ? AND field = ?"
        " ORDER BY changed_at DESC, id DESC",
        (entity_type, entity_id, field),
    ).fetchall()
    return [dict(r) for r in rows]


def linked_since(conn, entity_type, entity_id, col, own=None):
    """(source id, when this cell was bound to it) -- or (0, None).

    Recorded when the link is made rather than inferred from the log: a cell
    linked on its first fill leaves no entry to infer from, and getting this
    wrong means claiming a slip that happened before this cell was following
    anything.
    """
    spec = col.get("link")
    if not spec:
        return 0, None
    table = schema.TABLES[entity_type]
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None or not row[spec["column"]]:
        return 0, None
    return row[spec["column"]], row[spec["since"]] or row["created_at"]


def inherited_history(conn, entity_type, entity_id, col, own=None):
    """The source's own changes, for the stretch this cell has been bound to it.

    Not copied into this cell's log -- assembled when it is read. One slip of a
    deliverable stays one stored entry however many actions are waiting on it,
    and every one of those actions can still replay what its date did.
    """
    link_id, since = linked_since(conn, entity_type, entity_id, col, own)
    if not link_id:
        return []
    spec = col["link"]
    rows = conn.execute(
        "SELECT id, old_value, new_value, changed_at FROM field_history"
        " WHERE entity_type = ? AND entity_id = ? AND field = ? AND changed_at > ?"
        " ORDER BY changed_at DESC, id DESC",
        (spec["entity"], link_id, spec["date"], since or ""),
    ).fetchall()
    label = source_label(conn, col, link_id)
    return [{**dict(r), "inherited": True, "source": label} for r in rows]


def full_history(conn, entity_type, entity_id, col):
    """A cell's own revisions and its source's, newest first."""
    own = cell_history(conn, entity_type, entity_id, col["key"])
    borrowed = inherited_history(conn, entity_type, entity_id, col, own)
    if not borrowed:
        return own
    return sorted(own + borrowed, key=lambda e: (e["changed_at"], e["id"]), reverse=True)


def forget_entry(conn, entry_id):
    """Drop one line from the log.

    The log is otherwise only ever appended to. This exists because a mistyped
    value becomes a permanent revision the moment it is corrected, and a record
    of a typo is worth less than a log you are willing to keep reading. It
    removes the line only -- the cell's current value is not touched.
    """
    row = conn.execute("SELECT * FROM field_history WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise KeyError(f"no history entry with id {entry_id}")
    conn.execute("DELETE FROM field_history WHERE id = ?", (entry_id,))
    return dict(row)
