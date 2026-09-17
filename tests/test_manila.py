"""Tests for the parts that would be silently wrong if they broke.

History correctness is the priority: the log is only worth having if it is
complete and ordered, and a bug there is invisible until you go looking for a
past value and it isn't there.
"""

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manila import (db, files, history, mcp, net, project,  # noqa: E402
                    richtext, schema, server, sources, suggest, window)

# Tests make edits far faster than anyone types. Each one is its own revision
# here; TestSettling turns the window back on to test folding them together.
history.SETTLE_SECONDS = 0


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = db.connect(Path(self.dir.name) / "test.db", init=True)
        self.addCleanup(self.conn.close)
        self.folder = self.conn.execute(
            "INSERT INTO folder (name, position, created_at) VALUES ('F', 1.0, 'now')"
        ).lastrowid

    def new_action(self):
        return self.conn.execute(
            "INSERT INTO action_item (folder_id, position, created_at)"
            " VALUES (?, ?, 'now')",
            (self.folder, db.next_position(self.conn, "action_item", self.folder)),
        ).lastrowid

    def log(self, row_id, field):
        return history.cell_history(self.conn, "action_item", row_id, field)

    def set(self, row_id, field, value):
        return history.update_field(self.conn, "action_item", row_id, field, value)


class TestHistory(Base):
    def test_revisions_are_logged_newest_first(self):
        row = self.new_action()
        for value in ("Kicked off with Alice", "Waiting on supplier quote", "Alice working with suppliers"):
            self.set(row, "current_state", value)

        entries = self.log(row, "current_state")
        self.assertEqual(len(entries), 2, "first fill is not a revision")
        self.assertEqual(entries[0]["old_value"], "Waiting on supplier quote")
        self.assertEqual(entries[1]["old_value"], "Kicked off with Alice")
        # Each entry's new_value chains into the next one's old_value.
        self.assertEqual(entries[0]["new_value"], "Alice working with suppliers")
        self.assertEqual(entries[1]["new_value"], entries[0]["old_value"])

    def test_first_fill_leaves_no_chip(self):
        row = self.new_action()
        changed, _ = self.set(row, "item", "Determine power state reporting")
        self.assertTrue(changed)
        self.assertEqual(self.log(row, "item"), [])

    def test_no_op_edit_writes_nothing(self):
        row = self.new_action()
        self.set(row, "waiting_on", "Alice")
        self.set(row, "waiting_on", "Alice")
        changed, _ = self.set(row, "waiting_on", "Alice")
        self.assertFalse(changed)
        self.assertEqual(self.log(row, "waiting_on"), [])

    def test_clearing_a_value_is_logged(self):
        row = self.new_action()
        self.set(row, "waiting_on", "Alice")
        self.set(row, "waiting_on", "")
        entries = self.log(row, "waiting_on")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["old_value"], "Alice")
        self.assertEqual(entries[0]["new_value"], "")

    def test_datenote_is_one_cell_with_one_history(self):
        row = self.new_action()
        self.set(row, "last_touched",
                 {"date": "2026-08-20", "via": "Meeting", "note": "in standup"})
        self.set(row, "last_touched",
                 {"date": "2026-08-29", "via": "Email", "note": "chased again"})
        entries = self.log(row, "last_touched")
        # When, how and the note are one cell with one revision between them.
        self.assertEqual(len(entries), 1)
        # Stored literally, so the log still reads correctly years later.
        self.assertEqual(entries[0]["old_value"], "2026-08-20|Meeting|in standup")
        self.assertEqual(entries[0]["new_value"], "2026-08-29|Email|chased again")

    def test_changing_only_the_method_counts_as_a_revision(self):
        row = self.new_action()
        self.set(row, "last_touched", {"date": "2026-08-20", "via": "Email", "note": ""})
        changed, _ = self.set(row, "last_touched",
                              {"date": "2026-08-20", "via": "Call", "note": ""})
        self.assertTrue(changed)
        self.assertEqual(self.log(row, "last_touched")[0]["old_value"], "2026-08-20|Email|")

    def test_a_method_off_the_list_is_kept_as_typed(self):
        # The picker is a convenience, not a constraint.
        row = self.new_action()
        _, value = self.set(row, "last_touched",
                            {"date": "2026-08-20", "via": "Carrier pigeon", "note": ""})
        self.assertEqual(value["via"], "Carrier pigeon")

    def test_resolve_by_has_two_parts_plus_its_source(self):
        # The composites are shaped by their storage list, not by their type.
        # Resolve By carries where its date came from; 0 means "my own".
        row = self.new_action()
        _, value = self.set(row, "resolve_by", {"date": "2026-09-15", "note": "EOM"})
        self.assertEqual(set(value), {"date", "note", "link"})
        self.assertEqual(value["link"], 0)
        self.assertEqual(self.log(row, "resolve_by"), [])
        # The link is not part of the logged text: the cell said a date.
        self.set(row, "resolve_by", {"date": "2026-10-15", "note": "EOM"})
        self.assertEqual(self.log(row, "resolve_by")[0]["old_value"], "2026-09-15|EOM")

    def test_changing_only_the_note_still_counts(self):
        row = self.new_action()
        self.set(row, "last_touched", {"date": "2026-08-20", "note": "in standup"})
        changed, _ = self.set(row, "last_touched", {"date": "2026-08-20", "note": "via email"})
        self.assertTrue(changed)
        self.assertEqual(len(self.log(row, "last_touched")), 1)

    def test_checkbox_is_not_versioned(self):
        row = self.new_action()
        self.set(row, "done", True)
        self.set(row, "done", False)
        self.assertEqual(self.log(row, "done"), [])

    def test_row_and_history_land_together(self):
        row = self.new_action()
        self.set(row, "current_state", "one")
        self.set(row, "current_state", "two")
        stored = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)
        ).fetchone()["current_state"]
        entries = self.log(row, "current_state")
        self.assertEqual(stored, "two")
        self.assertEqual(entries[0]["new_value"], stored)

    def test_unknown_field_is_rejected(self):
        row = self.new_action()
        with self.assertRaises(KeyError):
            self.set(row, "not_a_field", "x")

    def test_missing_row_is_rejected(self):
        with self.assertRaises(KeyError):
            self.set(9999, "item", "x")

    def test_counts_are_per_cell(self):
        row = self.new_action()
        self.set(row, "item", "a")
        self.set(row, "item", "b")
        self.set(row, "current_state", "x")
        self.set(row, "current_state", "y")
        self.set(row, "current_state", "z")
        counts = history.history_counts(self.conn, "action_item", [row])
        self.assertEqual(counts.get((row, "item")), 1)
        self.assertEqual(counts.get((row, "current_state")), 2)
        self.assertIsNone(counts.get((row, "waiting_on")))


class TestForgettingHistory(Base):
    """A mistyped value becomes a permanent revision the moment it is
    corrected. Removing that line must not disturb anything else."""

    def test_one_entry_goes_and_the_rest_stay(self):
        row = self.new_action()
        for value in ("Alice", "Allice", "Bob"):
            self.set(row, "waiting_on", value)
        entries = self.log(row, "waiting_on")
        self.assertEqual([e["old_value"] for e in entries], ["Allice", "Alice"])

        typo = next(e for e in entries if e["old_value"] == "Allice")
        history.forget_entry(self.conn, typo["id"])
        self.assertEqual([e["old_value"] for e in self.log(row, "waiting_on")], ["Alice"])

    def test_the_cell_value_is_untouched(self):
        row = self.new_action()
        self.set(row, "waiting_on", "Alice")
        self.set(row, "waiting_on", "Bob")
        history.forget_entry(self.conn, self.log(row, "waiting_on")[0]["id"])
        current = self.conn.execute(
            "SELECT waiting_on FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(current["waiting_on"], "Bob")
        self.assertEqual(self.log(row, "waiting_on"), [])

    def test_other_cells_and_rows_keep_their_logs(self):
        one, two = self.new_action(), self.new_action()
        self.set(one, "waiting_on", "Alice")
        self.set(one, "waiting_on", "Bob")
        self.set(one, "current_state", "Kicked off")
        self.set(one, "current_state", "Blocked")
        self.set(two, "waiting_on", "Carol")
        self.set(two, "waiting_on", "Sam")

        history.forget_entry(self.conn, self.log(one, "waiting_on")[0]["id"])
        self.assertEqual(len(self.log(one, "waiting_on")), 0)
        self.assertEqual(len(self.log(one, "current_state")), 1)
        self.assertEqual(len(self.log(two, "waiting_on")), 1)

    def test_forgetting_something_that_is_not_there(self):
        with self.assertRaises(KeyError):
            history.forget_entry(self.conn, 999999)

    def test_entries_carry_an_id_to_delete_them_by(self):
        row = self.new_action()
        self.set(row, "waiting_on", "Alice")
        self.set(row, "waiting_on", "Bob")
        self.assertIn("id", self.log(row, "waiting_on")[0])


class TestSettling(Base):
    """Going straight back into a cell is finishing the edit, not revising it."""

    def setUp(self):
        super().setUp()
        self.clock = 0.0
        start = history.datetime(2026, 9, 15, tzinfo=history.timezone.utc)
        stamp = lambda: (start + history.timedelta(seconds=self.clock)).isoformat(
            timespec="microseconds")
        for patch in (mock.patch.object(history, "SETTLE_SECONDS", 10),
                      mock.patch.object(history, "now", stamp)):
            patch.start()
            self.addCleanup(patch.stop)

    def at(self, seconds, row, field, value):
        self.clock = seconds
        return self.set(row, field, value)

    def test_a_quick_correction_folds_into_one_entry(self):
        row = self.new_action()
        self.at(0, row, "waiting_on", "Alice")
        self.at(60, row, "waiting_on", "Bbo")
        self.at(65, row, "waiting_on", "Bob")
        entries = self.log(row, "waiting_on")
        self.assertEqual([(e["old_value"], e["new_value"]) for e in entries],
                         [("Alice", "Bob")])
        self.assertEqual(entries[0]["changed_at"], history.now())

    def test_the_window_restarts_on_each_edit(self):
        row = self.new_action()
        self.at(0, row, "waiting_on", "Alice")
        for t, value in ((60, "B"), (68, "Bo"), (76, "Bob")):
            self.at(t, row, "waiting_on", value)
        self.assertEqual(len(self.log(row, "waiting_on")), 1)

    def test_a_settled_cell_logs_the_next_change(self):
        row = self.new_action()
        self.at(0, row, "waiting_on", "Alice")
        self.at(60, row, "waiting_on", "Bob")
        self.at(70, row, "waiting_on", "Carol")
        self.assertEqual([e["old_value"] for e in self.log(row, "waiting_on")],
                         ["Bob", "Alice"])

    def test_fixing_a_first_fill_is_still_a_first_fill(self):
        row = self.new_action()
        self.at(0, row, "item", "Determine power")
        self.at(5, row, "item", "Determine power state reporting")
        self.assertEqual(self.log(row, "item"), [])

    def test_a_new_row_is_born_settling(self):
        row = self.new_action()
        self.conn.execute("UPDATE action_item SET item = 'Determin' WHERE id = ?", (row,))
        history.born(self.conn, "action_item", row, ["item"])
        self.at(3, row, "item", "Determine")
        self.assertEqual(self.log(row, "item"), [])

    def test_putting_it_back_leaves_no_entry(self):
        row = self.new_action()
        self.at(0, row, "waiting_on", "Alice")
        self.at(60, row, "waiting_on", "Bob")
        self.at(62, row, "waiting_on", "Alice")
        self.assertEqual(self.log(row, "waiting_on"), [])
        # ...and changing it again straight after is still a real change.
        self.at(64, row, "waiting_on", "Carol")
        self.assertEqual([(e["old_value"], e["new_value"])
                          for e in self.log(row, "waiting_on")], [("Alice", "Carol")])

    def test_cells_settle_independently(self):
        row = self.new_action()
        self.at(0, row, "waiting_on", "Alice")
        self.at(0, row, "current_state", "Kicked off")
        self.at(60, row, "waiting_on", "Bob")
        self.at(61, row, "current_state", "Blocked")
        self.at(62, row, "waiting_on", "Carol")
        self.assertEqual(len(self.log(row, "waiting_on")), 1)
        self.assertEqual(len(self.log(row, "current_state")), 1)

    def test_forgetting_mid_burst_does_not_lose_the_next_change(self):
        row = self.new_action()
        self.at(0, row, "waiting_on", "Alice")
        self.at(60, row, "waiting_on", "Bob")
        history.forget_entry(self.conn, self.log(row, "waiting_on")[0]["id"])
        self.at(63, row, "waiting_on", "Carol")
        self.assertEqual([(e["old_value"], e["new_value"])
                          for e in self.log(row, "waiting_on")], [("Alice", "Carol")])

    def test_old_bursts_are_swept(self):
        one, two = self.new_action(), self.new_action()
        self.at(0, one, "waiting_on", "Alice")
        self.at(60, two, "waiting_on", "Bob")
        left = self.conn.execute("SELECT entity_id FROM settling_edit").fetchall()
        self.assertEqual([r["entity_id"] for r in left], [two])


class TestRowShape(Base):
    """row_to_dict is what the browser actually receives, so exercise it.

    Added after a schema change left a stale column reference here that no unit
    test touched -- it only surfaced as a 500 from the running server.
    """

    def test_row_serialises_with_every_declared_field(self):
        row_id = self.new_action()
        self.set(row_id, "item", "Supplier quote, revised")
        self.set(row_id, "resolve_by", {"date": "2026-09-15", "note": "worst case EOM"})
        row = self.conn.execute(
            "SELECT * FROM action_item WHERE id = ?", (row_id,)).fetchone()
        counts = history.history_counts(self.conn, "action_item", [row_id])

        payload = history.row_to_dict("action_item", row, counts)
        self.assertEqual(payload["id"], row_id)
        self.assertFalse(payload["archived"])
        self.assertEqual(
            set(payload["fields"]),
            {c["key"] for c in schema.columns("action_item")},
            "every declared column must appear in what the browser receives")
        self.assertEqual(payload["fields"]["resolve_by"],
                         {"date": "2026-09-15", "note": "worst case EOM", "link": 0})

    def test_every_entity_serialises(self):
        for entity, table in schema.TABLES.items():
            row_id = self.conn.execute(
                f"INSERT INTO {table} (folder_id, position, created_at)"
                " VALUES (?, 1.0, 'now')", (self.folder,)).lastrowid
            row = self.conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
            payload = history.row_to_dict(entity, row, {})
            self.assertEqual(set(payload["fields"]),
                             {c["key"] for c in schema.columns(entity)}, entity)


class TestResolveBy(Base):
    def test_resolve_by_is_a_dated_cell_with_its_own_history(self):
        row = self.new_action()
        self.set(row, "resolve_by", {"date": "2026-09-15", "note": "worst case EOM"})
        self.set(row, "resolve_by", {"date": "2026-09-29", "note": "slipped, supplier lead time"})
        entries = self.log(row, "resolve_by")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["old_value"], "2026-09-15|worst case EOM")

    def test_a_note_with_no_date_is_still_allowed(self):
        # "ASAP" is not a date, and the tool must not insist on one.
        row = self.new_action()
        changed, value = self.set(row, "resolve_by", {"date": "", "note": "ASAP"})
        self.assertTrue(changed)
        self.assertEqual(value, {"date": "", "note": "ASAP", "link": 0})


class TestMigration(unittest.TestCase):
    """An existing database has to survive both of today's schema changes."""

    def test_old_database_migrates_without_losing_anything(self):
        import sqlite3
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "old.db"

        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE folder (id INTEGER PRIMARY KEY, name TEXT, position REAL,
                                 created_at TEXT, archived_at TEXT, deleted_at TEXT);
            CREATE TABLE action_item (id INTEGER PRIMARY KEY, folder_id INTEGER,
                                 position REAL, done INTEGER DEFAULT 0, item TEXT DEFAULT '',
                                 waiting_on TEXT DEFAULT '', last_touched_at TEXT DEFAULT '',
                                 last_touched_note TEXT DEFAULT '', resolve_by TEXT DEFAULT '',
                                 current_state TEXT DEFAULT '', created_at TEXT,
                                 archived_at TEXT, deleted_at TEXT);
            INSERT INTO folder VALUES (1, 'ACME Corp', 1.0, 'then', NULL, NULL);
            INSERT INTO action_item (id, folder_id, position, item, resolve_by, created_at)
                VALUES (1, 1, 1.0, 'Supplier quote, revised', 'ASAP, worst case EOM', 'then');
        """)
        old.commit()
        old.close()

        conn = db.connect(path, init=True)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT * FROM action_item WHERE id = 1").fetchone()

        self.assertEqual(row["item"], "Supplier quote, revised")
        # The old free text survives as the note half of the new cell.
        self.assertEqual(row["resolve_by_note"], "ASAP, worst case EOM")
        self.assertEqual(row["resolve_by_at"], "")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(action_item)")}
        self.assertNotIn("deleted_at", columns)
        self.assertNotIn("resolve_by", columns)
        self.assertEqual(
            conn.execute("SELECT name FROM folder WHERE id = 1").fetchone()["name"],
            "ACME Corp")

    def test_watermarks_recorded_before_they_were_per_source_are_kept(self):
        import sqlite3
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "before.db"
        before = sqlite3.connect(path)
        before.executescript("""
            CREATE TABLE folder (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                                 position REAL NOT NULL, created_at TEXT NOT NULL,
                                 archived_at TEXT);
            CREATE TABLE suggestion_batch (id INTEGER PRIMARY KEY,
                                 folder_id INTEGER NOT NULL, source TEXT DEFAULT '',
                                 summary TEXT DEFAULT '', agent TEXT DEFAULT '',
                                 covered_through TEXT NOT NULL DEFAULT '',
                                 created_at TEXT NOT NULL);
            INSERT INTO folder VALUES (1, 'Acme', 1.0, 'then', NULL);
            INSERT INTO suggestion_batch (folder_id, source, covered_through, created_at)
                VALUES (1, 'Outlook', '2026-09-14', '2026-09-14T09:00:00+00:00'),
                       (1, 'Outlook', '2026-09-13T20:00:00+00:00',
                        '2026-09-15T09:00:00+00:00');
        """)
        before.commit()
        before.close()

        conn = db.connect(path, init=True)
        self.addCleanup(conn.close)
        db.migrate(conn)       # a second start must not copy them twice
        run = suggest.last_runs(conn, 1)[0]
        self.assertEqual(run["runs"], 2)
        # A bare date is the start of that day, so the 14th beats the 13th.
        self.assertEqual(run["covered_through"], "2026-09-14")

    def test_a_batch_label_left_behind_as_a_source_is_dropped(self):
        """The first build of per-source watermarks keyed a run with no
        watermark by its label, so a "both" run left a "both" source."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        conn = db.connect(Path(directory.name) / "p.db", init=True)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO folder (id, name, position, created_at)"
                     " VALUES (1, 'F', 1.0, 'then')")
        for batch, source, through in ((1, "both", ""),
                                       (2, "Outlook", "2026-09-14T16:07:02+00:00")):
            conn.execute("INSERT INTO suggestion_batch (id, folder_id, source,"
                         " created_at) VALUES (?, 1, ?, 'then')", (batch, source))
            conn.execute("INSERT INTO batch_coverage (batch_id, source,"
                         " covered_through) VALUES (?, ?, ?)", (batch, source, through))
        db.migrate(conn)
        self.assertEqual([r["source"] for r in suggest.last_runs(conn, 1)], ["Outlook"])

    def test_a_database_from_before_sources_gains_them_empty(self):
        """A folder that has been synced for months must not come back claiming
        to listen to nothing *and* to have read everything: the pointers start
        empty, and the runs already recorded keep no watermark they never had."""
        import sqlite3
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "before.db"

        before = sqlite3.connect(path)
        before.executescript("""
            CREATE TABLE folder (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                                 position REAL NOT NULL, created_at TEXT NOT NULL,
                                 archived_at TEXT);
            CREATE TABLE suggestion_batch (id INTEGER PRIMARY KEY,
                                 folder_id INTEGER NOT NULL, source TEXT DEFAULT '',
                                 summary TEXT DEFAULT '', agent TEXT DEFAULT '',
                                 created_at TEXT NOT NULL);
            INSERT INTO folder VALUES (1, 'Acme', 1.0, 'then', NULL);
            INSERT INTO suggestion_batch (folder_id, source, created_at)
                VALUES (1, 'Outlook', '2026-09-14T09:00:00+00:00');
        """)
        before.commit()
        before.close()

        conn = db.connect(path, init=True)
        self.addCleanup(conn.close)
        self.assertEqual(sources.listing(conn, 1)["sources"], [])
        self.assertEqual(suggest.last_runs(conn, 1), [])
        # And the folder can be pointed somewhere, which is the point of it.
        folder = sources.add(conn, 1, {"kind": "email_folder",
                                       "ref": "Inbox/Clients/Acme"})
        self.assertEqual(folder["leaf"], "Acme")
        self.assertEqual(sources.add(conn, 1, {"kind": "email_domain",
                                               "ref": "acme.example.com"})["ref"],
                         "acme.example.com")


class TestContacts(Base):
    """Organization is what makes the other two placement columns meaningful,
    and is what an org chart would eventually be built from."""

    def contact(self):
        return self.conn.execute(
            "INSERT INTO contact (folder_id, position, name, created_at)"
            " VALUES (?, 1.0, 'Alice', 'now')", (self.folder,)).lastrowid

    def test_every_column_is_labelled(self):
        # A column with no label reads as a bug in the header row.
        for entity in schema.COLUMNS:
            for col in schema.columns(entity):
                if col["type"] == "check":
                    continue  # the done box is deliberately headerless
                self.assertTrue(col["label"], f"{entity}.{col['key']} has no label")

    def test_placement_columns_are_present_and_in_order(self):
        keys = [c["key"] for c in schema.columns("contact")]
        self.assertEqual(keys[-3:], ["organization", "team", "reports_to"])

    def test_organization_round_trips_with_history(self):
        row_id = self.contact()
        history.update_field(self.conn, "contact", row_id, "organization", "ACME Corp")
        changed, value = history.update_field(
            self.conn, "contact", row_id, "organization", "Coyote Supplies")
        self.assertTrue(changed)
        self.assertEqual(value, "Coyote Supplies")
        log = history.cell_history(self.conn, "contact", row_id, "organization")
        self.assertEqual([e["old_value"] for e in log], ["ACME Corp"])

    def test_organization_is_serialised_with_the_row(self):
        row_id = self.contact()
        history.update_field(self.conn, "contact", row_id, "organization", "ACME Corp")
        row = self.conn.execute("SELECT * FROM contact WHERE id = ?", (row_id,)).fetchone()
        shaped = history.row_to_dict("contact", row, {})
        self.assertEqual(shaped["fields"]["organization"], "ACME Corp")

    def test_an_existing_database_gains_the_column_empty(self):
        # Migration must not invent an organization for contacts already typed.
        self.conn.execute("ALTER TABLE contact DROP COLUMN organization")
        row_id = self.contact()
        db.migrate(self.conn)
        row = self.conn.execute("SELECT * FROM contact WHERE id = ?", (row_id,)).fetchone()
        self.assertEqual(row["organization"], "")
        self.assertEqual(row["name"], "Alice")


