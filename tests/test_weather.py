"""Weather widget: Open-Meteo parsing, WMO code mapping, radar-location fallback, view slicing and owner API."""
from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pytest

from app import config, db
from app.widgets import weather

SAMPLE_RESPONSE = {
    "current": {
        "temperature_2m": 9.4,
        "apparent_temperature": 6.8,
        "weather_code": 2,
        "is_day": 1,
        "wind_speed_10m": 11.6,
    },
    "hourly": {
        "time": [
            "2026-11-25T23:00",
            "2026-11-26T00:00",
            "2026-11-26T12:00",
            "2026-11-26T18:00",
            "2026-11-27T09:00",
        ],
        "temperature_2m": [5.1, 4.6, 10.8, 7.2, 8.4],
        "weather_code": [0, 0, 0, 61, 61],
        "precipitation_probability": [0, 5, 10, 75, 80],
        "wind_speed_10m": [6.2, 5.4, 11.7, 13.1, 15.8],
        "is_day": [0, 0, 1, 0, 1],
    },
    "daily": {
        "time": [
            "2026-11-25",
            "2026-11-26",
            "2026-11-27",
            "2026-11-28",
            "2026-11-29",
            "2026-11-30",
            "2026-12-01",
            "2026-12-02",
        ],
        "weather_code": [3, 2, 61, 0, 80, 45, 71, 95],
        "temperature_2m_max": [10.1, 11.4, 8.6, 12.2, 9.7, 7.1, 3.4, 8.9],
        "temperature_2m_min": [4.2, 5.1, 3.8, 4.4, 6.2, 2.0, -1.2, 4.7],
        "precipitation_probability_max": [30, 20, 80, 5, 60, 15, 70, 90],
        "wind_speed_10m_max": [10.0, 12.3, 16.4, 7.1, 19.2, 5.4, 14.0, 24.5],
    },
}


def at(y, m, d, h=9):
    return datetime(y, m, d, h, tzinfo=config.TZ)


def test_describe_wmo_day_and_night():
    assert weather.describe_wmo(0, is_day=True) == ("Clear", "sun")
    assert weather.describe_wmo(0, is_day=False) == ("Clear", "moon")
    assert weather.describe_wmo(61, is_day=False) == ("Light rain", "wrain")
    assert weather.describe_wmo(95) == ("Thunderstorm", "wstorm")
    assert weather.describe_wmo(999) == ("Cloudy", "wcloud")
    assert weather.describe_wmo(None) == ("Cloudy", "wcloud")


def test_parse_latlong_valid_and_invalid():
    assert weather.parse_latlong("53.4808, -2.2426") == (53.4808, -2.2426)
    assert weather.parse_latlong("-33.8688,151.2093") == (-33.8688, 151.2093)
    assert weather.parse_latlong("") is None
    assert weather.parse_latlong("Salford") is None
    assert weather.parse_latlong("95.0,0.0") is None
    assert weather.parse_latlong("53.0,190.0") is None


def test_parse_open_meteo_rounds_and_requires_days():
    parsed = weather.parse_open_meteo(SAMPLE_RESPONSE)
    assert parsed["current"] == {"temp": 9, "feels": 7, "wind": 12, "code": 2, "is_day": True}
    assert len(parsed["days"]) == 8
    assert parsed["days"][1] == {
        "date": "2026-11-26",
        "code": 2,
        "max": 11,
        "min": 5,
        "rain_pct": 20,
        "wind": 12,
    }
    with pytest.raises(ValueError):
        weather.parse_open_meteo({"current": {}, "daily": {}})


