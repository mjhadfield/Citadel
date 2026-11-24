"""Start/stop projects as transient systemd *user* units (`hub-proj-<slug>.service`).

Why systemd rather than subprocess: each project becomes a supervised unit with a proper cgroup (so `stop`
kills the whole process tree, e.g. Vite's children), journald captures its output for the log view, and it
keeps running if the dashboard itself restarts.

Commands are argv lists and are never passed through a shell by us.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from typing import Any

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
started_at: dict[str, float] = {}  # slug -> when we last started it (for the "starting" state)


class ProcError(Exception):
    pass


def unit(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ProcError(f"invalid project slug {slug!r}")
    return f"hub-proj-{slug}.service"


async def _run(*args: str, timeout: float = 20) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProcError(f"{args[0]} timed out")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def active_units() -> set[str]:
    """Names of hub-proj (projects) and hub-vnc (remote desktop viewers) units that are currently running (transient
    units vanish once stopped). One systemctl call for both, since every dashboard refresh asks."""
    rc, out, _ = await _run("systemctl", "--user", "list-units", "hub-proj-*.service", "hub-vnc-*.service", "--all", "--no-legend", "--plain", "--no-pager")
    active: set[str] = set()
    for line in out.splitlines():
        parts = line.split(None, 4)  # UNIT LOAD ACTIVE SUB DESCRIPTION
        if len(parts) >= 4 and parts[2] in ("active", "activating", "reloading"):
            active.add(parts[0])
    return active


async def active_since() -> dict[str, float]:
    """unit name -> epoch seconds it became active, for every hub-proj unit currently active. Reads systemd's own
    record of when the unit started (not our own memory of it), so it's right even across a dashboard restart, and
    for a project someone started outside the dashboard by re-running `hub-proj-<slug>` themselves.

    Uses the *monotonic* timestamp systemd tracks (microseconds since boot) rather than parsing its human-readable
    wall-clock one, which is locale- and timezone-abbreviation-dependent and awkward to parse reliably. Python's own
    `time.monotonic()` reads the same underlying clock (CLOCK_MONOTONIC) on Linux, so the two are directly comparable."""
    rc, out, _ = await _run("systemctl", "--user", "show", "hub-proj-*.service", "--property=Id,ActiveEnterTimestampMonotonic", "--no-pager")
    if rc != 0:
        return {}
    now_mono, now_wall = time.monotonic(), time.time()
    since: dict[str, float] = {}
    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        usec = int(props.get("ActiveEnterTimestampMonotonic") or 0)
        if props.get("Id") and usec:
            since[props["Id"]] = now_wall - (now_mono - usec / 1_000_000)
    return since


async def start(project: dict[str, Any]) -> None:
    slug = project["slug"]
    argv = json.loads(project["start_argv"])
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise ProcError("start command must be a non-empty list of strings")
    cwd = project["cwd"]
    if not os.path.isdir(cwd):
        raise ProcError(f"working directory does not exist: {cwd}")
    exe = argv[0]
    if "/" in exe and not os.path.isabs(exe):
        exe = os.path.normpath(os.path.join(cwd, exe))  # NOT resolve(): a venv's python is a symlink and must stay one
    args = [
        "systemd-run", "--user", f"--unit=hub-proj-{slug}", "--collect", "--quiet",
        f"--description=Citadel: {project['name']}", f"--working-directory={cwd}",
        "--property=TimeoutStopSec=10", "--property=KillMode=control-group",
    ]
    for key, value in json.loads(project.get("env") or "{}").items():
        args += ["--setenv", f"{key}={value}"]
    rc, _, err = await _run(*args, "--", exe, *argv[1:])
    if rc != 0:
        if "already" in err.lower():
            return  # already running under our unit name — treat start as idempotent
        raise ProcError(err.strip().splitlines()[-1] if err.strip() else f"systemd-run exited {rc}")
    started_at[slug] = time.time()


def vnc_unit(host_id: int) -> str:
    return f"hub-vnc-{int(host_id)}.service"


def vnc_port(address: str) -> tuple[str, int]:
    """(host, TCP port) for a vncviewer server string: `host` (5900), `host:N` (display N = 5900+N; N of 100 or more is
    taken as a port, as vncviewer does), `host::port`, and `[IPv6]` with either suffix."""
    host, rest = address, ""
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
    elif "::" in address:
        host, rest = address.split("::", 1)
        rest = "::" + rest
    elif address.count(":") == 1:
        host, rest = address.split(":")
        rest = ":" + rest
    try:
        if rest.startswith("::"):
            return host, int(rest[2:])
        if rest.startswith(":"):
            n = int(rest[1:])
            return host, n if n >= 100 else 5900 + n
    except ValueError:
        pass
    return host, 5900


def viewer_argv(host: dict[str, Any]) -> list[str]:
    """The command for a host's viewer. KRDC takes a vnc:// URL and leaves the port off when it's 5900, because it keys
    each machine's saved settings (scale to window, skip the settings dialog) by that exact URL in ~/.config/krdcrc."""
    if host.get("viewer") == "tigervnc":
        return ["vncviewer", host["address"]]
    name, port = vnc_port(host["address"])
    if ":" in name:
        name = f"[{name}]"
    return ["krdc", f"vnc://{name}" + ("" if port == 5900 else f":{port}")]


