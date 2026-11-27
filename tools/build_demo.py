#!/usr/bin/env python3
"""Build Citadel's online demo: a frozen, read-only copy of the dashboard for mikehadfield.co.uk.

    .venv/bin/python tools/build_demo.py [--out ~/Projects/webserver/demo/citadel]

Citadel's frontend is plain ES modules reading a handful of /api/owner/* GETs, so a static copy can replay them. This:

1. copies the live database into a scratch folder (SQLite backup API, read-only on the original) and starts the app
   in-process on that copy with HUB_NO_FETCH=1, so the live service, its data and outside sites are left alone;
2. saves the GET responses the dashboard, News and Music pages ask for, as data/<key>.json;
3. censors them: bookmarks (shown, every one "Redacted"), addresses (Launchpad, Remote Access: "Redacted"), booked-gig
   order links and notes, calendar events (dates and times kept, every title "Redacted"), machine names, and reminders
   that could name any of those;
4. copies web/ with its absolute /static/ paths made relative, and adds demo.js (tools/demo/), which answers /api/*
   from data/, refuses every change, stubs the event stream and freezes the clock at the snapshot time;
5. gives the home-network pages a blurred screenshot behind their "not available" notice (data/demo-shots/), and Fleet
   a made-up history worked out in demo.js from FLEET (data/fleet.json): no real machine readings are published;
6. fails if any address, home path, calendar link or known secret turns up anywhere in the output.

Everything written to --out becomes public once the website repo is pushed (its mirror publishes every committed file).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LIVE_DATA = ROOT / "data"
WEB = ROOT / "web"
TEMPLATES = Path(__file__).resolve().parent / "demo"
DEFAULT_OUT = Path.home() / "Projects/webserver/demo/citadel"
MARKER = "demo-build.json"          # written into every build; a folder without it is never wiped

COMPANION_URL = "https://mjhadfield.github.io/music-companion/"
KEEP_ALERTS = {"bin", "release", "f1", "onsale", "fresh", "vinyl", "gig"}   # not: event (calendar), down, system, maintenance
MONITOR_FRAMES, MONITOR_EVERY = 20, 1.5

# The pages that only exist on the home network. Icons are Citadel's own (icons.js); the text is HTML (Mike's wording).
NOT_HERE = {
    "media": ("film", "Media is a Jellyfin server running locally"),
    "casino": ("spade", 'This project can be viewed here: <a href="https://github.com/mjhadfield/CasinoWorld" target="_blank" '
                        'rel="noopener">github.com/mjhadfield/CasinoWorld</a>'),
    "ai": ("spark", "Odysseus is an LLM container running locally"),
    "house": ("tool", "This is too sensitive to share online"),
    "labels": ("printer", "ZebraLab is my local thermal label designer and postage printer."),
    "holodeck": ("gamepad", "Holodeck is my local retro games library and browser emulator."),
    "lab": ("bulb", "HomeLab is my Home Assistant smart home controller, on my home network only."),
    "curate": ("tag", "Curate is Music Companion's private maintenance tool on my home network."),
    "sql": ("server", "SQL DB is Music Companion's local database explorer on my home network."),
}
# Behind each "not available" notice: a screenshot of the real page, blurred past reading. The originals are Mike's own
# captures in data/demo-shots/<view>.png|jpg (git-ignored, never copied out); only the small blurred copy is published.
# A page without one just shows the notice.
SHOTS = LIVE_DATA / "demo-shots"
SHOT_WIDTH, SHOT_BLUR = 480, 5          # px wide, then a Gaussian blur of this sigma: shapes and colours, no words

# Fleet (the Stats page) in the demo: no real readings leave the house (HUB_NO_MACHINES). These are the machines as they
# were on 2026-11-18 (what each one is, not what it was doing), and demo.js makes up their history for any range from a
# fixed seed, so every visit and every range agrees with itself. Matched to Remote Access by name; Mike's PC is "this"
# and shows as "Citadel" (its real host name is kept out of the demo).
GB = 1024 ** 3
FLEET = {
    "this": {"name": "Citadel", "os": "CachyOS", "cores": 16, "mem": 32 * GB, "swap": 48 * GB, "uptime_s": 101651,
             "gpu": {"name": "NVIDIA GeForce RTX 3080 Ti", "mem_total_mb": 12288}, "disks": [["/", 996e9, 325e9]],
             "p": {"cpu": 7, "evening": 22, "temp": 42, "temp_k": .45, "mem": 45, "swap": 8, "gpu": 6, "gpu_evening": 55,
                   "gpu_temp": 38, "spikes": 3, "procs": ["python3", "code", "brave", "ffmpeg", "kwin_wayland"]}},
    "Nasferatu": {"os": "Windows 10 Pro N", "cores": 2, "mem": 8 * GB, "swap": 1.2 * GB, "uptime_s": 6063337,
                  "disks": [["C:", 120e9, 35e9], ["D:", 12002e9, 1389e9]], "raid": {"name": "Intel Raid 5 Volume", "health": "Healthy", "status": "OK", "events_24h": 0},
                  "p": {"cpu": 2, "mem": 50, "swap": 5, "spikes": 1.2, "procs": ["MsMpEng", "svchost", "TiWorker", "SearchIndexer"]}},
    "NZXT": {"os": "CachyOS", "cores": 8, "mem": 24 * GB, "swap": 24 * GB, "uptime_s": 98103,
             "gpu": {"name": "NVIDIA GeForce GTX 1080", "mem_total_mb": 8192}, "disks": [["/", 500e9, 445e9]],
             "p": {"cpu": 2, "temp": 29, "temp_k": .5, "mem": 11, "swap": 0, "gpu": 1, "gpu_evening": 12, "gpu_temp": 37,
                   "spikes": 4, "procs": ["jellyfin", "ffmpeg", "python3", "ollama"]}},
    "Phoenix": {"os": "Debian GNU/Linux 13 (trixie)", "cores": 4, "mem": 16 * GB, "swap": 16 * GB, "uptime_s": 84422,
                "gpu": {"name": "AMD Radeon", "mem_total_mb": 1024}, "disks": [["/", 966e9, 902e9]],
                "p": {"cpu": 1, "temp": 31, "temp_k": .4, "mem": 17, "swap": 0, "gpu": 0, "gpu_temp": 31, "spikes": .8,
                      "procs": ["restic", "rclone", "plasmashell"]}},
    "Runabout": {"os": "CachyOS", "cores": 8, "mem": 8 * GB, "swap": 8 * GB, "uptime_s": 182285, "disks": [["/", 252e9, 171e9]],
                 "battery": {"percent": 100, "status": "Full"},
                 "p": {"cpu": 1, "temp": 44, "temp_k": .35, "mem": 24, "swap": 3, "spikes": .7, "procs": ["brave", "kwin_x11", "python3"]}},
    "Probe-1": {"os": "Debian GNU/Linux 13 (trixie)", "cores": 4, "mem": 4 * GB, "swap": 2 * GB, "uptime_s": 9731, "disks": [["/", 31e9, 22e9]],
                "pi": {"throttled": "0x0", "under_voltage_now": False, "under_voltage_seen": False},
                "p": {"cpu": 1, "temp": 49, "temp_k": .3, "mem": 12, "swap": 0, "spikes": .6, "procs": ["apt", "python3", "unattended-upgr"]}},
    "Probe-2": {"os": "Debian GNU/Linux 13 (trixie)", "cores": 4, "mem": 4 * GB, "swap": 2 * GB, "uptime_s": 9726, "disks": [["/", 31e9, 20e9]],
                "pi": {"throttled": "0x0", "under_voltage_now": False, "under_voltage_seen": False},
                "p": {"cpu": 1, "temp": 51, "temp_k": .3, "mem": 12, "swap": 0, "spikes": .6, "procs": ["apt", "python3", "unattended-upgr"]}},
}
# Settings is in the demo as a read-only snapshot (settings.html, framed like the real one): these are the GETs it asks
# for, censored by censor_settings. The log viewer opens on "warning" (Problems); Everything ("all") and Errors only are
# saved too, with no source or search filter.
SETTINGS_GETS = ["/api/owner/config", "/api/owner/maintenance", "/api/owner/gigs", "/api/owner/artists",
                 "/api/owner/news/learning", "/api/owner/news/f1"] + [
                 f"/api/owner/logs?level={lv}&limit=200" for lv in ("warning", "all", "error")]

REDACTED = "Redacted"
REDACTED_URL = "https://redacted/"     # a bookmark's address; demo.js stops it opening ("Bookmarks are redacted…")
PRIVATE_HOST = re.compile(r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|\[|[^.]+$|.*\.(local|lan|home|internal|ts\.net)$)")
HAS_ADDRESS = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|\.local\b|\.ts\.net\b")


def key_of(path: str) -> str:
    """The file a GET is saved as. Must match keyOf() in tools/demo/demo.js."""
    rest = path.removeprefix("/api/owner")
    return re.sub(r"[^A-Za-z0-9]+", "_", rest).strip("_") or "index"


# ---- 1. a scratch copy, and the app running on it -----------------------------------------------------------------------

def demo_controls() -> dict[str, Any]:
    """Synthetic sample rooms and lights for the Controls page (never touches Home Assistant)."""
    full = {"brightness": True, "color_temp": True, "color": True}
    amb = {"brightness": True, "color_temp": True, "color": False}
    rooms = [
        {"id": "office", "entity_id": "light.office", "name": "Office", "is_group": True, "on": True, "available": True,
         "brightness": 72, "color_mode": "color_temp", "color_temp_kelvin": 3500, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": [255, 196, 137],
         "supports": full,
         "lights": [
             {"entity_id": "light.lamp", "name": "Lamp", "on": True, "available": True,
              "brightness": 55, "color_mode": "xy", "color_temp_kelvin": None, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": [255, 152, 56], "supports": full},
             {"entity_id": "light.office_main", "name": "Office Main", "on": True, "available": True,
              "brightness": 85, "color_mode": "color_temp", "color_temp_kelvin": 3500, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": [255, 196, 137], "supports": full},
         ],
         "scenes": [{"entity_id": "scene.office_concentrate", "name": "Concentrate"}, {"entity_id": "scene.office_relax", "name": "Relax"}, {"entity_id": "scene.office_dimmed", "name": "Dimmed"}]},
        {"id": "living_room", "entity_id": "light.living_room", "name": "Living room", "is_group": True, "on": True, "available": True,
         "brightness": 58, "color_mode": "color_temp", "color_temp_kelvin": 2700, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": [255, 169, 87],
         "supports": full,
         "lights": [{"entity_id": "light.living_room_main", "name": "Living Room Main", "on": True, "available": True,
                     "brightness": 58, "color_mode": "color_temp", "color_temp_kelvin": 2700, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": [255, 169, 87], "supports": full}],
         "scenes": [{"entity_id": "scene.living_room_cinema", "name": "Cinema"}, {"entity_id": "scene.living_room_relax", "name": "Relax"}, {"entity_id": "scene.living_room_bright", "name": "Bright"}]},
        {"id": "bedroom", "entity_id": "light.bedroom", "name": "Bedroom", "is_group": True, "on": False, "available": True,
         "brightness": None, "color_mode": None, "color_temp_kelvin": None, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": None,
         "supports": full,
         "lights": [{"entity_id": "light.bedroom_main", "name": "Bedroom Main", "on": False, "available": True,
                     "brightness": None, "color_mode": None, "color_temp_kelvin": None, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": None, "supports": full}],
         "scenes": [{"entity_id": "scene.bedroom_morning", "name": "Morning"}, {"entity_id": "scene.bedroom_nightlight", "name": "Nightlight"}]},
        {"id": "study", "entity_id": "light.study", "name": "Study", "is_group": True, "on": True, "available": True,
         "brightness": 65, "color_mode": "xy", "color_temp_kelvin": None, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": [139, 92, 246],
         "supports": full,
         "lights": [{"entity_id": "light.study_main", "name": "Study Main", "on": True, "available": True,
                     "brightness": 65, "color_mode": "xy", "color_temp_kelvin": None, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": [139, 92, 246], "supports": full}],
         "scenes": [{"entity_id": "scene.study_read", "name": "Read"}, {"entity_id": "scene.study_savanna", "name": "Savanna"}]},
        {"id": "gym", "entity_id": "light.gym", "name": "Gym", "is_group": True, "on": False, "available": True,
         "brightness": None, "color_mode": None, "color_temp_kelvin": None, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": None,
         "supports": full,
         "lights": [{"entity_id": "light.gym_main", "name": "Gym Main", "on": False, "available": True,
                     "brightness": None, "color_mode": None, "color_temp_kelvin": None, "min_kelvin": 2000, "max_kelvin": 6500, "rgb": None, "supports": full}],
         "scenes": [{"entity_id": "scene.gym_energise", "name": "Energise"}]},
        {"id": "box_room", "entity_id": "light.box_room", "name": "Box room", "is_group": True, "on": False, "available": True,
         "brightness": None, "color_mode": None, "color_temp_kelvin": None, "min_kelvin": 2200, "max_kelvin": 6500, "rgb": None,
         "supports": amb,
         "lights": [{"entity_id": "light.box_room_main", "name": "Box Room Main", "on": False, "available": True,
                     "brightness": None, "color_mode": None, "color_temp_kelvin": None, "min_kelvin": 2200, "max_kelvin": 6500, "rgb": None, "supports": amb}],
         "scenes": []},
    ]
    return {"configured": True, "available": True, "summary": {"on": 4, "total": 7, "rooms_on": 3, "rooms_total": 6}, "rooms": rooms, "zones": []}


def demo_routines(rt_mod: Any, at: datetime) -> dict[str, Any]:
    """Synthetic sample routines for the Routines page, anchored to the snapshot time (never touches Home Assistant)."""
    def one(rid: str, name: str, on: bool, hhmm: str, days: list[str], skip: bool, last_days_ago: int | None) -> dict[str, Any]:
        nxt, skipped = rt_mod.next_runs(at, on, hhmm, days, skip)
        last = None
        if last_days_ago is not None:
            h, m = map(int, hhmm.split(":"))
            d = at.date() - timedelta(days=last_days_ago)
            last = datetime(d.year, d.month, d.day, h, m, tzinfo=at.tzinfo).isoformat()
        return {
            "id": rid, "name": name, "on": on, "available": True, "time": hhmm, "days": days,
            "can_days": True, "skip": skip, "can_skip": True, "can_run": True, "running": False,
            "last_run": last,
            "next_run": nxt.isoformat() if nxt else None,
            "skipped_run": skipped.isoformat() if skipped else None,
        }
    return {
        "configured": True, "available": True,
        "routines": [
            one("morning_routine", "Morning routine", True, "07:45", ["mon", "tue", "wed", "thu", "fri"], False, 1),
            one("evening_winddown", "Evening wind-down", True, "22:30", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], True, 1),
            one("weekend_wake", "Weekend wake", False, "09:15", ["sat", "sun"], False, 4),
        ],
    }


def make_scratch() -> Path:
    scratch = Path(tempfile.mkdtemp(prefix="citadel-demo-"))
    src = sqlite3.connect(f"file:{LIVE_DATA / 'hub.sqlite'}?mode=ro", uri=True)
    dst = sqlite3.connect(scratch / "hub.sqlite")
    src.backup(dst)
    dst.close()
    src.close()
    shutil.copy2(LIVE_DATA / "secret.key", scratch / "secret.key")
    os.chmod(scratch, 0o700)
    return scratch


def snapshot(scratch: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Every response the demo needs, plus the monitoring readings and the secret values to check the output against."""
    os.environ.update(HUB_DATA_DIR=str(scratch), HUB_NO_FETCH="1", HUB_NO_MACHINES="1", HUB_PORT="18097")   # no machines: none of their names/stats in public
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    from app import auth, logbook
    from app.main import app
    from app.music import discover
    from app.widgets import discover as widgets
    from app import db

    out: dict[str, Any] = {}
    with TestClient(app) as c:
        token = auth.create_enrol_token("owner", "Online demo build")
        r = c.post(f"/enrol/{token}", follow_redirects=False)
        assert r.status_code == 303, r.text
        c.headers["X-Hub"] = "1"
        print("waiting for health probes and the monitor…")
        time.sleep(5)

        def get(path: str) -> Any:
            r = c.get(path)
            r.raise_for_status()
            out[path] = r.json()
            return out[path]

        get("/api/owner/state")
        news = get("/api/owner/news")
        get("/api/owner/news/front")
        get("/api/owner/news/archive?")
        for s in news["sections"]:
            for q in ("", "?everything=true"):
                get(f"/api/owner/news/section/{s['id']}{q}")
            get(f"/api/owner/news/archive?section={s['id']}")
        music = get("/api/owner/music")
        for pl in music.get("playlists", []):
            get(f"/api/owner/music/playlists/{pl['id']}")
        for basis in discover.BASES:
            if discover.cached(basis) is not None:          # an uncached basis would start a Discover run; leave it out
                get(f"/api/owner/music/discover?basis={basis}")
        get("/api/owner/music/rediscover")
        get("/api/owner/music/gigs")
        for path in SETTINGS_GETS:
            get(path)
        # The Audio page and header volume buttons: the GoXLR as captured for the tests
        # (tests/fixtures/goxlr_status.json, serial replaced), shaped by the real app/audio.py and app/volume.py;
        # the demo never talks to a GoXLR, so its level meter stays still and every change gets the read-only toast.
        from app import audio, routines as rt_mod, volume as vol_mod
        gx = json.loads((ROOT / "tests" / "fixtures" / "goxlr_status.json").read_text())
        serial, mixer = next(iter(gx["mixers"].items()))
        out["/api/owner/audio"] = {**audio.view(serial, mixer, gx), "configured": True, "live": True,
                                   "unsaved": {"profile": False, "mic_profile": False}}
        vols = (mixer.get("levels") or {}).get("volumes") or {}
        # Fixture has LineOut/LineIn at 0 (muted); show Line out at a realistic listening level beside muted Vinyl.
        demo_vols = {"lineout": vol_mod.to_pct(vols.get("LineOut", 0)) or 72, "vinyl": vol_mod.to_pct(vols.get("LineIn", 0))}
        out["/api/owner/volume"] = {
            "configured": True, "available": True,
            "channels": [
                {"id": cid, "name": ch.name, "curve": ch.curve,
                 "volume": (v := demo_vols[cid]),
                 "muted": v == 0, "restore": v or 55}
                for cid, ch in vol_mod.CHANNELS.items()
            ],
        }
        # Routines & Controls pages: synthetic sample data (never contacts Home Assistant).
        snap_now = datetime.fromisoformat(out["/api/owner/state"]["now"])
        out["/api/owner/routines"] = demo_routines(rt_mod, snap_now)
        out["/api/owner/controls"] = demo_controls()
        # No Spotify call for the player: the demo just shows "Nothing playing".
        out["/api/owner/music/player"] = {"ready": True, "active": False, "playing": False}

        frames = []
        for _ in range(MONITOR_FRAMES):
            frames.append(c.get("/api/owner/monitoring").json())
            time.sleep(MONITOR_EVERY)

        secrets = set(logbook._known)
        for w in widgets().values():
            settings = db.get_settings(w.id, w.defaults)
            secrets.update(w.secrets(settings))
            for k in ("latlong", "uprn", "cal_url"):
                if isinstance(settings.get(k), str):
                    secrets.add(settings[k])
    return out, frames, sorted(s for s in secrets if isinstance(s, str) and len(s.strip()) >= 6)


