"""Monitoring's other machines: the stats script (run against a fake /proc + /sys), Citadel's side of the stats
collector (its API faked with httpx.MockTransport: tests never reach NZXT), the endpoints, and the reminders."""
import asyncio
import importlib.machinery
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import config, db, machines

BASE = "/api/owner"
AT = datetime(2026, 11, 17, 21, 0, tzinfo=config.TZ)
GB = 1024**3
SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "machines" / "citadel-stats"


def load_script():
    loader = importlib.machinery.SourceFileLoader("citadel_stats", str(SCRIPT))
    spec = importlib.util.spec_from_loader("citadel_stats", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def fake_root(tmp_path: Path, hwmon: dict[str, dict[str, str]], mounts: str = "", battery: bool = False) -> Path:
    def put(rel, text):
        p = tmp_path / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    put("/proc/stat", "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\ncpu1 50 0 50 350 50 0 0 0 0 0\nintr 1\n")
    put("/proc/meminfo", "MemTotal:       16000000 kB\nMemAvailable:   12000000 kB\nSwapTotal:       1000000 kB\nSwapFree:         900000 kB\nBuffers: 1 kB\nCached: 2 kB\n")
    put("/proc/uptime", "3600.5 100.0\n")
    put("/proc/loadavg", "0.10 0.20 0.30 1/100 1\n")
    put("/proc/sys/kernel/hostname", "Phoenix\n")
    put("/etc/os-release", 'NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    put("/proc/mounts", mounts or "/dev/sda1 / ext4 rw 0 0\n")
    for i, (name, files) in enumerate(hwmon.items()):
        put(f"/sys/class/hwmon/hwmon{i}/name", name + "\n")
        for f, v in files.items():
            put(f"/sys/class/hwmon/hwmon{i}/{f}", v + "\n")
    if battery:
        put("/sys/class/power_supply/BAT0/capacity", "87\n")
        put("/sys/class/power_supply/BAT0/status", "Discharging\n")
    (tmp_path / "mnt").mkdir(exist_ok=True)
    return tmp_path


def run_script(root: Path) -> dict:
    mod = load_script()
    mod.ROOT, mod.SAMPLE = str(root), 0
    return mod.collect()


# ---- the stats script ----------------------------------------------------------------------------

def test_script_on_a_phenom_with_a_radeon(tmp_path):
    """Phoenix: k10temp with only an unlabelled temp1 (Phenom II), radeon reports a temperature and nothing else."""
    root = fake_root(tmp_path, {"k10temp": {"temp1_input": "41500"}, "radeon": {"temp1_input": "52000"}, "atk0110": {}})
    s = run_script(root)
    assert s["v"] == 1 and s["host"] == "Phoenix" and s["os"] == "Debian GNU/Linux 13 (trixie)"
    assert s["cpu"] == {"percent": None, "cores": 2, "temp": 41.5}      # one sample under test: no percent
    assert s["gpu"] == {"name": "AMD Radeon", "percent": None, "temp": 52.0, "mem_used_mb": None, "mem_total_mb": None}
    assert s["mem"]["total"] == 16000000 * 1024 and s["mem"]["swap_used"] == 100000 * 1024
    assert s["uptime_s"] == 3600 and s["load"] == [0.1, 0.2, 0.3]
    assert s["battery"] is None and s["pi"] is None
    json.dumps(s)


def test_script_reads_radeontop_for_load_and_vram():
    """Phoenix's radeon driver has no load figure: radeontop's one-line dump gives it, and VRAM used + total."""
    mod = load_script()
    line = ("Dumping to -, line limit 1.\n1795028249.517164: bus 01, gpu 12.50%, ee 0.00%, vgt 0.00%, cb 0.00%, "
            "vram 43.14% 441.77mb, gtt 2.78% 56.80mb, mclk inf% 0.150ghz, sclk 30.00% 0.300ghz\n")
    assert mod.radeontop(line) == {"percent": 12.5, "mem_used_mb": 442, "mem_total_mb": 1024}
    assert mod.radeontop(None) == {} and mod.radeontop("Dumping to -, line limit 1.\n") == {}


def test_script_prefers_the_labelled_package_sensor(tmp_path):
    root = fake_root(tmp_path, {"coretemp": {"temp1_label": "Core 0", "temp1_input": "40000", "temp2_label": "Package id 0", "temp2_input": "47000"}},
                     battery=True)
    s = run_script(root)
    assert s["cpu"]["temp"] == 47.0
    assert s["battery"] == {"percent": 87, "status": "Discharging"}


def test_script_skips_network_and_pseudo_filesystems_and_lists_each_disk_once(tmp_path):
    """Statting /mnt/Films on NZXT would wake an automount (and hang with Nasferatu off); btrfs subvolumes share a device."""
    mounts = ("/dev/nvme0n1p3 / btrfs rw 0 0\n/dev/nvme0n1p3 /home btrfs rw 0 0\n/dev/nvme0n1p2 /boot vfat rw 0 0\n"
              "//192.168.0.32/Films /mnt cifs rw 0 0\nsystemd-1 /mnt autofs rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n/dev/loop0 /snap squashfs ro 0 0\n")
    s = run_script(fake_root(tmp_path, {}, mounts))
    assert [d["mount"] for d in s["disks"]] == ["/"]
    assert s["disks"][0]["total"] > 0


@pytest.mark.parametrize("text,now,seen", [("throttled=0x0", False, False), ("throttled=0x50005", True, True), ("throttled=0x50000", False, True)])
def test_pi_power_flags(text, now, seen):
    p = load_script().pi_power(text)
    assert (p["under_voltage_now"], p["under_voltage_seen"]) == (now, seen)


def test_cpu_percent_from_two_samples():
    mod = load_script()
    assert mod.cpu_percent((800, 1000, 4), (850, 1100, 4)) == 50.0
    assert mod.cpu_percent((800, 1000, 4), (800, 1000, 4)) is None


# ---- the stats script's busiest processes --------------------------------------------------------

def test_top_processes_sum_same_names_and_skip_the_quiet_ones():
    mod = load_script()
    a = {1: ("brave", 100), 2: ("brave", 50), 3: ("plexmediaserver", 0), 4: ("sshd", 10), 5: ("gone", 5)}
    b = {1: ("brave", 130), 2: ("brave", 60), 3: ("plexmediaserver", 100), 4: ("sshd", 10), 6: ("new", 50)}
    top = mod.top_procs(a, b, total=400)
    assert top == [{"name": "plexmediaserver", "percent": 25.0}, {"name": "brave", "percent": 10.0}]
    assert mod.top_procs(a, b, total=0) == []


def test_proc_ticks_reads_names_with_spaces_and_brackets(tmp_path):
    mod = load_script()
    (tmp_path / "proc" / "42").mkdir(parents=True)
    (tmp_path / "proc" / "42" / "stat").write_text("42 (Web Content (x)) S 1 42 42 0 -1 4194560 1 0 0 0 7 3 0 0 20 0 1 0 1 1 1\n")
    mod.ROOT = str(tmp_path)
    assert mod.proc_ticks() == {42: ("Web Content (x)", 10)}


# ---- reading the collector -----------------------------------------------------------------------

@pytest.fixture
def hosts(client):
    with db.db() as con:
        con.execute("DELETE FROM remote_hosts")
        con.executemany("INSERT INTO remote_hosts (id, name, address, viewer, ssh_user, always_on, sort) VALUES (?, ?, ?, 'krdc', ?, ?, ?)",
                        [(1, "NZXT", "192.168.0.70", "mike", 1, 10), (2, "Phoenix", "192.168.0.82", "mike", 0, 20),
                         (3, "Probe-1", "probe-1.local", "", 1, 30), (4, "Nasferatu", "192.168.0.32::5901", "admin", 1, 40)])
    yield
    with db.db() as con:
        con.execute("DELETE FROM remote_hosts")
        con.execute("DELETE FROM widget_settings WHERE widget = 'alerts'")


def stats(cpu_temp=40.0, disks=None, gpu=None, raid=None, pi=None):
    return {"v": 1, "host": "x", "cpu": {"percent": 5.0, "cores": 4, "temp": cpu_temp}, "mem": {"total": 16 * GB, "used": 4 * GB},
            "disks": disks or [{"mount": "/", "total": 500 * GB, "free": 400 * GB}], "gpu": gpu, "raid": raid, "pi": pi}


class FakeCollector:
    """Stands in for the collector's API (httpx.MockTransport): records what Citadel sends, answers /api/latest."""
    def __init__(self, latest=None, down=False, token="tok"):
        self.latest, self.down, self.token, self.calls, self.machines = latest or [], down, token, [], None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        self.calls.append((request.method, request.url.path))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorised"})
        if request.method == "PUT":
            self.machines = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/latest":
            return httpx.Response(200, json={"machines": self.latest})
        if request.url.path == "/api/series":
            return httpx.Response(200, json={"machine": request.url.params["machine"], "t": [1], "metrics": {}})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def token(monkeypatch, tmp_path):
    f = tmp_path / "collector_token"
    f.write_text("tok\n")
    monkeypatch.setattr(config, "COLLECTOR_TOKEN_FILE", f)
    monkeypatch.setattr(config, "COLLECTOR_URL", "http://collector.test:8680")
    return f


def watcher(health_state, fake=None):
    m = machines.Machines(SimpleNamespace(state=health_state))
    m._client = httpx.AsyncClient(transport=httpx.MockTransport(fake or FakeCollector()))

    async def local():
        return stats(cpu_temp=55)
    m._local_stats = local
    m._resolve = lambda host: asyncio.sleep(0, result={"probe-1.local": "192.168.0.79"}.get(host, host))
    return m


def test_sweep_sends_the_machines_pushes_gaming_and_reads_the_latest(hosts, token):
    fake = FakeCollector(latest=[{"id": "rh1", "at": 100, "stats": stats(), "error": None},
                                 {"id": "rh4", "at": None, "stats": None, "error": "Not set up for monitoring (run tools/machines_setup.py)"},
                                 {"id": "this", "at": 99, "stats": stats(cpu_temp=55), "error": None}])
    health = {"remote:1": {"ok": True}, "remote:2": {"ok": False, "down_since": 1}, "remote:4": {"ok": True}}
    m = watcher(health, fake)
    asyncio.run(m.sweep())
    # Probe-1 has no SSH user; Nasferatu's VNC port is stripped; Gaming goes first and is pushed, never polled
    assert [x["id"] for x in fake.machines] == ["this", "rh1", "rh2", "rh4"]
    assert fake.machines[0]["push"] and fake.machines[3]["host"] == "192.168.0.32" and fake.machines[3]["user"] == "admin"
    assert ("POST", "/api/ingest/this") in fake.calls
    view = {v["name"]: v for v in m.view()}
    assert set(view) == {"NZXT", "Phoenix", "Nasferatu"}
    assert view["NZXT"]["stats"]["cpu"]["temp"] == 40.0 and view["NZXT"]["error"] is None and view["NZXT"]["up"] is True
    assert view["Phoenix"]["up"] is False and view["Phoenix"]["stats"] is None
    assert view["Nasferatu"]["error"].startswith("Not set up")
    chips = m.stats_machines()
    assert [c["id"] for c in chips] == ["this", "rh1", "rh2", "rh4"] and chips[0]["stats"]["cpu"]["temp"] == 55
    assert m.collector["ok"] is True
    # an unchanged list isn't sent again on the next sweep
    asyncio.run(m.sweep())
    assert sum(1 for c in fake.calls if c[0] == "PUT") == 1


def test_collector_down_keeps_the_last_good_reading(hosts, token):
    fake = FakeCollector(latest=[{"id": "rh1", "at": 100, "stats": stats(), "error": None}])
    m = watcher({"remote:1": {"ok": True}}, fake)
    asyncio.run(m.sweep())
    fake.down = True
    asyncio.run(m.sweep())
    nzxt = next(v for v in m.view() if v["name"] == "NZXT")
    assert nzxt["stats"] is not None and nzxt["error"] == machines.DOWN
    assert m.collector == {"ok": False, "error": machines.DOWN, "at": m.collector["at"]}


def test_no_token_means_not_set_up(hosts, token):
    token.unlink()
    m = watcher({"remote:1": {"ok": True}})
    asyncio.run(m.sweep())
    assert m.collector["error"] == machines.NOT_SET_UP


def test_a_wrong_token_is_explained(hosts, token):
    m = watcher({}, FakeCollector(token="other"))
    asyncio.run(m.sweep())
    assert "token" in m.collector["error"]


def test_history_checks_what_it_asks_for(hosts, token):
    m = watcher({})
    got = asyncio.run(m.history("series", "rh4", 0, 100))
    assert got["machine"] == "rh4"
    for kind, mid, a, b in [("samples", "rh4", 0, 1), ("series", "rh4; drop", 0, 1), ("series", "this", 5, 5)]:
        with pytest.raises(machines.HistoryError):
            asyncio.run(m.history(kind, mid, a, b))


def test_ipv4_passes_addresses_through():
    assert machines.ipv4("192.168.0.70") == "192.168.0.70"
    assert machines.ipv4("definitely-not-a-host.invalid") is None


def test_machines_endpoints_are_owner_only(owner_client, guest_client, hosts):
    assert guest_client.get(f"{BASE}/machines").status_code == 403
    assert guest_client.get(f"{BASE}/stats/machines").status_code == 403
    body = owner_client.get(f"{BASE}/machines").json()
    assert [m["name"] for m in body["machines"]] == ["NZXT", "Phoenix", "Nasferatu"]
    body = owner_client.get(f"{BASE}/stats/machines").json()
    assert [m["id"] for m in body["machines"]] == ["this", "rh1", "rh2", "rh4"]
    # tests run with HUB_NO_MACHINES: no collector, so history is a plain 503
    r = owner_client.get(f"{BASE}/stats/series", params={"machine": "rh1", "start": 0, "end": 100})
    assert r.status_code == 503 and "collector" in r.json()["detail"]
    assert owner_client.get(f"{BASE}/stats/other", params={"machine": "rh1", "start": 0, "end": 100}).status_code == 422


# ---- reminders -----------------------------------------------------------------------------------

def test_reminders_disk_raid_hot_and_always_on_off(hosts):
    t = AT.timestamp()
    health = {"remote:1": {"ok": True}, "remote:2": {"ok": False, "down_since": int(t - 3600)},
              "remote:3": {"ok": False, "down_since": int(t - 900)}, "remote:4": {"ok": True}}
    m = watcher(health)
    m.state = {
        1: {"stats": stats(cpu_temp=60, gpu={"temp": 90.0}), "at": int(t), "error": None, "hot_since": t - 400},
        4: {"stats": stats(disks=[{"mount": "C:", "total": 120 * GB, "free": 60 * GB}, {"mount": "D:", "total": 12000 * GB, "free": 1000 * GB}],
                           raid={"health": "Warning", "status": "Degraded"}), "at": int(t), "error": None, "hot_since": None},
    }
    got = {a["key"]: a for a in m.alerts(AT)}
    # Phoenix is off but isn't "always on"; Probe-1 is (no SSH user needed for that one)
    assert f"mdown:3:{int(t - 900)}" in got and got[f"mdown:3:{int(t - 900)}"]["title"] == "Probe-1 isn't answering"
    assert not any(k.startswith("mdown:2:") for k in got)
    assert got[f"mdisk:4:D::{AT.date()}"]["title"] == "Nasferatu: D: is nearly full"
    assert not any(k.startswith("mdisk:4:C:") for k in got)
    assert got[f"mraid:4:{AT.date()}"]["level"] == "now"
    assert f"mhot:1:{AT.date()}" in got and "GPU 90°C" in got[f"mhot:1:{AT.date()}"]["detail"]


def test_hot_needs_five_minutes_and_a_pi_runs_cooler(hosts):
    t = AT.timestamp()
    m = watcher({"remote:1": {"ok": True}})
    m.state = {1: {"stats": stats(cpu_temp=88), "at": int(t), "error": None, "hot_since": t - 200}}
    assert not [a for a in m.alerts(AT) if a["key"].startswith("mhot")]
    assert machines._hot(stats(cpu_temp=81), is_pi=True) and not machines._hot(stats(cpu_temp=81), is_pi=False)


def test_the_other_machines_switch_turns_them_all_off(hosts, owner_client):
    t = AT.timestamp()
    m = watcher({"remote:3": {"ok": False, "down_since": int(t - 900)}})
    assert m.alerts(AT)
    r = owner_client.put(f"{BASE}/alerts/settings", json={"machines": False}, headers={"X-Hub": "1"})
    assert r.status_code == 200, r.text
    assert m.alerts(AT) == []


# ---- specifications (the Fleet page's Specifications card) --------------------------------------------------------

def test_script_reports_what_the_machine_is(tmp_path):
    root = fake_root(tmp_path, {})
    put = lambda rel, text: ((tmp_path / rel.lstrip("/")).parent.mkdir(parents=True, exist_ok=True), (tmp_path / rel.lstrip("/")).write_text(text))
    put("/proc/cpuinfo", "processor\t: 0\nmodel name\t: AMD Phenom(tm) II X4 955 Processor\n\nprocessor\t: 1\nmodel name\t: AMD Phenom(tm) II X4 955 Processor\n")
    for i in (0, 1):
        put(f"/sys/devices/system/cpu/cpu{i}/topology/core_id", f"{i}\n")
        put(f"/sys/devices/system/cpu/cpu{i}/topology/physical_package_id", "0\n")
    put("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "3200000\n")
    put("/sys/class/dmi/id/sys_vendor", "To Be Filled By O.E.M.\n")
    put("/sys/class/dmi/id/board_vendor", "ASUSTeK Computer INC.\n")
    put("/sys/class/dmi/id/board_name", "M4A79XTD EVO\n")
    sp = run_script(root)["spec"]
    assert sp["cpu"] == "AMD Phenom(tm) II X4 955 Processor" and sp["cores"] == 2 and sp["threads"] == 2 and sp["max_mhz"] == 3200
    assert sp["dmi"]["board_name"] == "M4A79XTD EVO" and sp["gpus"] == []          # lspci is skipped under --root
    assert machines.spec_view({"spec": sp, "mem": {"total": 16 * GB - 300 * 2**20}, "os": "Debian"})["model"] == "ASUS M4A79XTD EVO"


def test_script_names_raspberry_pi_os(tmp_path):
    root = fake_root(tmp_path, {})
    (tmp_path / "etc/os-release").write_text('ID=debian\nVERSION_ID="13"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    (tmp_path / "etc/rpi-issue").write_text("Raspberry Pi reference 2026-10-01\n")
    assert run_script(root)["os"] == "Raspberry Pi OS (Debian 13)"


def test_lspci_graphics_cards():
    mod = load_script()
    text = ('00:00.0 "Host bridge" "Intel Corporation" "Device 9a14" -r01 "Dell" "Device 0a7b"\n'
            '00:02.0 "VGA compatible controller" "Intel Corporation" "TigerLake-LP GT2 [Iris Xe Graphics]" -r01 -p00 "Dell" "Device 0a7b"\n'
            '01:00.0 "3D controller" "NVIDIA Corporation" "GA107M [GeForce RTX 3050 Mobile]" -ra1 "Dell" "Device 0a7b"\n')
    assert mod.gpus_lspci(text) == [{"vendor": "Intel Corporation", "device": "TigerLake-LP GT2 [Iris Xe Graphics]"},
                                    {"vendor": "NVIDIA Corporation", "device": "GA107M [GeForce RTX 3050 Mobile]"}]


@pytest.mark.parametrize("raw, name", [
    ("AMD Ryzen 7 5800X3D 8-Core Processor", "AMD Ryzen 7 5800X3D"),
    ("11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz", "Intel Core i5-1135G7"),
    ("Intel(R) Core(TM) i7-4770K CPU @ 3.50GHz", "Intel Core i7-4770K"),
    ("Intel(R) Pentium(R) CPU G3258 @ 3.20GHz", "Intel Pentium G3258"),
    ("AMD Phenom(tm) II X4 955 Processor", "AMD Phenom II X4 955"),
    (None, None),
])
def test_cpu_names(raw, name):
    assert machines.cpu_name(raw) == name


@pytest.mark.parametrize("vendor, device, name", [
    ("NVIDIA Corporation", "GA102 [GeForce RTX 3080 Ti]", "NVIDIA GeForce RTX 3080 Ti"),
    ("Intel Corporation", "TigerLake-LP GT2 [Iris Xe Graphics]", "Intel Iris Xe Graphics"),
    ("Advanced Micro Devices, Inc. [AMD/ATI]", "Cape Verde PRO [Radeon HD 7750/8740 / R7 250E]", "AMD Radeon HD 7750"),
    ("Intel Corporation", "Intel(R) HD Graphics", "Intel HD Graphics"),        # Windows' name already has the maker
])
def test_gpu_names(vendor, device, name):
    assert machines.gpu_name(vendor, device) == name


def test_ram_is_shown_as_the_size_it_was_sold_as():
    assert [machines.ram_gb(g * GB) for g in (3.7, 7.5, 15.6, 23.4, 31.3, 64)] == [4, 8, 16, 24, 32, 64]
    assert machines.ram_gb(None) is None


def test_spec_view_from_real_shaped_readings():
    gaming = {"os": "CachyOS", "mem": {"total": 33_600_000_000}, "gpu": {"name": "NVIDIA GeForce RTX 3080 Ti", "mem_total_mb": 12288.0},
              "spec": {"dmi": {"sys_vendor": "ASUS", "product_name": "System Product Name", "board_vendor": "ASUSTeK COMPUTER INC.",
                               "board_name": "ROG STRIX B550-F GAMING WIFI II"}, "cpu": "AMD Ryzen 7 5800X3D 8-Core Processor",
                       "cores": 8, "threads": 16, "max_mhz": 4553, "gpus": [{"vendor": "NVIDIA Corporation", "device": "GA102 [GeForce RTX 3080 Ti]"}],
                       "kernel": "7.3.5-2-cachyos", "arch": "x86_64"}}
    v = machines.spec_view(gaming)
    assert v == {"model": "ASUS ROG STRIX B550-F GAMING WIFI II", "cpu": "AMD Ryzen 7 5800X3D", "cores": 8, "threads": 16, "ghz": 4.6,
                 "gpus": [{"name": "NVIDIA GeForce RTX 3080 Ti", "vram_gb": 12.0}], "ram_gb": 32, "ram_type": None, "ram_sticks": None,
                 "ram_mts": None, "os": "CachyOS", "kernel": "7.3.5-2-cachyos", "arch": "x86_64"}
    probe = {"os": "Home Assistant OS 18.5", "mem": {"total": 3_976_265_728}, "gpu": None,
             "spec": {"model": "Raspberry Pi 4 Model B Rev 1.1", "cpu": None, "cpu_part": "0xd08", "cores": 4, "threads": 4, "max_mhz": 1500, "gpus": []}}
    v = machines.spec_view(probe)
    assert v["cpu"] == "Broadcom BCM2711 (Cortex-A72)" and v["gpus"] == [{"name": "VideoCore VI", "vram_gb": None}] and v["ram_gb"] == 4
    win = {"os": "Windows 10 Pro N", "mem": {"total": 8_238_239_744}, "cpu": {"cores": 2},
           "spec": {"dmi": {"sys_vendor": "To Be Filled By O.E.M.", "product_name": "To Be Filled By O.E.M.", "board_vendor": "ASRock", "board_name": "Z87M Pro4"},
                    "cpu": "Intel(R) Pentium(R) CPU G3258 @ 3.20GHz", "cores": 2, "threads": 2, "max_mhz": 3200,
                    "gpus": [{"vendor": "Intel Corporation", "device": "Intel(R) HD Graphics", "vram_mb": 1024}]}}
    v = machines.spec_view(win)
    assert v["model"] == "ASRock Z87M Pro4" and v["gpus"] == [{"name": "Intel HD Graphics", "vram_gb": None}]   # Intel's is shared memory


def test_spec_view_before_the_scripts_sent_specs():
    """A machine still on the old script (Phoenix asleep, say) shows what its reading has anyway."""
    v = machines.spec_view({"os": "Debian GNU/Linux 13 (trixie)", "cpu": {"cores": 4}, "mem": {"total": 16 * GB},
                            "gpu": {"name": "AMD Radeon", "mem_total_mb": 1024}})
    assert v["cpu"] is None and v["threads"] == 4 and v["cores"] == 4 and v["ram_gb"] == 16
    assert v["gpus"] == [{"name": "AMD Radeon", "vram_gb": 1.0}]
    assert machines.spec_view(None) is None


def test_memory_sticks_from_udev():
    mod = load_script()
    text = ("MEMORY_ARRAY_NUM_DEVICES=3\nMEMORY_DEVICE_0_SIZE=8589934592\nMEMORY_DEVICE_0_TYPE=DDR4\nMEMORY_DEVICE_0_SPEED_MTS=3200\n"
            "MEMORY_DEVICE_0_CONFIGURED_SPEED_MTS=3000\nMEMORY_DEVICE_0_FORM_FACTOR=SODIMM\n"
            "MEMORY_DEVICE_1_PRESENT=0\nMEMORY_DEVICE_1_TYPE=Unknown\n"
            "MEMORY_DEVICE_12_SIZE=8589934592\nMEMORY_DEVICE_12_TYPE=DDR4\nMEMORY_DEVICE_12_SPEED_MTS=3200\n")
    assert mod.memory_modules(text) == [{"type": "DDR4", "size": 8 * GB, "mts": 3000, "form": "SODIMM"},
                                        {"type": "DDR4", "size": 8 * GB, "mts": 3200, "form": None}]
    assert mod.memory_modules(None) == []


def test_memory_type_and_sticks():
    view = lambda ram, model=None: machines.spec_view({"mem": {"total": 16 * GB}, "spec": {"model": model, "ram": ram}})
    v = view([{"type": "DDR4", "size": 8 * GB, "mts": 3600}] * 2)
    assert (v["ram_type"], v["ram_sticks"], v["ram_mts"]) == ("DDR4", "2 × 8 GB", 3600)
    assert view([{"type": "DDR3", "size": 8 * GB}, {"type": "DDR3", "size": 4 * GB}])["ram_sticks"] == "8 GB + 4 GB"
    # Phoenix: the BIOS says "Other", the board only takes DDR3
    phoenix = machines.spec_view({"mem": {"total": 16 * GB}, "spec": {"dmi": {"board_vendor": "ASUSTeK Computer INC.", "board_name": "M4A77T"},
                                                                     "ram": [{"type": "Other", "size": 8 * GB, "mts": 1600}] * 2}})
    assert (phoenix["model"], phoenix["ram_type"]) == ("ASUS M4A77T", "DDR3")
    pi = view([], "Raspberry Pi 4 Model B Rev 1.1")
    assert (pi["ram_type"], pi["ram_sticks"], pi["ram_mts"]) == ("LPDDR4", None, None)
    assert view([{"type": "Other", "size": 8 * GB}])["ram_type"] is None          # unknown board: no guess
