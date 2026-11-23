"""The Holodeck page embeds one link's url in an iframe (retro games emulator) — for_holodeck marks which one."""
from __future__ import annotations

BASE = "/api/owner"
LINK = {"name": "Holodeck", "subtitle": "Retro games", "url": "http://192.168.0.66:8720", "icon": "gamepad"}


def _mk_link(client, name: str, **extra) -> int:
    r = client.post(f"{BASE}/links", json={**LINK, "name": f"pytest {name}", **extra})
    assert r.status_code == 201
    return r.json()["id"]


def _links_cfg(client) -> dict[str, dict]:
    return {l["name"]: l for l in client.get(f"{BASE}/config").json()["links"]}


def test_for_holodeck_create_and_uniqueness(owner_client):
    a = _mk_link(owner_client, "holodeck a", for_holodeck=True)
    b = _mk_link(owner_client, "holodeck b")
    assert _links_cfg(owner_client)["pytest holodeck a"]["for_holodeck"] is True
    assert _links_cfg(owner_client)["pytest holodeck b"]["for_holodeck"] is False

    r = owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest holodeck b", "for_holodeck": True})
    assert r.status_code == 200
    cfg = _links_cfg(owner_client)
    assert cfg["pytest holodeck b"]["for_holodeck"] is True
    assert cfg["pytest holodeck a"]["for_holodeck"] is False

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest holodeck b", "for_holodeck": False})
    assert _links_cfg(owner_client)["pytest holodeck b"]["for_holodeck"] is False
    assert a != b
