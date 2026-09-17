"""Manila as an MCP server, for Claude Desktop.

    py -m manila.mcp            # Windows
    python3 -m manila.mcp

What this is for: you keep the state of your work in Manila, and the evidence
that it moved arrives in Outlook and Teams. Reconciling the two by hand is the
chore. An assistant that can read both your mail and this folder can do that
reconciliation -- and should not be trusted to do it silently.

So this server can read a folder, and it can add to that folder's review queue.
It cannot change anything. There is no tool here that writes a cell, and no
address it could call to: the agent side of Manila's API is a route table of
its own holding nothing that writes. Everything proposed waits in Manila's
Review tab until you accept it, reject it, or edit it and then accept it. An
accepted suggestion is applied exactly as though you had typed it -- same
history entry, same chip -- because that is what it now is.

Mail and chat are not read here. Claude Desktop already reaches those through
its own connectors, and duplicating that would mean a second app registration,
a second consent screen, and a token cache to look after. This server is the
Manila half and only that.

It speaks JSON-RPC over stdin and stdout, which is all MCP's stdio transport
is, so it needs nothing installed -- the same rule as the rest of Manila.
stdout carries protocol and nothing else; anything worth saying goes to stderr,
where Claude Desktop's logs pick it up.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000"

# The versions of the protocol this speaks. A client asking for one of them
# gets it back; anything else is answered with the newest, which is what the
# spec asks for and lets a newer Claude Desktop still connect.
KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
PROTOCOL = KNOWN_PROTOCOLS[-1]

NAME = "manila"
VERSION = "1.1.0"

# Given to the model once, at connection. The rule it most needs is the one it
# cannot discover from the tool list: nothing here takes effect on its own.
INSTRUCTIONS = """\
Manila tracks action items that live with other people: what is owed, by whom,
when it was last chased, and what the state was the last few times you looked.

You can read a Manila folder and you can propose changes to it. You cannot
change it. Everything you propose goes into a review queue that the person
using Manila reads, and each suggestion is accepted, edited, or rejected by
hand. Nothing you send takes effect on its own, so propose what you actually
believe and let them decide -- but do not propose noise, because a queue that
is mostly noise stops being read.

For a report -- what is due today, what is overdue, what is coming up --
call manila_due once. It covers every folder, and every project unless you
name one, and has already worked out which dates count. Reporting proposes
nothing; do not queue suggestions unless you are asked to update Manila.

Working order, when updating a folder from mail or chat:

1. manila_folder first, always. Propose against what the folder says now, not
   what you remember. It also carries the three things a sync needs: `sources`,
   the channel, chat and mail pointers this folder is about; `last_runs`, how
   far each source has been read; and, on each entry, `refs` -- the messages
   already read into it. Pass brief:true after the first folder of a run: the
   column descriptions are the same every time.
2. Read the mail or chats through whatever connector you have. `sources` says
   where this folder's news arrives and is the place to start -- but it is a
   starting point, not a fence. Internal mail, a channel nobody has listed,
   and a colleague's aside carry plenty, and a folder with an empty `sources`
   has given you no hints rather than nothing to read. Widen with the words in
   the folder's own items and waiting_on cells, and with the names in its
   contact list, which is where the internal people are.
   An `email_folder` source is an Outlook folder the user files this folder's
   mail into by hand, given as a path from the top of the mailbox
   (Inbox/Clients/Acme); read it and every folder beneath it, and
   treat what is filed there as belonging to this folder. Mail search tools
   usually want the folder's own name rather than its path: that is `leaf`.
   Check what comes back against the full path, since two folders can share
   a leaf.
   A source with no entry in `last_runs`, or one whose `covered_through` is
   empty, has no watermark. Bound that first pass to a window you choose --
   30 days is usually right -- and say so in `summary`, rather than reaching
   for everything and queueing a backlog as though it were this morning.
3. Check each candidate against the entry's `refs` before proposing: a message
   already listed there is one that has been accepted into that entry, and
   proposing it again in different words is the thing that makes a queue stop
   being read. manila_queue as well, for what is waiting but not yet decided.
