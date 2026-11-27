"""The Controls page (#controls, web/js/controls.js): fast smart-home lighting through Home Assistant on Probe-1.

Reads Home Assistant's states with Citadel's non-admin token (app/homeassistant.py) and groups Hue room groups with
their member bulbs so single-bulb rooms (Bedroom, Study, Living room, Gym, Box room) appear as one clean card with no
duplicate switches, while multi-bulb rooms (Office: Office Main + Lamp) keep a master switch/slider plus expandable
individual bulbs. Any non-Hue light not in a Hue room shows up as its own standalone card automatically.

Every change is an ordinary light/scene service call allowed for a non-admin Home Assistant user. A small in-memory
allow-list on `app.state.controls` keeps the valid light/scene entity IDs from the latest read so brightness drags send
a single POST to Probe-1 without an extra GET per tick, while still refusing unknown or non-lighting entities.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from . import db, homeassistant as ha

LIGHT_ID = re.compile(r"^light\.[a-z0-9_]{1,80}$")
SCENE_ID = re.compile(r"^scene\.[a-z0-9_]{1,80}$")
COLOR_MODES_RGB = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
DEFAULT_MIN_K = 2000
DEFAULT_MAX_K = 6500
SETTINGS_KEY = "controls"
MAX_ORDER = 64


class OrderIn(BaseModel):
    """Saved room display order (list of room light.* entity IDs)."""

    order: list[str] = Field(default_factory=list, max_length=MAX_ORDER)

    @field_validator("order")
    @classmethod
    def _clean_order(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in v:
            eid = str(item).strip()
            if not LIGHT_ID.match(eid):
                raise ValueError(f"invalid light entity id: {eid!r}")
            if eid not in seen:
                seen.add(eid)
                out.append(eid)
        return out


def saved_order() -> list[str]:
    raw = db.get_settings(SETTINGS_KEY, {"order": []})
    order = raw.get("order") if isinstance(raw, dict) else []
    return [str(x) for x in order if isinstance(x, str) and LIGHT_ID.match(x)]


def save_order(order: list[str]) -> list[str]:
    clean = OrderIn(order=order).order
    db.put_settings(SETTINGS_KEY, {"order": clean})
    return clean


class ControlsState:
    """Per-lifespan cache of known light/scene entity IDs and Kelvin bounds (lives on app.state.controls)."""

    def __init__(self) -> None:
        self.lights: dict[str, dict[str, Any]] = {}
        self.scenes: set[str] = set()

    def update(self, lights: dict[str, dict[str, Any]], scenes: set[str]) -> None:
        self.lights = lights
        self.scenes = scenes


class LightChange(BaseModel):
    """One lighting command from the Controls page."""

    on: bool | None = None
    brightness: int | None = Field(None, ge=1, le=100)
    kelvin: int | None = Field(None, ge=1500, le=9000)
    rgb: list[int] | None = None
    transition: float | None = Field(None, ge=0, le=5)

    @field_validator("rgb", mode="before")
    @classmethod
    def _rgb(cls, v: Any) -> list[int] | None:
        if v is None:
            return None
        if not isinstance(v, (list, tuple)) or len(v) != 3 or any(not isinstance(c, int) or isinstance(c, bool) or not 0 <= c <= 255 for c in v):
            raise ValueError("rgb must be three integers from 0 to 255")
        return list(v)

    @model_validator(mode="after")
    def _consistent(self) -> LightChange:
        has_turn_on_attrs = any(x is not None for x in (self.brightness, self.kelvin, self.rgb))
        if self.on is None and not has_turn_on_attrs:
            raise ValueError("send at least one of on, brightness, kelvin or rgb")
        if self.on is False and has_turn_on_attrs:
            raise ValueError("cannot set brightness or colour while turning a light off")
        if self.kelvin is not None and self.rgb is not None:
            raise ValueError("choose either kelvin or rgb, not both")
        return self


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _kelvin_bounds(attrs: dict[str, Any]) -> tuple[int | None, int | None]:
    min_k = attrs.get("min_color_temp_kelvin")
    max_k = attrs.get("max_color_temp_kelvin")
    if not isinstance(min_k, (int, float)) and isinstance(attrs.get("max_mireds"), (int, float)) and attrs["max_mireds"] > 0:
        min_k = round(1_000_000 / attrs["max_mireds"])
    if not isinstance(max_k, (int, float)) and isinstance(attrs.get("min_mireds"), (int, float)) and attrs["min_mireds"] > 0:
        max_k = round(1_000_000 / attrs["min_mireds"])
    lo = int(min_k) if isinstance(min_k, (int, float)) and min_k >= 1000 else None
    hi = int(max_k) if isinstance(max_k, (int, float)) and max_k >= 1000 else None
    if lo is not None and hi is not None and lo > hi:
        return hi, lo
    return lo, hi


def _parse_light(s: dict[str, Any]) -> dict[str, Any]:
    eid = str(s.get("entity_id") or "")
    attrs = s.get("attributes") or {}
    raw_state = str(s.get("state") or "")
    on = raw_state == "on"
    available = raw_state in ("on", "off")
    modes = {str(m) for m in (attrs.get("supported_color_modes") or ()) if m}
    raw_b = attrs.get("brightness")
    supports_color = bool(modes & COLOR_MODES_RGB)
    supports_temp = "color_temp" in modes
    supports_brightness = bool(modes - {"onoff", "unknown"}) or supports_color or supports_temp or isinstance(raw_b, (int, float))
    raw_lo, raw_hi = _kelvin_bounds(attrs)
    min_k = raw_lo if raw_lo is not None else DEFAULT_MIN_K
    max_k = raw_hi if raw_hi is not None else DEFAULT_MAX_K

    brightness = max(1, min(100, round(raw_b * 100 / 255))) if on and isinstance(raw_b, (int, float)) else None
    kelvin: int | None = None
    if on:
        if isinstance(attrs.get("color_temp_kelvin"), (int, float)):
            kelvin = int(round(attrs["color_temp_kelvin"]))
        elif isinstance(attrs.get("color_temp"), (int, float)) and attrs["color_temp"] > 0:
            kelvin = round(1_000_000 / attrs["color_temp"])

    rgb: list[int] | None = None
    raw_rgb = attrs.get("rgb_color")
    if on and isinstance(raw_rgb, (list, tuple)) and len(raw_rgb) >= 3:
        if all(isinstance(c, (int, float)) for c in raw_rgb[:3]):
            rgb = [max(0, min(255, int(round(c)))) for c in raw_rgb[:3]]

    name = str(attrs.get("friendly_name") or eid.split(".", 1)[-1].replace("_", " ").capitalize()).strip()
    return {
        "entity_id": eid,
        "name": name,
        "on": on,
        "available": available,
        "brightness": brightness,
        "color_mode": str(attrs["color_mode"]) if on and attrs.get("color_mode") else None,
        "color_temp_kelvin": kelvin,
        "min_kelvin": min_k,
        "max_kelvin": max_k,
        "_raw_min_k": raw_lo,
        "_raw_max_k": raw_hi,
        "rgb": rgb,
        "supports": {
            "brightness": supports_brightness,
            "color_temp": supports_temp,
            "color": supports_color,
        },
    }


def _strip_private(light_obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in light_obj.items() if not k.startswith("_")}


def _merge_supports(items: list[dict[str, Any]]) -> dict[str, bool]:
    return {
        "brightness": any(i["supports"]["brightness"] for i in items),
        "color_temp": any(i["supports"]["color_temp"] for i in items),
        "color": any(i["supports"]["color"] for i in items),
    }


def _scene_label(scene_state: dict[str, Any], room_name: str) -> str:
    attrs = scene_state.get("attributes") or {}
    label = str(attrs.get("name") or attrs.get("friendly_name") or scene_state["entity_id"].split(".", 1)[-1].replace("_", " ")).strip()
    prefix = f"{room_name} "
    if label.lower().startswith(prefix.lower()) and len(label) > len(prefix):
        label = label[len(prefix):].strip()
    return label


def _matches_room_member(light: dict[str, Any], room_name: str, member_names: set[str], member_slugs: set[str], member_eids: set[str]) -> bool:
    eid = light["entity_id"]
    if eid in member_eids:
        return True
    lname = light["name"].strip().lower()
    if lname in member_names:
        return True
    room_lo = room_name.strip().lower()
    if lname.startswith(f"{room_lo} ") and lname[len(room_lo) + 1:].strip() in member_names:
        return True
    obj_id = eid.split(".", 1)[1]
    room_slug = _slug(room_name)
    return obj_id in member_slugs or (bool(room_slug) and any(obj_id == f"{room_slug}_{s}" for s in member_slugs))


def _tidy_member_name(name: str, room_name: str) -> str:
    """Drop a repeated room prefix like 'Bedroom Bedroom Main' -> 'Bedroom Main'."""
    prefix = f"{room_name} {room_name} "
    if name.lower().startswith(prefix.lower()):
        return name[len(room_name) + 1:].strip()
    return name


def parse_states(states: list[dict[str, Any]], order: list[str] | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]], set[str]]:
    """Shape Home Assistant's /api/states list into {summary, rooms, zones} plus the allow-lists for writes."""
    raw_lights = [s for s in states if isinstance(s, dict) and LIGHT_ID.match(str(s.get("entity_id") or ""))]
    raw_scenes = [s for s in states if isinstance(s, dict) and SCENE_ID.match(str(s.get("entity_id") or ""))]

    hue_rooms: list[dict[str, Any]] = []
    hue_zones: list[dict[str, Any]] = []
    ha_groups: list[dict[str, Any]] = []
    singles: list[dict[str, Any]] = []

    for s in raw_lights:
        attrs = s.get("attributes") or {}
        if attrs.get("is_hue_group") is True:
            if attrs.get("hue_type") == "zone":
                hue_zones.append(s)
            else:
                hue_rooms.append(s)
        elif isinstance(attrs.get("entity_id"), list) and attrs["entity_id"]:
            ha_groups.append(s)
        else:
            singles.append(s)

    parsed_singles = [_parse_light(s) for s in singles]
    claimed_eids: set[str] = set()

    # Group Hue scenes by their group_name (case-insensitive).
    scenes_by_group: dict[str, list[dict[str, str]]] = {}
    known_scenes: set[str] = set()
    for sc in raw_scenes:
        eid = str(sc["entity_id"])
        attrs = sc.get("attributes") or {}
        gname = str(attrs.get("group_name") or "").strip()
        if gname:
            scenes_by_group.setdefault(gname.lower(), []).append({"entity_id": eid, "name": _scene_label(sc, gname)})
            known_scenes.add(eid)

    rooms: list[dict[str, Any]] = []
    known_lights: dict[str, dict[str, Any]] = {}

    def remember(light_obj: dict[str, Any]) -> None:
        known_lights[light_obj["entity_id"]] = {
            "min_k": light_obj["min_kelvin"],
            "max_k": light_obj["max_kelvin"],
            "supports": light_obj["supports"],
        }

    for g in hue_rooms:
        base = _parse_light(g)
        attrs = g.get("attributes") or {}
        member_names = {str(n).strip().lower() for n in (attrs.get("lights") or ()) if str(n).strip()}
        member_slugs = {_slug(n) for n in member_names if _slug(n)}
        member_eids = {str(e) for e in (attrs.get("entity_id") or ()) if isinstance(e, str)}

        children: list[dict[str, Any]] = []
        for bulb in parsed_singles:
            if bulb["entity_id"] in claimed_eids:
                continue
            if _matches_room_member(bulb, base["name"], member_names, member_slugs, member_eids):
                claimed_eids.add(bulb["entity_id"])
                child = {**bulb, "name": _tidy_member_name(bulb["name"], base["name"])}
                children.append(child)
                remember(child)

        children.sort(key=lambda b: b["name"].lower())
        all_for_caps = [base, *children]
        supports = _merge_supports(all_for_caps)
        explicit_lo = [x["_raw_min_k"] for x in all_for_caps if x.get("_raw_min_k") is not None]
        explicit_hi = [x["_raw_max_k"] for x in all_for_caps if x.get("_raw_max_k") is not None]
        min_k = min(explicit_lo) if explicit_lo else DEFAULT_MIN_K
        max_k = max(explicit_hi) if explicit_hi else DEFAULT_MAX_K

        # If a Hue group omits colour/brightness while a single child bulb reports it, inherit cleanly.
        brightness = base["brightness"]
        kelvin = base["color_temp_kelvin"]
        rgb = base["rgb"]
        color_mode = base["color_mode"]
        if len(children) == 1 and base["on"]:
            c0 = children[0]
            if brightness is None:
                brightness = c0["brightness"]
            if kelvin is None:
                kelvin = c0["color_temp_kelvin"]
            if rgb is None:
                rgb = c0["rgb"]
            if color_mode is None:
                color_mode = c0["color_mode"]

        scenes = sorted(scenes_by_group.get(base["name"].lower(), []), key=lambda x: x["name"].lower())
        room = {
            "id": _slug(base["name"]) or base["entity_id"].split(".", 1)[1],
            "entity_id": base["entity_id"],
            "name": base["name"],
            "is_group": True,
            "on": base["on"],
            "available": base["available"],
            "brightness": brightness,
            "color_mode": color_mode,
            "color_temp_kelvin": kelvin,
            "min_kelvin": min_k,
            "max_kelvin": max_k,
            "rgb": rgb,
            "supports": supports,
            "lights": [_strip_private(c) for c in children],
            "scenes": scenes,
        }
        remember(room)
        rooms.append(room)

    # Any remaining non-group light not in a Hue room becomes its own standalone card.
    for bulb in parsed_singles:
        if bulb["entity_id"] in claimed_eids:
            continue
        remember(bulb)
        rooms.append({
            "id": bulb["entity_id"].split(".", 1)[1],
            "entity_id": bulb["entity_id"],
            "name": bulb["name"],
            "is_group": False,
            "on": bulb["on"],
            "available": bulb["available"],
            "brightness": bulb["brightness"],
            "color_mode": bulb["color_mode"],
            "color_temp_kelvin": bulb["color_temp_kelvin"],
            "min_kelvin": bulb["min_kelvin"],
            "max_kelvin": bulb["max_kelvin"],
            "rgb": bulb["rgb"],
            "supports": bulb["supports"],
            "lights": [],
            "scenes": [],
        })

    # Hue zones (separate compact strip so they don't duplicate rooms).
    zones: list[dict[str, Any]] = []
    for z in hue_zones:
        zb = _parse_light(z)
        remember(zb)
        zones.append({
            "entity_id": zb["entity_id"],
            "name": zb["name"],
            "on": zb["on"],
            "available": zb["available"],
            "brightness": zb["brightness"],
            "rgb": zb["rgb"],
            "color_temp_kelvin": zb["color_temp_kelvin"],
            "supports": zb["supports"],
            "scenes": sorted(scenes_by_group.get(zb["name"].lower(), []), key=lambda x: x["name"].lower()),
        })

    # Standard HA Light Groups stay allowed if someone controls them directly, without duplicating room cards.
    for hg in ha_groups:
        hb = _parse_light(hg)
        remember(hb)

    rank = {eid: idx for idx, eid in enumerate(order or ())}
    rooms.sort(key=lambda r: (rank.get(r["entity_id"], 10**6), r["name"].lower()))
    zones.sort(key=lambda z: z["name"].lower())

    bulbs_total = sum(max(1, len(r["lights"])) for r in rooms)
    bulbs_on = sum(
        sum(1 for b in r["lights"] if b["on"]) if r["lights"] else (1 if r["on"] else 0)
        for r in rooms
    )
    summary = {
        "on": bulbs_on,
        "total": bulbs_total,
        "rooms_on": sum(1 for r in rooms if r["on"]),
        "rooms_total": len(rooms),
    }
    return {"summary": summary, "rooms": rooms, "zones": zones}, known_lights, known_scenes


