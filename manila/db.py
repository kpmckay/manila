"""SQLite storage for Manila.

Plain stdlib sqlite3, no ORM -- the schema is small and fixed, and keeping the
SQL visible makes it easy to inspect or repair the file by hand.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS folder (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    position    REAL    NOT NULL,
    created_at  TEXT    NOT NULL,
    archived_at TEXT
);

-- Where a folder's news arrives: the Teams channels, the chats and the mail
-- addresses this folder's traffic comes in on. Pointers only -- a channel id,
-- a chat id, an address, a domain -- because those change about twice a year,
-- while the words people use for the work change every week. The words are
-- already in the folder's own items and Waiting On cells, so an assistant
-- reads them off there rather than being told them here, where they would go
-- stale unwatched.
--
-- `ref` is the pointer itself and `within` is whatever contains it: a team id
-- for a channel, empty for everything else, since nothing else has a parent
-- you have to name to find the child.
CREATE TABLE IF NOT EXISTS folder_source (
    id         INTEGER PRIMARY KEY,
    folder_id  INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,   -- a key of sources.KINDS
    name       TEXT    NOT NULL DEFAULT '',
    ref        TEXT    NOT NULL,
    within     TEXT    NOT NULL DEFAULT '',
    position   REAL    NOT NULL,
    added_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_folder ON folder_source (folder_id, position);

CREATE TABLE IF NOT EXISTS action_item (
    id                INTEGER PRIMARY KEY,
    folder_id         INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    position          REAL    NOT NULL,
    done              INTEGER NOT NULL DEFAULT 0,
    item              TEXT    NOT NULL DEFAULT '',
    waiting_on        TEXT    NOT NULL DEFAULT '',
    last_touched_at   TEXT    NOT NULL DEFAULT '',
    last_touched_via  TEXT    NOT NULL DEFAULT '',
    last_touched_note TEXT    NOT NULL DEFAULT '',
    resolve_by_at     TEXT    NOT NULL DEFAULT '',
    resolve_by_note   TEXT    NOT NULL DEFAULT '',
    resolve_by_milestone_id INTEGER REFERENCES milestone(id) ON DELETE SET NULL,
    resolve_by_linked_at TEXT NOT NULL DEFAULT '',
    current_state     TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    archived_at       TEXT
);

CREATE TABLE IF NOT EXISTS milestone (
    id          INTEGER PRIMARY KEY,
    folder_id   INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    position    REAL    NOT NULL,
    deliverable TEXT    NOT NULL DEFAULT '',
    due_date    TEXT    NOT NULL DEFAULT '',
    notes       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS contact (
    id          INTEGER PRIMARY KEY,
    folder_id   INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    position    REAL    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    title       TEXT    NOT NULL DEFAULT '',
    email       TEXT    NOT NULL DEFAULT '',
    phone       TEXT    NOT NULL DEFAULT '',
    organization TEXT   NOT NULL DEFAULT '',
    team        TEXT    NOT NULL DEFAULT '',
    reports_to  TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    archived_at TEXT
);

-- The Repository: free-positioned frames on a per-folder canvas.
-- Frames are laid out by hand (x, y, w, h in pixels), which is the point --
-- side by side, stacked, wherever you drop them.
CREATE TABLE IF NOT EXISTS repo_frame (
    id          INTEGER PRIMARY KEY,
    folder_id   INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,           -- text | files | image
    title       TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    x           REAL    NOT NULL DEFAULT 0,
    y           REAL    NOT NULL DEFAULT 0,
    w           REAL    NOT NULL DEFAULT 340,
    h           REAL    NOT NULL DEFAULT 240,
    z           REAL    NOT NULL DEFAULT 0,
    view        TEXT    NOT NULL DEFAULT 'plain',   -- plain | rich
    created_at  TEXT    NOT NULL
);

-- One row per file copied into the project. `stored` is the name on disk;
-- `name` is what it was called when it came in, and is what gets shown.
CREATE TABLE IF NOT EXISTS repo_file (
    id         INTEGER PRIMARY KEY,
    frame_id   INTEGER NOT NULL REFERENCES repo_frame(id) ON DELETE CASCADE,
    folder_id  INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    stored     TEXT    NOT NULL,
    mime       TEXT    NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    description TEXT   NOT NULL DEFAULT '',
    position   REAL    NOT NULL DEFAULT 0,
    added_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_frame_folder ON repo_frame (folder_id, z);
CREATE INDEX IF NOT EXISTS idx_file_frame   ON repo_file (frame_id, position);

-- One sync run: everything an assistant proposed in one go, so a morning's
-- worth of email is reviewed as a batch rather than as loose fragments.
--
-- `created_at` is when the run happened; `covered_through` is how far into the
-- source it claims to have read, which is not the same fact. A run that starts
-- at nine and gets a search result three hours stale has read to six, and only
-- the proposer knows that. Left empty when it does not say, which is honest --
-- the next run then has no watermark rather than a wrong one.
CREATE TABLE IF NOT EXISTS suggestion_batch (
    id         INTEGER PRIMARY KEY,
    folder_id  INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    source     TEXT    NOT NULL DEFAULT '',   -- Outlook, Teams, anything
    summary    TEXT    NOT NULL DEFAULT '',   -- what the run found, in its words
    agent      TEXT    NOT NULL DEFAULT '',   -- which client proposed it
    covered_through TEXT NOT NULL DEFAULT '', -- superseded by batch_coverage
    created_at TEXT    NOT NULL
);

-- How far one run read, per source. A run that reads Outlook and Teams can
-- finish one and stop early on the other, so a single watermark per run would
-- overstate the one that stopped. Rows come only from the sources a run
-- vouched for; the batch's own `source` is a label ("both") and never a key
-- here. `rebaseline` restarts that source's watermark from this row instead
-- of keeping the furthest point reached.
CREATE TABLE IF NOT EXISTS batch_coverage (
    batch_id        INTEGER NOT NULL REFERENCES suggestion_batch(id) ON DELETE CASCADE,
    source          TEXT    NOT NULL,
    covered_through TEXT    NOT NULL DEFAULT '',   -- UTC ISO-8601 timestamp
    rebaseline      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (batch_id, source)
);

-- A proposed change, and nothing more. Nothing here has touched a content
-- table: a suggestion becomes a real edit only when it is accepted, and only
-- then does it go through history.update_field like every other edit.
--
-- `seen` is what the cell said when the suggestion was made, captured by the
-- server rather than sent by the proposer. It is what makes a suggestion that
-- has been overtaken by your own typing show as stale instead of silently
-- reverting you.
CREATE TABLE IF NOT EXISTS suggestion (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES suggestion_batch(id) ON DELETE CASCADE,
    folder_id   INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,             -- update | create | archive | tick
    entity_type TEXT    NOT NULL,
    entity_id   INTEGER,                      -- NULL for create
    field       TEXT    NOT NULL DEFAULT '',  -- update only
    seen        TEXT    NOT NULL DEFAULT '',  -- JSON: the cell's value when proposed
    value       TEXT    NOT NULL DEFAULT '',  -- JSON: one value, or {field: value}
    proposed    TEXT    NOT NULL DEFAULT '',  -- JSON: the original, if you edited it
    evidence    TEXT    NOT NULL DEFAULT '',  -- the mail or chat this came from
    reason      TEXT    NOT NULL DEFAULT '',  -- why it thinks so
    ref         TEXT    NOT NULL DEFAULT '',  -- source message id, for dedupe
    state       TEXT    NOT NULL DEFAULT 'pending',   -- pending | accepted | rejected
    created_at  TEXT    NOT NULL,
    decided_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_suggestion_folder
    ON suggestion (folder_id, state, id);
CREATE INDEX IF NOT EXISTS idx_suggestion_ref
    ON suggestion (ref);

-- Which messages an entry is already built from. Written when a suggestion is
-- accepted and never before: a proposal that was rejected, or is still sitting
-- in the queue, has not been read into anything.
--
-- The review queue dedupes on `ref` too, but it answers a different question.
-- It knows what has been proposed to this folder; this knows what each entry
-- was made of, which is what a folder read has to say if the next sync is to
-- tell "already reflected here" from "new" without asking a second time. It
-- outlives the queue deliberately -- clearing a reviewed run must not make
-- last week's mail look unread.
CREATE TABLE IF NOT EXISTS entry_ref (
    id          INTEGER PRIMARY KEY,
    entity_type TEXT    NOT NULL,
    entity_id   INTEGER NOT NULL,
    ref         TEXT    NOT NULL,             -- the source message id
    field       TEXT    NOT NULL DEFAULT '',  -- the cell it built, if one
    at          TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entry_ref_cell
    ON entry_ref (entity_type, entity_id, ref, field);

-- Append-only. One row per committed change to one logical cell.
CREATE TABLE IF NOT EXISTS field_history (
    id          INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT NOT NULL,
    new_value   TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_cell
    ON field_history (entity_type, entity_id, field, changed_at);

-- Cells edited in the last few seconds, so a quick correction folds into the
-- edit it corrects instead of becoming a revision of its own. Rows here are
-- scaffolding, not data: anything older than the settling window is swept.
--
-- `base` is the log text of what the cell said before the burst began, or
-- NULL when it began blank (a first fill, which is never a revision).
-- `history_id` is the one entry the burst has written so far, if any.
CREATE TABLE IF NOT EXISTS settling_edit (
    entity_type TEXT    NOT NULL,
    entity_id   INTEGER NOT NULL,
    field       TEXT    NOT NULL,
    edited_at   TEXT    NOT NULL,
    base        TEXT,
    history_id  INTEGER,
    PRIMARY KEY (entity_type, entity_id, field)
);
"""