4. manila_propose once, with the run's findings together, and with
   `covered_through` set per source to how far you actually read -- keyed by
   the same names `last_runs` uses, as full timestamps with a timezone. A
   source you did not finish gets the point you did reach, or is left out.
   Do this even when the run found nothing: send an empty `suggestions`
   list, so the watermark still moves and the next run does not reread the
   same stretch.

What is worth proposing:

- current_state rewritten when a thread says the situation moved. One or two
  short sentences: where it stands, and what it waits on. Manila is a place
  to see the state at a glance, not to keep the task's details -- those stay
  in the thread, which `evidence` already points to.
    Good: "Quote received; waiting on Sam to approve pricing."
    Too much: a paragraph retelling who wrote what on which day.
- last_touched when there is a real contact: the date, how (Email, Chat, Call,
  Meeting, Text), and a short note.
- resolve_by when a date is agreed, slipped, or dropped.
- A new action item when a thread commits someone to something you are not
  tracking yet -- from anywhere you read, listed source or not. This is the one
  the folder cannot point you at: a task that is new matches no word written
  down in it yet, and is the thing most easily missed by searching only what is
  already there.
- A new contact when a name is on a thread and not in the folder.
- tick or archive when a thread plainly closes something out.

Every suggestion must carry `evidence`: the subject line, who it was from, and
when. That is what the reviewer reads first, and a suggestion without it is
usually rejected on sight. Carry `ref` too -- the message id -- so that running
the same sync twice does not queue everything a second time.