# ---- 2. censoring ----------------------------------------------------------------------------------------------------------

def redact_diary(d: dict[str, Any]) -> dict[str, Any]:
    """The real calendar's dates and times, nothing else: every title "Redacted", no places, no calendar names."""
    keep = ("date", "until", "time", "end_time", "all_day", "colour")
    d["events"] = [{**{k: e.get(k) for k in keep}, "title": REDACTED, "location": "", "cal": "Calendar"} for e in d.get("events") or []]
    d["calendars"] = [{"name": "Calendar", "colour": c.get("colour", "violet")} for c in d.get("calendars") or []] or [{"name": "Calendar", "colour": "violet"}]
    return {k: v for k, v in d.items() if k in ("events", "calendars")}


def private_url(url: str | None) -> bool:
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return True
    return not host or bool(PRIVATE_HOST.match(host))


def clean_link(l: dict[str, Any]) -> dict[str, Any]:
    """A Launchpad tile keeps its name and icon. One on the home network loses its address (demo.js: "home network
    only"), and a subtitle that is an address says "Redacted". Music Companion opens the public copy."""
    l = {k: v for k, v in l.items() if k not in ("status_url", "check_url")}
    if "music companion" in (l.get("name") or "").lower():
        return {**l, "url": COMPANION_URL, "subtitle": "The public copy", "up": True}
    if HAS_ADDRESS.search(l.get("subtitle") or ""):
        l["subtitle"] = REDACTED
    if private_url(l.get("url")):
        l["url"] = "#"
    return l


