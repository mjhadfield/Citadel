"""Local weather and 5-day forecast for the Now tile, from Open-Meteo.

Open-Meteo needs no API key and returns current conditions plus a 7-day daily forecast in one request.
Location comes from this widget's own `latlong` setting, falling back to Gig Radar's `latlong` when left
blank so Mike doesn't have to enter coordinates twice. Coordinates stay server-side: `view()` returns
only temperatures, conditions, rain chance, wind speed and the optional place label.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from .. import __version__, db
from .base import Widget

URL = "https://api.open-meteo.com/v1/forecast"
HEADERS = {"User-Agent": f"Citadel/{__version__} (personal homelab)"}
LATLONG_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
TRANSPORT: httpx.AsyncBaseTransport | None = None      # swapped in by tests

# WMO weather interpretation codes -> (plain-English label, icon key in web/js/icons.js).
WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "sun"),
    1: ("Mainly clear", "wpartly"),
    2: ("Partly cloudy", "wpartly"),
    3: ("Overcast", "wcloud"),
    45: ("Fog", "wfog"),
    48: ("Freezing fog", "wfog"),
    51: ("Light drizzle", "wrain"),
    53: ("Drizzle", "wrain"),
    55: ("Heavy drizzle", "wrain"),
    56: ("Freezing drizzle", "wrain"),
    57: ("Heavy freezing drizzle", "wrain"),
    61: ("Light rain", "wrain"),
    63: ("Rain", "wrain"),
    65: ("Heavy rain", "wrain"),
    66: ("Freezing rain", "wrain"),
    67: ("Heavy freezing rain", "wrain"),
    71: ("Light snow", "wsnow"),
    73: ("Snow", "wsnow"),
    75: ("Heavy snow", "wsnow"),
    77: ("Snow grains", "wsnow"),
    80: ("Light showers", "wrain"),
    81: ("Showers", "wrain"),
    82: ("Heavy showers", "wrain"),
    85: ("Snow showers", "wsnow"),
    86: ("Heavy snow showers", "wsnow"),
    95: ("Thunderstorm", "wstorm"),
    96: ("Thunderstorm & hail", "wstorm"),
    99: ("Severe thunderstorm", "wstorm"),
}


def describe_wmo(code: int | None, *, is_day: bool = True) -> tuple[str, str]:
    label, icon = WMO.get(int(code) if code is not None else -1, ("Cloudy", "wcloud"))
    if icon == "sun" and not is_day:
        return "Clear", "moon"
    return label, icon


def parse_latlong(text: str | None) -> tuple[float, float] | None:
    m = LATLONG_RE.match(str(text or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _round(v: Any) -> int | None:
    return round(float(v)) if isinstance(v, (int, float)) else None


def parse_open_meteo(raw: dict[str, Any]) -> dict[str, Any]:
    cur = raw.get("current") or {}
    daily = raw.get("daily") or {}
    dates = daily.get("time") or []
    codes = daily.get("weather_code") or []
    maxs = daily.get("temperature_2m_max") or []
    mins = daily.get("temperature_2m_min") or []
    rains = daily.get("precipitation_probability_max") or []
    winds = daily.get("wind_speed_10m_max") or []
    if not dates:
        raise ValueError("weather response had no daily forecast")
    days = []
    for i, d in enumerate(dates):
        days.append({
            "date": str(d),
            "code": int(codes[i]) if i < len(codes) and codes[i] is not None else 3,
            "max": _round(maxs[i]) if i < len(maxs) else None,
            "min": _round(mins[i]) if i < len(mins) else None,
            "rain_pct": _round(rains[i]) if i < len(rains) else None,
            "wind": _round(winds[i]) if i < len(winds) else None,
        })
    hourly_raw = raw.get("hourly") or {}
    h_times = hourly_raw.get("time") or []
    h_temps = hourly_raw.get("temperature_2m") or []
    h_codes = hourly_raw.get("weather_code") or []
    h_rains = hourly_raw.get("precipitation_probability") or []
    h_winds = hourly_raw.get("wind_speed_10m") or []
    h_isday = hourly_raw.get("is_day") or []
    hourly: dict[str, list[dict[str, Any]]] = {}
    for i, ts in enumerate(h_times):
        s = str(ts)
        if len(s) < 16 or s[10] != "T":
            continue
        day_key, hhmm = s[:10], s[11:16]
        hourly.setdefault(day_key, []).append({
            "time": hhmm,
            "temp": _round(h_temps[i]) if i < len(h_temps) else None,
            "code": int(h_codes[i]) if i < len(h_codes) and h_codes[i] is not None else 3,
            "rain_pct": _round(h_rains[i]) if i < len(h_rains) else None,
            "wind": _round(h_winds[i]) if i < len(h_winds) else None,
            "is_day": bool(h_isday[i]) if i < len(h_isday) and h_isday[i] is not None else True,
        })
    current = None
    if cur.get("temperature_2m") is not None:
        current = {
            "temp": _round(cur.get("temperature_2m")),
            "feels": _round(cur.get("apparent_temperature")),
            "wind": _round(cur.get("wind_speed_10m")),
            "code": int(cur["weather_code"]) if cur.get("weather_code") is not None else 3,
            "is_day": bool(cur.get("is_day", 1)),
        }
    return {"current": current, "days": days, "hourly": hourly}


class WeatherWidget(Widget):
    id = "weather"
    title = "Weather"
    interval = 1800               # every 30 minutes
    min_interval = 900            # at most every 15 minutes
    log_success = False           # routine 30-min polls don't need to fill the event log; failures still log
    timeout = 20
    defaults = {"latlong": "", "place": ""}
    setup_hint = "Settings → Widgets → Weather (or Gig Radar's location)"

    @staticmethod
    def _coords(settings: dict[str, Any]) -> tuple[float, float] | None:
        own = parse_latlong(settings.get("latlong"))
        if own:
            return own
        radar = db.get_settings("radar", {})
        return parse_latlong(radar.get("latlong"))

    def configured(self, settings: dict[str, Any]) -> bool:
        return self._coords(settings) is not None

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        ll = str(merged.get("latlong") or "").strip()
        if ll and parse_latlong(ll) is None:
            raise ValueError("location must be latitude,longitude (e.g. 53.4808,-2.2426)")
        merged["latlong"] = ll
        merged["place"] = str(merged.get("place") or "").strip()[:60]
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        coords = self._coords(settings)
        if not coords:
            raise ValueError("no location configured for weather")
        lat, lon = coords
        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "current": "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m",
            "hourly": "temperature_2m,weather_code,precipitation_probability,wind_speed_10m,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 7,
        }
        async with httpx.AsyncClient(headers=HEADERS, timeout=self.timeout, transport=TRANSPORT) as client:
            resp = await client.get(URL, params=params)
            if resp.status_code in (429, 503):
                retry = resp.headers.get("Retry-After")
                raise RuntimeError(f"Open-Meteo returned HTTP {resp.status_code}" + (f" (retry after {retry}s)" if retry else ""))
            resp.raise_for_status()
        return parse_open_meteo(resp.json())

    def summarise(self, data: dict[str, Any] | None) -> str:
        if not data:
            return ""
        cur = data.get("current") or {}
        if cur.get("temp") is not None:
            label, _ = describe_wmo(cur.get("code"), is_day=cur.get("is_day", True))
            return f"{cur['temp']}°C, {label} · {len(data.get('days') or [])}-day forecast"
        return f"{len(data.get('days') or [])}-day forecast"

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        if not data:
            return {}
        today = now.date().isoformat()
        cur_raw = data.get("current")
        current = None
        if isinstance(cur_raw, dict) and cur_raw.get("temp") is not None:
            label, icon = describe_wmo(cur_raw.get("code"), is_day=bool(cur_raw.get("is_day", True)))
            current = {**cur_raw, "label": label, "icon": icon}
        days_all = []
        for d in data.get("days") or []:
            if d.get("date", "") < today:
                continue
            label, icon = describe_wmo(d.get("code"), is_day=True)
            days_all.append({**d, "label": label, "icon": icon})
        extended = days_all[:7]
        hourly_src = data.get("hourly") or {}
        hourly: dict[str, list[dict[str, Any]]] = {}
        for d in extended:
            raw_hours = hourly_src.get(d["date"]) or []
            if not raw_hours:
                continue
            hourly[d["date"]] = [
                {**h, "label": lbl, "icon": ic}
                for h in raw_hours
                for lbl, ic in [describe_wmo(h.get("code"), is_day=bool(h.get("is_day", True)))]
            ]
        return {
            "place": str(settings.get("place") or "").strip(),
            "current": current,
            "days": days_all[:5],
            "extended": extended,
            "hourly": hourly,
        }


widget = WeatherWidget()
