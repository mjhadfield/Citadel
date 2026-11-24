"""Settings → Navigation: page names, phone tabs, desktop rail, order and the rail's groups. One set for every device, or
a device's own; the owner state carries whichever this device uses."""
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


def put(c, pages, device=False, groups=None):
    body = {"pages": pages, "device": device, **({"groups": groups} if groups is not None else {})}
    r = c.put(f"{BASE}/navigation", json=body)
    if r.status_code == 200:          # the tests read the page lists; groups are checked through cfg()
        j = r.json()
        return type("R", (), {"status_code": 200, "json": lambda self=None: {"shared": j["shared"]["pages"], "device": j["device"] and j["device"]["pages"]}})()
    return r


def cfg(c):
    return c.get(f"{BASE}/navigation").json()


def state_nav(c):
    return c.get(f"{BASE}/state").json()["navigation"]


def test_defaults_match_the_built_in_rail(owner_client):
    nav = owner_client.get(f"{BASE}/navigation").json()
    assert nav["device"] is None
    assert ids(nav["shared"]["pages"]) == [p[0] for p in navigation.PAGES]
    pages = by_id(nav["shared"]["pages"])
    assert pages["music"]["label"] == "Companion" and pages["music"]["icon"] == "disc"
    assert pages["home"]["phone"] and pages["house"]["phone"]
    assert not pages["curate"]["phone"] and not pages["audio"]["phone"] and not pages["settings"]["phone"]
    assert all(p["rail"] for p in nav["shared"]["pages"])
    assert state_nav(owner_client)["own"] is False


def test_rename_hide_and_reorder_show_in_config_and_state(owner_client):
    pages = [{"id": "home", "label": "", "phone": True}, {"id": "house", "label": "Repairs", "phone": True},
             {"id": "news", "label": "", "phone": True}, {"id": "casino", "label": "", "phone": False, "rail": False}]
    assert put(owner_client, pages).status_code == 200
    for got in (owner_client.get(f"{BASE}/config").json()["navigation"]["shared"]["pages"], state_nav(owner_client)["pages"]):
        order = ids(got)
        # Home first, then the groups (Command, Science, Engineering), each in the saved order with pages left out after
        # them; Curate + Audio + Settings last
        assert order[:4] == ["home", "news", "stats", "music"] and order[-3:] == ["curate", "audio", "settings"]
        assert order.index("casino") < order.index("house")
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
    assert by_id(nav["shared"]["pages"])["house"]["label"] == "Repairs" and by_id(nav["shared"]["pages"])["media"]["label"] == "Media"
    assert by_id(nav["device"]["pages"])["media"]["label"] == "Films" and by_id(nav["device"]["pages"])["house"]["label"] == "House"
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


# ---- desktop rail groups (Mike, 2026-11-23) ----------------------------------------------------------------------------

def test_default_groups(owner_client):
    nav = cfg(owner_client)["shared"]
    assert [g["label"] for g in nav["groups"]] == ["Command", "Science", "Engineering"]
    groups = {p["id"]: p["group"] for p in nav["pages"]}
    assert [i for i in ids(nav["pages"]) if groups[i] == "command"] == ["stats", "news", "music"]       # Fleet, News, Companion
    assert [i for i in ids(nav["pages"]) if groups[i] == "science"] == ["listen", "media", "casino", "holodeck"]
    assert [i for i in ids(nav["pages"]) if groups[i] == "engineering"] == ["labels", "house", "lab", "ai"]
    assert groups["home"] == groups["curate"] == groups["settings"] == ""
    assert state_nav(owner_client)["groups"] == nav["groups"]


def test_a_set_saved_before_groups_keeps_its_order_within_the_default_groups(owner_client):
    db.put_settings(navigation.KEY, {"pages": [{"id": i, "label": "", "phone": True, "rail": True} for i in
                                               ["news", "media", "music", "listen", "casino", "ai", "house", "stats", "labels", "holodeck"]]})
    order = ids(state_nav(owner_client)["pages"])
    assert order == ["home", "news", "music", "stats", "media", "listen", "casino", "holodeck", "ai", "house", "labels",
                     "lab", "curate", "audio", "settings"]


def test_moving_a_page_to_another_group_and_ungrouping(owner_client):
    put(owner_client, [{"id": "house", "label": "", "phone": True, "group": "command"},
                       {"id": "media", "label": "", "phone": True, "group": ""}])
    nav = cfg(owner_client)["shared"]
    order, pages = ids(nav["pages"]), by_id(nav["pages"])
    assert order[:2] == ["home", "media"] and pages["media"]["group"] == ""      # no group: straight under Home
    assert pages["house"]["group"] == "command" and order.index("house") < order.index("listen")


def test_rename_reorder_add_and_drop_groups(owner_client):
    put(owner_client, [{"id": "ai", "label": "", "phone": True, "group": "lab"}],
        groups=[{"id": "engineering", "label": "Eng"}, {"id": "lab", "label": "Lab"}])
    nav = cfg(owner_client)["shared"]
    assert nav["groups"] == [{"id": "engineering", "label": "Eng"}, {"id": "lab", "label": "Lab"}]
    pages = by_id(nav["pages"])
    assert pages["ai"]["group"] == "lab" and pages["news"]["group"] == ""          # Command was dropped
    order = ids(nav["pages"])
    assert order.index("news") < order.index("labels") < order.index("ai")
    # saving the pages alone keeps the groups
    put(owner_client, [{"id": "news", "label": "Headlines", "phone": True}])
    assert [g["id"] for g in cfg(owner_client)["shared"]["groups"]] == ["engineering", "lab"]
    # and Reset brings the defaults back
    put(owner_client, [])
    assert [g["id"] for g in cfg(owner_client)["shared"]["groups"]] == ["command", "science", "engineering"]


def test_no_groups_at_all(owner_client):
    put(owner_client, [], groups=[])
    nav = cfg(owner_client)["shared"]
    assert nav["groups"] == [] and all(p["group"] == "" for p in nav["pages"])


def test_a_devices_own_groups(owner_client):
    put(owner_client, [], device=True, groups=[{"id": "all", "label": "All"}])
    nav = cfg(owner_client)
    assert [g["id"] for g in nav["device"]["groups"]] == ["all"]
    assert [g["id"] for g in nav["shared"]["groups"]] == ["command", "science", "engineering"]


def test_bad_groups_are_refused(owner_client):
    assert put(owner_client, [], groups=[{"id": "Bad Id", "label": "x"}]).status_code == 422
    assert put(owner_client, [], groups=[{"id": "ok", "label": ""}]).status_code == 422
    assert put(owner_client, [], groups=[{"id": f"g{i}", "label": "x"} for i in range(9)]).status_code == 422
