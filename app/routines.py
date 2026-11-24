"""The Routines page (#routines, web/js/routines.js): when Home Assistant's routines run, changed on the fly.

Mike moves his morning routine's time and switches it on and off with his shifts (2026-11-24), so Citadel gives each
routine a time, its weekdays, on/off, "skip the next one" and Run now. The schedule itself lives in Home Assistant on
Probe-1, not here, so a routine still runs at its last time when Citadel (Gaming) is off or asleep.

A routine is a set of Home Assistant entities sharing one name `X` (HomeLab home-assistant/README.md "Routines"):
- `automation.X`: on/off is the routine's on/off; it triggers at the time helper on the listed days and runs the script,
  unless the skip helper is on, in which case it clears it instead (that run is skipped, the next one isn't).
- `input_datetime.X_time` (time only): the time. Its existence is what makes `automation.X` a routine.
- `input_text.X_days`: the weekdays it runs on, as "mon,tue,…".
- `input_boolean.X_skip`: skip the next run.
- `script.X` (optional): the steps, for Run now.

Every change is an ordinary service call on those entities, which a non-admin Home Assistant user may make, so Citadel's
token stays non-admin (Mike, 2026-11-24). Making a new routine or changing its steps is done in Home Assistant by an
admin. Nothing here is cached: the page reads Home Assistant's states (one request) each time it's shown, every 30 s
while it's on screen, and after each change.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from . import homeassistant as ha

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")      # Python's weekday() order
ROUTINE_ID = re.compile(r"^[a-z0-9_]{1,60}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Change(BaseModel):
    """One change from the page. Exactly one field is set."""
    on: bool | None = None
    time: str | None = None
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] | None = None
    skip: bool | None = None
    run: Literal[True] | None = None

    @field_validator("time")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is not None and not HHMM.match(v):
            raise ValueError("time must be HH:MM")
        return v

    def which(self) -> str:
        set_ = [k for k, v in self.model_dump().items() if v is not None]
        if len(set_) != 1:
            raise ValueError("send exactly one change")
        return set_[0]


def _days(text: str | None) -> list[str]:
    got = {d.strip().lower() for d in (text or "").split(",")}
    return [d for d in DAYS if d in got]


def _time(state: dict[str, Any] | None) -> str | None:
    """HH:MM from an input_datetime's state ("07:45:00"), or None when it's unknown."""
    m = re.match(r"^(\d{2}):(\d{2})", (state or {}).get("state") or "")
    return f"{m.group(1)}:{m.group(2)}" if m else None


def next_runs(now: datetime, on: bool, hhmm: str | None, days: list[str], skip: bool) -> tuple[datetime | None, datetime | None]:
    """(next run, the run being skipped). The skipped one is the first due run while skip is on."""
    if not on or not hhmm or not days:
        return None, None
    h, m = map(int, hhmm.split(":"))
    due = []
    for i in range(15):
        d: date = now.date() + timedelta(days=i)
        at = datetime(d.year, d.month, d.day, h, m, tzinfo=now.tzinfo)
        if DAYS[d.weekday()] in days and at > now:
            due.append(at)
            if len(due) == 2:
                break
    if not due:
        return None, None
    if skip:
        return (due[1] if len(due) > 1 else None), due[0]
    return due[0], None


def routines(states: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Every routine found in Home Assistant's states, in name order, shaped for the page."""
    by_id = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
    out = []
    for eid, s in by_id.items():
        if not eid or not eid.startswith("automation."):
            continue
        rid = eid.split(".", 1)[1]
        time_state = by_id.get(f"input_datetime.{rid}_time")
        if not ROUTINE_ID.match(rid) or not time_state:
            continue
        on = s.get("state") == "on"
        hhmm = _time(time_state)
        days_state = by_id.get(f"input_text.{rid}_days")
        days = _days(days_state.get("state")) if days_state else list(DAYS)
        skip_state = by_id.get(f"input_boolean.{rid}_skip")
        skip = bool(skip_state and skip_state.get("state") == "on")
        nxt, skipped = next_runs(now, on, hhmm, days, skip)
        attrs = s.get("attributes") or {}
        out.append({
            "id": rid,
            "name": attrs.get("friendly_name") or rid.replace("_", " ").capitalize(),
            "on": on,
            "available": s.get("state") in ("on", "off"),
            "time": hhmm,
            "days": days,
            "can_days": days_state is not None,
            "skip": skip,
            "can_skip": skip_state is not None,
            "can_run": f"script.{rid}" in by_id,
            "running": (by_id.get(f"script.{rid}") or {}).get("state") == "on",
            "last_run": attrs.get("last_triggered"),
            "next_run": nxt.isoformat() if nxt else None,
            "skipped_run": skipped.isoformat() if skipped else None,
        })
    return sorted(out, key=lambda r: r["name"].lower())


async def view() -> dict[str, Any]:
    if not ha.token():
        return {"configured": False, "available": False, "routines": []}
    states = await ha.request("GET", "/api/states")
    return {"configured": True, "available": True, "routines": routines(states, ha._now())}


async def change(rid: str, ch: Change) -> None:
    """Apply one change to routine `rid`, which must exist (checked against Home Assistant first)."""
    what = ch.which()
    if not ROUTINE_ID.match(rid):
        raise LookupError("no such routine")
    states = await ha.request("GET", "/api/states")
    found = {r["id"]: r for r in routines(states, ha._now())}.get(rid)
    if not found:
        raise LookupError("no such routine")
    if what == "on":
        await ha.service("automation", "turn_on" if ch.on else "turn_off", {"entity_id": f"automation.{rid}"})
    elif what == "time":
        await ha.service("input_datetime", "set_datetime", {"entity_id": f"input_datetime.{rid}_time", "time": f"{ch.time}:00"})
    elif what == "days":
        if not found["can_days"]:
            raise LookupError("this routine has no days setting in Home Assistant")
        await ha.service("input_text", "set_value", {"entity_id": f"input_text.{rid}_days", "value": ",".join(d for d in DAYS if d in ch.days)})
    elif what == "skip":
        if not found["can_skip"]:
            raise LookupError("this routine has no skip setting in Home Assistant")
        await ha.service("input_boolean", "turn_on" if ch.skip else "turn_off", {"entity_id": f"input_boolean.{rid}_skip"})
    elif what == "run":
        if not found["can_run"]:
            raise LookupError("this routine has no script to run")
        await ha.service("script", "turn_on", {"entity_id": f"script.{rid}"})
