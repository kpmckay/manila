"""Working out which address to bind to.

Manila has no login. Whatever address it binds to is a door into every project,
so the point of this module is to bind to the *narrowest* address that reaches
the devices you want -- the Tailscale interface itself, never 0.0.0.0, which
would also answer on hotel wifi.
"""

import ipaddress
import os
import shutil
import subprocess

WINDOWS = os.name == "nt"

# Tailscale hands out addresses from the CGNAT block. Anything outside it did
# not come from the tailnet, whatever the command that produced it claimed.
TAILNET = ipaddress.ip_network("100.64.0.0/10")

LOOPBACK = "127.0.0.1"
ANY = "0.0.0.0"


def in_tailnet(address):
    try:
        return ipaddress.ip_address(address) in TAILNET
    except ValueError:
        return False


def _run(*command):
    if not shutil.which(command[0]):
        return ""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def tailscale_ip():
    """This machine's IPv4 tailnet address, or None if it isn't on a tailnet."""
    for line in _run("tailscale", "ip", "-4").split():
        if in_tailnet(line):
            return line
    if WINDOWS:
        # The fallback below reads a Linux interface, and there is no Windows
        # equivalent here yet -- so on Windows the CLI is the only route, and
        # not finding it is the end of the search rather than half of it.
        return None
    # No CLI on PATH (or it failed) but the interface may still be up.
    for word in _run("ip", "-4", "-o", "addr", "show", "tailscale0").split():
        if in_tailnet(word.split("/")[0]):
            return word.split("/")[0]
    return None


def resolve_host(host=None, tailscale=False):
    """Turn --host / --tailscale into an address, explaining any refusal."""
    if tailscale:
        if host and host not in (LOOPBACK, ANY):
            raise ValueError(f"--tailscale and --host {host} disagree; pass just one")
        found = tailscale_ip()
        if not found:
            if WINDOWS:
                raise ValueError(
                    "no Tailscale IPv4 address found. On Windows this needs the "
                    "tailscale CLI on PATH -- it installs to "
                    r'"C:\Program Files\Tailscale" but is not added to PATH. '
                    "Add that folder to PATH, or pass --host with the address "
                    "from the Tailscale tray icon.")
            raise ValueError(
                "no Tailscale IPv4 address found. Is tailscaled running? "
                "Check with: tailscale ip -4")
        return found
    return host or LOOPBACK


def reach(host, port):
    """The URL to type on another device, given what we actually bound to."""
    if host == ANY:
        return f"http://{tailscale_ip() or LOOPBACK}:{port}"
    return f"http://{host}:{port}"
