"""What the Start Menu shortcut runs.

Started by `pythonw.exe`, so Manila has no console window -- it behaves like an
installed program rather than a terminal you must leave open. Two things follow
from having no console, and both are handled here:

  * There is nowhere for an error to go, and an icon that silently does nothing
    is the worst way to fail, so anything fatal goes in a message box.

  * There is nothing to close to stop it. Closing the browser tab does not stop
    a server, and never has for any local web app -- so Manila puts an icon in
    the notification area and is stopped from there.

Launching it a second time opens the window you meant rather than complaining
about the port; that is handled in `server.create`.
"""

import ctypes
import os
import sys
import threading
import traceback
from pathlib import Path

# The shortcut sets its own working directory, but a .pyw opened any other way
# should still find the package sitting beside this folder.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MB_ICONERROR = 0x10


def alert(text, title="Manila"):
    ctypes.windll.user32.MessageBoxW(None, str(text), title, MB_ICONERROR)


def leave():
    """Exit now, whatever else in the process is still alive.

    Under pythonw there is no console, so sys.stdout and sys.stderr are None
    rather than streams -- flushing them without checking raises, and raising
    here would be caught as a tray failure and reported as one, which is a lie
    about what happened.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    os._exit(0)


def main():
    import argparse

    from manila import net, server, window

    parser = argparse.ArgumentParser(prog="Manila")
    parser.add_argument("-p", "--port", type=int, default=8000)
    parser.add_argument("--host", default=net.LOOPBACK)
    parser.add_argument("--no-tray", action="store_true",
                        help="serve without the notification-area icon")
    parser.add_argument("--tab", action="store_true",
                        help="open in a browser tab rather than its own window")
    args = parser.parse_args()

    url = net.reach(args.host, args.port)

    def show():
        """Put Manila in front of you: raise its window, or make one."""
        try:
            from tray import focus_window
            if not args.tab and focus_window("Manila"):
                return
        except Exception:
            pass            # no tray module, or nothing to raise -- just open
        window.open_manila(url, prefer_window=not args.tab)

    httpd = server.create(args.host, args.port)
    if httpd is None:
        # Already running. Open the window that exists and get out of the way,
        # so a second icon never appears for a server this process does not own.
        show()
        return

    serving = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving.start()
    show()

    if args.no_tray:
        serving.join()
        return

    try:
        from tray import Tray
        icon = Tray(title=f"Manila - {url}",
                    icon_path=HERE / "manila.ico",
                    on_open=show,
                    # Not httpd.shutdown: that ends the accept loop but keeps
                    # the port, and a port held by a server that has stopped
                    # answering is how the next start fails for no reason.
                    on_stop=lambda: server.stop(httpd))
        icon.run(hello=("Manila is running",
                        "Right-click this icon to stop it. Closing the browser "
                        "tab leaves it running."))
    except Exception:
        # A missing icon or a refused window class must not take the server
        # with it -- Manila is still perfectly usable, just harder to stop.
        alert("Manila is running, but its notification-area icon could not be "
              "created, so there is nothing to stop it from.\n\n"
              f"It is serving at {url} and will stop when you sign out.\n\n"
              + traceback.format_exc())
        serving.join()
        return

    # Stop means stop. The icon was the only control Manila had and it is gone
    # now, so anything still alive at this point could no longer be stopped by
    # any means the app offers -- which is the exact state the icon exists to
    # prevent. Leaving is not conditional on every thread agreeing to end.
    # Outside the try, so a fault here is never reported as a tray failure.
    leave()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        # argparse and our own startup refusals both leave by this door. Code 0
        # and None are ordinary exits and say nothing.
        if exc.code not in (0, None):
            alert(exc.code)
    except Exception:
        alert(traceback.format_exc())
