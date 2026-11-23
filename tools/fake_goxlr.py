"""A fake goxlr-utility daemon for trying the Audio page on a scratch copy, never the real GoXLR.

    .venv/bin/python tools/fake_goxlr.py [--port 14599] [--fixture tests/fixtures/goxlr_status.json]
    HUB_DATA_DIR=<scratch> HUB_PORT=18081 HUB_NO_FETCH=1 HUB_GOXLR_URL=http://127.0.0.1:14599 .venv/bin/python -m app

It speaks the same API as goxlr-utility 1.2.4 (HomeLab/audio/README.md): `GET /api/get-devices`, `POST /api/command`
(applied to its copy of the status, then a Patch pushed to every websocket) and `/api/websocket` (`GetStatus`,
`GetMicLevel`). Extra endpoints for checks:

    GET  /fake/log                         every command received, oldest first
    POST /fake/volume?channel=Music&value=200   a "physical fader" move (0–255), pushed like the real thing
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI()
STATE: dict[str, Any] = {}
LOG: list[Any] = []
SOCKETS: set[WebSocket] = set()


def mixer() -> dict[str, Any]:
    return next(iter(STATE["mixers"].values()))


def apply(cmd: dict[str, Any]) -> None:
    """Change the fake status the way the daemon would, for the commands the Audio page sends."""
    m = mixer()
    name, arg = next(iter(cmd.items()))
    lv, mic, fx = m["levels"], m["mic_status"], m["effects"]
    cur = fx["current"]
    match name:
        case "SetVolume": lv["volumes"][arg[0]] = arg[1]
        case "SetFaderMuteState": m["fader_status"][arg[0]]["mute_state"] = arg[1]
        case "SetCoughMuteState": m["cough_button"]["state"] = arg
        case "SetRouter": m["router"][arg[0]][arg[1]] = arg[2]
        case "LoadProfile": m["profile_name"] = arg[0]
        case "LoadMicProfile": m["mic_profile_name"] = arg[0]
        case "SetActiveEffectPreset": fx["active_preset"] = arg
        case "SetFXEnabled": fx["is_enabled"] = arg
        case "SetMegaphoneEnabled": cur["megaphone"]["is_enabled"] = arg
        case "SetRobotEnabled": cur["robot"]["is_enabled"] = arg
        case "SetHardTuneEnabled": cur["hard_tune"]["is_enabled"] = arg
        case "SetReverbAmount": cur["reverb"]["amount"] = arg
        case "SetEchoAmount": cur["echo"]["amount"] = arg
        case "SetPitchAmount": cur["pitch"]["amount"] = arg
        case "SetGenderAmount": cur["gender"]["amount"] = arg
        case "SetSubMixEnabled":
            lv["submix"] = None if not arg else {
                "inputs": {c: {"volume": lv["volumes"].get(c, 0), "linked": True, "ratio": 1.0}
                           for c in ("Mic", "LineIn", "Console", "System", "Game", "Chat", "Sample", "Music")},
                "outputs": {o: "A" for o in ("Headphones", "BroadcastMix", "ChatMic", "Sampler", "LineOut", "StreamMix2")}}
        case "SetSubMixVolume": lv["submix"]["inputs"][arg[0]]["volume"] = arg[1]
        case "SetSubMixLinked": lv["submix"]["inputs"][arg[0]]["linked"] = arg[1]
        case "SetSubMixOutputMix": lv["submix"]["outputs"][arg[0]] = arg[1]
        case "SetMonitorMix": lv["output_monitor"] = arg
        case "SetMicrophoneType": mic["mic_type"] = arg
        case "SetMicrophoneGain": mic["mic_gains"][arg[0]] = arg[1]
        case "SetGateActive": mic["noise_gate"]["enabled"] = arg
        case "SetGateThreshold": mic["noise_gate"]["threshold"] = arg
        case "SetGateAttenuation": mic["noise_gate"]["attenuation"] = arg
        case "SetGateAttack": mic["noise_gate"]["attack"] = arg
        case "SetGateRelease": mic["noise_gate"]["release"] = arg
        case "SetCompressorThreshold": mic["compressor"]["threshold"] = arg
        case "SetCompressorRatio": mic["compressor"]["ratio"] = arg
        case "SetCompressorAttack": mic["compressor"]["attack"] = arg
        case "SetCompressorReleaseTime": mic["compressor"]["release"] = arg
        case "SetCompressorMakeupGain": mic["compressor"]["makeup_gain"] = arg
        case "SetEqGain": mic["equaliser"]["gain"][arg[0]] = arg[1]
        case "SetDeeser": lv["deess"] = arg
        case "SetSwearButtonVolume": lv["bleep"] = arg
        case "SetActiveSamplerBank": m["sampler"]["active_bank"] = arg
        case "PlayNextSample": m["sampler"]["banks"][arg[0]][arg[1]]["is_playing"] = True
        case "StopSamplePlayback": m["sampler"]["banks"][arg[0]][arg[1]]["is_playing"] = False
        case "SaveProfile" | "SaveMicProfile": pass
        case "SaveProfileAs":
            STATE["files"]["profiles"].append(arg); m["profile_name"] = arg
        case "SaveMicProfileAs":
            STATE["files"]["mic_profiles"].append(arg); m["mic_profile_name"] = arg
        case _ if name in LIGHT: light(m["lighting"], name, arg)
        case _: raise ValueError(f"fake daemon doesn't know {name}")


GROUPS = {"FaderMute": [f"Fader{i}Mute" for i in range(1, 5)], "EffectSelector": [f"EffectSelect{i}" for i in range(1, 7)],
          "EffectTypes": ["EffectFx", "EffectMegaphone", "EffectRobot", "EffectHardTune"]}
LIGHT = {"SetAnimationMode", "SetAnimationMod1", "SetAnimationMod2", "SetAnimationWaterfall", "SetGlobalColour", "SetSimpleColour",
         "SetFaderColours", "SetAllFaderColours", "SetFaderDisplayStyle", "SetAllFaderDisplayStyle", "SetButtonColours",
         "SetButtonOffStyle", "SetButtonGroupColours", "SetButtonGroupOffStyle", "SetEncoderColour", "SetSampleColour",
         "SetSampleOffStyle", "LoadProfileColours"}


def light(lt: dict[str, Any], name: str, arg: Any) -> None:
    """The Lighting tab's commands, applied like the daemon does (refusals as in goxlr-utility 1.2.4's animation.rs)."""
    an, faders, btns = lt["animation"], lt["faders"], lt["buttons"]
    def colours(target: dict, *cs: str) -> None:
        target.update({k: c for k, c in zip(("colour_one", "colour_two", "colour_three"), cs) if c is not None})
    match name:
        case "SetAnimationMode": an["mode"] = arg
        case "SetAnimationMod1":
            if an["mode"] == "None": raise ValueError("Mod1 Not Available in this Mode")
            an["mod1"] = arg
        case "SetAnimationMod2":
            if an["mode"] not in ("RainbowBright", "RainbowDark"): raise ValueError("Mod2 Not Available in this Mode")
            an["mod2"] = arg
        case "SetAnimationWaterfall":
            if an["mode"] in ("None", "RetroRainbow"): raise ValueError("Waterfall not Availabe in this Mode")
            an["waterfall_direction"] = arg
        case "SetGlobalColour": lt["simple"]["Global"]["colour_one"] = arg
        case "SetSimpleColour": lt["simple"][arg[0]]["colour_one"] = arg[1]
        case "SetFaderColours": colours(faders[arg[0]]["colours"], arg[1], arg[2])
        case "SetAllFaderColours":
            for f in faders.values(): colours(f["colours"], *arg)
        case "SetFaderDisplayStyle": faders[arg[0]]["style"] = arg[1]
        case "SetAllFaderDisplayStyle":
            for f in faders.values(): f["style"] = arg
        case "SetButtonColours": colours(btns[arg[0]]["colours"], arg[1], arg[2])
        case "SetButtonOffStyle": btns[arg[0]]["off_style"] = arg[1]
        case "SetButtonGroupColours":
            for b in GROUPS[arg[0]]: colours(btns[b]["colours"], arg[1], arg[2])
        case "SetButtonGroupOffStyle":
            for b in GROUPS[arg[0]]: btns[b]["off_style"] = arg[1]
        case "SetEncoderColour": colours(lt["encoders"][arg[0]], *arg[1:])
        case "SetSampleColour": colours(lt["sampler"][arg[0]]["colours"], *arg[1:])
        case "SetSampleOffStyle": lt["sampler"][arg[0]]["off_style"] = arg[1]
        case "LoadProfileColours":            # a stand-in: every colour becomes one derived from the profile's name
            if arg not in STATE["files"]["profiles"]: raise ValueError("no such profile")
            c = f"{hash(arg) & 0xFFFFFF:06X}"
            for f in faders.values(): colours(f["colours"], "000000", c)
            for k in lt["simple"]: lt["simple"][k]["colour_one"] = c


async def push() -> None:
    """Tell every socket something changed (the real daemon sends the JSON patch; the hub only needs to know)."""
    msg = json.dumps({"id": 0, "data": {"Patch": [{"op": "replace", "path": "/mixers", "value": None}]}})
    for ws in list(SOCKETS):
        try:
            await ws.send_text(msg)
        except Exception:                      # noqa: BLE001 - gone
            SOCKETS.discard(ws)


@app.get("/")
async def index():
    return {"fake": "goxlr-utility"}


@app.get("/api/get-devices")
async def get_devices():
    return STATE


@app.post("/api/command")
async def command(request: Request):
    body = await request.json()
    serial, cmd = body["Command"]
    LOG.append(cmd)
    if serial not in STATE["mixers"]:
        return {"Error": "no such device"}
    try:
        apply(cmd)
    except (ValueError, KeyError, TypeError) as e:
        return {"Error": str(e)}
    await push()
    return "Ok"


@app.get("/fake/log")
async def fake_log():
    return LOG


@app.post("/fake/volume")
async def fake_volume(channel: str, value: int):
    mixer()["levels"]["volumes"][channel] = value
    await push()
    return "Ok"


@app.websocket("/api/websocket")
async def socket(ws: WebSocket):
    await ws.accept()
    SOCKETS.add(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            data = msg.get("data")
            if data == "GetStatus":
                reply = {"Status": STATE}
            elif isinstance(data, dict) and "GetMicLevel" in data:
                t = time.time()                    # a voice-ish wobble between -60 and -8 dBFS
                reply = {"MicLevel": -60 + 52 * max(0.0, math.sin(t * 3.1) * math.sin(t * 0.7 + 1))}
            else:
                reply = {"Error": "not faked"}
            await ws.send_text(json.dumps({"id": msg.get("id"), "data": reply}))
    except WebSocketDisconnect:
        pass
    finally:
        SOCKETS.discard(ws)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=14599)
    ap.add_argument("--fixture", default=str(ROOT / "tests" / "fixtures" / "goxlr_status.json"))
    args = ap.parse_args()
    STATE.update(copy.deepcopy(json.loads(Path(args.fixture).read_text())))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
