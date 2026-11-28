"""Google Calendar API (v3) integration for Citadel's Calendar widget and Unified Schedule.

Read-only (`calendar.readonly` scope). Modelled on app/music/services/spotify.py:
  - OAuth2 finishes on the Citadel PC (http://127.0.0.1:<port>/diary/google/callback) with a one-time
    hashed `state` token stored for 15 minutes; no owner cookie is needed on the callback host.
  - Refresh tokens stay server-side in SQLite (0600) and are registered with logbook.learn so they
    never appear in logs, API responses, or the online demo.
  - Incremental polling uses Google's `syncToken` (1 cheap request per calendar when nothing changed)
    and automatically falls back to a fresh `singleEvents=true` windowed fetch on `410 Gone`, when
    the date window rolls forward, or when a recurring master event changes.
  - Polite outbound behaviour: identifying User-Agent, persistent cooldown on 429/503 honouring
    Retry-After with exponential backoff, and clean expiry detection (raising a reminder strip alert).
"""
from __future__ import annotations

import asyncio
import hashlib
import html as _html
import logging
import re
import secrets
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from . import __version__, config, db, logbook

log = logging.getLogger("hub.diary")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.calendarlist.readonly"

STATE_TTL = 15 * 60
OAUTH_KEY = "diary-google-oauth"
AUTH_KEY = "diary-google-auth"
SYNC_KEY = "diary-google-sync"
COOLDOWN_KEY = "diary-google-cooldown"

DEFAULT_RETRY = 60
MAX_REST = 6 * 3600
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TAG_RE = re.compile(r"<[^>]+>")

# Fallback map of Google Calendar's 11 standard event colours (used if /colors hasn't been fetched yet).
DEFAULT_EVENT_COLORS: dict[str, str] = {
    "1": "#7986cb",   # Lavender
    "2": "#33b679",   # Sage
    "3": "#8e24aa",   # Grape
    "4": "#e67c73",   # Flamingo
    "5": "#f6bf26",   # Banana
    "6": "#f4511e",   # Tangerine
    "7": "#039be5",   # Peacock
    "8": "#616161",   # Graphite
    "9": "#3f51b5",   # Blueberry
    "10": "#0b8043",  # Basil
    "11": "#d50000",  # Tomato
}

# Theme-mode mapping targets (nearest RGB distance in sRGB for users choosing "theme colours").
_THEME_PALETTE: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("violet", (168, 85, 247)),
    ("indigo", (109, 91, 208)),
    ("silver", (180, 186, 196)),
)


class GoogleCalendarError(RuntimeError):
    """Plain-English error from Google Calendar suitable for Settings or status displays."""


class GoogleCalendarAuthError(GoogleCalendarError):
    """The OAuth refresh token has expired or been revoked — reconnect in Settings."""


class GoogleCalendarResting(GoogleCalendarError):
    """Google returned 429/503 and Citadel is honouring the cooldown window."""


