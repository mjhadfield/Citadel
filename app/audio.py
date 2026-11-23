"""The Audio page (#audio, web/js/audio.js): Citadel's own GoXLR mixer. Plan: ~/Projects/HomeLab/audio.

Two halves, both pure (the connection is `app/goxlr.py`):

- `view(serial, mixer, status)` shapes the daemon's ~13 KB status into what the page draws: volumes as %, the faders and
  their mutes, routing, profiles, effects, the microphone, submix and sampler. The page never sees the daemon's own
  shape, so a daemon update that moves things around is fixed here once.
- `command(body, mixer, files)` turns one page action (`{"cmd": …}`, validated by the pydantic models below) into the
  daemon command to send. It's a whitelist: only these actions exist, each range-checked, and names (profiles, mic
  profiles) must be ones the daemon listed. Nothing from the browser is passed through blindly.

- Lighting (session 4): `view()` adds `lighting` (every colour and style, hex upper-case), and `commands()` turns the
  Lighting tab's actions (`light`, `anim`, `light_theme`, `light_copy`) into one or more daemon commands: "Apply to
  all" and "Match my theme" are several commands, sent in order. Ranges and which animation modes take which setting
  are from the utility's source (HomeLab/audio/README.md "Lighting details"); the daemon refuses the rest, so we do too.

- `Unsaved` tracks whether the profile and the mic profile have changes that aren't saved. The daemon applies every
  change live but only writes the profile file on Save, and loading a profile drops what wasn't saved; it doesn't
  report "unsaved" itself, so the hub compares each part of the status with how it was when it was last loaded or
  saved (a fingerprint per profile name). That also catches changes made on the hardware or in the utility's own page.
  After a hub restart the first status seen counts as saved.

Argument shapes and ranges come from the goxlr-utility v1.2.4 source and its own page (HomeLab/audio/README.md). Two
need care:
- **Mute** sets a state, not a toggle: `MutedToAll` when the fader's (or cough button's) mute type is `All`, otherwise
  `MutedToX` (muted to whatever its type names), as the hardware's press does.
- **Pitch** is sent in the daemon's own units, which depend on the style and on Hard Tune: Narrow counts half-semitones
  (±24 for ±12), Wide whole semitones (±24), and with Hard Tune on it moves in octave steps (±1 / ±2). The page works in
  semitones; `_pitch_*` converts both ways, as the utility's page does.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from .goxlr import to_pct, to_raw

# ---- names --------------------------------------------------------------------------------------------------------

INPUTS = [("Mic", "Mic"), ("Chat", "Voice chat"), ("Music", "Music"), ("Game", "Game"), ("Console", "Console"),
          ("LineIn", "Vinyl"), ("System", "System"), ("Sample", "Samples")]
OUTPUTS = [("Headphones", "Headphones"), ("LineOut", "Line out"), ("MicMonitor", "Mic monitor")]
CHANNELS = {c for c, _ in INPUTS + OUTPUTS}
SUBMIX_OUTPUTS = {"Headphones": "Headphones", "LineOut": "LineOut"}     # our output strip → the daemon's OutputDevice

ROUTE_IN = [("Microphone", "Mic"), ("Chat", "Chat"), ("Music", "Music"), ("Game", "Game"), ("Console", "Console"),
            ("LineIn", "Vinyl"), ("System", "System"), ("Samples", "Samples")]
ROUTE_OUT = [("Headphones", "Headphones", "HP"), ("BroadcastMix", "Stream", "Strm"), ("ChatMic", "Chat mic", "Chat"),
             ("Sampler", "Sampler", "Smp"), ("LineOut", "Line out", "Out"), ("StreamMix2", "Stream 2", "St 2")]
ROUTE_BLOCKED = {("Chat", "ChatMic")}       # the hardware can't send chat back into itself; the utility greys it out too

FADERS = ("A", "B", "C", "D")
PRESETS = tuple(f"Preset{i}" for i in range(1, 7))
MIC_TYPES = ("Dynamic", "Condenser", "Jack")
EQ = [("Equalizer31Hz", "31"), ("Equalizer63Hz", "63"), ("Equalizer125Hz", "125"), ("Equalizer250Hz", "250"),
      ("Equalizer500Hz", "500"), ("Equalizer1KHz", "1K"), ("Equalizer2KHz", "2K"), ("Equalizer4KHz", "4K"),
      ("Equalizer8KHz", "8K"), ("Equalizer16KHz", "16K")]
EQ_MINI = [("Equalizer90Hz", "90"), ("Equalizer250Hz", "250"), ("Equalizer500Hz", "500"), ("Equalizer1KHz", "1K"),
           ("Equalizer3KHz", "3K"), ("Equalizer8KHz", "8K")]
PADS = [("TopLeft", "Top left"), ("TopRight", "Top right"), ("BottomLeft", "Bottom left"), ("BottomRight", "Bottom right")]
PAD_FN = {"PlayNext": "Play next", "PlayStop": "Play / stop", "PlayFade": "Play / fade", "StopOnRelease": "Stop on release",
          "FadeOnRelease": "Fade on release", "Loop": "Loop"}
# the daemon gives (and takes) these as step numbers: the labels for each step
GATE_MS = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 250, 300, 350,
           400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 900, 950, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700,
           1800, 1900, 2000]
RATIO = ["1.0", "1.1", "1.2", "1.4", "1.6", "1.8", "2.0", "2.5", "3.2", "4.0", "5.6", "8.0", "16", "32", "64"]
COMP_ATTACK_MS = [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 20, 23, 26, 30, 35, 40]
COMP_RELEASE_MS = [0, 15, 25, 35, 45, 55, 65, 75, 85, 100, 115, 140, 170, 230, 340, 680, 1000, 1500, 2000, 3000]
GENDER_RANGE = {"Narrow": 12, "Medium": 25, "Wide": 50}


def _muted(state: str | None) -> bool:
    return bool(state) and state != "Unmuted"


def _mute_state(mute_type: str | None) -> str:
    return "MutedToAll" if (mute_type or "All") == "All" else "MutedToX"


def _pitch_semis(amount: int, style: str, hardtune: bool) -> int:
    if hardtune:
        return amount * 12
    return amount // 2 if style == "Narrow" else amount


def _pitch_amount(semis: int, style: str, hardtune: bool) -> int:
    if hardtune:
        return round(semis / 12)
    return semis * 2 if style == "Narrow" else semis


def _pitch_range(style: str, hardtune: bool) -> tuple[int, int, int]:
    top = 12 if style == "Narrow" else 24
    return -top, top, (12 if hardtune else 1)


def _ver(v: Any) -> str:
    return ".".join(str(x) for x in (v or [])[:3])


# ---- the page's view ------------------------------------------------------------------------------------------------

def view(serial: str, mixer: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    hw = mixer.get("hardware") or {}
    mini = hw.get("device_type") == "Mini"
    files = status.get("files") or {}
    lv = mixer.get("levels") or {}
    vols = lv.get("volumes") or {}
    sub = lv.get("submix") or None
    sub_in = (sub or {}).get("inputs") or {}
    sub_out = (sub or {}).get("outputs") or {}
    faders = mixer.get("fader_status") or {}
    cough = mixer.get("cough_button") or {}
    mic = mixer.get("mic_status") or {}
    gate, comp = mic.get("noise_gate") or {}, mic.get("compressor") or {}
    eq_src, eq_names = (mic.get("equaliser_mini"), EQ_MINI) if mini else (mic.get("equaliser"), EQ)
    gains = (eq_src or {}).get("gain") or {}
    router = mixer.get("router") or {}

    out: dict[str, Any] = {
        "available": True,
        "device": {"type": "GoXLR Mini" if mini else "GoXLR", "firmware": _ver((hw.get("versions") or {}).get("firmware")),
                   "daemon": (status.get("config") or {}).get("daemon_version") or ""},
        "profile": mixer.get("profile_name") or "", "profiles": sorted(files.get("profiles") or [], key=str.lower),
        "mic_profile": mixer.get("mic_profile_name") or "", "mic_profiles": sorted(files.get("mic_profiles") or [], key=str.lower),
        "faders": [{"id": f, "channel": (faders.get(f) or {}).get("channel"), "muted": _muted((faders.get(f) or {}).get("mute_state")),
                    "mute_to": (faders.get(f) or {}).get("mute_type") or "All"} for f in FADERS],
        "cough": {"muted": _muted(cough.get("state")), "mute_to": cough.get("mute_type") or "All"},
        "channels": [{"id": c, "name": n, "v": to_pct(vols.get(c, 0)),
                      **({"sub": to_pct((sub_in.get(c) or {}).get("volume", 0)), "linked": bool((sub_in.get(c) or {}).get("linked"))} if sub else {})}
                     for c, n in INPUTS],
        "outputs": [{"id": c, "name": n, "v": to_pct(vols.get(c, 0)),
                     **({"mix": sub_out.get(SUBMIX_OUTPUTS[c], "A")} if sub and c in SUBMIX_OUTPUTS else {})}
                    for c, n in OUTPUTS],
        "monitor": lv.get("output_monitor") or "Headphones",
        "submix": {"supported": bool(lv.get("submix_supported")), "enabled": sub is not None},
        "router": {"inputs": [list(i) for i in ROUTE_IN], "outputs": [list(o) for o in ROUTE_OUT],
                   "on": {i: [bool((router.get(i) or {}).get(o)) for o, _, _ in ROUTE_OUT] for i, _ in ROUTE_IN},
                   "blocked": [list(b) for b in sorted(ROUTE_BLOCKED)]},
        "mic": {"type": mic.get("mic_type") or "Jack", "gain": int(((mic.get("mic_gains") or {}).get(mic.get("mic_type") or "Jack")) or 0),
                "gate": {"on": bool(gate.get("enabled")), "threshold": gate.get("threshold", 0), "attenuation": gate.get("attenuation", 0),
                         "attack": gate.get("attack", 0), "release": gate.get("release", 0)},
                "comp": {"threshold": comp.get("threshold", 0), "ratio": comp.get("ratio", 0), "attack": comp.get("attack", 0),
                         "release": comp.get("release", 0), "makeup": comp.get("makeup_gain", 0)},
                "eq": [[label, int(gains.get(band, 0))] for band, label in eq_names],
                "deess": None if mini else lv.get("deess", 0), "bleep": lv.get("bleep", 0)},
        "scales": {"gate_ms": GATE_MS, "ratio": RATIO, "comp_attack_ms": COMP_ATTACK_MS, "comp_release_ms": COMP_RELEASE_MS},
        "effects": None, "sampler": None,
    }
    fx = mixer.get("effects")
    if fx and not mini:
        cur = fx.get("current") or {}
        pitch, gender = cur.get("pitch") or {}, cur.get("gender") or {}
        ht = bool((cur.get("hard_tune") or {}).get("is_enabled"))
        pmin, pmax, pstep = _pitch_range(pitch.get("style") or "Narrow", ht)
        g = GENDER_RANGE.get(gender.get("style") or "Narrow", 12)
        names = fx.get("preset_names") or {}
        out["effects"] = {
            "enabled": bool(fx.get("is_enabled")), "preset": fx.get("active_preset") or "Preset1",
            "presets": [[p, names.get(p) or f"Preset {i}"] for i, p in enumerate(PRESETS, 1)],
            "megaphone": bool((cur.get("megaphone") or {}).get("is_enabled")), "robot": bool((cur.get("robot") or {}).get("is_enabled")),
            "hardtune": ht,
            "amounts": {"reverb": (cur.get("reverb") or {}).get("amount", 0), "echo": (cur.get("echo") or {}).get("amount", 0),
                        "pitch": _pitch_semis(pitch.get("amount", 0), pitch.get("style") or "Narrow", ht), "pitch_min": pmin, "pitch_max": pmax, "pitch_step": pstep,
                        "gender": gender.get("amount", 0), "gender_min": -g, "gender_max": g},
        }
    smp = mixer.get("sampler")
    if smp and not mini:
        bank = smp.get("active_bank") or "A"
        btns = (smp.get("banks") or {}).get(bank) or {}
        out["sampler"] = {"bank": bank, "pads": [
            {"id": b, "name": n, "samples": [s.get("name", "") for s in (btns.get(b) or {}).get("samples") or []],
             "fn": PAD_FN.get((btns.get(b) or {}).get("function"), ""), "playing": bool((btns.get(b) or {}).get("is_playing"))}
            for b, n in PADS]}
    out["lighting"] = _lighting_view(mixer, mini)
    return out


# ---- lighting ---------------------------------------------------------------------------------------------------------

SCRIBBLES = {"A": "Scribble1", "B": "Scribble2", "C": "Scribble3", "D": "Scribble4"}
MUTE_BUTTONS = {"A": "Fader1Mute", "B": "Fader2Mute", "C": "Fader3Mute", "D": "Fader4Mute"}
PRESET_BUTTONS = tuple(f"EffectSelect{i}" for i in range(1, 7))
EFFECT_BUTTONS = ("EffectMegaphone", "EffectRobot", "EffectHardTune", "EffectFx")
KNOBS = ("Reverb", "Echo", "Pitch", "Gender")
BANKS = ("A", "B", "C")
ANIM_MODES = ("None", "Simple", "Ripple", "RainbowBright", "RainbowDark", "RetroRainbow")
FADER_STYLES = ("TwoColour", "Gradient", "Meter", "GradientMeter")
OFF_STYLES = ("Dimmed", "Colour2", "DimmedColour2")


def _hex(c: Any) -> str:
    return (c or "000000").upper()


def _lighting_view(mixer: dict[str, Any], mini: bool) -> dict[str, Any]:
    """Every colour the Lighting tab draws. Colour slots are renamed to what they are on the device (the daemon's
    colour_one/two/three mean different things per part: README "Which colour is which")."""
    lt = mixer.get("lighting") or {}
    simple, btns, anim = lt.get("simple") or {}, lt.get("buttons") or {}, lt.get("animation") or {}
    fs = mixer.get("fader_status") or {}
    sc = lambda k: _hex((simple.get(k) or {}).get("colour_one"))   # noqa: E731

    def button(name: str) -> dict[str, str]:
        b = btns.get(name) or {}
        c = b.get("colours") or {}
        return {"on": _hex(c.get("colour_one")), "off": _hex(c.get("colour_two")), "off_style": b.get("off_style") or "Dimmed"}

    faders = {}
    for f in FADERS:
        fl = (lt.get("faders") or {}).get(f) or {}
        c, scr = fl.get("colours") or {}, (fs.get(f) or {}).get("scribble") or {}
        faders[f] = {"style": fl.get("style") or "Gradient", "top": _hex(c.get("colour_one")), "bottom": _hex(c.get("colour_two")),
                     "screen": sc(SCRIBBLES[f]), "text": scr.get("bottom_text") or "", "number": scr.get("left_text") or ""}
    names = list(MUTE_BUTTONS.values()) + ["Cough", "Bleep"] + ([] if mini else list(PRESET_BUTTONS + EFFECT_BUTTONS))
    knobs = {k: {"left": _hex(e.get("colour_two")), "right": _hex(e.get("colour_one")), "knob": _hex(e.get("colour_three"))}
             for k in ([] if mini else KNOBS) for e in [(lt.get("encoders") or {}).get(k) or {}]}
    banks = {}
    for b in ([] if mini else BANKS):
        sb = (lt.get("sampler") or {}).get(f"SamplerSelect{b}") or {}
        c = sb.get("colours") or {}
        banks[b] = {"on": _hex(c.get("colour_one")), "off": _hex(c.get("colour_two")), "empty": _hex(c.get("colour_three")),
                    "off_style": sb.get("off_style") or "Dimmed"}
    return {
        # firmware decides whether animations exist at all; the Mini has them too, just not Ripple
        "animation": {"mode": anim.get("mode") or "None", "mod1": anim.get("mod1", 0), "mod2": anim.get("mod2", 0),
                      "waterfall": anim.get("waterfall_direction") or "Down"} if anim.get("supported") else None,
        "global": sc("Global"), "accent": sc("Accent"), "faders": faders,
        "buttons": {n: button(n) for n in names}, "knobs": knobs, "banks": banks,
    }


# ---- the page's actions ---------------------------------------------------------------------------------------------

Channel = Literal["Mic", "Chat", "Music", "Game", "Console", "LineIn", "System", "Sample", "Headphones", "LineOut", "MicMonitor"]
SubChannel = Literal["Mic", "Chat", "Music", "Game", "Console", "LineIn", "System", "Sample"]
Pct = Annotated[int, Field(ge=0, le=100)]


class _Volume(BaseModel):
    cmd: Literal["volume"]
    channel: Channel
    value: Pct


class _FaderMute(BaseModel):
    cmd: Literal["fader_mute"]
    fader: Literal["A", "B", "C", "D"]
    muted: bool


class _Cough(BaseModel):
    cmd: Literal["cough"]
    muted: bool


class _Route(BaseModel):
    cmd: Literal["route"]
    input: Literal["Microphone", "Chat", "Music", "Game", "Console", "LineIn", "System", "Samples"]
    output: Literal["Headphones", "BroadcastMix", "ChatMic", "Sampler", "LineOut", "StreamMix2"]
    on: bool


class _Profile(BaseModel):
    cmd: Literal["profile", "mic_profile"]
    name: str = Field(min_length=1, max_length=200)


class _Preset(BaseModel):
    cmd: Literal["preset"]
    preset: Literal["Preset1", "Preset2", "Preset3", "Preset4", "Preset5", "Preset6"]


class _Switch(BaseModel):
    cmd: Literal["switch"]
    what: Literal["fx", "megaphone", "robot", "hardtune", "submix", "gate"]
    on: bool


class _Amount(BaseModel):
    cmd: Literal["amount"]
    what: Literal["reverb", "echo", "pitch", "gender"]
    value: int = Field(ge=-50, le=100)


class _SubVolume(BaseModel):
    cmd: Literal["sub_volume"]
    channel: SubChannel
    value: Pct


class _SubLink(BaseModel):
    cmd: Literal["sub_link"]
    channel: SubChannel
    linked: bool


class _OutMix(BaseModel):
    cmd: Literal["out_mix"]
    output: Literal["Headphones", "LineOut"]
    mix: Literal["A", "B"]


class _Monitor(BaseModel):
    cmd: Literal["monitor"]
    output: Literal["Headphones", "BroadcastMix"]


class _MicType(BaseModel):
    cmd: Literal["mic_type"]
    type: Literal["Dynamic", "Condenser", "Jack"]


class _MicGain(BaseModel):
    cmd: Literal["mic_gain"]
    value: int = Field(ge=0, le=72)


class _Gate(BaseModel):
    cmd: Literal["gate"]
    what: Literal["threshold", "attenuation", "attack", "release"]
    value: int


class _Comp(BaseModel):
    cmd: Literal["comp"]
    what: Literal["threshold", "ratio", "attack", "release", "makeup"]
    value: int


class _Eq(BaseModel):
    cmd: Literal["eq"]
    band: int = Field(ge=0, le=9)
    value: int = Field(ge=-9, le=9)


class _Deess(BaseModel):
    cmd: Literal["deess"]
    value: Pct


class _Bleep(BaseModel):
    cmd: Literal["bleep"]
    value: int = Field(ge=-34, le=0)


class _Bank(BaseModel):
    cmd: Literal["bank"]
    bank: Literal["A", "B", "C"]


class _Pad(BaseModel):
    cmd: Literal["pad"]
    pad: Literal["TopLeft", "TopRight", "BottomLeft", "BottomRight"]
    play: bool


class _Save(BaseModel):
    cmd: Literal["save"]
    which: Literal["profile", "mic_profile"]


class _SaveAs(BaseModel):
    cmd: Literal["save_as"]
    which: Literal["profile", "mic_profile"]
    name: str = Field(min_length=1, max_length=64)


Hex = Annotated[str, Field(pattern=r"^[0-9A-Fa-f]{6}$")]
# a part of the front panel, as the Lighting tab names it
PART = (r"^(?:(?:fader|scribble|mute):[ABCD]|knob:(?:Reverb|Echo|Pitch|Gender)|bank:[ABC]"
        r"|btn:(?:Cough|Bleep|EffectSelect[1-6]|EffectFx|EffectMegaphone|EffectRobot|EffectHardTune)|global|accent)$")


class _Light(BaseModel):
    """A part's colours (in the order the page shows them), its style, or both; `all` = every part of its kind."""
    cmd: Literal["light"]
    part: str = Field(pattern=PART)
    colours: list[Hex] | None = Field(default=None, min_length=1, max_length=3)
    style: Literal["TwoColour", "Gradient", "Meter", "GradientMeter", "Dimmed", "Colour2", "DimmedColour2"] | None = None
    all: bool = False


