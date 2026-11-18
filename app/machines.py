"""The other machines on the estate, for Monitoring's Machines row and their reminders.

Every Remote Access device with an SSH user is asked for its stats every 30 s. Citadel logs in with its own key
(config.MACHINES_KEY), which each machine only accepts with a forced command: all it can do is run the stats script
(tools/machines/citadel-stats, or citadel-stats.ps1 on Windows) and print one JSON object. Nothing keeps running on the
machines between sweeps, and Mike's own key, which can do far more, is never used here.

A machine the health ping already says is off isn't asked (Phoenix and the laptop sleep most of the time; each would
otherwise cost a connect timeout every sweep). Its last reading is kept and shown as asleep.

One instance per app lifespan on app.state, like Monitor and Health, never module state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from . import alerts as alert_settings, config, procs
from .db import db
from .health import Health
from .monitor import DISK_LOW_BYTES, DISK_LOW_FRACTION

log = logging.getLogger("hub.machines")

INTERVAL = 30.0
TIMEOUT = 15.0                # the script samples CPU for 0.5 s; Windows PowerShell takes a few seconds to start
# "Running hot": held this long, so a compile or a game loading doesn't raise a reminder.
HOT_FOR = 5 * 60
CPU_HOT, PI_HOT, GPU_HOT = 85.0, 80.0, 85.0     # a Pi 4 starts slowing itself down at 80 °C
DOWN_AFTER = 10 * 60          # same patience as the services' "isn't answering"


def ssh_argv(user: str, host: str) -> list[str]:
    """`-F /dev/null`: none of Mike's ssh_config (aliases, his IdentityFile) applies. IdentitiesOnly: only Citadel's key is
    offered, never one from an agent. StrictHostKeyChecking=yes against Mike's known_hosts: a reinstalled machine is
    reported, not trusted on sight."""
    return ["ssh", "-F", "/dev/null", "-i", str(config.MACHINES_KEY), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR", "-T", f"{user}@{host}"]


def explain(stderr: str, code: int | None) -> str:
    """What went wrong, for Mike to read on the panel."""
    s = stderr.lower()
    if "remote host identification has changed" in s:
        return "Host key changed: if it was reinstalled, connect once by hand to accept the new key"
    if "host key verification failed" in s or "no matching host key" in s:
        return "Host key not known yet: connect once by hand from this PC"
    if "permission denied" in s:
        return "Not set up for monitoring (run tools/machines_setup.py)"
    if "no such identity" in s or "identity file" in s and "not accessible" in s:
        return "Citadel's monitoring key is missing (run tools/machines_setup.py)"
    if "could not resolve" in s or "name or service not known" in s:
        return "Can't find it on the network"
    if code is None or "timed out" in s or "no route to host" in s or "connection refused" in s:
        return "No answer"
    return "Stats script failed" + (f": {stderr.strip().splitlines()[-1][:120]}" if stderr.strip() else "")


def _hot(stats: dict[str, Any], is_pi: bool) -> bool:
    cpu = (stats.get("cpu") or {}).get("temp")
    gpu = (stats.get("gpu") or {}).get("temp")
    return (cpu is not None and cpu >= (PI_HOT if is_pi else CPU_HOT)) or (gpu is not None and gpu >= GPU_HOT)


class Machines:
    def __init__(self, health: Health) -> None:
        self.health = health
        # remote_hosts id -> {stats, at, error, hot_since}
        self.state: dict[int, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not config.NO_MACHINES:
            self._task = asyncio.create_task(self._loop(), name="machines")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        await asyncio.sleep(5)          # let the first health sweep say which machines are off
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("machine sweep failed")
            await asyncio.sleep(INTERVAL)

    @staticmethod
    def _rows() -> list[dict[str, Any]]:
        with db() as con:
            return [dict(r) for r in con.execute("SELECT id, name, address, ssh_user, always_on FROM remote_hosts ORDER BY sort, id")]

    def _asleep(self, host_id: int) -> bool:
        h = self.health.state.get(f"remote:{host_id}")
        return bool(h and not h["ok"])

    async def sweep(self) -> None:
        rows = [r for r in self._rows() if r["ssh_user"]]
        keep = {r["id"] for r in rows}
        self.state = {k: v for k, v in self.state.items() if k in keep}
        due = [r for r in rows if not self._asleep(r["id"])]
        results = await asyncio.gather(*(self.fetch(r["ssh_user"], procs.vnc_port(r["address"])[0]) for r in due))
        now = time.time()
        for r, (stats, error) in zip(due, results):
            s = self.state.setdefault(r["id"], {"stats": None, "at": None, "error": None, "hot_since": None})
            s["error"] = error
            if stats is not None:
                s["stats"], s["at"] = stats, int(now)
                hot = _hot(stats, bool(stats.get("pi")))
                s["hot_since"] = (s["hot_since"] or now) if hot else None
        for r in rows:
            if self._asleep(r["id"]) and r["id"] in self.state:
                self.state[r["id"]]["hot_since"] = None

    @staticmethod
    async def fetch(user: str, host: str) -> tuple[dict[str, Any] | None, str | None]:
        """(stats, None) or (None, what went wrong). The forced command ignores anything sent, so nothing is."""
        try:
            proc = await asyncio.create_subprocess_exec(*ssh_argv(user, host), stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            return None, f"Couldn't run ssh: {exc}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None, explain("", None)
        if proc.returncode != 0:
            return None, explain(err.decode(errors="replace"), proc.returncode)
        try:
            stats = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None, "Stats script printed something unexpected"
        if not isinstance(stats, dict) or stats.get("v") != 1:
            return None, "Stats script is a different version: run tools/machines_setup.py again"
        return stats, None

    def view(self) -> list[dict[str, Any]]:
        """Every monitored machine in Remote Access order: on/asleep from the ping, the last stats and when they came."""
        out = []
        for r in self._rows():
            if not r["ssh_user"]:
                continue
            s = self.state.get(r["id"], {})
            h = self.health.state.get(f"remote:{r['id']}")
            out.append({"id": r["id"], "name": r["name"], "up": h["ok"] if h else None, "always_on": bool(r["always_on"]),
                        "stats": s.get("stats"), "at": s.get("at"), "error": s.get("error")})
        return out

    def alerts(self, at: datetime) -> list[dict[str, Any]]:
        """Reminders: a disk nearly full, running hot, an always-on machine off, Nasferatu's RAID not healthy."""
        if not alert_settings.settings().get("machines", True):
            return []
        t, day, out = at.timestamp(), at.date(), []
        for r in self._rows():
            name, h = r["name"], self.health.state.get(f"remote:{r['id']}")
            if r["always_on"] and h and not h["ok"] and h.get("down_since") and t - h["down_since"] >= DOWN_AFTER:
                since = datetime.fromtimestamp(h["down_since"], config.TZ)
                out.append({"kind": "machine", "key": f"mdown:{r['id']}:{h['down_since']}", "icon": "alert", "title": f"{name} isn't answering",
                            "detail": f"off since {since:%H:%M}" if since.date() == day else f"off since {since:%-d %b}", "chip": "Off", "level": "soon"})
            s = self.state.get(r["id"])
            stats = s and s.get("stats")
            if not stats or not r["ssh_user"]:
                continue
            for d in stats.get("disks") or []:
                if d.get("total") and (d["free"] < DISK_LOW_BYTES or d["free"] / d["total"] < DISK_LOW_FRACTION):
                    out.append({"kind": "machine", "key": f"mdisk:{r['id']}:{d['mount']}:{day}", "icon": "alert",
                                "title": f"{name}: {d['mount']} is nearly full",
                                "detail": f"{d['free'] / 1024**3:,.0f} GB free of {d['total'] / 1024**3:,.0f} GB", "chip": "Disk", "level": "soon"})
            raid = stats.get("raid")
            if raid and raid.get("health") not in (None, "Healthy"):
                out.append({"kind": "machine", "key": f"mraid:{r['id']}:{day}", "icon": "alert", "title": f"{name}: the RAID isn't healthy",
                            "detail": f"{raid.get('health')} · {raid.get('status') or ''}".strip(" ·"), "chip": "RAID", "level": "now"})
            if s.get("hot_since") and t - s["hot_since"] >= HOT_FOR:
                cpu, gpu = (stats.get("cpu") or {}).get("temp"), (stats.get("gpu") or {}).get("temp")
                parts = [f"CPU {cpu:.0f}°C" if cpu is not None else "", f"GPU {gpu:.0f}°C" if gpu is not None else ""]
                out.append({"kind": "machine", "key": f"mhot:{r['id']}:{day}", "icon": "alert", "title": f"{name} is running hot",
                            "detail": " · ".join(p for p in parts if p), "chip": "Hot", "level": "now"})
        return out
