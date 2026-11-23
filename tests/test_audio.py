"""The Audio page (app/audio.py) and its daemon connection (app/goxlr.py), against fakes: never the real GoXLR.

The status fixture is a read-only capture of the real daemon (goxlr-utility 1.2.4) with the serial replaced."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app import audio, goxlr
from app.main import app

STATUS = json.loads((Path(__file__).parent / "fixtures" / "goxlr_status.json").read_text())


def status() -> dict:
    return copy.deepcopy(STATUS)


def mixer_of(st: dict) -> tuple[str, dict]:
    return next(iter(st["mixers"].items()))


class FakeHttp:
    """The daemon's HTTP side: status reads and commands (recorded, and SetVolume applied)."""
    def __init__(self, st: dict | None = None, up: bool = True, answer: object = "Ok"):
        self.st = st or status()
        self.up = up
        self.answer = answer
        self.commands: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("daemon down")
        if request.url.path == "/api/get-devices":
            return httpx.Response(200, json=self.st)
        if request.url.path == "/api/command":
            serial, cmd = json.loads(request.content)["Command"]
            self.commands.append((serial, cmd))
            if "SetVolume" in cmd:
                mixer_of(self.st)[1]["levels"]["volumes"][cmd["SetVolume"][0]] = cmd["SetVolume"][1]
            return httpx.Response(200, json=self.answer)
        return httpx.Response(404)


# ---- the view ------------------------------------------------------------------------------------------------------

def test_view_shapes_the_status():
    st = status()
    serial, m = mixer_of(st)
    v = audio.view(serial, m, st)
    assert v["available"] and v["device"] == {"type": "GoXLR", "firmware": "1.4.3", "daemon": "1.2.4"}
    ch = {c["id"]: c for c in v["channels"]}
    assert ch["Mic"]["v"] == goxlr.to_pct(176) and ch["LineIn"]["name"] == "Vinyl" and "sub" not in ch["Mic"]
    assert [o["id"] for o in v["outputs"]] == ["Headphones", "LineOut", "MicMonitor"]
    assert [f["channel"] for f in v["faders"]] == ["Mic", "Music", "Chat", "System"]
    assert v["submix"] == {"supported": True, "enabled": False}
    assert v["router"]["on"]["Microphone"] == [True, True, True, True, False, True]
    assert ["Chat", "ChatMic"] in v["router"]["blocked"]
    assert v["profile"] in v["profiles"] and v["mic_profile"] in v["mic_profiles"]
    fx = v["effects"]
    assert fx["presets"][0] == ["Preset1", "Big Verb"] and fx["amounts"]["reverb"] == 62
    assert (fx["amounts"]["pitch_min"], fx["amounts"]["pitch_max"]) == (-12, 12)
    assert v["mic"]["comp"]["ratio"] == 8 and v["scales"]["ratio"][8] == "3.2" and len(v["mic"]["eq"]) == 10
    assert v["sampler"]["pads"][0]["samples"] == ["airhorn.wav"]


def test_view_with_submix_on_and_muted_fader():
    st = status()
    serial, m = mixer_of(st)
    m["levels"]["submix"] = {"inputs": {c: {"volume": 51, "linked": c != "Music", "ratio": 1.0} for c, _ in audio.INPUTS},
                             "outputs": {"Headphones": "B", "LineOut": "A", "BroadcastMix": "A", "ChatMic": "A", "Sampler": "A", "StreamMix2": "B"}}
    m["fader_status"]["B"]["mute_state"] = "MutedToX"
    v = audio.view(serial, m, st)
    ch = {c["id"]: c for c in v["channels"]}
    assert v["submix"]["enabled"] and ch["Music"]["sub"] == 20 and ch["Music"]["linked"] is False and ch["Mic"]["linked"]
    assert {o["id"]: o.get("mix") for o in v["outputs"]} == {"Headphones": "B", "LineOut": "A", "MicMonitor": None}
    assert v["faders"][1]["muted"] is True


