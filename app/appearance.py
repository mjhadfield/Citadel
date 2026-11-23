"""Appearance: themes, families, built-in and custom styles, and background pictures (Settings → Appearance).

A device chooses three things on its own (kept in its browser):
  - mode:   Day or Night ('day' | 'night')
  - theme:  page design — layout, shapes, fonts, motion ('citadel' | 'lcars' | 'lcars25' | 'borg')
  - style:  colour variant for that theme's family ('citadel' | 'lcars' | 'borg') — either a built-in
            style (Standard for every family, plus COM/SCI/ENG/ENT for lcars) or a custom style ('c:<id>').

LCARS and LCARS 25 share the 'lcars' family, so built-in LCARS styles and custom LCARS styles work on both.
Every style has a Day half and a Night half. Each half has four Basic colours (primary, kept as `accent` for
older entries; secondary; third; panel), how see-through the panels are, an optional background picture, and
`tokens`: Advanced overrides of single colour tokens. The browser derives every other token from the Basic
colours (web/js/palette.js, whose TOKENS list mirrors this file; web/js/themes.js mirrors THEMES, FAMILIES
and BUILTIN_STYLES — tests/test_appearance.py asserts they stay in sync).

Entries saved in older shapes (pre-Day/Night `{base, ...}`, or `{themes: [...], seeded_presets: [...]}` with
`skin`) are read through `_upgrade_setting` / `_upgrade_entry`. Unchanged copies of the previously seeded
LCARS presets are dropped (recording their old UUID -> built-in id in `migrated_presets` so devices switch
seamlessly); edited copies stay as custom styles named "<name> (custom)".

Background pictures are uploaded to the data folder and served by Citadel itself (see main.py), because the
page's CSP only lets the browser load images from a few named CDNs.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

from . import config, db

KEY = "appearance"
DEFAULTS: dict[str, Any] = {"styles": []}
MAX_STYLES = 20
MAX_BACKGROUND = 15_000_000                      # bytes — a big phone photo is well under this
FITS = ("cover", "contain", "tile", "center")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TOKEN_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")      # Advanced: #rrggbb or #rrggbbaa (see-through)
ID_RE = re.compile(r"^[a-z0-9]{1,24}$")
BG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
FAMILIES = ("citadel", "lcars", "borg")
THEMES: tuple[dict[str, str], ...] = (
    {"id": "citadel", "name": "Citadel", "family": "citadel"},
    {"id": "lcars", "name": "LCARS", "family": "lcars"},
    {"id": "lcars25", "name": "LCARS 25", "family": "lcars"},
    {"id": "borg", "name": "Borg", "family": "borg"},
)
MODES = ("day", "night")
BASE_PANEL = {"night": "#0e0b14", "day": "#f8f0e2"}       # Citadel's built-in --surface
BASE_ACCENT = {"night": "#8b5cf6", "day": "#b5541f"}
# Each family's four Basic colours (palette.js BASE): what a missing one falls back to.
BASE: dict[str, dict[str, dict[str, str]]] = {
    "citadel": {"night": {"accent": "#8b5cf6", "secondary": "#c084fc", "third": "#67e8f9", "panel": "#0e0b14"},
                "day": {"accent": "#b5541f", "secondary": "#8a2e2e", "third": "#3d5a73", "panel": "#f8f0e2"}},
    "lcars": {"night": {"accent": "#a78bfa", "secondary": "#8577e0", "third": "#d3a4ea", "panel": "#0e0b14"},
              "day": {"accent": "#b5541f", "secondary": "#8a2e2e", "third": "#3d5a73", "panel": "#f8f0e2"}},
    "borg": {"night": {"accent": "#2ee86c", "secondary": "#d8a328", "third": "#26c6b8", "panel": "#060e08"},
             "day": {"accent": "#167a3e", "secondary": "#9c6d14", "third": "#186b6e", "panel": "#f3f7f4"}},
}
# The colour tokens Advanced may set (palette.js TOKENS, same order; tests/test_appearance.py checks they match).
TOKENS: tuple[str, ...] = (
    "--bg", "--bg-2", "--surface", "--surface-2", "--surface-3", "--well", "--vignette",
    "--accent", "--accent-hi", "--accent-soft", "--accent-deep", "--accent-wash", "--glow-2",
    "--silver-hi", "--silver", "--silver-mute", "--silver-dim", "--edge", "--edge-strong",
    "--ok", "--warn", "--bad", "--heat-1", "--heat-2", "--heat-3",
    "--series-1", "--series-2", "--series-3", "--series-4", "--series-5",
    "--lc-frame", "--lc-1", "--lc-2", "--lc-3", "--lc-4", "--lc-5", "--lc-6", "--lc-7", "--lc-red",
    "--lc-gig", "--lc-gig-on", "--lc-diary", "--lc-on",
    "--lc-hd-frame", "--lc-hd-1", "--lc-hd-2", "--lc-hd-3", "--lc-hd-4",
    "--brg-node", "--brg-axis", "--brg-cyan", "--brg-sub", "--brg-on",
)
_NO_PICTURE = {"file": "", "fit": "cover", "dim": 40, "glow": True}


def _standard_style(family: str) -> dict[str, Any]:
    return {
        "id": "standard",
        "name": "Standard",
        "night": {**BASE[family]["night"], "panel_opacity": 100, "background": dict(_NO_PICTURE), "tokens": {}},
        "day": {**BASE[family]["day"], "panel_opacity": 100, "background": dict(_NO_PICTURE), "tokens": {}},
    }


# Built-in styles per family (mirrored in web/js/themes.js BUILTIN_STYLES; tests/test_appearance.py asserts parity).
# LCARS COM/SCI/ENG/ENT moved here from the old seeded PRESETS so they work on both LCARS and LCARS 25.
BUILTIN_STYLES: dict[str, list[dict[str, Any]]] = {
    "citadel": [_standard_style("citadel")],
    "lcars": [
        _standard_style("lcars"),
        # COM: red, the command division's colour.
        {"id": "com", "name": "COM",
         "night": {"accent": "#e02b2b", "secondary": "#ff7a1f", "third": "#f5a524", "panel": "#140909", "panel_opacity": 100,
                   "background": dict(_NO_PICTURE),
                   "tokens": {"--bg": "#0b0404", "--lc-4": "#d9a07a", "--lc-7": "#ffd866", "--lc-gig": "#a8141e",
                              "--lc-hd-frame": "#c41f1f"}},
         "day": {"accent": "#a8191c", "secondary": "#bf3a12", "third": "#3d5a73", "panel": "#f8f0e2", "panel_opacity": 100,
                 "background": dict(_NO_PICTURE),
                 "tokens": {"--lc-4": "#7d6b52", "--lc-gig": "#6e1212"}}},
        # SCI: sciences blue.
        {"id": "sci", "name": "SCI",
         "night": {"accent": "#2f72ff", "secondary": "#22c3ff", "third": "#25c4a4", "panel": "#070b14", "panel_opacity": 100,
                   "background": dict(_NO_PICTURE),
                   "tokens": {"--bg": "#03060c", "--lc-4": "#9db3d1", "--lc-gig": "#2c55c9", "--lc-hd-frame": "#1f5be0"}},
         "day": {"accent": "#1d4f91", "secondary": "#1f6f86", "third": "#b5541f", "panel": "#f8f0e2", "panel_opacity": 100,
                 "background": dict(_NO_PICTURE),
                 "tokens": {"--lc-4": "#6b6a62", "--lc-gig": "#173a6b"}}},
        # ENG: engineering/operations gold.
        {"id": "eng", "name": "ENG",
         "night": {"accent": "#f5a300", "secondary": "#ff6f1a", "third": "#8c9bff", "panel": "#120d05", "panel_opacity": 100,
                   "background": dict(_NO_PICTURE),
                   "tokens": {"--bg": "#0a0703", "--lc-4": "#ddbb99", "--lc-7": "#ff6b57", "--lc-gig": "#b35c00",
                              "--lc-diary": "#f08c3c", "--lc-hd-frame": "#e09500"}},
         "day": {"accent": "#9a6400", "secondary": "#b5541f", "third": "#3d5a73", "panel": "#f8f0e2", "panel_opacity": 100,
                 "background": dict(_NO_PICTURE),
                 "tokens": {"--lc-4": "#7d6b52", "--lc-7": "#a8191c", "--lc-gig": "#7a4a00"}}},
        # ENT: silver with steel-blue counterpoint.
        {"id": "ent", "name": "ENT",
         "night": {"accent": "#c3cad6", "secondary": "#8d99ad", "third": "#5c8fd6", "panel": "#0d0f13", "panel_opacity": 100,
                   "background": dict(_NO_PICTURE),
                   "tokens": {"--bg": "#060709", "--lc-gig": "#3b5b8f", "--lc-gig-on": "#eef2f8", "--lc-hd-frame": "#aab3c2"}},
         "day": {"accent": "#4a5568", "secondary": "#6b7a90", "third": "#2f5f9e", "panel": "#f1f2f4", "panel_opacity": 100,
                 "background": dict(_NO_PICTURE),
                 "tokens": {"--silver-hi": "#1d232c", "--silver": "#3a4350", "--silver-mute": "#5b6575", "--silver-dim": "#8a93a1",
                            "--lc-4": "#8a93a1", "--lc-gig": "#2f4f86"}}},
    ],
    "borg": [_standard_style("borg")],
}

LEGACY_BASE = {"dark": "night", "light": "day"}
_LEGACY_PRESET_KEY_TO_ID = {"command": "com", "com": "com", "sci": "sci", "eng": "eng", "ent": "ent"}
_LEGACY_PRESET_NAME_TO_ID = {
    "LCARS (COM)": "com",
    "LCARS (Command)": "com",
    "LCARS (SCI)": "sci",
    "LCARS (ENG)": "eng",
    "LCARS (ENT)": "ent",
}


class AppearanceError(ValueError):
    pass


def bg_dir():
    return config.DATA_DIR / "backgrounds"


def _default_half(mode: str, family: str = "citadel") -> dict[str, Any]:
    return {**BASE[family][mode], "panel_opacity": 100, "background": dict(_NO_PICTURE), "tokens": {}}


def _norm_family_raw(value: Any) -> Any:
    if value == "lcars2":
        return "lcars"
    return value


def _upgrade(t: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a single style entry from older shapes ({base, ...} or {skin, ...}) to {id, name, family, day, night}.
    Shape only; `clean` does the checking."""
    if "day" not in t and "night" not in t and "base" in t:
        mode = LEGACY_BASE.get(t.get("base"), "night")
        old = {k: t[k] for k in ("accent", "panel", "panel_opacity", "background") if k in t}
        other = "day" if mode == "night" else "night"
        return {
            "id": t.get("id", ""),
            "name": t.get("name", ""),
            "family": "citadel",
            mode: {**_default_half(mode), **old},
            other: _default_half(other),
        }
    out = dict(t)
    if "family" not in out and "skin" in out:
        out["family"] = _norm_family_raw(out.pop("skin"))
    elif "family" in out:
        out["family"] = _norm_family_raw(out["family"])
        out.pop("skin", None)
    return out


