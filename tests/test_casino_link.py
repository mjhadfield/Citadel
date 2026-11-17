"""The Casino page embeds one link's url in an iframe — for_casino marks which one, mirroring for_house
exactly, as its own independent column."""
import pytest

from app import db

BASE = "/api/owner"
LINK = {"name": "pytest link", "url": "http://192.168.0.50/", "icon": "spade"}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with db.db() as con:
        con.execute("DELETE FROM links WHERE name LIKE 'pytest%'")


def mk_link(c, name, **kw):
    return c.post(f"{BASE}/links", json={**LINK, "name": f"pytest {name}", **kw}).json()["id"]


def links_cfg(c):
    return {l["name"]: l for l in c.get(f"{BASE}/config").json()["links"] if l["name"].startswith("pytest")}


def state_links(c):
    return {l["name"]: l for l in c.get(f"{BASE}/state").json()["links"] if l["name"].startswith("pytest")}


def test_spade_is_an_allowed_link_icon(owner_client):
    lid = mk_link(owner_client, "a")
    assert links_cfg(owner_client)["pytest a"]["icon"] == "spade"
    assert "spade" in owner_client.get(f"{BASE}/config").json()["icons"]


def test_for_casino_can_be_set_on_create_and_shows_in_state(owner_client):
    mk_link(owner_client, "a", for_casino=True)
    got = state_links(owner_client)["pytest a"]
    assert got["for_casino"] is True and got["url"] == LINK["url"]


def test_only_one_link_is_for_casino_at_a_time(owner_client):
    a, b = mk_link(owner_client, "a", for_casino=True), mk_link(owner_client, "b")
    assert links_cfg(owner_client)["pytest a"]["for_casino"] is True

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest b", "for_casino": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_casino"] is True
    assert cfg["pytest a"]["for_casino"] is False           # setting b cleared a, not just added b


def test_for_casino_can_be_unset(owner_client):
    lid = mk_link(owner_client, "a", for_casino=True)
    owner_client.put(f"{BASE}/links/{lid}", json={**LINK, "name": "pytest a", "for_casino": False})
    assert links_cfg(owner_client)["pytest a"]["for_casino"] is False


def test_for_casino_defaults_off(owner_client):
    mk_link(owner_client, "a")
    assert links_cfg(owner_client)["pytest a"]["for_casino"] is False


def test_for_casino_is_independent_of_the_other_pages(owner_client):
    lid = mk_link(owner_client, "a", for_media=True, for_music=True, for_sql=True, for_ai=True, for_house=True, for_casino=True)
    other = mk_link(owner_client, "b")
    owner_client.put(f"{BASE}/links/{other}", json={**LINK, "name": "pytest b", "for_casino": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_casino"] is True
    assert cfg["pytest a"]["for_casino"] is False                # cleared, same column
    assert cfg["pytest a"]["for_media"] is True and cfg["pytest a"]["for_music"] is True and cfg["pytest a"]["for_sql"] is True
    assert cfg["pytest a"]["for_ai"] is True and cfg["pytest a"]["for_house"] is True   # the Odysseus and House pages are untouched
