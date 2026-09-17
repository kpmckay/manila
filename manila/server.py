"""Manila's local web server.

Deliberately stdlib-only: http.server plus sqlite3. Manila is a single-user app
on localhost, so a framework would buy request validation it doesn't need while
costing an install step on every machine it has to run on.
"""

import json
import os
import re
import socket
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
from urllib.request import urlopen

from . import (db, files, history, net, project, richtext, schema, sources,
               suggest, window)

STATIC = Path(__file__).resolve().parent / "static"

# The types Manila's own front end must be served as, spelled out rather than
# guessed. `mimetypes` consults the registry on Windows, where any installer is
# free to have claimed `.js` as `text/plain` -- and a module script sent as
# text/plain is refused outright by the browser, so a machine with the wrong
# registry entry would show a blank page and nothing in the log to explain it.
# These few files are ours and their types are not in question.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "text/javascript; charset=utf-8",
    ".mjs":  "text/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".ico":  "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt":  "text/plain; charset=utf-8",
}

SCOPES = {
    "active":   "archived_at IS NULL",
    "archived": "archived_at IS NOT NULL",
}

# Archive parks something you may want back. Delete is permanent and takes the
# history with it -- there is no in-between state to manage.
ROW_ACTIONS = {
    "archive":   ("archived_at", history.now),
    "unarchive": ("archived_at", lambda: None),
}


class SendFile(Exception):
    """Not an error: the way a handler returns a file instead of JSON."""
    def __init__(self, path, mime, disposition):
        super().__init__(str(path))
        self.path = path
        self.mime = mime
        self.disposition = disposition


