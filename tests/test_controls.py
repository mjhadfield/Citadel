"""The Controls page (app/controls.py, /api/owner/controls/*). Home Assistant is a fake transport: no test reaches Probe-1."""
from __future__ import annotations

import json

import httpx
import pytest

from app import config, homeassistant as ha


def sample_states():
    """Realistic Home Assistant Hue states mirroring Mike's house: multi-bulb Office, single-bulb Bedroom,
    a Hue Zone, Hue Scenes, and a non-Hue standalone light."""
    return [
        # Office Hue room group (2 bulbs: Office Main + Lamp)
        {"entity_id": "light.office", "state": "on", "attributes": {
            "friendly_name": "Office", "is_hue_group": True, "hue_type": "room",
            "lights": ["Office Main", "Lamp"], "brightness": 191,
            "supported_color_modes": ["color_temp", "xy"], "color_mode": "color_temp",
            "color_temp_kelvin": 3500, "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500,
            "rgb_color": [255, 196, 137],
        }},
        {"entity_id": "light.office_main", "state": "on", "attributes": {
            "friendly_name": "Office Main", "brightness": 217,
            "supported_color_modes": ["color_temp", "xy"], "color_mode": "color_temp",
            "color_temp_kelvin": 3500, "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500,
            "rgb_color": [255, 196, 137],
        }},
        {"entity_id": "light.lamp", "state": "on", "attributes": {
            "friendly_name": "Lamp", "brightness": 140,
            "supported_color_modes": ["color_temp", "xy"], "color_mode": "xy",
            "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500,
            "rgb_color": [255, 140, 40],
        }},
        # Bedroom Hue room group (single bulb whose HA friendly_name repeats the room prefix)
        {"entity_id": "light.bedroom", "state": "off", "attributes": {
            "friendly_name": "Bedroom", "is_hue_group": True, "hue_type": "room",
            "lights": ["Bedroom Main"], "supported_color_modes": ["color_temp", "xy"],
        }},
        {"entity_id": "light.bedroom_bedroom_main", "state": "off", "attributes": {
            "friendly_name": "Bedroom Bedroom Main",
            "supported_color_modes": ["color_temp", "xy"],
            "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500,
        }},
        # Standalone non-Hue light (brightness-only)
        {"entity_id": "light.hallway_strip", "state": "on", "attributes": {
            "friendly_name": "Hallway Strip", "brightness": 128,
            "supported_color_modes": ["brightness"], "color_mode": "brightness",
        }},
        # Hue Zone (appears in zones[], not duplicated into rooms[])
        {"entity_id": "light.upstairs", "state": "on", "attributes": {
            "friendly_name": "Upstairs", "is_hue_group": True, "hue_type": "zone",
            "lights": ["Office Main", "Bedroom Main"], "brightness": 191,
            "supported_color_modes": ["color_temp", "xy"],
        }},
        # Hue Scenes attached by group_name
        {"entity_id": "scene.office_relax", "state": "scening", "attributes": {
            "friendly_name": "Office Relax", "group_name": "Office", "name": "Relax",
        }},
        {"entity_id": "scene.office_concentrate", "state": "scening", "attributes": {
            "friendly_name": "Office Concentrate", "group_name": "Office", "name": "Concentrate",
        }},
        # Non-lighting entities must be ignored
        {"entity_id": "switch.kettle", "state": "off", "attributes": {"friendly_name": "Kettle"}},
    ]


