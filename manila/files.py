"""Copying imported documents into the project.

A document dropped on the Repository is *copied*, not referenced. The original
can then be moved, renamed, or deleted and the project still has it -- which is
the whole reason to import rather than link.

Files land under the project's own directory:

    <project>/files/<folder_id>/<n>-<safe name>

so a project directory stays the complete backup, and the files stay browsable
in a normal file manager rather than buried in a database blob.
"""

import mimetypes
import re
import unicodedata
from pathlib import Path

# Generous but finite. The point is to stop a mistake (or a runaway upload)
# from filling the disk, not to police what you keep.
MAX_BYTES = 256 * 1024 * 1024

# What the browser is allowed to render inline rather than download. Everything
# else is sent as an attachment: an HTML file served inline from this origin
# could script against the app, and nothing here needs that.
INLINE_TYPES = {
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "image/svg+xml", "image/bmp", "image/tiff",
    "text/plain", "text/markdown",
}
# Deliberately not inline: a CSV shown as raw text in a browser tab is useless.
# As an attachment it lands in the spreadsheet app, which is the point.

IMAGE_PREFIX = "image/"

# Windows refuses these as filenames whatever the extension: `NUL.txt` is the
# null device, not a document. They are legal names on Linux, so a project
# directory has to stay openable on either platform -- an underscore keeps the
# name readable and makes the file real everywhere.
RESERVED = {"con", "prn", "aux", "nul",
            *(f"com{n}" for n in range(1, 10)),
            *(f"lpt{n}" for n in range(1, 10))}


def guess_mime(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def is_image(mime):
    return mime.startswith(IMAGE_PREFIX)


def safe_name(name):
    """A filename safe to join onto a path, preserving what it was called.

    Strips directory separators and anything that could climb out of the folder;
    keeps the visible name recognisable so the files directory stays browsable.
    """
    name = unicodedata.normalize("NFKC", (name or "").strip())
    name = name.replace("\\", "/").split("/")[-1]        # no directory parts
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)          # no control characters
    name = re.sub(r'[<>:"|?*]', "_", name)               # awkward on Windows
    name = name.strip(". ")                              # no leading/trailing dots
    if not name or name in {".", ".."}:
        name = "document"
    if name.partition(".")[0].lower() in RESERVED:
        name = "_" + name
    stem, dot, suffix = name.rpartition(".")
    if dot and len(suffix) <= 12:
        return f"{stem[:96]}.{suffix}"
    return name[:110]


def folder_dir(project, folder_id):
    return project.files_dir / str(int(folder_id))


def store(project, folder_id, name, data):
    """Copy bytes into the project. Returns (stored name, mime, size).

    The stored name is prefixed with a counter so two documents that arrive
    with the same name never overwrite each other.
    """
    if len(data) > MAX_BYTES:
        raise ValueError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise ValueError("file is empty")

    visible = safe_name(name)
    directory = folder_dir(project, folder_id)
    directory.mkdir(parents=True, exist_ok=True)

    n = 1
    while (directory / f"{n}-{visible}").exists():
        n += 1
    target = directory / f"{n}-{visible}"

    # Write to a temporary neighbour first so a failed upload never leaves a
    # half-written file that looks like a real document.
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    partial.replace(target)
    return target.name, guess_mime(visible), len(data)


def resolve(project, folder_id, stored):
    """The path of a stored file, refusing anything outside the folder."""
    directory = folder_dir(project, folder_id).resolve()
    target = (directory / stored).resolve()
    if not target.is_relative_to(directory):
        raise ValueError("file path escapes the project")
    return target


def discard(project, folder_id, stored):
    """Delete one stored file. A missing file is not an error -- the row goes
    either way, and a dangling row is worse than a dangling file."""
    try:
        resolve(project, folder_id, stored).unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def discard_folder(project, folder_id):
    """Remove everything a deleted folder had copied into the project.

    Called after the rows are gone, so a failure here leaves orphaned files
    rather than rows pointing at nothing -- the harmless direction.
    """
    directory = folder_dir(project, folder_id)
    try:
        resolved = directory.resolve()
        if not resolved.is_relative_to(project.files_dir.resolve()):
            return
        for child in sorted(resolved.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        resolved.rmdir()
    except (OSError, ValueError):
        pass


def disposition(mime, name):
    kind = "inline" if mime in INLINE_TYPES else "attachment"
    quoted = name.replace('"', "")
    return f'{kind}; filename="{quoted}"'
