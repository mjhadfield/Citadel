"""Unified Schedule aggregator (#schedule): every dated event Citadel knows about in one view.

Zero network calls in the request path — reads Citadel's own cached widgets/tables plus optional
strictly read-only SQLite reads (`mode=ro`) of Home Improvements (house.sqlite) and Poker Companion
(poker.sqlite). If either external database is absent, that layer is simply marked unavailable.

F1 sessions are strictly spoiler-free (round name, session name, and UK start time only).
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, content, db, google_calendar as gcal
from .widgets import bins as bins_w, diary as diary_w, radar as radar_w, releases as releases_w

log = logging.getLogger("hub.schedule")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_SPAN_DAYS = 400

# Non-calendar layers in toolbar order (each synced calendar appears before these as its own layer).
OTHER_LAYERS: tuple[dict[str, Any], ...] = (
    {"id": "gigs", "label": "Gigs", "icon": "bolt"},
    {"id": "onsale", "label": "On sale", "icon": "radar"},
    {"id": "releases", "label": "Releases", "icon": "star"},
    {"id": "vinyl", "label": "Vinyl", "icon": "disc"},
    {"id": "f1", "label": "F1", "icon": "bolt"},
    {"id": "bins", "label": "Bins", "icon": "bin"},
    {"id": "house", "label": "House", "icon": "tool"},
    {"id": "poker", "label": "Poker", "icon": "spade"},
)


def _cal_layer_id(raw_key: str) -> str:
    """Stable, email-free layer id for one calendar (so personal addresses never appear in DOM or localStorage)."""
    return "cal:" + hashlib.sha1(raw_key.encode()).hexdigest()[:8]


def _in_range(first: str, last: str | None, start_s: str, end_s: str) -> bool:
    return bool(first) and first <= end_s and (last or first) >= start_s


def _safe_url(u: Any) -> str | None:
    s = str(u or "").strip()
    return s if s.startswith(("https://", "http://")) else None


def _diary_events(start_s: str, end_s: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Return (events, per-calendar source descriptors, configured). Each synced calendar gets its own layer."""
    settings = db.get_settings("diary", diary_w.widget.defaults)
    configured = diary_w.widget.configured(settings)
    cached = db.cache_get("diary")["data"] or {}
    cached_cals = cached.get("calendars") or []

    # Build ordered calendar map (keyed by stable id when available, else name) so even 0-event calendars show a chip.
    cal_order: list[str] = []
    cal_info: dict[str, dict[str, Any]] = {}
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}

    def register_cal(cid: str, name: str, colour: str, source: str) -> str:
        raw_key = f"{source}:{cid}" if cid else f"{source}:{name}"
        lid = _cal_layer_id(raw_key)
        if lid not in cal_info:
            cal_order.append(lid)
            cal_info[lid] = {
                "id": lid,
                "kind": "diary",
                "label": name or "Calendar",
                "icon": "cal",
                "colour": colour or "violet",
                "available": True,
                "count": 0,
            }
        if cid:
            by_id[cid] = lid
        if name and name not in by_name:
            by_name[name] = lid
        return lid

    for c in cached_cals:
        register_cal(str(c.get("id") or ""), str(c.get("name") or "Calendar"), str(c.get("colour") or "violet"), str(c.get("source") or "ical"))

    out: list[dict[str, Any]] = []
    # Deduplicate identical events entered on multiple calendars (e.g. Mum + Dad sharing "Food delivery")
    # into one event that belongs to all of their calendar layers so toggling or hovering either calendar works.
    merged_by_key: dict[tuple[str, str | None, str | None, str], dict[str, Any]] = {}
    for idx, e in enumerate(cached.get("events") or []):
        d = str(e.get("date") or "")
        until = e.get("until")
        if not DATE_RE.match(d) or not _in_range(d, until, start_s, end_s):
            continue
        cid = str(e.get("cal_id") or "")
        cname = str(e.get("cal") or "Calendar")
        src = str(e.get("source") or "ical")
        lid = (by_id.get(cid) if cid else None) or by_name.get(cname) or register_cal(cid, cname, str(e.get("colour") or "violet"), src)
        cal_info[lid]["count"] += 1

        clean_until = until if until and DATE_RE.match(str(until)) and str(until) > d else None
        clean_time = None if e.get("all_day") else e.get("time")
        title = str(e.get("title") or "(no title)")
        dedupe_key = (d, clean_until, clean_time, re.sub(r"\s+", " ", title).strip().casefold())
        existing = merged_by_key.get(dedupe_key)
        if existing is not None:
            if lid not in existing["layers"]:
                existing["layers"].append(lid)
            if cname not in existing["meta"]["cals"]:
                existing["meta"]["cals"].append(cname)
                existing["meta"]["cal_colours"].append(str(e.get("colour") or "violet"))
            if not existing["location"] and e.get("location"):
                existing["location"] = str(e.get("location"))
            if not existing["detail"] and e.get("description"):
                existing["detail"] = str(e.get("description"))
            if not existing["href"] and _safe_url(e.get("href")):
                existing["href"] = _safe_url(e.get("href"))
            continue

        ev_obj = {
            "id": f"diary:{e.get('id') or idx}:{d}:{e.get('time') or ''}",
            "layer": lid,
            "layers": [lid],
            "kind": "diary",
            "date": d,
            "until": clean_until,
            "time": clean_time,
            "end_time": None if e.get("all_day") else e.get("end_time"),
            "all_day": bool(e.get("all_day")),
            "title": title,
            "subtitle": "",
            "location": str(e.get("location") or ""),
            "detail": str(e.get("description") or ""),
            "colour": str(e.get("colour") or "violet"),
            "href": _safe_url(e.get("href")),
            "meta": {"cal": cname, "cals": [cname], "cal_colours": [str(e.get("colour") or "violet")],
                     "status": e.get("status") or "confirmed", "source": src},
        }
        merged_by_key[dedupe_key] = ev_obj
        out.append(ev_obj)

    sources = [cal_info[lid] for lid in cal_order]
    if not sources:
        sources = [{"id": "diary", "kind": "diary", "label": "Calendar", "icon": "cal", "available": configured, "count": 0}]
    return out, sources, configured