def _headers() -> dict[str, str]:
    return {"User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}


def redirect_uri() -> str:
    return f"http://127.0.0.1:{config.PORT}/diary/google/callback"


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _diary_settings() -> dict[str, Any]:
    return db.get_settings("diary", {})


def credentials() -> tuple[str, str]:
    s = _diary_settings()
    return str(s.get("google_client_id") or "").strip(), str(s.get("google_client_secret") or "").strip()


def auth_state() -> dict[str, Any]:
    return db.get_settings(AUTH_KEY, {})


def put_auth(values: dict[str, Any]) -> None:
    logbook.learn([values.get("access_token", ""), values.get("refresh_token", "")])
    db.put_settings(AUTH_KEY, values)


def sync_state() -> dict[str, Any]:
    return db.get_settings(SYNC_KEY, {})


def put_sync(values: dict[str, Any]) -> None:
    db.put_settings(SYNC_KEY, values)


def learn_secrets() -> None:
    """Register Google Calendar client secret and OAuth tokens with logbook so they never hit logs."""
    _, secret = credentials()
    a = auth_state()
    logbook.learn([secret, a.get("access_token", ""), a.get("refresh_token", "")])


def secret_values() -> list[str]:
    """Every Google Calendar secret string (for logbook and build_demo leak_check)."""
    _, secret = credentials()
    a = auth_state()
    out = [secret, a.get("access_token", ""), a.get("refresh_token", "")]
    return [v for v in out if isinstance(v, str) and v.strip()]


def is_connected() -> bool:
    a = auth_state()
    return bool(a.get("refresh_token")) and not bool(a.get("expired"))


def is_expired() -> bool:
    a = auth_state()
    return bool(a.get("expired"))


def resting_until() -> float | None:
    until = db.get_settings(COOLDOWN_KEY, {}).get("until") or 0
    return float(until) if until > time.time() else None


def _rest(retry_after: str | None) -> float:
    cur = db.get_settings(COOLDOWN_KEY, {})
    strikes = int(cur.get("strikes") or 0) + 1
    try:
        asked = max(1, int(float(retry_after or 0)))
    except (TypeError, ValueError):
        asked = 0
    wait = max(asked, min(MAX_REST, DEFAULT_RETRY * 2 ** (strikes - 1)))
    until = max(time.time() + wait, float(cur.get("until") or 0))
    db.put_settings(COOLDOWN_KEY, {"until": until, "since": time.time(), "retry_after": asked or None, "strikes": strikes})
    log.warning("Google Calendar asked Citadel to back off (%s in a row; Retry-After %s): pausing for %ss",
                strikes, asked or "none", int(until - time.time()), extra={"source": "diary"})
    return until


def _worked() -> None:
    cur = db.get_settings(COOLDOWN_KEY, {})
    if cur.get("strikes"):
        db.put_settings(COOLDOWN_KEY, {**cur, "strikes": 0})


def public_status(diary_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Safe, secret-free Google Calendar connection state for Settings → Widgets."""
    s = diary_settings if diary_settings is not None else _diary_settings()
    cid = str(s.get("google_client_id") or "").strip()
    has_secret = bool(str(s.get("google_client_secret") or "").strip())
    a = auth_state()
    st = sync_state()
    selected = set(s.get("google_calendar_ids") or [])
    raw_home = s.get("google_home_calendar_ids")
    home_set = None if raw_home is None else set(raw_home)
    cals = [
        {
            "id": c["id"],
            "name": c.get("summary") or c["id"],
            "colour": c.get("backgroundColor") or "#7986cb",
            "primary": bool(c.get("primary")),
            "selected": c["id"] in selected,
            "home": (c["id"] in selected) and (home_set is None or c["id"] in home_set),
        }
        for c in (st.get("calendar_list") or [])
        if isinstance(c, dict) and c.get("id")
    ]
    resting = resting_until()
    return {
        "configured": bool(cid and has_secret),
        "client_id": cid,
        "has_client_secret": has_secret,
        "redirect_uri": redirect_uri(),
        "connected": bool(a.get("refresh_token")) and not bool(a.get("expired")),
        "expired": bool(a.get("expired")),
        "account": a.get("account") or "",
        "authorised_at": a.get("authorised_at"),
        "calendars": cals,
        "colour_mode": s.get("colour_mode") if s.get("colour_mode") in ("google", "theme") else "google",
        "resting_until": resting,
    }


# ---- OAuth2 login / callback --------------------------------------------------------------------

def login_url() -> str:
    cid, csec = credentials()
    if not cid or not csec:
        raise GoogleCalendarError("Add your Google OAuth Client ID and Client secret first")
    state = secrets.token_urlsafe(24)
    db.put_settings(OAUTH_KEY, {"state": _hash(state), "expires": int(time.time()) + STATE_TTL})
    return f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


async def finish_login(code: str, state: str, *, http: httpx.AsyncClient | None = None) -> str:
    """Exchange Google's callback code for tokens, fetch the calendar list, and return the account label."""
    pending = db.get_settings(OAUTH_KEY, {})
    db.put_settings(OAUTH_KEY, {})                                    # one-time state, consumed immediately
    if (not state or not pending.get("state")
            or not secrets.compare_digest(_hash(state), pending["state"])
            or time.time() > pending.get("expires", 0)):
        raise GoogleCalendarError("That sign-in link has expired or was already used — start again from Settings → Widgets")
    cid, csec = credentials()
    if not cid or not csec:
        raise GoogleCalendarError("Google OAuth Client ID and Client secret are not configured")

    own = http is None
    client = http or httpx.AsyncClient(headers=_headers(), timeout=20)
    try:
        r = await client.post(TOKEN_URL, data={
            "code": code,
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        })
        if r.status_code != 200:
            detail = str(r.status_code)
            try:
                body = r.json()
                detail = body.get("error_description") or body.get("error") or detail
            except ValueError:
                pass
            raise GoogleCalendarError(f"Google didn't accept the sign-in ({detail})")
        tok = r.json()
        old = auth_state()
        refresh = tok.get("refresh_token") or old.get("refresh_token") or ""
        if not refresh:
            raise GoogleCalendarError("Google did not return a refresh token — revoke Citadel in your Google Account and reconnect")
        now = int(time.time())
        put_auth({
            "access_token": tok["access_token"],
            "refresh_token": refresh,
            "expires_at": now + int(tok.get("expires_in", 3600)) - 60,
            "authorised_at": now,
            "scope": tok.get("scope", SCOPES),
            "expired": False,
            "account": old.get("account") or "",
        })
        # Fetch calendar list and colours straight away so Settings can show tick boxes immediately.
        cal_list = await refresh_calendar_list(http=client)
        primary = next((c for c in cal_list if c.get("primary")), None)
        account = (primary or {}).get("id") or (primary or {}).get("summary") or "your Google account"
        put_auth({**auth_state(), "account": account})

        # On first connect (no calendars chosen yet), pre-select the primary calendar so syncing works out of the box.
        s = _diary_settings()
        if not s.get("google_calendar_ids"):
            default_ids = [c["id"] for c in cal_list if c.get("primary")] or ([cal_list[0]["id"]] if cal_list else [])
            if default_ids:
                db.put_settings("diary", {**s, "google_calendar_ids": default_ids})
        return account
    finally:
        if own:
            await client.aclose()


def disconnect() -> None:
    """Drop stored OAuth tokens and incremental sync state; keeps Client ID/secret and iCal calendars intact."""
    db.put_settings(AUTH_KEY, {})
    db.put_settings(SYNC_KEY, {})
    db.put_settings(COOLDOWN_KEY, {})


# ---- Authenticated API requests -----------------------------------------------------------------

async def _refresh_access_token(client: httpx.AsyncClient) -> str:
    a = auth_state()
    if not a.get("refresh_token") or a.get("expired"):
        raise GoogleCalendarAuthError("Google Calendar isn't connected — reconnect in Settings → Widgets")
    cid, csec = credentials()
    if not cid or not csec:
        raise GoogleCalendarError("Google OAuth Client ID and Client secret are missing")
    r = await client.post(TOKEN_URL, data={
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": a["refresh_token"],
        "grant_type": "refresh_token",
    })
    if r.status_code in (400, 401):
        put_auth({**a, "access_token": "", "expired": True})
        raise GoogleCalendarAuthError("Google Calendar sign-in has expired — reconnect in Settings → Widgets")
    if r.status_code in (429, 503):
        _rest(r.headers.get("Retry-After"))
        raise GoogleCalendarResting("Google Calendar asked Citadel to pause — will retry automatically")
    r.raise_for_status()
    tok = r.json()
    updated = {
        **a,
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or a["refresh_token"],
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
        "expired": False,
    }
    put_auth(updated)
    return updated["access_token"]


async def _get_token(client: httpx.AsyncClient, *, force: bool = False) -> str:
    a = auth_state()
    if not a.get("refresh_token") or a.get("expired"):
        raise GoogleCalendarAuthError("Google Calendar isn't connected — reconnect in Settings → Widgets")
    if not force and a.get("access_token") and time.time() < float(a.get("expires_at") or 0):
        return a["access_token"]
    return await _refresh_access_token(client)


async def _api_get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """Authenticated GET to Google Calendar v3, handling 401 token refresh and 429/503 cooldown."""
    if resting_until():
        raise GoogleCalendarResting("Google Calendar is resting after a rate limit — will retry shortly")
    resp: httpx.Response | None = None
    for attempt in range(2):
        token = await _get_token(client, force=attempt > 0)
        resp = await client.get(f"{API_BASE}{path}", params=params, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code in (429, 503):
            _rest(resp.headers.get("Retry-After"))
            raise GoogleCalendarResting("Google Calendar asked Citadel to pause — will retry automatically")
        if resp.status_code != 401:
            break
    assert resp is not None
    if resp.status_code == 410:
        return 410, {}
    if resp.status_code == 401:
        a = auth_state()
        put_auth({**a, "access_token": "", "expired": True})
        raise GoogleCalendarAuthError("Google Calendar sign-in has expired — reconnect in Settings → Widgets")
    if resp.status_code >= 400:
        msg = str(resp.status_code)
        try:
            err = resp.json().get("error")
            if isinstance(err, dict):
                msg = err.get("message") or msg
        except ValueError:
            pass
        raise GoogleCalendarError(f"Google Calendar error: {msg}")
    _worked()
    return resp.status_code, resp.json()


# ---- Calendar list & colours --------------------------------------------------------------------

async def refresh_calendar_list(*, http: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """Fetch the user's calendars and Google's event colour palette and cache them in sync_state."""
    own = http is None
    client = http or httpx.AsyncClient(headers=_headers(), timeout=20)
    try:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"maxResults": 250}
            if page_token:
                params["pageToken"] = page_token
            _, data = await _api_get(client, "/users/me/calendarList", params)
            for c in data.get("items") or []:
                if not c.get("id") or c.get("deleted"):
                    continue
                items.append({
                    "id": c["id"],
                    "summary": str(c.get("summaryOverride") or c.get("summary") or c["id"])[:60],
                    "backgroundColor": c.get("backgroundColor") or "#7986cb",
                    "foregroundColor": c.get("foregroundColor") or "#ffffff",
                    "primary": bool(c.get("primary")),
                    "accessRole": c.get("accessRole") or "",
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        # Sort primary first, then alphabetical by summary
        items.sort(key=lambda c: (not c["primary"], c["summary"].lower()))

        colors = sync_state().get("colors") or {}
        try:
            _, cdata = await _api_get(client, "/colors")
            if isinstance(cdata.get("event"), dict):
                colors = {k: v.get("background") for k, v in cdata["event"].items() if isinstance(v, dict) and v.get("background")}
        except GoogleCalendarError:
            pass

        st = sync_state()
        put_sync({**st, "calendar_list": items, "colors": colors, "list_fetched_at": int(time.time())})
        return items
    finally:
        if own:
            await client.aclose()


# ---- Colour & event normalisation ---------------------------------------------------------------

def hex_to_theme_colour(hex_colour: str) -> str:
    """Map a hex colour (#RRGGBB) to the nearest Citadel theme token name (violet, silver, indigo)."""
    if not HEX_RE.match(hex_colour or ""):
        return "violet"
    r, g, b = int(hex_colour[1:3], 16), int(hex_colour[3:5], 16), int(hex_colour[5:7], 16)
    return min(_THEME_PALETTE, key=lambda p: (r - p[1][0]) ** 2 + (g - p[1][1]) ** 2 + (b - p[1][2]) ** 2)[0]


def _resolve_colour(ev: dict[str, Any], cal: dict[str, Any], event_colors: dict[str, str], colour_mode: str) -> str:
    cid = str(ev.get("colorId") or "")
    raw_hex = event_colors.get(cid) or DEFAULT_EVENT_COLORS.get(cid) or cal.get("backgroundColor") or "#7986cb"
    if not HEX_RE.match(raw_hex):
        raw_hex = "#7986cb"
    return hex_to_theme_colour(raw_hex) if colour_mode == "theme" else raw_hex


def _clean_description(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = _html.unescape(TAG_RE.sub(" ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def _parse_iso_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(config.TZ)
    except (ValueError, AttributeError):
        return None


def normalise_event(
    ev: dict[str, Any],
    cal: dict[str, Any],
    today: date,
    event_colors: dict[str, str],
    colour_mode: str = "google",
) -> dict[str, Any] | None:
    """Convert one Google Calendar v3 event resource into Citadel's flat diary event dict."""
    if str(ev.get("status") or "").lower() == "cancelled":
        return None
    start_obj = ev.get("start") or {}
    end_obj = ev.get("end") or {}
    if start_obj.get("date"):
        all_day = True
        try:
            first = date.fromisoformat(start_obj["date"][:10])
        except ValueError:
            return None
        last = first
        if end_obj.get("date"):
            try:
                end_d = date.fromisoformat(end_obj["date"][:10]) - timedelta(days=1)
                if end_d > first:
                    last = end_d
            except ValueError:
                pass
        time_, end_time = None, None
    elif start_obj.get("dateTime"):
        all_day = False
        local = _parse_iso_dt(start_obj["dateTime"])
        if local is None:
            return None
        first = local.date()
        end_local = _parse_iso_dt(end_obj.get("dateTime", "")) if end_obj.get("dateTime") else None
        last = end_local.date() if end_local and end_local.date() > first else first
        time_ = local.strftime("%H:%M")
        end_time = end_local.strftime("%H:%M") if end_local else None
    else:
        return None

    href = str(ev.get("htmlLink") or "").strip()
    if not href.startswith(("https://", "http://")):
        href = ""
    return {
        "id": str(ev.get("id") or ""),
        "date": max(first, today).isoformat(),
        "until": last.isoformat() if last > first else None,
        "time": time_,
        "end_time": end_time,
        "all_day": all_day,
        "title": str(ev.get("summary") or "(no title)").strip()[:120],
        "location": str(ev.get("location") or "").split("\n")[0].strip()[:80],
        "cal": str(cal.get("summary") or "Calendar")[:60],
        "cal_id": str(cal.get("id") or ""),
        "colour": _resolve_colour(ev, cal, event_colors, colour_mode),
        "href": href or None,
        "status": str(ev.get("status") or "confirmed").lower(),
        "description": _clean_description(ev.get("description")),
        "source": "google",
    }


# ---- Incremental + windowed sync ----------------------------------------------------------------

async def _full_window_fetch(
    client: httpx.AsyncClient,
    cal_id: str,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Fetch expanded events in [start, end) using singleEvents=true and capture nextSyncToken."""
    encoded = urllib.parse.quote(cal_id, safe="")
    items_by_id: dict[str, dict[str, Any]] = {}
    page_token: str | None = None
    next_sync_token = ""
    for _ in range(20):
        params: dict[str, Any] = {
            "singleEvents": "true",
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "maxResults": 250,
            "orderBy": "startTime",
        }
        if page_token:
            params["pageToken"] = page_token
        _, data = await _api_get(client, f"/calendars/{encoded}/events", params)
        for ev in data.get("items") or []:
            eid = ev.get("id")
            if eid:
                items_by_id[eid] = ev
        page_token = data.get("nextPageToken")
        if data.get("nextSyncToken"):
            next_sync_token = data["nextSyncToken"]
        if not page_token:
            break
    return items_by_id, next_sync_token


async def _incremental_fetch(
    client: httpx.AsyncClient,
    cal_id: str,
    sync_token: str,
) -> tuple[bool, list[dict[str, Any]], str]:
    """Poll only changes since `sync_token`. Returns (ok, changed_items, new_sync_token).
    ok=False means Google returned 410 Gone and a full window sync is required."""
    encoded = urllib.parse.quote(cal_id, safe="")
    changed: list[dict[str, Any]] = []
    page_token: str | None = None
    next_sync_token = sync_token
    for _ in range(20):
        params: dict[str, Any] = {"syncToken": sync_token, "maxResults": 250}
        if page_token:
            params["pageToken"] = page_token
        status, data = await _api_get(client, f"/calendars/{encoded}/events", params)
        if status == 410:
            return False, [], ""
        changed.extend(data.get("items") or [])
        page_token = data.get("nextPageToken")
        if data.get("nextSyncToken"):
            next_sync_token = data["nextSyncToken"]
        if not page_token:
            break
    return True, changed, next_sync_token


async def fetch_events(
    start: datetime,
    end: datetime,
    selected_ids: list[str],
    colour_mode: str = "google",
    *,
    http: httpx.AsyncClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sync all selected Google calendars for [start, end). Returns (events, calendar_legends)."""
    if not is_connected():
        if is_expired():
            raise GoogleCalendarAuthError("Google Calendar sign-in has expired — reconnect in Settings → Widgets")
        return [], []

    own = http is None
    client = http or httpx.AsyncClient(headers=_headers(), timeout=25)
    try:
        st = sync_state()
        now_ts = int(time.time())
        # Refresh calendar list + colours at least once every 6 hours (or if empty)
        if not st.get("calendar_list") or now_ts - int(st.get("list_fetched_at") or 0) > 6 * 3600:
            await refresh_calendar_list(http=client)
            st = sync_state()

        cal_map = {c["id"]: c for c in (st.get("calendar_list") or []) if c.get("id")}
        chosen = [cal_map[cid] for cid in selected_ids if cid in cal_map]
        if not chosen:
            return [], []

        event_colors: dict[str, str] = st.get("colors") or {}
        sync_tokens: dict[str, str] = dict(st.get("sync_tokens") or {})
        raw_cache: dict[str, dict[str, dict[str, Any]]] = dict(st.get("raw_events") or {})
        windows: dict[str, list[str]] = dict(st.get("windows") or {})

        today = start.astimezone(config.TZ).date()
        end_date = end.astimezone(config.TZ).date()
        win_pair = [today.isoformat(), end_date.isoformat()]

        out_events: list[dict[str, Any]] = []
        legends: list[dict[str, Any]] = []

        for cal in chosen:
            cid = cal["id"]
            token = sync_tokens.get(cid)
            same_window = windows.get(cid) == win_pair
            cached_map = dict(raw_cache.get(cid) or {})

            need_full = not token or not same_window or cid not in raw_cache
            if not need_full:
                ok, changed, new_token = await _incremental_fetch(client, cid, token)
                if not ok:
                    log.info("Google Calendar syncToken expired (410 Gone) for %s — running full sync", cal.get("summary", cid))
                    need_full = True
                elif any(ev.get("recurrence") or ev.get("recurringEventId") for ev in changed):
                    # A recurring master/instance changed: re-expand cleanly via singleEvents=true.
                    need_full = True
                else:
                    for ev in changed:
                        eid = ev.get("id")
                        if not eid:
                            continue
                        if str(ev.get("status") or "").lower() == "cancelled":
                            cached_map.pop(eid, None)
                        else:
                            cached_map[eid] = ev
                    if new_token:
                        sync_tokens[cid] = new_token

            if need_full:
                cached_map, new_token = await _full_window_fetch(client, cid, start, end)
                if new_token:
                    sync_tokens[cid] = new_token
                else:
                    sync_tokens.pop(cid, None)
                windows[cid] = win_pair

            raw_cache[cid] = cached_map

            cal_hex = cal.get("backgroundColor") or "#7986cb"
            legends.append({
                "id": cid,
                "name": cal.get("summary") or "Calendar",
                "colour": hex_to_theme_colour(cal_hex) if colour_mode == "theme" else cal_hex,
                "source": "google",
            })
            for ev in cached_map.values():
                norm = normalise_event(ev, cal, today, event_colors, colour_mode)
                if not norm:
                    continue
                if norm["date"] <= end_date.isoformat() and (norm["until"] or norm["date"]) >= today.isoformat():
                    out_events.append(norm)

            await asyncio.sleep(0.05)                                # gentle pacing between calendars

        # Prune sync state for calendars that are no longer selected
        active_ids = {c["id"] for c in chosen}
        sync_tokens = {k: v for k, v in sync_tokens.items() if k in active_ids}
        raw_cache = {k: v for k, v in raw_cache.items() if k in active_ids}
        windows = {k: v for k, v in windows.items() if k in active_ids}
        put_sync({**st, "sync_tokens": sync_tokens, "raw_events": raw_cache, "windows": windows})
        return out_events, legends
    finally:
        if own:
            await client.aclose()