def test_pitch_is_shown_and_sent_in_semitones():
    st = status()
    _, m = mixer_of(st)
    pitch, ht = m["effects"]["current"]["pitch"], m["effects"]["current"]["hard_tune"]
    for style, hardtune, semis, amount in [("Narrow", False, 5, 10), ("Wide", False, -20, -20), ("Wide", True, 24, 2), ("Narrow", True, -12, -1)]:
        pitch["style"], ht["is_enabled"] = style, hardtune
        assert audio.command(audio.parse({"cmd": "amount", "what": "pitch", "value": semis}), m, st) == {"SetPitchAmount": amount}
        pitch["amount"] = amount
        assert audio.view("S", m, st)["effects"]["amounts"]["pitch"] == semis


# ---- actions → daemon commands ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("body, expected", [
    ({"cmd": "volume", "channel": "Music", "value": 50}, {"SetVolume": ["Music", 128]}),
    ({"cmd": "fader_mute", "fader": "B", "muted": True}, {"SetFaderMuteState": ["B", "MutedToAll"]}),
    ({"cmd": "fader_mute", "fader": "B", "muted": False}, {"SetFaderMuteState": ["B", "Unmuted"]}),
    ({"cmd": "cough", "muted": True}, {"SetCoughMuteState": "MutedToAll"}),
    ({"cmd": "route", "input": "Music", "output": "LineOut", "on": False}, {"SetRouter": ["Music", "LineOut", False]}),
    ({"cmd": "profile", "name": "Sleep"}, {"LoadProfile": ["Sleep", True]}),
    ({"cmd": "mic_profile", "name": "DEFAULT"}, {"LoadMicProfile": ["DEFAULT", True]}),
    ({"cmd": "preset", "preset": "Preset4"}, {"SetActiveEffectPreset": "Preset4"}),
    ({"cmd": "switch", "what": "fx", "on": True}, {"SetFXEnabled": True}),
    ({"cmd": "switch", "what": "hardtune", "on": False}, {"SetHardTuneEnabled": False}),
    ({"cmd": "switch", "what": "submix", "on": True}, {"SetSubMixEnabled": True}),
    ({"cmd": "switch", "what": "gate", "on": False}, {"SetGateActive": False}),
    ({"cmd": "amount", "what": "reverb", "value": 40}, {"SetReverbAmount": 40}),
    ({"cmd": "amount", "what": "gender", "value": -7}, {"SetGenderAmount": -7}),
    ({"cmd": "sub_volume", "channel": "Game", "value": 100}, {"SetSubMixVolume": ["Game", 255]}),
    ({"cmd": "sub_link", "channel": "Game", "linked": False}, {"SetSubMixLinked": ["Game", False]}),
    ({"cmd": "out_mix", "output": "Headphones", "mix": "B"}, {"SetSubMixOutputMix": ["Headphones", "B"]}),
    ({"cmd": "monitor", "output": "BroadcastMix"}, {"SetMonitorMix": "BroadcastMix"}),
    ({"cmd": "mic_type", "type": "Dynamic"}, {"SetMicrophoneType": "Dynamic"}),
    ({"cmd": "mic_gain", "value": 41}, {"SetMicrophoneGain": ["Jack", 41]}),
    ({"cmd": "gate", "what": "threshold", "value": -40}, {"SetGateThreshold": -40}),
    ({"cmd": "gate", "what": "release", "value": 45}, {"SetGateRelease": 45}),
    ({"cmd": "comp", "what": "ratio", "value": 14}, {"SetCompressorRatio": 14}),
    ({"cmd": "comp", "what": "makeup", "value": 6}, {"SetCompressorMakeupGain": 6}),
    ({"cmd": "eq", "band": 9, "value": -3}, {"SetEqGain": ["Equalizer16KHz", -3]}),
    ({"cmd": "deess", "value": 30}, {"SetDeeser": 30}),
    ({"cmd": "bleep", "value": -20}, {"SetSwearButtonVolume": -20}),
    ({"cmd": "bank", "bank": "C"}, {"SetActiveSamplerBank": "C"}),
    ({"cmd": "pad", "pad": "TopLeft", "play": True}, {"PlayNextSample": ["A", "TopLeft"]}),
    ({"cmd": "pad", "pad": "TopLeft", "play": False}, {"StopSamplePlayback": ["A", "TopLeft"]}),
])
def test_each_action_maps_to_its_daemon_command(body, expected):
    st = status()
    _, m = mixer_of(st)
    assert audio.command(audio.parse(body), m, st) == expected


