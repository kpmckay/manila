"""Where a folder's news arrives.

Manila never reads a mailbox or a Teams channel. What it can do is remember
which ones a folder is about, so an assistant that *can* read them is told
where to look instead of guessing from the folder's name -- and so that two
two people's idea of what a folder covers is one list you can see rather than
a sentence in a prompt somewhere.

What goes in here is pointers and nothing else: a team and channel id, a chat
id, an address, a domain. Not keywords. The distinction is what keeps the list
honest. Ids change perhaps twice a year and you notice when they do, because
the sync comes back empty. Keywords change every week, and a stale keyword
list fails quietly -- it keeps working, just on last quarter's vocabulary. The
words worth searching for are already written down in the folder's own Items
and Waiting On cells, where they are kept current by being used, so that is
where they should be read from.

Nothing here is written by an assistant. The agent route table can read this
list and cannot add to it, the same as everything else in a folder.
"""

import re
import urllib.parse

from . import db, history

# What a pointer can be. Each kind names the one field that identifies it;
# `within` is filled only where the id is not enough on its own, since a Graph
# channel id means nothing without the team that holds it. `add` and `help` are
# what the box asks for when you are adding one, which is a different question
# from what the value is called once it is stored.
KINDS = {
    "teams_channel": {
        "label": "Teams channel",
        "ref": "channel id",
        "within": "team id",
        "add": "paste the channel link",
        "help": "In Teams, right-click the channel and choose Get link to "
                "channel. The channel id, the team that holds it, and the "
                "channel's own name all come out of that one link.",
    },
    "teams_chat": {
        "label": "Teams chat",
        "ref": "chat id",
        "add": "chat id, or the chat's web address",
        "help": "Chats have no copy-link. Open the chat at teams.microsoft.com "
                "and copy the address bar -- the 19:... in it is the chat id.",
    },
    "email_folder": {
        "label": "Mail folder",
        "ref": "folder path",
        "add": "Inbox/Clients/Acme",
        "help": "Mail you have filed into this Outlook folder, or into any "
                "folder beneath it. Write the path as the folder pane shows "
                "it, from the top, with / between the levels.",
    },
    "email_address": {
        "label": "Email address",
        "ref": "address",
        "add": "someone@example.com",
        "help": "Mail from this person, wherever in their organization they sit.",
    },
    "email_domain": {
        "label": "Email domain",
        "ref": "domain",
        "add": "example.com",
        "help": "Mail from anyone at this domain.",
    },
}

# Addresses and domains are matched by whoever reads the mailbox, and no mail
# system treats case as meaningful in either. Ids are opaque and are left
# exactly as they were pasted.
FOLDED = ("email_address", "email_domain")

# A mail folder keeps the case it was typed in, because that is how it is
# shown back to you, but Outlook will not hold "Acme" and "acme" side by
# side, so two paths differing only in case are the same folder.
CASELESS = ("email_folder",)


# --- reading a pointer out of the link you were given -------------------------
#
# Nobody has a channel id. What they have is the link Teams hands out, which is
# where that id is written down in a form you can copy -- percent-encoded, with
# the team id beside it as `groupId` and the channel's name in between. Asking
# for the link is asking for the thing that actually exists; asking for the two
# ids is asking someone to do a URL decode by hand, and getting `19%3a...` in
# the box instead, which matches nothing and says nothing about why.

CHANNEL_IN_LINK = re.compile(r"/l/channel/([^/?#]+)(?:/([^/?#]*))?")
CHAT_IN_LINK = re.compile(r"19:[^/?#&\s]+")


def _is_link(text):
    return text.startswith(("http://", "https://")) or "/l/channel/" in text


def channel_from_link(url):
    """The channel id, the team that holds it, and its name, out of one link."""
    parts = urllib.parse.urlsplit(url)
    found = CHANNEL_IN_LINK.search(parts.path) or CHANNEL_IN_LINK.search(parts.fragment)
    if not found:
        raise ValueError(
            "that does not look like a channel link. In Teams, right-click the "
            "channel and choose Get link to channel, then paste the whole thing.")
    query = urllib.parse.parse_qs(parts.query) | urllib.parse.parse_qs(
        urllib.parse.urlsplit(parts.fragment).query)
    group = next((v[0].strip() for k, v in query.items() if k.lower() == "groupid"), "")
    if not group:
        # A link copied from the address bar rather than from the menu can come
        # without it, and a channel id on its own is not enough to find a channel.
        raise ValueError(
            "that link carries no groupId, so it does not say which team the "
            "channel is in. Use Get link to channel on the channel itself.")
    return {"ref": urllib.parse.unquote(found.group(1)),
            "within": group,
            "name": urllib.parse.unquote(found.group(2) or "").strip()}


def chat_from_link(url):
    """The chat id out of a chat's web address, which is the only place it shows."""
    found = CHAT_IN_LINK.search(urllib.parse.unquote(url))
    if not found:
        raise ValueError(
            "no chat id in that. Open the chat at teams.microsoft.com and copy "
            "the address bar -- the part beginning 19: is the chat id.")
    return {"ref": found.group(0)}


