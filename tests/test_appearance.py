"""Appearance: custom themes (a Day half and a Night half, built on Citadel or LCARS) and their background pictures."""
import os
import time

import pytest

from app import appearance, db

BASE = "/api/owner/appearance"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 64


@pytest.fixture(autouse=True)
def _clean(client):
    with db.db() as con:
        con.execute("DELETE FROM widget_settings WHERE widget = 'appearance'")
    folder = appearance.bg_dir()
    if folder.exists():
        for f in folder.iterdir():
            f.unlink()
    yield


def half(**kw):
    return {"accent": "#3B82F6", "panel": "#0a1020", "panel_opacity": 80,
            "background": {"file": "", "fit": "cover", "dim": 40, "glow": True}, **kw}


def theme(day=None, night=None, **kw):
    return {"name": "Midnight", "skin": "citadel", "day": day or half(accent="#B5541F", panel="#f8f0e2"), "night": night or half(), **kw}


def test_a_theme_is_cleaned_and_each_half_defaults_to_its_mode():
    t = appearance.clean({"name": " Sunset ", "skin": "lcars"})
    assert t["name"] == "Sunset" and t["skin"] == "lcars"
    for mode in ("day", "night"):
        assert t[mode]["accent"] == appearance.BASE_ACCENT[mode] and t[mode]["panel"] == appearance.BASE_PANEL[mode]
        assert t[mode]["panel_opacity"] == 100 and t[mode]["background"] == {"file": "", "fit": "cover", "dim": 40, "glow": True}
    assert appearance.clean({"name": "x"})["skin"] == "citadel"
    assert appearance.clean(theme(night=half(panel_opacity=5, background={"dim": 400})))["night"]["panel_opacity"] == 30   # clamped, not refused
    assert appearance.clean(theme())["night"]["accent"] == "#3b82f6"


def test_a_theme_saved_before_the_day_night_split_is_read_as_citadel_with_its_old_mode_kept():
    old = {"id": "abc", "name": "Old", "base": "light", "accent": "#123456", "panel": "#abcdef", "panel_opacity": 70,
           "background": {"file": "", "fit": "tile", "dim": 20, "glow": False}}
    t = appearance.clean(old)
    assert t["skin"] == "citadel"
    assert t["day"] == {"accent": "#123456", "panel": "#abcdef", "panel_opacity": 70, "background": {"file": "", "fit": "tile", "dim": 20, "glow": False}}
    assert t["night"]["accent"] == appearance.BASE_ACCENT["night"]
    db.put_settings(appearance.KEY, {"themes": [old]})          # and straight out of storage, as every screen reads it
    stored = appearance.themes()[0]
    assert stored["id"] == "abc" and stored["day"]["accent"] == "#123456" and "base" not in stored


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"skin": "blue"}, {"night": half(accent="red")}, {"day": half(accent="#12345")}, {"night": half(panel="url(x)")},
    {"day": half(background={"fit": "stretch"})}, {"night": half(background={"file": "../hub.sqlite"})},
    {"day": half(background={"file": "0" * 32 + ".png"})},   # not uploaded
    {"night": half(panel_opacity="lots")}, {"day": "bright"},
])
def test_bad_themes_are_refused(bad):
    with pytest.raises(appearance.AppearanceError):
        appearance.clean(theme(**bad))


def test_create_edit_delete_through_the_api_and_every_screen_sees_them(owner_client):
    r = owner_client.post(f"{BASE}/themes", json=theme())
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert owner_client.get("/api/owner/state").json()["appearance"]["themes"][0]["name"] == "Midnight"
    cfg = owner_client.get("/api/owner/config").json()["appearance"]
    assert cfg["themes"][0]["id"] == tid and cfg["skins"] == ["citadel", "lcars"] and set(cfg["base_accent"]) == {"day", "night"}
    assert owner_client.put(f"{BASE}/themes/{tid}", json=theme(name="Midnight 2", skin="lcars")).json()["skin"] == "lcars"
    assert owner_client.put(f"{BASE}/themes/{tid}", json=theme(night=half(accent="nope"))).status_code == 422
    assert owner_client.put(f"{BASE}/themes/nope", json=theme()).status_code == 404
    assert owner_client.delete(f"{BASE}/themes/{tid}").status_code == 200
    assert owner_client.get("/api/owner/state").json()["appearance"]["themes"] == []


def test_guests_cannot_change_themes(guest_client):
    assert guest_client.post(f"{BASE}/themes", json=theme(), headers={"X-Hub": "1"}).status_code == 403


def test_background_upload_checks_the_bytes_and_serves_owner_only_and_sandboxed(owner_client, guest_client):
    r = owner_client.post(f"{BASE}/backgrounds", content=JPG, headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 201, r.text
    name = r.json()["file"]
    assert name.endswith(".jpg")
    # an SVG (could carry script) or a page pretending to be a picture is refused, whatever it claims to be
    assert owner_client.post(f"{BASE}/backgrounds", content=b"<svg onload=alert(1)></svg>", headers={"Content-Type": "image/png"}).status_code == 422
    assert owner_client.post(f"{BASE}/backgrounds", content=b"<html></html>", headers={"Content-Type": "image/jpeg"}).status_code == 422
    assert owner_client.post(f"{BASE}/backgrounds", content=b"", headers={"Content-Type": "image/png"}).status_code == 422
    got = owner_client.get(f"/backgrounds/{name}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg" and "sandbox" in got.headers["content-security-policy"]
    assert guest_client.get(f"/backgrounds/{name}").status_code == 403
    for bad in ("../hub.sqlite", "secret.key", name.upper(), f"{name}.html"):
        assert owner_client.get(f"/backgrounds/{bad}").status_code == 404
    # and one half of a theme can now use it
    t = owner_client.post(f"{BASE}/themes", json=theme(night=half(background={"file": name, "fit": "tile", "dim": 25, "glow": False}))).json()
    assert t["night"]["background"] == {"file": name, "fit": "tile", "dim": 25, "glow": False}
    assert t["day"]["background"]["file"] == ""


def test_oversized_pictures_are_refused(owner_client, monkeypatch):
    monkeypatch.setattr(appearance, "MAX_BACKGROUND", 100)
    assert owner_client.post(f"{BASE}/backgrounds", content=PNG + b"\x00" * 200, headers={"Content-Type": "image/png"}).status_code == 413


def test_unused_pictures_are_tidied_but_either_half_keeps_its_picture_and_a_fresh_upload_is_spared():
    day_pic, night_pic = appearance.store_background(PNG), appearance.store_background(PNG + b"\x02")
    fresh, old = appearance.store_background(JPG), appearance.store_background(PNG + b"\x01")
    appearance.save_theme(theme(day=half(background={"file": day_pic}), night=half(background={"file": night_pic})))
    week_ago = time.time() - 7 * 86400
    for n in (day_pic, night_pic, old):
        os.utime(appearance.bg_dir() / n, (week_ago, week_ago))
    appearance.tidy()
    assert {f.name for f in appearance.bg_dir().iterdir()} == {day_pic, night_pic, fresh}   # only the old unused one went
