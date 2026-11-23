"""Appearance: themes, families, built-in and custom styles (Day + Night halves, Advanced token overrides),
legacy storage migration, JS/Python registry parity, and background pictures."""
import json
import os
import re
import time
from pathlib import Path

import pytest

from app import appearance, db

BASE = "/api/owner/appearance"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 64


@pytest.fixture(autouse=True)
def _clean(client):
    with db.db() as con:
        con.execute("DELETE FROM widget_settings WHERE widget = 'appearance'")
    db.put_settings(appearance.KEY, {"styles": []})
    folder = appearance.bg_dir()
    if folder.exists():
        for f in folder.iterdir():
            f.unlink()
    yield


def half(**kw):
    return {"accent": "#3B82F6", "panel": "#0a1020", "panel_opacity": 80,
            "background": {"file": "", "fit": "cover", "dim": 40, "glow": True}, **kw}


def style(day=None, night=None, **kw):
    return {"name": "Midnight", "family": "citadel", "day": day or half(accent="#B5541F", panel="#f8f0e2"), "night": night or half(), **kw}


def test_a_style_is_cleaned_and_each_half_defaults_to_its_family():
    s = appearance.clean({"name": " Sunset ", "family": "lcars"})
    assert s["name"] == "Sunset" and s["family"] == "lcars"
    for mode in ("day", "night"):
        assert {k: s[mode][k] for k in ("accent", "secondary", "third", "panel")} == appearance.BASE["lcars"][mode]
        assert s[mode]["panel_opacity"] == 100 and s[mode]["background"] == {"file": "", "fit": "cover", "dim": 40, "glow": True}
        assert s[mode]["tokens"] == {}
    assert appearance.clean({"name": "x"})["night"]["accent"] == appearance.BASE_ACCENT["night"]
    assert appearance.clean({"name": "x"})["family"] == "citadel"
    assert appearance.clean(style(night=half(panel_opacity=5, background={"dim": 400})))["night"]["panel_opacity"] == 30
    assert appearance.clean(style())["night"]["accent"] == "#3b82f6"
    b = appearance.clean({"name": "Unimatrix", "family": "borg", "night": {"tokens": {"--brg-axis": "#E5B23A"}}})
    assert b["family"] == "borg" and b["night"]["accent"] == appearance.BASE["borg"]["night"]["accent"]
    assert b["night"]["tokens"] == {"--brg-axis": "#e5b23a"}
    # Legacy skin field (including lcars2 -> lcars) upgrades cleanly
    b2 = appearance.clean({"name": "Stargazer", "skin": "lcars2"})
    assert b2["family"] == "lcars" and "skin" not in b2
    assert b2["night"]["accent"] == appearance.BASE["lcars"]["night"]["accent"]


def test_old_pre_day_night_theme_upgrades_to_citadel_style():
    old = {"id": "abc", "name": "Old", "base": "light", "accent": "#123456", "panel": "#abcdef", "panel_opacity": 70,
           "background": {"file": "", "fit": "tile", "dim": 20, "glow": False}}
    s = appearance.clean(old)
    assert s["family"] == "citadel"
    assert s["day"] == {"accent": "#123456", "panel": "#abcdef", "panel_opacity": 70, "background": {"file": "", "fit": "tile", "dim": 20, "glow": False},
                        "secondary": appearance.BASE["citadel"]["day"]["secondary"], "third": appearance.BASE["citadel"]["day"]["third"], "tokens": {}}
    assert s["night"]["accent"] == appearance.BASE_ACCENT["night"]
    db.put_settings(appearance.KEY, {"themes": [old]})
    stored = appearance.styles()[0]
    assert stored["id"] == "abc" and stored["family"] == "citadel" and stored["day"]["accent"] == "#123456" and "base" not in stored


