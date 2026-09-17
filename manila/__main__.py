"""Command line entry point.

    python3 -m manila                       serve every project on one port
    python3 -m manila --tailscale           reach it from your other devices
    python3 -m manila --list                show what's registered
    python3 -m manila --import DIR          open a project folder from another PC
    python3 -m manila --mcp-config          set Claude Desktop up to suggest

Projects are created, renamed, reordered and opened in the browser. The flags
below exist for scripting and for putting a project somewhere specific.
"""

import argparse
import os
import sys

from . import mcp, net, project, server

CONFIG_PATHS = {
    "nt":     "%APPDATA%\\Claude\\claude_desktop_config.json",
    "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
}

# How to say "run this" on the platform doing the reading. Windows has no
# `python3`, and the usage line is the one place people copy from.
INVOCATION = "py -m manila" if os.name == "nt" else "python3 -m manila"


def build_parser():
    parser = argparse.ArgumentParser(
        prog=INVOCATION,
        description="Manila - a simple action item tracking tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="One server, one port, every project. Projects share nothing but\n"
               "the port: separate directories, separate databases, separate\n"
               "history. Pick between them at the entry page.")
    parser.add_argument("-p", "--port", type=int, default=8000,
                        help="port to serve on (default: 8000)")
    parser.add_argument("--host", metavar="ADDR",
                        help="bind address (default: 127.0.0.1, local only)")
    parser.add_argument("--tailscale", action="store_true",
                        help="bind to this machine's Tailscale IP, so your other "
                             "tailnet devices can reach it and nothing else can")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="open Manila once it is listening; if it is already "
                             "running, just open the window")
    parser.add_argument("--tab", action="store_true",
                        help="with --open, use an ordinary browser tab rather "
                             "than a window of Manila's own")
    parser.add_argument("--allow-host", metavar="NAME", action="append", default=[],
                        help="answer to this host name as well. Manila serves "
                             "only the names it knows it is reached by, so that "
                             "a name someone else controls cannot be pointed at "
                             "it; add yours here if you reach it by one of your "
                             "own. Repeatable")

    agent = parser.add_argument_group(
        "suggestions",
        "Let an assistant read your folders and propose changes to them. It\n"
        "cannot make any: everything it proposes waits in Manila's Review tab\n"
        "until you accept, edit or reject it.")
    agent.add_argument("--mcp", action="store_true",
                       help="run the MCP server on stdin/stdout (what Claude "
                            "Desktop launches; not useful to run by hand)")
    agent.add_argument("--mcp-config", action="store_true",
                       help="print the block to paste into "
                            "claude_desktop_config.json, paths filled in")
    agent.add_argument("--mcp-install", nargs="?", const=True, metavar="PATH",
                       help="add Manila to Claude Desktop's config, keeping "
                            "every other server and setting in it; backs the "
                            "file up first. Give PATH for a config elsewhere")
    agent.add_argument("--mcp-check", action="store_true",
                       help="say why Claude Desktop is not seeing Manila: "
                            "checks the server, Manila itself, and the config")
    agent.add_argument("--mcp-url", metavar="URL", default=mcp.DEFAULT_URL,
                       help=f"where the MCP server should find Manila "
                            f"(default: {mcp.DEFAULT_URL})")

    group = parser.add_argument_group("managing projects")
    group.add_argument("--list", action="store_true", help="list registered projects")
    group.add_argument("--add", metavar="NAME", help="create and register a project")
    group.add_argument("--path", metavar="DIR", help="where --add should put it")
    group.add_argument("--import", metavar="DIR", dest="import_dir",
                       help="register a project folder that already exists -- "
                            "one copied from another machine. Its data is read "
                            "where it lies; nothing is moved or created")
    group.add_argument("--name", metavar="NAME",
                       help="what to call an imported project (default: its "
                            "folder's name)")
    group.add_argument("--color", metavar="HEX", help="accent color for --add")
    group.add_argument("--forget", metavar="ID",
                       help="remove a project from the picker, leaving its data alone")
    return parser