async def view(cache: ControlsState | None = None) -> dict[str, Any]:
    if not ha.token():
        return {"configured": False, "available": False, "summary": {"on": 0, "total": 0, "rooms_on": 0, "rooms_total": 0}, "rooms": [], "zones": []}
    states = await ha.request("GET", "/api/states")
    shaped, known_lights, known_scenes = parse_states(states, saved_order())
    if cache is not None:
        cache.update(known_lights, known_scenes)
    return {"configured": True, "available": True, **shaped}


async def set_light(entity_id: str, ch: LightChange, cache: ControlsState | None = None) -> None:
    """Apply one lighting command to a known Home Assistant light.* entity."""
    if not LIGHT_ID.match(entity_id):
        raise LookupError("no such light")
    info = cache.lights.get(entity_id) if cache is not None else None
    if info is None:
        states = await ha.request("GET", "/api/states")
        _, known_lights, known_scenes = parse_states(states)
        if cache is not None:
            cache.update(known_lights, known_scenes)
        info = known_lights.get(entity_id)
        if info is None:
            raise LookupError("no such light")

    if ch.on is False:
        payload: dict[str, Any] = {"entity_id": entity_id}
        if ch.transition is not None:
            payload["transition"] = ch.transition
        await ha.service("light", "turn_off", payload)
        return

    payload = {"entity_id": entity_id}
    if ch.brightness is not None:
        payload["brightness_pct"] = ch.brightness
    if ch.kelvin is not None:
        lo, hi = info["min_k"], info["max_k"]
        payload["color_temp_kelvin"] = max(lo, min(hi, ch.kelvin))
    if ch.rgb is not None:
        payload["rgb_color"] = list(ch.rgb)
    if ch.transition is not None:
        payload["transition"] = ch.transition
    await ha.service("light", "turn_on", payload)


async def activate_scene(entity_id: str, cache: ControlsState | None = None) -> None:
    """Activate a known Home Assistant Hue scene.* entity."""
    if not SCENE_ID.match(entity_id):
        raise LookupError("no such scene")
    if cache is None or entity_id not in cache.scenes:
        states = await ha.request("GET", "/api/states")
        _, known_lights, known_scenes = parse_states(states)
        if cache is not None:
            cache.update(known_lights, known_scenes)
        if entity_id not in known_scenes:
            raise LookupError("no such scene")
    await ha.service("scene", "turn_on", {"entity_id": entity_id})


async def all_off(cache: ControlsState | None = None) -> dict[str, Any]:
    """Turn off every currently-on managed room / standalone light in one Home Assistant service call."""
    current = await view(cache)
    if not current.get("configured"):
        raise ha.Unavailable("No Home Assistant token on this PC (HomeLab home-assistant/set-token.sh)")
    on_eids = [r["entity_id"] for r in current["rooms"] if r["on"] and r["available"]]
    if on_eids:
        await ha.service("light", "turn_off", {"entity_id": on_eids, "transition": 0.4})
    return {"ok": True, "turned_off": len(on_eids)}