class _Anim(BaseModel):
    cmd: Literal["anim"]
    mode: Literal["None", "Simple", "Ripple", "RainbowBright", "RainbowDark", "RetroRainbow"] | None = None
    mod1: int | None = Field(default=None, ge=0, le=100)
    mod2: int | None = Field(default=None, ge=0, le=100)
    waterfall: Literal["Down", "Up", "Off"] | None = None


class _Theme(BaseModel):
    """The current theme's Night colours, brightened for LEDs by the page (it knows the theme; the hub doesn't)."""
    cmd: Literal["light_theme"]
    frame: Hex
    soft: Hex
    alt: Hex
    warm: Hex
    attention: Hex
    alert: Hex


class _CopyColours(BaseModel):
    cmd: Literal["light_copy"]
    name: str = Field(min_length=1, max_length=200)


Action = Annotated[Union[_Volume, _FaderMute, _Cough, _Route, _Profile, _Preset, _Switch, _Amount, _SubVolume, _SubLink,
                         _OutMix, _Monitor, _MicType, _MicGain, _Gate, _Comp, _Eq, _Deess, _Bleep, _Bank, _Pad, _Save, _SaveAs,
                         _Light, _Anim, _Theme, _CopyColours],
                   Field(discriminator="cmd")]
ACTION = TypeAdapter(Action)