@pytest.mark.parametrize("body, expected", [
    ({"cmd": "save", "which": "profile"}, {"SaveProfile": []}),
    ({"cmd": "save", "which": "mic_profile"}, {"SaveMicProfile": []}),
    ({"cmd": "save_as", "which": "profile", "name": " Evening "}, {"SaveProfileAs": "Evening"}),
    ({"cmd": "save_as", "which": "mic_profile", "name": "Podcast"}, {"SaveMicProfileAs": "Podcast"}),
])
def test_save_actions(body, expected):
    st = status()
    _, m = mixer_of(st)
    assert audio.command(audio.parse(body), m, st) == expected


@pytest.mark.parametrize("name", ["sleep", "../evil", "a/b", "a\\b", ".hidden", "What?", "tab\there"])
def test_save_as_refuses_existing_and_unsafe_names(name):
    st = status()
    _, m = mixer_of(st)
    with pytest.raises(audio.Refused):
        audio.command(audio.parse({"cmd": "save_as", "which": "profile", "name": name}), m, st)


def test_unsaved_follows_changes_saves_and_loads():
    st = status()
    _, m = mixer_of(st)
    u = audio.Unsaved()
    assert u.observe(m) == {"profile": False, "mic_profile": False}          # first sight counts as saved
    m["levels"]["volumes"]["Music"] = 1                                       # a fader moved (here or on the hardware)
    assert u.observe(m) == {"profile": True, "mic_profile": False}
    m["mic_status"]["noise_gate"]["threshold"] = -20
    m["levels"]["bleep"] = -5                                                 # bleep lives in the mic profile
    assert u.observe(m) == {"profile": True, "mic_profile": True}
    u.saved("profile", m)
    assert u.observe(m) == {"profile": False, "mic_profile": True}
    m["sampler"]["banks"]["A"]["TopLeft"]["is_playing"] = True                # live-only: never a change
    assert u.observe(m)["profile"] is False
    m["mic_profile_name"] = "DEFAULT"                                         # another mic profile loaded: its own baseline
    assert u.observe(m) == {"profile": False, "mic_profile": False}


def test_mute_to_x_when_the_fader_mutes_to_one_place():
    st = status()
    _, m = mixer_of(st)
    m["fader_status"]["C"]["mute_type"] = "ToStream"
    m["cough_button"]["mute_type"] = "ToVoiceChat"
    assert audio.command(audio.parse({"cmd": "fader_mute", "fader": "C", "muted": True}), m, st) == {"SetFaderMuteState": ["C", "MutedToX"]}
    assert audio.command(audio.parse({"cmd": "cough", "muted": True}), m, st) == {"SetCoughMuteState": "MutedToX"}


@pytest.mark.parametrize("body", [
    {"cmd": "route", "input": "Chat", "output": "ChatMic", "on": True},
    {"cmd": "profile", "name": "Not a profile"},
    {"cmd": "gate", "what": "threshold", "value": -60},
    {"cmd": "gate", "what": "attack", "value": 46},
    {"cmd": "comp", "what": "threshold", "value": 1},
    {"cmd": "amount", "what": "pitch", "value": 13},
    {"cmd": "amount", "what": "gender", "value": 13},
])
def test_refused_actions(body):
    st = status()
    _, m = mixer_of(st)
    with pytest.raises(audio.Refused):
        audio.command(audio.parse(body), m, st)


@pytest.mark.parametrize("body", [
    {"cmd": "SetVolume", "channel": "Music", "value": 1},                 # daemon commands can't be sent directly
    {"cmd": "volume", "channel": "Music", "value": 101},
    {"cmd": "volume", "channel": "Everything", "value": 1},
    {"cmd": "eq", "band": 10, "value": 0},
    {"cmd": "bleep", "value": 5},
    {"channel": "Music", "value": 5},
])
def test_malformed_actions_never_parse(body):
    with pytest.raises(ValueError):
        audio.parse(body)


