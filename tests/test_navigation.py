"""Settings → Navigation: page names, phone tabs, desktop rail and order. One set for every device, or a device's own;
the owner state carries whichever this device uses."""
import pytest

from app import auth, db, navigation

BASE = "/api/owner"


@pytest.fixture(autouse=True)
def _reset():
    db.put_settings(navigation.KEY, {})
    yield
    db.put_settings(navigation.KEY, {})


def ids(pages):
    return [p["id"] for p in pages]


def by_id(pages):
    return {p["id"]: p for p in pages}


def put(c, pages, device=False):
    return c.put(f"{BASE}/navigation", json={"pages": pages, "device": device})


def state_nav(c):
    return c.get(f"{BASE}/state").json()["navigation"]


def test_defaults_match_the_built_in_rail(owner_client):
    nav = owner_client.get(f"{BASE}/navigation").json()
    assert nav["device"] is None
    assert ids(nav["shared"]) == [p[0] for p in navigation.PAGES]
    pages = by_id(nav["shared"])
    assert pages["music"]["label"] == "Companion" and pages["music"]["icon"] == "disc"
    assert pages["home"]["phone"] and pages["house"]["phone"]
    assert not pages["curate"]["phone"] and not pages["audio"]["phone"] and not pages["settings"]["phone"]
    assert all(p["rail"] for p in nav["shared"])
    assert state_nav(owner_client)["own"] is False


def test_rename_hide_and_reorder_show_in_config_and_state(owner_client):
    pages = [{"id": "home", "label": "", "phone": True}, {"id": "house", "label": "Repairs", "phone": True},
             {"id": "news", "label": "", "phone": True}, {"id": "casino", "label": "", "phone": False, "rail": False}]
    assert put(owner_client, pages).status_code == 200
    for got in (owner_client.get(f"{BASE}/config").json()["navigation"]["shared"], state_nav(owner_client)["pages"]):
        order = ids(got)
        # Home first, the moved pages in their new order, pages left out after them, Curate + Audio + Settings last
        assert order[:3] == ["home", "house", "news"] and order[-3:] == ["curate", "audio", "settings"]
        assert set(order) == {p[0] for p in navigation.PAGES}
        pages_ = by_id(got)
        assert pages_["house"]["label"] == "Repairs" and pages_["house"]["default"] == "House"
        assert pages_["casino"]["phone"] is False and pages_["casino"]["rail"] is False
        assert pages_["media"]["phone"] is True and pages_["media"]["rail"] is True


def test_home_and_settings_cant_leave_the_rail_and_home_keeps_its_phone_tab(owner_client):
    nav = put(owner_client, [{"id": "news", "label": "", "phone": True},
                             {"id": "home", "label": "Base", "phone": False, "rail": False},
                             {"id": "settings", "label": "", "phone": False, "rail": False}]).json()["shared"]
    assert ids(nav)[0] == "home"
    home, settings = by_id(nav)["home"], by_id(nav)["settings"]
    assert home["label"] == "Base" and home["phone"] is True and home["rail"] is True
    assert settings["rail"] is True and settings["phone"] is False


def test_a_device_can_have_its_own_set(owner_client):
    put(owner_client, [{"id": "house", "label": "Repairs", "phone": True}])
    put(owner_client, [{"id": "media", "label": "Films", "phone": False}], device=True)
    nav = owner_client.get(f"{BASE}/navigation").json()
    assert by_id(nav["shared"])["house"]["label"] == "Repairs" and by_id(nav["shared"])["media"]["label"] == "Media"
    assert by_id(nav["device"])["media"]["label"] == "Films" and by_id(nav["device"])["house"]["label"] == "House"
    st = state_nav(owner_client)
    assert st["own"] is True and by_id(st["pages"])["media"]["phone"] is False
    # and back to every device's
    nav = owner_client.delete(f"{BASE}/navigation/device").json()
    assert nav["device"] is None
    st = state_nav(owner_client)
    assert st["own"] is False and by_id(st["pages"])["house"]["label"] == "Repairs"


def test_another_device_doesnt_see_this_ones_own_set(owner_client):
    put(owner_client, [{"id": "media", "label": "Films", "phone": False}], device=True)
    assert by_id(navigation.settings("someone-else")["pages"])["media"]["label"] == "Media"


def test_revoking_a_device_drops_its_own_set(owner_client):
    token = auth.create_enrol_token("owner", "pytest phone")
    other = auth.redeem_enrol_token(token).split(".")[0]
    navigation.save([{"id": "media", "label": "Films", "phone": False, "rail": True}], other)
    assert navigation.settings(other)["own"] is True
    assert owner_client.delete(f"{BASE}/devices/{other}").status_code == 200
    assert other not in db.get_settings(navigation.KEY, {}).get("devices", {})


def test_default_names_are_not_stored(owner_client):
    put(owner_client, [{"id": "news", "label": "News", "phone": True}, {"id": "ai", "label": "  ", "phone": True}])
    saved = {p["id"]: p for p in db.get_settings(navigation.KEY, {})["pages"]}
    assert saved["news"]["label"] == "" and saved["ai"]["label"] == ""


def test_unknown_pages_and_duplicates_are_ignored(owner_client):
    nav = put(owner_client, [{"id": "nope", "label": "x", "phone": True}, {"id": "news", "label": "A", "phone": True},
                             {"id": "news", "label": "B", "phone": False}]).json()["shared"]
    assert "nope" not in ids(nav) and by_id(nav)["news"]["label"] == "A"


def test_long_names_are_refused(owner_client):
    assert put(owner_client, [{"id": "news", "label": "x" * 17, "phone": True}]).status_code == 422


def test_reset_with_an_empty_list(owner_client):
    put(owner_client, [{"id": "house", "label": "Repairs", "phone": False}])
    nav = put(owner_client, []).json()["shared"]
    assert ids(nav) == [p[0] for p in navigation.PAGES] and by_id(nav)["house"]["label"] == "House"


def test_guests_cannot_change_it(guest_client):
    assert guest_client.put(f"{BASE}/navigation", json={"pages": []}).status_code in (401, 403)
