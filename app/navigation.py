"""Navigation (Settings → Navigation): what each page is called, which pages get a phone tab and a place on the desktop
rail, their order, and the group each sits in on the desktop rail.

A phone's tab bar has room for a few pages, so the rest go under its More tab. The desktop rail has room for all of
them, but a page that's never used there can be taken off it too (its address still opens it).

One set is shared by every device. A device can have its own instead (`devices`, keyed by device id), made from a copy
of the shared one in Settings and dropped again with "Use every device's" or when the device is revoked. Both live in
the one `navigation` setting: {"pages": [...], "devices": {id: {"pages": [...]}}}.

Home always comes first, always has a phone tab and always stays on the rail: on LCARS the rail's Home lines up with the
header, and a phone with no way home would be stuck. Settings stays on the rail too (on desktop there's no header gear
to reach it by). Curate, Audio and Settings sit at the foot of the rail, below the gap, so they keep their place; on
phones they start under More.

Groups (Mike, 2026-11-23): the desktop rail sorts its pages into named groups (Command, Science, Engineering by
default), each under a small heading, with Curate, Audio and Settings as Ops at the foot as before. A page with no group
sits straight under Home. The one page order is kept in group order, so phones (which show no headings) follow it too.
`groups` lives beside `pages` in each set; a set saved before groups existed gets the default groups.

Saved pages are read through `_resolve`, which drops pages that no longer exist and adds new ones (with their defaults)
at the end, so adding a page to Citadel never needs a migration here.
"""
from __future__ import annotations

from typing import Any

from . import db

KEY = "navigation"
MAX_LABEL = 16
MAX_GROUPS = 8

# (id, default name). The default groups, in rail order.
GROUPS: tuple[tuple[str, str], ...] = (("command", "Command"), ("science", "Science"), ("engineering", "Engineering"))

# (id, default name, on phones by default, can move, icon, default group). Order is the default order; the rail's bottom
# group (Ops) keeps its place. Icons match index.html's rail.
PAGES: tuple[tuple[str, str, bool, bool, str, str], ...] = (
    ("home", "Home", True, False, "home", ""),
    ("stats", "Fleet", False, True, "fleet", "command"),
    ("news", "News", True, True, "news", "command"),
    ("music", "Companion", True, True, "disc", "command"),
    ("listen", "Music", True, True, "wave", "science"),
    ("media", "Media", True, True, "film", "science"),
    ("casino", "Casino", True, True, "spade", "science"),
    ("holodeck", "Holodeck", False, True, "gamepad", "science"),
    ("labels", "Labels", False, True, "printer", "engineering"),
    ("house", "House", True, True, "tool", "engineering"),
    ("ai", "Odysseus", True, True, "spark", "engineering"),
    ("curate", "Curate", False, False, "tag", ""),
    ("audio", "Audio", False, False, "mixer", ""),
    ("settings", "Settings", False, False, "gear", ""),
)
BY_ID = {p[0]: p for p in PAGES}
FIXED_PHONE = {"home"}                     # always has a phone tab
FIXED_RAIL = {"home", "settings"}          # always on the desktop rail
MOVABLE = [p[0] for p in PAGES if p[3]]


def _groups(groups: list[Any] | None) -> list[dict[str, str]]:
    """The saved groups, or the defaults for a set saved before groups existed (None). An empty list means no groups."""
    if groups is None:
        return [{"id": g, "label": label} for g, label in GROUPS]
    out, seen = [], set()
    for g in groups:
        if isinstance(g, dict) and g.get("id") and g["id"] not in seen and str(g.get("label") or "").strip():
            seen.add(g["id"])
            out.append({"id": str(g["id"]), "label": str(g["label"]).strip()[:MAX_LABEL]})
    return out[:MAX_GROUPS]