def test_old_setting_drops_unchanged_seeded_presets_and_keeps_edited_ones_as_custom():
    com_builtin = next(x for x in appearance.BUILTIN_STYLES["lcars"] if x["id"] == "com")
    sci_builtin = next(x for x in appearance.BUILTIN_STYLES["lcars"] if x["id"] == "sci")
    edited_sci = {
        "id": "sci111111111",
        "name": "LCARS (SCI)",
        "skin": "lcars",
        "day": sci_builtin["day"],
        "night": {**sci_builtin["night"], "accent": "#00ffaa"},
    }
    unchanged_com = {
        "id": "com111111111",
        "name": "LCARS (COM)",
        "skin": "lcars",
        "day": com_builtin["day"],
        "night": com_builtin["night"],
    }
    lcars2_custom = {
        "id": "usr222222222",
        "name": "Stargazer",
        "skin": "lcars2",
        "day": half(),
        "night": half(),
    }
    db.put_settings(appearance.KEY, {
        "themes": [unchanged_com, edited_sci, lcars2_custom],
        "seeded_presets": ["command", "eng", "ent", "sci"],
    })
    upgraded = appearance.styles()
    assert [s["name"] for s in upgraded] == ["LCARS (SCI) (custom)", "Stargazer"]
    assert all(s["family"] == "lcars" and "skin" not in s for s in upgraded)
    assert appearance.migrated_presets() == {"com111111111": "com"}

    # Persisting via migrate_storage writes the new shape and is idempotent
    res = appearance.migrate_storage()
    assert res == {"styles": 2, "dropped_presets": 1}
    raw = db.get_settings(appearance.KEY)
    assert "themes" not in raw and "seeded_presets" not in raw
    assert [s["name"] for s in raw["styles"]] == ["LCARS (SCI) (custom)", "Stargazer"]
    assert raw["migrated_presets"] == {"com111111111": "com"}
    assert appearance.migrate_storage() == {}


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"family": "blue"}, {"family": "lcars25"}, {"night": half(accent="red")}, {"day": half(accent="#12345")}, {"night": half(panel="url(x)")},
    {"day": half(background={"fit": "stretch"})}, {"night": half(background={"file": "../hub.sqlite"})},
    {"day": half(background={"file": "0" * 32 + ".png"})},   # not uploaded
    {"night": half(panel_opacity="lots")}, {"day": "bright"},
    {"night": half(secondary="blue")}, {"day": half(third="#1234567")},
    {"night": half(tokens=["--bg"])}, {"night": half(tokens={"--nope": "#000000"})}, {"day": half(tokens={"--bg": "red"})},
    {"night": half(tokens={"--bg": "#12345"})}, {"night": half(tokens={"--bg": 7})},
])
def test_bad_styles_are_refused(bad):
    with pytest.raises(appearance.AppearanceError):
        appearance.clean(style(**bad))


def test_advanced_colours_are_kept_lower_cased_and_may_be_see_through():
    s = appearance.clean(style(night=half(secondary="#AA0000", tokens={"--bg": "#0A0606", "--edge": "#C9CED621"})))
    assert s["night"]["secondary"] == "#aa0000"
    assert s["night"]["tokens"] == {"--bg": "#0a0606", "--edge": "#c9ced621"}


def test_too_many_advanced_colours_are_refused():
    many = {f"--x{i}": "#000000" for i in range(len(appearance.TOKENS) + 1)}
    with pytest.raises(appearance.AppearanceError):
        appearance.clean(style(night=half(tokens=many)))


def test_the_token_list_and_base_match_palette_js():
    js = (Path(__file__).parent.parent / "web/js/palette.js").read_text()
    block = js[js.index("export const TOKENS"):js.index("export const GROUPS")]
    assert tuple(re.findall(r"\['(--[a-z0-9-]+)'", block)) == appearance.TOKENS
    # BASE keys match FAMILIES (no lcars2)
    base_block = js[js.index("export const BASE"):js.index("// ---- colour maths")]
    assert set(re.findall(r"^\s{2}([a-z0-9]+):", base_block, re.M)) == set(appearance.FAMILIES)
    assert "lcars2" not in base_block


