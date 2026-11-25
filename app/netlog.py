"""Every outbound HTTP request Citadel makes, in the journal under "hub.net" (Mike, 2026-11-24).

httpx logs each request at INFO, but main.py keeps its logger quiet because some URLs carry secrets (the Ticketmaster
key in a query string, a Google Calendar secret address in the path). NetLog lets those records through a filter that
rewrites them first: the query string is dropped and what's left goes through logbook.redact().

  * Internet hosts: one line per request, e.g. `GET api.spotify.com/v1/me/player/devices -> 200`.
  * This PC, the LAN and Tailscale (the health checks every 30 s, the stats collector, GoXLR, Home Assistant): counted
    only, or they would bury everything else. Spotify's player polling (GET /v1/me/player, every few seconds while the
    Music page is open) is counted only too, unless it fails.
  * Once an hour, one summary line: calls per host over the hour, busiest first, with how many failed.

What httpx doesn't see isn't here: GoXLR's websocket, pings, Wake-on-LAN packets, mDNS, and the collector's SSH (that
runs on NZXT). A request that never gets a response (a timeout, a refused connection) isn't logged by httpx either;
the caller's own warning covers those.

`journalctl --user -u hub | grep hub.net` shows it all.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections import Counter
from typing import Any

from . import logbook

log = logging.getLogger("hub.net")
SUMMARY_EVERY = 3600.0
SUMMARY_HOSTS = 15
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def is_local(host: str) -> bool:
    """This PC, the LAN or Tailscale: an address in those ranges, or a name that only means something at home."""
    host = host.strip("[]").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".local", ".lan", ".home.arpa", ".ts.net"))
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in TAILSCALE


def _quiet(method: str, host: str, path: str) -> bool:
    return host == "api.spotify.com" and method == "GET" and path == "/v1/me/player"


class _Filter(logging.Filter):
    def __init__(self, owner: NetLog) -> None:
        super().__init__()
        self.owner = owner

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not (str(record.msg).startswith("HTTP Request") and isinstance(args, tuple) and len(args) == 5):
            return record.levelno >= logging.WARNING          # httpx's other INFO chatter stays as quiet as before
        method, url, _version, status, _reason = args
        try:
            host, port, path = url.host, url.port, url.path
        except AttributeError:
            return False
        status = int(status)
        local = is_local(host)
        self.owner.count(f"{host}:{port}" if local and port else host, status >= 400)
        if local or (_quiet(method, host, path) and status < 400):
            return False
        record.name = log.name
        record.msg = "%s %s -> %s"
        record.args = (method, logbook.redact(f"https://{host}{path}").removeprefix("https://"), status)
        return True


class NetLog:
    """Turns httpx's request log on (rewritten by _Filter) while the app runs, and keeps the hourly counts."""

    def __init__(self, every: float = SUMMARY_EVERY) -> None:
        self.every = every
        self._filter = _Filter(self)
        self._lock = threading.Lock()                         # a few calls are made from worker threads
        self._calls: Counter[str] = Counter()
        self._failed: Counter[str] = Counter()
        self._since = time.monotonic()

    def start(self) -> None:
        httpx_log = logging.getLogger("httpx")
        httpx_log.addFilter(self._filter)
        httpx_log.setLevel(logging.INFO)

    def stop(self) -> None:
        httpx_log = logging.getLogger("httpx")
        httpx_log.removeFilter(self._filter)
        httpx_log.setLevel(logging.WARNING)
        self.flush()

    def count(self, host: str, failed: bool) -> None:
        with self._lock:
            self._calls[host] += 1
            if failed:
                self._failed[host] += 1
            due = time.monotonic() - self._since >= self.every
        if due:
            self.flush()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"minutes": round((time.monotonic() - self._since) / 60), "calls": dict(self._calls), "failed": dict(self._failed)}

    def flush(self) -> str | None:
        """Log the summary since the last one (if there were any calls) and start counting again."""
        with self._lock:
            calls, failed, since = self._calls, self._failed, self._since
            self._calls, self._failed, self._since = Counter(), Counter(), time.monotonic()
        if not calls:
            return None
        parts = [f"{h} {n}" + (f" ({failed[h]} failed)" if failed[h] else "") for h, n in calls.most_common(SUMMARY_HOSTS)]
        if len(calls) > SUMMARY_HOSTS:
            parts.append(f"and {len(calls) - SUMMARY_HOSTS} more hosts")
        line = f"Calls in the last {round((time.monotonic() - since) / 60)} min: {sum(calls.values())}. " + ", ".join(parts)
        log.info("%s", line)
        return line