def test_view_drops_past_days_slices_5_and_7_and_omits_coords():
    parsed = weather.parse_open_meteo(SAMPLE_RESPONSE)
    v = weather.widget.view(parsed, {"latlong": "53.4808,-2.2426", "place": "Salford"}, at(2026, 11, 26))
    assert v["place"] == "Salford"
    assert "latlong" not in v
    assert v["current"]["temp"] == 9
    assert v["current"]["label"] == "Partly cloudy"
    assert v["current"]["icon"] == "wpartly"
    assert [d["date"] for d in v["days"]] == [
        "2026-11-26", "2026-11-27", "2026-11-28", "2026-11-29", "2026-11-30",
    ]
    assert len(v["extended"]) == 7
    assert v["extended"][0]["date"] == "2026-11-26"
    assert v["days"][1]["label"] == "Light rain" and v["days"][1]["icon"] == "wrain"
    # Hourly drops past dates and enriches icons with day/night awareness
    assert "2026-11-25" not in v["hourly"]
    assert len(v["hourly"]["2026-11-26"]) == 3
    assert v["hourly"]["2026-11-26"][0]["time"] == "00:00"
    assert v["hourly"]["2026-11-26"][0]["icon"] == "moon"
    assert v["hourly"]["2026-11-26"][1]["time"] == "12:00"
    assert v["hourly"]["2026-11-26"][1]["icon"] == "sun"
    assert v["hourly"]["2026-11-27"][0]["rain_pct"] == 80
    assert "2026-11-28" not in v["hourly"]

    # Older cached payloads without an hourly key still produce a clean view
    legacy = {k: val for k, val in parsed.items() if k != "hourly"}
    assert weather.widget.view(legacy, {"place": ""}, at(2026, 11, 26))["hourly"] == {}


def test_configured_falls_back_to_radar_latlong(client):
    db.put_settings("radar", {"latlong": ""})
    db.put_settings("weather", {"latlong": "", "place": ""})
    assert weather.widget.configured({"latlong": ""}) is False
    assert weather.widget.configured({"latlong": "53.4808,-2.2426"}) is True

    db.put_settings("radar", {"latlong": "53.4808,-2.2426"})
    try:
        assert weather.widget.configured({"latlong": ""}) is True
    finally:
        db.put_settings("radar", {"latlong": ""})


def test_merge_settings_validation():
    merged = weather.widget.merge_settings({}, {"latlong": " 53.4808,-2.2426 ", "place": "  Salford  "})
    assert merged == {"latlong": "53.4808,-2.2426", "place": "Salford"}
    assert weather.widget.merge_settings({}, {"latlong": "", "place": ""}) == {"latlong": "", "place": ""}
    with pytest.raises(ValueError):
        weather.widget.merge_settings({}, {"latlong": "not-coords"})
    with pytest.raises(ValueError):
        weather.widget.merge_settings({}, {"unknown": 1})


def test_fetch_uses_open_meteo_and_handles_rate_limit(monkeypatch):
    seen: list[httpx.Request] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SAMPLE_RESPONSE)

    monkeypatch.setattr(weather, "TRANSPORT", httpx.MockTransport(ok_handler))
    data = asyncio.run(weather.widget.fetch({"latlong": "53.4808,-2.2426", "place": ""}))
    assert len(seen) == 1
    assert seen[0].url.params["latitude"] == "53.4808"
    assert seen[0].url.params["longitude"] == "-2.2426"
    assert seen[0].url.params["wind_speed_unit"] == "mph"
    assert "temperature_2m" in seen[0].url.params["hourly"]
    assert data["current"]["temp"] == 9
    assert "2026-11-26" in data["hourly"]
    assert "9°C, Partly cloudy" in weather.widget.summarise(data)

    def limit_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    monkeypatch.setattr(weather, "TRANSPORT", httpx.MockTransport(limit_handler))
    with pytest.raises(RuntimeError, match="HTTP 429.*120s"):
        asyncio.run(weather.widget.fetch({"latlong": "53.4808,-2.2426", "place": ""}))


def test_owner_settings_and_state_include_weather(owner_client):
    r = owner_client.put("/api/owner/widgets/weather/settings", json={"latlong": "53.4808,-2.2426", "place": "Salford"})
    assert r.status_code == 200, r.text
    db.cache_put("weather", weather.parse_open_meteo(SAMPLE_RESPONSE))

    state = owner_client.get("/api/owner/state").json()
    w = state["widgets"]["weather"]
    assert w["configured"] is True
    assert w["data"]["place"] == "Salford"
    assert w["data"]["current"]["temp"] == 9
    assert "latlong" not in w["data"]
