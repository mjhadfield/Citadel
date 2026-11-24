"""The spoken bin reminder (app/homeassistant.py). Home Assistant is a fake transport: no test reaches Probe-1."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import config, db, homeassistant as ha

LONDON = ZoneInfo("Europe/London")
TUESDAY_EVENING = datetime(2026, 11, 24, 21, 55, 30, tzinfo=LONDON)   # collections below are on Wednesday the 25th


@pytest.fixture(autouse=True)
def _setup(client, tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("test-token\n")
    monkeypatch.setattr(config, "HA_TOKEN_FILE", token)
    monkeypatch.setattr(config, "HA_URL", "http://ha.test")
    monkeypatch.setattr(ha, "_now", lambda: TUESDAY_EVENING)
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "auth": request.headers["Authorization"], "body": json.loads(request.content)})
        return httpx.Response(200, json=[])
    monkeypatch.setattr(ha, "TRANSPORT", httpx.MockTransport(handler))

    def wipe():
        with db.db() as con:
            con.execute("DELETE FROM widget_cache WHERE widget = 'bins'")
            con.execute("DELETE FROM widget_settings WHERE widget IN ('bins', '_schedule')")
    wipe()
    yield sent
    wipe()


def _bins(*collections: tuple[str, str]) -> None:
    db.cache_put("bins", {"collections": [{"date": d, "stream": s, "label": s} for d, s in collections]})


def run(job_settings=None):
    return asyncio.run(ha.bin_job.fetch(job_settings or dict(ha.bin_job.defaults)))


def test_it_names_tomorrows_bins_on_every_echo(_setup):
    _bins(("2026-11-25", "domestic"), ("2026-11-25", "blue"), ("2026-12-02", "brown"))
    data = run()
    assert data["said"] == "Put out the blue recycling bin and the domestic bin tonight."
    assert _setup == [{"url": "http://ha.test/api/services/notify/send_message", "auth": "Bearer test-token",
                       "body": {"entity_id": "notify.everywhere_announce", "message": data["said"]}}]
    assert ha.bin_job.summarise(data).startswith('said "Put out the blue')


def test_nothing_due_tomorrow_says_nothing(_setup):
    _bins(("2026-11-26", "domestic"), ("2026-11-24", "blue"))
    data = run()
    assert data["said"] is None and _setup == [] and ha.bin_job.summarise(data) == "no bins tomorrow"


def test_it_follows_the_bins_settings_choice_of_streams(_setup):
    _bins(("2026-11-25", "food"), ("2026-11-25", "brown"))
    assert run()["said"] == "Put out the brown recycling bin tonight."          # food isn't in the default alert streams
    db.put_settings("bins", {**db.get_settings("bins", {}), "alert_streams": ["food", "brown"]})
    assert run()["said"] == "Put out the brown recycling bin and the food and garden bin tonight."


def test_a_late_run_the_same_evening_still_speaks(_setup, monkeypatch):
    _bins(("2026-11-25", "domestic"))
    monkeypatch.setattr(ha, "_now", lambda: datetime(2026, 11, 24, 23, 59, tzinfo=LONDON))   # Citadel was off at 21:55
    assert run()["said"] == "Put out the domestic bin tonight." and len(_setup) == 1


def test_a_missed_evening_stays_quiet_the_next_day_but_run_now_speaks(_setup, monkeypatch):
    _bins(("2026-11-26", "domestic"))
    monkeypatch.setattr(ha, "_now", lambda: datetime(2026, 11, 25, 8, 0, tzinfo=LONDON))     # back the next morning
    data = run()
    assert data["said"] is None and "missed 21:55 yesterday" in ha.bin_job.summarise(data) and _setup == []
    assert run({**ha.bin_job.defaults, "_forced": True})["said"] == "Put out the domestic bin tonight."


def test_a_changed_time_in_settings_moves_the_window(_setup, monkeypatch):
    _bins(("2026-11-25", "domestic"))
    db.put_settings("_schedule", {"ha-bins": {"mode": "daily", "at": "23:00"}})
    assert run()["said"] is None                                    # 21:55 is before the new 23:00
    monkeypatch.setattr(ha, "_now", lambda: datetime(2026, 11, 24, 23, 10, tzinfo=LONDON))
    assert run()["said"] == "Put out the domestic bin tonight."


def test_a_refused_token_fails_the_run_once_without_retrying(_setup, monkeypatch):
    calls = []

    def refuse(request):
        calls.append(1)
        return httpx.Response(401)
    monkeypatch.setattr(ha, "TRANSPORT", httpx.MockTransport(refuse))
    _bins(("2026-11-25", "domestic"))
    with pytest.raises(RuntimeError, match="refused the token"):
        run()
    assert calls == [1]


def test_it_is_not_set_up_without_a_token_and_never_talks_to_the_real_one_in_tests(tmp_path, monkeypatch):
    assert ha.bin_job.configured({})
    monkeypatch.setattr(config, "HA_TOKEN_FILE", tmp_path / "missing")
    assert not ha.bin_job.configured({})


def test_the_job_is_scheduled_alongside_the_others(client):
    job = client.app.state.scheduler.widgets["ha-bins"]
    assert job.daily_at == "21:55" and job.kind == "maintenance" and not job.local