def _builtin_lcars_by_id() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in BUILTIN_STYLES["lcars"] if s["id"] != "standard"}


def _same_halves(a: dict[str, Any], b: dict[str, Any], family: str) -> bool:
    try:
        ca = {m: _clean_half(a.get(m), m, family) for m in MODES}
        cb = {m: _clean_half(b.get(m), m, family) for m in MODES}
    except AppearanceError:
        return False
    return ca == cb


def _upgrade_setting(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str], bool]:
    """Read an appearance setting (new `styles[]` or legacy `themes[]` + `seeded_presets`).
    Returns (styles, migrated_presets, changed)."""
    migrated: dict[str, str] = dict(raw.get("migrated_presets") or {})
    if "styles" in raw and "themes" not in raw and "seeded_presets" not in raw:
        upgraded = [_upgrade(s) for s in raw.get("styles", [])]
        return upgraded, migrated, False
    if not raw:
        return [], migrated, False

    raw_items = raw.get("themes", []) if ("themes" in raw and not raw.get("styles")) else raw.get("styles", [])
    seeded = {_LEGACY_PRESET_KEY_TO_ID.get(k, k) for k in raw.get("seeded_presets", [])}
    builtins = _builtin_lcars_by_id()
    out: list[dict[str, Any]] = []

    for item in raw_items:
        s = _upgrade(item)
        name = str(s.get("name") or "")
        fam = s.get("family") or "citadel"
        preset_id = _LEGACY_PRESET_NAME_TO_ID.get(name) if fam == "lcars" else None
        # Seeded presets are matched by name and family, and only dropped when their colours are unchanged.
        matched_builtin = preset_id if (preset_id and preset_id in builtins) else None

        if matched_builtin and (not seeded or matched_builtin in seeded):
            if _same_halves(s, builtins[matched_builtin], "lcars"):
                if s.get("id"):
                    migrated[str(s["id"])] = matched_builtin
                continue
            # Edited copy of a seeded preset: keep as a custom style marked "(custom)"
            if not name.endswith("(custom)"):
                s["name"] = f"{name} (custom)"[:40]
        out.append(s)

    return out, migrated, True


