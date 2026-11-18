"""The stats collector that runs on NZXT (collector/citadel_collector.py): storage tiers, history queries, spikes,
pruning and its HTTP API. SSH is never run: the fetcher is faked, and the API is served on a loopback port."""
import importlib.machinery
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "collector" / "citadel_collector.py"


def load():
    loader = importlib.machinery.SourceFileLoader("citadel_collector", str(SRC))
    spec = importlib.util.spec_from_loader("citadel_collector", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cc = load()
T0 = 1_800_000_000 - 1_800_000_000 % 3600           # on an hour boundary


def stats(cpu=10.0, used=4, total=16, temp=50.0, top=None, disks=None, gpu=None):
    return {"v": 1, "cpu": {"percent": cpu, "cores": 4, "temp": temp},
            "mem": {"total": total * 2**30, "used": used * 2**30, "swap_total": 0, "swap_used": 0},
            "disks": disks or [{"mount": "/", "total": 100, "free": 40}], "gpu": gpu, "top": top or []}


@pytest.fixture
def store(tmp_path):
    s = cc.Store(tmp_path / "m.sqlite")
    s.set_machines([{"id": "rh5", "name": "Nasferatu", "host": "192.168.0.32", "user": "admin"},
                    {"id": "this", "name": "Gaming", "push": True}])
    return s


def test_figures_from_a_stats_reading():
    f = cc.figures(stats(cpu=12.5, used=4, total=16, gpu={"percent": 30, "temp": 61}))
    assert f == {"cpu": 12.5, "mem": 25.0, "swap": None, "cpu_temp": 50.0, "gpu": 30.0, "gpu_temp": 61.0}
    assert cc.figures({"v": 1})["cpu"] is None


def test_tiers_fill_as_samples_arrive(store):
    for i, cpu in enumerate([10, 20, 30, 40]):
        store.record("rh5", stats(cpu=cpu), T0 + i * 30)
    with store.con() as con:
        assert con.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 4
        r = con.execute("SELECT n, cpu_s, cpu_n, cpu_x, gpu_n, gpu_x FROM agg5m").fetchone()
        assert tuple(r) == (4, 100.0, 4, 40.0, 0, None)
        assert con.execute("SELECT n, cpu_x FROM agg1h").fetchone()[1] == 40.0
        assert con.execute("SELECT COUNT(*) FROM disks").fetchone()[0] == 1          # one row per disk per hour


def test_series_picks_the_tier_and_drops_figures_it_never_had(store):
    for i in range(20):
        store.record("rh5", stats(cpu=i), T0 + i * 30)
    now = T0 + 600
    s = store.series("rh5", T0, T0 + 600, now)
    assert s["res"] == 30 and s["bucket"] == 30 and len(s["t"]) == 20
    assert s["metrics"]["cpu"]["avg"][:3] == [0.0, 1.0, 2.0]
    assert "gpu" not in s["metrics"] and "swap" not in s["metrics"]
    # a week: the 5-minute tier, grouped into ≤ MAX_POINTS buckets
    wk = store.series("rh5", now - 7 * 86400, now, now)
    assert wk["res"] == 300 and wk["bucket"] % 300 == 0 and (7 * 86400) / wk["bucket"] <= cc.MAX_POINTS
    assert wk["metrics"]["cpu"]["max"] == [19.0] and wk["metrics"]["cpu"]["avg"] == [9.5]
    # a year: hourly
    assert store.pick(now - 365 * 86400, now, now)[0] == "agg1h"
    # a short range that's older than the raw tier keeps still uses the 5-minute one
    assert store.pick(now - 10 * 86400, now - 10 * 86400 + 3600, now)[0] == "agg5m"


def test_spikes_need_two_high_readings_and_group_into_moments(store):
    col = cc.Collector(store, fetcher=lambda m: (None, "unused"))
    hot = [{"name": "Plex Media Scanner", "percent": 70.0}, {"name": "svchost", "percent": 5.0}]
    col.accept("rh5", stats(cpu=90, top=hot), T0)            # first high one: not yet
    col.accept("rh5", stats(cpu=80, top=hot), T0 + 30)
    col.accept("rh5", stats(cpu=95, top=hot), T0 + 60)
    col.accept("rh5", stats(cpu=5), T0 + 90)
    col.accept("rh5", stats(cpu=99, top=hot), T0 + 900)       # a lone high reading later: no spike
    sp = store.spikes("rh5", T0 - 1, T0 + 1000)
    assert len(sp) == 1
    assert sp[0]["start"] == T0 + 30 and sp[0]["end"] == T0 + 60 and sp[0]["peak"] == 95 and sp[0]["readings"] == 2
    assert sp[0]["top"][0] == {"name": "Plex Media Scanner", "percent": 70.0}


def test_prune_keeps_each_tier_for_its_own_time(store):
    now = T0 + 100 * 86400
    store.record("rh5", stats(), now - 8 * 86400)        # raw gone, 5 min kept
    store.record("rh5", stats(), now - 95 * 86400)       # 5 min gone, hourly kept
    store.prune(now)
    with store.con() as con:
        assert con.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM agg5m").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM agg1h").fetchone()[0] == 2