class TestOrdering(Base):
    def order(self):
        rows = self.conn.execute(
            "SELECT id FROM action_item ORDER BY position"
        ).fetchall()
        return [r["id"] for r in rows]

    def move(self, row_id, after_id):
        pos = db.midpoint(self.conn, "action_item", row_id, after_id, self.folder)
        self.conn.execute("UPDATE action_item SET position = ? WHERE id = ?", (pos, row_id))

    def test_drag_to_top_and_middle(self):
        a, b, c = (self.new_action() for _ in range(3))
        self.assertEqual(self.order(), [a, b, c])
        self.move(c, None)
        self.assertEqual(self.order(), [c, a, b])
        self.move(c, a)
        self.assertEqual(self.order(), [a, c, b])

    def test_repeated_midpoint_drags_stay_ordered(self):
        rows = [self.new_action() for _ in range(4)]
        # Repeatedly wedge the last row between the first two. Without
        # renormalisation the gap would collapse to zero and the order would
        # start scrambling.
        for _ in range(80):
            self.move(rows[-1], rows[0])
            self.assertEqual(self.order()[0], rows[0])
            self.assertEqual(self.order()[1], rows[-1])
        positions = [
            r["position"] for r in
            self.conn.execute("SELECT position FROM action_item ORDER BY position")
        ]
        self.assertEqual(len(set(positions)), len(positions), "positions must stay distinct")


class TestSchema(unittest.TestCase):
    def test_storage_columns_cover_every_table_column(self):
        for entity, table in schema.TABLES.items():
            declared = set(schema.all_storage_columns(entity))
            conn = db.connect(":memory:", init=True)
            self.addCleanup(conn.close)
            actual = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            meta = {"id", "folder_id", "position", "created_at", "archived_at"}
            self.assertEqual(declared, actual - meta,
                             f"{entity}: schema.py and the SQL table disagree")


class TestProjects(unittest.TestCase):
    """Projects are independent and renameable. These check that renaming never
    moves data, and that removing one never deletes it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        self.addCleanup(self.restore)

    def restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def ids(self):
        return [p.id for p in project.projects()]

    def test_registry_and_data_live_outside_the_code_tree(self):
        space = project.add("ACME Corp")
        tree = Path(__file__).resolve().parent.parent
        self.assertFalse(space.path.is_relative_to(tree))
        self.assertFalse(project.registry_path().is_relative_to(tree))

    def test_nothing_exists_until_you_name_it(self):
        self.assertEqual(project.projects(), [])

    def test_projects_share_no_directory_or_database(self):
        one = project.add("ACME Corp")
        two = project.add("Coyote Corp")
        self.assertNotEqual(one.path, two.path)
        self.assertNotEqual(one.db_path, two.db_path)
        self.assertNotEqual(one.color, two.color)

    def test_id_is_a_slug_of_the_creation_name(self):
        self.assertEqual(project.add("ACME Corp / Programs").id, "acme-corp-programs")
        self.assertEqual(project.add("!!!").id, "project")

    def test_same_name_twice_is_allowed_with_distinct_ids(self):
        # Two clients really can be called the same thing; only the id must differ.
        first = project.add("ACME Corp")
        second = project.add("ACME Corp")
        self.assertEqual(first.name, second.name)
        self.assertEqual([first.id, second.id], ["acme-corp", "acme-corp-2"])
        self.assertNotEqual(first.path, second.path)

    def test_renaming_changes_the_name_and_nothing_else(self):
        before = project.add("Untitled")
        renamed = project.rename(before.id, "ACME Corp")
        self.assertEqual(renamed.name, "ACME Corp")
        self.assertEqual(renamed.id, before.id, "the id must survive a rename")
        self.assertEqual(renamed.path, before.path, "data must not move on rename")

    def test_renaming_can_repeat_and_can_recolor(self):
        space = project.add("First")
        project.rename(space.id, "Second")
        project.rename(space.id, "Third")
        project.rename(space.id, color="#123456")
        again = project.find(space.id)
        self.assertEqual(again.name, "Third")
        self.assertEqual(again.color, "#123456")

    def test_empty_name_is_refused(self):
        space = project.add("ACME Corp")
        with self.assertRaises(ValueError):
            project.add("   ")
        with self.assertRaises(ValueError):
            project.rename(space.id, "  ")
        self.assertEqual(project.find(space.id).name, "ACME Corp")

    def test_unknown_id_is_refused_not_invented(self):
        project.add("ACME Corp")
        for call in (lambda: project.find("typo"),
                     lambda: project.rename("typo", "x"),
                     lambda: project.forget("typo")):
            with self.assertRaises(KeyError):
                call()

    def test_reorder_puts_a_project_where_asked(self):
        for name in ("One", "Two", "Three"):
            project.add(name)
        self.assertEqual(self.ids(), ["one", "two", "three"])
        project.reorder("three", after_id=None)
        self.assertEqual(self.ids(), ["three", "one", "two"])
        project.reorder("three", after_id="one")
        self.assertEqual(self.ids(), ["one", "three", "two"])

    # --- importing a project that already exists ------------------------------
    #
    # Moving to another PC is copying the project's folder across, because a
    # project is that folder: its database, and the documents named relative to
    # it. These cover the other half -- telling the new machine it is there.

    def carried_over(self, name="ACME Corp", folder="Work"):
        """A project directory as it arrives from another machine: made, used,
        then unregistered here so nothing but the folder remains."""
        made = project.add(name)
        conn = db.connect(made.db_path, init=True)
        conn.execute("INSERT INTO folder (name, position, created_at)"
                     " VALUES (?, 1, 'now')", (folder,))
        conn.close()
        project.forget(made.id)
        return made.path

    def test_a_project_folder_from_another_machine_is_opened_where_it_lies(self):
        path = self.carried_over()
        self.assertEqual(self.ids(), [])

        opened = project.attach(str(path))
        self.assertEqual(opened.path, path)
        self.assertEqual(self.ids(), [opened.id])
        # Its own data, read where it sits: nothing copied, nothing created.
        conn = db.connect(opened.db_path)
        self.assertEqual([r["name"] for r in conn.execute("SELECT name FROM folder")],
                         ["Work"])
        conn.close()

    def test_an_imported_project_is_named_after_its_folder(self):
        path = self.carried_over(name="Acme Rollout")
        self.assertEqual(project.attach(str(path)).name, path.name)
        # And can be called something else on the way in.
        other = self.carried_over(name="Second")
        self.assertEqual(project.attach(str(other), name="Renamed").name, "Renamed")

    def test_the_same_folder_cannot_be_opened_twice(self):
        path = self.carried_over()
        project.attach(str(path))
        with self.assertRaises(ValueError) as caught:
            project.attach(str(path))
        self.assertIn("already open", str(caught.exception))

    def test_importing_a_folder_that_is_not_a_project_says_so(self):
        empty = Path(self.dir.name) / "not-a-project"
        empty.mkdir()
        with self.assertRaises(ValueError) as caught:
            project.attach(str(empty))
        self.assertIn("manila.db", str(caught.exception))
        self.assertEqual(self.ids(), [])

    def test_importing_the_folder_above_names_the_projects_in_it(self):
        """The likely mistake: pointing at the folder projects were copied
        into, rather than at one of them."""
        path = self.carried_over()
        with self.assertRaises(ValueError) as caught:
            project.attach(str(path.parent))
        self.assertIn(path.name, str(caught.exception))

    def test_importing_refuses_a_database_that_is_not_manilas(self):
        """Registering must never be a way to have Manila write its schema into
        somebody else's file."""
        stray = Path(self.dir.name) / "stray"
        stray.mkdir()
        (stray / "manila.db").write_text("this is not a database", encoding="utf-8")
        with self.assertRaises(ValueError):
            project.attach(str(stray))
        self.assertEqual((stray / "manila.db").read_text(encoding="utf-8"),
                         "this is not a database")

    def test_a_missing_folder_is_refused_rather_than_created(self):
        gone = Path(self.dir.name) / "nowhere"
        with self.assertRaises(ValueError):
            project.attach(str(gone))
        self.assertFalse(gone.exists())

    def test_browsing_says_which_folders_are_projects(self):
        path = self.carried_over()
        listing = project.browse(str(path.parent))
        marked = {e["name"]: e["project"] for e in listing["entries"]}
        self.assertEqual(marked.get(path.name), True)
        (path.parent / "ordinary").mkdir()
        listing = project.browse(str(path.parent))
        marked = {e["name"]: e["project"] for e in listing["entries"]}
        self.assertEqual(marked.get("ordinary"), False)
        self.assertEqual(project.browse(str(path))["project"], True)

    def test_two_projects_cannot_claim_one_directory(self):
        # An empty directory is not proof it is free: a project's database is
        # not written until it is first opened, so the registry is the check.
        first = project.add("ACME Corp")
        self.assertTrue(first.path.is_dir())
        self.assertFalse(first.db_path.exists())
        with self.assertRaises(ValueError):
            project.add("Something Else", path=str(first.path))
        self.assertEqual(self.ids(), ["acme-corp"])

    def test_a_non_empty_directory_is_refused(self):
        where = Path(self.dir.name) / "has stuff"
        where.mkdir()
        (where / "notes.txt").write_text("something already here")
        with self.assertRaises(ValueError):
            project.add("Careless", path=str(where))
        self.assertEqual((where / "notes.txt").read_text(), "something already here")

    def test_forgetting_keeps_the_data(self):
        space = project.add("ACME Corp")
        space.db_path.write_text("not really a database, but a real file")
        project.forget(space.id)
        self.assertEqual(self.ids(), [])
        self.assertTrue(space.db_path.exists(), "removing must never delete data")

    def test_a_custom_path_is_honoured(self):
        where = Path(self.dir.name) / "encrypted" / "acme"
        space = project.add("ACME Corp", path=str(where))
        self.assertEqual(space.path, where)
        self.assertTrue(where.is_dir())

    def test_legacy_registry_is_carried_forward(self):
        # A v1 workspaces.json must become projects without losing a name.
        old = project.legacy_registry_path()
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(json.dumps({"default": "work", "workspaces": {
            "work": {"path": "/tmp/manila-work", "port": 8000, "color": "#8a6a35"},
            "side": {"path": "/tmp/manila-side", "port": 8001},
        }}))
        carried = {p.id: p for p in project.projects()}
        self.assertEqual(set(carried), {"work", "side"})
        # Spelled through Path so the comparison is about the path being
        # carried forward unchanged, not about which separator this OS uses.
        self.assertEqual(carried["work"].path, Path("/tmp/manila-work"))
        self.assertTrue(carried["side"].color, "every project gets an accent")
        self.assertTrue(old.exists(), "the old registry must be left in place")
        # And the carried-forward projects are now renameable like any other.
        project.rename("work", "ACME Corp")
        self.assertEqual(project.find("work").name, "ACME Corp")
        # The legacy file is still on disk. Migration must not run a second time
        # and undo that rename -- it happens once, when projects.json is absent.
        self.assertEqual(project.find("work").name, "ACME Corp")
        self.assertTrue(project.registry_path().exists())
        for _ in range(3):
            project.projects()
        self.assertEqual(project.find("work").name, "ACME Corp")

    def test_missing_legacy_registry_invents_nothing(self):
        self.assertEqual(project.projects(), [])
        self.assertFalse(project.registry_path().exists())