def _gig_events(start_s: str, end_s: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in content.list_gigs():
        d = str(g.get("date") or "")
        if not DATE_RE.match(d) or not _in_range(d, None, start_s, end_s):
            continue
        where = ", ".join(x for x in (g.get("venue"), g.get("city")) if x)
        doors = g.get("doors") or None
        out.append({
            "id": f"gigs:{g['id']}",
            "layer": "gigs",
            "date": d,
            "until": None,
            "time": doors,
            "end_time": None,
            "all_day": not bool(doors),
            "title": str(g["artist"]),
            "subtitle": f"Doors {doors}" if doors else "Booked gig",
            "location": where,
            "detail": str(g.get("notes") or ""),
            "colour": None,
            "href": _safe_url(g.get("ticket_url")),
            "meta": {
                "gig": {
                    "id": g["id"], "artist": g["artist"], "venue": g.get("venue") or "", "city": g.get("city") or "",
                    "date": d, "doors": doors or "", "ticket_url": _safe_url(g.get("ticket_url")) or "", "notes": g.get("notes") or "",
                }
            },
        })
    return out


def _fmt_plain_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d:%a} {d.day} {d:%b} {d.year}"
    except ValueError:
        return iso


def _onsale_events(start_s: str, end_s: str) -> list[dict[str, Any]]:
    """Ticket on-sales & presales from Gig Radar, grouped to one entry per gig per day for presales
    (so Artist + O2 Priority + Co-op presales on the same day never clutter the calendar)."""
    out: list[dict[str, Any]] = []
    items = content.list_items("radar", oldest_first=True, keep=lambda i: not radar_w.is_premium_package(i["title"])).get("items") or []
    # Dedupe across identical (artist, venue, gig_date, kind, sale_date) in case Ticketmaster has multiple listings.
    seen_general: set[tuple[str, str, str, str]] = set()
    presale_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for item in items:
        meta = item.get("meta") or {}
        if meta.get("tribute"):
            continue
        artist = str(item.get("artist") or "")
        where = str(meta.get("venue") or item.get("subtitle") or "")
        gig_date = str(item.get("date") or "")
        sub = f"Show on {_fmt_plain_date(gig_date)}" if gig_date else ""
        href = _safe_url(item.get("url"))

        # 1. General public on-sale
        if meta.get("onsale"):
            try:
                when = datetime.fromisoformat(str(meta["onsale"]).replace("Z", "+00:00")).astimezone(config.TZ)
            except (ValueError, AttributeError):
                when = None
            if when is not None:
                d = when.date().isoformat()
                gkey = (content.norm(artist), content.norm(where), gig_date, d)
                if _in_range(d, None, start_s, end_s) and gkey not in seen_general:
                    seen_general.add(gkey)
                    out.append({
                        "id": f"onsale:{item['id']}:public:{d}",
                        "layer": "onsale",
                        "date": d,
                        "until": None,
                        "time": when.strftime("%H:%M"),
                        "end_time": None,
                        "all_day": False,
                        "title": f"{artist} — Tickets on sale",
                        "subtitle": sub or "General sale",
                        "location": where,
                        "detail": "",
                        "colour": None,
                        "href": href,
                        "meta": {"gig_date": gig_date, "kind": "public"},
                    })

        # 2. Presales — collect per (artist, venue, gig_date, sale_date)
        for p in meta.get("presales") or []:
            if not p.get("at"):
                continue
            try:
                when = datetime.fromisoformat(str(p["at"]).replace("Z", "+00:00")).astimezone(config.TZ)
            except (ValueError, AttributeError):
                continue
            d = when.date().isoformat()
            if not _in_range(d, None, start_s, end_s):
                continue
            pkey = (content.norm(artist), content.norm(where), gig_date, d)
            grp = presale_groups.setdefault(pkey, {
                "id": f"onsale:{item['id']}:presales:{d}",
                "artist": artist, "where": where, "gig_date": gig_date, "sub": sub,
                "date": d, "href": href, "entries": [],
            })
            name = str(p.get("name") or "Presale").strip()
            t_str = when.strftime("%H:%M")
            if (t_str, name.casefold()) not in {(e["time"], e["name"].casefold()) for e in grp["entries"]}:
                grp["entries"].append({"time": t_str, "name": name})

    for grp in presale_groups.values():
        entries = sorted(grp["entries"], key=lambda e: (e["time"], e["name"].lower()))
        first_time = entries[0]["time"]
        if len(entries) == 1:
            title = f"{grp['artist']} — {entries[0]['name']}"
            detail = ""
            meta_presales = None
        else:
            same_time = len({e["time"] for e in entries}) == 1
            title = f"{grp['artist']} — {len(entries)} presales"
            detail = " · ".join(e["name"] if same_time else f"{e['time']} {e['name']}" for e in entries)
            meta_presales = entries
        out.append({
            "id": grp["id"],
            "layer": "onsale",
            "date": grp["date"],
            "until": None,
            "time": first_time,
            "end_time": None,
            "all_day": False,
            "title": title,
            "subtitle": grp["sub"] or "Presale",
            "location": grp["where"],
            "detail": detail,
            "colour": None,
            "href": grp["href"],
            "meta": {"gig_date": grp["gig_date"], "kind": "presale", **({"presales": meta_presales} if meta_presales else {})},
        })
    return out


