"""The Routines page (app/routines.py, /api/owner/routines). Home Assistant is a fake transport: no test reaches Probe-1."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import config, homeassistant as ha, routines

LONDON = ZoneInfo("Europe/London")
TUESDAY_MORNING = datetime(2026, 11, 24, 6, 0, tzinfo=LONDON)       # Tue 24 Nov 2026, 06:00


def morning(on="on", time="07:45:00", days="mon,tue,wed,thu,fri", skip="off", script="off", extra=()):
    states = [
        {"entity_id": "automation.morning_routine", "state": on,
         "attributes": {"friendly_name": "Morning routine", "last_triggered": "2026-11-23T07:45:00+00:00"}},
        {"entity_id": "input_datetime.morning_routine_time", "state": time, "attributes": {}},
        {"entity_id": "input_text.morning_routine_days", "state": days, "attributes": {}},
        {"entity_id": "input_boolean.morning_routine_skip", "state": skip, "attributes": {}},
        {"entity_id": "script.morning_routine", "state": script, "attributes": {}},
        # an automation without a time helper is not a routine
        {"entity_id": "automation.something_else", "state": "on", "attributes": {"friendly_name": "Something else"}},
        {"entity_id": "light.bedroom_bedroom_main", "state": "on", "attributes": {}},
    ]
    return states + list(extra)


@pytest.fixture
def fake(client, tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("test-token\n")
    monkeypatch.setattr(config, "HA_TOKEN_FILE", token)
    monkeypatch.setattr(config, "HA_URL", "http://ha.test")
    monkeypatch.setattr(ha, "_now", lambda: TUESDAY_MORNING)
    box = {"states": morning(), "sent": [], "status": 200, "down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if box["down"]:
            raise httpx.ConnectError("no route to host")
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(box["status"], json=box["states"])
        box["sent"].append((request.url.path, json.loads(request.content)))
        return httpx.Response(box["status"], json=[])
    monkeypatch.setattr(ha, "TRANSPORT", httpx.MockTransport(handler))
    return box


# ---- reading -------------------------------------------------------------------------------------------------------

def test_a_routine_is_an_automation_with_a_time_helper(owner_client, fake):
    got = owner_client.get("/api/owner/routines").json()
    assert got["available"] and got["configured"]
    [r] = got["routines"]
    assert r["id"] == "morning_routine" and r["name"] == "Morning routine"
    assert r["on"] and r["time"] == "07:45" and r["days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert r["can_days"] and r["can_skip"] and r["can_run"] and not r["skip"] and not r["running"]
    assert r["next_run"] == "2026-11-24T07:45:00+00:00" and r["skipped_run"] is None
    assert r["last_run"] == "2026-11-23T07:45:00+00:00"


def test_skip_moves_the_next_run_on_and_names_the_skipped_one(owner_client, fake):
    fake["states"] = morning(skip="on")
    [r] = owner_client.get("/api/owner/routines").json()["routines"]
    assert r["skipped_run"] == "2026-11-24T07:45:00+00:00" and r["next_run"] == "2026-11-25T07:45:00+00:00"


def test_next_run_follows_the_days_and_the_time_of_day():
    at = lambda h, m: datetime(2026, 11, 27, h, m, tzinfo=LONDON)        # Friday 27 Nov
    nxt, _ = routines.next_runs(at(8, 0), True, "07:45", ["mon", "tue", "wed", "thu", "fri"], False)
    assert nxt == datetime(2026, 11, 30, 7, 45, tzinfo=LONDON)             # past Friday's time: Monday, over the weekend
    nxt, _ = routines.next_runs(at(7, 44), True, "07:45", ["fri"], False)
    assert nxt == at(7, 45)
    nxt, skipped = routines.next_runs(at(8, 0), True, "07:45", ["fri"], True)
    assert skipped == datetime(2026, 12, 4, 7, 45, tzinfo=LONDON) and nxt == datetime(2026, 12, 11, 7, 45, tzinfo=LONDON)
    assert routines.next_runs(at(8, 0), False, "07:45", ["fri"], False) == (None, None)       # off
    assert routines.next_runs(at(8, 0), True, "07:45", [], False) == (None, None)             # no days
    assert routines.next_runs(at(8, 0), True, None, ["fri"], False) == (None, None)           # time unknown


def test_missing_helpers_mean_every_day_and_no_skip_or_run(owner_client, fake):
    fake["states"] = [s for s in morning() if not s["entity_id"].startswith(("input_text.", "input_boolean.", "script."))]
    [r] = owner_client.get("/api/owner/routines").json()["routines"]
    assert r["days"] == list(routines.DAYS) and not r["can_days"] and not r["can_skip"] and not r["can_run"]


def test_no_token_is_not_set_up(owner_client, fake, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HA_TOKEN_FILE", tmp_path / "missing")
    assert owner_client.get("/api/owner/routines").json() == {"configured": False, "available": False, "routines": []}


def test_home_assistant_down_or_refusing_says_so(owner_client, fake):
    fake["down"] = True
    got = owner_client.get("/api/owner/routines").json()
    assert not got["available"] and "isn't answering" in got["error"]
    fake["down"], fake["status"] = False, 401
    got = owner_client.get("/api/owner/routines").json()
    assert not got["available"] and "refused" in got["error"]


def test_guests_cant_see_or_change_routines(guest_client, fake):
    assert guest_client.get("/api/owner/routines").status_code in (401, 403)
    assert guest_client.post("/api/owner/routines/morning_routine", json={"on": False}, headers={"X-Hub": "1"}).status_code in (401, 403)
    assert fake["sent"] == []


# ---- changing ------------------------------------------------------------------------------------------------------

def post(client, body, rid="morning_routine"):
    return client.post(f"/api/owner/routines/{rid}", json=body, headers={"X-Hub": "1"})


@pytest.mark.parametrize("body, call", [
    ({"on": False}, ("/api/services/automation/turn_off", {"entity_id": "automation.morning_routine"})),
    ({"on": True}, ("/api/services/automation/turn_on", {"entity_id": "automation.morning_routine"})),
    ({"time": "06:15"}, ("/api/services/input_datetime/set_datetime", {"entity_id": "input_datetime.morning_routine_time", "time": "06:15:00"})),
    ({"days": ["sat", "mon"]}, ("/api/services/input_text/set_value", {"entity_id": "input_text.morning_routine_days", "value": "mon,sat"})),
    ({"days": []}, ("/api/services/input_text/set_value", {"entity_id": "input_text.morning_routine_days", "value": ""})),
    ({"skip": True}, ("/api/services/input_boolean/turn_on", {"entity_id": "input_boolean.morning_routine_skip"})),
    ({"run": True}, ("/api/services/script/turn_on", {"entity_id": "script.morning_routine"})),
])
def test_each_change_is_one_service_call(owner_client, fake, body, call):
    resp = post(owner_client, body)
    assert resp.status_code == 200, resp.text
    assert fake["sent"] == [call]


@pytest.mark.parametrize("body", [{}, {"on": True, "skip": True}, {"time": "7:45"}, {"time": "24:00"}, {"days": ["monday"]},
                                  {"run": False}, {"name": "x"}])
def test_bad_changes_are_refused_before_home_assistant_is_asked(owner_client, fake, body):
    assert post(owner_client, body).status_code == 422
    assert fake["sent"] == []


def test_only_routines_can_be_changed(owner_client, fake):
    assert post(owner_client, {"on": False}, rid="something_else").status_code == 404      # an automation, not a routine
    assert post(owner_client, {"on": False}, rid="Bad-Id").status_code == 404
    assert post(owner_client, {"on": False}, rid="nope").status_code == 404
    assert fake["sent"] == []


def test_a_change_the_routine_has_no_helper_for_is_refused(owner_client, fake):
    fake["states"] = [s for s in morning() if not s["entity_id"].startswith(("input_boolean.", "script."))]
    assert post(owner_client, {"skip": True}).status_code == 404
    assert post(owner_client, {"run": True}).status_code == 404
    assert fake["sent"] == []


def test_home_assistant_refusing_a_change_is_a_503_with_its_reason(owner_client, fake):
    fake["down"] = True
    resp = post(owner_client, {"on": False})
    assert resp.status_code == 503 and "isn't answering" in resp.json()["detail"]