def show_list():
    known = project.projects()
    if not known:
        print("No projects yet. Start the server and create one, or:")
        print(f"  {INVOCATION} --add 'ACME Corp'")
        return
    print(f"registry: {project.registry_path()}\n")
    width = max(len(p.id) for p in known)
    for space in known:
        mark = "" if space.db_path.exists() else "   (empty)"
        print(f"  {space.id:<{width}}  {space.name}")
        print(f"  {'':<{width}}  {space.path}{mark}")
    print("\nThe left column is the id: fixed, used in URLs. The name is yours to change.")


def in_wsl():
    """Whether this is WSL -- where Claude Desktop is a Windows app, not ours.

    Manila is developed here and run on Windows Python, so this is the one case
    where the machine printing the config is reliably not the machine that will
    read it.
    """
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as version:
            return "microsoft" in version.read().lower()
    except OSError:
        return False


def show_mcp_config(url):
    """How to introduce Manila to Claude Desktop, on this machine.

    The block names an interpreter and a directory, so it is only good on the
    machine it was printed on. Saying so matters more than it sounds: the paths
    look plausible everywhere, and a Windows Claude Desktop handed a WSL path
    fails at launch with nothing on screen to say why.
    """
    if in_wsl():
        print("This is WSL, and Claude Desktop is a Windows application -- so the\n"
              "paths below would be printed for the wrong machine. Run this from\n"
              "Windows instead, where Manila is actually installed:\n")
        print("    py -m manila --mcp-config\n")
        print("in PowerShell, from your Manila folder (or from\n"
              "%LOCALAPPDATA%\\Programs\\Manila if you installed it with\n"
              "windows\\install.ps1). Paste what it prints into\n"
              f"{CONFIG_PATHS['nt']} and restart Claude Desktop.\n")
        print("For reference, this WSL copy would have said:\n")
        print(mcp.config_block(url))
        return

    where = CONFIG_PATHS.get("nt" if os.name == "nt" else sys.platform)
    if where is None:
        print("Claude Desktop does not run on this platform, so there is nowhere\n"
              "here to put this. It is the right block for the machine that does\n"
              "run it, once the paths point at that machine's Manila:\n")
        print(mcp.config_block(url))
        return

    print(f"Add this to {where}\n"
          "(Claude Desktop: Settings -> Developer -> Edit Config), then restart it:\n")
    print(mcp.config_block(url))
    print("\nThe paths above are this machine's. Manila must be running for the\n"
          "tools to answer. What they can do is read your folders and add to a\n"
          "folder's Review tab -- nothing there changes anything until you\n"
          "accept it.")


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.mcp:
        mcp.main(["--url", args.mcp_url])
        return
    if args.mcp_config:
        show_mcp_config(args.mcp_url)
        return
    if args.mcp_install:
        where = None if args.mcp_install is True else args.mcp_install
        raise SystemExit(0 if mcp.install(args.mcp_url, where) else 1)
    if args.mcp_check:
        raise SystemExit(0 if mcp.check(args.mcp_url) else 1)
    if args.list:
        show_list()
        return
    if args.add:
        created = project.add(args.add, args.path, args.color)
        print(f"created {created.name!r} (id: {created.id}) at {created.path}")
        return
    if args.import_dir:
        try:
            attached = project.attach(args.import_dir, args.name, args.color)
        except ValueError as exc:
            raise SystemExit(f"manila: {exc}")
        print(f"opened {attached.name!r} (id: {attached.id}) at {attached.path}")
        return
    if args.forget:
        try:
            path = project.forget(args.forget)
        except KeyError:
            raise SystemExit(f"manila: no project with id {args.forget!r} "
                             f"(see: {INVOCATION} --list)")
        print(f"forgot {args.forget!r}. Its data is still at {path}")
        return

    try:
        host = net.resolve_host(args.host, args.tailscale)
    except ValueError as exc:
        raise SystemExit(f"manila: {exc}")

    server.serve(host=host, port=args.port, open_browser=args.open_browser,
                 prefer_window=not args.tab, allow_hosts=args.allow_host)


if __name__ == "__main__":
    main()