# (lowest, highest) for the step-numbered and dB settings
GATE_RANGES = {"threshold": (-59, 0), "attenuation": (0, 100), "attack": (0, len(GATE_MS) - 1), "release": (0, len(GATE_MS) - 1)}
COMP_RANGES = {"threshold": (-40, 0), "ratio": (0, len(RATIO) - 1), "attack": (0, len(COMP_ATTACK_MS) - 1),
               "release": (0, len(COMP_RELEASE_MS) - 1), "makeup": (0, 24)}
GATE_CMD = {"threshold": "SetGateThreshold", "attenuation": "SetGateAttenuation", "attack": "SetGateAttack", "release": "SetGateRelease"}
COMP_CMD = {"threshold": "SetCompressorThreshold", "ratio": "SetCompressorRatio", "attack": "SetCompressorAttack",
            "release": "SetCompressorReleaseTime", "makeup": "SetCompressorMakeupGain"}
SWITCH_CMD = {"fx": "SetFXEnabled", "megaphone": "SetMegaphoneEnabled", "robot": "SetRobotEnabled",
              "hardtune": "SetHardTuneEnabled", "submix": "SetSubMixEnabled", "gate": "SetGateActive"}


BAD_NAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


class Refused(ValueError):
    """The action is well-formed but not allowed right now (a blocked route, an unknown profile, out of range)."""