def styles() -> list[dict[str, Any]]:
    raw = db.get_settings(KEY, {})
    items, _, _ = _upgrade_setting(raw)
    return items


def migrated_presets() -> dict[str, str]:
    raw = db.get_settings(KEY, {})
    _, migrated, _ = _upgrade_setting(raw)
    return migrated


def migrate_storage() -> dict[str, int]:
    """Upgrade an old-shape appearance setting on disk once (called at startup)."""
    raw = db.get_settings(KEY, {})
    items, migrated, changed = _upgrade_setting(raw)
    if not changed:
        return {}
    payload: dict[str, Any] = {"styles": items}
    if migrated:
        payload["migrated_presets"] = migrated
    db.put_settings(KEY, payload)
    return {"styles": len(items), "dropped_presets": len(migrated)}


def _save(all_styles: list[dict[str, Any]]) -> None:
    raw = db.get_settings(KEY, {})
    _, migrated, _ = _upgrade_setting(raw)
    payload: dict[str, Any] = {"styles": all_styles}
    if migrated:
        payload["migrated_presets"] = migrated
    db.put_settings(KEY, payload)


def _clean_tokens(t: Any, mode: str) -> dict[str, str]:
    if t is None:
        return {}
    if not isinstance(t, dict):
        raise AppearanceError(f"the {mode} advanced colours must be an object")
    if len(t) > len(TOKENS):
        raise AppearanceError(f"too many {mode} advanced colours")
    out = {}
    for k, v in t.items():
        if k not in TOKENS:
            raise AppearanceError(f"unknown colour {str(k)[:30]}")
        if not isinstance(v, str) or not TOKEN_RE.match(v):
            raise AppearanceError(f"the {mode} {k} colour must look like #8b5cf6 (or #8b5cf680, see-through)")
        out[k] = v.lower()
    return out