class Http(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        # Anything the caller can act on beyond the sentence. A refused batch
        # of suggestions says which ones and why, so the proposer can fix those
        # and send again rather than guess at the whole payload.
        self.extra = extra


_local = threading.local()


def current_project():
    """The project this request is for. Set by the dispatcher, per thread."""
    chosen = getattr(_local, "project", None)
    if chosen is None:
        raise Http(500, "no project bound to this request")
    return chosen


def conn_for(chosen):
    """The connection for one project, per thread.

    Projects never share a connection or a file, so a request cannot read
    another project's data even by accident -- the only handle it is given is
    the one for the project named in its URL.
    """
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    if chosen.id not in cache:
        chosen.prepare()
        cache[chosen.id] = db.connect(chosen.db_path, init=True)
    return cache[chosen.id]


# --- helpers -----------------------------------------------------------------

def scope_clause(scope):
    if scope not in SCOPES:
        raise Http(400, f"unknown scope {scope!r}")
    return SCOPES[scope]


def entity_or_400(entity):
    if entity not in schema.TABLES:
        raise Http(404, f"unknown table {entity!r}")
    return entity


def folder_row(c, folder_id):
    row = c.execute("SELECT * FROM folder WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        raise Http(404, "no such folder")
    return row


# --- API handlers ------------------------------------------------------------

def api_schema(_q, _body):
    # Source kinds come from here too, so what a pointer can be is declared in
    # one place rather than once in Python and again in JavaScript.
    return {"columns": schema.COLUMNS, "sections": schema.SECTIONS,
            "source_kinds": sources.KINDS}


# --- projects (global: these do not run against a project's database) ---------

def project_summary(chosen):
    """A picker row. A project on an unmounted volume is reported, not fatal."""
    described = chosen.describe()
    try:
        if not chosen.db_path.exists():
            return {**described, "folders": 0, "status": "empty"}
        c = db.connect(chosen.db_path)
        try:
            count = c.execute(
                "SELECT COUNT(*) AS n FROM folder WHERE archived_at IS NULL"
            ).fetchone()["n"]
        finally:
            c.close()
        return {**described, "folders": count, "status": "ok"}
    except Exception as exc:
        # Most likely the path is on a volume that isn't mounted right now.
        return {**described, "folders": None, "status": "unavailable",
                "detail": f"{type(exc).__name__}: {exc}"}


def list_projects(_q, _body):
    return {"projects": [project_summary(p) for p in project.projects()]}


def create_project(_q, body):
    created = project.add(body.get("name"), body.get("path"),
                          body.get("color"), body.get("parent"))
    return {"project": project_summary(created)}


def import_project(_q, body):
    """Register a project directory that is already there.

    The other half of copying a project to another machine: the folder carries
    the data, this tells Manila where it landed. It creates nothing and writes
    nothing inside the folder.
    """
    attached = project.attach(body.get("path"), body.get("name"), body.get("color"))
    return {"project": project_summary(attached)}


def browse_places(_q, _body):
    """Starting points for choosing where a project should live."""
    return {"places": project.places(), "default": str(project.data_home())}


def browse_directory(q, _body):
    where = (q.get("path") or [None])[0]
    return project.browse(where)


def make_directory(_q, body):
    """Make a folder while choosing where a project should live."""
    return project.make_folder(body.get("parent"), body.get("name"))


def update_project(project_id, _q, body):
    """Rename or recolor. The id, the path, and the data all stay put."""
    changed = project.rename(project_id, body.get("name"), body.get("color"))
    return {"project": project_summary(changed)}


def move_project(project_id, _q, body):
    ordered = project.reorder(project_id, body.get("after_id"))
    return {"projects": [p.describe() for p in ordered]}


def forget_project(project_id, _q, _body):
    """Remove from the picker only. Manila never deletes a project's files."""
    path = project.forget(project_id)
    return {"id": project_id, "forgotten": True, "path": path}


def list_folders(c, q, _body):
    scope = (q.get("scope") or ["active"])[0]
    rows = c.execute(
        f"SELECT * FROM folder WHERE {scope_clause(scope)} ORDER BY position"
    ).fetchall()
    # How many suggestions each folder has waiting, so the Review tab can say
    # so without the browser asking folder by folder.
    waiting = suggest.pending_counts(c)
    return {"folders": [
        {"id": r["id"], "name": r["name"], "position": r["position"],
         "archived": r["archived_at"] is not None,
         "pending": waiting.get(r["id"], 0)}
        for r in rows
    ]}


def create_folder(c, _q, body):
    name = (body.get("name") or "").strip() or "Untitled"
    cur = c.execute(
        "INSERT INTO folder (name, position, created_at) VALUES (?, ?, ?)",
        (name, db.next_position(c, "folder"), history.now()),
    )
    return {"id": cur.lastrowid, "name": name}


def rename_folder(c, folder_id, _q, body):
    folder_row(c, folder_id)
    name = (body.get("name") or "").strip()
    if not name:
        raise Http(400, "name cannot be empty")
    c.execute("UPDATE folder SET name = ? WHERE id = ?", (name, folder_id))
    return {"id": folder_id, "name": name}


def move_folder(c, folder_id, _q, body):
    folder_row(c, folder_id)
    pos = db.midpoint(c, "folder", folder_id, body.get("after_id"))
    c.execute("UPDATE folder SET position = ? WHERE id = ?", (pos, folder_id))
    return {"id": folder_id, "position": pos}


def folder_action(c, folder_id, action, _q, _body):
    folder_row(c, folder_id)
    if action == "delete":
        held = c.execute("SELECT COUNT(*) AS n FROM repo_file WHERE folder_id = ?",
                         (folder_id,)).fetchone()["n"]
        c.execute("BEGIN")
        try:
            for entity, table in schema.TABLES.items():
                ids = [r["id"] for r in c.execute(
                    f"SELECT id FROM {table} WHERE folder_id = ?", (folder_id,))]
                if ids:
                    marks = ",".join("?" * len(ids))
                    c.execute("DELETE FROM field_history"
                              f" WHERE entity_type = ? AND entity_id IN ({marks})",
                              (entity, *ids))
                    history.unsettle(c, entity, ids)
                    suggest.forget_refs(c, entity, ids)
            # repo_frame and repo_file cascade off folder_id, but rows cascading
            # would leave the copies on disk. The directory goes with them.
            c.execute("DELETE FROM folder WHERE id = ?", (folder_id,))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        files.discard_folder(current_project(), folder_id)
        return {"id": folder_id, "deleted": True, "files": held}
    if action not in ROW_ACTIONS:
        raise Http(404, f"unknown action {action!r}")
    column, value = ROW_ACTIONS[action]
    c.execute(f"UPDATE folder SET {column} = ? WHERE id = ?", (value(), folder_id))
    return {"id": folder_id, action: True}


# --- borrowed dates ----------------------------------------------------------
#
# A cell may take its date from another row -- Resolve By from a milestone --
# so a deliverable that slips carries everything waiting on it. The link is
# live: the date is read through on every request rather than copied, which is
# what keeps one slip to one edit and one history entry.

def link_source(c, spec, row_id):
    table = schema.TABLES[spec["entity"]]
    return c.execute(
        f"SELECT {spec['label']} AS label, {spec['date']} AS date"
        f" FROM {table} WHERE id = ?", (row_id,)).fetchone()


def apply_links(c, entity, fields):
    """Read a borrowed date through to its source, for display."""
    for col in schema.columns(entity):
        spec = col.get("link")
        value = fields.get(col["key"]) if spec else None
        if not isinstance(value, dict) or not value.get("link"):
            continue
        source = link_source(c, spec, value["link"])
        if source is None:
            value["link"] = 0        # source is gone; the stored date stands
            continue
        value["date"] = source["date"] or ""
        value["link_label"] = source["label"]
    return fields


def resolve_incoming(c, entity, field, value):
    """Stamp an incoming linked value with its source's date before it is stored.

    The stored date is then always the effective one, so unlinking, or losing
    the source entirely, never leaves the cell without a date.
    """
    spec = schema.column(entity, field).get("link")
    if not spec or not isinstance(value, dict) or not value.get("link"):
        return value
    source = link_source(c, spec, value["link"])
    if source is None:
        return {**value, "link": 0}
    return {**value, "date": source["date"] or ""}


def materialise_links(c, entity, row_id):
    """Before a row is deleted, give every cell borrowing from it its own date.

    Nothing dangles and nothing loses its meaning: an action that was due with
    a deliverable stays due on that deliverable's last date.
    """
    for other in schema.TABLES:
        for col in schema.columns(other):
            spec = col.get("link")
            if not spec or spec["entity"] != entity:
                continue
            source = link_source(c, spec, row_id)
            if source is None:
                continue
            at = schema.storage_columns(col)[0]
            c.execute(
                f"UPDATE {schema.TABLES[other]}"
                f" SET {at} = ?, {spec['column']} = NULL, {spec['since']} = ''"
                f" WHERE {spec['column']} = ?",
                (source["date"] or "", row_id))


def link_options(c, folder_id, entity, field, _q, _body):
    """What this cell's date can be borrowed from, in this folder."""
    entity_or_400(entity)
    spec = schema.column(entity, field).get("link")
    if not spec:
        raise Http(400, f"{field} does not borrow its value")
    rows = c.execute(
        f"SELECT id, {spec['label']} AS label, {spec['date']} AS date"
        f" FROM {schema.TABLES[spec['entity']]}"
        " WHERE folder_id = ? AND archived_at IS NULL ORDER BY position",
        (folder_id,)).fetchall()
    return {"options": [dict(r) for r in rows if (r["label"] or "").strip()]}


def list_rows(c, folder_id, entity, q, _body):
    entity_or_400(entity)
    folder_row(c, folder_id)
    scope = (q.get("scope") or ["active"])[0]
    table = schema.TABLES[entity]
    rows = c.execute(
        f"SELECT * FROM {table} WHERE folder_id = ? AND {scope_clause(scope)}"
        " ORDER BY position",
        (folder_id,),
    ).fetchall()
    counts = history.history_counts(c, entity, [r["id"] for r in rows])
    shaped = [history.row_to_dict(entity, r, counts) for r in rows]
    for row in shaped:
        apply_links(c, entity, row["fields"])
        add_inherited_counts(c, entity, row)
    return {"rows": shaped}


def add_inherited_counts(c, entity, shaped):
    """A borrowed cell's chip counts its source's changes too."""
    for col in schema.columns(entity):
        if not col.get("link"):
            continue
        borrowed = history.inherited_history(c, entity, shaped["id"], col)
        if borrowed:
            shaped["history_counts"][col["key"]] = (
                shaped["history_counts"].get(col["key"], 0) + len(borrowed))


def create_row(c, folder_id, entity, _q, body):
    entity_or_400(entity)
    folder_row(c, folder_id)
    table = schema.TABLES[entity]
    cur = c.execute(
        f"INSERT INTO {table} (folder_id, position, created_at) VALUES (?, ?, ?)",
        (folder_id, db.next_position(c, table, folder_id), history.now()),
    )
    row_id = cur.lastrowid
    # A new row's first value is the value it was born with, not a change, so it
    # is written directly and no history record is created for it.
    field, value = body.get("field"), body.get("value")
    if field:
        col = schema.column(entity, field)
        value = resolve_incoming(c, entity, field, value)
        updates = history.to_storage(col, history.coerce(col, value))
        c.execute(
            f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
            (*updates.values(), row_id),
        )
        history.born(c, entity, row_id, [field])
    row = c.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    shaped = history.row_to_dict(entity, row, {})
    apply_links(c, entity, shaped["fields"])
    return {"row": shaped}


def update_row(c, entity, row_id, _q, body):
    entity_or_400(entity)
    field = body.get("field")
    if not field:
        raise Http(400, "field is required")
    col = schema.column(entity, field)
    incoming = resolve_incoming(c, entity, field, body.get("value"))
    changed, value = history.update_field(c, entity, row_id, field, incoming)
    apply_links(c, entity, {field: value})
    counts = history.history_counts(c, entity, [row_id])
    total = counts.get((row_id, field), 0)
    total += len(history.inherited_history(c, entity, row_id, col))
    return {"changed": changed, "value": value, "history_count": total}


def move_row(c, entity, row_id, _q, body):
    entity_or_400(entity)
    table = schema.TABLES[entity]
    row = c.execute(f"SELECT folder_id FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise Http(404, "no such row")
    pos = db.midpoint(c, table, row_id, body.get("after_id"), row["folder_id"])
    c.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, row_id))
    return {"id": row_id, "position": pos}


def row_action(c, entity, row_id, action, _q, _body):
    entity_or_400(entity)
    table = schema.TABLES[entity]
    if c.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is None:
        raise Http(404, "no such row")
    if action == "delete":
        c.execute("BEGIN")
        try:
            # Anything borrowing this row's date keeps that date as its own.
            materialise_links(c, entity, row_id)
            c.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            c.execute(
                "DELETE FROM field_history WHERE entity_type = ? AND entity_id = ?",
                (entity, row_id),
            )
            history.unsettle(c, entity, [row_id])
            suggest.forget_refs(c, entity, [row_id])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return {"id": row_id, "deleted": True}
    if action not in ROW_ACTIONS:
        raise Http(404, f"unknown action {action!r}")
    column, value = ROW_ACTIONS[action]
    c.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value(), row_id))
    # Bringing something back means it needs attention again, so it comes back
    # unticked. Archive is where finished things go; the active list is not.
    if action == "unarchive":
        for col in schema.columns(entity):
            if col["type"] == "check":
                c.execute(f"UPDATE {table} SET {col['key']} = 0 WHERE id = ?", (row_id,))
    return {"id": row_id, action: True}