def migrate(conn):
    """Bring an existing database up to the current shape.

    Both migrations are lossless: the trash flag was never user-visible data,
    and an old free-text Resolve By becomes the note half of the new date+note
    cell, so nothing anyone typed is thrown away.
    """
    def columns(table):
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    for table in ("folder", "action_item", "milestone", "contact"):
        if "deleted_at" in columns(table):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN deleted_at")

    before = columns("action_item")
    if "resolve_by_at" not in before:
        conn.execute("ALTER TABLE action_item ADD COLUMN resolve_by_at TEXT NOT NULL DEFAULT ''")
    if "resolve_by_note" not in before:
        conn.execute("ALTER TABLE action_item ADD COLUMN resolve_by_note TEXT NOT NULL DEFAULT ''")
        if "resolve_by" in before:
            conn.execute("UPDATE action_item SET resolve_by_note = resolve_by")
    if "resolve_by" in columns("action_item"):
        conn.execute("ALTER TABLE action_item DROP COLUMN resolve_by")

    # Contacts gained Organization: Team and Reports To are ambiguous without it
    # once the list spans more than one organization.
    if "organization" not in columns("contact"):
        conn.execute("ALTER TABLE contact ADD COLUMN organization TEXT NOT NULL DEFAULT ''")

    # Last Touched gained a "how" of its own. Existing notes stay put as the
    # free-text half rather than being guessed at and moved.
    if "last_touched_via" not in columns("action_item"):
        conn.execute("ALTER TABLE action_item ADD COLUMN last_touched_via TEXT NOT NULL DEFAULT ''")

    # Resolve By can now borrow its date from a milestone. Everything already
    # typed keeps its own date -- nothing is linked to anything by guesswork.
    if "resolve_by_milestone_id" not in columns("action_item"):
        conn.execute("ALTER TABLE action_item"
                     " ADD COLUMN resolve_by_milestone_id INTEGER"
                     " REFERENCES milestone(id) ON DELETE SET NULL")
    # A note can be shown as written, or rendered. Existing notes stay plain:
    # how something is displayed is a choice, not something to assume.
    if "view" not in columns("repo_frame"):
        conn.execute("ALTER TABLE repo_frame ADD COLUMN view TEXT NOT NULL DEFAULT 'plain'")
    # An earlier spelling of the same idea.
    conn.execute("UPDATE repo_frame SET view = 'rich' WHERE view = 'formatted'")

    # A document can say what it is, in your words. Optional and empty by
    # default -- nothing already imported gains a description it never had.
    if "description" not in columns("repo_file"):
        conn.execute("ALTER TABLE repo_file ADD COLUMN description TEXT NOT NULL DEFAULT ''")

    if "resolve_by_linked_at" not in columns("action_item"):
        conn.execute("ALTER TABLE action_item"
                     " ADD COLUMN resolve_by_linked_at TEXT NOT NULL DEFAULT ''")

    # A run can now say how far it read, which is not the same as when it ran.
    # Runs recorded before this say nothing, rather than claiming their own
    # start time -- a watermark invented after the fact is the one kind of
    # watermark that is worse than none.
    if "covered_through" not in columns("suggestion_batch"):
        conn.execute("ALTER TABLE suggestion_batch"
                     " ADD COLUMN covered_through TEXT NOT NULL DEFAULT ''")

    # Watermarks moved to one row per source. A run recorded before that which
    # said how far it read keeps that, under its own source -- a date, perhaps,
    # which is read as the start of that day. One that said nothing has nothing
    # to carry, and an empty row keyed by a batch label ("both") is exactly the
    # pseudo-source that made the lookup ambiguous, so any such row is dropped.
    conn.execute("DELETE FROM batch_coverage WHERE covered_through = ''")
    conn.execute(
        "INSERT INTO batch_coverage (batch_id, source, covered_through)"
        " SELECT id, source, covered_through FROM suggestion_batch b"
        " WHERE covered_through <> '' AND NOT EXISTS"
        " (SELECT 1 FROM batch_coverage c WHERE c.batch_id = b.id)")