def _clean_half(h: Any, mode: str, family: str = "citadel") -> dict[str, Any]:
    """One mode's settings, checked; anything missing comes from that family's built-in palette."""
    if h is None:
        h = {}
    if not isinstance(h, dict):
        raise AppearanceError(f"the {mode} settings must be an object")
    base = BASE[family][mode]
    colours = {k: str(h.get(k) or base[k]) for k in ("accent", "secondary", "third", "panel")}
    for label, v in colours.items():
        if not HEX_RE.match(v):
            raise AppearanceError(f"the {mode} {'primary' if label == 'accent' else label} colour must look like #8b5cf6")
    try:
        opacity = int(h.get("panel_opacity", 100))
    except (TypeError, ValueError):
        raise AppearanceError(f"the {mode} panel see-through must be a number")
    bg = h.get("background") or {}
    if not isinstance(bg, dict):
        raise AppearanceError("background must be an object")
    file = str(bg.get("file") or "")
    if file and (not BG_NAME_RE.match(file) or not (bg_dir() / file).is_file()):
        raise AppearanceError(f"the {mode} background picture isn’t here any more — upload it again")
    fit = bg.get("fit") or "cover"
    if fit not in FITS:
        raise AppearanceError("unknown background fit")
    try:
        dim = int(bg.get("dim", 40))
    except (TypeError, ValueError):
        raise AppearanceError("dim must be a number")
    return {**{k: v.lower() for k, v in colours.items()}, "panel_opacity": max(30, min(100, opacity)),
            "background": {"file": file, "fit": fit, "dim": max(0, min(90, dim)), "glow": bool(bg.get("glow", True))},
            "tokens": _clean_tokens(h.get("tokens"), mode)}