# --- the repository ----------------------------------------------------------

FRAME_KINDS = ("text", "files", "image")

# What a frame lets you change. Geometry is written on every drag, so it is
# saved plainly and never touches field_history: a note's position is not a
# revision, and neither is a working draft of its text.
FRAME_FIELDS = {"title": str, "body": str, "view": str,
                "x": float, "y": float, "w": float, "h": float, "z": float}

# How a note is written. Plain is a text box holding text. Rich is an editor
# holding a small, sanitised subset of HTML -- and `body` holds whichever of
# those the frame is currently in.
FRAME_VIEWS = ("plain", "rich")


def frame_row(c, frame_id):
    row = c.execute("SELECT * FROM repo_frame WHERE id = ?", (frame_id,)).fetchone()
    if row is None:
        raise Http(404, "no such frame")
    return row


def frame_to_dict(c, row):
    out = {k: row[k] for k in ("id", "folder_id", "kind", "title", "body",
                               "view", "x", "y", "w", "h", "z")}
    out["files"] = [file_to_dict(f) for f in c.execute(
        "SELECT * FROM repo_file WHERE frame_id = ? ORDER BY position, id",
        (row["id"],))]
    return out


def file_to_dict(row):
    chosen = current_project()
    return {"id": row["id"], "name": row["name"], "mime": row["mime"],
            "size": row["size"], "description": row["description"],
            "is_image": files.is_image(row["mime"]),
            "url": f"/api/p/{chosen.id}/files/{row['id']}",
            # The real path, so a document can also be opened natively when you
            # are sitting at the machine holding it.
            "path": str(files.folder_dir(chosen, row["folder_id"]) / row["stored"])}


def list_frames(c, folder_id, _q, _body):
    folder_row(c, folder_id)
    rows = c.execute("SELECT * FROM repo_frame WHERE folder_id = ? ORDER BY z, id",
                     (folder_id,)).fetchall()
    return {"frames": [frame_to_dict(c, r) for r in rows]}


def create_frame(c, folder_id, _q, body):
    folder_row(c, folder_id)
    kind = body.get("kind")
    if kind not in FRAME_KINDS:
        raise Http(400, f"kind must be one of {', '.join(FRAME_KINDS)}")
    top = c.execute("SELECT MAX(z) AS m FROM repo_frame WHERE folder_id = ?",
                    (folder_id,)).fetchone()["m"]
    cur = c.execute(
        "INSERT INTO repo_frame (folder_id, kind, title, x, y, w, h, z, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (folder_id, kind, (body.get("title") or "").strip(),
         float(body.get("x") or 0), float(body.get("y") or 0),
         float(body.get("w") or 360), float(body.get("h") or 260),
         (top or 0) + 1, history.now()),
    )
    return {"frame": frame_to_dict(c, frame_row(c, cur.lastrowid))}


def update_frame(c, frame_id, _q, body):
    was = frame_row(c, frame_id)
    updates = {}
    for key, cast in FRAME_FIELDS.items():
        if key in body:
            try:
                updates[key] = cast(body[key] if body[key] is not None else cast())
            except (TypeError, ValueError):
                raise Http(400, f"{key} must be a {cast.__name__}")
    if "view" in updates and updates["view"] not in FRAME_VIEWS:
        raise Http(400, f"view must be one of {', '.join(FRAME_VIEWS)}")
    if not updates:
        raise Http(400, "nothing to update")

    # Switching between the two rewrites the note into the other form. Done
    # here rather than in the browser so it happens once, the same way, and
    # cannot be skipped.
    view = updates.get("view", was["view"])
    if "view" in updates and updates["view"] != was["view"]:
        carried = updates.get("body", was["body"])
        updates["body"] = (richtext.from_text(carried) if view == "rich"
                           else richtext.to_text(carried))
    # Whatever a rich note ends up holding, it is sanitised before it is stored.
    if view == "rich" and "body" in updates:
        try:
            updates["body"] = richtext.clean(updates["body"])
        except ValueError as exc:
            raise Http(413, str(exc)) from exc
    c.execute(f"UPDATE repo_frame SET {', '.join(f'{k} = ?' for k in updates)}"
              " WHERE id = ?", (*updates.values(), frame_id))
    return {"frame": frame_to_dict(c, frame_row(c, frame_id))}


