"""Projects: independent bodies of work, served from one place.

A project is a directory holding its own database. Projects share nothing --
separate files, separate history, separate everything -- so one can live on an
encrypted volume and another in a synced folder, and copying a project's
directory copies the whole project.

One Manila process serves all of them from a single port. The picker at `/`
chooses which one you are looking at; the server opens that project's database
and no other while serving the request.

Two names matter and they are deliberately different things:

    id    a slug, fixed at creation, used in the URL and the directory name
    name  free text, yours to change whenever, shown everywhere in the UI

Renaming a project changes only `name`. Bookmarks, paths, and anything else
holding an id keep working, which is the whole reason the two are separate.

The registry naming them lives at:

    ~/.config/manila/projects.json
"""

import json
import os
import re
from pathlib import Path

APP = "manila"

# Assigned in turn to new projects so two of them never look alike at a glance.
# A color is a label, not content -- change it whenever, it means nothing.
PALETTE = [
    "#8a6a35",  # manila
    "#3d6b8a",  # slate blue
    "#6b7f4a",  # olive
    "#8a4a52",  # brick
    "#5c5a8a",  # iris
    "#3f7a72",  # teal
]


WINDOWS = os.name == "nt"


def _xdg(variable, default):
    return Path(os.environ.get(variable) or Path.home() / default).expanduser()


def _windows_base(variable, fallback):
    return Path(os.environ.get(variable) or Path.home() / "AppData" / fallback)


def data_home():
    """Where projects go when you don't choose somewhere yourself."""
    if WINDOWS:
        return _windows_base("LOCALAPPDATA", "Local") / "Manila"
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def config_home():
    if WINDOWS:
        return _windows_base("APPDATA", "Roaming") / "Manila"
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def registry_path():
    return config_home() / "projects.json"


def legacy_registry_path():
    return config_home() / "workspaces.json"


def places():
    """Sensible starting points for the "where should this live" browser.

    Backed-up folders first, because that is usually the reason for choosing a
    location at all. Only what actually exists is offered.
    """
    home = Path.home()
    found, seen = [], set()

    def offer(label, path):
        try:
            path = Path(path).expanduser()
            if path.is_dir() and str(path) not in seen:
                seen.add(str(path))
                found.append({"label": label, "path": str(path)})
        except (OSError, RuntimeError):
            pass

    offer("Home", home)
    for name in ("Documents", "Desktop"):
        offer(name, home / name)
    # Anything synced is a backup by another name; these are the common ones on
    # both platforms, and each is skipped unless it is really there.
    for entry in sorted(home.iterdir()) if home.is_dir() else []:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        lowered = entry.name.lower()
        if lowered.startswith("onedrive") or lowered in {
                "dropbox", "google drive", "googledrive", "nextcloud",
                "icloud drive", "icloudrive", "sync", "box", "proton drive"}:
            offer(entry.name, entry)

    if WINDOWS:
        # Ask the OS which drives exist rather than trying all 26 letters: a
        # mapped drive whose share is not reachable takes seconds to answer, and
        # walking the alphabet means paying that for every letter that is free.
        try:
            drives = os.listdrives()
        except (AttributeError, OSError):        # listdrives is 3.12+
            drives = [f"{letter}:\\" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"]
        for drive in drives:
            offer(drive, drive)
    else:
        for mount in ("/mnt", "/media", Path("/media") / os.environ.get("USER", "")):
            offer(str(mount), mount)
    offer("Manila default", data_home())
    return found


def browse(where=None):
    """List the sub-directories of one directory, for choosing a location.

    Directory names only -- never file contents. Everything Manila can already
    read is a project's own data; this adds no new kind of access.
    """
    target = Path(where).expanduser() if where else Path.home()
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        raise ValueError(f"cannot read {where}")
    if not target.is_dir():
        raise ValueError(f"{target} is not a directory")

    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    # Whether it is already a project, so importing can show
                    # which folder to pick rather than asking you to remember.
                    entries.append({"name": child.name, "path": str(child),
                                    "project": looks_like_project(child)})
            except OSError:
                continue
    except PermissionError:
        raise ValueError(f"no permission to read {target}")

    parent = target.parent
    return {
        "path": str(target),
        "parent": None if parent == target else str(parent),
        "sep": os.sep,
        "writable": os.access(target, os.W_OK),
        "project": looks_like_project(target),
        "entries": entries,
    }


def make_folder(where, name):
    """Create one sub-directory of `where`, for a project to be made inside.

    Only a bare name is accepted. Choosing a location is browsing, not writing
    wherever a name reaches: this puts a folder in the directory on screen and
    nowhere else.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a folder needs a name")
    # Both separators are refused on both platforms: projects move between
    # them, and a backslash that is a legal Linux filename is a path on Windows.
    if name in {".", ".."} or "\\" in name or name != Path(name).name:
        raise ValueError("a folder name cannot contain a path")
    if not where:
        raise ValueError("choose where the folder should go first")

    parent = Path(where).expanduser()
    try:
        parent = parent.resolve()
    except (OSError, RuntimeError):
        raise ValueError(f"cannot read {where}")
    if not parent.is_dir():
        raise ValueError(f"{parent} is not a directory")

    target = parent / name
    if target.exists():
        raise ValueError(f"{name!r} is already in {parent}")
    try:
        target.mkdir()
    except OSError as exc:
        raise ValueError(f"could not create {target}: {exc}")
    return {"path": str(target), "name": name}


def slugify(name):
    """A URL- and filesystem-safe id derived from the name it was created with."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or "project"


def unique_id(name, taken):
    base = slugify(name)
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


class Project:
    def __init__(self, id, name, path, color=None):
        self.id = id
        self.name = name
        self.path = Path(path).expanduser()
        self.color = color

    @property
    def db_path(self):
        return self.path / "manila.db"

    @property
    def files_dir(self):
        return self.path / "files"

    def prepare(self):
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def describe(self):
        return {"id": self.id, "name": self.name,
                "path": str(self.path), "color": self.color}

    def __repr__(self):
        return f"<Project {self.id} {self.name!r} at {self.path}>"


# --- registry ----------------------------------------------------------------

def _blank():
    return {"version": 2, "projects": []}


def load_registry():
    path = registry_path()
    if not path.exists():
        return _migrate_legacy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"could not read {path}: {exc}")
    data.setdefault("version", 2)
    data.setdefault("projects", [])
    return data


