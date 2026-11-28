"""Unified Schedule (#schedule): aggregates Calendar, Gigs, Ticket on-sales, Releases, Vinyl, F1,
Bins, plus optional read-only House and Poker SQLite overlays."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from app import config, content, db, schedule
from tools import build_demo


def test_schedule_aggregates_all_sources(owner_client, tmp_path, monkeypatch):
    # 1. Seed Diary cache (two separate Google calendars: Personal + Holidays)
    db.put_settings("diary", {**db.get_settings("diary", {}), "calendars": [{"id": "c1", "name": "Main", "colour": "violet", "url": "https://example.com/a.ics"}]})
    db.cache_put("diary", {
        "events": [
            {"id": "d1", "date": "2026-12-02", "until": "2026-12-04", "time": None, "end_time": None, "all_day": True,
             "title": "Long weekend", "location": "Lake District", "cal": "Personal", "cal_id": "mike@example.com", "colour": "#039be5",
             "href": "https://calendar.google.com/calendar/event?eid=abc", "description": "Cabin booked", "source": "google"},
            {"id": "d2", "date": "2026-12-25", "until": None, "time": None, "end_time": None, "all_day": True,
             "title": "Christmas Day", "location": "", "cal": "UK Holidays", "cal_id": "holidays@group.v.calendar.google.com", "colour": "#0b8043",
             "source": "google"},
        ],
        "calendars": [
            {"id": "mike@example.com", "name": "Personal", "colour": "#039be5", "source": "google"},
            {"id": "holidays@group.v.calendar.google.com", "name": "UK Holidays", "colour": "#0b8043", "source": "google"},
        ],
    })

    # 2. Booked gig (via owner API)
    gr = owner_client.post("/api/owner/gigs", json={"artist": "Biffy Clyro", "date": "2026-12-06", "venue": "Co-op Live", "city": "Manchester",
                                                    "doors": "18:30", "ticket_url": "https://ticketmaster.co.uk/user/orders/123", "notes": "Block 105"})
    assert gr.status_code in (200, 201), gr.text
    gid = gr.json()["id"]

    # 3. Gig radar on-sale + multiple same-day presales (grouped into 1 entry) + a tribute that must be skipped
    content.upsert_items("radar", "ticketmaster", [
        {"external_id": "tm-1", "artist": "Deftones", "title": "Deftones Live", "subtitle": "AO Arena, Manchester",
         "url": "https://ticketmaster.co.uk/event/1", "date": "2027-02-14",
         "meta": {"venue": "AO Arena", "tribute": False,
                  "onsale": "2026-12-05T10:00:00Z",
                  "presales": [
                      {"at": "2026-12-03T09:00:00Z", "name": "Artist Presale"},
                      {"at": "2026-12-03T09:00:00Z", "name": "Co-op Presale"},
                      {"at": "2026-12-03T09:00:00Z", "name": "Priority from O2"},
                      {"at": "2026-12-04T10:00:00Z", "name": "Live Nation Presale"},
                  ]}},
        {"external_id": "tm-2", "artist": "Muse", "title": "Muse Tribute Night", "subtitle": "Pub",
         "url": "https://ticketmaster.co.uk/event/2", "date": "2027-02-15",
         "meta": {"venue": "Pub", "tribute": True, "onsale": "2026-12-05T10:00:00Z"}},
    ], baseline=False)

    # 4. Album release (full date kept, partial date skipped)
    content.upsert_items("release", "musicbrainz", [
        {"external_id": "mb-1", "artist": "Gojira", "title": "New LP", "subtitle": "Album",
         "url": "https://musicbrainz.org/release-group/mb-1", "date": "2026-12-11", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "mb-2", "artist": "Gojira", "title": "TBA", "subtitle": "Album",
         "url": "https://musicbrainz.org/release-group/mb-2", "date": "2026-12", "meta": {"type": "Album", "secondary": []}},
    ], baseline=False)

    # 5. Confirmed vinyl pre-order
    vid = content.add_manual_purchase("vinyl", artist="Tool", title="Lateralus", variant="Limited 2LP",
                                      date="2026-12-12", image="", url="https://roughtrade.com/tool", shop="Rough Trade")

    # 6. F1 session (spoiler-free)
    race_epoch = int(datetime(2026, 12, 6, 13, 0, tzinfo=config.TZ).timestamp())
    db.cache_put("f1cal", {
        "season": 2026,
        "rounds": [{"round": 24, "name": "Abu Dhabi Grand Prix", "short": "Abu Dhabi GP", "venues": ["Yas Marina"],
                    "sessions": [{"key": "Race", "label": "Race", "at": race_epoch}]}],
    })

    # 7. Salford bins (grouped per day)
    db.put_settings("bins", {"uprn": "100012345678", "put_out_hour": 17, "alert_streams": ["domestic", "blue", "brown"]})
    db.cache_put("bins", {"collections": [
        {"date": "2026-12-03", "stream": "blue", "label": "Blue"},
        {"date": "2026-12-03", "stream": "food", "label": "Food"},
    ]})

    # 8. Temp read-only House DB
    house_db = tmp_path / "house.sqlite"
    with sqlite3.connect(house_db) as con:
        con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, status TEXT)")
        con.execute("CREATE TABLE deadlines (id INTEGER PRIMARY KEY, job_id INTEGER, due TEXT, what TEXT, done INTEGER)")
        con.execute("CREATE TABLE recurring (id INTEGER PRIMARY KEY, name TEXT, amount INTEGER, months INTEGER, next_due TEXT, notes TEXT)")
        con.execute("INSERT INTO jobs VALUES (1, 'Boiler service', 'booked')")
        con.execute("INSERT INTO deadlines VALUES (1, 1, '2026-12-08', 'Engineer visit', 0)")
        con.execute("INSERT INTO deadlines VALUES (2, 1, '2026-12-09', 'Done task', 1)")
        con.execute("INSERT INTO recurring VALUES (1, 'Window cleaner', 25, 1, '2026-12-15', 'Front & back')")
    monkeypatch.setattr(config, "HOUSE_DB", house_db)

    # 9. Temp read-only Poker DB
    poker_db = tmp_path / "poker.sqlite"
    with sqlite3.connect(poker_db) as con:
        con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, date TEXT, title TEXT, kind TEXT, variant TEXT, notes TEXT, status TEXT)")
        con.execute("INSERT INTO events VALUES (1, '2026-12-18', 'Christmas Poker Night', 'cash', 'No Limit Hold’em', '8pm start', 'planned')")
    monkeypatch.setattr(config, "POKER_DB", poker_db)

    try:
        r = owner_client.get("/api/owner/schedule?start=2026-12-01&end=2026-12-31")
        assert r.status_code == 200
        body = r.json()
        by_layer: dict[str, list[dict]] = {}
        for ev in body["events"]:
            by_layer.setdefault(ev["layer"], []).append(ev)

        # Each synced calendar has its own separate source chip (not combined into one "Calendar")
        diary_sources = [s for s in body["sources"] if s.get("kind") == "diary"]
        assert [(s["label"], s["colour"], s["count"]) for s in diary_sources] == [
            ("Personal", "#039be5", 1),
            ("UK Holidays", "#0b8043", 1),
        ]
        # Layer IDs never expose raw email addresses
        assert all("@" not in s["id"] and s["id"].startswith("cal:") for s in diary_sources)
        personal_evs = by_layer[diary_sources[0]["id"]]
        assert [e["title"] for e in personal_evs] == ["Long weekend"]
        assert personal_evs[0]["until"] == "2026-12-04"
        assert personal_evs[0]["colour"] == "#039be5"
        assert [e["title"] for e in by_layer[diary_sources[1]["id"]]] == ["Christmas Day"]

        assert [e["title"] for e in by_layer["gigs"]] == ["Biffy Clyro"]
        assert by_layer["gigs"][0]["time"] == "18:30"

        onsales = {e["title"]: e for e in by_layer["onsale"]}
        assert set(onsales) == {"Deftones — 3 presales", "Deftones — Live Nation Presale", "Deftones — Tickets on sale"}
        assert len(onsales["Deftones — 3 presales"]["meta"]["presales"]) == 3
        assert [e["title"] for e in by_layer["releases"]] == ["Gojira — New LP"]
        assert [e["title"] for e in by_layer["vinyl"]] == ["Tool — Lateralus"]
        assert [e["title"] for e in by_layer["f1"]] == ["Abu Dhabi GP — Race"]
        assert by_layer["f1"][0]["time"] == "13:00"
        assert [e["title"] for e in by_layer["bins"]] == ["Blue + Food & garden bins"]
        assert len(by_layer["bins"][0]["meta"]["streams"]) == 2

        assert {e["title"] for e in by_layer["house"]} == {"Engineer visit", "Window cleaner"}
        assert [e["title"] for e in by_layer["poker"]] == ["Christmas Poker Night"]

        # Verify demo censoring collapses/redacts all diary calendars (no calendar names), cleans gigs, drops house & poker
        censored = build_demo.censor_schedule(body)
        layers = {e["layer"] for e in censored["events"]}
        assert "house" not in layers and "poker" not in layers
        assert all(s["label"] != "Personal" and s["label"] != "UK Holidays" for s in censored["sources"])
        diary_ev = next(e for e in censored["events"] if e["layer"] == "diary")
        assert diary_ev["title"] == "Redacted" and diary_ev["location"] == "" and diary_ev["href"] is None and diary_ev["detail"] == ""
        gig_ev = next(e for e in censored["events"] if e["layer"] == "gigs")
        assert gig_ev["href"] is None and gig_ev["detail"] == "" and gig_ev["meta"]["gig"]["ticket_url"] is None
    finally:
        owner_client.delete(f"/api/owner/gigs/{gid}")
        content.delete_item(vid)


def test_schedule_range_validation_and_auth(owner_client, guest_client):
    assert guest_client.get("/api/owner/schedule").status_code in (401, 403)
    assert owner_client.get("/api/owner/schedule?start=not-a-date").status_code == 422
    assert owner_client.get("/api/owner/schedule?start=2026-12-10&end=2026-12-01").status_code == 422
    assert owner_client.get("/api/owner/schedule?start=2026-01-01&end=2028-01-01").status_code == 422


def test_schedule_missing_external_dbs_are_graceful(owner_client):
    r = owner_client.get("/api/owner/schedule?start=2026-12-01&end=2026-12-31")
    assert r.status_code == 200
    sources = {s["id"]: s for s in r.json()["sources"]}
    assert sources["house"]["available"] is False
    assert sources["poker"]["available"] is False


def test_schedule_merges_cross_calendar_duplicates(owner_client):
    """Identical events on two calendars (e.g. Mum + Dad sharing 'Food delivery') merge into one event carrying both layers."""
    db.cache_put("diary", {
        "events": [
            {"id": "d1", "date": "2026-12-10", "until": None, "time": "14:00", "end_time": "15:00", "all_day": False,
             "title": "Food delivery", "location": "", "cal": "Dad", "cal_id": "dad@example.com", "colour": "#039be5", "source": "google"},
            {"id": "m1", "date": "2026-12-10", "until": None, "time": "14:00", "end_time": "15:00", "all_day": False,
             "title": "  food   delivery ", "location": "Home", "cal": "Mum", "cal_id": "mum@example.com", "colour": "#e67c73", "source": "google"},
            {"id": "m2", "date": "2026-12-11", "until": None, "time": None, "end_time": None, "all_day": True,
             "title": "Mum only", "location": "", "cal": "Mum", "cal_id": "mum@example.com", "colour": "#e67c73", "source": "google"},
        ],
        "calendars": [
            {"id": "dad@example.com", "name": "Dad", "colour": "#039be5", "source": "google"},
            {"id": "mum@example.com", "name": "Mum", "colour": "#e67c73", "source": "google"},
        ],
    })
    r = owner_client.get("/api/owner/schedule?start=2026-12-01&end=2026-12-31")
    assert r.status_code == 200
    body = r.json()
    diary_evs = [e for e in body["events"] if e.get("kind") == "diary"]
    assert len(diary_evs) == 2
    shared = next(e for e in diary_evs if e["title"] == "Food delivery")
    assert len(shared["layers"]) == 2
    assert shared["meta"]["cals"] == ["Dad", "Mum"]
    assert shared["location"] == "Home"      # fills missing metadata from the second copy