def test_sweep_polls_only_ssh_machines_and_keeps_the_last_good_reading(store):
    asked = []

    def fetcher(m):
        asked.append(m["id"])
        return (stats(cpu=7), None) if len(asked) == 1 else (None, "Off or asleep")
    col = cc.Collector(store, fetcher=fetcher)
    col.sweep()
    col.sweep()
    assert asked == ["rh5", "rh5"]                           # the pushed machine is never polled
    by = {m["id"]: m for m in store.latest()}
    assert by["rh5"]["stats"]["cpu"]["percent"] == 7 and by["rh5"]["error"] == "Off or asleep"
    assert by["rh5"]["checked"] >= by["rh5"]["at"]
    assert by["this"]["stats"] is None and by["this"]["push"]
    col.stop()


def test_ssh_argv_checks_the_name_but_connects_to_the_address():
    argv = cc.ssh_argv({"host": "probe-2.local", "addr": "192.168.0.80", "user": "mike"})
    assert "HostKeyAlias=probe-2.local" in argv and argv[-1] == "mike@192.168.0.80" and "-F" in argv
    assert cc.explain("Permission denied (publickey).", 255).startswith("Not set up")


@pytest.fixture
def api(store):
    col = cc.Collector(store, fetcher=lambda m: (None, "unused"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cc.make_handler(col, "s3cret"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None, token="s3cret"):
        req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield call
    srv.shutdown()
    col.stop()


def test_api_needs_the_token_except_for_health(api):
    assert api("GET", "/api/health", token=None)[0] == 200
    assert api("GET", "/api/latest", token=None)[0] == 401
    assert api("GET", "/api/latest", token="wrong")[0] == 401
    assert api("GET", "/api/latest")[0] == 200


def test_api_machines_ingest_and_history(api):
    code, _ = api("PUT", "/api/machines", [{"id": "this", "name": "Gaming", "push": True}, {"id": "rh4", "name": "NZXT", "host": "192.168.0.70", "user": "mike"}])
    assert code == 200
    assert api("PUT", "/api/machines", [{"name": "no id"}])[0] == 400
    assert api("POST", "/api/ingest/this", stats(cpu=33))[0] == 200
    assert api("POST", "/api/ingest/rh4", stats())[0] == 404            # only pushed machines take readings
    assert api("POST", "/api/ingest/this", {"v": 2})[0] == 400
    code, latest = api("GET", "/api/latest")
    assert [m["id"] for m in latest["machines"]] == ["this", "rh4"]
    assert latest["machines"][0]["stats"]["cpu"]["percent"] == 33
    now = int(time.time())
    code, s = api("GET", f"/api/series?machine=this&from={now - 3600}&to={now + 60}")
    assert code == 200 and s["metrics"]["cpu"]["avg"] == [33.0]
    assert api("GET", f"/api/disks?machine=this&from={now - 7200}&to={now + 60}")[1]["mounts"]["/"]["free"] == [40]
    assert api("GET", f"/api/spikes?machine=this&from={now - 3600}&to={now}")[1]["spikes"] == []
    assert api("GET", "/api/series?machine=this&from=10&to=5")[0] == 400
