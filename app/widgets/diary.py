"""Diary: upcoming events from Google Calendar (API v3) and/or iCal feeds.

Two sources can work side by side:
  - Google Calendar API (app/google_calendar.py): real Google colours, calendar picker, event links,
    and cheap incremental sync via syncToken.
  - Secret iCal addresses: kept for non-Google calendars (or while Google OAuth isn't set up).

Both secret iCal addresses and Google OAuth credentials/tokens stay strictly server-side in the
(0600) database, are registered with logbook, and are never returned to the browser.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import icalendar
import recurring_ical_events

from .. import config, google_calendar as gcal
from .base import Widget

MAX_EVENTS = 500


def _as_local(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=config.TZ)).astimezone(config.TZ)


def expand(content: bytes, cal: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Expand recurring events in [start, end) into flat, JSON-ready dicts. CPU-bound: run in a thread."""
    calendar = icalendar.Calendar.from_ical(content)
    today = start.astimezone(config.TZ).date()
    out: list[dict[str, Any]] = []
    for ev in recurring_ical_events.of(calendar).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = ev["DTSTART"].dt
        dtend = ev["DTEND"].dt if "DTEND" in ev else None
        all_day = not isinstance(dtstart, datetime)
        if all_day:
            first = dtstart
            last = (dtend - timedelta(days=1)) if isinstance(dtend, date) and dtend > dtstart else dtstart
            time_, end_time = None, None
        else:
            local = _as_local(dtstart)
            first, last = local.date(), local.date()
            time_ = local.strftime("%H:%M")
            end_time = _as_local(dtend).strftime("%H:%M") if isinstance(dtend, datetime) else None
        out.append({
            "date": max(first, today).isoformat(),          # an event already under way shows from today
            "until": last.isoformat() if last > first else None,
            "time": time_,
            "end_time": end_time,
            "all_day": all_day,
            "title": str(ev.get("SUMMARY") or "(no title)").strip()[:120],
            "location": str(ev.get("LOCATION") or "").split("\n")[0].strip()[:80],
            "cal": cal.get("name", "Calendar"),
            "cal_id": cal.get("id") or "",
            "colour": cal.get("colour", "violet"),
            "source": "ical",
        })
    return out


