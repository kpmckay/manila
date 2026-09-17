"""Column definitions for Manila's fixed tables.

This module is the single source of truth for what a table looks like. The
server builds SQL from it, the browser builds the grid from it (served at
GET /api/schema), and history.py uses it to know how a logical field maps onto
storage columns. Changing a table means changing it here, in one place.

Cell types
    check     boolean tick box, no history
    text      single-line free text
    longtext  multi-line free text (Shift+Enter for a newline)
    date      ISO date, rendered absolute + relative ("15-Sep-2026 - in 15 days")
    datenote  a date plus a short note, stored in two columns but edited and
              versioned as ONE cell, because the sketch draws it as one cell.
              Used by Last Touched ("3 days ago - via email") and Resolve By
              ("in 15 days - worst case EOM"): both are a real date you want
              counted, carrying a human phrase the date alone cannot say.
"""

# Logical field -> the storage columns behind it. Everything except datenote is
# a straight 1:1 mapping.
COLUMNS = {
    "action_item": [
        {"key": "done",          "label": "",              "type": "check",    "width": "2.4rem", "history": False},
        {"key": "item",          "label": "Item",          "type": "longtext", "width": "minmax(14rem, 2fr)"},
        {"key": "waiting_on",    "label": "Waiting On",    "type": "text",     "width": "minmax(7rem, 0.8fr)",
         "suggest": True},
        # Three parts: when, how, and anything else worth remembering. "How" is
        # a picker that also takes whatever you type -- the list covers most
        # ways of chasing someone, never all of them.
        {"key": "last_touched",  "label": "Last Touched",  "type": "datenote", "width": "minmax(10rem, 1.1fr)",
         "storage": ["last_touched_at", "last_touched_via", "last_touched_note"],
         "choices": ["Email", "Chat", "Text", "Call", "Meeting"],
         "placeholder": "further context"},
        # A deadline: once the date is behind us, the cell says so in red.
        # Its date has a source -- either typed here, or borrowed from one of
        # the folder's milestones, so a deliverable that slips takes everything
        # waiting on it along without a single edit.
        {"key": "resolve_by",    "label": "Resolve By",    "type": "datenote", "width": "minmax(9.5rem, 1fr)",
         "storage": ["resolve_by_at", "resolve_by_note"],
         "link": {"entity": "milestone", "column": "resolve_by_milestone_id",
                  "since": "resolve_by_linked_at",
                  "label": "deliverable", "date": "due_date"},
         "deadline": True,
         "placeholder": "ASAP, worst case EOM..."},
        {"key": "current_state", "label": "Current State", "type": "longtext", "width": "minmax(11rem, 1.5fr)"},
    ],

    "milestone": [
        {"key": "deliverable",   "label": "Deliverable",   "type": "text",     "width": "minmax(12rem, 1.4fr)"},
        {"key": "due_date", "deadline": True,      "label": "Date",          "type": "date",     "width": "minmax(9rem, 0.8fr)"},
        {"key": "notes",         "label": "Notes",         "type": "longtext", "width": "minmax(12rem, 2fr)"},
    ],
    # Organization, Team and Reports To are the three that describe where a
    # person sits. Team and Reports To only mean something once you know which
    # organization they are in -- half these people work for someone else.
    "contact": [
        {"key": "name",          "label": "Name",          "type": "text",     "width": "minmax(9rem, 1fr)"},
        {"key": "title", "suggest": True,         "label": "Title",         "type": "text",     "width": "minmax(9rem, 1fr)"},
        {"key": "email",         "label": "Email",         "type": "text",     "width": "minmax(11rem, 1.2fr)"},
        {"key": "phone",         "label": "Phone",         "type": "text",     "width": "minmax(8rem, 0.8fr)"},
        {"key": "organization", "suggest": True,  "label": "Organization",  "type": "text",     "width": "minmax(8rem, 1fr)"},
        {"key": "team", "suggest": True,          "label": "Team",          "type": "text",     "width": "minmax(8rem, 0.9fr)"},
        {"key": "reports_to", "suggest": True,    "label": "Reports To",    "type": "text",     "width": "minmax(8rem, 0.9fr)"},
    ],
}

# Where each entity shows up in the UI. The Dashboard always has exactly these
# two sections, in this order -- that is a fixed part of the design.
SECTIONS = {
    "dashboard": [
        {"entity": "action_item", "title": "Actions"},
        {"entity": "milestone",   "title": "Dates and Milestones"},
    ],
    "contacts": [
        {"entity": "contact",     "title": "Contact List"},
    ],
}

TABLES = {"action_item": "action_item", "milestone": "milestone", "contact": "contact"}


def columns(entity):
    if entity not in COLUMNS:
        raise KeyError(f"unknown entity type: {entity}")
    return COLUMNS[entity]


def column(entity, field):
    for col in columns(entity):
        if col["key"] == field:
            return col
    raise KeyError(f"unknown field {field!r} on {entity}")


def storage_columns(col):
    """The DB column names behind one logical field."""
    return col.get("storage", [col["key"]])


def link_column(col):
    """The column holding what a cell's value is borrowed from, if anything."""
    spec = col.get("link")
    return spec["column"] if spec else None


def all_storage_columns(entity):
    out = []
    for col in columns(entity):
        out.extend(storage_columns(col))
        if link_column(col):
            out.append(link_column(col))
            out.append(col["link"]["since"])
    return out


def tracks_history(col):
    return col.get("history", True) and col["type"] != "check"