def clean_gig(g: dict[str, Any]) -> dict[str, Any]:
    return {**g, "notes": "", "ticket_url": None}          # booked tickets link to Ticketmaster *orders*


def censor_state(s: dict[str, Any]) -> dict[str, Any]:
    s["host"], s["ip"] = "Online demo", ""
    s["device"] = {"id": 0, "name": "Demo", "role": "owner"}
    s["links"] = [clean_link(l) for l in s["links"]]
    # no addresses (ports and "this PC only" are harmless), except Companion's page, which is the public copy
    s["projects"] = [{**p, "url": COMPANION_URL if p.get("for_music") else None} for p in s["projects"]]
    # device names only: the address reads "Redacted", and demo.js answers a click with "not available in the demo"
    s["remote"] = [{**r, "address": REDACTED, "lan_ip": None, "open": False} for r in s.get("remote", [])]
    s["health"] = {k: v for k, v in s["health"].items() if k.startswith("project:")}
    s["alerts"] = [a for a in s["alerts"] if a.get("kind") in KEEP_ALERTS]

    w = s["widgets"]
    if "shortcuts" in w and (sc := w["shortcuts"].get("data")):
        # as many bookmarks as there really are, every one "Redacted", with the same plain icon and no address
        w["shortcuts"]["data"] = {"show": bool(sc.get("show")), "failed": [], "shortcuts": [
            {"id": i + 1, "label": REDACTED, "url": REDACTED_URL, "icon_mode": "builtin", "icon": "globe", "img": None}
            for i, _ in enumerate(sc.get("shortcuts") or [])]}
    if "diary" in w and w["diary"].get("data"):
        w["diary"]["data"] = redact_diary(w["diary"]["data"])
    if "gigs" in w and (g := w["gigs"].get("data")):
        if g.get("next"):
            g["next"] = clean_gig(g["next"])
        for k in ("upcoming", "past"):
            g[k] = [clean_gig(x) for x in g.get(k) or []]
    if "system" in w and (sysd := w["system"].get("data")):
        sysd["host"] = "citadel"
        disks = []
        for d in sysd.get("disks", []):
            if d.get("mount") in ("/", "/boot", "/home"):
                disks.append(d)
            elif d.get("name") == "NAS":
                disks.append({**d, "mount": "/mnt/nas"})
        sysd["disks"] = disks
    return s