class DiaryWidget(Widget):
    id = "diary"
    title = "Calendar"
    interval = 30 * 60           # one small request per calendar; syncToken keeps Google polls tiny (adjustable down to 5 min in Settings)
    min_interval = 5 * 60
    log_success = False
    secret_keys = ("calendars", "google_client_secret")
    defaults = {
        "calendars": [],
        "horizon_days": 93,      # to the end of the full screen view's third month
        "google_client_id": "",
        "google_client_secret": "",
        "google_calendar_ids": [],
        "google_home_calendar_ids": None,   # None = show all synced Google calendars on the Home tile; list = show only those
        "colour_mode": "google",
    }

    def secrets(self, settings: dict[str, Any]) -> list[str]:
        ical = [c["url"] for c in settings.get("calendars", []) if c.get("url")]
        secret = str(settings.get("google_client_secret") or "").strip()
        out = ical + ([secret] if secret else []) + gcal.secret_values()
        return list(dict.fromkeys(out))

    def configured(self, settings: dict[str, Any]) -> bool:
        has_ical = any(c.get("url") for c in settings.get("calendars", []))
        has_google = (gcal.is_connected() and bool(settings.get("google_calendar_ids"))) or gcal.is_expired()
        return has_ical or has_google

    @staticmethod
    def _cid(cal: dict[str, Any], index: int) -> str:
        """A calendar's stable id. Calendars stored before ids existed get a positional one, so they keep working."""
        return cal.get("id") or f"cal{index}"

    def public_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        cals = [
            {"id": self._cid(c, i), "name": c.get("name", ""), "colour": c.get("colour", "violet"),
             "has_url": bool(c.get("url")), "home": bool(c.get("home", True))}
            for i, c in enumerate(settings.get("calendars", []))
        ]
        keep = {k: v for k, v in settings.items() if k not in ("calendars", "google_client_secret")}
        return {
            **keep,
            "calendars": cals,
            "has_google_client_secret": bool(str(settings.get("google_client_secret") or "").strip()),
            "google": gcal.public_status(settings),
        }

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if "calendars" in new:
            if not isinstance(new["calendars"], list) or len(new["calendars"]) > 8:
                raise ValueError("calendars must be a list of at most 8")
            # Match by id, never by name: renaming a calendar must not lose its secret address.
            stored = {self._cid(c, i): c.get("url") for i, c in enumerate(old.get("calendars", []))}
            cals = []
            for c in new["calendars"]:
                url = (c.get("url") or "").strip() or stored.get(c.get("id"))     # omitted url = keep the stored secret
                if not url:
                    raise ValueError("every calendar needs its secret iCal address")
                if not str(url).startswith(("https://", "http://")):
                    raise ValueError("calendar url must be http(s)")
                colour = c.get("colour", "violet")
                if colour not in ("violet", "silver", "indigo"):
                    raise ValueError("colour must be violet, silver or indigo")
                cals.append({"id": c.get("id") or secrets.token_hex(4), "name": str(c.get("name") or "Calendar")[:40],
                             "colour": colour, "url": url, "home": bool(c.get("home", True))})
            merged["calendars"] = cals
        if "horizon_days" in new:
            horizon = new["horizon_days"]
            if not isinstance(horizon, int) or isinstance(horizon, bool) or not 7 <= horizon <= 365:
                raise ValueError("horizon_days must be a whole number from 7 to 365")
        if "google_client_id" in new:
            cid = str(new["google_client_id"] or "").strip()
            if len(cid) > 200:
                raise ValueError("google_client_id is too long")
            merged["google_client_id"] = cid
        if "google_client_secret" in new and new["google_client_secret"]:
            csec = str(new["google_client_secret"]).strip()
            if len(csec) > 200:
                raise ValueError("google_client_secret is too long")
            merged["google_client_secret"] = csec
        if "google_calendar_ids" in new:
            ids = new["google_calendar_ids"]
            if not isinstance(ids, list) or len(ids) > 20 or not all(isinstance(x, str) and x.strip() for x in ids):
                raise ValueError("google_calendar_ids must be a list of up to 20 calendar ids")
            merged["google_calendar_ids"] = list(dict.fromkeys(x.strip() for x in ids))
        if "google_home_calendar_ids" in new:
            hids = new["google_home_calendar_ids"]
            if hids is None:
                merged["google_home_calendar_ids"] = None
            else:
                if not isinstance(hids, list) or len(hids) > 20 or not all(isinstance(x, str) and x.strip() for x in hids):
                    raise ValueError("google_home_calendar_ids must be a list of up to 20 calendar ids")
                merged["google_home_calendar_ids"] = list(dict.fromkeys(x.strip() for x in hids))
        if "colour_mode" in new:
            if new["colour_mode"] not in ("google", "theme"):
                raise ValueError("colour_mode must be 'google' or 'theme'")
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        start = datetime.now(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=int(settings.get("horizon_days", 93)))
        events: list[dict[str, Any]] = []
        legends: list[dict[str, Any]] = []

        # 1. Google Calendar API sources
        google_ids = list(settings.get("google_calendar_ids") or [])
        colour_mode = settings.get("colour_mode") if settings.get("colour_mode") in ("google", "theme") else "google"
        if gcal.is_connected() and google_ids:
            g_events, g_legends = await gcal.fetch_events(start, end, google_ids, colour_mode=colour_mode)
            events += g_events
            legends += g_legends
        elif gcal.is_expired():
            raise gcal.GoogleCalendarAuthError("Google Calendar sign-in has expired — reconnect in Settings → Widgets")

        # 2. iCal sources (kept for non-Google calendars or alongside Google)
        ical_cals = [(self._cid(c, i), c) for i, c in enumerate(settings.get("calendars", [])) if c.get("url")]
        if ical_cals:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                for cid, cal in ical_cals:
                    resp = await client.get(cal["url"])
                    resp.raise_for_status()
                    events += await asyncio.to_thread(expand, resp.content, {**cal, "id": cid}, start, end)
            legends += [{"id": cid, "name": c.get("name", "Calendar"), "colour": c.get("colour", "violet"), "source": "ical"} for cid, c in ical_cals]

        seen, unique = set(), []
        for e in sorted(events, key=lambda e: (e["date"], e["time"] or "", e["title"])):
            key = (e["date"], e["time"], e["title"], e["cal"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return {
            "events": unique[:MAX_EVENTS],
            "calendars": legends,
        }

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        g_expired = gcal.is_expired()
        if not data:
            return {"events": [], "calendars": [], "google_expired": True} if g_expired else {}
        today = now.date().isoformat()
        raw_home = settings.get("google_home_calendar_ids")
        google_home = None if raw_home is None else set(raw_home)
        hidden_ical_ids = {self._cid(c, i) for i, c in enumerate(settings.get("calendars") or []) if not c.get("home", True)}
        hidden_ical_names = {c.get("name", "Calendar") for c in (settings.get("calendars") or []) if not c.get("home", True)}

        def show_on_home(item: dict[str, Any], *, is_cal: bool = False) -> bool:
            src = item.get("source")
            cid = item.get("id") if is_cal else item.get("cal_id")
            if src == "google":
                return google_home is None or (cid in google_home)
            if src == "ical":
                if cid and cid in hidden_ical_ids:
                    return False
                if not cid and (item.get("name") if is_cal else item.get("cal")) in hidden_ical_names:
                    return False
            return True

        raw_events = [e for e in (data.get("events") or []) if (e["until"] or e["date"]) >= today and show_on_home(e)]
        # Deduplicate identical events shared across multiple calendars shown on Home (e.g. Mum + Dad)
        seen_home: set[tuple[str, str | None, str | None, str]] = set()
        events: list[dict[str, Any]] = []
        for e in raw_events:
            key = (e["date"], e.get("until"), None if e.get("all_day") else e.get("time"),
                   " ".join(str(e.get("title") or "").split()).casefold())
            if key in seen_home:
                continue
            seen_home.add(key)
            events.append(e)
        calendars = [c for c in (data.get("calendars") or []) if show_on_home(c, is_cal=True)]
        return {
            **data,
            "events": events,
            "calendars": calendars,
            "google_expired": g_expired,
        }


widget = DiaryWidget()