class TestMakingAFolder(unittest.TestCase):
    """A location can be made while choosing one, so a project need not go in
    a folder that happens to exist already."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_a_folder_is_made_and_a_project_fits_in_it(self):
        made = project.make_folder(str(self.root), "Clients")
        self.assertEqual(Path(made["path"]), self.root / "Clients")
        self.assertTrue((self.root / "Clients").is_dir())
        self.assertEqual(made["name"], "Clients")

    def test_only_one_level_is_made_and_nothing_is_overwritten(self):
        project.make_folder(str(self.root), "Clients")
        with self.assertRaises(ValueError):
            project.make_folder(str(self.root), "Clients")

    def test_a_name_carrying_a_path_is_refused(self):
        for name in ("a/b", "a\\b", "..", ".", "   ", "", None):
            with self.assertRaises(ValueError):
                project.make_folder(str(self.root), name)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_the_folder_must_go_somewhere_that_exists(self):
        with self.assertRaises(ValueError):
            project.make_folder(None, "Clients")
        with self.assertRaises(ValueError):
            project.make_folder(str(self.root / "nowhere"), "Clients")

    def test_a_file_is_not_a_place_to_make_a_folder(self):
        note = self.root / "notes.txt"
        note.write_text("not a directory")
        with self.assertRaises(ValueError):
            project.make_folder(str(note), "Clients")


class TestOwnWindow(unittest.TestCase):
    """Manila opens in a window of its own, not a tab that slides behind
    fifteen others. A tab has no taskbar button and no place in alt-tab, which
    for something you keep open beside your work all day is the whole problem.
    """

    def test_the_app_flag_is_what_makes_it_a_window(self):
        # `--app=` is the entire mechanism: no tab strip, no address bar, its
        # own taskbar button. Without it this is just another tab.
        with mock.patch.object(window.subprocess, "Popen") as spawned:
            self.assertTrue(window.open_window("http://127.0.0.1:8000",
                                               browser="/usr/bin/chromium"))
        argv = spawned.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/chromium")
        self.assertIn("--app=http://127.0.0.1:8000", argv)

    def test_no_chromium_means_no_window(self):
        with mock.patch.object(window, "find_browser", return_value=None):
            self.assertFalse(window.open_window("http://127.0.0.1:8000"))

    def test_a_tab_is_better_than_nothing(self):
        # Refusing to show Manila at all because the window could not be made
        # would be a worse outcome than the tab we were trying to avoid.
        with mock.patch.object(window, "find_browser", return_value=None), \
             mock.patch.object(window.webbrowser, "open") as opened:
            self.assertEqual(window.open_manila("http://127.0.0.1:8000"), "tab")
        self.assertTrue(opened.called)

    def test_a_browser_that_will_not_start_falls_back_too(self):
        with mock.patch.object(window, "find_browser", return_value="/nope/chrome"), \
             mock.patch.object(window.subprocess, "Popen", side_effect=OSError), \
             mock.patch.object(window.webbrowser, "open") as opened:
            self.assertEqual(window.open_manila("http://127.0.0.1:8000"), "tab")
        self.assertTrue(opened.called)

    def test_asking_for_a_tab_does_not_look_for_a_browser(self):
        with mock.patch.object(window, "find_browser") as looked, \
             mock.patch.object(window.webbrowser, "open"):
            self.assertEqual(window.open_manila("http://x", prefer_window=False), "tab")
        self.assertFalse(looked.called)

    def test_an_unset_windows_variable_is_skipped_not_fatal(self):
        # ProgramFiles(x86) does not exist on 32-bit Windows, and a KeyError
        # out of a path template would take the whole launch with it.
        with mock.patch.object(window, "WINDOWS", True), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(window.find_browser())


class TestSecondLaunch(unittest.TestCase):
    """Launching Manila twice is an ordinary thing to do -- the icon is right
    there on the Start Menu. The second one has to find the first and stand
    down. On Windows that is also what keeps a second tray icon from appearing
    for a server the new process does not own and could not stop.
    """

    def _serving(self):
        httpd = server.create(port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def test_a_second_launch_stands_down(self):
        port = self._serving()
        self.assertIsNone(server.create(port=port),
                          "the second launch should defer to the first")

    def test_a_port_held_by_something_else_is_refused(self):
        import socket
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        self.addCleanup(held.close)
        with self.assertRaises(SystemExit):
            server.create(port=held.getsockname()[1])

    def test_another_web_server_is_not_mistaken_for_manila(self):
        # Something else on the port answering HTTP is still not Manila, and
        # deferring to it would mean silently never starting.
        from http.server import SimpleHTTPRequestHandler

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *_):    # its 404 is expected, not news
                pass

        other = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(other.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(other.shutdown)
        self.assertFalse(server.already_serving("127.0.0.1", other.server_address[1]))


class TestWindowsLocations(unittest.TestCase):
    """The Windows half of the path code, exercised wherever this runs.

    Manila is written on Linux and used on Windows, so these branches would
    otherwise never execute under test -- which is precisely where a UNIX
    assumption survives unnoticed until someone opens the app on Windows.
    """

    def setUp(self):
        patch = mock.patch.object(project, "WINDOWS", True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_projects_and_registry_follow_windows_conventions(self):
        with mock.patch.dict(os.environ, {
                "LOCALAPPDATA": r"C:\Users\kim\AppData\Local",
                "APPDATA":      r"C:\Users\kim\AppData\Roaming"}):
            self.assertEqual(project.data_home(),
                             Path(r"C:\Users\kim\AppData\Local") / "Manila")
            self.assertEqual(project.registry_path(),
                             Path(r"C:\Users\kim\AppData\Roaming") / "Manila" / "projects.json")

    def test_a_missing_appdata_falls_back_beneath_home(self):
        # Both variables are normally set; an unusual shell is not a crash.
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("LOCALAPPDATA", None)
            os.environ.pop("APPDATA", None)
            self.assertEqual(project.data_home(),
                             Path.home() / "AppData" / "Local" / "Manila")
            self.assertEqual(project.config_home(),
                             Path.home() / "AppData" / "Roaming" / "Manila")

    def test_paths_differing_only_in_case_are_one_place(self):
        # They collide on disk, so registering both would mean two projects
        # sharing a database -- the one thing projects must never do.
        self.assertTrue(project._same_place(r"C:\Work\ACME", r"c:\work\acme"))

    def test_case_still_matters_everywhere_else(self):
        with mock.patch.object(project, "WINDOWS", False):
            self.assertFalse(project._same_place("/work/ACME", "/work/acme"))

    def test_drives_come_from_the_os_not_the_alphabet(self):
        # Walking A-Z means waiting on every disconnected mapped drive in turn,
        # and each one takes seconds to say no.
        with mock.patch.object(os, "listdrives", create=True,
                               return_value=["C:\\", "D:\\"]) as listed:
            project.places()
        self.assertTrue(listed.called, "places() should ask the OS which drives exist")

    def test_an_older_python_still_gets_a_drive_list(self):
        # os.listdrives arrived in 3.12; 3.11 falls back to the alphabet.
        with mock.patch.object(os, "listdrives", create=True,
                               side_effect=AttributeError):
            project.places()      # must not raise


class TestBinding(unittest.TestCase):
    """--tailscale must never widen the bind address beyond the tailnet."""

    def test_default_is_loopback(self):
        self.assertEqual(net.resolve_host(None, False), "127.0.0.1")

    def test_explicit_host_passes_through(self):
        self.assertEqual(net.resolve_host("192.168.1.5", False), "192.168.1.5")

    def test_tailscale_returns_a_tailnet_address(self):
        with mock.patch.object(net, "tailscale_ip", return_value="100.101.102.103"):
            self.assertEqual(net.resolve_host(None, True), "100.101.102.103")

    def test_tailscale_without_tailnet_refuses(self):
        with mock.patch.object(net, "tailscale_ip", return_value=None):
            with self.assertRaises(ValueError):
                net.resolve_host(None, True)

    def test_conflicting_flags_refuse(self):
        with self.assertRaises(ValueError):
            net.resolve_host("192.168.1.5", True)

    def test_tailscale_tolerates_redundant_loopback(self):
        with mock.patch.object(net, "tailscale_ip", return_value="100.64.0.1"):
            self.assertEqual(net.resolve_host("127.0.0.1", True), "100.64.0.1")

    def test_membership_boundaries(self):
        self.assertTrue(net.in_tailnet("100.64.0.0"))
        self.assertTrue(net.in_tailnet("100.127.255.255"))
        self.assertFalse(net.in_tailnet("100.63.255.255"))
        self.assertFalse(net.in_tailnet("100.128.0.0"))
        self.assertFalse(net.in_tailnet("192.168.1.5"))
        self.assertFalse(net.in_tailnet("not-an-ip"))

    def test_non_tailnet_output_is_rejected(self):
        # A `tailscale ip -4` that somehow prints a LAN address must not be
        # trusted -- we would bind wider than the user asked for.
        with mock.patch.object(net, "_run", return_value="192.168.1.5\n"):
            self.assertIsNone(net.tailscale_ip())

    def test_missing_command_is_not_an_error(self):
        with mock.patch.object(net, "_run", return_value=""):
            self.assertIsNone(net.tailscale_ip())

    def test_reach_names_a_typeable_url(self):
        self.assertEqual(net.reach("100.101.102.103", 8000), "http://100.101.102.103:8000")
        with mock.patch.object(net, "tailscale_ip", return_value="100.101.102.103"):
            self.assertEqual(net.reach("0.0.0.0", 8000), "http://100.101.102.103:8000")

    def test_windows_does_not_look_for_a_linux_interface(self):
        # The `ip` fallback reads tailscale0, which does not exist on Windows.
        # Asking anyway means shelling out for a guaranteed nothing.
        with mock.patch.object(net, "WINDOWS", True), \
             mock.patch.object(net, "_run", return_value="") as ran:
            self.assertIsNone(net.tailscale_ip())
        self.assertEqual(ran.call_count, 1, "only the tailscale CLI should be tried")

    def test_the_windows_refusal_says_where_the_cli_lives(self):
        # It installs outside PATH, so "not found" without that is a dead end.
        with mock.patch.object(net, "WINDOWS", True), \
             mock.patch.object(net, "tailscale_ip", return_value=None):
            with self.assertRaises(ValueError) as caught:
                net.resolve_host(None, True)
        self.assertIn("Program Files", str(caught.exception))


class TestServerRoutes(unittest.TestCase):
    """End to end over real HTTP. Unit tests exercise storage; only this catches
    a broken route table or a serializer that no longer matches the schema."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = Path(cls.dir.name)
        cls.saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_DATA_HOME"] = str(root / "data")

        cls.one = project.add("ACME Corp")
        cls.two = project.add("Coyote Corp")

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.dir.cleanup()

    def call(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_entry_page_is_served(self):
        status, _ = self.call("GET", "/api/schema")
        self.assertEqual(status, 200)

    def test_picker_lists_every_project(self):
        status, payload = self.call("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in payload["projects"]],
                         ["acme-corp", "coyote-corp"])

    def test_one_port_serves_both_projects_without_mixing_them(self):
        # The whole point of the single entry URL: same server, same port,
        # separate data.
        self.call("POST", "/api/p/acme-corp/folders", {"name": "Power"})
        self.call("POST", "/api/p/coyote-corp/folders", {"name": "Rockets"})

        _, acme = self.call("GET", "/api/p/acme-corp/folders")
        _, coyote = self.call("GET", "/api/p/coyote-corp/folders")
        here = {f["name"] for f in acme["folders"]}
        there = {f["name"] for f in coyote["folders"]}
        self.assertIn("Power", here)
        self.assertIn("Rockets", there)
        self.assertNotIn("Rockets", here, "a folder leaked between projects")
        self.assertNotIn("Power", there, "a folder leaked between projects")

    def test_rows_round_trip_through_a_scoped_route(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Rows"})
        fid = folder["id"]
        status, created = self.call(
            "POST", f"/api/p/acme-corp/folders/{fid}/rows/action_item",
            {"field": "item", "value": "Determine power state reporting"})
        self.assertEqual(status, 200)
        row = created["row"]
        self.assertEqual(row["fields"]["item"], "Determine power state reporting")

        status, _ = self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row['id']}",
                              {"field": "current_state", "value": "Kicked off"})
        self.assertEqual(status, 200)
        status, updated = self.call(
            "PATCH", f"/api/p/acme-corp/rows/action_item/{row['id']}",
            {"field": "current_state", "value": "Waiting on supplier quote"})
        self.assertEqual(updated["history_count"], 1)

        status, log = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{row['id']}/history/current_state")
        self.assertEqual(status, 200)
        self.assertEqual([e["old_value"] for e in log["entries"]], ["Kicked off"])
        self.assertEqual([e["new_value"] for e in log["entries"]], ["Waiting on supplier quote"])

    def test_a_new_entry_can_be_fixed_straight_away(self):
        # The window on for real: create, then correct at HTTP speed.
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Settle"})
        with mock.patch.object(history, "SETTLE_SECONDS", 10):
            _, created = self.call(
                "POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                {"field": "item", "value": "Determine power"})
            row = created["row"]["id"]
            _, updated = self.call(
                "PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                {"field": "item", "value": "Determine power state reporting"})
        self.assertTrue(updated["changed"])
        self.assertEqual(updated["history_count"], 0)

    def test_renaming_a_project_keeps_its_url_and_its_data(self):
        self.call("POST", "/api/p/coyote-corp/folders", {"name": "Keep me"})
        status, payload = self.call("PATCH", "/api/projects/coyote-corp",
                                    {"name": "Coyote Aerospace"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["project"]["name"], "Coyote Aerospace")
        self.assertEqual(payload["project"]["id"], "coyote-corp")
        status, folders = self.call("GET", "/api/p/coyote-corp/folders")
        self.assertEqual(status, 200)
        self.assertIn("Keep me", [f["name"] for f in folders["folders"]])

    def test_unknown_project_is_404_not_a_crash(self):
        status, payload = self.call("GET", "/api/p/nope/folders")
        self.assertEqual(status, 404)
        self.assertIn("nope", payload["error"])

    def test_wrong_method_on_a_real_route_is_405(self):
        status, _ = self.call("PATCH", "/api/p/acme-corp/folders")
        self.assertEqual(status, 405)

    def upload(self, frame_id, name, data, mime="application/octet-stream"):
        url = f"http://127.0.0.1:{self.port}/api/p/acme-corp/frames/{frame_id}/files"
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": mime, "X-Filename": name})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_frames_round_trip_and_hold_their_position(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Repo"})
        fid = folder["id"]
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{fid}/frames",
                            {"kind": "text", "title": "Kickoff notes"})
        frame = made["frame"]
        self.assertEqual(frame["kind"], "text")

        # Moved, resized, and typed into -- all of it must survive a reload.
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                  {"x": 120, "y": 40, "w": 500, "h": 300, "body": "Alice owns power"})
        _, listed = self.call("GET", f"/api/p/acme-corp/folders/{fid}/frames")
        again = listed["frames"][0]
        self.assertEqual([again["x"], again["y"], again["w"], again["h"]],
                         [120, 40, 500, 300])
        self.assertEqual(again["body"], "Alice owns power")

    def rich_frame(self, name):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": name})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "text", "title": "Notes"})
        return folder["id"], made["frame"]

    def body_of(self, folder_id, frame_id):
        _, listed = self.call("GET", f"/api/p/acme-corp/folders/{folder_id}/frames")
        return next(f for f in listed["frames"] if f["id"] == frame_id)

    def test_a_note_starts_plain(self):
        _, frame = self.rich_frame("Starts")
        self.assertEqual(frame["view"], "plain")

    def test_switching_to_rich_carries_the_note_over(self):
        fid, frame = self.rich_frame("Carried")
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                  {"body": "Kickoff\n- power\n- packaging"})
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}", {"view": "rich"})
        after = self.body_of(fid, frame["id"])
        self.assertEqual(after["view"], "rich")
        self.assertEqual(after["body"],
                         "<p>Kickoff</p><ul><li>power</li><li>packaging</li></ul>")

    def test_switching_back_to_plain_carries_it_back(self):
        fid, frame = self.rich_frame("Back")
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}", {"view": "rich"})
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                  {"body": "<p>Kickoff</p><ul><li>power</li></ul>"})
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}", {"view": "plain"})
        after = self.body_of(fid, frame["id"])
        self.assertEqual(after["view"], "plain")
        self.assertEqual(after["body"], "Kickoff\n- power")

    def test_a_rich_note_is_sanitised_before_it_is_stored(self):
        """The browser cleans a paste too; this is the copy that matters."""
        fid, frame = self.rich_frame("Hostile")
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}", {"view": "rich"})
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                  {"body": '<p onclick="steal()">note</p><script>alert(1)</script>'
                           '<img src=x onerror=alert(1)>'
                           '<a href="javascript:alert(1)">click</a>'})
        stored = self.body_of(fid, frame["id"])["body"]
        for banned in ("script", "onclick", "onerror", "javascript:", "<img"):
            self.assertNotIn(banned, stored, f"{banned} reached the database")
        self.assertIn("note", stored, "the words themselves are kept")
        self.assertIn("click", stored)

    def test_a_safe_link_survives_with_its_query_string(self):
        fid, frame = self.rich_frame("Linked")
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}", {"view": "rich"})
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                  {"body": '<p><a href="https://example.com/a?b=1&amp;c=2">spec</a></p>'})
        stored = self.body_of(fid, frame["id"])["body"]
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', stored)
        self.assertIn('rel="noopener noreferrer"', stored)

    def test_a_plain_note_is_not_touched_by_the_cleaner(self):
        # Plain notes hold text, and text with angle brackets in it is fine.
        fid, frame = self.rich_frame("Plain kept")
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                  {"body": "if a < b and c > d then"})
        self.assertEqual(self.body_of(fid, frame["id"])["body"],
                         "if a < b and c > d then")

    def test_an_unknown_view_is_refused(self):
        _, frame = self.rich_frame("Bad view")
        status, _ = self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                              {"view": "rendered-html"})
        self.assertEqual(status, 400)

    def test_bad_frame_kind_is_refused(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Kinds"})
        status, _ = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                              {"kind": "spreadsheet"})
        self.assertEqual(status, 400)

    def test_a_document_is_copied_in_and_served_back(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Docs"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files", "title": "Specs"})
        frame = made["frame"]

        payload = self.upload(frame["id"], "Power Spec.pdf", b"%PDF-1.7 pretend")
        stored = payload["file"]
        self.assertEqual(stored["name"], "Power Spec.pdf")
        self.assertEqual(stored["size"], 16)

        # The copy is inside the project, not a reference to somewhere else.
        copy = Path(stored["path"])
        self.assertTrue(copy.is_file())
        self.assertTrue(copy.is_relative_to(self.one.path))
        self.assertEqual(copy.read_bytes(), b"%PDF-1.7 pretend")

        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{stored['url']}", timeout=5) as response:
            self.assertEqual(response.read(), b"%PDF-1.7 pretend")
            self.assertEqual(response.headers["Content-Type"], "application/pdf")
            self.assertIn("Power Spec.pdf", response.headers["Content-Disposition"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_spaces_in_a_filename_survive_the_upload(self):
        # The name travels percent-encoded in a header; if it is not decoded the
        # user reads "Power%20Spec.pdf" for the life of the document.
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Names"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        frame = made["frame"]
        for sent, expected in [("Power%20Spec%20v3.pdf", "Power Spec v3.pdf"),
                               ("Q3%20plan%20%28final%29.docx", "Q3 plan (final).docx"),
                               ("plain.txt", "plain.txt")]:
            stored = self.upload(frame["id"], sent, b"bytes")["file"]
            self.assertEqual(stored["name"], expected)
            self.assertNotIn("%", stored["name"])
            self.assertNotIn("%", Path(stored["path"]).name)

    def test_a_document_can_be_described_in_your_own_words(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Described"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        frame = made["frame"]
        stored = self.upload(frame["id"], "Power Spec v3.pdf", b"bytes")["file"]
        self.assertEqual(stored["description"], "", "a fresh document describes nothing")

        status, payload = self.call(
            "PATCH", f"/api/p/acme-corp/files/{stored['id']}",
            {"description": "The one Alice marked up, not the supplier's draft"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["file"]["description"],
                         "The one Alice marked up, not the supplier's draft")

        _, listed = self.call("GET", f"/api/p/acme-corp/folders/{folder['id']}/frames")
        held = listed["frames"][0]["files"][0]
        self.assertEqual(held["description"], "The one Alice marked up, not the supplier's draft")
        self.assertEqual(held["name"], "Power Spec v3.pdf", "the filename is untouched")

    def test_a_description_can_be_cleared_again(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Cleared"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        stored = self.upload(made["frame"]["id"], "notes.txt", b"bytes")["file"]
        self.call("PATCH", f"/api/p/acme-corp/files/{stored['id']}",
                  {"description": "temporary"})
        _, payload = self.call("PATCH", f"/api/p/acme-corp/files/{stored['id']}",
                               {"description": "   "})
        self.assertEqual(payload["file"]["description"], "")

    def test_describing_a_document_that_is_not_there(self):
        status, _ = self.call("PATCH", "/api/p/acme-corp/files/999999",
                              {"description": "ghost"})
        self.assertEqual(status, 404)

    def test_a_patch_with_nothing_in_it_is_refused(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Empty patch"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        stored = self.upload(made["frame"]["id"], "notes.txt", b"bytes")["file"]
        status, _ = self.call("PATCH", f"/api/p/acme-corp/files/{stored['id']}", {})
        self.assertEqual(status, 400)

    def test_a_description_survives_its_frame_being_moved(self):
        # The note belongs to the document, not to where the frame sits.
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Moved"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        frame = made["frame"]
        stored = self.upload(frame["id"], "spec.pdf", b"bytes")["file"]
        self.call("PATCH", f"/api/p/acme-corp/files/{stored['id']}",
                  {"description": "signed copy"})
        self.call("PATCH", f"/api/p/acme-corp/frames/{frame['id']}",
                  {"x": 300, "y": 400, "title": "Specs"})
        _, listed = self.call("GET", f"/api/p/acme-corp/folders/{folder['id']}/frames")
        self.assertEqual(listed["frames"][0]["files"][0]["description"], "signed copy")

    def test_a_hostile_filename_cannot_escape_the_project(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Hostile"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        payload = self.upload(made["frame"]["id"], "../../../../tmp/escaped.txt", b"nope")
        copy = Path(payload["file"]["path"])
        self.assertTrue(copy.is_relative_to(self.one.path), copy)
        self.assertFalse(Path(Path("/").anchor, "tmp", "escaped.txt").exists())

    def test_deleting_a_frame_removes_its_copies_from_disk(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Sweep"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        frame = made["frame"]
        copy = Path(self.upload(frame["id"], "throwaway.txt", b"bytes")["file"]["path"])
        self.assertTrue(copy.is_file())

        status, result = self.call("POST", f"/api/p/acme-corp/frames/{frame['id']}/delete")
        self.assertEqual(status, 200)
        self.assertEqual(result["files"], 1)
        self.assertFalse(copy.exists(), "the frame's copies must go with it")

    def test_a_missing_file_on_disk_reports_rather_than_crashes(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Gone"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/frames",
                            {"kind": "files"})
        stored = self.upload(made["frame"]["id"], "vanishing.txt", b"bytes")["file"]
        Path(stored["path"]).unlink()
        status, payload = self.call("GET", stored["url"])
        self.assertEqual(status, 410)
        self.assertIn("vanishing.txt", payload["error"])

    def test_deleting_a_folder_takes_its_documents_with_it(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Whole folder"})
        fid = folder["id"]
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{fid}/frames",
                            {"kind": "files"})
        copy = Path(self.upload(made["frame"]["id"], "in here.txt", b"bytes")["file"]["path"])
        self.assertTrue(copy.is_file())

        status, result = self.call("POST", f"/api/p/acme-corp/folders/{fid}/delete")
        self.assertEqual(status, 200)
        self.assertEqual(result["files"], 1)
        self.assertFalse(copy.exists(), "a deleted folder must not orphan its files")
        self.assertFalse(copy.parent.exists())
        # And the project itself is untouched.
        self.assertTrue(self.one.path.is_dir())

    def test_deleting_a_folder_drops_its_frames(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Framed"})
        fid = folder["id"]
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{fid}/frames",
                            {"kind": "text", "title": "Notes"})
        self.call("POST", f"/api/p/acme-corp/folders/{fid}/delete")
        status, _ = self.call("PATCH", f"/api/p/acme-corp/frames/{made['frame']['id']}",
                              {"title": "still here?"})
        self.assertEqual(status, 404)

    def test_suggestions_come_from_what_was_already_typed(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Suggest"})
        fid = folder["id"]
        for who in ("Alice", "Bob", "Alice"):
            self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/action_item",
                      {"field": "waiting_on", "value": who})
        status, payload = self.call(
            "GET", f"/api/p/acme-corp/folders/{fid}/suggest/action_item/waiting_on")
        self.assertEqual(status, 200)
        # Most used first: the name you keep chasing is the one you want offered.
        self.assertEqual(payload["values"], ["Alice", "Bob"])

    def test_suggestions_do_not_cross_folders(self):
        _, mine = self.call("POST", "/api/p/acme-corp/folders", {"name": "Mine"})
        _, theirs = self.call("POST", "/api/p/acme-corp/folders", {"name": "Theirs"})
        self.call("POST", f"/api/p/acme-corp/folders/{mine['id']}/rows/action_item",
                  {"field": "waiting_on", "value": "Only Here"})
        _, payload = self.call(
            "GET", f"/api/p/acme-corp/folders/{theirs['id']}/suggest/action_item/waiting_on")
        self.assertNotIn("Only Here", payload["values"])

    def test_a_column_without_suggestions_refuses(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "NoSuggest"})
        status, _ = self.call(
            "GET", f"/api/p/acme-corp/folders/{folder['id']}/suggest/action_item/current_state")
        self.assertEqual(status, 400)
        # And an unknown field is refused rather than reaching the SQL.
        status, _ = self.call(
            "GET", f"/api/p/acme-corp/folders/{folder['id']}/suggest/action_item/nonsense")
        self.assertEqual(status, 404)

    def test_last_touched_carries_when_how_and_note(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Touched"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "item", "value": "Supplier quote"})
        row = made["row"]["id"]
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                  {"field": "last_touched",
                   "value": {"date": "2026-08-20", "via": "Call", "note": "left a message"}})
        _, listed = self.call("GET", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item")
        value = listed["rows"][0]["fields"]["last_touched"]
        self.assertEqual(value, {"date": "2026-08-20", "via": "Call",
                                 "note": "left a message"})

    def test_browse_lists_directories_and_can_climb(self):
        status, view = self.call("GET",
            f"/api/browse?path={urllib.parse.quote(str(self.one.path))}")
        self.assertEqual(status, 200)
        self.assertEqual(view["path"], str(self.one.path.resolve()))
        self.assertTrue(view["writable"])
        self.assertEqual(view["sep"], os.sep)
        self.assertIsNotNone(view["parent"])

    def test_browse_refuses_a_path_that_is_not_a_directory(self):
        status, _ = self.call("GET", "/api/browse?path=/definitely/not/here/at/all")
        self.assertEqual(status, 400)

    def test_a_folder_can_be_made_while_choosing_a_location(self):
        parent = Path(self.dir.name) / "chooser"
        parent.mkdir()
        status, made = self.call("POST", "/api/browse",
                                 {"parent": str(parent), "name": "Clients"})
        self.assertEqual(status, 200)
        self.assertTrue(Path(made["path"]).is_dir())
        # And the project the folder was made for lands inside it.
        _, payload = self.call("POST", "/api/projects",
                               {"name": "Made A Home", "parent": made["path"]})
        self.assertEqual(Path(payload["project"]["path"]).parent,
                         Path(made["path"]))
        self.call("POST", f"/api/projects/{payload['project']['id']}/forget")

    def test_making_a_folder_refuses_a_name_that_is_a_path(self):
        status, payload = self.call("POST", "/api/browse",
                                    {"parent": self.dir.name, "name": "../escaped"})
        self.assertEqual(status, 400)
        self.assertIn("path", payload["error"])
        self.assertFalse((Path(self.dir.name).parent / "escaped").exists())

    def test_places_are_real_directories(self):
        status, payload = self.call("GET", "/api/places")
        self.assertEqual(status, 200)
        self.assertTrue(payload["places"])
        for place in payload["places"]:
            self.assertTrue(Path(place["path"]).is_dir(), place)

    def test_a_project_can_be_created_inside_a_chosen_directory(self):
        parent = Path(self.dir.name) / "backed up"
        parent.mkdir()
        status, payload = self.call("POST", "/api/projects",
                                    {"name": "On The Share", "parent": str(parent)})
        self.assertEqual(status, 200)
        made = Path(payload["project"]["path"])
        self.assertEqual(made.parent, parent)
        self.assertTrue(made.is_dir())
        self.call("POST", f"/api/projects/{payload['project']['id']}/forget")

    def test_a_project_carried_from_another_pc_is_opened_over_http(self):
        """The picker's other button: a folder that is already a project,
        registered without anything being created or moved."""
        carried = Path(self.dir.name) / "carried"
        carried.mkdir()
        conn = db.connect(carried / "manila.db", init=True)
        conn.execute("INSERT INTO folder (name, position, created_at)"
                     " VALUES ('Acme', 1, 'now')")
        conn.close()

        status, payload = self.call("POST", "/api/projects/import",
                                    {"path": str(carried)})
        self.assertEqual(status, 200)
        self.assertEqual(payload["project"]["path"], str(carried))
        self.assertEqual(payload["project"]["folders"], 1)

        # It is in the picker, and its folder is readable through its own id.
        opened = payload["project"]["id"]
        _, listed = self.call("GET", f"/api/p/{opened}/folders")
        self.assertEqual([f["name"] for f in listed["folders"]], ["Acme"])
        self.call("POST", f"/api/projects/{opened}/forget")

    def test_opening_a_folder_that_is_not_a_project_is_an_error_not_a_project(self):
        plain = Path(self.dir.name) / "just a folder"
        plain.mkdir()
        status, payload = self.call("POST", "/api/projects/import", {"path": str(plain)})
        self.assertEqual(status, 400)
        self.assertIn("manila.db", payload["error"])
        self.assertFalse((plain / "manila.db").exists())

    def test_unarchiving_brings_it_back_unticked(self):
        """Archive is where finished things go. Bringing one back means it
        needs attention again, so it must not still read as done."""
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Revived"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "item", "value": "Finished, or so I thought"})
        row = made["row"]["id"]
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                  {"field": "done", "value": True})
        self.call("POST", f"/api/p/acme-corp/rows/action_item/{row}/archive")

        _, archived = self.call(
            "GET", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item?scope=archived")
        self.assertTrue(archived["rows"][0]["fields"]["done"], "it was done when parked")

        self.call("POST", f"/api/p/acme-corp/rows/action_item/{row}/unarchive")
        _, active = self.call(
            "GET", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item")
        back = next(r for r in active["rows"] if r["id"] == row)
        self.assertFalse(back["fields"]["done"], "it came back still struck through")
        self.assertEqual(back["fields"]["item"], "Finished, or so I thought",
                         "and nothing else was disturbed")

    def test_unarchiving_a_row_with_no_tick_box_is_fine(self):
        # Milestones and contacts have no done column; the same route serves
        # all three.
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "No box"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/milestone",
                            {"field": "deliverable", "value": "Gate"})
        row = made["row"]["id"]
        self.call("POST", f"/api/p/acme-corp/rows/milestone/{row}/archive")
        status, _ = self.call("POST", f"/api/p/acme-corp/rows/milestone/{row}/unarchive")
        self.assertEqual(status, 200)

    def test_unticking_on_unarchive_leaves_no_history(self):
        # The tick box is not tracked, so this must not add a revision.
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Quiet revive"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "item", "value": "Tick"})
        row = made["row"]["id"]
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                  {"field": "done", "value": True})
        self.call("POST", f"/api/p/acme-corp/rows/action_item/{row}/archive")
        self.call("POST", f"/api/p/acme-corp/rows/action_item/{row}/unarchive")
        _, log = self.call("GET", f"/api/p/acme-corp/rows/action_item/{row}/history")
        self.assertEqual(log["entries"], [])

    def test_a_whole_entry_replays_as_one_story(self):
        """The reason for the row reading: changing several cells in one sitting
        is one event, and per cell it reads as three unrelated ones."""
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Story"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "item", "value": "Supplier quote"})
        row = made["row"]["id"]
        edits = [("waiting_on", "Alice"), ("waiting_on", "Bob"),
                 ("current_state", "Kicked off"), ("current_state", "Blocked")]
        for field, value in edits:
            self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                      {"field": field, "value": value})

        status, log = self.call("GET", f"/api/p/acme-corp/rows/action_item/{row}/history")
        self.assertEqual(status, 200)
        # Two revisions, from two different cells, in one list.
        self.assertEqual({e["field"] for e in log["entries"]},
                         {"waiting_on", "current_state"})
        self.assertEqual([e["old_value"] for e in log["entries"]],
                         ["Kicked off", "Alice"])

    def test_the_row_reading_and_the_cell_reading_agree(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Agree"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "waiting_on", "value": "Alice"})
        row = made["row"]["id"]
        for value in ("Bob", "Carol"):
            self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                      {"field": "waiting_on", "value": value})

        _, whole = self.call("GET", f"/api/p/acme-corp/rows/action_item/{row}/history")
        _, cell = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{row}/history/waiting_on")
        # Narrowing is a filter on the same log, not a different log.
        self.assertEqual([e["id"] for e in whole["entries"] if e["field"] == "waiting_on"],
                         [e["id"] for e in cell["entries"]])

    def test_an_inherited_slip_appears_in_the_whole_entry(self):
        fid, stone, action = self.borrowing_setup("Whole slip")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "", "link": stone}})
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2026-11-01"})
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "current_state", "value": "Blocked"})
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "current_state", "value": "Moving again"})

        _, log = self.call("GET", f"/api/p/acme-corp/rows/action_item/{action}/history")
        borrowed = [e for e in log["entries"] if e.get("inherited")]
        self.assertEqual(len(borrowed), 1)
        self.assertEqual(borrowed[0]["field"], "resolve_by",
                         "an inherited entry belongs to the cell that borrowed it")
        # The deliverable slipping and the state changing are one story.
        self.assertIn("current_state", {e["field"] for e in log["entries"]})

    def test_a_row_with_no_history_replays_nothing(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Quiet"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "item", "value": "Just typed"})
        _, log = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{made['row']['id']}/history")
        self.assertEqual(log["entries"], [], "a first fill is not a revision")

    def test_replaying_a_row_that_is_not_there(self):
        status, _ = self.call("GET", "/api/p/acme-corp/rows/action_item/999999/history")
        self.assertEqual(status, 404)

    def test_the_done_box_stays_out_of_the_story(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Ticked"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "item", "value": "Tick me"})
        row = made["row"]["id"]
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                  {"field": "done", "value": True})
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                  {"field": "done", "value": False})
        _, log = self.call("GET", f"/api/p/acme-corp/rows/action_item/{row}/history")
        self.assertNotIn("done", {e["field"] for e in log["entries"]})

    def test_deleting_a_history_entry_reports_the_new_count(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Forget"})
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item",
                            {"field": "waiting_on", "value": "Alice"})
        row = made["row"]["id"]
        for value in ("Allice", "Bob"):
            self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{row}",
                      {"field": "waiting_on", "value": value})
        _, log = self.call("GET", f"/api/p/acme-corp/rows/action_item/{row}/history/waiting_on")
        self.assertEqual(len(log["entries"]), 2)

        typo = next(e for e in log["entries"] if e["old_value"] == "Allice")
        status, result = self.call("POST", f"/api/p/acme-corp/history/{typo['id']}/delete")
        self.assertEqual(status, 200)
        # The chip has to show the new count without a reload.
        self.assertEqual(result["history_count"], 1)

        _, after = self.call("GET", f"/api/p/acme-corp/rows/action_item/{row}/history/waiting_on")
        self.assertEqual([e["old_value"] for e in after["entries"]], ["Alice"])
        # And the cell still says what it said.
        _, rows = self.call("GET", f"/api/p/acme-corp/folders/{folder['id']}/rows/action_item")
        self.assertEqual(rows["rows"][0]["fields"]["waiting_on"], "Bob")

    def test_deleting_an_unknown_history_entry_is_404(self):
        status, _ = self.call("POST", "/api/p/acme-corp/history/999999/delete")
        self.assertEqual(status, 404)

    def borrowing_setup(self, name):
        """A folder with one milestone and one action, ready to be linked."""
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": name})
        fid = folder["id"]
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/milestone",
                            {"field": "deliverable", "value": "Phase One Signoff"})
        stone = made["row"]["id"]
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2026-09-15"})
        _, act = self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/action_item",
                           {"field": "item", "value": "Supplier quote"})
        return fid, stone, act["row"]["id"]

    def resolve_by(self, fid, action):
        _, listed = self.call("GET", f"/api/p/acme-corp/folders/{fid}/rows/action_item")
        return next(r for r in listed["rows"] if r["id"] == action)["fields"]["resolve_by"]

    def test_a_borrowed_date_is_read_from_its_deliverable(self):
        fid, stone, action = self.borrowing_setup("Borrow")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "ASAP", "link": stone}})
        value = self.resolve_by(fid, action)
        self.assertEqual(value["date"], "2026-09-15")
        self.assertEqual(value["link_label"], "Phase One Signoff")
        self.assertEqual(value["note"], "ASAP")

    def test_a_slipped_deliverable_moves_everything_waiting_on_it(self):
        # The whole point: one edit on the milestone, no edits on the actions.
        fid, stone, action = self.borrowing_setup("Slip")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "", "link": stone}})
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2026-10-30"})
        self.assertEqual(self.resolve_by(fid, action)["date"], "2026-10-30")

        # The slip is stored once, on the milestone. The action's panel shows
        # it too, but as an inherited entry rather than a copy.
        _, theirs = self.call(
            "GET", f"/api/p/acme-corp/rows/milestone/{stone}/history/due_date")
        _, ours = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{action}/history/resolve_by")
        self.assertEqual([e["old_value"] for e in theirs["entries"]], ["2026-09-15"])
        self.assertEqual([e for e in ours["entries"] if not e.get("inherited")], [])
        self.assertEqual([e["new_value"] for e in ours["entries"] if e.get("inherited")],
                         ["2026-10-30"])

    def test_unlinking_keeps_the_date_it_had(self):
        fid, stone, action = self.borrowing_setup("Unlink")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "", "link": stone}})
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by",
                   "value": {"date": "2026-09-15", "note": "", "link": 0}})
        value = self.resolve_by(fid, action)
        self.assertEqual(value["date"], "2026-09-15")
        self.assertEqual(value["link"], 0)
        self.assertNotIn("link_label", value)
        # Now it is genuinely its own: the milestone moving does not touch it.
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2027-01-01"})
        self.assertEqual(self.resolve_by(fid, action)["date"], "2026-09-15")

    def test_deleting_a_deliverable_leaves_the_date_behind(self):
        fid, stone, action = self.borrowing_setup("Delete")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "EOM", "link": stone}})
        self.call("POST", f"/api/p/acme-corp/rows/milestone/{stone}/delete")
        value = self.resolve_by(fid, action)
        # Nothing dangles and nothing loses its meaning.
        self.assertEqual(value["date"], "2026-09-15")
        self.assertEqual(value["link"], 0)
        self.assertEqual(value["note"], "EOM")

    def test_a_deliverable_with_no_date_yet_is_not_overdue(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Undated"})
        fid = folder["id"]
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/milestone",
                            {"field": "deliverable", "value": "Someday"})
        _, act = self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/action_item",
                           {"field": "item", "value": "Waits on someday"})
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{act['row']['id']}",
                  {"field": "resolve_by",
                   "value": {"date": "", "note": "", "link": made["row"]["id"]}})
        value = self.resolve_by(fid, act["row"]["id"])
        self.assertEqual(value["date"], "")
        self.assertEqual(value["link_label"], "Someday")

    def test_link_options_are_the_folders_own_deliverables(self):
        fid, stone, _ = self.borrowing_setup("Options")
        _, elsewhere = self.call("POST", "/api/p/acme-corp/folders", {"name": "Elsewhere"})
        self.call("POST", f"/api/p/acme-corp/folders/{elsewhere['id']}/rows/milestone",
                  {"field": "deliverable", "value": "Not Mine"})

        status, payload = self.call(
            "GET", f"/api/p/acme-corp/folders/{fid}/links/action_item/resolve_by")
        self.assertEqual(status, 200)
        labels = [o["label"] for o in payload["options"]]
        self.assertEqual(labels, ["Phase One Signoff"])
        self.assertEqual(payload["options"][0]["date"], "2026-09-15")

    def test_a_deliverable_added_now_is_offered_now(self):
        """The picker is fed by a live query, so a milestone added a moment ago
        is borrowable straight away."""
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Fresh"})
        fid = folder["id"]
        _, empty = self.call(
            "GET", f"/api/p/acme-corp/folders/{fid}/links/action_item/resolve_by")
        self.assertEqual(empty["options"], [])

        self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/milestone",
                  {"field": "deliverable", "value": "Phase Two Build"})
        _, now = self.call(
            "GET", f"/api/p/acme-corp/folders/{fid}/links/action_item/resolve_by")
        self.assertEqual([o["label"] for o in now["options"]], ["Phase Two Build"])

    def test_an_unnamed_deliverable_is_not_offered(self):
        # A blank row at the bottom of the milestones table is not a thing to
        # resolve by.
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Unnamed"})
        fid = folder["id"]
        self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/milestone",
                  {"field": "due_date", "value": "2026-09-15"})
        _, options = self.call(
            "GET", f"/api/p/acme-corp/folders/{fid}/links/action_item/resolve_by")
        self.assertEqual(options["options"], [])

    def test_an_archived_deliverable_is_not_offered(self):
        _, folder = self.call("POST", "/api/p/acme-corp/folders", {"name": "Archived"})
        fid = folder["id"]
        _, made = self.call("POST", f"/api/p/acme-corp/folders/{fid}/rows/milestone",
                            {"field": "deliverable", "value": "Old Gate"})
        self.call("POST", f"/api/p/acme-corp/rows/milestone/{made['row']['id']}/archive")
        _, options = self.call(
            "GET", f"/api/p/acme-corp/folders/{fid}/links/action_item/resolve_by")
        self.assertEqual(options["options"], [])

    def test_a_column_that_borrows_nothing_refuses(self):
        fid, _, _ = self.borrowing_setup("NoBorrow")
        status, _ = self.call(
            "GET", f"/api/p/acme-corp/folders/{fid}/links/action_item/last_touched")
        self.assertEqual(status, 400)

    def test_linking_to_something_that_is_not_there(self):
        fid, _, action = self.borrowing_setup("Ghost")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "x", "link": 999999}})
        value = self.resolve_by(fid, action)
        self.assertEqual(value["link"], 0, "a link to nothing must not be stored")

    def test_a_slip_shows_in_the_actions_own_history(self):
        """The point of the merge: the action's cell can replay its own date."""
        fid, stone, action = self.borrowing_setup("Replay")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "ASAP", "link": stone}})
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2026-10-30"})

        _, log = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{action}/history/resolve_by")
        borrowed = [e for e in log["entries"] if e.get("inherited")]
        self.assertEqual(len(borrowed), 1, "the slip must be replayable from here")
        self.assertEqual(borrowed[0]["new_value"], "2026-10-30")
        self.assertEqual(borrowed[0]["source"], "Phase One Signoff")

        # Still stored once, on the milestone. Linking a blank cell is a first
        # fill and writes nothing, and the slip writes nothing here either --
        # the action's panel is assembled, not accumulated.
        own = self.conn_rows(
            "SELECT COUNT(*) AS n FROM field_history"
            " WHERE entity_type = 'action_item' AND field = 'resolve_by'")
        self.assertEqual(own, 0, "a slip must not be copied into every action")

    def conn_rows(self, sql):
        c = db.connect(self.one.db_path)
        try:
            return c.execute(sql).fetchone()["n"]
        finally:
            c.close()

    def test_the_chip_counts_what_the_panel_shows(self):
        fid, stone, action = self.borrowing_setup("Counting")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "ASAP", "link": stone}})
        for date in ("2026-10-30", "2026-11-30"):
            self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                      {"field": "due_date", "value": date})

        _, log = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{action}/history/resolve_by")
        _, rows = self.call("GET", f"/api/p/acme-corp/folders/{fid}/rows/action_item")
        chip = next(r for r in rows["rows"] if r["id"] == action)["history_counts"]["resolve_by"]
        self.assertEqual(chip, len(log["entries"]),
                         "a chip that disagrees with its panel is worse than no chip")

    def test_slips_from_before_the_link_are_not_claimed(self):
        # The action was not following this deliverable then, so its date did
        # not move with it, and its history must not say otherwise.
        fid, stone, action = self.borrowing_setup("Before")
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2026-10-01"})   # slip, unlinked
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "", "link": stone}})
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2026-11-01"})   # slip, linked

        _, log = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{action}/history/resolve_by")
        borrowed = [e["new_value"] for e in log["entries"] if e.get("inherited")]
        self.assertEqual(borrowed, ["2026-11-01"])

    def test_unlinking_stops_inheriting(self):
        fid, stone, action = self.borrowing_setup("Stop")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "", "link": stone}})
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by",
                   "value": {"date": "2026-09-15", "note": "", "link": 0}})
        self.call("PATCH", f"/api/p/acme-corp/rows/milestone/{stone}",
                  {"field": "due_date", "value": "2027-03-01"})

        _, log = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{action}/history/resolve_by")
        self.assertEqual([e for e in log["entries"] if e.get("inherited")], [])

    def test_linking_to_a_matching_date_is_a_real_entry_not_a_duplicate(self):
        # Before the source was written into the log, this wrote an entry whose
        # two sides read identically.
        fid, stone, action = self.borrowing_setup("Same")
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by",
                   "value": {"date": "2026-09-15", "note": "ASAP", "link": 0}})
        self.call("PATCH", f"/api/p/acme-corp/rows/action_item/{action}",
                  {"field": "resolve_by", "value": {"date": "", "note": "ASAP", "link": stone}})
        _, log = self.call(
            "GET", f"/api/p/acme-corp/rows/action_item/{action}/history/resolve_by")
        entry = next(e for e in log["entries"] if not e.get("inherited"))
        self.assertEqual(entry["old_value"], "2026-09-15|ASAP")
        self.assertIn("Phase One Signoff", entry["new_value"])
        self.assertNotEqual(entry["old_value"], entry["new_value"])

    def test_unscoped_row_routes_no_longer_exist(self):
        # Data routes must be reachable only through a named project.
        status, _ = self.call("GET", "/api/folders")
        self.assertEqual(status, 404)