def _unpack(kind, ref):
    """What a pasted link says, for the kinds that come as links."""
    if not _is_link(ref):
        return {}
    if kind == "teams_channel":
        return channel_from_link(ref)
    if kind == "teams_chat":
        return chat_from_link(ref)
    return {}


def folder_path(text):
    """A mail folder path, with its separators and stray spaces made regular.

    Backslashes are accepted because that is how a Windows user writes a path,
    and the result always uses /, so the same folder typed two ways is stored
    one way.
    """
    parts = [part.strip() for part in re.split(r"[/\\]", text)]
    return "/".join(part for part in parts if part)


def _clean(body):
    """One incoming source, checked into the row that would store it."""
    kind = str(body.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    spec = KINDS[kind]

    ref = str(body.get("ref") or "").strip()
    if not ref:
        raise ValueError(f"a {spec['label'].lower()} needs its {spec['ref']}")

    # A pasted link is unpacked into the ids it carries, and anything it also
    # happens to know -- a channel's name -- is used only where you have not
    # said otherwise.
    from_link = _unpack(kind, ref)
    ref = from_link.get("ref", ref)
    if kind in FOLDED:
        ref = ref.lower().lstrip("@")
    if kind == "email_folder":
        ref = folder_path(ref)
        if not ref:
            raise ValueError("a mail folder needs its path, e.g. "
                             "Inbox/Clients/Acme")

    within = str(body.get("within") or "").strip() or from_link.get("within", "")
    if spec.get("within") and not within:
        raise ValueError(
            f"a {spec['label'].lower()} needs its {spec['within']} too, and a "
            "channel id does not carry one. Paste the channel link instead: "
            "right-click the channel in Teams -> Get link to channel.")
    if not spec.get("within"):
        within = ""

    return {"kind": kind, "ref": ref, "within": within,
            "name": str(body.get("name") or "").strip() or from_link.get("name", "")}


def shape(row):
    out = {"id": row["id"], "kind": row["kind"], "name": row["name"],
           "ref": row["ref"], "within": row["within"]}
    if row["kind"] == "email_folder":
        # Mail search tools take the folder's own name, not its path. The path
        # stays the pointer: two folders can share a leaf, and it is the path
        # that tells a result from one apart from the other.
        out["leaf"] = row["ref"].rsplit("/", 1)[-1]
    return out


def listing(conn, folder_id):
    rows = conn.execute(
        "SELECT * FROM folder_source WHERE folder_id = ? ORDER BY position, id",
        (folder_id,)).fetchall()
    return {"sources": [shape(row) for row in rows]}


def _key(kind, ref):
    return ref.casefold() if kind in CASELESS else ref


def _clash(conn, folder_id, entry, excluding=None):
    """The same pointer twice in one folder is a mistake, not a preference."""
    found = conn.execute(
        "SELECT id, kind, ref FROM folder_source WHERE folder_id = ? AND kind = ?",
        (folder_id, entry["kind"])).fetchall()
    want = _key(entry["kind"], entry["ref"])
    return any(row["id"] != excluding and _key(row["kind"], row["ref"]) == want
               for row in found)


def add(conn, folder_id, body):
    entry = _clean(body)
    if _clash(conn, folder_id, entry):
        raise ValueError("this folder already listens to that one")
    row_id = conn.execute(
        "INSERT INTO folder_source (folder_id, kind, name, ref, within, position,"
        " added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (folder_id, entry["kind"], entry["name"], entry["ref"], entry["within"],
         db.next_position(conn, "folder_source", folder_id), history.now()),
    ).lastrowid
    return shape(conn.execute("SELECT * FROM folder_source WHERE id = ?",
                              (row_id,)).fetchone())


def _source(conn, source_id):
    row = conn.execute("SELECT * FROM folder_source WHERE id = ?",
                       (source_id,)).fetchone()
    if row is None:
        raise KeyError(f"no source with id {source_id}")
    return row


def update(conn, source_id, body):
    """Change one. The kind is fixed -- a channel does not become a domain."""
    row = _source(conn, source_id)
    entry = _clean({"kind": row["kind"],
                    "ref": body.get("ref", row["ref"]),
                    "within": body.get("within", row["within"]),
                    "name": body.get("name", row["name"])})
    if _clash(conn, row["folder_id"], entry, excluding=source_id):
        raise ValueError("this folder already listens to that one")
    conn.execute(
        "UPDATE folder_source SET name = ?, ref = ?, within = ? WHERE id = ?",
        (entry["name"], entry["ref"], entry["within"], source_id))
    return shape(_source(conn, source_id))


def remove(conn, source_id):
    _source(conn, source_id)
    conn.execute("DELETE FROM folder_source WHERE id = ?", (source_id,))
    return {"id": source_id, "deleted": True}