def clean(t: dict[str, Any]) -> dict[str, Any]:
    """A style as stored: built for a family, with a checked Day half and Night half (missing ones filled from defaults)."""
    t = _upgrade(t)
    name = str(t.get("name") or "").strip()[:40]
    if not name:
        raise AppearanceError("give the style a name")
    family = t.get("family") or "citadel"
    if family not in FAMILIES:
        raise AppearanceError("a style belongs to the Citadel, LCARS or Borg family")
    return {"id": str(t.get("id") or ""), "name": name, "family": family,
            **{mode: _clean_half(t.get(mode), mode, family) for mode in MODES}}


def save_style(t: dict[str, Any], style_id: str | None = None) -> dict[str, Any]:
    """Create (no id) or replace (id) a custom style. Returns it as stored."""
    style = clean(t)
    all_styles = styles()
    if style_id:
        if not any(x["id"] == style_id for x in all_styles):
            raise LookupError("no such style")
        style["id"] = style_id
        all_styles = [style if x["id"] == style_id else x for x in all_styles]
    else:
        if len(all_styles) >= MAX_STYLES:
            raise AppearanceError(f"at most {MAX_STYLES} styles")
        style["id"] = uuid.uuid4().hex[:12]
        all_styles.append(style)
    _save(all_styles)
    tidy()
    return style


def delete_style(style_id: str) -> None:
    all_styles = styles()
    if not any(x["id"] == style_id for x in all_styles):
        raise LookupError("no such style")
    _save([x for x in all_styles if x["id"] != style_id])
    tidy()


def sniff(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def store_background(data: bytes) -> str:
    """Keep an uploaded picture (PNG, JPEG, WebP or GIF; checked by its bytes, not its name) and return its file name."""
    if not data:
        raise AppearanceError("that file is empty")
    if len(data) > MAX_BACKGROUND:
        raise AppearanceError(f"pictures up to {MAX_BACKGROUND // 1_000_000} MB, please")
    ext = sniff(data)
    if not ext:
        raise AppearanceError("use a JPEG, PNG, WebP or GIF picture")
    folder = bg_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
    if not (folder / name).exists():
        (folder / name).write_bytes(data)
    return name


def tidy(older_than: float = 24 * 3600) -> None:
    """Remove pictures no style uses. A fresh upload is spared for a day, so one waiting in an open, unsaved
    editor isn't swept away by someone saving a different style meanwhile."""
    folder = bg_dir()
    if not folder.exists():
        return
    used = {f for s in styles() for mode in MODES if (f := ((s.get(mode) or {}).get("background") or {}).get("file"))}
    for f in folder.iterdir():
        if f.is_file() and f.name not in used and time.time() - f.stat().st_mtime > older_than:
            f.unlink(missing_ok=True)
