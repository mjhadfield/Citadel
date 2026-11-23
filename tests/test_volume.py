"""The header's GoXLR volume buttons (app/volume.py), against a fake goxlr-utility daemon: never the real one."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import db, volume
from app.main import app


class FakeDaemon:
    def __init__(self, line_out: int = 204, line_in: int = 128, up: bool = True):
        self.vol = {"LineOut": line_out, "LineIn": line_in, "Music": 139}
        self.up = up
        self.commands: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("daemon down")
        if request.url.path == "/api/get-devices":
            return httpx.Response(200, json={"mixers": {"S1": {"levels": {"volumes": dict(self.vol)}}}})
        if request.url.path == "/api/command":
            body = json.loads(request.content)
            serial, cmd = body["Command"]
            ch, raw = cmd["SetVolume"]
            self.commands.append((serial, ch, raw))
            self.vol[ch] = raw
            return httpx.Response(200, json="Ok")
        return httpx.Response(404)


@pytest.fixture()
def fake(client):
    db.put_settings(volume.KEY, {"boot": "", "restore": {}})
    d = FakeDaemon()
    vol = volume.Volume("http://goxlr.test", transport=httpx.MockTransport(d.handler))
    yield d, vol


def test_conversions():
    assert volume.to_raw(100) == 255 and volume.to_raw(0) == 0 and volume.to_raw(80) == 204
    assert volume.to_pct(204) == 80 and volume.to_pct(255) == 100


def test_view_reads_both_channels(fake):
    d, vol = fake
    v = asyncio.run(vol.view())
    assert v["available"] and v["configured"]
    ch = {c["id"]: c for c in v["channels"]}
    assert ch["lineout"]["volume"] == 80 and ch["lineout"]["curve"] == 2.2 and not ch["lineout"]["muted"]
    assert ch["vinyl"]["volume"] == 50 and ch["vinyl"]["curve"] == 1.0


def test_unconfigured_and_down(client):
    v = asyncio.run(volume.Volume(None).view())
    assert not v["configured"] and not v["available"]
    d = FakeDaemon(up=False)
    v = asyncio.run(volume.Volume("http://goxlr.test", transport=httpx.MockTransport(d.handler)).view())
    assert v["configured"] and not v["available"] and v["channels"][0]["volume"] is None


def test_set_remembers_level_and_mute_round_trips(fake):
    d, vol = fake
    asyncio.run(vol.set("vinyl", 30))
    assert d.vol["LineIn"] == volume.to_raw(30)
    v = asyncio.run(vol.mute("vinyl", True))
    assert d.vol["LineIn"] == 0 and next(c for c in v["channels"] if c["id"] == "vinyl")["muted"]
    asyncio.run(vol.mute("vinyl", False))
    assert d.vol["LineIn"] == volume.to_raw(30)
    assert d.vol["LineOut"] == 204 and d.vol["Music"] == 139      # nothing else touched


def test_boot_mutes_once_per_boot(fake):
    d, vol = fake
    assert asyncio.run(vol.mute_for_boot("boot-a")) is True
    assert d.vol["LineOut"] == 0 and d.vol["LineIn"] == 0
    assert db.get_settings(volume.KEY)["restore"] == {"lineout": 80, "vinyl": 50}
    asyncio.run(vol.set("lineout", 60))
    assert asyncio.run(vol.mute_for_boot("boot-a")) is False      # a hub restart in the same boot leaves it alone
    assert d.vol["LineOut"] == volume.to_raw(60)
    asyncio.run(vol.mute("lineout", True))
    asyncio.run(vol.mute("lineout", False))
    assert d.vol["LineOut"] == volume.to_raw(60)                   # unmute brings back the last level set
    assert asyncio.run(vol.mute_for_boot("boot-b")) is True       # the next boot mutes again
    assert d.vol["LineOut"] == 0


def test_boot_waits_for_the_daemon(fake, monkeypatch):
    d, vol = fake
    d.up = False
    monkeypatch.setattr(volume, "BOOT_POLL_S", 0)
    calls = {"n": 0}
    real = vol._status

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 3:
            d.up = True
        return await real()
    monkeypatch.setattr(vol, "_status", flaky)
    assert asyncio.run(vol.mute_for_boot("boot-c")) is True
    assert d.vol["LineIn"] == 0 and calls["n"] == 3


def test_api(owner_client, guest_client, fake):
    d, vol = fake
    live = app.state.volume                    # the app's own (unconfigured under tests), put back afterwards
    app.state.volume = vol
    try:
        _api_checks(owner_client, guest_client, d)
    finally:
        app.state.volume = live


def _api_checks(owner_client, guest_client, d):
    r = owner_client.get("/api/owner/volume")
    assert r.status_code == 200 and r.json()["available"]
    r = owner_client.put("/api/owner/volume/lineout", json={"volume": 40})
    assert r.status_code == 200 and d.vol["LineOut"] == volume.to_raw(40)
    assert owner_client.put("/api/owner/volume/lineout", json={"volume": 101}).status_code == 422
    assert owner_client.put("/api/owner/volume/music", json={"volume": 10}).status_code == 404
    assert owner_client.put("/api/owner/volume/vinyl/mute", json={"muted": True}).status_code == 200 and d.vol["LineIn"] == 0
    assert guest_client.put("/api/owner/volume/vinyl/mute", json={"muted": False}).status_code in (401, 403)
    d.up = False
    assert owner_client.put("/api/owner/volume/vinyl", json={"volume": 10}).status_code == 503


def test_tests_never_reach_the_real_daemon():
    from app import config
    assert config.GOXLR_URL is None


def test_a_read_straight_after_mute_sees_it_while_the_socket_is_up(fake):
    """The header's Mute flickered back and forth: with the daemon's socket up, the SSE nudge after a mute re-read the
    socket's cached status before the daemon's own patch had landed. A command now reads the result back at once."""
    d, vol = fake
    gx = vol.gx
    asyncio.run(vol.view())
    gx._last, gx.live = asyncio.run(gx._http_status()), True     # as if the socket were up, with no patch to come
    asyncio.run(vol.mute("vinyl", True))
    v = asyncio.run(vol.view())                                    # what the page's re-read gets
    assert next(c for c in v["channels"] if c["id"] == "vinyl")["muted"]
    gx.live = False