def censor_music(path: str, data: dict[str, Any]) -> dict[str, Any]:
    if path == "/api/owner/music":
        if "desktop_command" in data.get("settings", {}):
            data["settings"]["desktop_command"] = REDACTED
        # Settings' service forms: every filled-in value and the database path read "Redacted" (keys never reach here)
        data["services"] = [{**sv, "path": REDACTED if sv.get("path") else "",
                             "values": {k: (REDACTED if v else v) for k, v in (sv.get("values") or {}).items()}}
                            for sv in data.get("services", [])]
        if isinstance(data.get("spotify"), dict):
            data["spotify"]["redirect_uri"] = ""
    if path == "/api/owner/music/gigs":
        data["booked"] = [clean_gig(g) for g in data.get("booked", [])]
    return scrub(data)


def scrub(obj: Any) -> Any:
    """The backstop under every Settings response: any address, home path, private host name or email address left in
    a string becomes "Redacted". The explicit rules in censor_settings come first; the leak check still runs after."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        for rx in SCRUB:
            obj = rx.sub(REDACTED, obj)
    return obj


SCRUB = [re.compile(r"https?://(localhost|127\.0\.0\.1|10\.|192\.168\.|\{host\})[^\s\"')]*"),
         re.compile(r"\b(192\.168|10\.\d{1,3}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}(:\d+)?\b"),
         re.compile(r"/home/[^\s\"',)]*"), re.compile(r"/tmp/[^\s\"',)]*"),
         re.compile(r"\b[\w.-]+\.(local|lan|ts\.net)\b"), re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
         re.compile(r"\bGaming\b")]                       # the PC's host name


def censor_settings(path: str, data: Any) -> Any:
    """Settings, read-only: everything that shows how it works stays (sections, feeds, shops, schedules, artists, the
    log's own lines); addresses, folders, commands, keys, device details, the bins' property number, the radar's
    coordinates, calendar and bookmark names don't."""
    if path == "/api/owner/config":
        d = data
        d["host"], d["ip"], d["public_url"] = "citadel", "", ""
        d["paths"] = {k: REDACTED for k in d.get("paths", {})}
        for l in d.get("links", []):
            for k in ("url", "health_url"):
                if l.get(k) and private_url(l[k].replace("{host}", "localhost")):
                    l[k] = REDACTED
            if HAS_ADDRESS.search(l.get("subtitle") or ""):
                l["subtitle"] = REDACTED
        for pr in d.get("projects", []):
            pr.update(url=REDACTED if pr.get("url") else "", cwd=REDACTED, command=REDACTED,
                      env={k: REDACTED for k in (pr.get("env") or {})})
        d["remote"] = [{**x, "address": REDACTED, "mac": REDACTED if x.get("mac") else "", "ssh_user": REDACTED if x.get("ssh_user") else ""} for x in d.get("remote", [])]
        d["devices"] = [{**dv, "id": f"demo-{i + 1}", "name": REDACTED, "user_agent": REDACTED, "current": i == 0}
                        for i, dv in enumerate(d.get("devices", []))]
        w = d.get("widgets", {})
        if "bins" in w:
            w["bins"]["settings"]["uprn"] = REDACTED
        if "radar" in w:
            w["radar"]["settings"]["latlong"] = REDACTED
        if "weather" in w and w["weather"]["settings"].get("latlong"):
            w["weather"]["settings"]["latlong"] = REDACTED
        if "diary" in w:
            w["diary"]["settings"]["calendars"] = [{**c, "name": "Calendar"} for c in w["diary"]["settings"].get("calendars", [])]
        if "shortcuts" in w:
            sc = w["shortcuts"]["settings"]
            sc["shortcuts"] = [{**x, "label": REDACTED, "url": REDACTED_URL, "icon_mode": "builtin", "icon": "globe", "image": ""}
                               for x in sc.get("shortcuts", [])]
    elif path == "/api/owner/gigs":
        data["gigs"] = [clean_gig(g) for g in data.get("gigs", [])]
    elif path.startswith("/api/owner/logs"):
        data["items"] = [{**i, "detail": None} for i in data.get("items", [])]   # tracebacks and request details
    return scrub(data)


# ---- 3. the frontend -------------------------------------------------------------------------------------------------------

def must_replace(text: str, old: str, new: str, where: str, count: int | None = None) -> str:
    n = text.count(old)
    if not n or (count is not None and n != count):
        raise SystemExit(f"build_demo: expected {count or 'some'} of {old!r} in {where}, found {n} — Citadel's code has moved; update the build")
    return text.replace(old, new)


def copy_frontend(out: Path, snap_label: str) -> None:
    for sub in ("css", "js", "fonts", "img"):
        shutil.copytree(WEB / sub, out / sub)
    for js in (out / "js").glob("*.js"):
        if js.name in ("tv.js", "guest.js", "pair.js"):
            js.unlink()                                       # pages the demo doesn't have
    (out / "css" / "tv.css").unlink(missing_ok=True)
    (out / "css" / "guest.css").unlink(missing_ok=True)
    (out / "css" / "jellyfin-theme.css").unlink(missing_ok=True)

    def edit(rel: str, *pairs: tuple[str, str]) -> None:
        p = out / rel
        text = p.read_text()
        for old, new in pairs:
            text = must_replace(text, old, new, rel)
        p.write_text(text)

    edit("js/music.js",
         ("/static/img/", "img/"),
         (", ['buying', 'Worth buying', 'bag']]", "]"))          # Discogs' terms restrict republishing prices
    edit("css/music.css", ('url("/static/img/', 'url("../img/'))
    edit("js/content-cards.js", ("startsWith('/covers/')", "startsWith('covers/')"))
    edit("js/motion.js", ("import('/static/js/motion.js')", "import('./js/motion.js')"))   # a comment, but the check below reads it
    edit("js/app.js", ('src="/backgrounds/', 'src="backgrounds/'),
         ("let MUSIC_ORIGIN = 'http://192.168.0.66:8642';", f"let MUSIC_ORIGIN = '{COMPANION_URL.split('/', 3)[0]}//{COMPANION_URL.split('/', 3)[2]}';"))
    # Settings' example placeholders are this house's own: a LAN address and the radar's coordinates; backgrounds are relative too
    edit("js/settings.js", ('placeholder="e.g. 192.168.0.70:8096"', 'placeholder="e.g. 10.0.0.20:8096"'),
         ("input('latlong', s.latlong, 'placeholder=\"53.4808,-2.2426\"')", "input('latlong', s.latlong, 'placeholder=\"51.5074,-0.1278\"')"),
         ('src="/backgrounds/', 'src="backgrounds/'))
    # Settings is the static settings.html here: the frame opens it, keeping any ?add=…#section a link asked for
    edit("js/app.js",
         ("if (l.pathname === '/settings') at = l.pathname + l.search + l.hash;", "if (l.pathname.endsWith('/settings.html')) at = 'settings.html' + l.search + l.hash;"),
         ("const to = settingsNext || at || '/settings';", "const to = (settingsNext || at || '/settings').replace(/^\\/settings/, 'settings.html');"))
    # A url() in a custom property set from script is resolved against… it depends; an absolute URL settles it.
    edit("js/theme.js", ('`url("/backgrounds/${bg.file}")`', '`url("${new URL(`backgrounds/${bg.file}`, document.baseURI).href}")`'))

    html = (WEB / "index.html").read_text()
    html = must_replace(html, '"/static/', '"', "index.html")
    html = must_replace(html, '<script src="js/theme-init.js"></script>',
                        '<script src="demo.js"></script>\n<script src="js/theme-init.js"></script>', "index.html", 1)
    html = must_replace(html, '<link rel="stylesheet" href="css/borg.css">',
                        '<link rel="stylesheet" href="css/borg.css">\n<link rel="stylesheet" href="demo.css">', "index.html", 1)
    html = must_replace(html, '<title>Citadel</title>', '<title>Citadel · online demo</title>', "index.html", 1)
    snap_short = snap_label.rsplit(" ", 1)[0]
    html = must_replace(html, '<span class="hud" id="host-label"></span>',
                        f'<span class="hud" id="host-label"></span><span class="chip demo-chip" title="A frozen copy of my home dashboard ({snap_label}): nothing here is live or saved">'
                        f'<span class="demo-chip__full">Snapshot · {snap_label} · read-only</span>'
                        f'<span class="demo-chip__short" aria-hidden="true">Demo · {snap_short}</span></span>',
                        "index.html", 1)
    shots = blur_shots(out)
    for view, (icon, why) in NOT_HERE.items():
        html = re.sub(rf'(<section class="view" id="view-{view}"[^>]*>\s*)<div class="frame-shell">',
                      rf'\1<div class="frame-shell" hidden>', html, count=1)
        old = f'id="{view}-frame"'
        i = html.find(old)
        if i < 0:
            raise SystemExit(f"build_demo: no #{view}-frame in index.html")
        end = html.find("</div>", i) + len("</div>")
        shot = f' style="background-image:url(&quot;{shots[view]}&quot;)"' if view in shots else ""   # inline: resolved against the page
        html = html[:end] + (f'\n      <div class="demo-na{" has-shot" if shot else ""}"{shot}><div class="demo-na__card">'
                             f'<svg class="i"><use href="#i-{icon}"/></svg>'
                             f'<b>Not available in the online demo</b><span>{why}</span></div></div>') + html[end:]
    if html.count('class="frame-shell" hidden') != len(NOT_HERE):
        raise SystemExit("build_demo: couldn't hide every home-network frame in index.html")
    (out / "index.html").write_text(html)

    sx = (WEB / "settings.html").read_text()
    sx = must_replace(sx, '"/static/', '"', "settings.html")
    sx = must_replace(sx, '<script src="js/theme-init.js"></script>', '<script src="demo.js"></script>\n<script src="js/theme-init.js"></script>', "settings.html", 1)
    sx = must_replace(sx, '<link rel="stylesheet" href="css/borg-settings.css">', '<link rel="stylesheet" href="css/borg-settings.css">\n<link rel="stylesheet" href="demo.css">', "settings.html", 1)
    sx = must_replace(sx, '<a class="btn" href="/">', '<a class="btn" href="index.html">', "settings.html", 1)
    sx = must_replace(sx, '<div class="sx">', '<div class="sx">\n  <div class="demo-banner" role="note"><b>Read-only snapshot</b>'
                      f'<span>These are my real settings as they were on {snap_label}, with anything private redacted. Look around freely: '
                      'nothing you change here is saved.</span></div>', "settings.html", 1)
    (out / "settings.html").write_text(sx)

    for p in out.rglob("*"):
        if p.suffix in (".js", ".css", ".html") and "/static/" in p.read_text():
            raise SystemExit(f"build_demo: an absolute /static/ path is left in {p.relative_to(out)}")


def blur_shots(out: Path) -> dict[str, str]:
    """The "not available" pages' backdrops: each of Mike's screenshots shrunk and blurred into img/demo-na/<view>.jpg.
    Needs ImageMagick; without it (or without a screenshot) that page keeps the plain notice."""
    magick = shutil.which("magick")
    done: dict[str, str] = {}
    if not magick or not SHOTS.is_dir():
        print("note: no ImageMagick or no data/demo-shots/; the 'not available' pages have no backdrop")
        return done
    (out / "img" / "demo-na").mkdir(parents=True, exist_ok=True)
    for view in NOT_HERE:
        src = next((p for p in (SHOTS / f"{view}.png", SHOTS / f"{view}.jpg") if p.is_file()), None)
        if not src:
            continue
        rel = f"img/demo-na/{view}.jpg"
        r = subprocess.run([magick, str(src), "-resize", f"{SHOT_WIDTH}x", "-blur", f"0x{SHOT_BLUR}", "-strip", "-quality", "70",
                            str(out / rel)], capture_output=True)
        if r.returncode == 0:
            done[view] = rel
        else:
            print(f"note: couldn't blur {src.name}: {r.stderr.decode()[:200]}")
    return done


def fleet_spec(state: dict[str, Any]) -> dict[str, Any]:
    """data/fleet.json for demo.js: Mike's PC, then the Remote Access devices that have a FLEET entry, in Remote Access
    order, each on or asleep as its tile shows."""
    def one(mid: str, spec: dict[str, Any], up: bool, rid: int | None, always_on: bool) -> dict[str, Any]:
        return {"id": mid, "rid": rid, "up": up, "always_on": always_on, **{k: v for k, v in spec.items()}}
    always = {"Nasferatu", "NZXT", "Probe-1", "Probe-2"}          # "Always on" in Remote Access
    out = [one("this", FLEET["this"], True, None, True)]
    for r in state.get("remote", []):
        if spec := FLEET.get(r["name"]):
            out.append(one(f"rh{r['id']}", {"name": r["name"], **spec}, r.get("up") is not False, r["id"], r["name"] in always))
    return {"machines": out}


def rewrite_paths(obj: Any, files: set[str]) -> Any:
    """Same-origin pictures (/covers/…, /backgrounds/…) become relative, and are noted to be copied in."""
    if isinstance(obj, dict):
        out = {k: rewrite_paths(v, files) for k, v in obj.items()}
        bg = out.get("background")
        if isinstance(bg, dict) and isinstance(bg.get("file"), str) and bg["file"]:
            files.add(f"backgrounds/{bg['file']}")
        return out
    if isinstance(obj, list):
        return [rewrite_paths(v, files) for v in obj]
    if isinstance(obj, str) and obj.startswith(("/covers/", "/backgrounds/")):
        files.add(obj[1:])
        return obj[1:]
    return obj


# Covers show as small thumbnails and backgrounds fill a screen at most, but the originals are full-size scans (a few MB
# each). ImageMagick, when it's installed, makes web-sized JPEGs; otherwise the originals are copied as they are.
PICTURE_MAX = {"covers": 400, "backgrounds": 1920}


def copy_pictures(out: Path, files: set[str]) -> dict[str, str]:
    """Copy the referenced pictures in; returns {old file name: new file name} for any that became .jpg."""
    renamed: dict[str, str] = {}
    magick = shutil.which("magick")
    for rel in sorted(files):
        src = LIVE_DATA / rel
        if not src.is_file():
            print(f"note: {rel} is referenced but missing from data/; it will show as a broken picture")
            continue
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if magick and src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            jpg = dst.with_suffix(".jpg")
            size = PICTURE_MAX.get(Path(rel).parts[0], 1200)
            r = subprocess.run([magick, str(src), "-auto-orient", "-resize", f"{size}x{size}>", "-background", "black", "-flatten",
                                "-strip", "-quality", "82", str(jpg)], capture_output=True)
            if r.returncode == 0 and jpg.stat().st_size < src.stat().st_size:
                if jpg.name != src.name:
                    renamed[src.name] = jpg.name
                continue
            jpg.unlink(missing_ok=True)
        shutil.copy2(src, dst)
    return renamed


# ---- 4. the leak check -----------------------------------------------------------------------------------------------------

LEAKS = [
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}"), "a LAN address"),
    (re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"), "a Tailscale address"),
    (re.compile(r"/home/mike"), "a home folder path"),
    (re.compile(r"calendar\.google\.com/calendar/ical|/private-[0-9A-Za-z_-]{8,}"), "a calendar address"),
    (re.compile(r"ticketmaster\.[a-z.]+/user/"), "a Ticketmaster order link"),
    (re.compile(r'"Gaming"'), "the PC's hostname"),            # not checked in news files: "Gaming" is a real topic tag there
    (re.compile(r"/enrol/[A-Za-z0-9_-]{8,}"), "an enrolment link"),
    (re.compile(r"@gmail\.com"), "an email address"),
]


def env_values() -> list[str]:
    vals = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(v) >= 6:
                    vals.append(v)
    return vals


def leak_check(out: Path, secrets: list[str]) -> None:
    problems = []
    for p in sorted(out.rglob("*")):
        if not p.is_file() or p.suffix not in (".json", ".js", ".css", ".html"):
            continue
        text = p.read_text(errors="replace")
        rel = str(p.relative_to(out))
        for rx, what in LEAKS:
            if what == "the PC's hostname" and rel.startswith("data/news"):
                continue
            if m := rx.search(text):
                problems.append(f"{p.relative_to(out)}: {what} ({m.group(0)[:40]!r})")
        for s in secrets:
            if s in text:
                problems.append(f"{p.relative_to(out)}: a secret value (from .env or a widget's settings)")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit("build_demo: the output contains private data — nothing should be published until this is fixed")


# ---- main --------------------------------------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"where the demo goes (default {DEFAULT_OUT})")
    args = ap.parse_args()
    out: Path = args.out.expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not (out / MARKER).exists():
        raise SystemExit(f"build_demo: {out} isn't empty and isn't an earlier demo build ({MARKER} missing); not touching it")

    scratch = make_scratch()
    try:
        responses, frames, secrets = snapshot(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    secrets = sorted(set(secrets) | set(env_values()))

    state = responses["/api/owner/state"]
    snap_at = datetime.fromisoformat(state["now"])
    snap_label = f"{snap_at.astimezone():%-d %b %Y}"

    files: set[str] = set()
    cleaned: dict[str, Any] = {}
    for path, data in responses.items():
        if path == "/api/owner/state":
            data = censor_state(data)
        elif path.startswith("/api/owner/music"):
            data = censor_music(path, data)
        elif path in SETTINGS_GETS:
            data = censor_settings(path, data)
        cleaned[path] = rewrite_paths(data, files)

    tmp_out = out.with_name(out.name + ".building")
    shutil.rmtree(tmp_out, ignore_errors=True)
    tmp_out.mkdir(parents=True)
    copy_frontend(tmp_out, snap_label)
    renamed = copy_pictures(tmp_out, files)
    (tmp_out / "data").mkdir()
    for path, data in cleaned.items():
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        for old_name, new_name in renamed.items():
            text = text.replace(old_name, new_name)
        (tmp_out / "data" / f"{key_of(path)}.json").write_text(text)
    (tmp_out / "data" / "monitoring_frames.json").write_text(json.dumps(frames, separators=(",", ":")))
    (tmp_out / "data" / "fleet.json").write_text(json.dumps(fleet_spec(cleaned["/api/owner/state"]), separators=(",", ":")))
    # what was saved, so demo.js answers "not available" for anything else without asking the server (no 404s)
    (tmp_out / "data" / "index.json").write_text(json.dumps(sorted([key_of(p) for p in cleaned] + ["monitoring_frames", "fleet"])))
    shim = (TEMPLATES / "demo.js").read_text().replace("__SNAPSHOT_MS__", str(int(snap_at.timestamp() * 1000)))
    (tmp_out / "demo.js").write_text(shim)
    shutil.copy2(TEMPLATES / "demo.css", tmp_out / "demo.css")
    (tmp_out / MARKER).write_text(json.dumps({"snapshot": state["now"], "built": datetime.now().astimezone().isoformat(timespec="seconds"),
                                              "by": "Citadel/tools/build_demo.py"}, indent=2) + "\n")

    leak_check(tmp_out, secrets)

    shutil.rmtree(out, ignore_errors=True)
    tmp_out.rename(out)
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"saved {len(cleaned)} responses + {len(frames)} monitoring readings, {len(files)} pictures")
    for path in sorted(cleaned):
        print(f"  {path}")
    print(f"demo written to {out} ({size / 1e6:.1f} MB), snapshot {snap_label}; leak check passed")


if __name__ == "__main__":
    main()