def test_themes_registry_matches_themes_js():
    js = (Path(__file__).parent.parent / "web/js/themes.js").read_text()
    # FAMILIES
    fam_m = re.search(r"export const FAMILIES = \[([^\]]+)\]", js)
    assert fam_m
    assert tuple(re.findall(r"'([^']+)'", fam_m.group(1))) == appearance.FAMILIES
    # THEMES
    themes_block = js[js.index("export const THEMES"):js.index("const NO_PICTURE")]
    js_themes = [
        {"id": m.group(1), "name": m.group(2), "family": m.group(3)}
        for m in re.finditer(r"\{\s*id:\s*'([^']+)',\s*name:\s*'([^']+)',\s*family:\s*'([^']+)'\s*\}", themes_block)
    ]
    assert tuple(js_themes) == appearance.THEMES
    # BUILTIN_STYLES ids & names per family
    bs_block = js[js.index("export const BUILTIN_STYLES"):js.index("const THEME_BY_ID")]
    for fam in appearance.FAMILIES:
        fam_m = re.search(rf"^\s{{2}}{fam}:\s*\[(.*?)^\s{{2}}\],", bs_block, re.M | re.S)
        assert fam_m, f"missing {fam} in themes.js BUILTIN_STYLES"
        ids = re.findall(r"id:\s*'([^']+)'", fam_m.group(1))
        names = re.findall(r"name:\s*'([^']+)'", fam_m.group(1))
        py_list = appearance.BUILTIN_STYLES[fam]
        assert ids == [x["id"] for x in py_list]
        assert names == [x["name"] for x in py_list]
        # Every accent colour in BUILTIN_STYLES matches too
        accents = re.findall(r"accent:\s*'([^']+)'", fam_m.group(1))
        py_accents = [c for x in py_list for c in (x["night"]["accent"], x["day"]["accent"])]
        assert accents == py_accents


def test_every_builtin_style_is_valid():
    for fam, items in appearance.BUILTIN_STYLES.items():
        for item in items:
            appearance.clean({**item, "family": fam})


def test_create_edit_delete_through_the_api_and_every_screen_sees_them(owner_client):
    r = owner_client.post(f"{BASE}/styles", json=style())
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert owner_client.get("/api/owner/state").json()["appearance"]["styles"][0]["name"] == "Midnight"
    cfg = owner_client.get("/api/owner/config").json()["appearance"]
    assert cfg["styles"][0]["id"] == sid and cfg["families"] == ["citadel", "lcars", "borg"] and set(cfg["base_accent"]) == {"day", "night"}
    assert [t["id"] for t in cfg["themes"]] == ["citadel", "lcars", "lcars25", "borg"]
    assert cfg["base"]["lcars"]["night"]["accent"] == "#a78bfa" and cfg["base"]["borg"]["night"]["accent"] == "#2ee86c"
    assert "lcars2" not in cfg["base"]
    assert [s["id"] for s in cfg["builtin_styles"]["lcars"]] == ["standard", "com", "sci", "eng", "ent"]
    assert owner_client.put(f"{BASE}/styles/{sid}", json=style(name="Midnight 2", family="lcars")).json()["family"] == "lcars"
    assert owner_client.put(f"{BASE}/styles/{sid}", json=style(name="Unimatrix", family="borg")).json()["family"] == "borg"
    assert owner_client.put(f"{BASE}/styles/{sid}", json=style(night=half(accent="nope"))).status_code == 422
    assert owner_client.put(f"{BASE}/styles/nope", json=style()).status_code == 404
    assert owner_client.delete(f"{BASE}/styles/{sid}").status_code == 200
    assert owner_client.get("/api/owner/state").json()["appearance"]["styles"] == []


def test_guests_cannot_change_styles(guest_client):
    assert guest_client.post(f"{BASE}/styles", json=style(), headers={"X-Hub": "1"}).status_code == 403