Quote what the mail actually says. Do not infer a date that was not given, and
do not write a current_state that reads like a summary of the thread; write
what the reviewer would have written in the cell -- tersely. The same goes
for `reason` and the note on last_touched: a line, not a paragraph.\
"""


class Down(Exception):
    """Manila is not answering. Worth saying plainly rather than as a trace."""


# --- talking to Manila -------------------------------------------------------

class Manila:
    """The agent half of Manila's HTTP API, and nothing else.

    Every path here begins /api/agent/. That prefix is matched in the server
    against a route table with no write in it, so a bug in this file cannot
    turn into an edit -- the worst it can do is ask for something that 404s.
    """

    def __init__(self, base):
        self.base = base.rstrip("/")

    def _call(self, method, path, body=None):
        url = f"{self.base}/api/agent{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            payload = {}
            try:
                payload = json.loads(exc.read() or b"{}")
            except (ValueError, OSError):
                pass
            raise ValueError(payload.get("error")
                             or f"Manila refused that ({exc.code})") from exc
        except urllib.error.URLError as exc:
            raise Down(
                f"Manila is not answering at {self.base} ({exc.reason}). "
                "Start it first -- `py -m manila` on Windows, `python3 -m manila` "
                "elsewhere -- or set MANILA_URL if it serves on another port."
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise Down(f"Manila did not answer at {self.base}: {exc}") from exc

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)


# --- tools -------------------------------------------------------------------

def _project(arguments):
    chosen = str(arguments.get("project") or "").strip()
    if not chosen:
        raise ValueError("which project? Call manila_projects to list them.")
    return urllib.parse.quote(chosen, safe="")


def _folder(arguments):
    try:
        return int(arguments["folder"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("which folder? Call manila_folders for this project's ids.")


def tool_projects(client, _arguments):
    return client.get("/projects")


def tool_folders(client, arguments):
    return client.get(f"/p/{_project(arguments)}/folders")


def tool_folder(client, arguments):
    scope = str(arguments.get("scope") or "active")
    brief = "&brief=true" if arguments.get("brief") else ""
    return client.get(f"/p/{_project(arguments)}/folders/{_folder(arguments)}"
                      f"?scope={urllib.parse.quote(scope)}{brief}")


def tool_due(client, arguments):
    query = {}
    if arguments.get("days") is not None:
        query["days"] = arguments["days"]
    if arguments.get("overdue") is not None:
        query["overdue"] = "true" if arguments["overdue"] else "false"
    if arguments.get("today"):
        query["today"] = arguments["today"]
    suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
    # No project means every project: "what is due" is a question about your
    # week, and your week does not stop at one of them.
    if str(arguments.get("project") or "").strip():
        return client.get(f"/p/{_project(arguments)}/due{suffix}")
    return client.get(f"/due{suffix}")


def tool_history(client, arguments):
    entity = str(arguments.get("entity") or "").strip()
    try:
        row = int(arguments["row"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("row must be the id of an entry, as given by manila_folder")
    return client.get(f"/p/{_project(arguments)}/rows/{entity}/{row}/history")


def tool_queue(client, arguments):
    scope = str(arguments.get("scope") or "pending")
    return client.get(
        f"/p/{_project(arguments)}/folders/{_folder(arguments)}/suggestions"
        f"?scope={urllib.parse.quote(scope)}")


def tool_propose(client, arguments):
    items = arguments.get("suggestions")
    items = [] if items is None else items
    if not isinstance(items, list):
        raise ValueError("suggestions must be a list")
    if not items and not arguments.get("covered_through"):
        raise ValueError("send at least one suggestion, or covered_through to "
                         "record how far a run that found nothing read")
    result = client.post(
        f"/p/{_project(arguments)}/folders/{_folder(arguments)}/suggestions",
        {"source": arguments.get("source") or "",
         "summary": arguments.get("summary") or "",
         "agent": arguments.get("agent") or "Claude Desktop",
         "covered_through": arguments.get("covered_through") or "",
         "rebaseline": bool(arguments.get("rebaseline")),
         "suggestions": items})
    if result.get("recorded"):
        result["note"] = (f"{result['recorded']} suggestion(s) are waiting in "
                          "Manila's Review tab. Nothing has changed yet: they take "
                          "effect only when accepted there.")
    elif result.get("duplicates"):
        result["note"] = ("Every one of those had already been proposed from the "
                          "same message, so nothing was queued twice.")
        if result.get("batch_id"):
            result["note"] += " How far this run read was recorded."
    elif result.get("batch_id"):
        result["note"] = ("Nothing was queued; how far this run read was "
                          "recorded, and the next run starts from there.")
    return result


ANY = {"description": "A cell value. Text for most columns; for last_touched an "
                      "object {date, via, note}; for resolve_by {date, note} and "
                      "optionally {link: <milestone id>} to borrow a deliverable's "
                      "date. Dates are ISO, YYYY-MM-DD. current_state is one or "
                      "two short sentences -- the state at a glance, not the "
                      "task's details."}

PROJECT_ARG = {"type": "string", "description": "Project id, from manila_projects."}
FOLDER_ARG = {"type": "integer", "description": "Folder id, from manila_folders."}

SUGGESTION = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["update", "create", "archive", "tick"],
                 "description": "update one cell; create a new entry; archive or "
                                "tick an entry a thread has closed out."},
        "entity": {"type": "string",
                   "enum": ["action_item", "milestone", "contact"]},
        "row": {"type": "integer",
                "description": "The entry's id, for update, archive and tick."},
        "field": {"type": "string",
                  "description": "Column key to change, for update. Keys come from "
                                 "manila_folder's `columns`."},
        "value": ANY,
        "fields": {"type": "object",
                   "description": "For create: the new entry's columns, e.g. "
                                  "{\"item\": \"Chase the supplier quote\", "
                                  "\"waiting_on\": \"Sam Rivera\"}. Keep "
                                  "current_state to one or two short sentences."},
        "evidence": {"type": "string",
                     "description": "The mail or chat this rests on: subject, who "
                                    "from, when. Shown to the reviewer first."},
        "reason": {"type": "string",
                   "description": "Why you read it that way, in one line."},
        "ref": {"type": "string",
                "description": "The source message id. Lets a repeated sync drop "
                               "what it already proposed instead of queueing it "
                               "twice. Send one whenever you have one."},
    },
    "required": ["kind", "entity", "evidence"],
}

TOOLS = [
    {
        "name": "manila_projects",
        "description": "List the Manila projects on this machine. Start here when "
                       "you do not already know the project id.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_projects,
    },
    {
        "name": "manila_folders",
        "description": "List a project's folders -- one per workstream "
                       "-- with how many suggestions each has waiting.",
        "inputSchema": {"type": "object",
                        "properties": {"project": PROJECT_ARG},
                        "required": ["project"]},
        "handler": tool_folders,
    },
    {
        "name": "manila_folder",
        "description": "Read one folder whole: every action item, deliverable and "
                       "contact with its current values and id, what each column "
                       "means, which Teams channels, chats, mail folders and mail "
                       "addresses this folder is about, how far each source has "
                       "been read, and which messages each entry was already "
                       "built from. Call this before proposing anything, so "
                       "proposals are made "
                       "against what the folder says now. `sources` is where to "
                       "start reading, not the limit of it: news about this folder "
                       "also arrives internally and from places nobody has listed, "
                       "and a new task is by definition one nothing here names "
                       "yet.",
        "inputSchema": {"type": "object",
                        "properties": {
                            "project": PROJECT_ARG,
                            "folder": FOLDER_ARG,
                            "scope": {"type": "string",
                                      "enum": ["active", "archived"],
                                      "description": "Default active."},
                            "brief": {"type": "boolean",
                                      "description": "Leave out the column "
                                                     "descriptions, which are the "
                                                     "same for every folder and "
                                                     "do not change. Read one "
                                                     "folder in full first, then "
                                                     "ask for the rest brief."}},
                        "required": ["project", "folder"]},
        "handler": tool_folder,
    },
    {
        "name": "manila_due",
        "description": "Everything with a deadline in a window, from every active "
                       "folder in one call: action items by Resolve By, "
                       "deliverables by Date. Use it for reports -- what is due "
                       "today, what is overdue, what is coming up this week -- "
                       "rather than reading folders one by one. Manila works out "
                       "the dates: dates borrowed from a deliverable are read "
                       "through, ticked and archived entries are left out, and "
                       "each entry says how many days until it is due (negative "
                       "when overdue) and whether it is overdue, today, or "
                       "upcoming. Soonest first.",
        "inputSchema": {"type": "object",
                        "properties": {
                            "project": {"type": "string",
                                        "description": "Project id, from "
                                                       "manila_projects. Leave "
                                                       "out to cover every "
                                                       "project."},
                            "days": {"type": "integer", "minimum": 0,
                                     "maximum": 366,
                                     "description": "How far past today to look. "
                                                    "0 is today only (default); "
                                                    "6 is the coming week."},
                            "overdue": {"type": "boolean",
                                        "description": "Include anything whose "
                                                       "date has already passed. "
                                                       "Default true."},
                            "today": {"type": "string",
                                      "description": "Count from this date, "
                                                     "YYYY-MM-DD, instead of "
                                                     "today on the machine "
                                                     "Manila runs on."}}},
        "handler": tool_due,
    },
    {
        "name": "manila_history",
        "description": "Replay one entry: every value each of its cells has held, "
                       "newest first. Worth reading before rewriting a current "
                       "state, so the new one carries on from the last rather "
                       "than repeating it.",
        "inputSchema": {"type": "object",
                        "properties": {
                            "project": PROJECT_ARG,
                            "entity": {"type": "string",
                                       "enum": ["action_item", "milestone", "contact"]},
                            "row": {"type": "integer",
                                    "description": "Entry id, from manila_folder."}},
                        "required": ["project", "entity", "row"]},
        "handler": tool_history,
    },
    {
        "name": "manila_queue",
        "description": "What is already in this folder's review queue, and what "
                       "has been accepted or rejected before. Read it before "
                       "proposing, to avoid queueing the same thing twice and to "
                       "see what kind of suggestion this person actually accepts.",
        "inputSchema": {"type": "object",
                        "properties": {
                            "project": PROJECT_ARG,
                            "folder": FOLDER_ARG,
                            "scope": {"type": "string",
                                      "enum": ["pending", "decided", "all"],
                                      "description": "Default pending."}},
                        "required": ["project", "folder"]},
        "handler": tool_queue,
    },
    {
        "name": "manila_propose",
        "description": "Put suggested changes into a folder's review queue for the "
                       "person to accept, edit or reject. THIS DOES NOT CHANGE "
                       "MANILA. Nothing proposed here takes effect until it is "
                       "accepted by hand in Manila's Review tab. Send one call per "
                       "sync run with everything that run found, and give every "
                       "suggestion its evidence. A batch is refused whole if any "
                       "part of it is malformed, and says which part. A run that "
                       "found nothing new is still sent, with an empty "
                       "`suggestions` and `covered_through`, so its watermark is "
                       "kept.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": PROJECT_ARG,
                "folder": FOLDER_ARG,
                "source": {"type": "string",
                           "description": "Where this run read from: Outlook, "
                                          "Teams, or both."},
                "summary": {"type": "string",
                            "description": "What the run covered, in one line -- "
                                           "e.g. '9 threads since 10-Sep'."},
                "covered_through": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "How far into each source you actually read, "
                                   "keyed by source name as last_runs gives it: "
                                   "{\"Outlook\": \"2026-09-16T23:50:00Z\", "
                                   "\"Teams\": \"2026-09-16T23:48:20Z\"}. Full "
                                   "ISO-8601 timestamps with a timezone; a date "
                                   "alone is refused. Not when you ran -- what "
                                   "you have read everything up to. The next run "
                                   "starts here, so send the oldest point you are "
                                   "confident about for each source, and leave "
                                   "out any source you did not read or cannot "
                                   "vouch for rather than guess. Only the sources "
                                   "named here move, and a point earlier than the "
                                   "one already recorded does not move them back."},
                "rebaseline": {
                    "type": "boolean",
                    "description": "Restart the named sources' watermarks from "
                                   "covered_through, even if that is earlier than "
                                   "what is recorded. Only when the recorded one "
                                   "is known to be wrong."},
                "suggestions": {"type": "array", "items": SUGGESTION,
                                "maxItems": 50,
                                "description": "May be empty when the run found "
                                               "nothing, as long as "
                                               "covered_through is given."},
            },
            "required": ["project", "folder", "source", "suggestions"],
        },
        "handler": tool_propose,
    },
]

BY_NAME = {tool["name"]: tool for tool in TOOLS}
ADVERTISED = [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]


# --- JSON-RPC ----------------------------------------------------------------

def _result(payload):
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, indent=1, default=str)}]}


def _failed(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


class Session:
    def __init__(self, client):
        self.client = client
        self.protocol = PROTOCOL

    def handle(self, message):
        """One request in, one response out -- or None for a notification."""
        method = message.get("method")
        ident = message.get("id")
        if ident is None:
            return None                      # a notification expects no answer

        try:
            result = self.dispatch(method, message.get("params") or {})
        except Down as exc:
            # The server being down is not the model's fault and not a protocol
            # error; it is something the person can fix, so say so as a result.
            return self.reply(ident, _failed(str(exc)))
        except Exception as exc:              # pragma: no cover - belt and braces
            return self.error(ident, -32603, f"{type(exc).__name__}: {exc}")
        if result is None:
            return self.error(ident, -32601, f"unknown method {method!r}")
        return self.reply(ident, result)

    def dispatch(self, method, params):
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.protocol = asked if asked in KNOWN_PROTOCOLS else PROTOCOL
            return {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": NAME, "version": VERSION,
                               "title": "Manila"},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": ADVERTISED}
        if method == "tools/call":
            return self.call(params)
        # prompts and resources are advertised as absent; some clients ask anyway.
        if method in ("prompts/list", "resources/list", "resources/templates/list"):
            return {method.split("/")[0]: []}
        return None

    def call(self, params):
        name = params.get("name")
        tool = BY_NAME.get(name)
        if tool is None:
            return _failed(f"No tool called {name!r}. "
                           f"This server has: {', '.join(BY_NAME)}.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _failed("arguments must be an object")
        try:
            return _result(tool["handler"](self.client, arguments))
        except ValueError as exc:
            # Manila refusing a proposal is information the model can act on --
            # it names the suggestion and what was wrong with it -- so it comes
            # back as a tool result rather than a protocol error.
            return _failed(str(exc))

    @staticmethod
    def reply(ident, result):
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    @staticmethod
    def error(ident, code, message):
        return {"jsonrpc": "2.0", "id": ident,
                "error": {"code": code, "message": message}}


def use_plain_utf8(stream):
    """Make a real stdio stream carry bytes the protocol can survive.

    Two Windows defaults break this transport, both silently, and neither
    shows up on Linux, which is where this was written.

    stdout translates "\n" into "\r\n", so every message this server sends
    carries a stray carriage return inside the framing.

    stdin decodes with the locale encoding -- a legacy code page, not UTF-8.
    What this server writes is escaped to ASCII by json.dumps, so the outgoing
    half survives either way; the incoming half does not. Claude Desktop is an
    Electron app, and JSON.stringify emits raw UTF-8 rather than backslash-u
    escapes -- so a folder named "Ostergaard", or evidence quoting a subject
    line with a real dash in it, arrives as mojibake and the model proposes
    against a folder nobody has.
    """
    try:
        stream.reconfigure(encoding="utf-8", newline="\n", errors="replace")
    except (AttributeError, ValueError):
        pass          # already wrapped, or not a real stream: a test's StringIO
    return stream


def serve(base=DEFAULT_URL, stdin=None, stdout=None):
    """Read requests until the client hangs up.

    One JSON object per line, both ways. Nothing but protocol goes to stdout:
    a stray print here is not a log line, it is a corrupt message.
    """
    stdin = use_plain_utf8(stdin or sys.stdin)
    stdout = use_plain_utf8(stdout or sys.stdout)
    session = Session(Manila(base))

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            answer = Session.error(None, -32700, f"invalid JSON: {exc}")
        else:
            if isinstance(message, list):
                # Batches were dropped from the protocol in 2025-06-18 and no
                # client has ever sent one here, but answering plainly beats
                # failing silently.
                answer = Session.error(None, -32600, "batched requests are not supported")
            else:
                answer = session.handle(message)
        if answer is not None:
            stdout.write(json.dumps(answer) + "\n")
            stdout.flush()


def windows_runner():
    """The interpreter path to write into a Windows config.

    Two things make sys.executable the wrong answer here.

    pythonw.exe is what launches Manila from its Start Menu icon, and it is
    built to have no console -- while this server is nothing but stdin and
    stdout. Its twin is the one to name.

    Store Python -- the one you get from the Microsoft Store -- reports an
    executable deep inside the WindowsApps folder of Program Files, under a
    versioned package name. That path is ACL-locked, and it changes the
    next time Python updates, so a config naming it works until it silently
    does not. The stable way in is the app execution alias the Store installs
    on PATH, which survives updates because that is what it is for.
    """
    runner = sys.executable
    name = os.path.basename(runner)
    if name.lower().startswith("pythonw"):
        runner = os.path.join(os.path.dirname(runner),
                              name.replace("pythonw", "python", 1))

    if "windowsapps" in runner.lower() or "pythonsoftwarefoundation" in runner.lower():
        local = os.environ.get("LOCALAPPDATA")
        if local:
            alias = os.path.join(local, "Microsoft", "WindowsApps", "python.exe")
            if os.path.exists(alias):
                return alias
    return runner


def config_block(base=DEFAULT_URL):
    """The block to paste into claude_desktop_config.json, with real paths."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # pythonw.exe is what launches Manila from its Start Menu icon, and it is
    # the wrong interpreter to name here: it is built to have no console, and
    # this server is nothing but stdin and stdout. Point at its twin.
    runner = windows_runner() if os.name == "nt" else sys.executable
    env = {"PYTHONPATH": root}
    # Naming `env` at all makes Claude Desktop launch the server with close to
    # nothing else in the environment, and Python on Windows will not start
    # without SYSTEMROOT -- it needs it before it can even set up its own
    # imports. The failure is a process that exits instantly with no message,
    # reported by the client as a server that would not start, so the variable
    # is carried through rather than assumed.
    if os.name == "nt":
        for needed in ("SYSTEMROOT", "SystemRoot"):
            if os.environ.get(needed):
                env["SYSTEMROOT"] = os.environ[needed]
                break
    if base != DEFAULT_URL:
        env["MANILA_URL"] = base
    entry = {"command": runner, "args": ["-m", "manila.mcp"], "env": env}
    return json.dumps({"mcpServers": {NAME: entry}}, indent=2)