def connect(path, init=False):
    """Open a connection. Pass init=True once at startup to apply the schema.

    One connection per request thread; SQLite in WAL mode handles that well and
    it keeps the server free of cross-thread connection sharing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        conn.executescript(SCHEMA)
        migrate(conn)
    return conn


def next_position(conn, table, folder_id=None):
    """Append position: one past the current maximum."""
    if folder_id is None:
        row = conn.execute(f"SELECT MAX(position) AS m FROM {table}").fetchone()
    else:
        row = conn.execute(
            f"SELECT MAX(position) AS m FROM {table} WHERE folder_id = ?", (folder_id,)
        ).fetchone()
    return (row["m"] or 0.0) + 1.0


def midpoint(conn, table, row_id, after_id, folder_id=None):
    """Position that places row_id directly after after_id (None = first).

    Positions are REAL so a drag inserts between neighbours without renumbering
    the table. If neighbours get too close to separate, renormalise first.
    """
    where = ""
    params = []
    if folder_id is not None:
        where = "WHERE folder_id = ?"
        params.append(folder_id)
    rows = conn.execute(
        f"SELECT id, position FROM {table} {where} ORDER BY position", params
    ).fetchall()
    order = [r["id"] for r in rows if r["id"] != row_id]
    pos = {r["id"]: r["position"] for r in rows}

    idx = 0 if after_id is None else order.index(after_id) + 1
    before = pos[order[idx - 1]] if idx > 0 else None
    after = pos[order[idx]] if idx < len(order) else None

    if before is None and after is None:
        return 1.0
    if before is None:
        return after - 1.0
    if after is None:
        return before + 1.0
    if after - before < 1e-6:
        _renormalise(conn, table, order, folder_id)
        return midpoint(conn, table, row_id, after_id, folder_id)
    return (before + after) / 2.0


def _renormalise(conn, table, order, folder_id):
    for i, rid in enumerate(order, start=1):
        conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (float(i), rid))