def delete_frame(c, frame_id, _q, _body):
    """Deleting a frame deletes the copies it holds. The originals you imported
    from are somewhere else entirely and are never touched."""
    row = frame_row(c, frame_id)
    stored = c.execute("SELECT stored FROM repo_file WHERE frame_id = ?",
                       (frame_id,)).fetchall()
    c.execute("BEGIN")
    try:
        c.execute("DELETE FROM repo_file WHERE frame_id = ?", (frame_id,))
        c.execute("DELETE FROM repo_frame WHERE id = ?", (frame_id,))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    for f in stored:
        files.discard(current_project(), row["folder_id"], f["stored"])
    return {"id": frame_id, "deleted": True, "files": len(stored)}


def add_file(c, frame_id, _q, body):
    """Copy an uploaded document into the project and hang it off a frame."""
    frame = frame_row(c, frame_id)
    name = body.get("name") or "document"
    data = body.get("data")
    if not isinstance(data, (bytes, bytearray)):
        raise Http(400, "no file content")
    stored, mime, size = files.store(current_project(), frame["folder_id"], name, bytes(data))
    top = c.execute("SELECT MAX(position) AS m FROM repo_file WHERE frame_id = ?",
                    (frame_id,)).fetchone()["m"]
    cur = c.execute(
        "INSERT INTO repo_file (frame_id, folder_id, name, stored, mime, size,"
        " position, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (frame_id, frame["folder_id"], files.safe_name(name), stored, mime, size,
         (top or 0) + 1, history.now()),
    )
    row = c.execute("SELECT * FROM repo_file WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"file": file_to_dict(row)}


def update_file(c, file_id, _q, body):
    """What a document is, in your words. Free text, and optional.

    Not tracked in the cell history: the Repository is a place to keep things,
    and a note being reworded is not a revision of anything.
    """
    if c.execute("SELECT 1 FROM repo_file WHERE id = ?", (file_id,)).fetchone() is None:
        raise Http(404, "no such file")
    if "description" not in body:
        raise Http(400, "nothing to update")
    text = str(body.get("description") or "").strip()
    c.execute("UPDATE repo_file SET description = ? WHERE id = ?", (text, file_id))
    row = c.execute("SELECT * FROM repo_file WHERE id = ?", (file_id,)).fetchone()
    return {"file": file_to_dict(row)}


def delete_file(c, file_id, _q, _body):
    row = c.execute("SELECT * FROM repo_file WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise Http(404, "no such file")
    c.execute("DELETE FROM repo_file WHERE id = ?", (file_id,))
    files.discard(current_project(), row["folder_id"], row["stored"])
    return {"id": file_id, "deleted": True}


def download_file(c, file_id, _q, _body):
    """Hand the document back so the browser opens it in whatever handles it.

    Manila does not render documents. It keeps them, and gets out of the way.
    """
    row = c.execute("SELECT * FROM repo_file WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise Http(404, "no such file")
    try:
        path = files.resolve(current_project(), row["folder_id"], row["stored"])
    except ValueError as exc:
        raise Http(400, str(exc)) from exc
    if not path.is_file():
        raise Http(410, f"{row['name']} is no longer on disk")
    raise SendFile(path, row["mime"], files.disposition(row["mime"], row["name"]))


# --- where a folder's news comes from ----------------------------------------
#
# Typed here, by you, like everything else in a folder. The agent side can read
# this list -- that is the whole point of it -- and there is no route in that
# table by which it could add itself a source.

def list_sources(c, folder_id, _q, _body):
    """The pointers, and how far each source has actually been read.

    One call, because they are read together: a channel you have listed and
    never synced looks exactly like one you sync every morning until the second
    half of this is on the screen beside it.
    """
    folder_row(c, folder_id)
    return {**sources.listing(c, folder_id),
            "last_runs": suggest.last_runs(c, folder_id)}


def add_source(c, folder_id, _q, body):
    folder_row(c, folder_id)
    return {"source": sources.add(c, folder_id, body)}


def edit_source(c, source_id, _q, body):
    return {"source": sources.update(c, source_id, body)}


def remove_source(c, source_id, _q, _body):
    return sources.remove(c, source_id)



# --- suggestions -------------------------------------------------------------
#
# Two sides of one table, and the split is the point. Everything below this
# comment that decides anything is reachable only from the browser -- from you,
# at the keyboard. What an assistant can reach is the agent route table at the
# bottom of this file, which can read the folder and add to the queue and do
# nothing else. There is no route, at any address, by which something that is
# not you turns a suggestion into an edit.

def linker(c):
    """Read a borrowed date through to its source, for suggest.decide().

    The same rule the browser's own writes follow: what is stored is always the
    effective date, so unlinking later never leaves a cell without one.
    """
    return lambda entity, field, value: resolve_incoming(c, entity, field, value)


def list_suggestions(c, folder_id, q, _body):
    folder_row(c, folder_id)
    scope = (q.get("scope") or ["pending"])[0]
    return suggest.listing(c, folder_id, scope)


def decide_suggestion(c, suggestion_id, verdict, _q, body):
    """Accept or reject one. Accepting is where it stops being a suggestion.

    A `value` in the body is an edit made while reviewing -- a suggestion right
    but for one word, which is the common case. What was originally proposed is
    kept, so an accepted suggestion still says what was actually suggested.
    """
    return suggest.decide(c, suggestion_id, verdict, body.get("value"), linker(c))


def decide_suggestions(c, verdict, _q, body):
    """Accept or reject several at once -- one entry's changes from one run.

    `values` holds any edits made while reviewing, keyed by suggestion id.
    """
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise Http(400, "ids must be a non-empty list")
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        raise Http(400, "ids must be suggestion ids")
    return suggest.decide_many(c, ids, verdict, body.get("values"), linker(c))


def decide_batch(c, batch_id, verdict, _q, _body):
    return suggest.decide_batch(c, batch_id, verdict, linker(c))


def discard_batch(c, batch_id, _q, _body):
    return suggest.discard_batch(c, batch_id)


# --- what an assistant is allowed to see and say -----------------------------

def agent_folders(c, q, body):
    return list_folders(c, q, body)


def agent_folder(c, folder_id, q, _body):
    """One folder, whole: every entry, what the columns mean, what is queued.

    A single call on purpose. The proposals worth having are the ones made with
    the folder in view -- an action's current state read before it is rewritten,
    a contact list checked before a name is proposed as new -- and a shape that
    takes four calls to assemble is a shape that gets assembled badly.

    Each entry carries the messages already read into it, so deciding whether a
    thread has been handled is a check against what this call already returned
    rather than a second one against the queue.

    `brief` drops the column schema, and nothing else. Six folders read in one
    run is six copies of a description of the same three tables, which has not
    changed since the first folder and will not change before the sixth; the
    entries are what differ, and those are all still here.
    """
    folder = folder_row(c, folder_id)
    scope = (q.get("scope") or ["active"])[0]
    brief = (q.get("brief") or ["false"])[0].lower() not in ("0", "false", "no", "")
    entries = {}
    for entity in schema.TABLES:
        rows = list_rows(c, folder_id, entity, {"scope": [scope]}, {})["rows"]
        refs = suggest.refs_for(c, entity, [row["id"] for row in rows])
        for row in rows:
            held = refs.get(row["id"])
            if held:
                row["refs"] = held
        entries[entity] = rows
    out = {
        "folder": {"id": folder["id"], "name": folder["name"]},
        "sources": sources.listing(c, folder_id)["sources"],
        "entries": entries,
        "pending": suggest.pending_count(c, folder_id),
        "last_runs": suggest.last_runs(c, folder_id),
    }
    if brief:
        out["brief"] = True
    else:
        out["columns"] = schema.COLUMNS
    return out


# --- what is due --------------------------------------------------------------
#
# A report rather than a folder: everything with a deadline that falls in a
# window, from every active folder at once. Read the way the table reads it --
# a borrowed date through to its deliverable, a ticked action as finished, and
# anything archived as parked -- so the report and the screen never disagree
# about what is late.

def due_window(q):
    """(today, last day wanted, whether overdue counts) from a query string."""
    def one(key, default=""):
        return (q.get(key) or [default])[0].strip()
    try:
        today = date.fromisoformat(one("today")) if one("today") else date.today()
    except ValueError:
        raise Http(400, "today must be a date, YYYY-MM-DD")
    try:
        days = int(one("days", "0"))
    except ValueError:
        raise Http(400, "days must be a whole number")
    if not 0 <= days <= 366:
        raise Http(400, "days must be between 0 and 366")
    overdue = one("overdue", "true").lower() not in ("0", "false", "no")
    return today, today + timedelta(days=days), overdue


def due_entries(c, today, through, overdue):
    """What falls due by `through`, soonest first -- earlier too, if overdue."""
    found = []
    folders = c.execute("SELECT id, name FROM folder WHERE archived_at IS NULL"
                        " ORDER BY position").fetchall()
    for folder in folders:
        for entity in schema.TABLES:
            cols = schema.columns(entity)
            deadline = next((col for col in cols if col.get("deadline")), None)
            if deadline is None:
                continue
            title = next(col["key"] for col in cols if col["type"] != "check")
            for row in list_rows(c, folder["id"], entity, {"scope": ["active"]}, {})["rows"]:
                fields = row["fields"]
                if fields.get("done"):
                    continue
                value = fields[deadline["key"]]
                try:
                    when = date.fromisoformat(
                        value.get("date") if isinstance(value, dict) else value)
                except (TypeError, ValueError):
                    continue           # no date, or "ASAP": nothing to count
                if when > through or (when < today and not overdue):
                    continue
                days = (when - today).days
                found.append({
                    "folder": {"id": folder["id"], "name": folder["name"]},
                    "entity": entity, "id": row["id"], "title": fields[title],
                    "due": when.isoformat(), "days": days,
                    "status": "overdue" if days < 0 else "today" if days == 0 else "upcoming",
                    "fields": {k: v for k, v in fields.items() if k != "done"},
                })
    found.sort(key=lambda e: e["due"])
    return found


def due_report(today, through, overdue, entries, **extra):
    counts = {"overdue": 0, "today": 0, "upcoming": 0}
    for entry in entries:
        counts[entry["status"]] += 1
    return {"today": today.isoformat(), "through": through.isoformat(),
            "overdue_included": overdue, **extra, "counts": counts, "entries": entries}


def agent_due(c, q, _body):
    today, through, overdue = due_window(q)
    return due_report(today, through, overdue, due_entries(c, today, through, overdue))


def agent_due_everywhere(q, _body):
    """The same report across every project. One that cannot be opened -- on a
    volume that is not mounted, say -- is named rather than failing the rest."""
    today, through, overdue = due_window(q)
    entries, unavailable = [], []
    for chosen in project.projects():
        if not chosen.db_path.exists():
            continue                   # never opened: nothing can be due
        try:
            found = due_entries(conn_for(chosen), today, through, overdue)
        except Exception as exc:
            unavailable.append({**chosen.describe(),
                                "detail": f"{type(exc).__name__}: {exc}"})
            continue
        here = {"id": chosen.id, "name": chosen.name}
        entries.extend({"project": here, **entry} for entry in found)
    entries.sort(key=lambda e: e["due"])
    return due_report(today, through, overdue, entries, unavailable=unavailable)


def propose(c, folder_id, _q, body):
    """Add to the review queue. This is the only write an assistant has."""
    folder_row(c, folder_id)
    try:
        return suggest.record(c, folder_id, body, body.get("suggestions"))
    except suggest.Refused as exc:
        # Named one by one so the proposer can correct those and send again,
        # rather than being told the batch was wrong and left to guess which.
        raise Http(400, f"nothing was recorded: {exc}", problems=exc.problems) from exc


def forget_history(c, entry_id, _q, _body):
    """Delete one revision from a cell's log, leaving the cell's value alone."""
    gone = history.forget_entry(c, entry_id)
    remaining = c.execute(
        "SELECT COUNT(*) AS n FROM field_history"
        " WHERE entity_type = ? AND entity_id = ? AND field = ?",
        (gone["entity_type"], gone["entity_id"], gone["field"]),
    ).fetchone()["n"]
    return {"id": entry_id, "deleted": True, "history_count": remaining}


def suggestions(c, folder_id, entity, field, _q, _body):
    """What has already been typed into this column, in this folder.

    Offered as a picker so the same person is spelled the same way twice --
    which is what makes the column worth grouping or charting by later.
    """
    entity_or_400(entity)
    col = schema.column(entity, field)          # also whitelists the SQL below
    if not col.get("suggest"):
        raise Http(400, f"{field} does not offer suggestions")
    rows = c.execute(
        f"SELECT {field} AS value, COUNT(*) AS uses, MAX(id) AS recent"
        f" FROM {schema.TABLES[entity]}"
        f" WHERE folder_id = ? AND TRIM({field}) <> ''"
        f" GROUP BY {field} ORDER BY uses DESC, recent DESC LIMIT 50",
        (folder_id,),
    ).fetchall()
    return {"values": [r["value"] for r in rows]}


def cell_history(c, entity, row_id, field, _q, _body):
    entity_or_400(entity)
    col = schema.column(entity, field)
    return {"entries": history.full_history(c, entity, row_id, col)}


def row_history(c, entity, row_id, _q, _body):
    """Everything that has happened to one entry, newest first.

    The same log the chips read, asked a different way. Changing a cell is
    rarely a thing on its own -- you chase someone, so Last Touched moves and
    Current State is rewritten in the same breath. Per cell those are three
    logs; here they are one afternoon.
    """
    entity_or_400(entity)
    table = schema.TABLES[entity]
    if c.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is None:
        raise Http(404, "no such row")
    out = []
    for col in schema.columns(entity):
        if not schema.tracks_history(col):
            continue
        for entry in history.full_history(c, entity, row_id, col):
            out.append({**entry, "field": col["key"]})
    out.sort(key=lambda e: (e["changed_at"], e["id"]), reverse=True)
    return {"entries": out}


# --- routing -----------------------------------------------------------------

# Two tables, because the two kinds of request differ in what they may touch.
# Global routes manage the registry and never open a project's database.
GLOBAL_ROUTES = [
    ("GET",    r"^/api/schema$", api_schema),
    ("GET",    r"^/api/projects$", list_projects),
    ("GET",    r"^/api/places$", browse_places),
    ("GET",    r"^/api/browse$", browse_directory),
    ("POST",   r"^/api/browse$", make_directory),
    ("POST",   r"^/api/projects$", create_project),
    ("POST",   r"^/api/projects/import$", import_project),
    ("PATCH",  r"^/api/projects/([\w.-]+)$", update_project),
    ("POST",   r"^/api/projects/([\w.-]+)/move$", move_project),
    ("POST",   r"^/api/projects/([\w.-]+)/forget$", forget_project),
]

# Project routes are reached only through /api/p/<project-id>/... and are handed
# exactly one connection: the one for the project named in the URL.
PROJECT_ROUTES = [
    ("GET",   r"^/folders$", list_folders),
    ("POST",  r"^/folders$", create_folder),
    ("PATCH", r"^/folders/(\d+)$", rename_folder),
    ("POST",  r"^/folders/(\d+)/move$", move_folder),
    ("POST",  r"^/folders/(\d+)/(archive|unarchive|delete)$", folder_action),
    ("GET",   r"^/folders/(\d+)/rows/(\w+)$", list_rows),
    ("POST",  r"^/folders/(\d+)/rows/(\w+)$", create_row),
    ("PATCH", r"^/rows/(\w+)/(\d+)$", update_row),
    ("POST",  r"^/rows/(\w+)/(\d+)/move$", move_row),
    ("POST",  r"^/rows/(\w+)/(\d+)/(archive|unarchive|delete)$", row_action),
    ("GET",   r"^/rows/(\w+)/(\d+)/history$", row_history),
    ("GET",   r"^/rows/(\w+)/(\d+)/history/(\w+)$", cell_history),
    ("GET",   r"^/folders/(\d+)/suggest/(\w+)/(\w+)$", suggestions),
    ("GET",   r"^/folders/(\d+)/links/(\w+)/(\w+)$", link_options),
    ("POST",  r"^/history/(\d+)/delete$", forget_history),

    ("GET",   r"^/folders/(\d+)/sources$", list_sources),
    ("POST",  r"^/folders/(\d+)/sources$", add_source),
    ("PATCH", r"^/sources/(\d+)$", edit_source),
    ("POST",  r"^/sources/(\d+)/delete$", remove_source),

    ("GET",   r"^/folders/(\d+)/suggestions$", list_suggestions),
    ("POST",  r"^/suggestions/(\d+)/(accept|reject)$", decide_suggestion),
    ("POST",  r"^/suggestions/(accept|reject)$", decide_suggestions),
    ("POST",  r"^/batches/(\d+)/(accept|reject)$", decide_batch),
    ("POST",  r"^/batches/(\d+)/delete$", discard_batch),

    ("GET",    r"^/folders/(\d+)/frames$", list_frames),
    ("POST",   r"^/folders/(\d+)/frames$", create_frame),
    ("PATCH",  r"^/frames/(\d+)$", update_frame),
    ("POST",   r"^/frames/(\d+)/delete$", delete_frame),
    ("POST",   r"^/frames/(\d+)/files$", add_file),
    ("GET",    r"^/files/(\d+)$", download_file),
    ("PATCH",  r"^/files/(\d+)$", update_file),
    ("POST",   r"^/files/(\d+)/delete$", delete_file),
]

# What an assistant may ask for, and the whole of it.
#
# This table is the fence. It holds no route that writes a content table, no
# route that decides a suggestion, and no route that deletes anything -- not by
# convention but because those routes are not in it, and a request under
# /api/agent/ is matched against this table and no other. An assistant can read
# a folder and add to its review queue. Everything else needs you.
#
# That is a fence around the protocol, not around the machine: anything running
# as you could open the database directly. What it buys is that a model cannot
# express a write, however it is asked to -- there is no address for one.
AGENT_ROUTES = [
    ("GET",  r"^/folders$", agent_folders),
    ("GET",  r"^/folders/(\d+)$", agent_folder),
    ("GET",  r"^/folders/(\d+)/suggestions$", list_suggestions),
    ("POST", r"^/folders/(\d+)/suggestions$", propose),
    ("GET",  r"^/rows/(\w+)/(\d+)/history$", row_history),
    ("GET",  r"^/due$", agent_due),
]

AGENT_GLOBAL = [
    ("GET", r"^/api/agent/projects$", list_projects),
    ("GET", r"^/api/agent/due$", agent_due_everywhere),
]

AGENT = re.compile(r"^/api/agent/p/([\w.-]+)(/.*)$")
SCOPED = re.compile(r"^/api/p/([\w.-]+)(/.*)$")
COMPILED_GLOBAL = [(m, re.compile(p), fn) for m, p, fn in GLOBAL_ROUTES]
COMPILED_PROJECT = [(m, re.compile(p), fn) for m, p, fn in PROJECT_ROUTES]
COMPILED_AGENT = [(m, re.compile(p), fn) for m, p, fn in AGENT_ROUTES]
COMPILED_AGENT_GLOBAL = [(m, re.compile(p), fn) for m, p, fn in AGENT_GLOBAL]


# --- who is allowed to talk to this server ------------------------------------
#
# Manila has no login, so what keeps another website out of your projects is the
# browser's own idea of who is asking. Two of its rules need help from this end.
#
# Any page you happen to have open can post a form to 127.0.0.1:8000. No
# permission is asked and no preflight is sent, so an unguarded delete runs on
# behalf of a site that has nothing to do with Manila. `Origin` says who asked;
# a write from anyone but Manila's own pages is refused.
#
# And a name its owner controls can be pointed at 127.0.0.1 -- DNS rebinding --
# after which the browser believes their page and this server are the same
# origin, and lets them read every answer. Nothing in the request gives that
# away except `Host`, which still carries the name that was typed. So the names
# this server answers to are listed, and anything else is refused.
#
# Neither check touches a request with no browser headers at all: curl, the MCP
# server and anything else scripted carry no Origin and no Sec-Fetch-Site, and
# are not what either rule is about.

LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}

# A tailnet name always ends here, and the suffix is Tailscale's to hand out --
# so it cannot be the name an attacker rebinds. Allowed only when Manila is
# actually serving a tailnet address, which is when MagicDNS is how you reach it.
TAILNET_SUFFIX = ".ts.net"

WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")


def split_host(value):
    """("host", "port") from a Host header or an origin's authority.

    Handles the bracketed IPv6 form, and a bare name with no port at all.
    """
    text = (value or "").strip().lower()
    if text.startswith("["):                       # [::1]:8000
        address, _, rest = text[1:].partition("]")
        return address, rest.lstrip(":")
    host, colon, port = text.rpartition(":")
    if colon and port.isdigit():
        return host, port
    return text, ""


class Allowed:
    """The addresses this server answers to, decided once at bind time."""

    def __init__(self, bound_host, port, extra=()):
        self.port = str(port)
        self.tailnet = net.in_tailnet(bound_host)
        self.names = set(LOCAL_NAMES)
        # 0.0.0.0 is not a name anything is reached by; the addresses it covers
        # are, so the machine's own names have to be listed instead.
        if bound_host and bound_host != net.ANY:
            self.names.add(bound_host.lower())
        for name in (socket.gethostname(), *extra):
            if name:
                self.names.add(str(name).strip().lower().rstrip("."))
        if bound_host == net.ANY:
            # Bound to everything on purpose: the Host check cannot say which
            # names are legitimate, so it only holds the port and the shape.
            self.names.add("*")

    def says(self, value):
        """Whether a Host header or origin authority names this server."""
        host, port = split_host(value)
        if not host or (port and port != self.port):
            return False
        if "*" in self.names:
            return True
        if host in self.names:
            return True
        return self.tailnet and host.endswith(TAILNET_SUFFIX)


class Handler(BaseHTTPRequestHandler):
    server_version = "Manila"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # the access log is noise for a single-user local app

    def handle_one_request(self):
        """A client that hangs up, or speaks something other than HTTP, is not a
        server fault. On a tailnet address a stray probe is routine, and a stack
        trace per probe buries anything that actually matters."""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    # -- plumbing --

    @property
    def allowed(self):
        """The names this server answers to.

        Built from the bound address if nothing set one, so a server put
        together by hand is guarded too -- the check is not something a caller
        can forget to switch on.
        """
        settled = getattr(self.server, "allowed", None)
        if settled is None:
            host, port = self.server.server_address[:2]
            settled = self.server.allowed = Allowed(host, port)
        return settled

    def check_caller(self, method):
        """Refuse what the browser's own rules cannot refuse for us.

        Read the two comments above `Allowed`: this is the enforcement, and it
        happens before any route is matched, so no handler has to remember it.
        """
        allowed = self.allowed
        if not allowed.says(self.headers.get("Host")):
            raise Http(403, "that host name is not one Manila answers to")
        if method not in WRITE_METHODS:
            return

        origin = self.headers.get("Origin")
        if origin is not None:
            # A file:// page and a sandboxed frame both send "null", which is
            # nobody: there is no origin that could be Manila's own.
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or not allowed.says(parsed.netloc):
                raise Http(403, "that change came from another site, so it was "
                                "refused. Nothing was written.")
            return
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            raise Http(403, "that change came from another site, so it was "
                            "refused. Nothing was written.")

    def dispatch(self, method):
        url = urlparse(self.path)
        path = url.path
        try:
            self.check_caller(method)
            if path.startswith("/api/"):
                self.serve_api(method, path, parse_qs(url.query))
            elif method == "GET":
                self.serve_static(path)
            else:
                raise Http(405, "method not allowed")
        except SendFile as send:
            self.send_file(send)
        except Http as exc:
            self.send_json({"error": exc.message, **exc.extra}, exc.status)
        except KeyError as exc:
            self.send_json({"error": exc.args[0]}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:  # a local tool should say what broke, loudly
            import traceback
            traceback.print_exc()
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def serve_api(self, method, path, query):
        # Checked first, and matched against its own table: what arrives under
        # /api/agent/ can never fall through to a route that writes.
        agent = AGENT.match(path)
        if agent:
            project_id, rest = agent.groups()
            self.run(COMPILED_AGENT, method, rest, path, query,
                     lead=(conn_for(self.bind(project_id)),))
            return
        if path.startswith("/api/agent/"):
            self.run(COMPILED_AGENT_GLOBAL, method, path, path, query)
            return

        scoped = SCOPED.match(path)
        if scoped:
            project_id, rest = scoped.groups()
            self.run(COMPILED_PROJECT, method, rest, path, query,
                     lead=(conn_for(self.bind(project_id)),))
            return
        self.run(COMPILED_GLOBAL, method, path, path, query)

    def bind(self, project_id):
        """The project this request is for, bound to this thread."""
        try:
            chosen = project.find(project_id)
        except KeyError:
            raise Http(404, f"no project {project_id!r}")
        _local.project = chosen
        return chosen

    def run(self, table, method, path, original, query, lead=()):
        allowed = False
        for verb, pattern, fn in table:
            match = pattern.match(path)
            if not match:
                continue
            allowed = True
            if verb != method:
                continue
            args = [int(a) if a.isdigit() else a for a in match.groups()]
            self.send_json(fn(*lead, *args, query, self.read_body()))
            return
        if allowed:
            raise Http(405, f"{method} not allowed on {original}")
        raise Http(404, f"no route for {method} {original}")

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > files.MAX_BYTES:
            raise Http(413, f"file is larger than {files.MAX_BYTES // (1024 * 1024)} MB")
        if not length:
            return {}
        raw = self.read_exactly(length)
        # A document is posted as its own bytes with the name in a header --
        # far less machinery than multipart, and nothing here needs the rest.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            # The header is percent-encoded so it survives a byte-limited HTTP
            # header; decode it here or every space arrives as %20.
            sent = self.headers.get("X-Filename") or "document"
            return {"data": raw, "name": unquote(sent)}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Http(400, f"invalid JSON body: {exc}") from exc

    def read_exactly(self, length):
        """rfile.read can return short; a truncated upload must not be stored."""
        chunks, remaining = [], length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                raise Http(400, "upload ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def serve_static(self, path):
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC) or not target.is_file():
            raise Http(404, "not found")
        kind = STATIC_TYPES.get(target.suffix.lower()) or "application/octet-stream"
        self.send_bytes(target.read_bytes(), kind)

    def page_headers(self):
        """What the app's own page is allowed to do, which is very little.

        Everything Manila loads is served from here -- no CDN, no analytics, no
        embedded anything -- so the policy can say exactly that, and a script
        that ever did get into a note would have nowhere to send what it read.
        `frame-ancestors` keeps the page out of somebody else's frame, where
        your clicks would be theirs to aim.
        """
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         # Set on elements by the editor's own toolbar.
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self'; font-src 'self'; object-src 'none'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_file(self, send):
        body = send.path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", send.mime)
        self.send_header("Content-Disposition", send.disposition)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Stored documents are opaque bytes someone imported; never let one
        # execute against this origin.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200):
        self.send_bytes(json.dumps(payload).encode(), "application/json", status)

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # so a reload picks up edits
        self.page_headers()
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    # SO_REUSEADDR means opposite things on the two platforms. On Unix it lets
    # a restart reclaim a port still in TIME_WAIT, which is what we want. On
    # Windows it lets a *second* live server bind a port another process is
    # already listening on, and which of the two gets a given connection is
    # anyone's guess -- so a second Manila would silently half-work. Refusing
    # to reuse there turns that into a clean "already running", which is also
    # how the launcher tells a fresh start from a second double-click.
    allow_reuse_address = os.name != "nt"


def already_serving(host, port):
    """Whether a Manila is answering here already.

    Asked only after a bind has failed, to tell "my other window has it" from
    "something else owns this port" -- two situations with different answers.
    """
    try:
        with urlopen(f"http://{host}:{port}/api/schema", timeout=2) as response:
            return "columns" in json.loads(response.read())
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def create(host="127.0.0.1", port=8000, allow_hosts=()):
    """Bind the port and announce what is being served, returning the server.

    Returns None if a Manila is already answering there -- the ordinary result
    of launching it twice, and the caller's cue to open the window that already
    exists rather than report a port clash. Split out from `serve` so a caller
    that has its own event loop -- the Windows tray icon -- can own the server
    and shut it down on request.

    `allow_hosts` adds names this server should answer to, for a setup that
    reaches it by some name of its own. See `Allowed`.
    """
    known = project.projects()
    try:
        httpd = Server((host, port), Handler)
    except OSError as exc:
        if already_serving(host, port):
            print(f"Manila is already running -> {net.reach(host, port)}")
            return None
        raise SystemExit(f"manila: cannot serve on {host}:{port} -- {exc}")
    # The port actually bound, which is the one to type. They differ only when
    # port 0 was asked for and the OS chose -- but a banner naming a port
    # nothing is listening on would be worse than useless.
    # Decided from the port actually bound, so a server on port 0 still knows
    # which Host headers name it.
    httpd.allowed = Allowed(host, httpd.server_address[1], allow_hosts)
    print(f"Manila -> {net.reach(host, httpd.server_address[1])}")
    if known:
        print(f"  {len(known)} project(s): " + ", ".join(p.name for p in known))
    else:
        print("  no projects yet - create the first one in the browser")
    if host != net.LOOPBACK:
        # Manila has no login, so anything that can route to this address can
        # read and edit every project. Say so rather than let it be a surprise.
        where = "your tailnet" if net.in_tailnet(host) else "every network this machine is on"
        print(f"  open to: {where} - no password, anyone who reaches it can edit")
    return httpd


def stop(httpd):
    """Stop serving, and give the port back.

    shutdown() only ends the accept loop. The listening socket stays open, so
    the port stays bound for as long as the process lives -- and on Windows,
    where Manila deliberately refuses to reuse an address, the next Manila to
    start then finds the port taken by a server that is no longer answering and
    refuses to start at all.

    Stopping is safe at any moment: every edit is committed before its response
    is sent, so there is never anything buffered to lose.
    """
    httpd.shutdown()
    httpd.server_close()


def serve(host="127.0.0.1", port=8000, open_browser=False, prefer_window=True,
          allow_hosts=()):
    """One server, one port, every project. The picker at / chooses between them."""
    httpd = create(host, port, allow_hosts)
    if open_browser:
        # After the bind, so the browser never races the server and lands on a
        # connection refused. Also the right thing when one is already up.
        window.open_manila(net.reach(host, port), prefer_window)
    if httpd is None:
        return
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        # Ctrl-C leaves by here too, and a port held by a dead server is the
        # thing that makes the next start fail for no visible reason.
        httpd.server_close()
