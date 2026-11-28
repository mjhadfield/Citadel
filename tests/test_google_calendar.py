"""Google Calendar API v3 integration: OAuth flow, token refresh/expiry, syncToken + 410 Gone,
colour mapping, rate-limit backoff, secret redaction, and mixing Google API + iCal sources."""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timedelta

import httpx
import pytest

from app import config, db, google_calendar as gcal, logbook
from app.widgets import diary


@pytest.fixture(autouse=True)
def _clean_google_state():
    db.put_settings("diary", dict(diary.widget.defaults))
    db.put_settings(gcal.OAUTH_KEY, {})
    db.put_settings(gcal.AUTH_KEY, {})
    db.put_settings(gcal.SYNC_KEY, {})
    db.put_settings(gcal.COOLDOWN_KEY, {})
    yield
    db.put_settings("diary", dict(diary.widget.defaults))
    db.put_settings(gcal.OAUTH_KEY, {})
    db.put_settings(gcal.AUTH_KEY, {})
    db.put_settings(gcal.SYNC_KEY, {})
    db.put_settings(gcal.COOLDOWN_KEY, {})


def _set_creds(cid="client-id-123.apps.googleusercontent.com", csec="GOCSPX-super-secret-value"):
    cur = db.get_settings("diary", diary.widget.defaults)
    db.put_settings("diary", {**cur, "google_client_id": cid, "google_client_secret": csec})


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_login_url_requires_credentials_and_uses_one_time_state(owner_client):
    r = owner_client.post("/api/owner/diary/google/login")
    assert r.status_code == 422

    _set_creds()
    r = owner_client.post("/api/owner/diary/google/login")
    assert r.status_code == 200
    body = r.json()
    assert body["redirect_uri"].endswith("/diary/google/callback")
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query)
    assert qs["scope"] == [gcal.SCOPES]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["state"][0]


def test_finish_login_exchanges_token_and_auto_selects_primary():
    import asyncio
    _set_creds()
    url = gcal.login_url()
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url) == gcal.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.access-token-abc1234567890", "refresh_token": "1//0refresh-token-xyz987654321", "expires_in": 3600})
        if req.url.path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": [
                {"id": "mike@example.com", "summary": "Personal", "backgroundColor": "#039be5", "primary": True},
                {"id": "holidays@group.v.calendar.google.com", "summary": "UK Holidays", "backgroundColor": "#0b8043"},
            ]})
        if req.url.path.endswith("/colors"):
            return httpx.Response(200, json={"event": {"11": {"background": "#d50000", "foreground": "#ffffff"}}})
        raise AssertionError(f"unexpected request {req.url}")

    async def run():
        async with _mock_client(handler) as http:
            who = await gcal.finish_login("auth-code-1", state, http=http)
            assert who == "mike@example.com"
            # State is one-time: reusing it must fail
            with pytest.raises(gcal.GoogleCalendarError, match="expired or was already used"):
                await gcal.finish_login("auth-code-1", state, http=http)

    asyncio.run(run())
    assert gcal.is_connected()
    assert db.get_settings("diary", {})["google_calendar_ids"] == ["mike@example.com"]

    # Secrets must never appear in public settings and must be scrubbed by logbook
    pub = diary.widget.public_settings(db.get_settings("diary", diary.widget.defaults))
    dumped = json.dumps(pub)
    assert "GOCSPX-super-secret-value" not in dumped
    assert "ya29.access-token-abc1234567890" not in dumped
    assert "1//0refresh-token-xyz987654321" not in dumped
    assert pub["google"]["connected"] is True
    assert len(pub["google"]["calendars"]) == 2
    assert logbook.redact("leaked 1//0refresh-token-xyz987654321 here") == "leaked *** here"


def test_token_refresh_and_expiry_raises_alert(owner_client):
    import asyncio
    _set_creds()
    gcal.put_auth({"access_token": "ya29.old-expired-token-12345678", "refresh_token": "1//0refresh-expired-1234567890",
                   "expires_at": int(time.time()) - 10, "expired": False, "account": "mike@example.com"})
    gcal.put_sync({"calendar_list": [{"id": "primary", "summary": "Main", "backgroundColor": "#039be5", "primary": True}],
                   "colors": {}, "list_fetched_at": int(time.time())})

    # 1. Refresh succeeds when token endpoint returns a new access token
    calls = []
    def ok_handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if str(req.url) == gcal.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.fresh-token-00000000000000", "expires_in": 3600})
        return httpx.Response(200, json={"items": [], "nextSyncToken": "sync-1"})

    start = datetime(2026, 11, 28, tzinfo=config.TZ)
    async def run_ok():
        async with _mock_client(ok_handler) as http:
            await gcal.fetch_events(start, start + timedelta(days=30), ["primary"], http=http)

    asyncio.run(run_ok())
    assert gcal.TOKEN_URL in calls
    assert gcal.auth_state()["access_token"] == "ya29.fresh-token-00000000000000"

    # 2. Refresh fails with 400 invalid_grant -> marks expired and raises alert in owner state
    gcal.put_auth({**gcal.auth_state(), "expires_at": int(time.time()) - 10})
    def bad_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async def run_bad():
        async with _mock_client(bad_handler) as http:
            with pytest.raises(gcal.GoogleCalendarAuthError):
                await gcal.fetch_events(start, start + timedelta(days=30), ["primary"], http=http)

    asyncio.run(run_bad())
    assert gcal.is_expired() is True
    assert gcal.is_connected() is False

    st = owner_client.get("/api/owner/state").json()
    assert any(a["key"].startswith("sync:diary-google:") for a in st["alerts"])
    assert st["widgets"]["diary"]["data"]["google_expired"] is True