CONFIG_NAME = "claude_desktop_config.json"


def windows_config_paths():
    """Where a Windows Claude Desktop keeps its config -- both installs of it.

    Installed from the Microsoft Store it is an MSIX package, and a packaged
    app does not get the real Roaming AppData: Windows redirects it into the
    package's own container, several levels down under a family name nobody
    would think to look for. The classic installer uses plain %APPDATA%.

    Both are listed, packaged first, because that is the one that is live when
    both exist. Looking in only the obvious place reports a machine as having
    no config at all while Claude Desktop is reading one happily.
    """
    import glob

    found = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.extend(sorted(glob.glob(os.path.join(
            local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
            CONFIG_NAME))))
    roaming = os.environ.get("APPDATA")
    if roaming:
        found.append(os.path.join(roaming, "Claude", CONFIG_NAME))
    return found


def config_path():
    """The config to act on: the one that exists, else where to create it."""
    if os.name == "nt":
        candidates = windows_config_paths()
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return candidates[-1] if candidates else None
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/" + CONFIG_NAME)
    return None


def handshake():
    """Drive this server the way a client does, in process.

    Separates "the server is broken" from "the client never launched it",
    which from the outside look identical: no tools, no error.
    """
    import io
    out = io.StringIO()
    asked = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL, "capabilities": {}}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    serve(DEFAULT_URL, stdin=io.StringIO("".join(json.dumps(m) + "\n" for m in asked)),
          stdout=out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    return replies[0]["result"], [t["name"] for t in replies[1]["result"]["tools"]]


def check(base=DEFAULT_URL):
    """Say which half is wrong: this server, Manila, or the client's config."""
    ok = True

    try:
        shook, names = handshake()
        print(f"[ok]   the server speaks MCP {shook['protocolVersion']}, "
              f"{len(names)} tools")
    except Exception as exc:
        print(f"[FAIL] the server itself is broken: {type(exc).__name__}: {exc}")
        return False

    try:
        found = Manila(base).get("/projects")["projects"]
        print(f"[ok]   Manila is answering at {base} "
              f"({len(found)} project(s): {', '.join(p['id'] for p in found) or 'none yet'})")
    except Down as exc:
        print(f"[FAIL] {exc}")
        ok = False
    except Exception as exc:
        print(f"[FAIL] Manila answered oddly at {base}: {exc}")
        ok = False

    where = config_path()
    if where is None:
        print("[--]   Claude Desktop does not run on this platform, so its config\n"
              "       is not here to check. Run this on the machine it runs on.")
        return ok
    if not os.path.exists(where):
        print(f"[FAIL] no config at {where}\n"
              "       Claude Desktop writes it the first time you open\n"
              "       Settings -> Developer -> Edit Config.")
        return False
    try:
        with open(where, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {where} cannot be read: {exc}")
        return False

    servers = config.get("mcpServers") or {}
    entry = servers.get(NAME)
    if entry is None:
        listed = ", ".join(servers) or "nothing"
        print(f"[FAIL] {where} lists {listed}, but no {NAME!r} entry.\n"
              f"       Add the block from --mcp-config beside what is there.")
        return False
    print(f"[ok]   {where} has a {NAME!r} entry")

    runner = entry.get("command")
    if not runner or not os.path.exists(runner):
        print(f"[FAIL] its command is {runner!r}, which is not a file on this\n"
              "       machine. A config copied from elsewhere -- WSL, another\n"
              "       PC -- names an interpreter that is not here.")
        ok = False
    root = (entry.get("env") or {}).get("PYTHONPATH")
    if not root or not os.path.exists(os.path.join(root or "", "manila", "mcp.py")):
        print(f"[FAIL] its PYTHONPATH is {root!r}, which does not contain\n"
              "       manila/mcp.py. Manila has moved since this was written.")
        ok = False
    if os.name == "nt" and entry.get("env") and not entry["env"].get("SYSTEMROOT"):
        print("[warn] its env names no SYSTEMROOT. Python on Windows will not\n"
              "       start without it once env is given, and exits with no\n"
              "       message. Re-run --mcp-config to get one that has it.")
        ok = False
    if ok:
        print("\nAll good. If Claude Desktop still shows no Manila tools, quit it\n"
              "from the notification area -- closing the window is not enough --\n"
              "and start it again.")
    return ok


def install(base=DEFAULT_URL, path=None):
    """Add Manila to an MCP config without disturbing anything already in it.

    The block --mcp-config prints is a whole file, which is the right thing for
    a machine with no config and exactly the wrong thing for one with a working
    setup in it: pasted over the top, it takes every other server with it.

    So this reads what is there, changes one key, and writes it back. Every
    other server, and every setting beside mcpServers, is carried through
    untouched. The file is copied to a timestamped backup first, and a file
    that is not valid JSON is refused rather than replaced -- if it cannot be
    read, it certainly cannot be safely rewritten.
    """
    import datetime
    import shutil
    import tempfile

    target = path or config_path()
    if target is None:
        print("There is no Claude Desktop config location on this platform.\n"
              "Pass the path to merge into: --mcp-install <path>")
        return False

    entry = json.loads(config_block(base))["mcpServers"][NAME]
    config, before = {}, []

    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as handle:
                text = handle.read()
            config = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            print(f"[FAIL] {target} is not valid JSON ({exc}).\n"
                  "       Nothing was written. Fix or move it first -- a file\n"
                  "       that cannot be read cannot be safely rewritten.")
            return False
        except OSError as exc:
            print(f"[FAIL] cannot read {target}: {exc}")
            return False
        if not isinstance(config, dict):
            print(f"[FAIL] {target} holds {type(config).__name__}, not an object.")
            return False

        before = sorted((config.get("mcpServers") or {}))
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        # Never write over a backup. Run this twice inside one second and the
        # second copy would otherwise replace the first -- which is the only
        # one holding the config as it was before any of this touched it.
        backup = f"{target}.bak-{stamp}"
        nth = 2
        while os.path.exists(backup):
            backup = f"{target}.bak-{stamp}-{nth}"
            nth += 1
        try:
            shutil.copy2(target, backup)
        except OSError as exc:
            print(f"[FAIL] could not back {target} up: {exc}\n"
                  "       Nothing was written.")
            return False
        print(f"[ok]   backed up to {backup}")
    else:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except OSError as exc:
            print(f"[FAIL] cannot create {os.path.dirname(target)}: {exc}")
            return False
        print(f"[ok]   {target} does not exist yet; creating it")

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f"[FAIL] mcpServers in {target} is not an object. Nothing written.")
        return False
    replaced = NAME in servers
    servers[NAME] = entry

    # Written beside the target and moved into place, so an interrupted write
    # cannot leave a half-file where a working config was.
    folder = os.path.dirname(target) or "."
    handle, temporary = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(config, out, indent=2)
            out.write("\n")
        os.replace(temporary, target)
    except OSError as exc:
        os.unlink(temporary) if os.path.exists(temporary) else None
        print(f"[FAIL] could not write {target}: {exc}")
        return False

    after = sorted(servers)
    print(f"[ok]   {'replaced' if replaced else 'added'} the {NAME!r} entry in {target}")
    if before:
        print(f"       servers before: {', '.join(before)}")
    print(f"       servers now:    {', '.join(after)}")
    kept = [k for k in config if k != "mcpServers"]
    if kept:
        print(f"       left untouched: {', '.join(sorted(kept))}")
    print("\nQuit Claude Desktop from the notification area -- closing the window\n"
          "is not enough -- and start it again.")
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    base = os.environ.get("MANILA_URL") or DEFAULT_URL
    if "--url" in argv:
        at = argv.index("--url")
        try:
            base = argv[at + 1]
        except IndexError:
            raise SystemExit("manila.mcp: --url needs an address")
        del argv[at:at + 2]
    if "--config" in argv:
        print(config_block(base))
        return
    if "--check" in argv:
        raise SystemExit(0 if check(base) else 1)
    if "--install" in argv:
        at = argv.index("--install")
        where = argv[at + 1] if len(argv) > at + 1 else None
        raise SystemExit(0 if install(base, where) else 1)
    if argv:
        raise SystemExit(f"manila.mcp: unexpected argument {argv[0]!r}")
    # Claude Desktop keeps stderr as this server's log; stdout is protocol only.
    print(f"manila-mcp {VERSION}: talking to {base}", file=sys.stderr, flush=True)
    try:
        serve(base)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