class TestWhoIsAllowedToAsk(unittest.TestCase):
    """Manila has no login, so the browser's own rules are the fence -- and two
    of them do not hold on their own.

    A page on any site can post a form at 127.0.0.1 without asking anyone, and
    a name its owner controls can be pointed at 127.0.0.1 so that their page
    and this server look to the browser like one origin. The first would let
    another site delete your work; the second would let it read everything.
    These are what stops both."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = Path(cls.dir.name)
        cls.saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        project.add("Guarded")

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.httpd.allowed = server.Allowed("127.0.0.1", cls.port)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.dir.cleanup()

    def ask(self, method, path, body=None, headers=None, host=None):
        """One request, with whatever headers a caller would really send."""
        sent = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            sent.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers=sent)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def folders(self):
        return self.ask("GET", "/api/p/guarded/folders")[1]["folders"]

    # --- a write from somewhere else ----------------------------------------

    def test_a_write_from_another_site_is_refused(self):
        """A form on any page can reach this port, with no preflight to stop
        it. What it cannot do is lie about which site it came from."""
        status, payload = self.ask(
            "POST", "/api/p/guarded/folders", {"name": "From elsewhere"},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("another site", payload["error"])
        self.assertNotIn("From elsewhere", [f["name"] for f in self.folders()])

    def test_a_delete_from_another_site_is_refused_too(self):
        """The dangerous shape: no body at all, so nothing about the request
        needs a preflight and the browser sends it happily."""
        self.ask("POST", "/api/p/guarded/folders", {"name": "Keep me"})
        folder = self.folders()[0]["id"]
        status, _ = self.ask("POST", f"/api/p/guarded/folders/{folder}/delete",
                             headers={"Origin": "https://evil.example",
                                      "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertIn("Keep me", [f["name"] for f in self.folders()])
        self.ask("POST", f"/api/p/guarded/folders/{folder}/delete")

    def test_a_page_with_no_origin_of_its_own_is_refused(self):
        """A file:// page and a sandboxed frame both send "null", which is not
        an origin Manila could ever have served."""
        status, _ = self.ask("POST", "/api/p/guarded/folders", {"name": "null"},
                             headers={"Origin": "null"})
        self.assertEqual(status, 403)

    def test_a_cross_site_fetch_is_refused_even_without_an_origin(self):
        status, _ = self.ask("POST", "/api/p/guarded/folders", {"name": "fetched"},
                             headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertNotIn("fetched", [f["name"] for f in self.folders()])

    def test_manilas_own_page_writes_as_before(self):
        status, _ = self.ask("POST", "/api/p/guarded/folders", {"name": "Mine"},
                             headers={"Origin": f"http://127.0.0.1:{self.port}",
                                      "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertIn("Mine", [f["name"] for f in self.folders()])

    def test_a_scripted_caller_is_not_what_this_is_about(self):
        """curl, a script, the MCP server: no Origin, no Sec-Fetch-Site, and no
        browser in the middle for another site to borrow."""
        status, _ = self.ask("POST", "/api/p/guarded/folders", {"name": "Scripted"})
        self.assertEqual(status, 200)
        self.assertIn("Scripted", [f["name"] for f in self.folders()])

    # --- a name pointed at this machine --------------------------------------

    def test_a_name_manila_does_not_answer_to_is_refused(self):
        """DNS rebinding: their name, resolved to 127.0.0.1, would otherwise be
        same-origin with this server and could read every project."""
        status, payload = self.ask("GET", "/api/projects", host="evil.example.com")
        self.assertEqual(status, 403)
        self.assertIn("host", payload["error"])

    def test_the_names_this_machine_is_reached_by_are_answered(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}",
                     f"[::1]:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.ask("GET", "/api/schema", host=host)[0], 200)

    def test_the_right_name_on_the_wrong_port_is_refused(self):
        self.assertEqual(
            self.ask("GET", "/api/schema", host="127.0.0.1:1")[0], 403)

    def test_the_static_page_is_guarded_as_well_as_the_api(self):
        self.assertEqual(self.ask("GET", "/", host="evil.example.com")[0], 403)

    def test_the_app_page_says_what_it_is_allowed_to_do(self):
        """A note is sanitised on the way in; this is what would hold if one
        ever got past that."""
        with urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{self.port}/",
                                       headers={"Host": f"127.0.0.1:{self.port}"}),
                timeout=5) as response:
            policy = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("object-src 'none'", policy)


class TestWhichNamesAreAnswered(unittest.TestCase):
    """The rules themselves, without a socket in the way."""

    def test_a_host_header_is_split_the_way_browsers_write_it(self):
        self.assertEqual(server.split_host("127.0.0.1:8000"), ("127.0.0.1", "8000"))
        self.assertEqual(server.split_host("[::1]:8000"), ("::1", "8000"))
        self.assertEqual(server.split_host("Localhost"), ("localhost", ""))
        self.assertEqual(server.split_host(""), ("", ""))

    def test_loopback_answers_to_the_usual_names(self):
        allowed = server.Allowed("127.0.0.1", 8000)
        for name in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"):
            self.assertTrue(allowed.says(name), name)
        for name in ("evil.example.com:8000", "evil.example.com", "", None,
                     "127.0.0.1:9999"):
            self.assertFalse(allowed.says(name), name)

    def test_a_tailnet_answers_to_magicdns_but_only_there(self):
        """A MagicDNS name is Tailscale's to hand out, so it cannot be the name
        someone else points at this machine -- but it is only meaningful when
        Manila is actually serving the tailnet."""
        tailnet = server.Allowed("100.73.20.25", 8000)
        self.assertTrue(tailnet.says("my-laptop.example-tailnet.ts.net:8000"))
        self.assertTrue(tailnet.says("100.73.20.25:8000"))
        self.assertFalse(tailnet.says("evil.example.com:8000"))

        local = server.Allowed("127.0.0.1", 8000)
        self.assertFalse(local.says("my-laptop.example-tailnet.ts.net:8000"))

    def test_a_name_of_your_own_can_be_added(self):
        allowed = server.Allowed("127.0.0.1", 8000, ["desk.lan"])
        self.assertTrue(allowed.says("desk.lan:8000"))
        self.assertFalse(allowed.says("other.lan:8000"))

    def test_bound_to_everything_the_port_is_all_there_is_to_check(self):
        """--host 0.0.0.0 is a deliberate "answer anywhere", and nothing in the
        request says which of this machine's names is the legitimate one."""
        allowed = server.Allowed("0.0.0.0", 8000)
        self.assertTrue(allowed.says("whatever.example:8000"))
        self.assertFalse(allowed.says("whatever.example:9999"))