def test_sync_token_incremental_and_410_gone_reset():
    import asyncio
    _set_creds()
    gcal.put_auth({"access_token": "ya29.valid-token-11111111111111", "refresh_token": "1//0refresh-token-111111111111",
                   "expires_at": int(time.time()) + 3600, "expired": False})
    gcal.put_sync({"calendar_list": [{"id": "cal1", "summary": "Personal", "backgroundColor": "#039be5", "primary": True}],
                   "colors": {"11": "#d50000"}, "list_fetched_at": int(time.time())})

    requests_log: list[dict[str, str]] = []
    phase = {"step": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        requests_log.append(params)
        if phase["step"] == 1:
            # Initial full window sync
            assert params.get("singleEvents") == "true"
            return httpx.Response(200, json={
                "items": [{
                    "id": "e1", "status": "confirmed", "summary": "Dentist", "colorId": "11",
                    "location": "Salford", "htmlLink": "https://calendar.google.com/calendar/event?eid=1",
                    "start": {"dateTime": "2026-12-01T10:00:00Z"}, "end": {"dateTime": "2026-12-01T11:00:00Z"},
                }],
                "nextSyncToken": "tok-v1",
            })
        if phase["step"] == 2:
            # Incremental delta: adds e2, cancels nothing
            assert params.get("syncToken") == "tok-v1"
            assert "timeMin" not in params and "singleEvents" not in params
            return httpx.Response(200, json={
                "items": [{
                    "id": "e2", "status": "confirmed", "summary": "Holiday",
                    "start": {"date": "2026-12-05"}, "end": {"date": "2026-12-08"},
                }],
                "nextSyncToken": "tok-v2",
            })
        if phase["step"] == 3:
            # 410 Gone on syncToken -> client must automatically fall back to full window sync
            if params.get("syncToken") == "tok-v2":
                return httpx.Response(410, json={"error": {"code": 410, "message": "Sync token is no longer valid"}})
            assert params.get("singleEvents") == "true"
            return httpx.Response(200, json={
                "items": [{
                    "id": "e3", "status": "confirmed", "summary": "Fresh after 410",
                    "start": {"date": "2026-12-10"}, "end": {"date": "2026-12-11"},
                }],
                "nextSyncToken": "tok-v3",
            })
        raise AssertionError("unexpected step")

    start = datetime(2026, 11, 28, tzinfo=config.TZ)
    end = start + timedelta(days=30)

    async def run():
        async with _mock_client(handler) as http:
            evs1, legs1 = await gcal.fetch_events(start, end, ["cal1"], colour_mode="google", http=http)
            assert len(evs1) == 1
            assert evs1[0]["title"] == "Dentist"
            assert evs1[0]["colour"] == "#d50000"      # event colorId overrides calendar backgroundColor
            assert evs1[0]["href"] == "https://calendar.google.com/calendar/event?eid=1"
            assert legs1[0]["colour"] == "#039be5"

            phase["step"] = 2
            evs2, _ = await gcal.fetch_events(start, end, ["cal1"], colour_mode="google", http=http)
            titles = {e["title"]: e for e in evs2}
            assert set(titles) == {"Dentist", "Holiday"}
            assert titles["Holiday"]["colour"] == "#039be5"   # inherits calendar colour
            assert titles["Holiday"]["date"] == "2026-12-05" and titles["Holiday"]["until"] == "2026-12-07"

            phase["step"] = 3
            evs3, _ = await gcal.fetch_events(start, end, ["cal1"], colour_mode="theme", http=http)
            assert [e["title"] for e in evs3] == ["Fresh after 410"]
            assert evs3[0]["colour"] in ("violet", "silver", "indigo")    # theme colour mapping applied

    asyncio.run(run())
    assert gcal.sync_state()["sync_tokens"]["cal1"] == "tok-v3"


def test_rate_limit_429_sets_cooldown():
    import asyncio
    _set_creds()
    gcal.put_auth({"access_token": "ya29.valid-token-22222222222222", "refresh_token": "1//0refresh-token-222222222222",
                   "expires_at": int(time.time()) + 3600, "expired": False})
    gcal.put_sync({"calendar_list": [{"id": "cal1", "summary": "Personal", "backgroundColor": "#039be5"}],
                   "colors": {}, "list_fetched_at": int(time.time())})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    start = datetime(2026, 11, 28, tzinfo=config.TZ)
    async def run():
        async with _mock_client(handler) as http:
            with pytest.raises(gcal.GoogleCalendarResting):
                await gcal.fetch_events(start, start + timedelta(days=10), ["cal1"], http=http)

    asyncio.run(run())
    until = gcal.resting_until()
    assert until is not None and until > time.time() + 100


def test_diary_widget_mixes_google_api_and_ical(monkeypatch):
    import asyncio
    _set_creds()
    gcal.put_auth({"access_token": "ya29.mix-token-3333333333333333", "refresh_token": "1//0refresh-mix-333333333333333",
                   "expires_at": int(time.time()) + 3600, "expired": False})

    async def fake_google_fetch(start, end, ids, colour_mode="google"):
        return (
            [{"date": "2026-12-03", "until": None, "time": "14:00", "end_time": "15:00", "all_day": False,
              "title": "Google Event", "location": "", "cal": "Work", "colour": "#0b8043", "source": "google"}],
            [{"id": "work", "name": "Work", "colour": "#0b8043", "source": "google"}],
        )

    ics = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:i1\r\nDTSTART;VALUE=DATE:20261204\r\nDTEND;VALUE=DATE:20261205\r\nSUMMARY:iCal Event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""

    class FakeHttp:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            return httpx.Response(200, content=ics, request=httpx.Request("GET", url))

    monkeypatch.setattr(gcal, "fetch_events", fake_google_fetch)
    monkeypatch.setattr(diary.httpx, "AsyncClient", FakeHttp)

    settings = {
        **diary.widget.defaults,
        "google_calendar_ids": ["work"],
        "calendars": [{"id": "ic1", "name": "Club", "colour": "silver", "url": "https://club.example/cal.ics"}],
        "horizon_days": 30,
    }
    assert diary.widget.configured(settings) is True
    out = asyncio.run(diary.widget.fetch(settings))
    assert [e["title"] for e in out["events"]] == ["Google Event", "iCal Event"]
    assert [c["name"] for c in out["calendars"]] == ["Work", "Club"]


def test_home_tile_calendar_filter_keeps_all_synced_in_schedule():
    """Home Calendar tile only shows calendars ticked for Home, while cache (and Schedule) keeps all synced calendars."""
    now = datetime(2026, 12, 1, 9, 0, tzinfo=config.TZ)
    data = {
        "events": [
            {"date": "2026-12-02", "until": None, "title": "Doctor", "cal": "Personal", "cal_id": "primary", "source": "google", "colour": "#039be5"},
            {"date": "2026-12-03", "until": None, "title": "Bank Holiday", "cal": "Holidays", "cal_id": "hol", "source": "google", "colour": "#0b8043"},
            {"date": "2026-12-04", "until": None, "title": "Club Night", "cal": "Club", "cal_id": "ic1", "source": "ical", "colour": "silver"},
        ],
        "calendars": [
            {"id": "primary", "name": "Personal", "colour": "#039be5", "source": "google"},
            {"id": "hol", "name": "Holidays", "colour": "#0b8043", "source": "google"},
            {"id": "ic1", "name": "Club", "colour": "silver", "source": "ical"},
        ],
    }
    # Default (google_home_calendar_ids=None): all synced calendars show on Home tile
    out_all = diary.widget.view(data, {**diary.widget.defaults, "google_calendar_ids": ["primary", "hol"]}, now)
    assert [e["title"] for e in out_all["events"]] == ["Doctor", "Bank Holiday", "Club Night"]

    # Hide Holidays and Club from Home tile
    settings = {
        **diary.widget.defaults,
        "google_calendar_ids": ["primary", "hol"],
        "google_home_calendar_ids": ["primary"],
        "calendars": [{"id": "ic1", "name": "Club", "colour": "silver", "url": "https://x/c.ics", "home": False}],
    }
    out_filtered = diary.widget.view(data, settings, now)
    assert [e["title"] for e in out_filtered["events"]] == ["Doctor"]
    assert [c["name"] for c in out_filtered["calendars"]] == ["Personal"]

    # Cross-calendar identical events shown on Home tile are deduplicated
    dup_data = {
        "events": [
            {"date": "2026-12-02", "until": None, "time": "14:00", "all_day": False, "title": "Food delivery", "cal": "Dad", "cal_id": "dad", "source": "google", "colour": "#039be5"},
            {"date": "2026-12-02", "until": None, "time": "14:00", "all_day": False, "title": "Food delivery", "cal": "Mum", "cal_id": "mum", "source": "google", "colour": "#e67c73"},
        ],
        "calendars": [
            {"id": "dad", "name": "Dad", "colour": "#039be5", "source": "google"},
            {"id": "mum", "name": "Mum", "colour": "#e67c73", "source": "google"},
        ],
    }
    out_dedup = diary.widget.view(dup_data, {**diary.widget.defaults, "google_calendar_ids": ["dad", "mum"]}, now)
    assert [e["title"] for e in out_dedup["events"]] == ["Food delivery"]


def test_guests_cannot_access_google_calendar_routes(guest_client):
    assert guest_client.post("/api/owner/diary/google/login").status_code in (401, 403)
    assert guest_client.post("/api/owner/diary/google/logout").status_code in (401, 403)
    assert guest_client.post("/api/owner/diary/google/calendars/refresh").status_code in (401, 403)