def _migrate_legacy():
    """Carry a v1 workspaces.json forward, leaving the old file in place.

    This moves existing registrations, never data, and invents no projects: an
    absent or empty legacy file yields an empty registry.
    """
    old = legacy_registry_path()
    if not old.exists():
        return _blank()
    try:
        data = json.loads(old.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return _blank()

    registry = _blank()
    for name, entry in (data.get("workspaces") or {}).items():
        registry["projects"].append({
            "id": unique_id(name, {p["id"] for p in registry["projects"]}),
            "name": name,
            "path": entry["path"],
            "color": entry.get("color") or PALETTE[len(registry["projects"]) % len(PALETTE)],
        })
    if registry["projects"]:
        save_registry(registry)
        print(f"migrated {len(registry['projects'])} project(s) from {old}")
    return registry


def save_registry(data):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written UTF-8 explicitly: the default is the locale's encoding, which on
    # Windows is a legacy code page that cannot spell every project name.
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def projects(registry=None):
    """Registered projects, in the order the picker shows them."""
    registry = registry if registry is not None else load_registry()
    return [Project(e["id"], e["name"], e["path"], e.get("color"))
            for e in registry["projects"]]


def find(project_id, registry=None):
    for project in projects(registry):
        if project.id == project_id:
            return project
    raise KeyError(f"no project {project_id!r}")


def _entry(registry, project_id):
    for entry in registry["projects"]:
        if entry["id"] == project_id:
            return entry
    raise KeyError(f"no project {project_id!r}")


def add(name, path=None, color=None, parent=None):
    """Register a project. Its directory is created; its database stays empty.

    `path` is the project directory itself; `parent` is a directory to make it
    inside, which is what choosing a location in the browser sends.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a project needs a name")
    registry = load_registry()
    taken = {p["id"] for p in registry["projects"]}
    new_id = unique_id(name, taken)

    if path:
        where = Path(path).expanduser()
    elif parent:
        where = Path(parent).expanduser() / new_id
    else:
        where = data_home() / new_id
    # Two projects sharing a directory would share a database, which is the one
    # thing projects must never do. An empty directory is not proof it is free:
    # a project's database is not written until the project is first opened.
    for other in registry["projects"]:
        if _same_place(other["path"], where):
            raise ValueError(
                f"{other['name']!r} already lives at {where}. "
                "Pick another folder, or remove that project first.")
    if where.exists() and any(where.iterdir()):
        raise ValueError(f"{where} already exists and is not empty")

    entry = {
        "id": new_id,
        "name": name,
        "path": str(where),
        "color": color or PALETTE[len(registry["projects"]) % len(PALETTE)],
    }
    registry["projects"].append(entry)
    save_registry(registry)
    return Project(**entry).prepare()


DB_NAME = "manila.db"


def looks_like_project(path):
    """Whether a directory holds a project's database.

    The database file is the whole test. A project directory is its database
    plus the documents that belong to it, so anything with one is a project,
    wherever it came from.
    """
    try:
        return (Path(path).expanduser() / DB_NAME).is_file()
    except (OSError, RuntimeError):
        return False


def _is_manila_db(path):
    """Whether that file is really one of ours, asked before registering it.

    Opened read-only and read for one table name: importing must not be a way
    to have Manila write its schema into whatever file was pointed at.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'folder'"
        ).fetchone() is not None
    except sqlite3.DatabaseError:       # not an SQLite file at all
        return False
    finally:
        conn.close()