def _release_events(start_s: str, end_s: str) -> list[dict[str, Any]]:
    settings = db.get_settings("releases", releases_w.widget.defaults)
    types = settings.get("types") or ["Album"]
    include_extras = bool(settings.get("include_extras", False))

    def keep(i: dict[str, Any]) -> bool:
        d = i.get("date") or ""
        if not DATE_RE.match(d) or not _in_range(d, None, start_s, end_s):
            return False
        m = i.get("meta") or {}
        return releases_w.wanted(m.get("type"), m.get("secondary") or [], types, include_extras)

    items = content.list_items("release", keep=keep, oldest_first=True).get("items") or []
    return [{
        "id": f"releases:{r['id']}",
        "layer": "releases",
        "date": r["date"],
        "until": None,
        "time": None,
        "end_time": None,
        "all_day": True,
        "title": f"{r['artist']} — {r['title']}" if r.get("artist") else str(r["title"]),
        "subtitle": str(r.get("subtitle") or "Release"),
        "location": "",
        "detail": "",
        "colour": None,
        "href": _safe_url(r.get("url")),
        "meta": {"artist": r.get("artist") or "", "album": r.get("title") or ""},
    } for r in items]


def _vinyl_events(start_s: str, end_s: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in content.list_purchased("vinyl"):
        d = str(p.get("date") or "")
        if not DATE_RE.match(d) or not _in_range(d, None, start_s, end_s):
            continue
        meta = p.get("meta") or {}
        sub = " · ".join(x for x in (meta.get("variant") or meta.get("format") or "Vinyl pre-order", p.get("subtitle") or meta.get("shop") or "") if x)
        out.append({
            "id": f"vinyl:{p['id']}",
            "layer": "vinyl",
            "date": d,
            "until": None,
            "time": None,
            "end_time": None,
            "all_day": True,
            "title": f"{p['artist']} — {p['title']}" if p.get("artist") else str(p["title"]),
            "subtitle": sub,
            "location": "",
            "detail": "",
            "colour": None,
            "href": _safe_url(p.get("url")),
            "meta": {"shop": meta.get("shop") or ""},
        })
    return out


def _f1_events(start_s: str, end_s: str) -> tuple[list[dict[str, Any]], bool]:
    cal = db.cache_get("f1cal")["data"] or {}
    rounds = cal.get("rounds") or []
    out: list[dict[str, Any]] = []
    for rnd in rounds:
        venues = ", ".join(rnd.get("venues") or [])
        for sess in rnd.get("sessions") or []:
            at = sess.get("at")
            if not at:
                continue
            dt = datetime.fromtimestamp(int(at), config.TZ)
            d = dt.date().isoformat()
            if not _in_range(d, None, start_s, end_s):
                continue
            out.append({
                "id": f"f1:{cal.get('season')}:{rnd['round']}:{sess['key']}",
                "layer": "f1",
                "date": d,
                "until": None,
                "time": dt.strftime("%H:%M"),
                "end_time": None,
                "all_day": False,
                "title": f"{rnd['short']} — {sess['label']}",
                "subtitle": str(rnd.get("name") or ""),
                "location": venues,
                "detail": f"Round {rnd['round']}",
                "colour": None,
                "href": None,
                "meta": {"round": rnd["round"], "session": sess["key"]},
            })
    return out, bool(rounds)


def _bin_events(start_s: str, end_s: str) -> tuple[list[dict[str, Any]], bool]:
    settings = db.get_settings("bins", bins_w.widget.defaults)
    configured = bins_w.widget.configured(settings)
    cached = db.cache_get("bins")["data"] or {}
    by_date: dict[str, list[str]] = {}
    for c in cached.get("collections") or []:
        d = str(c.get("date") or "")
        st = str(c.get("stream") or "")
        if DATE_RE.match(d) and _in_range(d, None, start_s, end_s) and st in bins_w.STREAMS:
            by_date.setdefault(d, [])
            if st not in by_date[d]:
                by_date[d].append(st)

    order = ("domestic", "blue", "brown", "food")
    out: list[dict[str, Any]] = []
    for d, raw_streams in sorted(by_date.items()):
        streams = sorted(raw_streams, key=lambda s: order.index(s) if s in order else 99)
        stream_objs = [{"id": s, "title": bins_w.STREAMS[s]["title"], "sub": bins_w.STREAMS[s]["sub"], "colour": bins_w.STREAMS[s]["colour"]} for s in streams]
        primary = next((s for s in streams if s != "food"), streams[0])
        names = [bins_w.STREAMS[s]["title"].replace("Recycling · ", "") for s in streams]
        out.append({
            "id": f"bins:{d}",
            "layer": "bins",
            "date": d,
            "until": None,
            "time": None,
            "end_time": None,
            "all_day": True,
            "title": " + ".join(names) + " bin" + ("s" if len(names) > 1 else ""),
            "subtitle": "Put out the evening before",
            "location": "",
            "detail": " · ".join(f"{o['title']} ({o['sub']})" for o in stream_objs),
            "colour": bins_w.STREAMS[primary]["colour"],
            "href": None,
            "meta": {"streams": stream_objs},
        })
    return out, configured


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error as exc:
        log.warning("Couldn't open %s read-only: %s", path.name, exc)
        return None


def _house_events(start_s: str, end_s: str) -> tuple[list[dict[str, Any]], bool]:
    con = _open_ro(config.HOUSE_DB)
    if con is None:
        return [], False
    out: list[dict[str, Any]] = []
    try:
        rows = con.execute(
            """SELECT d.id, d.due, d.what, j.title AS job_title, j.status AS job_status
               FROM deadlines d LEFT JOIN jobs j ON j.id = d.job_id
               WHERE d.done = 0 AND d.due >= ? AND d.due <= ?
               ORDER BY d.due, d.id""",
            (start_s, end_s),
        ).fetchall()
        for r in rows:
            due = str(r["due"] or "")
            if not DATE_RE.match(due):
                continue
            out.append({
                "id": f"house:deadline:{r['id']}",
                "layer": "house",
                "date": due,
                "until": None,
                "time": None,
                "end_time": None,
                "all_day": True,
                "title": str(r["what"]),
                "subtitle": str(r["job_title"] or "House deadline"),
                "location": "",
                "detail": f"Job status: {r['job_status']}" if r["job_status"] else "",
                "colour": None,
                "href": None,
                "meta": {"kind": "deadline"},
            })

        rec = con.execute(
            """SELECT id, name, amount, months, next_due, notes
               FROM recurring
               WHERE next_due IS NOT NULL AND next_due >= ? AND next_due <= ?
               ORDER BY next_due, id""",
            (start_s, end_s),
        ).fetchall()
        for r in rec:
            due = str(r["next_due"] or "")
            if not DATE_RE.match(due):
                continue
            amt = f"£{r['amount']}" if r["amount"] else ""
            sub = " · ".join(x for x in ("House recurring", amt) if x)
            out.append({
                "id": f"house:recurring:{r['id']}",
                "layer": "house",
                "date": due,
                "until": None,
                "time": None,
                "end_time": None,
                "all_day": True,
                "title": str(r["name"]),
                "subtitle": sub,
                "location": "",
                "detail": str(r["notes"] or ""),
                "colour": None,
                "href": None,
                "meta": {"kind": "recurring"},
            })
    except sqlite3.Error as exc:
        log.warning("Couldn't read House schedule data: %s", exc)
        return [], False
    finally:
        con.close()
    return out, True


def _poker_events(start_s: str, end_s: str) -> tuple[list[dict[str, Any]], bool]:
    con = _open_ro(config.POKER_DB)
    if con is None:
        return [], False
    out: list[dict[str, Any]] = []
    try:
        rows = con.execute(
            """SELECT id, date, title, kind, variant, notes, status
               FROM events
               WHERE date >= ? AND date <= ?
               ORDER BY date, id""",
            (start_s, end_s),
        ).fetchall()
        for r in rows:
            d = str(r["date"] or "")
            if not DATE_RE.match(d):
                continue
            sub = " · ".join(x for x in (r["variant"], str(r["kind"] or "").capitalize()) if x)
            out.append({
                "id": f"poker:{r['id']}",
                "layer": "poker",
                "date": d,
                "until": None,
                "time": None,
                "end_time": None,
                "all_day": True,
                "title": str(r["title"]),
                "subtitle": sub or "Poker night",
                "location": "",
                "detail": str(r["notes"] or ""),
                "colour": None,
                "href": None,
                "meta": {"status": r["status"] or "planned"},
            })
    except sqlite3.Error as exc:
        log.warning("Couldn't read Poker schedule data: %s", exc)
        return [], False
    finally:
        con.close()
    return out, True


def parse_range(start_raw: str | None, end_raw: str | None, today: date) -> tuple[date, date]:
    """Validate optional ?start=&end= dates; default to a generous 4-month window around the current month."""
    default_start = today.replace(day=1) - timedelta(days=7)
    default_end = today + timedelta(days=120)
    try:
        start = date.fromisoformat(start_raw) if start_raw else default_start
        end = date.fromisoformat(end_raw) if end_raw else default_end
    except ValueError as exc:
        raise ValueError("start and end must be YYYY-MM-DD dates") from exc
    if end < start:
        raise ValueError("end date must be on or after start date")
    if (end - start).days > MAX_SPAN_DAYS:
        raise ValueError(f"date range cannot exceed {MAX_SPAN_DAYS} days")
    return start, end


def build(start_raw: str | None = None, end_raw: str | None = None, *, at: datetime | None = None) -> dict[str, Any]:
    """Assemble the unified schedule payload for [start, end]."""
    now_dt = at or datetime.now(config.TZ)
    today = now_dt.date()
    start_d, end_d = parse_range(start_raw, end_raw, today)
    start_s, end_s = start_d.isoformat(), end_d.isoformat()

    diary_evs, diary_sources, diary_cfg = _diary_events(start_s, end_s)
    gig_evs = _gig_events(start_s, end_s)
    onsale_evs = _onsale_events(start_s, end_s)
    release_evs = _release_events(start_s, end_s)
    vinyl_evs = _vinyl_events(start_s, end_s)
    f1_evs, f1_avail = _f1_events(start_s, end_s)
    bin_evs, bin_cfg = _bin_events(start_s, end_s)
    house_evs, house_avail = _house_events(start_s, end_s)
    poker_evs, poker_avail = _poker_events(start_s, end_s)

    by_layer: dict[str, tuple[list[dict[str, Any]], bool]] = {
        "gigs": (gig_evs, True),
        "onsale": (onsale_evs, True),
        "releases": (release_evs, True),
        "vinyl": (vinyl_evs, True),
        "f1": (f1_evs, f1_avail),
        "bins": (bin_evs, bin_cfg),
        "house": (house_evs, house_avail),
        "poker": (poker_evs, poker_avail),
    }

    events: list[dict[str, Any]] = list(diary_evs)
    sources: list[dict[str, Any]] = list(diary_sources)
    for layer in OTHER_LAYERS:
        evs, avail = by_layer[layer["id"]]
        events.extend(evs)
        sources.append({
            **layer,
            "available": avail,
            "count": len(evs),
        })

    # Sort chronologically: date asc, all-day first (bins/releases/holidays), then timed events in time order
    events.sort(key=lambda e: (e["date"], 0 if e["all_day"] else 1, e["time"] or "", e["layer"], e["title"].lower()))

    return {
        "now": now_dt.isoformat(),
        "today": today.isoformat(),
        "start": start_s,
        "end": end_s,
        "diary_configured": diary_cfg,
        "google_expired": gcal.is_expired(),
        "sources": sources,
        "events": events,
    }
