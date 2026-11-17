"""Appearance: custom themes and background pictures (Settings → Appearance).

A device chooses two things on its own (kept in its browser): the mode, Day or Night, and the theme, which is one of
the built-ins (Citadel, LCARS) or a custom theme. Every theme has both modes, so a custom theme is a variant of a
built-in one ("skin") with a Day half and a Night half. Each half changes what you see most: the accent colour of the
UI, the colour and see-through-ness of the panels, and an optional background picture with how it's fitted and how
much it's dimmed. Custom themes are stored here, so every device can use them.

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
ID_RE = re.compile(r"^[a-z0-9]{1,24}$")
BG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
SKINS = ("citadel", "lcars")
MODES = ("day", "night")
BASE_PANEL = {"night": "#0e0b14", "day": "#f8f0e2"}       # the built-in modes' own --surface
BASE_ACCENT = {"night": "#8b5cf6", "day": "#b5541f"}
LEGACY_BASE = {"dark": "night", "light": "day"}


class AppearanceError(ValueError):
    pass


def bg_dir():
    return config.DATA_DIR / "backgrounds"


def _default_half(mode: str) -> dict[str, Any]:
    return {"accent": BASE_ACCENT[mode], "panel": BASE_PANEL[mode], "panel_opacity": 100,
            "background": {"file": "", "fit": "cover", "dim": 40, "glow": True}}


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
    db.put_settings(KEY, {"themes": all_themes})


def _clean_half(h: Any, mode: str) -> dict[str, Any]:
    """One mode's settings, checked; anything missing comes from that mode's built-in look."""
    if h is None:
        h = {}
    if not isinstance(h, dict):
        raise AppearanceError(f"the {mode} settings must be an object")
    accent = str(h.get("accent") or BASE_ACCENT[mode])
    panel = str(h.get("panel") or BASE_PANEL[mode])
    for label, v in (("accent", accent), ("panel", panel)):
        if not HEX_RE.match(v):
            raise AppearanceError(f"the {mode} {label} colour must look like #8b5cf6")
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
    return {"accent": accent.lower(), "panel": panel.lower(), "panel_opacity": max(30, min(100, opacity)),
            "background": {"file": file, "fit": fit, "dim": max(0, min(90, dim)), "glow": bool(bg.get("glow", True))}}


def clean(t: dict[str, Any]) -> dict[str, Any]:
    """A theme as stored: built on a skin, with a checked Day half and Night half (missing ones filled from the defaults)."""
    t = _upgrade(t)
    name = str(t.get("name") or "").strip()[:40]
    if not name:
        raise AppearanceError("give the theme a name")
    skin = t.get("skin") or "citadel"
    if skin not in SKINS:
        raise AppearanceError("a theme is built on Citadel or LCARS")
    return {"id": str(t.get("id") or ""), "name": name, "skin": skin,
            **{mode: _clean_half(t.get(mode), mode) for mode in MODES}}


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