def attach(path, name=None, color=None):
    """Register a project that already exists on disk -- a moved machine.

    A project's directory is everything it is: its database, and the documents
    copied into it, named relative to that directory. So carrying one to
    another PC is copying the folder, and this is the other half -- telling
    this machine the folder is there. Nothing is created, moved, or written
    inside it; the registry gains a line.
    """
    if not path or not str(path).strip():
        raise ValueError("choose the project's folder")
    where = Path(str(path).strip()).expanduser()
    try:
        where = where.resolve()
    except (OSError, RuntimeError):
        raise ValueError(f"cannot read {path}")
    if not where.is_dir():
        raise ValueError(f"there is no folder at {where}")

    if not looks_like_project(where):
        # Pointing at the folder the project sits in, rather than at the
        # project, is the likely mistake -- so say which ones are there.
        inside = []
        try:
            inside = sorted(c.name for c in where.iterdir()
                            if c.is_dir() and looks_like_project(c))
        except OSError:
            pass
        if inside:
            raise ValueError(
                f"{where} is not a project, but it holds "
                + ", ".join(repr(n) for n in inside[:4])
                + ". Choose one of those.")
        raise ValueError(f"no {DB_NAME} in {where}. Is that the project's folder?")

    if not _is_manila_db(where / DB_NAME):
        raise ValueError(
            f"{where / DB_NAME} is not a Manila database. Nothing was changed.")

    registry = load_registry()
    for other in registry["projects"]:
        if _same_place(other["path"], where):
            raise ValueError(f"that folder is already open here as {other['name']!r}.")

    name = (name or "").strip() or where.name
    entry = {
        "id": unique_id(name, {p["id"] for p in registry["projects"]}),
        "name": name,
        "path": str(where),
        "color": color or PALETTE[len(registry["projects"]) % len(PALETTE)],
    }
    registry["projects"].append(entry)
    save_registry(registry)
    return Project(**entry)


def _same_place(one, two):
    """Whether two paths name the same directory, before either exists.

    Compared case-insensitively on Windows, where they would collide on disk
    even when they differ as strings.
    """
    a, b = Path(one).expanduser(), Path(two).expanduser()
    try:
        a, b = a.resolve(), b.resolve()
    except (OSError, RuntimeError):
        pass
    return str(a).lower() == str(b).lower() if WINDOWS else a == b


def rename(project_id, name=None, color=None):
    """Change what a project is called. Its id, path, and data do not move."""
    registry = load_registry()
    entry = _entry(registry, project_id)
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("a project needs a name")
        entry["name"] = name
    if color is not None:
        entry["color"] = color
    save_registry(registry)
    return Project(**entry)


def reorder(project_id, after_id=None):
    """Put a project after another in the picker; after_id=None means first."""
    registry = load_registry()
    entry = _entry(registry, project_id)
    registry["projects"].remove(entry)
    if after_id is None:
        registry["projects"].insert(0, entry)
    else:
        index = next(i for i, e in enumerate(registry["projects"]) if e["id"] == after_id)
        registry["projects"].insert(index + 1, entry)
    save_registry(registry)
    return projects(registry)


def forget(project_id):
    """Drop a project from the picker. Its directory and data are left alone."""
    registry = load_registry()
    entry = _entry(registry, project_id)
    registry["projects"].remove(entry)
    save_registry(registry)
    return entry["path"]