@pytest.fixture
def fake(client, tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("test-token\n")
    monkeypatch.setattr(config, "HA_TOKEN_FILE", token)
    monkeypatch.setattr(config, "HA_URL", "http://ha.test")
    box = {"states": sample_states(), "sent": [], "gets": 0, "status": 200, "down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if box["down"]:
            raise httpx.ConnectError("no route to host")
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "GET" and request.url.path == "/api/states":
            box["gets"] += 1
            return httpx.Response(box["status"], json=box["states"])
        box["sent"].append((request.url.path, json.loads(request.content)))
        return httpx.Response(box["status"], json=[])

    monkeypatch.setattr(ha, "TRANSPORT", httpx.MockTransport(handler))
    return box


# ---- reading & grouping --------------------------------------------------------------------------------------------

def test_rooms_group_hue_bulbs_without_duplicating_single_bulb_rooms(owner_client, fake):
    got = owner_client.get("/api/owner/controls").json()
    assert got["configured"] and got["available"]
    assert [r["name"] for r in got["rooms"]] == ["Bedroom", "Hallway Strip", "Office"]
    assert [z["name"] for z in got["zones"]] == ["Upstairs"]

    by_name = {r["name"]: r for r in got["rooms"]}
    office = by_name["Office"]
    assert office["on"] and office["brightness"] == 75
    assert [b["name"] for b in office["lights"]] == ["Lamp", "Office Main"]
    assert [s["name"] for s in office["scenes"]] == ["Concentrate", "Relax"]

    # Bedroom matched light.bedroom_bedroom_main despite the doubled prefix and inherited its Kelvin bounds.
    bedroom = by_name["Bedroom"]
    assert not bedroom["on"] and len(bedroom["lights"]) == 1
    assert bedroom["lights"][0]["name"] == "Bedroom Main"
    assert bedroom["min_kelvin"] == 2200 and bedroom["max_kelvin"] == 6500

    # Standalone non-Hue light kept its brightness-only capability.
    hall = by_name["Hallway Strip"]
    assert hall["on"] and hall["brightness"] == 50 and not hall["is_group"]
    assert hall["supports"] == {"brightness": True, "color_temp": False, "color": False}

    # Summary counts individual bulbs (Office: 2 on, Bedroom: 0/1, Hallway Strip: 1/1 -> 3 of 4 on; 2 of 3 rooms on).
    assert got["summary"] == {"on": 3, "total": 4, "rooms_on": 2, "rooms_total": 3}


def test_no_token_or_unavailable_home_assistant(owner_client, fake, tmp_path, monkeypatch):
    fake["down"] = True
    got = owner_client.get("/api/owner/controls").json()
    assert got["configured"] and not got["available"] and "isn't answering" in got["error"]

    fake["down"], fake["status"] = False, 401
    got = owner_client.get("/api/owner/controls").json()
    assert not got["available"] and "refused" in got["error"]

    monkeypatch.setattr(config, "HA_TOKEN_FILE", tmp_path / "missing")
    got = owner_client.get("/api/owner/controls").json()
    assert got["configured"] is False and got["available"] is False and got["rooms"] == []


def test_guests_cant_read_or_change_lights(guest_client, fake):
    assert guest_client.get("/api/owner/controls").status_code in (401, 403)
    assert guest_client.post("/api/owner/controls/light/light.office", json={"on": False}, headers={"X-Hub": "1"}).status_code in (401, 403)
    assert guest_client.post("/api/owner/controls/scene/scene.office_relax", json={}, headers={"X-Hub": "1"}).status_code in (401, 403)
    assert guest_client.post("/api/owner/controls/all-off", json={}, headers={"X-Hub": "1"}).status_code in (401, 403)
    assert fake["sent"] == []


# ---- mutations & validation ----------------------------------------------------------------------------------------

def post_light(client, eid, body):
    return client.post(f"/api/owner/controls/light/{eid}", json=body, headers={"X-Hub": "1"})


@pytest.mark.parametrize("eid, body, expected", [
    ("light.office", {"on": False}, ("/api/services/light/turn_off", {"entity_id": "light.office"})),
    ("light.bedroom", {"on": True, "transition": 0.3}, ("/api/services/light/turn_on", {"entity_id": "light.bedroom", "transition": 0.3})),
    ("light.office", {"brightness": 40}, ("/api/services/light/turn_on", {"entity_id": "light.office", "brightness_pct": 40})),
    ("light.bedroom", {"kelvin": 1800}, ("/api/services/light/turn_on", {"entity_id": "light.bedroom", "color_temp_kelvin": 2200})),  # clamped to bedroom min_kelvin
    ("light.lamp", {"rgb": [139, 92, 246]}, ("/api/services/light/turn_on", {"entity_id": "light.lamp", "rgb_color": [139, 92, 246]})),
])
def test_light_commands_send_single_service_call(owner_client, fake, eid, body, expected):
    assert owner_client.get("/api/owner/controls").status_code == 200
    gets_before = fake["gets"]
    r = post_light(owner_client, eid, body)
    assert r.status_code == 200, r.text
    assert fake["sent"] == [expected]
    # Cached allow-list avoids an extra GET /api/states round-trip during dimming.
    assert fake["gets"] == gets_before


def test_scene_activation_and_all_off(owner_client, fake):
    assert owner_client.get("/api/owner/controls").status_code == 200
    r = owner_client.post("/api/owner/controls/scene/scene.office_relax", json={}, headers={"X-Hub": "1"})
    assert r.status_code == 200
    assert fake["sent"][-1] == ("/api/services/scene/turn_on", {"entity_id": "scene.office_relax"})

    r = owner_client.post("/api/owner/controls/all-off", json={}, headers={"X-Hub": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "turned_off": 2}
    # Only currently-on rooms (Hallway Strip + Office) are turned off — Bedroom (off) and switch.kettle are untouched.
    assert fake["sent"][-1] == ("/api/services/light/turn_off", {"entity_id": ["light.hallway_strip", "light.office"], "transition": 0.4})


@pytest.mark.parametrize("body", [
    {},
    {"on": False, "brightness": 50},
    {"brightness": 0},
    {"brightness": 101},
    {"kelvin": 2700, "rgb": [255, 0, 0]},
    {"rgb": [255, 0]},
    {"rgb": [256, 0, 0]},
    {"rgb": [True, 0, 0]},
    {"kelvin": 500},
])
def test_invalid_light_commands_refused_with_422(owner_client, fake, body):
    assert post_light(owner_client, "light.office", body).status_code == 422
    assert fake["sent"] == []


def test_unknown_or_non_light_entities_refused_with_404(owner_client, fake):
    assert post_light(owner_client, "switch.kettle", {"on": False}).status_code == 404
    assert post_light(owner_client, "light.does_not_exist", {"on": True}).status_code == 404
    assert post_light(owner_client, "light.Bad-Id", {"on": True}).status_code == 404
    assert owner_client.post("/api/owner/controls/scene/scene.nope", json={}, headers={"X-Hub": "1"}).status_code == 404
    assert fake["sent"] == []


def test_home_assistant_down_during_command_returns_503(owner_client, fake):
    assert owner_client.get("/api/owner/controls").status_code == 200
    fake["down"] = True
    r = post_light(owner_client, "light.office", {"on": False})
    assert r.status_code == 503 and "isn't answering" in r.json()["detail"]


# ---- room ordering -------------------------------------------------------------------------------------------------

def test_room_order_persists_and_keeps_unsaved_rooms_alphabetical(owner_client, fake):
    r = owner_client.put("/api/owner/controls/order", json={"order": ["light.office", "light.bedroom"]}, headers={"X-Hub": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "order": ["light.office", "light.bedroom"]}
    got = owner_client.get("/api/owner/controls").json()
    assert [x["name"] for x in got["rooms"]] == ["Office", "Bedroom", "Hallway Strip"]

    # Invalid entity IDs or guests are refused.
    assert owner_client.put("/api/owner/controls/order", json={"order": ["switch.kettle"]}, headers={"X-Hub": "1"}).status_code == 422


def test_guests_cant_change_room_order(guest_client, fake):
    assert guest_client.put("/api/owner/controls/order", json={"order": ["light.office"]}, headers={"X-Hub": "1"}).status_code in (401, 403)

