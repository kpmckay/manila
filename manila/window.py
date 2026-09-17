"""Opening Manila in a window of its own rather than a browser tab.

A tab is the wrong shape for this. Manila is a thing you keep open beside your
work and come back to all day, and as a tab it slides behind fifteen others,
has no taskbar button, and cannot be reached with alt-tab.

Every Chromium browser will open a plain window with no tab strip and no
address bar, given `--app=URL`. Such a window gets its own taskbar button and
its own place in alt-tab -- which is the whole of what is wanted here -- and
takes its icon from the page's favicon. No dependency, no embedded runtime,
nothing to install: the browser is already there.

If no Chromium is found, a tab is still better than nothing, so the caller
falls back to one.
"""

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

WINDOWS = os.name == "nt"

# Where Windows itself records what an executable name means, and the first
# place to ask -- it is right even when the program was installed somewhere
# unusual, and needs no environment variable to be set.
APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

# Tried in this order. Edge is on every Windows by definition, so it is the
# one that always works; Chrome is first only because someone with both
# installed usually means Chrome.
WINDOWS_BROWSERS = (
    ("chrome.exe", (r"{ProgramFiles}\Google\Chrome\Application\chrome.exe",
                    r"{ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                    r"{LOCALAPPDATA}\Google\Chrome\Application\chrome.exe")),
    ("msedge.exe", (r"{ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
                    r"{ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe")),
    ("brave.exe",  (r"{ProgramFiles}\BraveSoftware\Brave-Browser\Application\brave.exe",
                    r"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\Application\brave.exe")),
    ("vivaldi.exe", (r"{LOCALAPPDATA}\Vivaldi\Application\vivaldi.exe",)),
)

POSIX_BROWSERS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "microsoft-edge", "vivaldi-stable",
)


def _environment():
    """os.environ, with the standard locations filled in where it is silent.

    A process does not always inherit the full Windows environment -- one
    started from WSL, for instance, has no ProgramFiles at all -- and a browser
    that is plainly there should still be found.
    """
    env = dict(os.environ)
    system = env.get("SystemDrive", "C:")
    env.setdefault("ProgramFiles", rf"{system}\Program Files")
    env.setdefault("ProgramFiles(x86)", rf"{system}\Program Files (x86)")
    env.setdefault("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return env


def _from_app_paths(exe):
    """What Windows says this executable name means, or None."""
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"{APP_PATHS}\{exe}") as key:
                recorded, _ = winreg.QueryValueEx(key, None)     # default value
        except OSError:
            continue
        path = Path(str(recorded).strip('"'))
        if path.is_file():
            return str(path)
    return None


def _windows_candidates():
    env = _environment()
    seen = set()

    def once(path):
        key = str(path).lower()
        if key in seen:
            return None
        seen.add(key)
        return path

    for exe, templates in WINDOWS_BROWSERS:
        try:
            recorded = _from_app_paths(exe)
        except (ImportError, OSError):
            recorded = None
        if recorded and once(recorded):
            yield recorded
        for template in templates:
            try:
                path = Path(template.format(**env))
            except (KeyError, IndexError):
                continue            # a template naming something we cannot fill
            if path.is_file() and once(path):
                yield str(path)
        # Last resort: it may simply be on PATH.
        found = shutil.which(exe)
        if found and once(found):
            yield found


def _posix_candidates():
    for name in POSIX_BROWSERS:
        found = shutil.which(name)
        if found:
            yield found


def find_browser():
    """A Chromium-family browser able to open an app window, or None."""
    return next(_windows_candidates() if WINDOWS else _posix_candidates(), None)


def open_window(url, browser=None):
    """Open `url` as its own window. False if there was nothing to open it with.

    The browser is left running on its own -- Manila neither waits for it nor
    holds on to it, because closing the window must not stop the server and
    stopping the server must not close the window.
    """
    executable = browser or find_browser()
    if not executable:
        return False
    try:
        subprocess.Popen(
            [executable, f"--app={url}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Do not let the browser die when Manila does, nor keep a console
            # alive behind it.
            start_new_session=not WINDOWS,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) if WINDOWS else 0,
        )
        return True
    except OSError:
        return False


def open_manila(url, prefer_window=True):
    """Show Manila: its own window if that is possible, otherwise a tab."""
    if prefer_window and open_window(url):
        return "window"
    webbrowser.open(url)
    return "tab"
