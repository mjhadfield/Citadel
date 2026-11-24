"""Gaming's wallpaper hands its new-window links to Mike's own Brave (POST /api/owner/browser/open). Only a page on
this PC itself may: anything else would open a tab on Gaming's screen. `procs._run` is monkeypatched, so no browser
ever opens."""
import asyncio

import pytest

from app import procs

BASE = "/api/owner"


@pytest.mark.parametrize("address, here", [
    ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("192.0.2.10", False),            # TEST-NET-1: never one of this PC's addresses
    ("testclient", False), ("", False), (None, False),
])
def test_is_this_pc(address, here):
    assert procs.is_this_pc(address) is here


def test_open_in_browser_runs_brave_in_its_own_unit(monkeypatch):
    calls = []

    async def fake_run(*args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(procs, "_run", fake_run)
    asyncio.run(procs.open_in_browser("https://example.com/a?b=1"))
    args = calls[0]
    assert args[0] == "systemd-run" and any(a.startswith("--unit=hub-open-") for a in args)
    assert list(args[args.index("--") + 1:]) == ["brave", "https://example.com/a?b=1"]


def test_only_from_this_pc(owner_client, monkeypatch):
    opened = []

    async def fake_open(url):
        opened.append(url)

    monkeypatch.setattr(procs, "open_in_browser", fake_open)
    # TestClient's address is "testclient": another machine
    assert owner_client.get(f"{BASE}/browser").json() == {"here": False}
    assert owner_client.post(f"{BASE}/browser/open", json={"url": "https://example.com/"}).status_code == 409
    assert opened == []

    monkeypatch.setattr(procs, "is_this_pc", lambda address: True)
    assert owner_client.get(f"{BASE}/browser").json() == {"here": True}
    r = owner_client.post(f"{BASE}/browser/open", json={"url": "https://example.com/"})
    assert r.status_code == 200 and opened == ["https://example.com/"]
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "--remote-debugging-port=1", "example.com"):
        assert owner_client.post(f"{BASE}/browser/open", json={"url": bad}).status_code == 422
    assert opened == ["https://example.com/"]


def test_launch_failure_is_reported(owner_client, monkeypatch):
    async def failing(url):
        raise procs.ProcError("brave: not found")

    monkeypatch.setattr(procs, "is_this_pc", lambda address: True)
    monkeypatch.setattr(procs, "open_in_browser", failing)
    r = owner_client.post(f"{BASE}/browser/open", json={"url": "https://example.com/"})
    assert r.status_code == 502 and "brave: not found" in r.json()["detail"]


def test_owner_only(guest_client):
    assert guest_client.get(f"{BASE}/browser").status_code == 403
    assert guest_client.post(f"{BASE}/browser/open", json={"url": "https://example.com/"}, headers={"X-Hub": "1"}).status_code == 403