class TestRichText(unittest.TestCase):
    """A rich note is HTML, so what reaches the database has to be inert --
    and switching a note between the two editors has to cost nothing."""

    def test_structure_and_emphasis_are_kept(self):
        self.assertEqual(richtext.clean("<p>hi <b>there</b> and <i>so</i></p>"),
                         "<p>hi <strong>there</strong> and <em>so</em></p>")

    def test_a_script_is_dropped_with_its_contents(self):
        self.assertEqual(richtext.clean("<script>alert(1)</script><p>after</p>"),
                         "<p>after</p>")

    def test_attributes_never_survive(self):
        cleaned = richtext.clean('<p onclick="steal()" style="color:red">x</p>')
        self.assertEqual(cleaned, "<p>x</p>")

    def test_an_image_with_a_handler_is_dropped(self):
        cleaned = richtext.clean("<img src=x onerror=alert(1)>words")
        self.assertNotIn("<img", cleaned)
        self.assertEqual(cleaned, "words")

    def test_only_safe_schemes_become_links(self):
        for hostile in ("javascript:alert(1)", "data:text/html,<b>x</b>",
                        "vbscript:x", "file:///etc/passwd"):
            cleaned = richtext.clean(f'<a href="{hostile}">click</a>')
            self.assertNotIn("href", cleaned, hostile)
            self.assertIn("click", cleaned, "the words are still kept")
        for fine in ("https://example.com/a", "http://example.com", "mailto:a@b.c",
                     "tel:+15551234"):
            self.assertIn(f'href="{fine}"', richtext.clean(f'<a href="{fine}">go</a>'))

    def test_output_is_always_balanced(self):
        # An editor mid-keystroke sends all sorts of things.
        for ragged in ("<p>unclosed <strong>bold", "</p></p><p>x</p>",
                       "<ul><li>a", "<p><em>a</p>"):
            cleaned = richtext.clean(ragged)
            self.assertEqual(cleaned.count("<p>"), cleaned.count("</p>"), ragged)
            self.assertEqual(cleaned.count("<li>"), cleaned.count("</li>"), ragged)

    def test_empty_wrappers_are_dropped(self):
        self.assertEqual(richtext.clean("<p></p><p>  </p><p>real</p>"), "<p>real</p>")

    def test_an_oversized_note_is_refused(self):
        with self.assertRaises(ValueError):
            richtext.clean("<p>x</p>" * (richtext.MAX_BYTES // 4))

    # -- switching between the two editors --

    ROUND_TRIP = (
        "## Kickoff\n"
        "See [the spec](https://example.com/s?a=1) and **chase** it.\n"
        "- power\n"
        "- packaging\n"
        "  - supplier quote\n"
        "    1. numbered\n"
        "- shipping"
    )

    def test_plain_to_rich_and_back_loses_nothing(self):
        rich = richtext.clean(richtext.from_text(self.ROUND_TRIP))
        self.assertEqual(richtext.to_text(rich), self.ROUND_TRIP)

    def test_a_link_keeps_its_address_when_switched_to_plain(self):
        rich = '<p><a href="https://example.com/s" target="_blank">spec</a></p>'
        self.assertEqual(richtext.to_text(rich), "[spec](https://example.com/s)")

    def test_a_heading_keeps_its_level(self):
        self.assertEqual(richtext.to_text("<h3>Deep</h3>"), "### Deep")

    def test_nested_lists_nest_properly(self):
        rich = richtext.from_text("- one\n  - deep\n- two")
        # The sublist belongs inside its item, not beside it.
        self.assertEqual(rich, "<ul><li>one<ul><li>deep</li></ul></li><li>two</li></ul>")

    def test_numbered_lists_are_renumbered_from_the_markup(self):
        self.assertEqual(richtext.to_text("<ol><li>a</li><li>b</li></ol>"), "1. a\n2. b")

    def test_a_table_keeps_its_words_when_switched_to_plain(self):
        text = richtext.to_text("<table><tr><td>left</td><td>right</td></tr></table>")
        self.assertIn("left", text)
        self.assertIn("right", text)

    def test_switching_an_empty_note_either_way(self):
        self.assertEqual(richtext.from_text(""), "")
        self.assertEqual(richtext.from_text("   \n  "), "")
        self.assertEqual(richtext.to_text(""), "")


class TestStylesheet(unittest.TestCase):
    """The stylesheet is the one part the browser tests cannot reach, and it is
    where a bug can hide in plain sight: a rule that quietly outranks one of the
    browser's own."""

    def setUp(self):
        static = Path(__file__).resolve().parent.parent / "manila" / "static"
        self.css = (static / "style.css").read_text(encoding="utf-8")
        self.js = "\n".join(f.read_text(encoding="utf-8") for f in static.glob("*.js"))

    def test_hiding_actually_hides(self):
        # Toggling .hidden is how every view in the app is shown and put away.
        # Several classes set display, which beats the browser's own [hidden]
        # rule -- so without this the header and tab bars stayed on screen over
        # the picker, live-looking and dead to the touch.
        self.assertIn("[hidden] { display: none !important; }", self.css)

    def test_the_app_relies_on_that_rule(self):
        # If nothing toggled .hidden any more, the rule above could go too.
        self.assertIn(".hidden =", self.js)

    def test_the_stylesheet_is_balanced(self):
        self.assertEqual(self.css.count("{"), self.css.count("}"))

    def test_the_stylesheet_is_plain_ascii(self):
        # A stray non-ASCII byte has broken this file before.
        for number, line in enumerate(self.css.splitlines(), 1):
            self.assertTrue(line.isascii(), f"line {number} is not ASCII: {line!r}")

    def test_no_browser_module_carries_a_stray_control_character(self):
        # A NUL once landed in a string literal and broke two modules at load.
        static = Path(__file__).resolve().parent.parent / "manila" / "static"
        for path in sorted(static.glob("*.js")):
            raw = path.read_bytes()
            bad = [i for i, b in enumerate(raw) if b < 9 or 13 < b < 32 or b == 127]
            self.assertEqual(bad, [], f"{path.name} has control bytes at {bad[:3]}")


class TestStaticTypes(unittest.TestCase):
    """The front end is served with types we state, not types the OS guesses.

    `mimetypes` reads the registry on Windows, where an installer may have
    claimed `.js` as text/plain -- and a module script sent as text/plain is
    refused by the browser, so the app would be a blank page on that machine
    with nothing in the log to say why.
    """

    def setUp(self):
        self.static = Path(__file__).resolve().parent.parent / "manila" / "static"

    def test_every_shipped_asset_has_a_stated_type(self):
        for path in sorted(self.static.rglob("*")):
            if path.is_file():
                self.assertIn(path.suffix.lower(), server.STATIC_TYPES, path.name)

    def test_modules_are_served_as_javascript(self):
        # The browser enforces this one: index.html loads app.js as a module.
        self.assertIn("javascript", server.STATIC_TYPES[".js"])
        self.assertNotIn("text/plain", server.STATIC_TYPES[".js"])

    def test_a_hostile_registry_cannot_change_what_is_sent(self):
        # Stand a real server up with mimetypes answering the way a badly
        # registered Windows box does, and check the wire, not the table.
        with mock.patch("mimetypes.guess_type", return_value=("text/plain", None)):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}/app.js"
                with urllib.request.urlopen(url, timeout=5) as response:
                    kind = response.headers["Content-Type"]
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()
        self.assertIn("javascript", kind)


class TestFileNames(unittest.TestCase):
    """Imported names come from outside, so they are treated as hostile."""

    def test_directory_parts_are_stripped(self):
        for hostile in ("../../etc/passwd", "/etc/passwd", "..\\..\\win.ini",
                        "C:\\Users\\me\\spec.pdf"):
            cleaned = files.safe_name(hostile)
            self.assertNotIn("/", cleaned)
            self.assertNotIn("\\", cleaned)
            self.assertNotIn("..", cleaned)

    def test_empty_and_dot_names_get_a_real_one(self):
        for odd in ("", "   ", ".", "..", "...", None):
            self.assertTrue(files.safe_name(odd))
            self.assertNotIn(files.safe_name(odd), (".", ".."))

    def test_a_normal_name_survives_intact(self):
        self.assertEqual(files.safe_name("Q3 Power Spec (final).pdf"),
                         "Q3 Power Spec (final).pdf")

    def test_long_names_are_trimmed_but_keep_the_extension(self):
        cleaned = files.safe_name("x" * 400 + ".pdf")
        self.assertTrue(cleaned.endswith(".pdf"))
        self.assertLess(len(cleaned), 120)

    def test_html_is_never_served_inline(self):
        # An HTML file served inline from this origin could script the app.
        self.assertTrue(files.disposition("text/html", "x.html").startswith("attachment"))

    def test_windows_device_names_become_real_filenames(self):
        # `NUL.txt` is the null device on Windows, not a document. The name has
        # to change or the import fails on one platform and not the other.
        for reserved in ("NUL.txt", "con", "AUX.pdf", "COM1.doc", "lpt9"):
            cleaned = files.safe_name(reserved)
            self.assertNotIn(cleaned.partition(".")[0].lower(), files.RESERVED)
            self.assertIn(reserved.partition(".")[0], cleaned,
                          "the name should stay recognisable")

    def test_a_name_merely_starting_with_a_device_name_is_left_alone(self):
        # Only the whole stem is reserved: `console.log` is an ordinary file.
        for fine in ("console.log", "connections.xlsx", "auxiliary.txt"):
            self.assertEqual(files.safe_name(fine), fine)

    def test_disposition_matches_what_opens_the_file(self):
        # Inline where the browser is the right viewer, attachment where a real
        # application is: a CSV belongs in a spreadsheet, not in a browser tab.
        for mime in ("application/pdf", "image/jpeg", "image/png", "text/plain"):
            self.assertTrue(files.disposition(mime, "x").startswith("inline"), mime)
        for mime in ("text/csv", "application/octet-stream",
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
            self.assertTrue(files.disposition(mime, "x").startswith("attachment"), mime)

    def test_images_stay_inline_so_a_picture_frame_can_show_them(self):
        # An image sent as an attachment would break the image frame entirely.
        for name in ("a.png", "a.jpg", "a.gif", "a.webp"):
            self.assertTrue(
                files.disposition(files.guess_mime(name), name).startswith("inline"), name)


class TestFileStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.project = project.Project("p", "P", Path(self.dir.name) / "p").prepare()

    def test_same_name_twice_does_not_overwrite(self):
        one, _, _ = files.store(self.project, 1, "spec.pdf", b"first")
        two, _, _ = files.store(self.project, 1, "spec.pdf", b"second")
        self.assertNotEqual(one, two)
        self.assertEqual(files.resolve(self.project, 1, one).read_bytes(), b"first")
        self.assertEqual(files.resolve(self.project, 1, two).read_bytes(), b"second")

    def test_files_land_inside_the_project(self):
        stored, mime, size = files.store(self.project, 3, "spec.pdf", b"pdf bytes")
        path = files.resolve(self.project, 3, stored)
        self.assertTrue(path.is_relative_to(self.project.path))
        self.assertEqual((mime, size), ("application/pdf", 9))

    def test_resolve_refuses_to_climb_out(self):
        with self.assertRaises(ValueError):
            files.resolve(self.project, 1, "../../../etc/passwd")

    def test_oversized_and_empty_are_refused(self):
        with self.assertRaises(ValueError):
            files.store(self.project, 1, "big", b"x" * (files.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            files.store(self.project, 1, "empty", b"")

    def test_no_part_file_is_left_behind(self):
        files.store(self.project, 1, "spec.pdf", b"data")
        leftovers = list(files.folder_dir(self.project, 1).glob("*.part"))
        self.assertEqual(leftovers, [])

    def test_discard_removes_the_copy(self):
        stored, _, _ = files.store(self.project, 1, "spec.pdf", b"data")
        path = files.resolve(self.project, 1, stored)
        files.discard(self.project, 1, stored)
        self.assertFalse(path.exists())
        files.discard(self.project, 1, stored)  # again: must not raise


class TestStopping(unittest.TestCase):
    """Stopping has to give the port back, not just stop answering on it.

    shutdown() alone leaves the listening socket open for as long as the
    process lives. On Windows Manila deliberately refuses to reuse an address,
    so the next start then finds the port held by a server that is no longer
    answering and refuses -- which reads as Manila failing to start for no
    reason at all.
    """

    def running(self):
        httpd = server.Server(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=5))
        return httpd, httpd.server_address[1]

    @staticmethod
    def free(port):
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def test_stopping_releases_the_port(self):
        httpd, port = self.running()
        self.assertFalse(self.free(port), "the port was not taken to begin with")
        server.stop(httpd)
        self.assertTrue(self.free(port), "the port is still held after stopping")

    def test_stopping_stops_answering(self):
        httpd, port = self.running()
        url = f"http://127.0.0.1:{port}/api/schema"
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
        server.stop(httpd)
        with self.assertRaises(OSError):
            urllib.request.urlopen(url, timeout=2)

    def test_a_browser_still_attached_does_not_hold_the_port(self):
        """The Manila window keeps an HTTP/1.1 connection open. Stopping has to
        work with one in hand, because there always is one."""
        httpd, port = self.running()
        keep = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(keep.close)
        keep.sendall(f"GET /api/schema HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
                     .encode())
        keep.settimeout(5)
        self.assertIn(b"200", keep.recv(200))
        server.stop(httpd)
        self.assertTrue(self.free(port))

    def test_the_tray_stops_through_that_and_not_through_shutdown(self):
        """The launcher used to pass httpd.shutdown straight in, which is the
        half that keeps the port."""
        launcher = (Path(__file__).resolve().parent.parent
                    / "windows" / "manila.pyw").read_text(encoding="utf-8")
        self.assertIn("server.stop(httpd)", launcher)
        self.assertNotIn("on_stop=httpd.shutdown", launcher)

    def test_the_shim_puts_manila_on_the_path_before_running_it(self):
        """`-m manila` works only from Manila's own folder, and the error when
        it is run from anywhere else -- No module named manila -- reads as a
        broken install rather than a wrong directory."""
        windows = Path(__file__).resolve().parent.parent / "windows"
        shim = (windows / "manila.cmd").read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH=%~dp0..", shim)
        # Probed, not merely found: a python.exe on PATH proves nothing
        # on Windows, where the shell ships a Store placeholder.
        self.assertIn("py -V >nul 2>&1", shim)
        self.assertIn("-m manila %*", shim)
        # install.ps1 copies the windows folder whole, so the shim ships with it.
        installer = (windows / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("'manila', 'windows', 'README.md'", installer)

    def test_installing_survives_a_folder_someone_is_standing_in(self):
        """Installing from a prompt opened in the Manila folder is the obvious
        thing to do, and Windows will not delete a directory any process is
        standing in. It failed with a bare "because it is in use" that named
        nothing and suggested nothing."""
        installer = (Path(__file__).resolve().parent.parent
                     / "windows" / "install.ps1").read_text(encoding="utf-8")
        # The contents go; the folder itself stays, because it may be a cwd.
        self.assertIn("function Clear-Folder", installer)
        self.assertIn("Get-ChildItem -LiteralPath $Path -Force", installer)
        self.assertNotIn("if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }",
                         installer)
        # A Manila running out of the target holds its own files open.
        self.assertIn("function Stop-InstalledManila", installer)
        self.assertIn("Stop-InstalledManila\n    Clear-Folder $Target", installer)

    def test_the_launcher_leaves_once_the_icon_is_gone(self):
        """The icon is the only control Manila has. A process that outlives it
        cannot be stopped by anything the app offers."""
        launcher = (Path(__file__).resolve().parent.parent
                    / "windows" / "manila.pyw").read_text(encoding="utf-8")
        self.assertIn("os._exit(0)", launcher)
        # pythonw gives no console, so these are None rather than streams.
        self.assertIn("if stream is not None", launcher)


class TestSuggestions(Base):
    """The queue exists so that nothing a model concludes reaches a table until
    it is read. These check the two things that makes it worth having: that a
    suggestion is inert until accepted, and that accepting one is an ordinary
    edit rather than a second, quieter write path."""

    def propose(self, *items, **meta):
        return suggest.record(self.conn, self.folder, meta or {"source": "Outlook"},
                              list(items))

    def update(self, row, field, value, **extra):
        return {"kind": "update", "entity": "action_item", "row": row,
                "field": field, "value": value, **extra}

    def only(self, scope="pending"):
        listed = suggest.listing(self.conn, self.folder, scope)
        return listed["batches"][0]["suggestions"][0]

    def test_a_suggestion_changes_nothing_until_it_is_accepted(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting on supplier quote")
        self.propose(self.update(row, "current_state", "Quote received"))

        held = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["current_state"], "Waiting on supplier quote")
        self.assertEqual(self.log(row, "current_state"), [],
                         "proposing wrote a history entry")

    def test_accepting_is_an_ordinary_edit(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting on supplier quote")
        self.propose(self.update(row, "current_state", "Quote received"))
        suggest.decide(self.conn, self.only()["id"], "accept")

        held = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["current_state"], "Quote received")
        # The log records what the cell became, not that something suggested it:
        # a year later the only interesting fact is what it said.
        entries = self.log(row, "current_state")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["old_value"], "Waiting on supplier quote")
        self.assertEqual(entries[0]["new_value"], "Quote received")

    def test_rejecting_leaves_no_trace_in_the_folder(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting on supplier quote")
        self.propose(self.update(row, "current_state", "Quote received"))
        suggest.decide(self.conn, self.only()["id"], "reject")

        held = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["current_state"], "Waiting on supplier quote")
        self.assertEqual(self.log(row, "current_state"), [])
        self.assertEqual(suggest.pending_count(self.conn, self.folder), 0)

    def test_nothing_can_be_decided_twice(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.propose(self.update(row, "current_state", "Moved"))
        one = self.only()["id"]
        suggest.decide(self.conn, one, "accept")
        with self.assertRaises(ValueError):
            suggest.decide(self.conn, one, "accept")
        with self.assertRaises(ValueError):
            suggest.decide(self.conn, one, "reject")

    def test_an_edit_comes_with_the_whole_entry_around_it(self):
        """One cell is changing, but it is judged with the rest of the row in
        view -- the same as a proposed new entry, which is shown whole."""
        row = self.new_action()
        self.set(row, "item", "Supplier quote, revised")
        self.set(row, "waiting_on", "Alice")
        self.propose(self.update(row, "current_state", "Quote received"))
        held = self.only()
        self.assertEqual(held["row"]["item"], "Supplier quote, revised")
        self.assertEqual(held["row"]["waiting_on"], "Alice")
        self.assertEqual(set(held["row"]),
                         {c["key"] for c in schema.columns("action_item")})
        # The row is as it stands; the proposal is still only the one cell.
        self.assertEqual(held["row"]["current_state"], "")
        self.assertEqual((held["field"], held["value"]),
                         ("current_state", "Quote received"))

    def test_archive_and_tick_come_with_the_entry_too(self):
        row = self.new_action()
        self.set(row, "item", "Supplier quote, revised")
        self.propose({"kind": "archive", "entity": "action_item", "row": row})
        self.assertEqual(self.only()["row"]["item"], "Supplier quote, revised")

    def test_a_cell_you_changed_since_is_marked_stale(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting on supplier quote")
        self.propose(self.update(row, "current_state", "Quote received"))
        # You got there first, by hand.
        self.set(row, "current_state", "Quote received, checking the numbers")

        shown = self.only()
        self.assertTrue(shown["stale"])
        self.assertEqual(shown["seen"], "Waiting on supplier quote")
        self.assertEqual(shown["now"], "Quote received, checking the numbers")

    def test_a_stale_one_can_still_be_accepted_deliberately(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.propose(self.update(row, "current_state", "Quote received"))
        self.set(row, "current_state", "Something I typed")
        suggest.decide(self.conn, self.only()["id"], "accept")
        held = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["current_state"], "Quote received")

    def test_accept_all_holds_back_the_stale_ones(self):
        """The reason a whole run can be accepted at once: the ones worth a
        second look are exactly the ones it declines to take."""
        one, two = self.new_action(), self.new_action()
        self.set(one, "current_state", "A")
        self.set(two, "current_state", "B")
        batch = self.propose(self.update(one, "current_state", "A moved"),
                             self.update(two, "current_state", "B moved"))
        self.set(two, "current_state", "B, as I typed it")

        result = suggest.decide_batch(self.conn, batch["batch_id"], "accept")
        self.assertEqual(result["decided"], 1)
        self.assertEqual([h["why"] for h in result["held"]], ["stale"])
        rows = {r["id"]: r["current_state"] for r in self.conn.execute(
            "SELECT id, current_state FROM action_item")}
        self.assertEqual(rows[one], "A moved")
        self.assertEqual(rows[two], "B, as I typed it")

    def pending_ids(self):
        listed = suggest.listing(self.conn, self.folder, "pending")
        return [s["id"] for b in listed["batches"] for s in b["suggestions"]]

    def test_one_entrys_changes_are_decided_together(self):
        """What the Review tab shows as one card is decided as one thing:
        everything a run proposed for a single entry."""
        row = self.new_action()
        self.set(row, "item", "Supplier quote")
        self.set(row, "waiting_on", "Alice")
        self.propose(self.update(row, "item", "Supplier quote, revised"),
                     self.update(row, "waiting_on", "Sam Rivera"))

        result = suggest.decide_many(self.conn, self.pending_ids(), "accept")
        self.assertEqual(result["decided"], 2)
        self.assertEqual(result["held"], [])
        held = self.conn.execute(
            "SELECT item, waiting_on FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual((held["item"], held["waiting_on"]),
                         ("Supplier quote, revised", "Sam Rivera"))

    def test_deciding_a_card_takes_the_stale_one_too(self):
        """Unlike Accept all, which holds those back: this one was on the card,
        flagged, when the choice was made."""
        row = self.new_action()
        self.set(row, "item", "Supplier quote")
        self.set(row, "waiting_on", "Alice")
        self.propose(self.update(row, "item", "Supplier quote, revised"),
                     self.update(row, "waiting_on", "Sam Rivera"))
        self.set(row, "item", "Supplier quote (as I typed it)")

        suggest.decide_many(self.conn, self.pending_ids(), "accept")
        held = self.conn.execute(
            "SELECT item FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["item"], "Supplier quote, revised")

    def test_two_proposals_for_one_cell_keep_the_later(self):
        """Both cannot be right, and applying both would write the earlier one
        into the cell's history for no reason."""
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.propose(self.update(row, "current_state", "Quote received"),
                     self.update(row, "current_state", "Quote received; "
                                                       "waiting on approval"))

        suggest.decide_many(self.conn, self.pending_ids(), "accept")
        held = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["current_state"], "Quote received; waiting on approval")
        states = [r["state"] for r in self.conn.execute(
            "SELECT state FROM suggestion ORDER BY id")]
        self.assertEqual(states, ["rejected", "accepted"])
        # One edit to the cell, not two: the superseded one never lands.
        self.assertEqual(len(self.log(row, "current_state")), 1)

    def test_a_card_is_rejected_whole(self):
        row = self.new_action()
        self.set(row, "item", "Supplier quote")
        self.propose(self.update(row, "item", "Supplier quote, revised"),
                     self.update(row, "waiting_on", "Sam Rivera"))

        suggest.decide_many(self.conn, self.pending_ids(), "reject")
        self.assertEqual(self.pending_ids(), [])
        held = self.conn.execute(
            "SELECT item, waiting_on FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual((held["item"], held["waiting_on"]), ("Supplier quote", ""))

    def test_an_entry_is_archived_after_its_cells_are_written(self):
        """Order matters within a card: an entry is put away only once it says
        why it was put away."""
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.propose({"kind": "archive", "entity": "action_item", "row": row,
                      "evidence": "a mail"},
                     self.update(row, "current_state", "Done, closed out"))

        suggest.decide_many(self.conn, self.pending_ids(), "accept")
        held = self.conn.execute(
            "SELECT current_state, archived_at FROM action_item WHERE id = ?",
            (row,)).fetchone()
        self.assertEqual(held["current_state"], "Done, closed out")
        self.assertIsNotNone(held["archived_at"])

    def test_deciding_a_card_refuses_what_was_already_decided(self):
        row = self.new_action()
        self.propose(self.update(row, "item", "One"))
        ids = self.pending_ids()
        suggest.decide(self.conn, ids[0], "reject")
        with self.assertRaises(ValueError):
            suggest.decide_many(self.conn, ids, "accept")

    def test_a_card_can_be_accepted_with_an_edit_to_one_change(self):
        row = self.new_action()
        self.set(row, "item", "Supplier quote")
        self.propose(self.update(row, "item", "Supplier quote, revised"),
                     self.update(row, "waiting_on", "Sam Rivera"))
        ids = self.pending_ids()

        suggest.decide_many(self.conn, ids, "accept",
                            {ids[0]: "Supplier quote, revised again"})
        held = self.conn.execute(
            "SELECT item, waiting_on FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual((held["item"], held["waiting_on"]),
                         ("Supplier quote, revised again", "Sam Rivera"))

    def test_a_batch_is_refused_whole_when_any_part_is_wrong(self):
        row = self.new_action()
        self.set(row, "item", "Real")
        with self.assertRaises(suggest.Refused) as caught:
            self.propose(self.update(row, "item", "Fine"),
                         self.update(99999, "item", "No such row"),
                         self.update(row, "not_a_column", "No such column"))
        self.assertEqual([p["index"] for p in caught.exception.problems], [1, 2])
        self.assertEqual(suggest.pending_count(self.conn, self.folder), 0,
                         "a refused batch recorded part of itself")

    def test_the_same_mail_is_not_queued_twice(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        first = self.propose(self.update(row, "current_state", "Moved", ref="AAMk-1"))
        self.assertEqual(first["recorded"], 1)
        again = self.propose(self.update(row, "current_state", "Moved", ref="AAMk-1"))
        self.assertEqual((again["recorded"], again["duplicates"]), (0, 1))
        self.assertIsNone(again["batch_id"], "an empty run was left to review")

    def test_a_proposed_contact_is_not_queued_twice_either(self):
        """A create has no row to point at, so its target is NULL -- which does
        not compare equal to itself with `=`, and would have let every sync
        queue the same new contact again."""
        bob = {"kind": "create", "entity": "contact",
               "fields": {"name": "Sam Rivera"}, "evidence": "e", "ref": "AAMk-2"}
        self.assertEqual(self.propose(dict(bob))["recorded"], 1)
        again = self.propose(dict(bob))
        self.assertEqual((again["recorded"], again["duplicates"]), (0, 1))
        # And twice within one run counts once.
        batch = self.propose(dict(bob, ref="AAMk-9"), dict(bob, ref="AAMk-9"))
        self.assertEqual((batch["recorded"], batch["duplicates"]), (1, 1))

    def test_without_a_message_id_nothing_is_treated_as_a_duplicate(self):
        """Dedupe is a claim about a mail, not about a value. With no ref there
        is no such claim, and suppressing it would hide real repeat findings."""
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.assertEqual(self.propose(
            self.update(row, "current_state", "Moved"))["recorded"], 1)
        self.assertEqual(self.propose(
            self.update(row, "current_state", "Moved"))["recorded"], 1)

    def test_something_rejected_is_not_offered_again(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.propose(self.update(row, "current_state", "Moved", ref="AAMk-1"))
        suggest.decide(self.conn, self.only()["id"], "reject")
        again = self.propose(self.update(row, "current_state", "Moved", ref="AAMk-1"))
        self.assertEqual(again["duplicates"], 1)

    def test_a_suggestion_that_changes_nothing_is_refused(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting on supplier quote")
        with self.assertRaises(suggest.Refused) as caught:
            self.propose(self.update(row, "current_state", "Waiting on supplier quote"))
        self.assertIn("already says that", caught.exception.problems[0]["error"])

    def test_a_row_from_another_folder_is_refused(self):
        other = self.conn.execute(
            "INSERT INTO folder (name, position, created_at) VALUES ('G', 2.0, 'now')"
        ).lastrowid
        row = self.new_action()
        self.set(row, "item", "Mine")
        with self.assertRaises(suggest.Refused):
            suggest.record(self.conn, other, {"source": "Outlook"},
                           [self.update(row, "item", "Reached across")])

    def test_a_create_is_born_with_its_values_and_logs_nothing(self):
        self.propose({"kind": "create", "entity": "contact",
                      "fields": {"name": "Sam Rivera", "email": "bob@example.com"},
                      "evidence": "'Phase One' - Sam Rivera"})
        suggest.decide(self.conn, self.only()["id"], "accept")
        made = self.conn.execute("SELECT * FROM contact").fetchone()
        self.assertEqual((made["name"], made["email"]), ("Sam Rivera", "bob@example.com"))
        self.assertEqual(made["folder_id"], self.folder)
        self.assertEqual(
            history.cell_history(self.conn, "contact", made["id"], "name"), [],
            "a row born with a value logged it as a revision")

    def test_ticking_and_archiving(self):
        one, two = self.new_action(), self.new_action()
        self.set(one, "item", "Finished")
        self.set(two, "item", "Parked")
        self.propose({"kind": "tick", "entity": "action_item", "row": one,
                      "evidence": "thread closed"},
                     {"kind": "archive", "entity": "action_item", "row": two,
                      "evidence": "thread closed"})
        for shown in suggest.listing(self.conn, self.folder)["batches"][0]["suggestions"]:
            suggest.decide(self.conn, shown["id"], "accept")
        rows = {r["id"]: r for r in self.conn.execute("SELECT * FROM action_item")}
        self.assertEqual(rows[one]["done"], 1)
        self.assertIsNotNone(rows[two]["archived_at"])

    def test_what_is_already_done_is_refused_rather_than_queued(self):
        row = self.new_action()
        self.set(row, "item", "Done already")
        self.conn.execute("UPDATE action_item SET done = 1 WHERE id = ?", (row,))
        with self.assertRaises(suggest.Refused):
            self.propose({"kind": "tick", "entity": "action_item", "row": row,
                          "evidence": "e"})

    def test_an_entry_deleted_since_is_shown_as_gone_not_applied(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.propose(self.update(row, "current_state", "Moved"))
        self.conn.execute("DELETE FROM action_item WHERE id = ?", (row,))

        shown = self.only()
        self.assertTrue(shown["gone"])
        with self.assertRaises(ValueError):
            suggest.decide(self.conn, shown["id"], "accept")
        # Rejecting it is still how you clear it away.
        suggest.decide(self.conn, shown["id"], "reject")
        self.assertEqual(suggest.pending_count(self.conn, self.folder), 0)

    def test_editing_before_accepting_keeps_what_was_suggested(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        self.propose(self.update(row, "current_state", "Quote recieved"))
        one = self.only()["id"]
        suggest.decide(self.conn, one, "accept", value="Quote received")

        held = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["current_state"], "Quote received")
        decided = suggest.listing(
            self.conn, self.folder, "decided")["batches"][0]["suggestions"][0]
        self.assertEqual(decided["value"], "Quote received")
        self.assertEqual(decided["proposed"], "Quote recieved",
                         "the original suggestion was lost when it was corrected")

    def test_a_borrowed_deadline_can_be_proposed_and_resolves_on_accept(self):
        row = self.new_action()
        self.set(row, "item", "Waiting on the paperwork")
        milestone = self.conn.execute(
            "INSERT INTO milestone (folder_id, position, deliverable, due_date,"
            " created_at) VALUES (?, 1.0, 'Phase One Signoff', '2026-10-15', 'now')",
            (self.folder,)).lastrowid
        self.propose({"kind": "update", "entity": "action_item", "row": row,
                      "field": "resolve_by", "evidence": "'Phase One schedule'",
                      "value": {"date": "", "note": "with Phase One", "link": milestone}})

        resolve = lambda entity, field, value: server.resolve_incoming(
            self.conn, entity, field, value)
        suggest.decide(self.conn, self.only()["id"], "accept", resolve=resolve)
        held = self.conn.execute(
            "SELECT * FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["resolve_by_milestone_id"], milestone)
        # Stored as the effective date, so losing the milestone later keeps one.
        self.assertEqual(held["resolve_by_at"], "2026-10-15")

    def test_a_deliverable_from_another_folder_cannot_be_borrowed(self):
        row = self.new_action()
        self.set(row, "item", "Mine")
        other = self.conn.execute(
            "INSERT INTO folder (name, position, created_at) VALUES ('G', 2.0, 'now')"
        ).lastrowid
        elsewhere = self.conn.execute(
            "INSERT INTO milestone (folder_id, position, deliverable, created_at)"
            " VALUES (?, 1.0, 'Theirs', 'now')", (other,)).lastrowid
        with self.assertRaises(suggest.Refused):
            self.propose({"kind": "update", "entity": "action_item", "row": row,
                          "field": "resolve_by", "evidence": "e",
                          "value": {"date": "", "note": "", "link": elsewhere}})

    def test_a_reviewed_run_can_be_cleared_but_not_an_unfinished_one(self):
        row = self.new_action()
        self.set(row, "current_state", "Waiting")
        batch = self.propose(self.update(row, "current_state", "Moved"))
        with self.assertRaises(ValueError):
            suggest.discard_batch(self.conn, batch["batch_id"])
        suggest.decide(self.conn, self.only()["id"], "accept")
        suggest.discard_batch(self.conn, batch["batch_id"])
        self.assertEqual(suggest.listing(self.conn, self.folder, "all")["batches"], [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM suggestion").fetchone()["n"], 0)
        # Clearing the record of a suggestion never unpicks what it did.
        held = self.conn.execute(
            "SELECT current_state FROM action_item WHERE id = ?", (row,)).fetchone()
        self.assertEqual(held["current_state"], "Moved")

    def test_a_run_says_when_each_source_was_last_read(self):
        row = self.new_action()
        self.set(row, "item", "A")
        self.propose(self.update(row, "item", "B"), source="Outlook",
                     covered_through={"Outlook": "2026-09-16T00:00:00Z"})
        self.propose(self.update(row, "waiting_on", "Alice"), source="Teams",
                     covered_through={"Teams": "2026-09-16T00:00:00Z"})
        sources = {r["source"] for r in suggest.last_runs(self.conn, self.folder)}
        self.assertEqual(sources, {"Outlook", "Teams"})

    def test_a_batch_has_a_size_a_person_can_actually_read(self):
        row = self.new_action()
        self.set(row, "item", "A")
        with self.assertRaises(ValueError):
            self.propose(*[self.update(row, "item", f"v{n}")
                           for n in range(suggest.MAX_BATCH + 1)])


class TestWhatABatchCovers(Base):
    """A run's watermark is a different fact from when the run happened, and the
    difference is the whole reason it is asked for: a run that reads a stale
    index at nine has not read to nine."""

    @staticmethod
    def keyed(meta):
        """Tests say a single point for the Teams source; the wire says a map."""
        through = meta.get("covered_through")
        if isinstance(through, str) and through:
            meta = {**meta, "covered_through": {"Teams": through}}
        return meta

    def propose(self, **meta):
        row = self.new_action()
        self.set(row, "item", "A")
        return suggest.record(self.conn, self.folder,
                              {"source": "Teams", **self.keyed(meta)},
                              [{"kind": "update", "entity": "action_item",
                                "row": row, "field": "item", "value": "B",
                                "evidence": "a chat"}])

    def runs(self):
        return {r["source"]: r for r in suggest.last_runs(self.conn, self.folder)}

    def test_a_run_says_how_far_it_read_not_just_when_it_ran(self):
        self.propose(covered_through="2026-09-10T06:00:00+00:00")
        run = self.runs()["Teams"]
        self.assertEqual(run["covered_through"], "2026-09-10T06:00:00+00:00")
        # The two are not the same instant, and nothing pretends otherwise.
        self.assertNotEqual(run["covered_through"], run["last_run"])

    def test_a_run_that_does_not_say_leaves_no_watermark(self):
        """Empty, not the run time. A source with no floor has to be given a
        window deliberately, and it can only do that if it can tell."""
        self.propose()
        self.assertEqual(self.runs(), {})

    def test_a_batch_label_is_never_a_source(self):
        """A run labelled "both" advances Outlook and Teams, and nothing called
        "both" -- otherwise the next run has two rows to choose between."""
        self.propose(source="both")          # a run that vouched for nothing
        self.quiet(source="both", covered_through={
            "Outlook": "2026-09-16T23:50:00Z", "Teams": "2026-09-16T23:48:20Z"})
        self.assertEqual(set(self.runs()), {"Outlook", "Teams"})
        self.assertEqual(self.runs()["Outlook"]["runs"], 1)

    def test_a_source_never_read_has_no_row_at_all(self):
        self.propose(source="Outlook")
        self.assertNotIn("Teams", self.runs())

    def test_the_furthest_point_read_is_the_one_kept(self):
        self.propose(covered_through="2026-09-10T00:00:00+00:00")
        self.propose(covered_through="2026-09-14T00:00:00+00:00")
        self.propose(covered_through="2026-09-12T00:00:00+00:00")
        self.assertEqual(self.runs()["Teams"]["covered_through"], "2026-09-14T00:00:00+00:00")

    def quiet(self, items=(), **meta):
        return suggest.record(self.conn, self.folder,
                              {"source": "Teams", **self.keyed(meta)}, list(items))

    def test_a_run_that_found_nothing_still_moves_the_watermark(self):
        self.propose(covered_through="2026-09-10T00:00:00+00:00")
        made = self.quiet(covered_through="2026-09-14T00:00:00+00:00")
        self.assertIsNotNone(made["batch_id"])
        self.assertEqual(made["recorded"], 0)
        self.assertEqual(self.runs()["Teams"]["covered_through"], "2026-09-14T00:00:00+00:00")
        self.assertEqual(self.runs()["Teams"]["runs"], 2)

    def test_a_quiet_run_leaves_nothing_to_review(self):
        self.quiet(covered_through="2026-09-14T00:00:00+00:00")
        listed = suggest.listing(self.conn, self.folder, "all")
        self.assertEqual(listed["batches"], [])
        self.assertEqual(listed["pending"], 0)

    def test_a_quiet_run_that_says_nothing_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.quiet()
        self.assertIn("covered_through", str(caught.exception))
        self.assertEqual(suggest.last_runs(self.conn, self.folder), [])

    def test_a_rerun_of_only_duplicates_still_moves_the_watermark(self):
        row = self.new_action()
        again = {"kind": "update", "entity": "action_item", "row": row,
                 "field": "item", "value": "B", "evidence": "e", "ref": "AAMk-1"}
        self.quiet([again], covered_through="2026-09-10T00:00:00+00:00")
        made = self.quiet([again], covered_through="2026-09-14T00:00:00+00:00")
        self.assertEqual((made["recorded"], made["duplicates"]), (0, 1))
        self.assertEqual(self.runs()["Teams"]["covered_through"], "2026-09-14T00:00:00+00:00")

    def test_a_rerun_of_only_duplicates_with_no_watermark_leaves_no_batch(self):
        row = self.new_action()
        again = {"kind": "update", "entity": "action_item", "row": row,
                 "field": "item", "value": "B", "evidence": "e", "ref": "AAMk-1"}
        self.quiet([again])
        made = self.quiet([again])
        self.assertIsNone(made["batch_id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM suggestion_batch").fetchone()["n"], 1)

    def test_clearing_a_reviewed_run_keeps_how_far_it_read(self):
        made = self.propose(covered_through="2026-09-14T00:00:00+00:00")
        only = suggest.listing(self.conn, self.folder)["batches"][0]["suggestions"][0]
        suggest.decide(self.conn, only["id"], "reject")
        suggest.discard_batch(self.conn, made["batch_id"])
        self.assertEqual(suggest.listing(self.conn, self.folder, "all")["batches"], [])
        self.assertEqual(self.runs()["Teams"]["covered_through"], "2026-09-14T00:00:00+00:00")

    def test_each_source_keeps_its_own_watermark(self):
        """Outlook read through tonight; Teams stopped early on a rate limit.
        One value for the run would overstate Teams."""
        self.quiet(source="both", covered_through={
            "Outlook": "2026-09-16T23:50:00Z",
            "Teams": "2026-09-16T18:05:00Z"})
        runs = self.runs()
        self.assertEqual(runs["Outlook"]["covered_through"], "2026-09-16T23:50:00+00:00")
        self.assertEqual(runs["Teams"]["covered_through"], "2026-09-16T18:05:00+00:00")
        # The run's own label is not a source anyone reads from.
        self.assertNotIn("both", runs)

    def test_a_run_that_names_one_source_moves_only_that_one(self):
        self.quiet(covered_through={"Outlook": "2026-09-10T00:00:00Z",
                                    "Teams": "2026-09-10T00:00:00Z"})
        made = self.quiet(covered_through={"Teams": "2026-09-16T00:00:00Z"})
        self.assertEqual(self.runs()["Outlook"]["covered_through"],
                         "2026-09-10T00:00:00+00:00")
        self.assertEqual(self.runs()["Outlook"]["runs"], 1)
        self.assertEqual([r["source"] for r in made["last_runs"]], ["Teams"])

    def test_an_earlier_point_does_not_move_a_source_back(self):
        self.quiet(covered_through={"Teams": "2026-09-16T12:00:00Z"})
        made = self.quiet(covered_through={"Teams": "2026-09-15T12:00:00Z"})
        # And the proposer is told what the watermark actually is now.
        self.assertEqual(made["last_runs"][0]["covered_through"],
                         "2026-09-16T12:00:00+00:00")

    def test_furthest_is_by_instant_not_by_how_it_was_written(self):
        """10:00 in New York is later than 12:00 in London; as text it is not."""
        self.quiet(covered_through={"Teams": "2026-09-16T12:00:00+01:00"})
        self.quiet(covered_through={"Teams": "2026-09-16T10:00:00-04:00"})
        self.assertEqual(self.runs()["Teams"]["covered_through"],
                         "2026-09-16T14:00:00+00:00")

    def test_rebaseline_restarts_a_source_from_an_earlier_point(self):
        self.quiet(covered_through={"Teams": "2026-09-16T12:00:00Z",
                                    "Outlook": "2026-09-16T12:00:00Z"})
        self.quiet(covered_through={"Teams": "2026-09-01T00:00:00Z"}, rebaseline=True)
        self.assertEqual(self.runs()["Teams"]["covered_through"],
                         "2026-09-01T00:00:00+00:00")
        self.assertEqual(self.runs()["Outlook"]["covered_through"],
                         "2026-09-16T12:00:00+00:00")
        # After which it is monotonic again, from the new floor.
        self.quiet(covered_through={"Teams": "2026-09-05T00:00:00Z"})
        self.assertEqual(self.runs()["Teams"]["covered_through"],
                         "2026-09-05T00:00:00+00:00")

    def test_rebaseline_without_a_point_is_refused(self):
        with self.assertRaises(ValueError):
            self.propose(rebaseline=True)

    def test_a_date_or_a_time_with_no_zone_is_refused(self):
        for bad in ("2026-09-16", "2026-09-16T23:50:00"):
            with self.assertRaises(ValueError, msg=bad) as caught:
                self.quiet(covered_through={"Teams": bad})
            self.assertIn("timezone", str(caught.exception))
        self.assertEqual(suggest.last_runs(self.conn, self.folder), [])

    def test_a_bare_timestamp_is_refused_since_it_names_no_source(self):
        with self.assertRaises(ValueError) as caught:
            suggest.record(self.conn, self.folder,
                           {"source": "both", "covered_through": "2026-09-16T23:50:00Z"}, [])
        self.assertIn("Outlook", str(caught.exception))
        self.assertEqual(self.runs(), {})

    def test_an_unnamed_or_empty_source_entry_is_refused(self):
        for bad in ({"": "2026-09-16T23:50:00Z"}, {"Teams": ""}, ["Teams"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.quiet(covered_through=bad)

    def test_the_queue_shows_what_each_run_claimed(self):
        self.propose(source="both", covered_through={
            "Outlook": "2026-09-16T23:50:00Z", "Teams": "2026-09-16T18:05:00Z"})
        batch = suggest.listing(self.conn, self.folder)["batches"][0]
        self.assertEqual(batch["covered_through"],
                         {"Outlook": "2026-09-16T23:50:00+00:00",
                          "Teams": "2026-09-16T18:05:00+00:00"})

    def test_a_watermark_that_is_not_a_date_is_refused_whole(self):
        """Refused rather than dropped: a batch recorded without the field the
        next run steers by is worse than a batch refused, because nothing
        afterwards can tell that the watermark went missing."""
        with self.assertRaises(ValueError) as caught:
            self.propose(covered_through="last Tuesday")
        self.assertIn("covered_through", str(caught.exception))
        self.assertEqual(suggest.last_runs(self.conn, self.folder), [])


class TestWhatAnEntryIsMadeOf(Base):
    """An accepted suggestion's message is written against the entry it changed,
    so a folder read can say what has already been read into each entry without
    asking the queue a second time."""

    def propose(self, row, value, ref, **extra):
        return suggest.record(
            self.conn, self.folder, {"source": "Outlook"},
            [{"kind": "update", "entity": "action_item", "row": row,
              "field": "current_state", "value": value, "ref": ref,
              "evidence": "a mail", **extra}])

    def refs(self, row):
        return suggest.refs_for(self.conn, "action_item", [row]).get(row, [])

    def accept(self, batch=None):
        held = suggest.listing(self.conn, self.folder, "pending")["batches"][0]
        return suggest.decide(self.conn, held["suggestions"][0]["id"], "accept")

    def test_accepting_writes_the_message_against_the_entry(self):
        row = self.new_action()
        self.propose(row, "Quote in", "AAMk-1")
        self.assertEqual(self.refs(row), [])      # nothing yet: it is a proposal
        self.accept()
        self.assertEqual(self.refs(row), ["AAMk-1"])

    def test_rejecting_writes_nothing(self):
        """Read and turned down is not part of what the entry is made of."""
        row = self.new_action()
        self.propose(row, "Quote in", "AAMk-1")
        held = suggest.listing(self.conn, self.folder, "pending")["batches"][0]
        suggest.decide(self.conn, held["suggestions"][0]["id"], "reject")
        self.assertEqual(self.refs(row), [])

    def test_a_created_entry_carries_the_mail_that_created_it(self):
        suggest.record(self.conn, self.folder, {"source": "Outlook"},
                       [{"kind": "create", "entity": "action_item",
                         "fields": {"item": "Chase the quote"},
                         "ref": "AAMk-9", "evidence": "a mail"}])
        made = self.accept()
        self.assertEqual(self.refs(made["entity_id"]), ["AAMk-9"])

    def test_one_mail_that_built_two_cells_is_listed_once(self):
        row = self.new_action()
        suggest.record(self.conn, self.folder, {"source": "Outlook"},
                       [{"kind": "update", "entity": "action_item", "row": row,
                         "field": "current_state", "value": "Quote in",
                         "ref": "AAMk-1", "evidence": "a mail"},
                        {"kind": "update", "entity": "action_item", "row": row,
                         "field": "waiting_on", "value": "Alice",
                         "ref": "AAMk-1", "evidence": "a mail"}])
        suggest.decide_batch(self.conn, 1, "accept")
        self.assertEqual(self.refs(row), ["AAMk-1"])

    def test_refs_survive_the_run_being_cleared(self):
        """The queue's own dedupe goes when a reviewed run is cleared. What the
        entry is made of must not, or last week's mail looks unread."""
        row = self.new_action()
        self.propose(row, "Quote in", "AAMk-1")
        self.accept()
        suggest.discard_batch(self.conn, 1)
        self.assertEqual(self.refs(row), ["AAMk-1"])

    def test_a_suggestion_with_no_message_id_records_nothing(self):
        row = self.new_action()
        suggest.record(self.conn, self.folder, {"source": "Outlook"},
                       [{"kind": "update", "entity": "action_item", "row": row,
                         "field": "current_state", "value": "Quote in",
                         "evidence": "a mail"}])
        self.accept()
        self.assertEqual(self.refs(row), [])

    def test_what_a_deleted_entry_was_made_of_goes_with_it(self):
        """Row ids are reused. A new entry must not be born believing it has
        already read last quarter's mail."""
        row = self.new_action()
        self.propose(row, "Quote in", "AAMk-1")
        self.accept()
        suggest.forget_refs(self.conn, "action_item", [row])
        self.assertEqual(self.refs(row), [])


class TestSources(Base):
    """Pointers, not keywords. Ids change about twice a year and say so when
    they do; a keyword list goes stale quietly and keeps working on last
    quarter's vocabulary."""

    def add(self, **body):
        return sources.add(self.conn, self.folder, body)

    LINK = ("https://teams.microsoft.com/l/channel/"
            "19%3aexample1%40thread.tacv2/Acme%20Rollout"
            "?groupId=00000000-0000-0000-0000-00000000c0de&tenantId=abc")

    def test_a_channel_is_added_by_pasting_the_link_teams_gives_you(self):
        """Nobody has a channel id. What they have is the link, which is where
        that id is written down in a form you can copy."""
        made = self.add(kind="teams_channel", ref=self.LINK)
        self.assertEqual(made["ref"], "19:example1@thread.tacv2")
        self.assertEqual(made["within"], "00000000-0000-0000-0000-00000000c0de")

    def test_the_link_is_decoded_rather_than_stored_as_typed(self):
        """%3a in the box matches nothing, and says nothing about why."""
        made = self.add(kind="teams_channel", ref=self.LINK)
        self.assertNotIn("%3a", made["ref"])
        self.assertNotIn("%40", made["ref"])

    def test_the_channel_names_itself_from_the_link(self):
        self.assertEqual(self.add(kind="teams_channel", ref=self.LINK)["name"],
                         "Acme Rollout")

    def test_but_what_you_called_it_wins(self):
        made = self.add(kind="teams_channel", ref=self.LINK, name="Acme shipping")
        self.assertEqual(made["name"], "Acme shipping")

    def test_a_link_with_no_team_in_it_says_which_link_to_copy(self):
        with self.assertRaises(ValueError) as caught:
            self.add(kind="teams_channel",
                     ref="https://teams.microsoft.com/l/channel/19%3aabc%40thread.tacv2/FW")
        self.assertIn("Get link to channel", str(caught.exception))

    def test_something_that_is_not_a_channel_link_is_named_as_such(self):
        with self.assertRaises(ValueError) as caught:
            self.add(kind="teams_channel", ref="https://example.com/nope")
        self.assertIn("channel link", str(caught.exception))

    def test_the_two_ids_are_still_accepted_directly(self):
        """Graph Explorer hands them over already separated."""
        made = self.add(kind="teams_channel", ref="19:abc@thread.tacv2",
                        within="team-1234")
        self.assertEqual((made["ref"], made["within"]),
                         ("19:abc@thread.tacv2", "team-1234"))

    def test_a_channel_needs_the_team_that_holds_it(self):
        with self.assertRaises(ValueError) as caught:
            self.add(kind="teams_channel", ref="19:abc@thread.tacv2")
        self.assertIn("Paste the channel link", str(caught.exception))

    def test_a_chat_can_be_added_by_its_web_address(self):
        """Chats have no copy-link, so the address bar is the only place the id
        shows -- and it arrives percent-encoded like everything else."""
        made = self.add(
            kind="teams_chat",
            ref="https://teams.microsoft.com/_#/conversations/"
                "19%3aexample2%40unq.gbl.spaces?ctx=chat")
        self.assertEqual(made["ref"], "19:example2@unq.gbl.spaces")

    def test_an_address_with_no_chat_id_in_it_says_what_to_look_for(self):
        with self.assertRaises(ValueError) as caught:
            self.add(kind="teams_chat", ref="https://teams.microsoft.com/_#/calendar")
        self.assertIn("19:", str(caught.exception))

    def test_a_chat_needs_no_container(self):
        made = self.add(kind="teams_chat", ref="19:def", name="Alice / Bob")
        self.assertEqual(made["within"], "")

    def test_an_address_is_folded_and_a_domain_loses_its_at(self):
        """Nothing that reads mail treats case as meaningful in either, and a
        domain typed as @acme.example.com is the same domain."""
        self.assertEqual(
            self.add(kind="email_address", ref="Alice@Acme.Example.com")["ref"],
            "alice@acme.example.com")
        self.assertEqual(self.add(kind="email_domain", ref="@ACME.EXAMPLE.COM")["ref"],
                         "acme.example.com")

    def test_an_id_is_left_exactly_as_it_was_pasted(self):
        ref = "19:AbC-XyZ@thread.tacv2"
        self.assertEqual(self.add(kind="teams_chat", ref=ref)["ref"], ref)

    def test_the_same_pointer_twice_is_a_mistake(self):
        self.add(kind="email_domain", ref="acme.example.com")
        with self.assertRaises(ValueError):
            self.add(kind="email_domain", ref="acme.example.com")

    def test_two_folders_may_listen_to_the_same_thing(self):
        other = self.conn.execute(
            "INSERT INTO folder (name, position, created_at)"
            " VALUES ('G', 2.0, 'now')").lastrowid
        self.add(kind="email_domain", ref="acme.example.com")
        sources.add(self.conn, other, {"kind": "email_domain", "ref": "acme.example.com"})
        self.assertEqual(len(sources.listing(self.conn, other)["sources"]), 1)

    def test_an_unknown_kind_is_named_rather_than_stored(self):
        with self.assertRaises(ValueError) as caught:
            self.add(kind="slack_channel", ref="C123")
        self.assertIn("slack_channel", str(caught.exception))

    def test_a_pointer_with_nothing_in_it_is_refused(self):
        with self.assertRaises(ValueError):
            self.add(kind="email_domain", ref="   ")

    def test_renaming_one_leaves_its_pointer_alone(self):
        made = self.add(kind="teams_chat", ref="19:def")
        changed = sources.update(self.conn, made["id"], {"name": "Alice / Bob"})
        self.assertEqual((changed["name"], changed["ref"]), ("Alice / Bob", "19:def"))

    def test_an_edit_cannot_collide_with_another_of_the_folders(self):
        self.add(kind="email_domain", ref="acme.example.com")
        other = self.add(kind="email_domain", ref="acme.com")
        with self.assertRaises(ValueError):
            sources.update(self.conn, other["id"], {"ref": "acme.example.com"})

    def test_they_go_with_the_folder(self):
        self.add(kind="email_domain", ref="acme.example.com")
        self.conn.execute("DELETE FROM folder WHERE id = ?", (self.folder,))
        self.assertEqual(sources.listing(self.conn, self.folder)["sources"], [])

    # --- mail folders ---------------------------------------------------------

    def test_a_mail_folder_is_its_path_from_the_top(self):
        made = self.add(kind="email_folder", ref="Inbox/Clients/Acme")
        self.assertEqual((made["kind"], made["ref"], made["within"]),
                         ("email_folder", "Inbox/Clients/Acme", ""))

    def test_a_mail_folder_path_is_written_one_way(self):
        """Backslashes, stray spaces, a slash at either end: the same folder."""
        made = self.add(kind="email_folder",
                        ref=" \\Inbox \\ Clients\\Acme/ ")
        self.assertEqual(made["ref"], "Inbox/Clients/Acme")

    def test_a_mail_folder_keeps_the_case_you_typed(self):
        self.assertEqual(self.add(kind="email_folder", ref="Inbox/ACME")["ref"],
                         "Inbox/ACME")

    def test_but_two_paths_differing_only_in_case_are_one_folder(self):
        """Outlook will not hold both side by side."""
        self.add(kind="email_folder", ref="Inbox/Clients/Acme")
        with self.assertRaises(ValueError):
            self.add(kind="email_folder", ref="inbox/clients/acme")

    def test_a_path_of_nothing_but_slashes_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.add(kind="email_folder", ref=" / // ")
        self.assertIn("Inbox/", str(caught.exception))

    def test_a_mail_folder_is_offered_to_the_ui(self):
        self.assertIn("email_folder", server.api_schema({}, {})["source_kinds"])


class TestTheAgentFence(unittest.TestCase):
    """What reaches /api/agent/ is matched against a table with no write in it.

    This is the guarantee the whole feature rests on, so it is checked over
    real HTTP against the real route tables rather than by reading them.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = Path(cls.dir.name)
        cls.saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        cls.project = project.add("Fenced")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.dir.cleanup()

    def call(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def setUp(self):
        _, folder = self.call("POST", "/api/p/fenced/folders", {"name": "F"})
        self.folder = folder["id"]
        _, made = self.call("POST", f"/api/p/fenced/folders/{self.folder}"
                                    "/rows/action_item",
                            {"field": "item", "value": "Original"})
        self.row = made["row"]["id"]

    def agent(self, path):
        return f"/api/agent/p/fenced{path}"

    def test_the_agent_table_holds_no_route_that_writes(self):
        """Read as a claim about the table itself: every route in it is a GET,
        bar the one that adds to the review queue."""
        writes = [(verb, pattern) for verb, pattern, _ in server.AGENT_ROUTES
                  if verb != "GET"]
        self.assertEqual(writes, [("POST", r"^/folders/(\d+)/suggestions$")])

    def test_no_write_route_is_reachable_through_the_agent_prefix(self):
        blocked = [
            ("PATCH", self.agent(f"/rows/action_item/{self.row}"),
             {"field": "item", "value": "reached in"}),
            ("POST", self.agent(f"/folders/{self.folder}/rows/action_item"),
             {"field": "item", "value": "reached in"}),
            ("POST", self.agent(f"/rows/action_item/{self.row}/delete"), {}),
            ("POST", self.agent(f"/rows/action_item/{self.row}/archive"), {}),
            ("POST", self.agent(f"/folders/{self.folder}/delete"), {}),
            ("PATCH", self.agent(f"/folders/{self.folder}"), {"name": "renamed"}),
            ("POST", self.agent("/suggestions/1/accept"), {}),
            ("POST", self.agent("/batches/1/accept"), {}),
            ("POST", self.agent("/history/1/delete"), {}),
            ("POST", self.agent("/folders"), {"name": "made by an agent"}),
            # Reading where a folder listens is the point of the list; adding
            # itself a source is not, and there is no address for it.
            ("POST", self.agent(f"/folders/{self.folder}/sources"),
             {"kind": "email_domain", "ref": "somewhere-else.com"}),
            ("PATCH", self.agent("/sources/1"), {"ref": "somewhere-else.com"}),
            ("POST", self.agent("/sources/1/delete"), {}),
        ]
        for method, path, body in blocked:
            status, _ = self.call(method, path, body)
            self.assertIn(status, (404, 405), f"{method} {path} was not refused")

        # And the folder is exactly as it was.
        _, rows = self.call("GET", f"/api/p/fenced/folders/{self.folder}"
                                   "/rows/action_item")
        self.assertEqual([r["fields"]["item"] for r in rows["rows"]], ["Original"])

    def test_the_agent_can_read_a_folder_and_queue_a_suggestion(self):
        status, payload = self.call("GET", self.agent(f"/folders/{self.folder}"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["folder"]["name"], "F")
        self.assertIn("action_item", payload["entries"])
        self.assertIn("action_item", payload["columns"])

        status, queued = self.call(
            "POST", self.agent(f"/folders/{self.folder}/suggestions"),
            {"source": "Outlook", "summary": "1 thread", "suggestions": [
                {"kind": "update", "entity": "action_item", "row": self.row,
                 "field": "item", "value": "Original, restated",
                 "evidence": "'Re: it' - Alice, 13-Sep"}]})
        self.assertEqual(status, 200)
        self.assertEqual(queued["recorded"], 1)

        # Queued, and the table still says what it said.
        _, rows = self.call("GET", f"/api/p/fenced/folders/{self.folder}"
                                   "/rows/action_item")
        self.assertEqual(rows["rows"][0]["fields"]["item"], "Original")

    def test_a_folder_read_says_where_this_folder_listens(self):
        self.call("POST", f"/api/p/fenced/folders/{self.folder}/sources",
                  {"kind": "teams_channel", "name": "Acme Rollout",
                   "ref": "19:abc@thread.tacv2", "within": "team-123"})
        _, payload = self.call("GET", self.agent(f"/folders/{self.folder}"))
        self.assertEqual([(s["kind"], s["ref"], s["within"])
                          for s in payload["sources"]],
                         [("teams_channel", "19:abc@thread.tacv2", "team-123")])

    def test_a_folder_read_says_what_each_entry_is_already_made_of(self):
        """So that deciding whether a thread has been handled is a check against
        what this call already returned, not a second call to the queue."""
        self.call("POST", self.agent(f"/folders/{self.folder}/suggestions"),
                  {"source": "Outlook", "suggestions": [
                      {"kind": "update", "entity": "action_item", "row": self.row,
                       "field": "current_state", "value": "Quote in",
                       "ref": "AAMk-1", "evidence": "e"}]})
        _, queued = self.call(
            "GET", f"/api/p/fenced/folders/{self.folder}/suggestions")
        held = queued["batches"][0]["suggestions"][0]["id"]

        _, before = self.call("GET", self.agent(f"/folders/{self.folder}"))
        entry = next(r for r in before["entries"]["action_item"] if r["id"] == self.row)
        self.assertNotIn("refs", entry, "nothing is read into an entry by proposing it")

        self.call("POST", f"/api/p/fenced/suggestions/{held}/accept", {})
        _, after = self.call("GET", self.agent(f"/folders/{self.folder}"))
        entry = next(r for r in after["entries"]["action_item"] if r["id"] == self.row)
        self.assertEqual(entry["refs"], ["AAMk-1"])

    def test_brief_drops_the_column_schema_and_nothing_else(self):
        """Six folders in a run is six copies of a description of the same three
        tables. The entries are what differ, and those all stay."""
        _, full = self.call("GET", self.agent(f"/folders/{self.folder}"))
        _, brief = self.call("GET", self.agent(f"/folders/{self.folder}?brief=true"))
        self.assertIn("columns", full)
        self.assertNotIn("columns", brief)
        self.assertEqual({k: v for k, v in full.items() if k != "columns"},
                         {k: v for k, v in brief.items() if k != "brief"})

    def test_a_run_can_say_how_far_it_read(self):
        self.call("POST", self.agent(f"/folders/{self.folder}/suggestions"),
                  {"source": "Teams",
                   "covered_through": {"Teams": "2026-09-10T06:00:00+00:00"},
                   "suggestions": [
                       {"kind": "update", "entity": "action_item", "row": self.row,
                        "field": "item", "value": "Restated", "evidence": "e"}]})
        _, payload = self.call("GET", self.agent(f"/folders/{self.folder}"))
        run = next(r for r in payload["last_runs"] if r["source"] == "Teams")
        self.assertEqual(run["covered_through"], "2026-09-10T06:00:00+00:00")
        self.assertNotEqual(run["covered_through"], run["last_run"])

    def test_a_refused_batch_names_which_part_was_wrong(self):
        status, payload = self.call(
            "POST", self.agent(f"/folders/{self.folder}/suggestions"),
            {"source": "Outlook", "suggestions": [
                {"kind": "update", "entity": "action_item", "row": 99999,
                 "field": "item", "value": "x", "evidence": "e"}]})
        self.assertEqual(status, 400)
        self.assertEqual([p["index"] for p in payload["problems"]], [0])
        self.assertIn("99999", payload["problems"][0]["error"])

    def test_deciding_is_reachable_only_from_the_browser_side(self):
        self.call("POST", self.agent(f"/folders/{self.folder}/suggestions"),
                  {"source": "Outlook", "suggestions": [
                      {"kind": "update", "entity": "action_item", "row": self.row,
                       "field": "item", "value": "Accepted by hand",
                       "evidence": "e"}]})
        _, queue = self.call("GET", f"/api/p/fenced/folders/{self.folder}"
                                    "/suggestions")
        one = queue["batches"][0]["suggestions"][0]["id"]

        status, _ = self.call("POST", self.agent(f"/suggestions/{one}/accept"))
        self.assertEqual(status, 404, "an agent could decide its own suggestion")

        status, applied = self.call(
            "POST", f"/api/p/fenced/suggestions/{one}/accept")
        self.assertEqual(status, 200)
        self.assertEqual(applied["state"], "accepted")
        _, rows = self.call("GET", f"/api/p/fenced/folders/{self.folder}"
                                   "/rows/action_item")
        self.assertEqual(rows["rows"][0]["fields"]["item"], "Accepted by hand")

    def make(self, entity, title, **cells):
        key = "item" if entity == "action_item" else "deliverable"
        _, made = self.call("POST", f"/api/p/fenced/folders/{self.folder}/rows/{entity}",
                            {"field": key, "value": title})
        row = made["row"]["id"]
        for field, value in cells.items():
            self.call("PATCH", f"/api/p/fenced/rows/{entity}/{row}",
                      {"field": field, "value": value})
        return row

    def due(self, query="", everywhere=False):
        path = "/api/agent/due" if everywhere else self.agent("/due")
        status, payload = self.call("GET", f"{path}{query}")
        self.assertEqual(status, 200, payload)
        mine = [e for e in payload["entries"] if e["folder"]["id"] == self.folder]
        return payload, mine

    def test_due_reads_dates_the_way_the_table_does(self):
        on = lambda day, note="": {"date": day, "note": note}
        late = self.make("action_item", "Chase supplier quote", resolve_by=on("2026-09-10"),
                         waiting_on="Alice")
        today = self.make("action_item", "Send test plan", resolve_by=on("2026-09-15", "EOD"))
        self.make("action_item", "Next week", resolve_by=on("2026-09-22"))
        self.make("action_item", "ASAP, no date", resolve_by=on("", "ASAP"))
        self.make("action_item", "Already done", resolve_by=on("2026-09-15"), done=True)
        parked = self.make("action_item", "Parked", resolve_by=on("2026-09-15"))
        self.call("POST", f"/api/p/fenced/rows/action_item/{parked}/archive")
        phase = self.make("milestone", "Phase One Signoff", due_date="2026-09-01")
        # Stamped with the phase's date when linked, then it slips to today: the
        # report must follow the deliverable, not the stale stamp.
        borrowed = self.make("action_item", "Waiting on Phase One",
                             resolve_by={"date": "", "note": "", "link": phase})
        self.call("PATCH", f"/api/p/fenced/rows/milestone/{phase}",
                  {"field": "due_date", "value": "2026-09-15"})

        payload, mine = self.due("?today=2026-09-15")
        self.assertEqual(payload["today"], "2026-09-15")
        self.assertEqual(payload["through"], "2026-09-15")
        self.assertEqual([(e["title"], e["status"], e["days"]) for e in mine], [
            ("Chase supplier quote", "overdue", -5),
            ("Send test plan", "today", 0),
            ("Waiting on Phase One", "today", 0),
            ("Phase One Signoff", "today", 0),
        ])
        first = mine[0]
        self.assertEqual((first["entity"], first["id"]), ("action_item", late))
        self.assertEqual(first["fields"]["waiting_on"], "Alice")
        self.assertNotIn("done", first["fields"])
        self.assertEqual(next(e for e in mine if e["id"] == today)["fields"]
                         ["resolve_by"]["note"], "EOD")
        self.assertEqual(next(e for e in mine if e["id"] == borrowed)["fields"]
                         ["resolve_by"]["link_label"], "Phase One Signoff")

        _, week = self.due("?today=2026-09-15&days=7&overdue=false")
        self.assertEqual([e["title"] for e in week],
                         ["Send test plan", "Waiting on Phase One", "Phase One Signoff",
                          "Next week"])

    def test_due_covers_every_project_when_none_is_named(self):
        self.make("action_item", "Cross-project", resolve_by={"date": "2026-09-15",
                                                              "note": ""})
        payload, mine = self.due("?today=2026-09-15", everywhere=True)
        self.assertEqual([e["title"] for e in mine], ["Cross-project"])
        self.assertEqual(mine[0]["project"], {"id": "fenced", "name": "Fenced"})
        self.assertEqual(payload["unavailable"], [])

    def test_due_refuses_a_window_it_cannot_read(self):
        for query in ("?today=15-Sep", "?days=soon", "?days=-1", "?days=9999"):
            status, _ = self.call("GET", self.agent(f"/due{query}"))
            self.assertEqual(status, 400, query)

    def test_the_folder_list_says_how_many_are_waiting(self):
        self.call("POST", self.agent(f"/folders/{self.folder}/suggestions"),
                  {"source": "Teams", "suggestions": [
                      {"kind": "update", "entity": "action_item", "row": self.row,
                       "field": "item", "value": "Counted", "evidence": "e"}]})
        _, payload = self.call("GET", "/api/p/fenced/folders")
        here = next(f for f in payload["folders"] if f["id"] == self.folder)
        self.assertEqual(here["pending"], 1)


class TestMcpServer(unittest.TestCase):
    """The protocol half: what Claude Desktop actually speaks to.

    Driven as a client would drive it -- lines of JSON in, lines of JSON out --
    because the failure that matters here is a malformed message, and a
    malformed message is invisible to a test that calls the handlers directly.
    """

    def drive(self, *messages, base="http://127.0.0.1:1"):
        import io
        out = io.StringIO()
        mcp.serve(base, stdin=io.StringIO(
            "".join(json.dumps(m) + "\n" for m in messages)), stdout=out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_it_answers_initialize_and_lists_its_tools(self):
        replies = self.drive(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(replies), 2, "a notification was answered")
        first = replies[0]["result"]
        self.assertEqual(first["protocolVersion"], "2025-06-18")
        self.assertEqual(first["serverInfo"]["name"], "manila")
        self.assertIn("tools", first["capabilities"])
        # The one thing the model cannot learn from the tool list itself.
        self.assertIn("Nothing you send takes effect on its own",
                      first["instructions"])

        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(names, ["manila_projects", "manila_folders", "manila_folder",
                                 "manila_due", "manila_history", "manila_queue",
                                 "manila_propose"])
        for tool in replies[1]["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertNotIn("handler", tool, "a Python callable was sent on the wire")

    def test_no_tool_offers_to_change_manila(self):
        """The tool list is the model's whole sense of what it may do here."""
        for tool in mcp.ADVERTISED:
            self.assertNotIn(tool["name"], ("manila_update", "manila_write",
                                            "manila_accept", "manila_delete"))
        self.assertIn("DOES NOT CHANGE MANILA",
                      mcp.BY_NAME["manila_propose"]["description"])

    class Recorder:
        def __init__(self):
            self.asked = []
            self.sent = []

        def get(self, path):
            self.asked.append(path)
            return {}

        def post(self, path, body):
            self.sent.append((path, body))
            return {"recorded": len(body.get("suggestions") or [])}

    def test_due_asks_everywhere_unless_a_project_is_named(self):
        client = self.Recorder()
        mcp.tool_due(client, {})
        mcp.tool_due(client, {"days": 6, "overdue": False})
        mcp.tool_due(client, {"project": "acme corp", "today": "2026-09-18", "days": 0})
        self.assertEqual(client.asked, [
            "/due",
            "/due?days=6&overdue=false",
            "/p/acme%20corp/due?days=0&today=2026-09-18",
        ])

    def test_folder_passes_its_scope_on(self):
        # It was advertised and then dropped, so archived came back as active.
        client = self.Recorder()
        mcp.tool_folder(client, {"project": "p", "folder": 3, "scope": "archived"})
        mcp.tool_folder(client, {"project": "p", "folder": 3})
        self.assertEqual(client.asked, ["/p/p/folders/3?scope=archived",
                                        "/p/p/folders/3?scope=active"])

    def test_brief_is_asked_for_only_when_it_is_wanted(self):
        client = self.Recorder()
        mcp.tool_folder(client, {"project": "p", "folder": 3, "brief": True})
        mcp.tool_folder(client, {"project": "p", "folder": 3, "brief": False})
        self.assertEqual(client.asked, ["/p/p/folders/3?scope=active&brief=true",
                                        "/p/p/folders/3?scope=active"])

    def test_a_proposal_carries_how_far_the_run_read(self):
        client = self.Recorder()
        mcp.tool_propose(client, {
            "project": "p", "folder": 3, "source": "Teams",
            "covered_through": "2026-09-10T06:00:00+00:00",
            "suggestions": [{"kind": "tick", "entity": "action_item",
                             "row": 1, "evidence": "e"}]})
        self.assertEqual(client.sent[0][1]["covered_through"],
                         "2026-09-10T06:00:00+00:00")

    def test_a_proposal_carries_each_sources_watermark_and_rebaseline(self):
        client = self.Recorder()
        through = {"Outlook": "2026-09-16T23:50:00Z", "Teams": "2026-09-16T23:48:20Z"}
        mcp.tool_propose(client, {
            "project": "p", "folder": 3, "source": "both", "suggestions": [],
            "covered_through": through, "rebaseline": True})
        self.assertEqual(client.sent[0][1]["covered_through"], through)
        self.assertIs(client.sent[0][1]["rebaseline"], True)

    def test_a_run_that_does_not_say_how_far_it_read_sends_nothing(self):
        """Empty, never the run's own clock: a watermark invented here is one
        nothing downstream can tell from a real one."""
        client = self.Recorder()
        mcp.tool_propose(client, {
            "project": "p", "folder": 3, "source": "Teams",
            "suggestions": [{"kind": "tick", "entity": "action_item",
                             "row": 1, "evidence": "e"}]})
        self.assertEqual(client.sent[0][1]["covered_through"], "")

    def test_an_unknown_protocol_version_still_connects(self):
        replies = self.drive(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(replies[0]["result"]["protocolVersion"], mcp.PROTOCOL)

    def test_manila_being_down_is_said_plainly_not_thrown(self):
        replies = self.drive(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "manila_projects", "arguments": {}}})
        result = replies[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("not answering", result["content"][0]["text"])
        self.assertIn("-m manila", result["content"][0]["text"])

    def test_an_unknown_tool_is_a_result_the_model_can_recover_from(self):
        replies = self.drive(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "manila_delete_everything", "arguments": {}}})
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertIn("manila_propose", replies[0]["result"]["content"][0]["text"])

    def test_an_unknown_method_is_a_protocol_error(self):
        replies = self.drive({"jsonrpc": "2.0", "id": 1, "method": "nonsense"})
        self.assertEqual(replies[0]["error"]["code"], -32601)

    def test_a_broken_line_does_not_take_the_server_down(self):
        replies = self.drive_raw("{not json\n",
                                 json.dumps({"jsonrpc": "2.0", "id": 2,
                                             "method": "ping"}) + "\n")
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"], {})

    def drive_raw(self, *lines, base="http://127.0.0.1:1"):
        import io
        out = io.StringIO()
        mcp.serve(base, stdin=io.StringIO("".join(lines)), stdout=out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

class TestMergingIntoAnExistingConfig(unittest.TestCase):
    """Most machines already have a working MCP setup, and the block that
    --mcp-config prints is a whole file. Pasted over the top it takes every
    other server with it, so there has to be a way in that adds one key."""

    BUSY = {
        "globalShortcut": "Ctrl+Alt+Space",
        "mcpServers": {
            "filesystem": {"command": "npx",
                           "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
            "github": {"command": "npx", "args": ["-y", "server-github"],
                       "env": {"GITHUB_TOKEN": "keep-me"}},
        },
    }

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / "claude_desktop_config.json")

    def write(self, payload):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return Path(self.path).read_text(encoding="utf-8")

    def merge(self):
        import contextlib
        import io
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            done = mcp.install(path=self.path)
        return done, said.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def backups(self):
        return sorted(f for f in os.listdir(self.dir.name) if ".bak-" in f)

    def test_everything_already_there_is_carried_through(self):
        self.write(self.BUSY)
        done, _ = self.merge()
        self.assertTrue(done)
        after = self.read()
        self.assertEqual(after["globalShortcut"], self.BUSY["globalShortcut"])
        for name, spec in self.BUSY["mcpServers"].items():
            self.assertEqual(after["mcpServers"][name], spec, f"{name} was disturbed")
        self.assertIn("manila", after["mcpServers"])

    def test_a_secret_in_another_server_is_not_touched(self):
        self.write(self.BUSY)
        self.merge()
        self.assertEqual(
            self.read()["mcpServers"]["github"]["env"]["GITHUB_TOKEN"], "keep-me")

    def test_the_file_as_it_was_is_kept(self):
        original = self.write(self.BUSY)
        self.merge()
        kept = Path(self.dir.name, self.backups()[0]).read_text(encoding="utf-8")
        self.assertEqual(kept, original)

    def test_running_it_twice_never_overwrites_the_first_backup(self):
        """Two runs inside one second would otherwise share a filename, and the
        second copy -- already merged -- would replace the only pristine one."""
        original = self.write(self.BUSY)
        self.merge()
        self.merge()
        kept = [Path(self.dir.name, name).read_text(encoding="utf-8")
                for name in self.backups()]
        self.assertEqual(len(kept), 2)
        self.assertIn(original, kept, "the original was lost")

    def test_merging_twice_replaces_rather_than_accumulates(self):
        self.write(self.BUSY)
        self.merge()
        self.merge()
        self.assertEqual(sorted(self.read()["mcpServers"]),
                         ["filesystem", "github", "manila"])

    def test_a_config_that_is_not_valid_json_is_refused_not_rewritten(self):
        Path(self.path).write_text('{"mcpServers": {"a": {},,}', encoding="utf-8")
        was = Path(self.path).read_text(encoding="utf-8")
        done, said = self.merge()
        self.assertFalse(done)
        self.assertIn("not valid JSON", said)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), was)
        self.assertEqual(self.backups(), [], "a broken file was backed up as if used")

    def test_an_absent_config_is_created_rather_than_refused(self):
        done, said = self.merge()
        self.assertTrue(done)
        self.assertIn("does not exist yet", said)
        self.assertEqual(sorted(self.read()["mcpServers"]), ["manila"])

    def test_the_entry_written_is_the_one_mcp_config_prints(self):
        self.write(self.BUSY)
        self.merge()
        printed = json.loads(mcp.config_block())["mcpServers"]["manila"]
        self.assertEqual(self.read()["mcpServers"]["manila"], printed)


    def test_the_store_build_keeps_its_config_inside_the_package(self):
        """Installed from the Microsoft Store, Claude Desktop is an MSIX app,
        and Windows redirects its Roaming AppData into the package container.
        Looking only in %APPDATA% reports a machine as having no config while
        Claude Desktop is reading one happily."""
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder, "Local")
            packaged = (local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache"
                        / "Roaming" / "Claude" / mcp.CONFIG_NAME)
            packaged.parent.mkdir(parents=True)
            packaged.write_text("{}", encoding="utf-8")
            with mock.patch.object(mcp.os, "name", "nt"), \
                    mock.patch.dict(os.environ, {
                        "LOCALAPPDATA": str(local),
                        "APPDATA": str(Path(folder, "Roaming"))}):
                self.assertEqual(mcp.config_path(), str(packaged))

    def test_the_classic_install_is_still_found(self):
        with tempfile.TemporaryDirectory() as folder:
            roaming = Path(folder, "Roaming")
            classic = roaming / "Claude" / mcp.CONFIG_NAME
            classic.parent.mkdir(parents=True)
            classic.write_text("{}", encoding="utf-8")
            with mock.patch.object(mcp.os, "name", "nt"), \
                    mock.patch.dict(os.environ, {
                        "LOCALAPPDATA": str(Path(folder, "Local")),
                        "APPDATA": str(roaming)}):
                self.assertEqual(mcp.config_path(), str(classic))

    def test_with_neither_there_it_says_where_to_create_one(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(mcp.os, "name", "nt"), \
                    mock.patch.dict(os.environ, {
                        "LOCALAPPDATA": str(Path(folder, "Local")),
                        "APPDATA": str(Path(folder, "Roaming"))}):
                self.assertTrue(mcp.config_path().endswith(mcp.CONFIG_NAME))

    def test_the_config_block_is_json_claude_desktop_can_read(self):
        block = json.loads(mcp.config_block())
        entry = block["mcpServers"]["manila"]
        self.assertEqual(entry["args"], ["-m", "manila.mcp"])
        self.assertTrue(Path(entry["env"]["PYTHONPATH"], "manila", "mcp.py").exists(),
                        "the PYTHONPATH does not contain manila.mcp")

    def test_windows_stdio_defaults_cannot_corrupt_the_framing(self):
        """Both of these are Windows-only and silent, which is why they are
        checked here rather than noticed there: the client reports a server
        that would not start, and there is nothing on screen either way."""
        import io
        raw = io.BytesIO()
        # A real Windows stdout: legacy code page, and \n written as \r\n.
        windows_stdout = io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n")
        mcp.serve("http://127.0.0.1:1", stdin=io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"),
            stdout=windows_stdout)
        written = raw.getvalue()
        self.assertNotIn(b"\r", written, "a carriage return landed in the framing")
        self.assertEqual(json.loads(written.decode("utf-8"))["id"], 1)

    def test_a_request_outside_the_code_page_arrives_intact(self):
        """Outgoing messages are escaped to ASCII by json.dumps, so the half
        that really carries raw UTF-8 is the incoming one.

        Written with ensure_ascii=False deliberately: that is what Claude
        Desktop sends. It is an Electron app, and JSON.stringify emits raw
        UTF-8 rather than escapes -- so this is the shape of a real request,
        and a test using Python's own default would send plain ASCII and prove
        nothing at all.
        """
        import io
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "manila_folders",
                                         "arguments": {"project": "\u00d8stergaard"}}},
                             ensure_ascii=False)
        incoming = io.BytesIO((request + "\n").encode("utf-8"))
        windows_stdin = io.TextIOWrapper(incoming, encoding="cp1252")

        seen = {}
        # The tool table holds the handler itself, so this is where a stand-in
        # has to go -- patching the module attribute would miss it.
        with mock.patch.dict(mcp.BY_NAME["manila_folders"],
                             {"handler": lambda _c, args: seen.update(args) or {}}):
            out = io.StringIO()
            mcp.serve("http://127.0.0.1:1", stdin=windows_stdin, stdout=out)
        self.assertEqual(seen.get("project"), "\u00d8stergaard")

    def test_store_python_is_named_by_its_stable_alias(self):
        """The Microsoft Store build reports an executable under a versioned
        package directory: ACL-locked, and gone the next time Python updates.
        A config naming it works until it silently does not."""
        import ntpath
        store = (r"C:\Program Files\WindowsApps"
                 r"\PythonSoftwareFoundation.Python.3.13_3.13.2032.0_x64__qbz5n2kfra8p0"
                 r"\python3.13.exe")
        local = r"C:\Users\someone\AppData\Local"
        alias = ntpath.join(local, "Microsoft", "WindowsApps", "python.exe")
        # ntpath, so this reads the same whichever platform runs the tests.
        with mock.patch.object(mcp.os, "name", "nt"), \
                mock.patch.object(mcp.os, "path", ntpath), \
                mock.patch.object(mcp.sys, "executable", store), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": local}), \
                mock.patch.object(ntpath, "exists", lambda p: p == alias):
            self.assertEqual(mcp.windows_runner(), alias)

    def test_a_console_less_interpreter_is_swapped_for_its_twin(self):
        """pythonw.exe launches Manila from its Start Menu icon and has no
        console at all -- while this server is nothing but stdin and stdout."""
        import ntpath
        with mock.patch.object(mcp.os, "name", "nt"), \
                mock.patch.object(mcp.os, "path", ntpath), \
                mock.patch.object(mcp.sys, "executable", r"C:\Py\pythonw.exe"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mcp.windows_runner(), r"C:\Py\python.exe")

    def test_the_windows_block_carries_systemroot(self):
        """Naming env at all leaves Claude Desktop launching Python with almost
        nothing else, and Python on Windows will not start without SYSTEMROOT.
        It exits instantly, with no message anywhere."""
        with mock.patch.object(mcp.os, "name", "nt"), \
                mock.patch.dict(os.environ, {"SYSTEMROOT": r"C:\Windows"}):
            env = json.loads(mcp.config_block())["mcpServers"]["manila"]["env"]
        self.assertEqual(env["SYSTEMROOT"], r"C:\Windows")
        self.assertIn("PYTHONPATH", env)

    def test_check_separates_a_broken_server_from_an_absent_manila(self):
        import io
        import contextlib
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            healthy = mcp.check("http://127.0.0.1:1")
        printed = said.getvalue()
        self.assertFalse(healthy)
        # The protocol half is fine; it is Manila that is not up.
        self.assertIn("[ok]   the server speaks MCP", printed)
        self.assertIn("not answering", printed)

    def test_check_names_a_config_that_lists_other_servers_but_not_manila(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "claude_desktop_config.json"
            path.write_text(json.dumps({"mcpServers": {"filesystem": {}}}),
                            encoding="utf-8")
            said = io.StringIO()
            with mock.patch.object(mcp, "config_path", return_value=str(path)), \
                    contextlib.redirect_stdout(said):
                healthy = mcp.check("http://127.0.0.1:1")
        self.assertFalse(healthy)
        self.assertIn("filesystem", said.getvalue())
        self.assertIn("no 'manila' entry", said.getvalue())

    def test_check_catches_a_config_whose_paths_are_from_another_machine(self):
        """The exact shape of a block pasted out of WSL into Windows."""
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "claude_desktop_config.json"
            # A block pasted out of WSL into Windows: on the machine meant to
            # read it, neither of these paths is there. Spelled with a name no
            # platform has, so the check is about the config and not about
            # which machine the tests happen to run on.
            path.write_text(json.dumps({"mcpServers": {"manila": {
                "command": "/not/a/real/interpreter/python3",
                "args": ["-m", "manila.mcp"],
                "env": {"PYTHONPATH": "/not/a/real/checkout/manila"}}}}),
                encoding="utf-8")
            said = io.StringIO()
            with mock.patch.object(mcp, "config_path", return_value=str(path)), \
                    contextlib.redirect_stdout(said):
                healthy = mcp.check("http://127.0.0.1:1")
        printed = said.getvalue()
        self.assertFalse(healthy)
        self.assertIn("not a file on this", printed)
        self.assertIn("does not contain", printed)

    def test_wsl_is_not_offered_a_config_for_the_wrong_machine(self):
        """Manila is developed in WSL and run on Windows Python, so this is the
        one case where the machine printing the block is reliably not the one
        that will read it -- and a Windows Claude Desktop handed a WSL path
        fails at launch with nothing on screen to say why."""
        import io
        import contextlib
        from manila import __main__ as cli

        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}), \
                mock.patch.object(cli.os, "name", "posix"):
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                cli.show_mcp_config(mcp.DEFAULT_URL)
        printed = said.getvalue()
        self.assertIn("py -m manila --mcp-config", printed)
        self.assertIn("APPDATA", printed)
        self.assertNotIn("Library/Application Support", printed)

    def test_windows_is_told_where_windows_keeps_the_file(self):
        import io
        import contextlib
        from manila import __main__ as cli

        with mock.patch.object(cli, "in_wsl", return_value=False), \
                mock.patch.object(cli.os, "name", "nt"):
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                cli.show_mcp_config(mcp.DEFAULT_URL)
        self.assertIn(r"%APPDATA%\Claude\claude_desktop_config.json",
                      said.getvalue())

    def test_every_tool_declares_an_object_schema(self):
        for tool in mcp.ADVERTISED:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            for name in tool["inputSchema"].get("required", []):
                self.assertIn(name, tool["inputSchema"]["properties"], tool["name"])


class TestMcpAgainstAServer(TestTheAgentFence):
    """The MCP tools over a real Manila, which is the only way to catch a tool
    that builds a path the server has no route for."""

    def client(self):
        return mcp.Manila(f"http://127.0.0.1:{self.port}")

    def test_every_read_tool_reaches_a_real_route(self):
        client = self.client()
        self.assertIn("fenced", [p["id"] for p in
                                 mcp.tool_projects(client, {})["projects"]])
        folders = mcp.tool_folders(client, {"project": "fenced"})["folders"]
        self.assertIn(self.folder, [f["id"] for f in folders])
        whole = mcp.tool_folder(client, {"project": "fenced", "folder": self.folder})
        self.assertEqual(whole["folder"]["id"], self.folder)
        self.assertIn("entries", mcp.tool_folder(
            client, {"project": "fenced", "folder": self.folder}))
        self.assertIn("entries", mcp.tool_history(
            client, {"project": "fenced", "entity": "action_item", "row": self.row}))
        self.assertIn("batches", mcp.tool_queue(
            client, {"project": "fenced", "folder": self.folder}))
        self.assertIn("counts", mcp.tool_due(client, {}))
        self.assertIn("counts", mcp.tool_due(
            client, {"project": "fenced", "days": 6, "overdue": False,
                     "today": "2026-09-15"}))

    def test_the_folder_tool_carries_every_source_of_every_kind(self):
        added = [
            {"kind": "teams_channel", "ref": "19:abc@thread.tacv2", "within": "team-123"},
            {"kind": "teams_chat", "ref": "19:chat@thread.v2"},
            {"kind": "email_folder", "ref": "Inbox/Clients/Acme"},
            {"kind": "email_address", "ref": "alice@example.com"},
            {"kind": "email_domain", "ref": "example.com"},
        ]
        self.assertEqual({s["kind"] for s in added}, set(sources.KINDS))
        for body in added:
            status, _ = self.call(
                "POST", f"/api/p/fenced/folders/{self.folder}/sources", body)
            self.assertEqual(status, 200, body)
        whole = mcp.tool_folder(self.client(), {"project": "fenced",
                                                "folder": self.folder, "brief": True})
        self.assertEqual([(s["kind"], s["ref"]) for s in whole["sources"]],
                         [(s["kind"], s["ref"]) for s in added])

    def test_a_quiet_run_through_the_tool_records_its_watermark(self):
        client = self.client()
        made = mcp.tool_propose(client, {
            "project": "fenced", "folder": self.folder, "source": "Outlook",
            "summary": "nothing since 10-Sep", "suggestions": [],
            "covered_through": {"Outlook": "2026-09-15T00:00:00+00:00"}})
        self.assertEqual(made["recorded"], 0)
        self.assertIn("recorded", made["note"])
        whole = mcp.tool_folder(client, {"project": "fenced", "folder": self.folder})
        self.assertIn({"source": "Outlook", "covered_through": "2026-09-15T00:00:00+00:00"},
                      [{k: r[k] for k in ("source", "covered_through")}
                       for r in whole["last_runs"]])
        with self.assertRaises(ValueError):
            mcp.tool_propose(client, {"project": "fenced", "folder": self.folder,
                                      "source": "Outlook", "suggestions": []})

    def test_an_archived_entry_is_reachable_through_the_folder_tool(self):
        self.call("POST", f"/api/p/fenced/rows/action_item/{self.row}/archive")
        seen = lambda scope: [r["id"] for r in mcp.tool_folder(self.client(), {
            "project": "fenced", "folder": self.folder, "scope": scope,
        })["entries"]["action_item"]]
        self.assertEqual(seen("active"), [])
        self.assertEqual(seen("archived"), [self.row])

    def test_proposing_through_the_tool_queues_without_changing_anything(self):
        client = self.client()
        result = mcp.tool_propose(client, {
            "project": "fenced", "folder": self.folder, "source": "Outlook",
            "suggestions": [{"kind": "update", "entity": "action_item",
                             "row": self.row, "field": "item",
                             "value": "Proposed through MCP",
                             "evidence": "'Re: it' - Alice"}]})
        self.assertEqual(result["recorded"], 1)
        self.assertIn("Nothing has changed yet", result["note"])
        _, rows = self.call("GET", f"/api/p/fenced/folders/{self.folder}"
                                   "/rows/action_item")
        self.assertEqual(rows["rows"][0]["fields"]["item"], "Original")

    def test_a_refusal_comes_back_as_something_the_model_can_fix(self):
        client = self.client()
        with self.assertRaises(ValueError) as caught:
            mcp.tool_propose(client, {
                "project": "fenced", "folder": self.folder, "source": "Outlook",
                "suggestions": [{"kind": "update", "entity": "action_item",
                                 "row": self.row, "field": "no_such_column",
                                 "value": "x", "evidence": "e"}]})
        self.assertIn("no_such_column", str(caught.exception))

    def test_a_missing_project_is_named_rather_than_thrown(self):
        with self.assertRaises(ValueError) as caught:
            mcp.tool_folders(self.client(), {"project": "not-a-project"})
        self.assertIn("not-a-project", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
