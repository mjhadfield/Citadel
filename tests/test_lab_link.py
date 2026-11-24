"""The HomeLab page embeds one link's url in an iframe (Home Assistant) — for_lab marks which one."""
from __future__ import annotations

BASE = "/api/owner"
LINK = {"name": "Home Assistant", "subtitle": "Smart home", "url": "http://192.168.0.79", "icon": "bulb"}


def _mk_link(client, name: str, **extra) -> int:
    r = client.post(f"{BASE}/links", json={**LINK, "name": f"pytest {name}", **extra})
    assert r.status_code == 201
    return r.json()["id"]


def _links_cfg(client) -> dict[str, dict]:
    return {l["name"]: l for l in client.get(f"{BASE}/config").json()["links"]}


def test_for_lab_create_and_uniqueness(owner_client):
    a = _mk_link(owner_client, "lab a", for_lab=True)
    b = _mk_link(owner_client, "lab b")
    assert _links_cfg(owner_client)["pytest lab a"]["for_lab"] is True
    assert _links_cfg(owner_client)["pytest lab b"]["for_lab"] is False

    r = owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest lab b", "for_lab": True})
    assert r.status_code == 200
    cfg = _links_cfg(owner_client)
    assert cfg["pytest lab b"]["for_lab"] is True
    assert cfg["pytest lab a"]["for_lab"] is False

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest lab b", "for_lab": False})
    assert _links_cfg(owner_client)["pytest lab b"]["for_lab"] is False
    assert a != b


def test_bulb_icon_offered(owner_client):
    assert "bulb" in owner_client.get(f"{BASE}/config").json()["icons"]
