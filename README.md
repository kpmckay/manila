# Manila

A simple action item tracking tool.

A local, single-user tool for tracking work that mostly lives with other
people: what you are waiting on, who owes it, when you last chased it, and what
the state was the last few times you looked.

Manila uses only the Python standard library and vanilla browser JavaScript.
There is nothing to install: it runs on any machine with Python 3.11+.

## Running it

    python3 -m manila            # Windows: py -m manila

Then open <http://127.0.0.1:8000>, or pass `--open` and let it open for you.

`--open` gives Manila a window of its own -- no tab strip, no address bar, its
own taskbar button -- by asking any installed Chromium for an app window. If
none is found you get an ordinary browser tab, which `--tab` asks for
deliberately. Asking for Manila when it is already open raises the window you
have rather than starting a second server.

### On Windows, as an installed program

    powershell -ExecutionPolicy Bypass -File windows\install.ps1 -Desktop

That copies Manila to `%LOCALAPPDATA%\Programs\Manila` and puts it in the Start
Menu, so it launches from an icon: no console window, no terminal to leave
open. Nothing is downloaded and no admin rights are needed. You need Python
3.11+ first, from [python.org](https://www.python.org/downloads/) (tick **Add
python.exe to PATH**) or the Microsoft Store.

While Manila runs there is a folder icon in the notification area: double-click
raises the window, right-click offers **Stop Manila**. Closing the window does
not stop the server, any more than closing a tab would. `--no-tray` turns the
icon off; `windows\stop.ps1` is the fallback if it cannot be created.

To remove the program:

    powershell -ExecutionPolicy Bypass -File windows\install.ps1 -Uninstall

Uninstalling never touches your projects.

## Projects

A **project** is a folder somewhere outside this directory holding its own
database. Projects share nothing -- separate files, separate history -- and one
server on one port serves all of them. The entry page lists them; pick one and
you are in it. The name in the header is the way back.

Creating one asks where it should live, and you can walk the machine's folders
to pick or type a path. Everything for a project, its database and every
document imported into it, lives in that one folder.

Left to itself, Manila uses the platform's own conventions:

| | Projects | Registry |
|---|---|---|
| **Linux** | `~/.local/share/manila/<id>/` | `~/.config/manila/projects.json` |
| **Windows** | `%LOCALAPPDATA%\Manila\<id>\` | `%APPDATA%\Manila\projects.json` |

A project has a **name** you can change whenever and an **id** fixed at
creation, used in the URL and the directory name, so renaming moves nothing and
bookmarks keep working.

From the command line:

    python3 -m manila --add "A Project" --path /somewhere/backed-up
    python3 -m manila --list
    python3 -m manila --forget a-project   # takes it off the list; data untouched

**Removing a project never deletes anything.** It comes off the entry page and
its folder stays where it was.

### Moving to another PC

A project is its folder -- the database, and every document copied into it,
named relative to that folder. So moving machines is copying the folder across
and then, on the new machine, **Open an existing project** on the entry page,
pointed at that folder. Or:

    python3 -m manila --import /path/to/the/folder

It is read where it lies: nothing is copied, moved or written inside it. Copy
the folder while Manila is closed, since a copy taken mid-write can miss the
last edits.

## Structure

A project holds **folders**, one per workstream, shown as tabs. Each folder has
four pages:

| | |
|---|---|
| **Dashboard** | Actions -- what is owed, by whom, by when -- and Dates and Milestones |
| **Contact List** | who the people are and where they sit |
| **Repository** | notes, documents and pictures, placed freely on a canvas |
| **Review** | what an assistant has proposed, waiting for you to decide |

## Tables and history

Tables are edited in place: click a cell, type, press Enter. Rows are dragged
into the order you want, or sorted by date. Anything finished can be ticked off
or archived, and archived rows are still there behind a pill.

**Every cell edit is kept.** A cell that has changed carries a small count;
clicking it shows what it said before, and when. That is the point of the tool
-- what a thing's state was the last few times you looked is usually the
question, and it is the thing a spreadsheet loses. History can be pruned
deliberately, entry by entry.

Dates are shown as both the date and the distance from today, and a deadline in
the past says so. A deadline can also borrow its date from one of the folder's
milestones, so a milestone that slips carries everything waiting on it along.

There is no Save button. The header says continuously what has been written and
what has not.

## The Repository

A per-folder canvas of frames you place by hand: notes, lists of documents, and
pictures. Notes are plain text or a small formatting editor. Documents dropped
on it are *copied* into the project and handed to whatever the machine already
uses to open them -- Manila does not display them.

## Suggestions over MCP

Manila ships an MCP server, so an assistant that already reaches your mail and
chats through its own connectors can read a folder, compare it with what it
finds, and say what it thinks should change.

**It cannot change anything.** Everything it proposes lands in the folder's
Review tab and waits. You accept, edit, or reject it, and accepting applies it
exactly as though you had typed it -- same write, same history entry. This is
enforced by the routing, not by a prompt: the agent side of the API is a route
table of its own with no write in it.

To set it up for Claude Desktop:

    python3 -m manila --mcp-install     # or --mcp-config to paste it yourself
    python3 -m manila --mcp-check       # says why it is not being seen

Each folder can also list its **sources** -- the channels, chats, mail folders
or addresses its news arrives on -- so an assistant is told where to look rather
than guessing. Pointers only, never keywords, and each run records how far it
read so the next one does not start over.

## Reaching it from your other devices

    python3 -m manila --tailscale

That binds to this machine's Tailscale address, and only that, so the tool
follows you across your own devices without answering on hotel wifi.

**Manila has no login.** Any device that can route to the address it bound to
can read and edit every project on it.

### What guards it while it runs

Manila serves only the names it knows it is reached by -- `localhost`, the
address it bound to, this machine's own name, and MagicDNS names when it is
serving a tailnet. A name someone else controls, pointed at this machine, is
refused, because otherwise their web page and this server would look to your
browser like one origin. Add your own with `--allow-host NAME` if you reach it
by some other name.

Writes must come from Manila's own pages. Another site can put a form on a page
you have open and post it here without asking anyone, so a change that arrives
claiming a different origin is refused before it reaches a route. Requests with
no browser behind them at all -- curl, a script, the MCP server -- are
unaffected.

## Tests

    python3 -m unittest discover -s tests     # storage, history, ordering, MCP
    node tests/js_check.js                    # browser modules and pure logic
    node tests/ui_check.js                    # the app driven through a fake DOM

The JS checks run under any ES-module runtime (`node`, `deno` or `gjs`).
`ui_check.js` renders the real app into `fake_dom.js`, a small tree that
dispatches events and answers the questions the app actually asks, so a view
that is never told something changed fails a test rather than being noticed in
use.
