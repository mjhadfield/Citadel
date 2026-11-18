"""Navigation (Settings → Navigation): what each page is called, which pages get a phone tab and a place on the desktop
rail, and their order.

A phone's tab bar has room for a few pages, so the rest go under its More tab. The desktop rail has room for all of
them, but a page that's never used there can be taken off it too (its address still opens it).

One set is shared by every device. A device can have its own instead (`devices`, keyed by device id), made from a copy
of the shared one in Settings and dropped again with "Use every device's" or when the device is revoked. Both live in
the one `navigation` setting: {"pages": [...], "devices": {id: {"pages": [...]}}}.

Home always comes first, always has a phone tab and always stays on the rail: on LCARS the rail's Home lines up with the
header, and a phone with no way home would be stuck. Settings stays on the rail too (on desktop there's no header gear
to reach it by). Curate and Settings sit at the foot of the rail, below the gap, so they keep their place; on phones
they start under More.

Saved pages are read through `_resolve`, which drops pages that no longer exist and adds new ones (with their defaults)
at the end, so adding a page to Citadel never needs a migration here.
"""
from __future__ import annotations

from typing import Any

from . import db

KEY = "navigation"
MAX_LABEL = 16

# (id, default name, on phones by default, can move, icon). Order is the default order; the rail's bottom group keeps its
# place. Icons match index.html's rail.
PAGES: tuple[tuple[str, str, bool, bool, str], ...] = (
    ("home", "Home", True, False, "home"),
    ("news", "News", True, True, "news"),
    ("media", "Media", True, True, "film"),
    ("music", "Companion", True, True, "disc"),
    ("listen", "Music", True, True, "wave"),
    ("casino", "Casino", True, True, "spade"),
    ("ai", "Odysseus", True, True, "spark"),
    ("house", "House", True, True, "tool"),
    ("stats", "Stats", False, True, "chart"),
    ("curate", "Curate", False, False, "tag"),
    ("settings", "Settings", False, False, "gear"),
)
BY_ID = {p[0]: p for p in PAGES}
FIXED_PHONE = {"home"}                     # always has a phone tab
FIXED_RAIL = {"home", "settings"}          # always on the desktop rail
MOVABLE = [p[0] for p in PAGES if p[3]]


def _page(pid: str, saved: dict[str, Any] | None) -> dict[str, Any]:
    _, default, phone, movable, icon = BY_ID[pid]
    saved = saved or {}
    label = str(saved.get("label") or "").strip()[:MAX_LABEL]
    return {"id": pid, "label": label or default, "default": default,
            "phone": True if pid in FIXED_PHONE else bool(saved.get("phone", phone)),
            "rail": True if pid in FIXED_RAIL else bool(saved.get("rail", True)),
            "movable": movable, "icon": icon}


def _resolve(pages: list[Any] | None) -> list[dict[str, Any]]:
    saved = {p.get("id"): p for p in (pages or []) if isinstance(p, dict) and p.get("id") in BY_ID}
    # movable pages in their saved order (new ones after), with Home first and the bottom group last
    order = [i for i in saved if i in MOVABLE] + [i for i in MOVABLE if i not in saved]
    ids = ["home", *order, *(p[0] for p in PAGES if not p[3] and p[0] != "home")]
    return [_page(i, saved.get(i)) for i in ids]


def _raw() -> dict[str, Any]:
    raw = db.get_settings(KEY, {})
    devices = raw.get("devices")
    return {"pages": raw.get("pages") or [], "devices": devices if isinstance(devices, dict) else {}}


def settings(device_id: str | None = None) -> dict[str, Any]:
    """What a device shows: its own set if it has one, else the shared one. `own` says which."""
    raw = _raw()
    own = raw["devices"].get(device_id) if device_id else None
    return {"pages": _resolve(own["pages"] if own else raw["pages"]), "own": bool(own), "max_label": MAX_LABEL}


def for_settings(device_id: str) -> dict[str, Any]:
    """Settings → Navigation edits both: the shared set, and this device's own (null until it has one)."""
    raw = _raw()
    own = raw["devices"].get(device_id)
    return {"shared": _resolve(raw["pages"]), "device": _resolve(own["pages"]) if own else None, "max_label": MAX_LABEL}


def _clean(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store only what differs from a page's default name, so a default renamed in code still reaches untouched pages."""
    out = []
    for p in pages:
        if p["id"] not in BY_ID:
            continue
        label = p["label"].strip()
        out.append({"id": p["id"], "label": "" if label == BY_ID[p["id"]][1] else label, "phone": p["phone"], "rail": p["rail"]})
    return out


def save(pages: list[dict[str, Any]], device_id: str | None = None) -> None:
    """The shared set, or (with a device id) that device's own."""
    raw = _raw()
    if device_id:
        raw["devices"][device_id] = {"pages": _clean(pages)}
    else:
        raw["pages"] = _clean(pages)
    db.put_settings(KEY, raw)


def forget(device_id: str) -> bool:
    """Back to the shared set: on "Use every device's", and when a device is revoked."""
    raw = _raw()
    if raw["devices"].pop(device_id, None) is None:
        return False
    db.put_settings(KEY, raw)
    return True