def _page(pid: str, saved: dict[str, Any] | None, groups: set[str]) -> dict[str, Any]:
    _, default, phone, movable, icon, group = BY_ID[pid]
    saved = saved or {}
    label = str(saved.get("label") or "").strip()[:MAX_LABEL]
    group = str(saved.get("group", group) or "") if movable else ""
    return {"id": pid, "label": label or default, "default": default,
            "phone": True if pid in FIXED_PHONE else bool(saved.get("phone", phone)),
            "rail": True if pid in FIXED_RAIL else bool(saved.get("rail", True)),
            "movable": movable, "icon": icon, "group": group if group in groups else ""}


def _resolve(pages: list[Any] | None, groups: list[Any] | None = None) -> dict[str, Any]:
    gs = _groups(groups)
    rank = {g["id"]: n for n, g in enumerate(gs, 1)}      # no group (0) comes first, straight under Home
    saved = {p.get("id"): p for p in (pages or []) if isinstance(p, dict) and p.get("id") in BY_ID}
    # movable pages in their saved order (new ones after), with Home first and the bottom group last; then sorted (stably)
    # into their groups
    order = [i for i in saved if i in MOVABLE] + [i for i in MOVABLE if i not in saved]
    moving = sorted((_page(i, saved.get(i), set(rank)) for i in order), key=lambda p: rank.get(p["group"], 0))
    fixed = [_page(p[0], saved.get(p[0]), set()) for p in PAGES if not p[3] and p[0] != "home"]
    return {"pages": [_page("home", saved.get("home"), set()), *moving, *fixed], "groups": gs}


def _raw() -> dict[str, Any]:
    raw = db.get_settings(KEY, {})
    devices = raw.get("devices")
    return {"pages": raw.get("pages") or [], "groups": raw.get("groups"), "devices": devices if isinstance(devices, dict) else {}}


def settings(device_id: str | None = None) -> dict[str, Any]:
    """What a device shows: its own set if it has one, else the shared one. `own` says which."""
    raw = _raw()
    own = raw["devices"].get(device_id) if device_id else None
    src = own or raw
    return {**_resolve(src["pages"], src.get("groups")), "own": bool(own), "max_label": MAX_LABEL}


def for_settings(device_id: str) -> dict[str, Any]:
    """Settings → Navigation edits both: the shared set, and this device's own (null until it has one)."""
    raw = _raw()
    own = raw["devices"].get(device_id)
    return {"shared": _resolve(raw["pages"], raw["groups"]), "device": _resolve(own["pages"], own.get("groups")) if own else None,
            "max_label": MAX_LABEL, "max_groups": MAX_GROUPS}


def _clean(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store only what differs from a page's default name, so a default renamed in code still reaches untouched pages."""
    out = []
    for p in pages:
        if p["id"] not in BY_ID:
            continue
        label = p["label"].strip()
        row = {"id": p["id"], "label": "" if label == BY_ID[p["id"]][1] else label, "phone": p["phone"], "rail": p["rail"]}
        if p.get("group") is not None:
            row["group"] = p["group"]
        out.append(row)
    return out


def save(pages: list[dict[str, Any]], device_id: str | None = None, groups: list[dict[str, str]] | None = None) -> None:
    """The shared set, or (with a device id) that device's own. Without `groups` the set's groups stay as they are (the
    defaults, for a set that never had any); an empty page list resets the groups too."""
    raw = _raw()
    old = raw["devices"].get(device_id) or raw if device_id else raw
    groups = None if not pages and groups is None else (groups if groups is not None else old.get("groups"))
    entry = {"pages": _clean(pages), **({"groups": _groups(groups)} if groups is not None else {})}
    if device_id:
        raw["devices"][device_id] = entry
    else:
        raw.pop("groups", None)
        raw.update(entry)
    db.put_settings(KEY, raw)


def forget(device_id: str) -> bool:
    """Back to the shared set: on "Use every device's", and when a device is revoked."""
    raw = _raw()
    if raw["devices"].pop(device_id, None) is None:
        return False
    db.put_settings(KEY, raw)
    return True
