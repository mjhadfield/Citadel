"""Status dots: probes each project's port and each link's health URL on a timer.

Projects are probed with a plain TCP connect to 127.0.0.1 (fast, no protocol assumptions), Remote Access devices
with a ping; links with a
`health_url` get an HTTP GET (any response below 500 counts as up). Results are held in memory and pushed
to browsers over SSE only when something changes.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from typing import Any

import httpx

from . import procs
from .bus import Bus
from .db import db

log = logging.getLogger("hub.health")


class Health:
    def __init__(self, bus: Bus, interval: float = 30.0) -> None:
        self.bus = bus
        self.interval = interval
        self.state: dict[str, dict[str, Any]] = {}
        # Remote Access device named by host name (pi-2.local) -> the LAN address it last resolved to, shown on its tile.
        # Kept while the device is off, so the tile doesn't fall back to the name every time a Pi sleeps.
        self.resolved: dict[str, str] = {}
        self._resolved_changed = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:
            while True:
                try:
                    if await self.check_all(client):
                        self.bus.publish("health", {})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("health sweep failed")
                await asyncio.sleep(self.interval)

    @staticmethod
    async def _tcp(port: int) -> tuple[bool, int]:
        t0 = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
            writer.close()
            return True, round((time.perf_counter() - t0) * 1000)
        except (OSError, asyncio.TimeoutError):
            return False, 0

    async def _remote(self, key: str, host: str) -> tuple[bool, int]:
        """A Remote Access device: look its name up (the router hands out addresses, so a Pi's moves), then ping it."""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            try:
                infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM), 3)
            except (OSError, asyncio.TimeoutError):
                return False, 0                            # not answering mDNS: it's off
            ip = infos[0][4][0]
            if self.resolved.get(key) != ip:
                self.resolved[key] = ip
                self._resolved_changed = True
            host = ip
        return await self._ping(host)

    @staticmethod
    async def _ping(host: str) -> tuple[bool, int]:
        """One ICMP echo via the system `ping` (unprivileged on this distro; a .local name resolves through mDNS)."""
        t0 = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec("ping", "-c", "1", "-W", "2", "-q", "--", host,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            ok = await asyncio.wait_for(proc.wait(), 5) == 0
        except (OSError, asyncio.TimeoutError):
            return False, 0
        return ok, round((time.perf_counter() - t0) * 1000) if ok else 0

    @staticmethod
    async def _http(client: httpx.AsyncClient, url: str) -> tuple[bool, int]:
        t0 = time.perf_counter()
        try:
            resp = await client.get(url)
            return resp.status_code < 500, round((time.perf_counter() - t0) * 1000)
        except httpx.HTTPError:
            return False, 0

    def _record(self, key: str, ok: bool, ms: int) -> bool:
        old = self.state.get(key)
        at = int(time.time())
        # `down_since`: when this run of failed probes began (for the "something's down" reminder), None while up.
        down_since = None if ok else (old.get("down_since") if old and not old["ok"] and old.get("down_since") else at)
        self.state[key] = {"ok": ok, "ms": ms, "at": at, "down_since": down_since}
        return old is None or old["ok"] != ok

    async def check_all(self, client: httpx.AsyncClient) -> bool:
        with db() as con:
            projects = con.execute("SELECT slug, port FROM projects WHERE enabled = 1 AND kind = 'web' AND port IS NOT NULL").fetchall()
            links = con.execute("SELECT id, health_url FROM links WHERE health_url IS NOT NULL AND health_url != ''").fetchall()
            remotes = con.execute("SELECT id, address FROM remote_hosts").fetchall()
        jobs: list[tuple[str, Any]] = [(f"project:{p['slug']}", self._tcp(p["port"])) for p in projects]
        jobs += [(f"link:{l['id']}", self._http(client, l["health_url"])) for l in links]
        # Remote Access: is the device on. Pinged, never a connection to its VNC port: a VNC server counts a connection
        # that closes before logging in as a failed login, and blacklists this PC after a few (TigerVNC did, 2026-11-17).
        jobs += [(f"remote:{r['id']}", self._remote(f"remote:{r['id']}", procs.vnc_port(r["address"])[0])) for r in remotes]
        results = await asyncio.gather(*(j[1] for j in jobs))
        current = {key for key, _ in jobs}
        changed = bool(set(self.state) - current)          # something was deleted: browsers should refresh
        self.state = {k: v for k, v in self.state.items() if k in current}
        self.resolved = {k: v for k, v in self.resolved.items() if k in current}
        changed |= self._resolved_changed
        self._resolved_changed = False
        for (key, _), (ok, ms) in zip(jobs, results):
            changed |= self._record(key, ok, ms)
        return changed

    async def sweep_now(self) -> None:
        """Probe everything immediately (after links/projects were edited)."""
        async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:
            if await self.check_all(client):
                self.bus.publish("health", {})

    async def recheck_project(self, slug: str, port: int, expect_up: bool, timeout: float = 45.0) -> None:
        """After a start/stop, poll quickly until the port reflects the change so the UI updates promptly."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ok, ms = await self._tcp(port)
            if self._record(f"project:{slug}", ok, ms) or ok == expect_up:
                self.bus.publish("health", {})
            if ok == expect_up:
                return
            await asyncio.sleep(1)
