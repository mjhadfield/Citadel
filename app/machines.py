"""The other machines on the estate, for Monitoring's Machines row, their reminders and the Stats page's history.

The machines are asked for their stats by the collector on NZXT (collector/citadel_collector.py), not by Citadel: Gaming
is off or asleep too often to keep a history, NZXT is left on. Citadel stays the source of truth for *which* machines:
every sweep it makes sure the collector has its Remote Access devices that have a monitoring SSH user (with the IPv4
address it resolves for each, since NZXT can't resolve `.local` names to IPv4). Gaming has no sshd, so Citadel runs
the stats script here and pushes Gaming's reading itself. Then it reads every machine's latest reading back; the Stats
page's history goes through `history`, so the browser never talks to NZXT. Plan: docs/tasks/stats-collector.md.

The collector being down only loses readings (nothing is buffered): the last good ones stay on the panels, each marked
with the collector's error, and the charts show a gap.

One instance per app lifespan on app.state, like Monitor and Health, never module state.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import sys
import time
from datetime import datetime
from typing import Any

import httpx

from . import alerts as alert_settings, config, procs
from .db import db
from .health import Health
from .monitor import DISK_LOW_BYTES, DISK_LOW_FRACTION

log = logging.getLogger("hub.machines")

INTERVAL = 30.0
# "Running hot": held this long, so a compile or a game loading doesn't raise a reminder.
HOT_FOR = 5 * 60
CPU_HOT, PI_HOT, GPU_HOT = 85.0, 80.0, 85.0     # a Pi 4 starts slowing itself down at 80 °C
DOWN_AFTER = 10 * 60          # same patience as the services' "isn't answering"
# The machine list is sent again this often even when it hasn't changed, so a reinstalled collector gets it back.
RESEND = 10 * 60
THIS_ID, THIS_NAME = "this", "Gaming"
STATS_SCRIPT = config.ROOT / "tools" / "machines" / "citadel-stats"
HISTORY = {"series", "disks", "spikes"}
MACHINE_ID = re.compile(r"^(this|rh\d{1,9})$")
DOWN = "The stats collector on NZXT isn't answering"
NOT_SET_UP = "The stats collector isn't set up (tools/collector_setup.py)"


def _hot(stats: dict[str, Any], is_pi: bool) -> bool:
    cpu = (stats.get("cpu") or {}).get("temp")
    gpu = (stats.get("gpu") or {}).get("temp")
    return (cpu is not None and cpu >= (PI_HOT if is_pi else CPU_HOT)) or (gpu is not None and gpu >= GPU_HOT)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def ipv4(host: str) -> str | None:
    """The IPv4 address this PC resolves a name to (an address is returned as it is), or None."""
    if _is_ip(host):
        return host
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None


class HistoryError(Exception):
    """The collector couldn't answer a history question; the message is for Mike."""


