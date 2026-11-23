"""Appearance: custom themes and background pictures (Settings → Appearance).

A device chooses two things on its own (kept in its browser): the mode, Day or Night, and the theme, which is one of
the built-ins (Citadel, LCARS) or a custom theme. Every theme has both modes, so a custom theme is a variant of a
built-in one ("skin") with a Day half and a Night half. Each half has four Basic colours (primary, kept as `accent`
for older themes; secondary; third; panel), how see-through the panels are, an optional background picture, and
`tokens`: Advanced overrides of single colour tokens. The browser works every other token out from the Basic colours
(web/js/palette.js, whose token list TOKENS mirrors), so a custom theme colours everything, not just the accent.
Custom themes are stored here, so every device can use them.

PRESETS are starting points for a new theme. `seed_presets` adds each one once as an ordinary custom theme, so it shows
in the Style menu straight away; deleting it doesn't bring it back.

Themes saved before the Day/Night split had a single `base` (dark or light) and no skin. They are read as a Citadel
theme with that mode's half filled in and the other half at the defaults (see `_upgrade`).

Background pictures are uploaded to the data folder and served by Citadel itself (see main.py), because the page's
CSP only lets the browser load images from a few named CDNs.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

from . import config, db

KEY = "appearance"
DEFAULTS: dict[str, Any] = {"themes": []}
MAX_THEMES = 20
MAX_BACKGROUND = 15_000_000                      # bytes — a big phone photo is well under this
FITS = ("cover", "contain", "tile", "center")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TOKEN_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")      # Advanced: #rrggbb or #rrggbbaa (see-through)
ID_RE = re.compile(r"^[a-z0-9]{1,24}$")
BG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
SKINS = ("citadel", "lcars", "borg")
MODES = ("day", "night")
BASE_PANEL = {"night": "#0e0b14", "day": "#f8f0e2"}       # the built-in modes' own --surface
BASE_ACCENT = {"night": "#8b5cf6", "day": "#b5541f"}
# Each built-in look's four Basic colours (palette.js BASE): what a missing one falls back to.
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
# LCARS (COM): red, the command division's colour. Night is vivid red on black with orange and gold runs, a tan neutral
# and a pale sunflower for attention (amber would read as one more gold); Day is parchment with a crimson frame,
# brick-red runs and the one slate counterpoint. Its Advanced overrides are where Basic's guesses weren't quite it.
PRESETS: dict[str, dict[str, Any]] = {
    "command": {"name": "LCARS (COM)", "skin": "lcars",
                "night": {"accent": "#e02b2b", "secondary": "#ff7a1f", "third": "#f5a524", "panel": "#140909", "panel_opacity": 100,
                          "background": _NO_PICTURE,
                          "tokens": {"--bg": "#0b0404", "--lc-4": "#d9a07a", "--lc-7": "#ffd866", "--lc-gig": "#a8141e",
                                     "--lc-hd-frame": "#c41f1f"}},
                "day": {"accent": "#a8191c", "secondary": "#bf3a12", "third": "#3d5a73", "panel": "#f8f0e2", "panel_opacity": 100,
                        "background": _NO_PICTURE,
                        "tokens": {"--lc-4": "#7d6b52", "--lc-gig": "#6e1212"}}},
    # LCARS (SCI): sciences blue. Night: blue with cyan and teal runs, a steel neutral, amber attention. Day: parchment
    # with deep blue and teal, burnt orange as the one warm counterpoint.
    "sci": {"name": "LCARS (SCI)", "skin": "lcars",
            "night": {"accent": "#2f72ff", "secondary": "#22c3ff", "third": "#25c4a4", "panel": "#070b14", "panel_opacity": 100,
                      "background": _NO_PICTURE,
                      "tokens": {"--bg": "#03060c", "--lc-4": "#9db3d1", "--lc-gig": "#2c55c9", "--lc-hd-frame": "#1f5be0"}},
            "day": {"accent": "#1d4f91", "secondary": "#1f6f86", "third": "#b5541f", "panel": "#f8f0e2", "panel_opacity": 100,
                    "background": _NO_PICTURE,
                    "tokens": {"--lc-4": "#6b6a62", "--lc-gig": "#173a6b"}}},
    # LCARS (ENG): engineering/operations gold. Night: the classic LCARS run — gold, orange and a periwinkle
    # counterpoint, tan neutral — with coral for attention (amber would vanish among the golds). Day: parchment with
    # deep ochre, burnt orange and slate.
    "eng": {"name": "LCARS (ENG)", "skin": "lcars",
            "night": {"accent": "#f5a300", "secondary": "#ff6f1a", "third": "#8c9bff", "panel": "#120d05", "panel_opacity": 100,
                      "background": _NO_PICTURE,
                      "tokens": {"--bg": "#0a0703", "--lc-4": "#ddbb99", "--lc-7": "#ff6b57", "--lc-gig": "#b35c00", "--lc-diary": "#f08c3c",
                                 "--lc-hd-frame": "#e09500"}},
            "day": {"accent": "#9a6400", "secondary": "#b5541f", "third": "#3d5a73", "panel": "#f8f0e2", "panel_opacity": 100,
                    "background": _NO_PICTURE,
                    "tokens": {"--lc-4": "#7d6b52", "--lc-7": "#a8191c", "--lc-gig": "#7a4a00"}}},
    # LCARS (ENT): silver, with one steel-blue counterpoint so it isn't grey on grey. Night: silver and steel on a neutral
    # black. Day: cool paper and slate inks instead of parchment and sepia (silver on parchment looks dirty).
    "ent": {"name": "LCARS (ENT)", "skin": "lcars",
            "night": {"accent": "#c3cad6", "secondary": "#8d99ad", "third": "#5c8fd6", "panel": "#0d0f13", "panel_opacity": 100,
                      "background": _NO_PICTURE,
                      "tokens": {"--bg": "#060709", "--lc-gig": "#3b5b8f", "--lc-gig-on": "#eef2f8", "--lc-hd-frame": "#aab3c2"}},
            "day": {"accent": "#4a5568", "secondary": "#6b7a90", "third": "#2f5f9e", "panel": "#f1f2f4", "panel_opacity": 100,
                    "background": _NO_PICTURE,
                    "tokens": {"--silver-hi": "#1d232c", "--silver": "#3a4350", "--silver-mute": "#5b6575", "--silver-dim": "#8a93a1",
                               "--lc-4": "#8a93a1", "--lc-gig": "#2f4f86"}}},
}
LEGACY_BASE = {"dark": "night", "light": "day"}


class AppearanceError(ValueError):
    pass


def bg_dir():
    return config.DATA_DIR / "backgrounds"


def _default_half(mode: str, skin: str = "citadel") -> dict[str, Any]:
    return {**BASE[skin][mode], "panel_opacity": 100, "background": dict(_NO_PICTURE), "tokens": {}}


def _upgrade(t: dict[str, Any]) -> dict[str, Any]:
    """A theme saved before the Day/Night split ({base, accent, panel, ...}) in today's shape: a Citadel theme whose
    old mode keeps its settings and whose other mode starts at the defaults. Shape only; `clean` does the checking."""
    if "day" in t or "night" in t or "base" not in t:
        return t
    mode = LEGACY_BASE.get(t.get("base"), "night")
    old = {k: t[k] for k in ("accent", "panel", "panel_opacity", "background") if k in t}
    other = "day" if mode == "night" else "night"
    return {"id": t.get("id", ""), "name": t.get("name", ""), "skin": "citadel", mode: {**_default_half(mode), **old}, other: _default_half(other)}


def themes() -> list[dict[str, Any]]:
    return [_upgrade(t) for t in db.get_settings(KEY, DEFAULTS).get("themes", [])]


def _save(all_themes: list[dict[str, Any]]) -> None:
    db.put_settings(KEY, {**db.get_settings(KEY, DEFAULTS), "themes": all_themes})


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


def _clean_half(h: Any, mode: str, skin: str = "citadel") -> dict[str, Any]:
    """One mode's settings, checked; anything missing comes from that mode's built-in look."""
    if h is None:
        h = {}
    if not isinstance(h, dict):
        raise AppearanceError(f"the {mode} settings must be an object")
    base = BASE[skin][mode]
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
    """A theme as stored: built on a skin, with a checked Day half and Night half (missing ones filled from the defaults)."""
    t = _upgrade(t)
    name = str(t.get("name") or "").strip()[:40]
    if not name:
        raise AppearanceError("give the theme a name")
    skin = t.get("skin") or "citadel"
    if skin not in SKINS:
        raise AppearanceError("a theme is built on Citadel, LCARS or Borg")
    return {"id": str(t.get("id") or ""), "name": name, "skin": skin,
            **{mode: _clean_half(t.get(mode), mode, skin) for mode in MODES}}


def save_theme(t: dict[str, Any], theme_id: str | None = None) -> dict[str, Any]:
    """Create (no id) or replace (id) a theme. Returns it as stored."""
    theme = clean(t)
    all_themes = themes()
    if theme_id:
        if not any(x["id"] == theme_id for x in all_themes):
            raise LookupError("no such theme")
        theme["id"] = theme_id
        all_themes = [theme if x["id"] == theme_id else x for x in all_themes]
    else:
        if len(all_themes) >= MAX_THEMES:
            raise AppearanceError(f"at most {MAX_THEMES} themes")
        theme["id"] = uuid.uuid4().hex[:12]
        all_themes.append(theme)
    _save(all_themes)
    tidy()
    return theme


def seed_presets() -> list[str]:
    """Add each preset once as a custom theme (at startup). Returns the names added."""
    stored = db.get_settings(KEY, DEFAULTS)
    done = set(stored.get("seeded_presets", []))
    added = []
    for key, preset in PRESETS.items():
        if key in done:
            continue
        all_themes = themes()
        if len(all_themes) < MAX_THEMES:
            all_themes.append({**clean(preset), "id": uuid.uuid4().hex[:12]})
            added.append(preset["name"])
        done.add(key)
        db.put_settings(KEY, {**stored, "themes": all_themes, "seeded_presets": sorted(done)})
        stored = db.get_settings(KEY, DEFAULTS)
    return added


def delete_theme(theme_id: str) -> None:
    all_themes = themes()
    if not any(x["id"] == theme_id for x in all_themes):
        raise LookupError("no such theme")
    _save([x for x in all_themes if x["id"] != theme_id])
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
    """Remove pictures no theme uses. A fresh upload is spared for a day, so one waiting in an open, unsaved
    editor isn't swept away by someone saving a different theme meanwhile."""
    folder = bg_dir()
    if not folder.exists():
        return
    used = {f for t in themes() for mode in MODES if (f := ((t.get(mode) or {}).get("background") or {}).get("file"))}
    for f in folder.iterdir():
        if f.is_file() and f.name not in used and time.time() - f.stat().st_mtime > older_than:
            f.unlink(missing_ok=True)