async def open_viewer(host: dict[str, Any]) -> bool:
    """Open a VNC viewer window (KRDC or TigerVNC, per host) on this PC's desktop, as a transient unit like a desktop
    project (the user manager carries the session's DISPLAY/WAYLAND_DISPLAY). False if that host's viewer is already
    open. KRDC runs one process per window, so the unit lives as long as the window, as vncviewer's does."""
    args = ["systemd-run", "--user", f"--unit={vnc_unit(host['id'])}", "--collect", "--quiet",
            f"--description=Citadel: remote desktop on {host['name']}", "--property=TimeoutStopSec=5"]
    rc, _, err = await _run(*args, "--", *viewer_argv(host))
    if rc != 0:
        if "already" in err.lower():
            return False
        raise ProcError(err.strip().splitlines()[-1] if err.strip() else f"systemd-run exited {rc}")
    return True


def is_this_pc(address: str | None) -> bool:
    """Whether a request came from this PC itself: loopback, or one of its own addresses (Gaming's wallpaper reaches
    Citadel through the LAN address). An address belongs to this PC exactly when a socket can bind to it."""
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if ip.is_loopback:
        return True
    try:
        with socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((str(ip), 0))
        return True
    except OSError:
        return False


async def open_in_browser(url: str) -> None:
    """Open a link as a tab in Mike's own Brave (its default profile, where his logins are) on this PC's desktop.
    Gaming's wallpaper runs in a profile of its own, so it hands its new-window links here. A transient unit, like a
    viewer: when Brave isn't running yet, the Brave this starts must not live in hub.service's cgroup, where a hub
    restart would close it. When it is running, the launcher passes the link on and exits at once."""
    args = ["systemd-run", "--user", f"--unit=hub-open-{secrets.token_hex(4)}", "--collect", "--quiet",
            "--description=Citadel: open a link in Brave"]
    rc, _, err = await _run(*args, "--", "brave", url)
    if rc != 0:
        raise ProcError(err.strip().splitlines()[-1] if err.strip() else f"systemd-run exited {rc}")


async def close_viewer(host_id: int) -> None:
    rc, _, err = await _run("systemctl", "--user", "stop", vnc_unit(host_id))
    if rc != 0 and "not loaded" not in err.lower():
        raise ProcError(err.strip() or f"systemctl stop exited {rc}")


async def stop(slug: str) -> None:
    rc, _, err = await _run("systemctl", "--user", "stop", unit(slug))
    started_at.pop(slug, None)
    if rc != 0 and "not loaded" not in err.lower():
        raise ProcError(err.strip() or f"systemctl stop exited {rc}")


async def logs(slug: str, lines: int = 200) -> list[dict[str, str]]:
    """Recent journal lines for a project unit (works after the unit has exited, too)."""
    rc, out, _ = await _run(
        "journalctl", "--user", "-u", unit(slug), "-n", str(max(1, min(lines, 1000))),
        "--no-pager", "-o", "json", "--output-fields=MESSAGE,__REALTIME_TIMESTAMP",
    )
    result = []
    for raw in out.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("MESSAGE")
        if isinstance(message, list):  # journald encodes non-UTF-8 output as a byte array
            message = bytes(message).decode(errors="replace")
        ts = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
        result.append({"t": time.strftime("%H:%M:%S", time.localtime(ts)), "msg": str(message or "")})
    return result
