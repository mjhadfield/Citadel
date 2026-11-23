"""The Labels page embeds one link's url in an iframe (ZebraLab) — for_labels marks which one."""
from __future__ import annotations

BASE = "/api/owner"
LINK = {"name": "ZebraLab", "subtitle": "Thermal labels", "url": "http://192.168.0.66:8710", "icon": "printer"}


def _mk_link(client, name: str, **extra) -> int:
    r = client.post(f"{BASE}/links", json={**LINK, "name": f"pytest {name}", **extra})
    assert r.status_code == 201
    return r.json()["id"]


def _links_cfg(client) -> dict[str, dict]:
    return {l["name"]: l for l in client.get(f"{BASE}/config").json()["links"]}


def test_for_labels_create_and_uniqueness(owner_client):
    a = _mk_link(owner_client, "labels a", for_labels=True)
    b = _mk_link(owner_client, "labels b")
    assert _links_cfg(owner_client)["pytest labels a"]["for_labels"] is True
    assert _links_cfg(owner_client)["pytest labels b"]["for_labels"] is False

    r = owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest labels b", "for_labels": True})
    assert r.status_code == 200
    cfg = _links_cfg(owner_client)
    assert cfg["pytest labels b"]["for_labels"] is True
    assert cfg["pytest labels a"]["for_labels"] is False

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest labels b", "for_labels": False})
    assert _links_cfg(owner_client)["pytest labels b"]["for_labels"] is False
    assert a != b
