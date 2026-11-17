"""Remote Access: the devices Home's tile opens a VNC viewer to, their Settings CRUD, and the viewer launch (procs is
monkeypatched, so no window ever opens)."""
import pytest

from app import db, procs

BASE = "/api/owner"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with db.db() as con:
        con.execute("DELETE FROM remote_hosts")


def mk(c, name="Pi 1 desktop", address="pi-1.local"):
    r = c.post(f"{BASE}/remote", json={"name": name, "address": address})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def cfg(c):
    return c.get(f"{BASE}/config").json()["remote"]


def test_crud_and_order(owner_client):
    a, b = mk(owner_client, "Lappy desktop", "100.64.0.9"), mk(owner_client)
    assert [r["name"] for r in cfg(owner_client)] == ["Lappy desktop", "Pi 1 desktop"]
    assert owner_client.put(f"{BASE}/remote/{a}", json={"name": "Lappy", "address": "lappy::5901"}).status_code == 200
    assert owner_client.post(f"{BASE}/remote/reorder", json={"ids": [b, a]}).status_code == 200
    assert [(r["name"], r["address"]) for r in cfg(owner_client)] == [("Pi 1 desktop", "pi-1.local"), ("Lappy", "lappy::5901")]
    assert owner_client.delete(f"{BASE}/remote/{b}").status_code == 200
    assert [r["id"] for r in cfg(owner_client)] == [a]
    assert owner_client.delete(f"{BASE}/remote/{b}").status_code == 404


def test_viewer_defaults_to_krdc_and_can_be_tigervnc(owner_client):
    a = mk(owner_client, "NZXT", "192.168.0.70")
    assert cfg(owner_client)[0]["viewer"] == "krdc"
    assert owner_client.put(f"{BASE}/remote/{a}", json={"name": "Pi 2", "address": "pi-2.local", "viewer": "tigervnc"}).status_code == 200
    assert cfg(owner_client)[0]["viewer"] == "tigervnc"
    assert owner_client.put(f"{BASE}/remote/{a}", json={"name": "x", "address": "pi-2.local", "viewer": "rustdesk"}).status_code == 422


@pytest.mark.parametrize("address,viewer,argv", [
    ("192.168.0.70", "krdc", ["krdc", "vnc://192.168.0.70"]),          # port 5900 left off: krdcrc keys settings by this URL
    ("pi-2.local", "tigervnc", ["vncviewer", "pi-2.local"]),
    ("server:1", "krdc", ["krdc", "vnc://server:5901"]),
    ("server::6000", "krdc", ["krdc", "vnc://server:6000"]),
    ("[fd00::1]", "krdc", ["krdc", "vnc://[fd00::1]"]),
    ("server::6000", "tigervnc", ["vncviewer", "server::6000"]),
])
def test_viewer_argv(address, viewer, argv):
    assert procs.viewer_argv({"address": address, "viewer": viewer}) == argv


def test_open_viewer_runs_the_chosen_viewer(monkeypatch):
    import asyncio
    calls = []

    async def fake_run(*args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(procs, "_run", fake_run)
    assert asyncio.run(procs.open_viewer({"id": 7, "name": "NZXT", "address": "192.168.0.70", "viewer": "krdc"})) is True
    args = calls[0]
    assert args[0] == "systemd-run" and "--unit=hub-vnc-7.service" in args
    assert list(args[args.index("--") + 1:]) == ["krdc", "vnc://192.168.0.70"]


@pytest.mark.parametrize("address", ["-via evil", "--help", "host name", "pi;rm", "", "a" * 300])
def test_address_is_validated(owner_client, address):
    """An address starting with '-' would be read as one of vncviewer's own options."""
    assert owner_client.post(f"{BASE}/remote", json={"name": "x", "address": address}).status_code == 422


def test_owner_only(guest_client):
    assert guest_client.post(f"{BASE}/remote", json={"name": "x", "address": "pi-1.local"}).status_code == 403


@pytest.mark.parametrize("address,expected", [
    ("pi-2.local", ("pi-2.local", 5900)),
    ("100.110.168.96", ("100.110.168.96", 5900)),
    ("server:1", ("server", 5901)),
    ("server:5999", ("server", 5999)),
    ("server::6000", ("server", 6000)),
    ("[fd00::1]", ("fd00::1", 5900)),
    ("[fd00::1]:2", ("fd00::1", 5902)),
    ("[fd00::1]::7000", ("fd00::1", 7000)),
])
def test_vnc_port(address, expected):
    assert procs.vnc_port(address) == expected


def test_state_and_open(owner_client, monkeypatch):
    hid = mk(owner_client)
    opened = []

    async def fake_open(host):
        opened.append((host["id"], host["address"]))
        return len(opened) == 1

    monkeypatch.setattr(procs, "open_viewer", fake_open)
    r = owner_client.post(f"{BASE}/remote/{hid}/open")
    assert r.status_code == 200 and r.json() == {"ok": True, "already_open": False}
    assert owner_client.post(f"{BASE}/remote/{hid}/open").json()["already_open"] is True
    assert opened == [(hid, "pi-1.local"), (hid, "pi-1.local")]
    assert owner_client.post(f"{BASE}/remote/999999/open").status_code == 404

    row = next(r for r in owner_client.get(f"{BASE}/state").json()["remote"] if r["id"] == hid)
    assert row["name"] == "Pi 1 desktop" and row["open"] is False and "up" in row


def test_viewer_unit_counts_as_open(owner_client, monkeypatch):
    hid = mk(owner_client)

    async def fake_units():
        return {procs.vnc_unit(hid)}

    monkeypatch.setattr(procs, "active_units", fake_units)
    row = next(r for r in owner_client.get(f"{BASE}/state").json()["remote"] if r["id"] == hid)
    assert row["open"] is True


def test_device_by_name_shows_its_lan_address(monkeypatch):
    """A .local name is looked up each sweep (the router moves the Pis around); the address is kept while it's off."""
    import asyncio
    import socket
    from app.bus import Bus
    from app.health import Health

    h = Health(Bus())
    answers = {"pi-2.local": "192.168.0.80"}
    pinged = []

    async def fake_getaddrinfo(host, *a, **kw):
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 0))]

    async def fake_ping(host):
        pinged.append(host)
        return True, 3

    monkeypatch.setattr(h, "_ping", fake_ping)

    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
        assert await h._remote("remote:1", "pi-2.local") == (True, 3)
        assert h.resolved == {"remote:1": "192.168.0.80"} and h._resolved_changed
        answers.clear()                                         # switched off: no mDNS answer, no ping, address kept
        assert await h._remote("remote:1", "pi-2.local") == (False, 0)
        assert h.resolved == {"remote:1": "192.168.0.80"}
        assert await h._remote("remote:2", "100.64.0.9") == (True, 3)   # an IP is pinged as it is, nothing to show
        assert "remote:2" not in h.resolved

    asyncio.run(run())
    assert pinged == ["192.168.0.80", "100.64.0.9"]