def parse(body: Any) -> Any:
    return ACTION.validate_python(body)


def _range(value: int, lo_hi: tuple[int, int], what: str) -> int:
    lo, hi = lo_hi
    if not lo <= value <= hi:
        raise Refused(f"{what} must be between {lo} and {hi}")
    return value


LIGHT_CMDS = ("light", "anim", "light_theme", "light_copy")


def commands(a: Any, mixer: dict[str, Any], status: dict[str, Any]) -> list[dict[str, Any]]:
    """One page action → the daemon commands to send, in order. Raises Refused for anything not allowed."""
    if a.cmd in LIGHT_CMDS:
        return _light_commands(a, mixer, status)
    return [command(a, mixer, status)]


def command(a: Any, mixer: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    """One page action → the daemon command ({Name: args}). Raises Refused for anything not allowed."""
    files = status.get("files") or {}
    mini = (mixer.get("hardware") or {}).get("device_type") == "Mini"
    match a.cmd:
        case "volume":
            return {"SetVolume": [a.channel, to_raw(a.value)]}
        case "fader_mute":
            f = (mixer.get("fader_status") or {}).get(a.fader) or {}
            return {"SetFaderMuteState": [a.fader, _mute_state(f.get("mute_type")) if a.muted else "Unmuted"]}
        case "cough":
            c = mixer.get("cough_button") or {}
            return {"SetCoughMuteState": _mute_state(c.get("mute_type")) if a.muted else "Unmuted"}
        case "route":
            if (a.input, a.output) in ROUTE_BLOCKED:
                raise Refused("Chat can't be routed to the chat mic")
            return {"SetRouter": [a.input, a.output, a.on]}
        case "profile" | "mic_profile":
            known = files.get("profiles" if a.cmd == "profile" else "mic_profiles") or []
            if a.name not in known:
                raise Refused(f"no profile called {a.name}")
            return {("LoadProfile" if a.cmd == "profile" else "LoadMicProfile"): [a.name, True]}
        case "preset":
            return {"SetActiveEffectPreset": a.preset}
        case "switch":
            if a.what == "submix" and not (mixer.get("levels") or {}).get("submix_supported"):
                raise Refused("this GoXLR has no submix (it needs newer firmware)")
            return {SWITCH_CMD[a.what]: a.on}
        case "amount":
            cur = ((mixer.get("effects") or {}).get("current") or {})
            if a.what in ("reverb", "echo"):
                return {("SetReverbAmount" if a.what == "reverb" else "SetEchoAmount"): _range(a.value, (0, 100), a.what)}
            if a.what == "gender":
                g = GENDER_RANGE.get((cur.get("gender") or {}).get("style") or "Narrow", 12)
                return {"SetGenderAmount": _range(a.value, (-g, g), "gender")}
            style = (cur.get("pitch") or {}).get("style") or "Narrow"
            ht = bool((cur.get("hard_tune") or {}).get("is_enabled"))
            lo, hi, _ = _pitch_range(style, ht)
            return {"SetPitchAmount": _pitch_amount(_range(a.value, (lo, hi), "pitch"), style, ht)}
        case "sub_volume":
            return {"SetSubMixVolume": [a.channel, to_raw(a.value)]}
        case "sub_link":
            return {"SetSubMixLinked": [a.channel, a.linked]}
        case "out_mix":
            return {"SetSubMixOutputMix": [SUBMIX_OUTPUTS[a.output], a.mix]}
        case "monitor":
            return {"SetMonitorMix": a.output}
        case "mic_type":
            return {"SetMicrophoneType": a.type}
        case "mic_gain":
            kind = (mixer.get("mic_status") or {}).get("mic_type") or "Jack"
            return {"SetMicrophoneGain": [kind, a.value]}
        case "gate":
            return {GATE_CMD[a.what]: _range(a.value, GATE_RANGES[a.what], f"gate {a.what}")}
        case "comp":
            return {COMP_CMD[a.what]: _range(a.value, COMP_RANGES[a.what], f"compressor {a.what}")}
        case "eq":
            bands = EQ_MINI if mini else EQ
            if a.band >= len(bands):
                raise Refused("no such EQ band")
            return {("SetEqMiniGain" if mini else "SetEqGain"): [bands[a.band][0], a.value]}
        case "deess":
            return {"SetDeeser": a.value}
        case "bleep":
            return {"SetSwearButtonVolume": a.value}
        case "bank":
            return {"SetActiveSamplerBank": a.bank}
        case "pad":
            bank = (mixer.get("sampler") or {}).get("active_bank") or "A"
            return {"PlayNextSample": [bank, a.pad]} if a.play else {"StopSamplePlayback": [bank, a.pad]}
        case "save":
            return {("SaveProfile" if a.which == "profile" else "SaveMicProfile"): []}
        case "save_as":
            name = a.name.strip()
            # the name becomes a file in the utility's profile folder: no path pieces, no hidden files
            if not name or BAD_NAME.search(name) or name.startswith(".") or name in (".", ".."):
                raise Refused("A profile name can't contain / \\ : * ? \" < > | or start with a dot")
            known = files.get("profiles" if a.which == "profile" else "mic_profiles") or []
            if name.lower() in (k.lower() for k in known):
                raise Refused(f"There's already a profile called {name}")
            return {("SaveProfileAs" if a.which == "profile" else "SaveMicProfileAs"): name}
        case "light" | "anim" | "light_theme" | "light_copy":
            cmds = _light_commands(a, mixer, status)
            if len(cmds) != 1:
                raise Refused("that change takes several commands")          # callers use commands() for these
            return cmds[0]
    raise Refused("unknown action")                                   # pragma: no cover - the union above is exhaustive


# how many colours each kind of part has, in the page's order
LIGHT_COLOURS = {"fader": 2, "scribble": 1, "mute": 2, "btn": 2, "knob": 3, "bank": 3, "global": 1, "accent": 1}


def _btn_group(name: str) -> str | None:
    if name in MUTE_BUTTONS.values():
        return "FaderMute"
    if name in PRESET_BUTTONS:
        return "EffectSelector"
    if name in EFFECT_BUTTONS:
        return "EffectTypes"
    return None


def _dim(h: str, k: float = 0.2) -> str:
    return "".join(f"{round(int(h[i:i + 2], 16) * k):02X}" for i in (0, 2, 4))


def _light_commands(a: Any, mixer: dict[str, Any], status: dict[str, Any]) -> list[dict[str, Any]]:
    mini = (mixer.get("hardware") or {}).get("device_type") == "Mini"
    anim = (mixer.get("lighting") or {}).get("animation") or {}
    if a.cmd == "anim":
        if not anim.get("supported"):
            raise Refused("This GoXLR's firmware has no lighting animations")
        given = [k for k in ("mode", "mod1", "mod2", "waterfall") if getattr(a, k) is not None]
        if len(given) != 1:
            raise Refused("Change one animation setting at a time")
        mode = anim.get("mode") or "None"
        if a.mode is not None:
            if a.mode == "Ripple" and mini:
                raise Refused("The GoXLR Mini has no Ripple animation")
            return [{"SetAnimationMode": a.mode}]
        if a.mod1 is not None:
            if mode == "None":
                raise Refused("Gradient 1 needs an animation")
            return [{"SetAnimationMod1": a.mod1}]
        if a.mod2 is not None:
            if mode not in ("RainbowBright", "RainbowDark"):
                raise Refused("Gradient 2 is only for Rainbow bright and Rainbow dark")
            return [{"SetAnimationMod2": a.mod2}]
        if mode in ("None", "RetroRainbow"):
            raise Refused("This animation has no waterfall")
        return [{"SetAnimationWaterfall": a.waterfall}]

    if a.cmd == "light_copy":
        known = (status.get("files") or {}).get("profiles") or []
        if a.name not in known:
            raise Refused(f"no profile called {a.name}")
        return [{"LoadProfileColours": a.name}]

    if a.cmd == "light_theme":
        T = {k: getattr(a, k).upper() for k in ("frame", "soft", "alt", "warm", "attention", "alert")}
        out: list[dict[str, Any]] = [{"SetGlobalColour": T["frame"]}, {"SetSimpleColour": ["Accent", T["alt"]]}]
        out += [{"SetSimpleColour": [SCRIBBLES[f], c]} for f, c in zip(FADERS, (T["frame"], T["soft"], T["alt"], T["warm"]))]
        out += [{"SetAllFaderColours": [T["soft"], T["frame"]]}, {"SetAllFaderDisplayStyle": "Gradient"},   # Mike, 2026-11-23
                {"SetButtonGroupColours": ["FaderMute", T["alert"], T["frame"]]}, {"SetButtonGroupOffStyle": ["FaderMute", "DimmedColour2"]},
                {"SetButtonColours": ["Cough", T["alert"], T["frame"]]}, {"SetButtonOffStyle": ["Cough", "DimmedColour2"]},
                {"SetButtonColours": ["Bleep", T["attention"], T["frame"]]}, {"SetButtonOffStyle": ["Bleep", "DimmedColour2"]}]
        if not mini:
            out += [{"SetButtonGroupColours": ["EffectSelector", T["attention"], T["frame"]]}, {"SetButtonGroupOffStyle": ["EffectSelector", "DimmedColour2"]},
                    {"SetButtonGroupColours": ["EffectTypes", T["warm"], T["frame"]]}, {"SetButtonGroupOffStyle": ["EffectTypes", "DimmedColour2"]}]
            out += [{"SetEncoderColour": [k, _dim(T["frame"]), T["soft"], T["frame"]]} for k in KNOBS]
            for b in BANKS:
                out += [{"SetSampleColour": [f"SamplerSelect{b}", T["attention"], T["frame"], "000000"]},
                        {"SetSampleOffStyle": [f"SamplerSelect{b}", "DimmedColour2"]}]
        return out

    # "light": one part's colours and/or style, or every part of its kind
    kind, _, key = a.part.partition(":")
    if mini and (kind in ("knob", "bank") or (kind == "btn" and key not in ("Cough", "Bleep"))):
        raise Refused("The GoXLR Mini doesn't have that")
    if a.colours is None and a.style is None:
        raise Refused("Nothing to change")
    cols = [c.upper() for c in a.colours or []]
    if a.colours is not None and len(cols) != LIGHT_COLOURS[kind]:
        raise Refused(f"{a.part} takes {LIGHT_COLOURS[kind]} colour(s)")
    if a.style is not None:
        allowed = FADER_STYLES if kind == "fader" else OFF_STYLES if kind in ("mute", "btn", "bank") else ()
        if a.style not in allowed:
            raise Refused(f"{a.part} has no style {a.style}")
    group = _btn_group(MUTE_BUTTONS[key] if kind == "mute" else key) if kind in ("mute", "btn") else None
    if a.all and (kind in ("global", "accent") or (kind == "btn" and group is None)):
        raise Refused(f"There's nothing else like {a.part}")
    out = []
    if kind == "global":
        out.append({"SetGlobalColour": cols[0]})
    elif kind == "accent":
        out.append({"SetSimpleColour": ["Accent", cols[0]]})
    elif kind == "scribble":
        out += [{"SetSimpleColour": [SCRIBBLES[f], cols[0]]} for f in (FADERS if a.all else (key,))]
    elif kind == "fader":
        if cols:
            out.append({"SetAllFaderColours": cols} if a.all else {"SetFaderColours": [key, *cols]})
        if a.style:
            out.append({"SetAllFaderDisplayStyle": a.style} if a.all else {"SetFaderDisplayStyle": [key, a.style]})
    elif kind in ("mute", "btn"):
        name = MUTE_BUTTONS[key] if kind == "mute" else key
        if cols:
            out.append({"SetButtonGroupColours": [group, *cols]} if a.all else {"SetButtonColours": [name, *cols]})
        if a.style:
            out.append({"SetButtonGroupOffStyle": [group, a.style]} if a.all else {"SetButtonOffStyle": [name, a.style]})
    elif kind == "knob":
        left, right, knob = cols
        out += [{"SetEncoderColour": [k, right, left, knob]} for k in (KNOBS if a.all else (key,))]
    elif kind == "bank":
        for b in (BANKS if a.all else (key,)):
            if cols:
                out.append({"SetSampleColour": [f"SamplerSelect{b}", *cols]})
            if a.style:
                out.append({"SetSampleOffStyle": [f"SamplerSelect{b}", a.style]})
    if not out:
        raise Refused("Nothing to change")
    return out


# ---- unsaved changes ------------------------------------------------------------------------------------------------

def _parts(mixer: dict[str, Any]) -> dict[str, Any]:
    """What each profile file holds, from the status: the mic profile is the mic's type, gains, EQ, gate, compressor,
    de-esser and bleep (goxlr-utility's mic_profile.rs); the profile is everything else that's saved. Live-only bits
    (a sample playing, buttons held) are left out so they never count as a change."""
    lv = dict(mixer.get("levels") or {})
    mic = {"mic": mixer.get("mic_status"), "deess": lv.pop("deess", None), "bleep": lv.pop("bleep", None)}
    smp = mixer.get("sampler") or {}
    banks = {bank: {b: {k: v for k, v in (btn or {}).items() if k in ("function", "order", "samples")} for b, btn in (btns or {}).items()}
             for bank, btns in (smp.get("banks") or {}).items()}
    profile = {"levels": lv, "router": mixer.get("router"), "faders": mixer.get("fader_status"), "cough": mixer.get("cough_button"),
               "lighting": mixer.get("lighting"), "effects": mixer.get("effects"), "sampler": {"bank": smp.get("active_bank"), "banks": banks}}
    return {"profile": profile, "mic_profile": mic}


def _fingerprint(part: Any) -> str:
    return hashlib.sha1(json.dumps(part, sort_keys=True, default=str).encode()).hexdigest()


class Unsaved:
    """Which of the two profiles have changed since they were loaded or saved (kept per lifespan on app.state)."""
    NAMES = {"profile": "profile_name", "mic_profile": "mic_profile_name"}

    def __init__(self) -> None:
        self._base: dict[str, tuple[str, str]] = {}          # which -> (profile name, fingerprint when loaded/saved)

    def observe(self, mixer: dict[str, Any]) -> dict[str, bool]:
        parts, out = _parts(mixer), {}
        for which, key in self.NAMES.items():
            name, fp = mixer.get(key) or "", _fingerprint(parts[which])
            base = self._base.get(which)
            if base is None or base[0] != name:              # first sight, or another profile was loaded
                self._base[which] = base = (name, fp)
            out[which] = fp != base[1]
        return out

    def saved(self, which: str, mixer: dict[str, Any]) -> None:
        self._base[which] = (mixer.get(self.NAMES[which]) or "", _fingerprint(_parts(mixer)[which]))