class Machines:
    def __init__(self, health: Health) -> None:
        self.health = health
        # remote_hosts id -> {stats, at, error, hot_since}
        self.state: dict[int, dict[str, Any]] = {}
        self.this: dict[str, Any] = {"stats": None, "at": None, "error": None}     # Gaming, as the collector has it
        self.collector: dict[str, Any] = {"ok": None, "error": None, "at": None}
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._sent: tuple[str, float] | None = None              # (machine list JSON, when) last sent to the collector
        self._addr: dict[str, tuple[str | None, float]] = {}     # host -> (IPv4, when resolved)

    async def start(self) -> None:
        if not config.NO_MACHINES:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0))
            self._task = asyncio.create_task(self._loop(), name="machines")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    async def _loop(self) -> None:
        await asyncio.sleep(5)
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

    @staticmethod
    def _token() -> str | None:
        try:
            return config.COLLECTOR_TOKEN_FILE.read_text().strip() or None
        except OSError:
            return None

    async def _resolve(self, host: str) -> str:
        """IPv4 for the collector, cached for 10 minutes (a `.local` lookup can take a moment)."""
        hit = self._addr.get(host)
        if hit and time.time() - hit[1] < 600:
            return hit[0] or ""
        addr = await asyncio.to_thread(ipv4, host)
        self._addr[host] = (addr, time.time())
        return addr or ""

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        token = self._token()
        if not token or not config.COLLECTOR_URL or self._client is None:
            raise HistoryError(NOT_SET_UP)
        try:
            r = await self._client.request(method, config.COLLECTOR_URL + path, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError:
            raise HistoryError(DOWN) from None
        if r.status_code == 401:
            raise HistoryError("The stats collector turned down Citadel's token: run tools/collector_setup.py again")
        if r.status_code >= 400:
            raise HistoryError(f"The stats collector said: {r.json().get('error', r.status_code) if r.headers.get('content-type', '').startswith('application/json') else r.status_code}")
        return r.json()

    async def _local_stats(self) -> dict[str, Any] | None:
        """Gaming's own reading, from the same script the other Linux machines run."""
        try:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-I", str(STATS_SCRIPT), stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
            return json.loads(out.decode().strip().splitlines()[-1])
        except (OSError, ValueError, IndexError, asyncio.TimeoutError):
            return None

    async def sweep(self) -> None:
        rows = [r for r in self._rows() if r["ssh_user"]]
        keep = {r["id"] for r in rows}
        self.state = {k: v for k, v in self.state.items() if k in keep}
        try:
            await self._send_machines(rows)
            local = await self._local_stats()
            if local is not None:
                await self._call("POST", f"/api/ingest/{THIS_ID}", json=local)
            latest = await self._call("GET", "/api/latest")
        except HistoryError as exc:
            self.collector.update(ok=False, error=str(exc))
            for s in [*self.state.values(), self.this]:
                s["error"] = str(exc)
            return
        self.collector.update(ok=True, error=None, at=int(time.time()))
        by_id = {m["id"]: m for m in latest.get("machines") or []}
        now = time.time()
        for r in rows:
            m = by_id.get(f"rh{r['id']}") or {}
            s = self.state.setdefault(r["id"], {"stats": None, "at": None, "error": None, "hot_since": None})
            s["error"] = m.get("error")
            if m.get("stats") is not None and m.get("at") != s["at"]:
                s["stats"], s["at"] = m["stats"], m["at"]
                hot = _hot(s["stats"], bool(s["stats"].get("pi")))
                s["hot_since"] = (s["hot_since"] or now) if hot else None
            if m.get("error") or self._asleep(r["id"]):
                s["hot_since"] = None
        t = by_id.get(THIS_ID) or {}
        self.this.update(stats=t.get("stats"), at=t.get("at"), error=t.get("error"))

    async def _send_machines(self, rows: list[dict[str, Any]]) -> None:
        items = [{"id": THIS_ID, "name": THIS_NAME, "push": True}]
        for r in rows:
            host = procs.vnc_port(r["address"])[0]
            items.append({"id": f"rh{r['id']}", "name": r["name"], "host": host, "addr": await self._resolve(host), "user": r["ssh_user"]})
        body = json.dumps(items, sort_keys=True)
        if self._sent and self._sent[0] == body and time.time() - self._sent[1] < RESEND:
            return
        await self._call("PUT", "/api/machines", json=items)
        self._sent = (body, time.time())

    def _asleep(self, host_id: int) -> bool:
        h = self.health.state.get(f"remote:{host_id}")
        return bool(h and not h["ok"])

    async def history(self, kind: str, machine: str, frm: int, to: int) -> dict[str, Any]:
        """The Stats page's history, from the collector: `series`, `disks` or `spikes` for one machine."""
        if kind not in HISTORY or not MACHINE_ID.match(machine) or frm >= to:
            raise HistoryError("That isn't a history Citadel can ask for")
        return await self._call("GET", f"/api/{kind}", params={"machine": machine, "from": frm, "to": to})

    def stats_machines(self) -> list[dict[str, Any]]:
        """The Stats page's machine chips: Gaming first, then the monitored Remote Access devices in their order."""
        out = [{"id": THIS_ID, "name": THIS_NAME, "up": True, "at": self.this.get("at"), "stats": self.this.get("stats"), "error": self.this.get("error")}]
        for v in self.view():
            out.append({**v, "id": f"rh{v['id']}"})
        return out

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