def test_background_upload_checks_the_bytes_and_serves_owner_only_and_sandboxed(owner_client, guest_client):
    r = owner_client.post(f"{BASE}/backgrounds", content=JPG, headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 201, r.text
    name = r.json()["file"]
    assert name.endswith(".jpg")
    assert owner_client.post(f"{BASE}/backgrounds", content=b"<svg onload=alert(1)></svg>", headers={"Content-Type": "image/png"}).status_code == 422
    assert owner_client.post(f"{BASE}/backgrounds", content=b"<html></html>", headers={"Content-Type": "image/jpeg"}).status_code == 422
    assert owner_client.post(f"{BASE}/backgrounds", content=b"", headers={"Content-Type": "image/png"}).status_code == 422
    got = owner_client.get(f"/backgrounds/{name}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg" and "sandbox" in got.headers["content-security-policy"]
    assert guest_client.get(f"/backgrounds/{name}").status_code == 403
    for bad in ("../hub.sqlite", "secret.key", name.upper(), f"{name}.html"):
        assert owner_client.get(f"/backgrounds/{bad}").status_code == 404
    s = owner_client.post(f"{BASE}/styles", json=style(night=half(background={"file": name, "fit": "tile", "dim": 25, "glow": False}))).json()
    assert s["night"]["background"] == {"file": name, "fit": "tile", "dim": 25, "glow": False}
    assert s["day"]["background"]["file"] == ""


def test_oversized_pictures_are_refused(owner_client, monkeypatch):
    monkeypatch.setattr(appearance, "MAX_BACKGROUND", 100)
    assert owner_client.post(f"{BASE}/backgrounds", content=PNG + b"\x00" * 200, headers={"Content-Type": "image/png"}).status_code == 413


def test_unused_pictures_are_tidied_but_either_half_keeps_its_picture_and_a_fresh_upload_is_spared():
    day_pic, night_pic = appearance.store_background(PNG), appearance.store_background(PNG + b"\x02")
    fresh, old = appearance.store_background(JPG), appearance.store_background(PNG + b"\x01")
    appearance.save_style(style(day=half(background={"file": day_pic}), night=half(background={"file": night_pic})))
    week_ago = time.time() - 7 * 86400
    for n in (day_pic, night_pic, old):
        os.utime(appearance.bg_dir() / n, (week_ago, week_ago))
    appearance.tidy()
    assert {f.name for f in appearance.bg_dir().iterdir()} == {day_pic, night_pic, fresh}


def _css_vars_in_block(css_text: str, selector: str) -> dict[str, str]:
    m = re.search(rf"^{re.escape(selector)}\s*\{{(.*?)^\}}", css_text, re.M | re.S)
    assert m, f"selector {selector!r} not found"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


def test_kit_tokens_matches_citadel_palettes():
    root = Path(__file__).parent.parent / "web"
    kit_css = (root / "kit/kit-tokens.css").read_text()
    checks = [
        ("css/tokens.css", ":root"),
        ("css/tokens.css", ':root[data-mode="day"]'),
        ("css/lcars.css", 'html[data-family="lcars"]'),
        ("css/lcars.css", 'html[data-family="lcars"][data-mode="day"]'),
        ("css/borg.css", 'html[data-family="borg"]'),
        ("css/borg.css", 'html[data-family="borg"][data-mode="day"]'),
    ]
    for rel, sel in checks:
        src_vars = _css_vars_in_block((root / rel).read_text(), sel)
        kit_vars = _css_vars_in_block(kit_css, sel)
        for token in appearance.TOKENS:
            if token in src_vars:
                assert kit_vars.get(token) == src_vars[token], f"{rel} {sel} {token}: kit={kit_vars.get(token)!r} != src={src_vars[token]!r}"


def test_sync_kit_copies_and_checks_without_touching_outside(tmp_path):
    import sys
    tools_dir = str(Path(__file__).parent.parent / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import sync_kit

    app_web = tmp_path / "web"
    keep_file = app_web / "keep.txt"
    keep_file.parent.mkdir(parents=True)
    keep_file.write_text("untouched")
    vendor = app_web / "vendor" / "citadel-kit"

    assert sync_kit.main(["--check", "--target-dir", f"holodeck={vendor}", "holodeck"]) == 1
    assert sync_kit.main(["--target-dir", f"holodeck={vendor}", "holodeck"]) == 0
    assert (vendor / "kit.css").is_file() and (vendor / "kit.js").is_file() and not (vendor / "demo.html").exists()
    assert sync_kit.main(["--check", "--target-dir", f"holodeck={vendor}", "holodeck"]) == 0
    assert keep_file.read_text() == "untouched"

    # A stale file inside vendor is cleaned up; outside files are untouched
    (vendor / "stale.txt").write_text("old")
    assert sync_kit.main(["--check", "--target-dir", f"holodeck={vendor}", "holodeck"]) == 1
    assert sync_kit.main(["--target-dir", f"holodeck={vendor}", "holodeck"]) == 0
    assert not (vendor / "stale.txt").exists() and keep_file.read_text() == "untouched"