def test_submix_refused_without_firmware_support():
    st = status()
    _, m = mixer_of(st)
    m["levels"]["submix_supported"] = False
    with pytest.raises(audio.Refused):
        audio.command(audio.parse({"cmd": "switch", "what": "submix", "on": True}), m, st)


# ---- the API --------------------------------------------------------------------------------------------------------

@pytest.fixture()
def fake_gx():
    d = FakeHttp()
    gx = goxlr.GoXLR("http://goxlr.test", transport=httpx.MockTransport(d.handler))
    live = app.state.goxlr                       # the app's own (unconfigured under tests), put back afterwards
    app.state.goxlr = gx
    try:
        yield d, gx
    finally:
        app.state.goxlr = live


def test_api_unconfigured(owner_client):
    r = owner_client.get("/api/owner/audio")
    assert r.status_code == 200 and r.json() == {"available": False, "configured": False}
    assert owner_client.post("/api/owner/audio", json={"cmd": "cough", "muted": True}).status_code == 503


def test_api_reads_and_sends(owner_client, fake_gx):
    d, _ = fake_gx
    r = owner_client.get("/api/owner/audio")
    assert r.status_code == 200 and r.json()["available"] and r.json()["live"] is False
    r = owner_client.post("/api/owner/audio", json={"cmd": "volume", "channel": "Game", "value": 20})
    assert r.status_code == 200 and d.commands[-1] == ("S1TEST", {"SetVolume": ["Game", 51]})
    assert owner_client.post("/api/owner/audio", json={"cmd": "route", "input": "Chat", "output": "ChatMic", "on": True}).status_code == 422
    assert owner_client.post("/api/owner/audio", json={"cmd": "nope"}).status_code == 422
    assert owner_client.post("/api/owner/audio", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 422
    assert len(d.commands) == 1                                    # nothing refused reached the daemon
    d.answer = {"Error": "nope"}
    assert owner_client.post("/api/owner/audio", json={"cmd": "cough", "muted": True}).status_code == 503
    d.up = False
    assert owner_client.get("/api/owner/audio").json()["available"] is False
    assert owner_client.post("/api/owner/audio", json={"cmd": "cough", "muted": True}).status_code == 503
    assert owner_client.get("/api/owner/audio/level").status_code == 503


def test_api_save_clears_unsaved(owner_client, fake_gx):
    d, _ = fake_gx
    assert owner_client.get("/api/owner/audio").json()["unsaved"] == {"profile": False, "mic_profile": False}
    mixer_of(d.st)[1]["levels"]["volumes"]["Game"] = 3
    assert owner_client.get("/api/owner/audio").json()["unsaved"]["profile"] is True
    assert owner_client.post("/api/owner/audio", json={"cmd": "save", "which": "profile"}).status_code == 200
    assert d.commands[-1] == ("S1TEST", {"SaveProfile": []})
    assert owner_client.get("/api/owner/audio").json()["unsaved"]["profile"] is False
    r = owner_client.post("/api/owner/audio", json={"cmd": "save_as", "which": "profile", "name": "Default"})
    assert r.status_code == 422 and "already" in r.json()["detail"]


def test_api_is_owner_only(guest_client, owner_client, fake_gx):
    d, _ = fake_gx
    assert guest_client.get("/api/owner/audio").status_code in (401, 403)
    assert guest_client.post("/api/owner/audio", json={"cmd": "cough", "muted": True}).status_code in (401, 403)
    owner_client.headers.pop("X-Hub")
    try:
        assert owner_client.post("/api/owner/audio", json={"cmd": "cough", "muted": True}).status_code == 403
    finally:
        owner_client.headers["X-Hub"] = "1"
    assert d.commands == []


# ---- the websocket --------------------------------------------------------------------------------------------------

class FakeSocket:
    """The daemon's websocket: answers GetStatus / GetMicLevel, and lets a test push patches or hang up."""
    def __init__(self, daemon: "FakeSocketDaemon"):
        self.d = daemon
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, text: str) -> None:
        msg = json.loads(text)
        if msg["data"] == "GetStatus":
            self.d.status_reads += 1
            reply = {"Status": copy.deepcopy(self.d.st)}
        elif "GetMicLevel" in msg["data"]:
            reply = {"MicLevel": -23.5}
        else:
            reply = {"Error": "?"}
        await self.inbox.put(json.dumps({"id": msg["id"], "data": reply}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        pass


class FakeSocketDaemon:
    def __init__(self):
        self.st = status()
        self.status_reads = 0
        self.opens = 0
        self.sock: FakeSocket | None = None
        self.refuse = False

    async def connect(self, url: str) -> FakeSocket:
        assert url == "ws://goxlr.test/api/websocket"
        if self.refuse:
            raise OSError("refused")
        self.opens += 1
        self.sock = FakeSocket(self)
        return self.sock

    async def patch(self) -> None:
        await self.sock.inbox.put(json.dumps({"id": 0, "data": {"Patch": [{"op": "replace", "path": "/x", "value": 1}]}}))

    async def hang_up(self) -> None:
        await self.sock.inbox.put(None)


async def _until(cond, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def test_socket_keeps_status_coalesces_patches_and_reconnects(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(goxlr.notify, "audio", lambda: events.append("audio"))
    monkeypatch.setattr(goxlr.notify, "volume", lambda: events.append("volume"))
    monkeypatch.setattr(goxlr, "REFRESH_S", 0.05)
    monkeypatch.setattr(goxlr, "BACKOFF_MIN_S", 0.01)

    async def run():
        d = FakeSocketDaemon()
        gx = goxlr.GoXLR("http://goxlr.test", connect=d.connect)
        await gx.start()
        try:
            await _until(lambda: gx.live)
            assert d.status_reads == 1 and events == ["audio", "volume"]       # first status: everything is news
            serial, m, _ = await gx.mixer()
            assert serial == "S1TEST"
            assert await gx.mic_level(serial) == -23.5

            events.clear()
            mixer_of(d.st)[1]["levels"]["volumes"]["Music"] = 10           # a physical fader move…
            for _ in range(10):
                await d.patch()                                              # …arrives as a burst of patches
            await _until(lambda: d.status_reads >= 2)
            await asyncio.sleep(0.15)
            assert d.status_reads == 2 and events == ["audio"]             # one read for the burst; Line out/in didn't move
            assert mixer_of(await gx.status())[1]["levels"]["volumes"]["Music"] == 10

            events.clear()
            mixer_of(d.st)[1]["levels"]["volumes"]["LineOut"] = 99
            await d.patch()
            await _until(lambda: "volume" in events)                       # the header's buttons hear about it

            await d.hang_up()
            await _until(lambda: d.opens == 2 and gx.live)                 # reconnects by itself
        finally:
            await gx.stop()
        assert not gx.live

    asyncio.run(run())


def test_socket_down_falls_back_to_http(monkeypatch):
    monkeypatch.setattr(goxlr, "BACKOFF_MIN_S", 0.01)
    events: list[str] = []
    monkeypatch.setattr(goxlr.notify, "audio", lambda: events.append("audio"))
    monkeypatch.setattr(goxlr.notify, "volume", lambda: None)

    async def run():
        d = FakeSocketDaemon()
        d.refuse = True
        h = FakeHttp()
        gx = goxlr.GoXLR("http://goxlr.test", transport=httpx.MockTransport(h.handler), connect=d.connect)
        await gx.start()
        try:
            await asyncio.sleep(0.05)
            assert not gx.live
            serial, _, _ = await gx.mixer()                                  # one HTTP read instead
            await gx.command(serial, {"SetVolume": ["Game", 1]})
            assert h.commands == [("S1TEST", {"SetVolume": ["Game", 1]})] and "audio" in events   # read back and announced
            with pytest.raises(goxlr.Unavailable):
                await gx.mic_level(serial)
        finally:
            await gx.stop()

    asyncio.run(run())


def test_tests_never_reach_the_real_daemon():
    from app import config
    assert config.GOXLR_URL is None and app.state.goxlr.configured is False


# ---- lighting (session 4) ---------------------------------------------------------------------------------------------

THEME = {"cmd": "light_theme", "frame": "aa8eff", "soft": "C6B6FF", "alt": "9787FF", "warm": "E6B3FF", "attention": "FFBE5F", "alert": "FF4262"}


def mini_status() -> dict:
    st = status()
    mixer_of(st)[1]["hardware"]["device_type"] = "Mini"
    return st


def test_lighting_view_names_each_colour_by_what_it_is():
    st = status()
    serial, m = mixer_of(st)
    lt = audio.view(serial, m, st)["lighting"]
    assert lt["animation"] == {"mode": "None", "mod1": 0, "mod2": 0, "waterfall": "Down"}
    assert (lt["global"], lt["accent"]) == ("00FFFF", "0A2B1B")
    assert lt["faders"]["A"] == {"style": "Gradient", "top": "000000", "bottom": "25FF24", "screen": "62FF39", "text": "Mic", "number": "1"}
    assert lt["buttons"]["Cough"] == {"on": "FE0A13", "off": "0A2B1B", "off_style": "Colour2"}
    assert set(lt["buttons"]) >= {"Fader1Mute", "Bleep", "EffectSelect6", "EffectFx"}
    assert lt["knobs"]["Reverb"] == {"left": "0A2B1B", "right": "000000", "knob": "0A2B1B"}   # colour_two = left, one = right
    assert lt["banks"]["A"] == {"on": "0A2B1B", "off": "FFFFFF", "empty": "000000", "off_style": "Dimmed"}


def test_lighting_view_mini_and_no_animation_firmware():
    st = mini_status()
    serial, m = mixer_of(st)
    lt = audio.view(serial, m, st)["lighting"]
    assert lt["knobs"] == {} and lt["banks"] == {} and "EffectFx" not in lt["buttons"] and "Cough" in lt["buttons"]
    assert lt["animation"] is not None                       # the Mini animates too (just not Ripple)
    m["lighting"]["animation"]["supported"] = False
    assert audio.view(serial, m, st)["lighting"]["animation"] is None


@pytest.mark.parametrize("body, expected", [
    ({"cmd": "light", "part": "global", "colours": ["00ff00"]}, [{"SetGlobalColour": "00FF00"}]),
    ({"cmd": "light", "part": "accent", "colours": ["123456"]}, [{"SetSimpleColour": ["Accent", "123456"]}]),
    ({"cmd": "light", "part": "scribble:C", "colours": ["ABCDEF"]}, [{"SetSimpleColour": ["Scribble3", "ABCDEF"]}]),
    ({"cmd": "light", "part": "scribble:C", "colours": ["ABCDEF"], "all": True},
     [{"SetSimpleColour": [f"Scribble{i}", "ABCDEF"]} for i in range(1, 5)]),
    ({"cmd": "light", "part": "fader:B", "colours": ["111111", "222222"]}, [{"SetFaderColours": ["B", "111111", "222222"]}]),
    ({"cmd": "light", "part": "fader:B", "style": "Meter"}, [{"SetFaderDisplayStyle": ["B", "Meter"]}]),
    ({"cmd": "light", "part": "fader:B", "colours": ["111111", "222222"], "style": "TwoColour", "all": True},
     [{"SetAllFaderColours": ["111111", "222222"]}, {"SetAllFaderDisplayStyle": "TwoColour"}]),
    ({"cmd": "light", "part": "mute:D", "colours": ["FF0000", "220000"]}, [{"SetButtonColours": ["Fader4Mute", "FF0000", "220000"]}]),
    ({"cmd": "light", "part": "mute:D", "colours": ["FF0000", "220000"], "style": "Colour2", "all": True},
     [{"SetButtonGroupColours": ["FaderMute", "FF0000", "220000"]}, {"SetButtonGroupOffStyle": ["FaderMute", "Colour2"]}]),
    ({"cmd": "light", "part": "btn:Cough", "style": "DimmedColour2"}, [{"SetButtonOffStyle": ["Cough", "DimmedColour2"]}]),
    ({"cmd": "light", "part": "btn:EffectSelect2", "colours": ["FFFFFF", "000000"], "all": True},
     [{"SetButtonGroupColours": ["EffectSelector", "FFFFFF", "000000"]}]),
    ({"cmd": "light", "part": "btn:EffectRobot", "style": "Dimmed", "all": True}, [{"SetButtonGroupOffStyle": ["EffectTypes", "Dimmed"]}]),
    ({"cmd": "light", "part": "knob:Pitch", "colours": ["111111", "222222", "333333"]},          # left, right, knob → right, left, knob
     [{"SetEncoderColour": ["Pitch", "222222", "111111", "333333"]}]),
    ({"cmd": "light", "part": "bank:B", "colours": ["111111", "222222", "333333"], "style": "Colour2"},
     [{"SetSampleColour": ["SamplerSelectB", "111111", "222222", "333333"]}, {"SetSampleOffStyle": ["SamplerSelectB", "Colour2"]}]),
    ({"cmd": "light", "part": "bank:B", "style": "Dimmed", "all": True},
     [{"SetSampleOffStyle": [f"SamplerSelect{b}", "Dimmed"]} for b in "ABC"]),
    ({"cmd": "anim", "mode": "RainbowDark"}, [{"SetAnimationMode": "RainbowDark"}]),
    ({"cmd": "light_copy", "name": "Default - Red"}, [{"LoadProfileColours": "Default - Red"}]),
])
def test_lighting_actions_map_to_daemon_commands(body, expected):
    st = status()
    _, m = mixer_of(st)
    assert audio.commands(audio.parse(body), m, st) == expected


def test_animation_settings_follow_the_mode():
    st = status()
    _, m = mixer_of(st)
    run = lambda body: audio.commands(audio.parse({"cmd": "anim", **body}), m, st)   # noqa: E731
    an = m["lighting"]["animation"]
    for mode, mod1, mod2, water in [("None", False, False, False), ("Simple", True, False, True), ("Ripple", True, False, True),
                                    ("RainbowBright", True, True, True), ("RainbowDark", True, True, True), ("RetroRainbow", True, False, False)]:
        an["mode"] = mode
        for body, ok in [({"mod1": 40}, mod1), ({"mod2": 40}, mod2), ({"waterfall": "Up"}, water)]:
            if ok:
                assert len(run(body)) == 1
            else:
                with pytest.raises(audio.Refused):
                    run(body)
    with pytest.raises(audio.Refused):
        run({"mode": "Simple", "mod1": 3})                    # one at a time
    an["supported"] = False
    with pytest.raises(audio.Refused):
        run({"mode": "Simple"})


def test_match_my_theme_is_one_short_batch():
    st = status()
    _, m = mixer_of(st)
    cmds = audio.commands(audio.parse(THEME), m, st)
    assert len(cmds) == 28 and cmds[0] == {"SetGlobalColour": "AA8EFF"}
    assert {"SetAllFaderDisplayStyle": "Gradient"} in cmds                                 # faders always as a gradient
    assert {"SetButtonGroupColours": ["FaderMute", "FF4262", "AA8EFF"]} in cmds
    assert {"SetEncoderColour": ["Echo", "221C33", "C6B6FF", "AA8EFF"]} in cmds          # right = the frame colour at 20 %
    mini = mini_status()
    _, mm = mixer_of(mini)
    names = [next(iter(c)) for c in audio.commands(audio.parse(THEME), mm, mini)]
    assert len(names) == 14 and "SetEncoderColour" not in names and "SetSampleColour" not in names
    with pytest.raises(audio.Refused):                       # several commands: never through the one-command path
        audio.command(audio.parse(THEME), m, st)


@pytest.mark.parametrize("body, mini", [
    ({"cmd": "light", "part": "fader:A", "colours": ["111111"]}, False),                       # a fader has two colours
    ({"cmd": "light", "part": "fader:A"}, False),                                              # nothing to change
    ({"cmd": "light", "part": "scribble:A", "style": "Meter"}, False),                        # screens have no style
    ({"cmd": "light", "part": "fader:A", "style": "Dimmed"}, False),                          # a button style on a fader
    ({"cmd": "light", "part": "mute:A", "style": "Gradient"}, False),
    ({"cmd": "light", "part": "btn:Cough", "colours": ["111111", "222222"], "all": True}, False),  # no other Cough
    ({"cmd": "light", "part": "global", "colours": ["111111"], "all": True}, False),
    ({"cmd": "light", "part": "knob:Echo", "colours": ["111111", "222222", "333333"]}, True),  # the Mini has none of these
    ({"cmd": "light", "part": "bank:A", "style": "Dimmed"}, True),
    ({"cmd": "light", "part": "btn:EffectFx", "style": "Dimmed"}, True),
    ({"cmd": "anim", "mode": "Ripple"}, True),
    ({"cmd": "light_copy", "name": "Not a profile"}, False),
])
def test_refused_lighting_actions(body, mini):
    st = mini_status() if mini else status()
    _, m = mixer_of(st)
    with pytest.raises(audio.Refused):
        audio.commands(audio.parse(body), m, st)


@pytest.mark.parametrize("body", [
    {"cmd": "light", "part": "global", "colours": ["#00FF00"]},                # six hex digits, no #
    {"cmd": "light", "part": "global", "colours": ["00FF0"]},
    {"cmd": "light", "part": "global", "colours": ["GGGGGG"]},
    {"cmd": "light", "part": "global", "colours": ["00FF00; rm"]},
    {"cmd": "light", "part": "fader:E", "colours": ["00FF00", "00FF00"]},
    {"cmd": "light", "part": "btn:SamplerClear", "colours": ["00FF00", "00FF00"]},
    {"cmd": "light", "part": "knob:Reverb", "colours": ["000000"] * 4},
    {"cmd": "light", "part": "fader:A", "style": "Sparkly"},
    {"cmd": "anim", "mod1": 101},
    {"cmd": "anim", "waterfall": "Sideways"},
    {**THEME, "alert": "red"},
])
def test_malformed_lighting_never_parses(body):
    with pytest.raises(ValueError):
        audio.parse(body)


def test_api_sends_a_batch_in_order(owner_client, fake_gx):
    d, _ = fake_gx
    assert owner_client.post("/api/owner/audio", json=THEME).status_code == 200
    assert len(d.commands) == 28 and d.commands[0] == ("S1TEST", {"SetGlobalColour": "AA8EFF"})
    assert owner_client.post("/api/owner/audio", json={"cmd": "light", "part": "fader:A", "colours": ["#000000", "000000"]}).status_code == 422
    assert owner_client.post("/api/owner/audio", json={"cmd": "anim", "mod2": 5}).status_code == 422   # mode is None
    assert len(d.commands) == 28


def test_fake_daemon_applies_what_the_page_sends():
    """The scratch check's fake daemon (tools/fake_goxlr.py) applies every lighting command the hub sends, so the page
    can be checked end to end; the view reads back what was set."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fake_goxlr", Path(__file__).parent.parent / "tools" / "fake_goxlr.py")
    fake = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake)
    fake.STATE.clear(); fake.STATE.update(status())
    serial, m = mixer_of(fake.STATE)
    for body in [THEME, {"cmd": "light", "part": "knob:Pitch", "colours": ["111111", "222222", "333333"]},
                 {"cmd": "light", "part": "fader:C", "style": "Meter"}, {"cmd": "anim", "mode": "RainbowBright"}, {"cmd": "anim", "mod2": 70}]:
        for cmd in audio.commands(audio.parse(body), m, fake.STATE):
            fake.apply(cmd)
    lt = audio.view(serial, m, fake.STATE)["lighting"]
    assert lt["global"] == "AA8EFF" and lt["faders"]["B"]["screen"] == "C6B6FF" and lt["faders"]["C"]["style"] == "Meter"
    assert lt["buttons"]["Fader3Mute"] == {"on": "FF4262", "off": "AA8EFF", "off_style": "DimmedColour2"}
    assert lt["knobs"]["Pitch"] == {"left": "111111", "right": "222222", "knob": "333333"}
    assert lt["animation"]["mode"] == "RainbowBright" and lt["animation"]["mod2"] == 70
