"""Home Assistant (Probe-1, ~/Projects/HomeLab/home-assistant): spoken reminders on the Echos.

Citadel talks to Home Assistant's REST API with the non-admin "Citadel" user's token (config.HA_TOKEN_FILE), and the
Echos speak through Home Assistant's Alexa Devices integration (`notify.<room>_announce` entities: a chime, then the
words). The first and so far only use is the bin reminder, replacing Alexa's own "Put out the bins" (Mike, 2026-11-24):
every Echo, 21:55 the evening before a collection, naming the bins from the council schedule, whether or not the strip
reminder has been dismissed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import __version__, config, db
from .maintenance import Job
from .scheduler import parse_hhmm, schedule_of
from .widgets.bins import widget as bins_widget

log = logging.getLogger("hub.homeassistant")

# How each bin is named out loud.
SPOKEN = {"domestic": "the domestic bin", "blue": "the blue recycling bin", "brown": "the brown recycling bin",
          "food": "the food and garden bin"}
TRANSPORT: httpx.AsyncBaseTransport | None = None     # tests swap in a fake; nothing in a test reaches Probe-1


def _now() -> datetime:
    return datetime.now(config.TZ)


def token() -> str | None:
    try:
        return config.HA_TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


class Unavailable(Exception):
    """Home Assistant couldn't be asked: no token, refused token, or not reachable. The message is for Mike to read."""


async def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One call to Home Assistant's REST API with the Citadel token. Never retries (a refused token is logged by Home
    Assistant as a failed login, so a loop would look like an attack)."""
    tok = token()
    if not tok:
        raise Unavailable("No Home Assistant token on this PC (HomeLab home-assistant/set-token.sh)")
    headers = {"Authorization": f"Bearer {tok}", "User-Agent": f"Citadel/{__version__}"}
    try:
        async with httpx.AsyncClient(timeout=15, transport=TRANSPORT) as client:
            resp = await client.request(method, f"{config.HA_URL}{path}", headers=headers, json=body)
    except httpx.HTTPError as e:
        raise Unavailable("Home Assistant isn't answering (Probe-1)") from e
    if resp.status_code in (401, 403):
        raise Unavailable("Home Assistant refused Citadel's token (make a new one with set-token.sh)")
    if resp.status_code >= 400:
        raise Unavailable(f"Home Assistant said {resp.status_code} {resp.reason_phrase}")
    return resp.json() if resp.content else None


async def service(domain: str, action: str, data: dict[str, Any]) -> None:
    await request("POST", f"/api/services/{domain}/{action}", data)


async def announce(message: str, target: str) -> None:
    """Chime and say `message` on the Echo(s) behind a notify entity. Raises on any refusal; never retries."""
    tok = token()
    if not tok:
        raise RuntimeError("no Home Assistant token")
    headers = {"Authorization": f"Bearer {tok}", "User-Agent": f"Citadel/{__version__}"}
    async with httpx.AsyncClient(timeout=15, transport=TRANSPORT) as client:
        resp = await client.post(f"{config.HA_URL}/api/services/notify/send_message", headers=headers,
                                 json={"entity_id": target, "message": message})
    if resp.status_code in (401, 403):
        raise RuntimeError("Home Assistant refused the token (make a new one with set-token.sh)")
    resp.raise_for_status()


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def bins_message(now: datetime) -> str | None:
    """What to say tonight, or None when nothing the bins settings alert on is collected tomorrow."""
    data = db.cache_get(bins_widget.id)["data"] or {}
    streams = db.get_settings(bins_widget.id, bins_widget.defaults).get("alert_streams") or []
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    due = {c["stream"] for c in data.get("collections") or [] if c.get("date") == tomorrow and c.get("stream") in streams}
    # Recycling first (it alternates, so it's the one worth hearing), then domestic, then the rest.
    order = [s for s in ("blue", "brown", "domestic", "food") if s in due]
    return f"Put out {_join([SPOKEN[s] for s in order])} tonight." if order else None


class BinAnnouncementJob(Job):
    id = "ha-bins"
    title = "Bin announcement"
    daily_at = "21:55"
    min_interval = 3600
    timeout = 30
    local = False                # talks to Home Assistant on Probe-1
    defaults = {"target": "notify.everywhere_announce"}
    setup_hint = "Save a Home Assistant token on this PC with ~/Projects/HomeLab/home-assistant/set-token.sh"

    def configured(self, settings: dict[str, Any]) -> bool:
        return token() is not None

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        if not settings.get("_forced"):
            sched = schedule_of(self, db.get_settings("_schedule"))
            hm = parse_hhmm(sched.get("at")) if sched.get("mode") == "daily" else None
            if hm:
                slot = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                # Citadel was off at the time: a run later the same evening still speaks (Mike, 2026-11-24), but one the next
                # day (now before today's time) would be for yesterday's evening, so it says nothing.
                if now < slot:
                    return {"said": None, "skipped": f"missed {sched['at']} yesterday (Citadel wasn't running then)"}
        message = bins_message(now)
        if message:
            await announce(message, settings.get("target") or self.defaults["target"])
        return {"said": message}

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        if d.get("skipped"):
            return d["skipped"]
        return f'said "{d["said"]}"' if d.get("said") else "no bins tomorrow"


bin_job = BinAnnouncementJob()
