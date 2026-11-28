"""The other machines on the estate, for Monitoring's Machines row, their reminders and the Stats page's history.

The machines are asked for their stats by the collector on NZXT (collector/citadel_collector.py), not by Citadel: Gaming
is off or asleep too often to keep a history, NZXT is left on. Citadel stays the source of truth for *which* machines:
every sweep it makes sure the collector has its Remote Access devices that have a monitoring SSH user (with the IPv4
address it resolves for each, since NZXT can't resolve `.local` names to IPv4). Gaming has no sshd, so Citadel runs
the stats script here and pushes Gaming's reading itself. Then it reads every machine's latest reading back; the Stats
page's history goes through `history`, so the browser never talks to NZXT. Plan: docs/tasks/stats-collector.md.

The collector being down only loses readings (nothing is buffered): the last good ones stay on the panels, each marked
with the collector's error, and the charts show a gap.

One instance per app lifespan on app.state, like Monitor and Health, never module state.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import sys
import time
from datetime import datetime
from typing import Any

import httpx

from . import alerts as alert_settings, config, procs
from .db import db
from .health import Health
from .monitor import DISK_LOW_BYTES, DISK_LOW_FRACTION

log = logging.getLogger("hub.machines")

INTERVAL = 30.0
# "Running hot": held this long, so a compile or a game loading doesn't raise a reminder.
HOT_FOR = 5 * 60
CPU_HOT, PI_HOT, GPU_HOT = 85.0, 80.0, 85.0     # a Pi 4 starts slowing itself down at 80 °C
DOWN_AFTER = 10 * 60          # same patience as the services' "isn't answering"
# The machine list is sent again this often even when it hasn't changed, so a reinstalled collector gets it back.
RESEND = 10 * 60
THIS_ID, THIS_NAME = "this", "Gaming"
STATS_SCRIPT = config.ROOT / "tools" / "machines" / "citadel-stats"
HISTORY = {"series", "disks", "spikes"}
MACHINE_ID = re.compile(r"^(this|rh\d{1,9})$")
DOWN = "The stats collector on NZXT isn't answering"
NOT_SET_UP = "The stats collector isn't set up (tools/collector_setup.py)"


def _hot(stats: dict[str, Any], is_pi: bool) -> bool:
    cpu = (stats.get("cpu") or {}).get("temp")
    gpu = (stats.get("gpu") or {}).get("temp")
    return (cpu is not None and cpu >= (PI_HOT if is_pi else CPU_HOT)) or (gpu is not None and gpu >= GPU_HOT)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def ipv4(host: str) -> str | None:
    """The IPv4 address this PC resolves a name to (an address is returned as it is), or None."""
    if _is_ip(host):
        return host
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None


# ---- specifications (the Fleet page's Specifications card) -----------------------------------------------------------
# The stats scripts report what the hardware calls itself (/proc/cpuinfo, lspci, DMI, WMI); these turn that into the
# names people use. Only the latest reading carries it, so it stays current without anyone keeping a list.

# What makers fill DMI fields with when they don't bother (desktop boards: the board's own name is the useful one)
DMI_JUNK = {"", "to be filled by o.e.m.", "system product name", "system version", "default string", "system manufacturer",
            "not applicable", "not specified", "none", "o.e.m.", "unknown", "type1productconfigid"}
VENDORS = {"asustek computer inc.": "ASUS", "asustek computer inc": "ASUS", "dell inc.": "Dell", "nvidia corporation": "NVIDIA",
           "intel corporation": "Intel", "advanced micro devices, inc. [amd/ati]": "AMD", "advanced micro devices, inc.": "AMD",
           "advanced micro devices, inc": "AMD", "micro-star international co., ltd.": "MSI", "gigabyte technology co., ltd.": "Gigabyte",
           "hewlett-packard": "HP", "lenovo": "Lenovo", "asrock": "ASRock", "ati technologies inc": "AMD"}
ARM_CORES = {"0xd03": "Cortex-A53", "0xd08": "Cortex-A72", "0xd0b": "Cortex-A76"}
PI_CHIPS = (("Raspberry Pi 5", "Broadcom BCM2712", "VideoCore VII", "LPDDR4X"), ("Raspberry Pi 4", "Broadcom BCM2711", "VideoCore VI", "LPDDR4"),
            ("Raspberry Pi 3", "Broadcom BCM2837", "VideoCore IV", "LPDDR2"))
# Boards whose firmware doesn't name the memory type ("Other"), and which only take one kind (from the board's manual)
BOARD_MEMORY = {"ASUS M4A77T": "DDR3"}           # Phoenix
RAM_SIZES = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512)


def _vendor(v: str | None) -> str:
    v = (v or "").strip()
    return VENDORS.get(v.lower(), re.sub(r"\s+(Inc\.?|Corporation|Corp\.?|Co\., Ltd\.?|Ltd\.?)$", "", v, flags=re.I))


def _clean(s: str) -> str:
    s = re.sub(r"\((R|TM|tm)\)|®|™", "", s)
    return re.sub(r"\s+", " ", s).strip()


def cpu_name(raw: str | None) -> str | None:
    """"11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz" → "Intel Core i5-1135G7"; "AMD Ryzen 7 5800X3D 8-Core
    Processor" → "AMD Ryzen 7 5800X3D"."""
    if not raw:
        return None
    s = _clean(raw)
    s = re.sub(r"^\d+(st|nd|rd|th) Gen ", "", s)
    s = re.sub(r"\s*@.*$", "", s)
    s = re.sub(r"\s+(\d+|Dual|Quad|Six|Eight)-Core Processor$|\s+Processor$|\s+CPU$", "", s, flags=re.I)
    return re.sub(r"\s+CPU\b", "", s).strip() or None


def gpu_name(vendor: str | None, device: str | None) -> str | None:
    """lspci / WMI names → "NVIDIA GeForce RTX 3080 Ti", "Intel Iris Xe Graphics", "AMD Radeon HD 7750"."""
    if not device:
        return None
    d = _clean(device)
    m = re.search(r"\[(.+)\]", d)
    if m:                                     # "GA102 [GeForce RTX 3080 Ti]": the marketing name is in the brackets
        d = m.group(1)
    d = d.split(" / ")[0].split("/")[0].strip()        # "Radeon HD 7750/8740 / R7 250E": the first of several
    v = _vendor(vendor)
    return d if not v or d.lower().startswith(v.lower()) else f"{v} {d}"


def ram_gb(total: Any) -> int | None:
    """The size the memory is sold as: Linux and Windows show a little less (firmware and onboard graphics keep some)."""
    if not isinstance(total, (int, float)) or total <= 0:
        return None
    gib = total / 1024**3
    return next((s for s in RAM_SIZES if s >= gib * 0.97), round(gib))


def _memory(sp: dict[str, Any], model: str | None, pi: tuple | None) -> dict[str, Any]:
    """The memory's type (DDR4…), its sticks ("4 × 8 GB") and speed (MT/s), from the firmware's table when it has one."""
    sticks = [m for m in sp.get("ram") or [] if isinstance(m.get("size"), (int, float)) and m["size"] > 0]
    named = [m["type"] for m in sticks if m.get("type") and m["type"] not in ("Other", "Unknown")]
    kind = max(set(named), key=named.count) if named else (pi[3] if pi else BOARD_MEMORY.get(model or ""))
    sizes = sorted({round(m["size"] / 1024**3) for m in sticks})
    layout = f"{len(sticks)} × {sizes[0]} GB" if len(sizes) == 1 else " + ".join(f"{round(m['size'] / 1024**3)} GB" for m in sticks) if sticks else None
    speeds = [m["mts"] for m in sticks if m.get("mts")]
    return {"ram_type": kind, "ram_sticks": layout, "ram_mts": max(speeds) if speeds else None}


def _model(sp: dict[str, Any]) -> str | None:
    if sp.get("model"):
        return _clean(sp["model"])
    dmi = sp.get("dmi") or {}
    ok = lambda k: (dmi.get(k) or "").strip().lower() not in DMI_JUNK
    for vk, pk in (("sys_vendor", "product_name"), ("board_vendor", "board_name")):
        if ok(pk):
            v, p = _vendor(dmi.get(vk)) if ok(vk) else "", dmi[pk].strip()
            return p if not v or p.lower().startswith(v.lower()) else f"{v} {p}"
    return None


def spec_view(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """One machine's specifications from its latest reading. A script from before 2026-11-26 sends no `spec`: what the
    reading has anyway (OS, memory, threads, the GPU's name) still shows."""
    if not stats:
        return None
    sp, gpu = stats.get("spec") or {}, stats.get("gpu") or {}
    model = _model(sp)
    pi = next((p for p in PI_CHIPS if model and model.startswith(p[0])), None)
    cpu = cpu_name(sp.get("cpu"))
    if not cpu and (sp.get("cpu_part") or pi):
        core = ARM_CORES.get((sp.get("cpu_part") or "").lower())
        cpu = f"{pi[1]} ({core})" if pi and core else pi[1] if pi else f"ARM {core}" if core else None
    gpus = []
    for g in sp.get("gpus") or []:
        name, vendor = gpu_name(g.get("vendor"), g.get("device")), _vendor(g.get("vendor"))
        vram = g.get("vram_mb") if vendor != "Intel" else None      # an Intel chip's "VRAM" is borrowed system memory
        if vendor == "NVIDIA" and str(gpu.get("name") or "").startswith("NVIDIA"):
            name, vram = gpu["name"], gpu.get("mem_total_mb")        # nvidia-smi knows it best
        elif vendor == "AMD" and gpu.get("mem_total_mb"):
            vram = gpu["mem_total_mb"]                              # radeontop's VRAM total
        if name:
            gpus.append({"name": name, "vram_gb": round(vram / 1024, 1) if vram else None})
    if not gpus and gpu.get("name"):
        gpus.append({"name": gpu["name"], "vram_gb": round(gpu["mem_total_mb"] / 1024, 1) if gpu.get("mem_total_mb") else None})
    if not gpus and pi:
        gpus.append({"name": pi[2], "vram_gb": None})
    threads = sp.get("threads") or (stats.get("cpu") or {}).get("cores")
    return {"model": model, "cpu": cpu, "cores": sp.get("cores") or threads, "threads": threads,
            "ghz": round(sp["max_mhz"] / 1000, 1) if sp.get("max_mhz") else None, "gpus": gpus,
            "ram_gb": ram_gb((stats.get("mem") or {}).get("total")), **_memory(sp, model, pi), "os": stats.get("os"),
            "kernel": sp.get("kernel"), "arch": sp.get("arch")}


class HistoryError(Exception):
    """The collector couldn't answer a history question; the message is for Mike."""


class Machines:
    def __init__(self, health: Health) -> None:
        self.health = health
        # remote_hosts id -> {stats, at, error, hot_since}
        self.state: dict[int, dict[str, Any]] = {}
        self.this: dict[str, Any] = {"stats": None, "at": None, "error": None}     # Gaming, as the collector has it
        self.collector: dict[str, Any] = {"ok": None, "error": None, "at": None}
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._sent: tuple[str, float] | None = None              # (machine list JSON, when) last sent to the collector
        self._addr: dict[str, tuple[str | None, float]] = {}     # host -> (IPv4, when resolved)

    async def start(self) -> None:
        if not config.NO_MACHINES:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0))
            self._task = asyncio.create_task(self._loop(), name="machines")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("machine sweep failed")
            await asyncio.sleep(INTERVAL)

    @staticmethod
    def _rows() -> list[dict[str, Any]]:
        with db() as con:
            return [dict(r) for r in con.execute("SELECT id, name, address, ssh_user, always_on FROM remote_hosts ORDER BY sort, id")]

    @staticmethod
    def _token() -> str | None:
        try:
            return config.COLLECTOR_TOKEN_FILE.read_text().strip() or None
        except OSError:
            return None

    async def _resolve(self, host: str) -> str:
        """IPv4 for the collector, cached for 10 minutes (a `.local` lookup can take a moment)."""
        hit = self._addr.get(host)
        if hit and time.time() - hit[1] < 600:
            return hit[0] or ""
        addr = await asyncio.to_thread(ipv4, host)
        self._addr[host] = (addr, time.time())
        return addr or ""

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        token = self._token()
        if not token or not config.COLLECTOR_URL or self._client is None:
            raise HistoryError(NOT_SET_UP)
        try:
            r = await self._client.request(method, config.COLLECTOR_URL + path, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError:
            raise HistoryError(DOWN) from None
        if r.status_code == 401:
            raise HistoryError("The stats collector turned down Citadel's token: run tools/collector_setup.py again")
        if r.status_code >= 400:
            raise HistoryError(f"The stats collector said: {r.json().get('error', r.status_code) if r.headers.get('content-type', '').startswith('application/json') else r.status_code}")
        return r.json()

    async def _local_stats(self) -> dict[str, Any] | None:
        """Gaming's own reading, from the same script the other Linux machines run."""
        try:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-I", str(STATS_SCRIPT), stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
            return json.loads(out.decode().strip().splitlines()[-1])
        except (OSError, ValueError, IndexError, asyncio.TimeoutError):
            return None

    async def sweep(self) -> None:
        rows = [r for r in self._rows() if r["ssh_user"]]
        keep = {r["id"] for r in rows}
        self.state = {k: v for k, v in self.state.items() if k in keep}
        try:
            await self._send_machines(rows)
            local = await self._local_stats()
            if local is not None:
                await self._call("POST", f"/api/ingest/{THIS_ID}", json=local)
            latest = await self._call("GET", "/api/latest")
        except HistoryError as exc:
            self.collector.update(ok=False, error=str(exc))
            for s in [*self.state.values(), self.this]:
                s["error"] = str(exc)
            return
        self.collector.update(ok=True, error=None, at=int(time.time()))
        by_id = {m["id"]: m for m in latest.get("machines") or []}
        now = time.time()
        for r in rows:
            m = by_id.get(f"rh{r['id']}") or {}
            s = self.state.setdefault(r["id"], {"stats": None, "at": None, "error": None, "hot_since": None})
            s["error"] = m.get("error")
            if m.get("stats") is not None and m.get("at") != s["at"]:
                s["stats"], s["at"] = m["stats"], m["at"]
                hot = _hot(s["stats"], bool(s["stats"].get("pi")))
                s["hot_since"] = (s["hot_since"] or now) if hot else None
            if m.get("error") or self._asleep(r["id"]):
                s["hot_since"] = None
        t = by_id.get(THIS_ID) or {}
        self.this.update(stats=t.get("stats"), at=t.get("at"), error=t.get("error"))

    async def _send_machines(self, rows: list[dict[str, Any]]) -> None:
        items = [{"id": THIS_ID, "name": THIS_NAME, "push": True}]
        for r in rows:
            host = procs.vnc_port(r["address"])[0]
            items.append({"id": f"rh{r['id']}", "name": r["name"], "host": host, "addr": await self._resolve(host), "user": r["ssh_user"]})
        body = json.dumps(items, sort_keys=True)
        if self._sent and self._sent[0] == body and time.time() - self._sent[1] < RESEND:
            return
        await self._call("PUT", "/api/machines", json=items)
        self._sent = (body, time.time())

    def _asleep(self, host_id: int) -> bool:
        h = self.health.state.get(f"remote:{host_id}")
        return bool(h and not h["ok"])

    async def history(self, kind: str, machine: str, frm: int, to: int) -> dict[str, Any]:
        """The Stats page's history, from the collector: `series`, `disks` or `spikes` for one machine."""
        if kind not in HISTORY or not MACHINE_ID.match(machine) or frm >= to:
            raise HistoryError("That isn't a history Citadel can ask for")
        return await self._call("GET", f"/api/{kind}", params={"machine": machine, "from": frm, "to": to})

    def stats_machines(self) -> list[dict[str, Any]]:
        """The Stats page's machine chips: Gaming first, then the monitored Remote Access devices in their order."""
        out = [{"id": THIS_ID, "name": THIS_NAME, "up": True, "at": self.this.get("at"), "stats": self.this.get("stats"), "error": self.this.get("error")}]
        for v in self.view():
            out.append({**v, "id": f"rh{v['id']}"})
        for m in out:
            m["spec"] = spec_view(m["stats"])
        return out

    def view(self) -> list[dict[str, Any]]:
        """Every monitored machine in Remote Access order: on/asleep from the ping, the last stats and when they came."""
        out = []
        for r in self._rows():
            if not r["ssh_user"]:
                continue
            s = self.state.get(r["id"], {})
            h = self.health.state.get(f"remote:{r['id']}")
            out.append({"id": r["id"], "name": r["name"], "up": h["ok"] if h else None, "always_on": bool(r["always_on"]),
                        "stats": s.get("stats"), "at": s.get("at"), "error": s.get("error")})
        return out

    def alerts(self, at: datetime) -> list[dict[str, Any]]:
        """Reminders: a disk nearly full, running hot, an always-on machine off, Nasferatu's RAID not healthy."""
        if not alert_settings.settings().get("machines", True):
            return []
        t, day, out = at.timestamp(), at.date(), []
        for r in self._rows():
            name, h = r["name"], self.health.state.get(f"remote:{r['id']}")
            if r["always_on"] and h and not h["ok"] and h.get("down_since") and t - h["down_since"] >= DOWN_AFTER:
                since = datetime.fromtimestamp(h["down_since"], config.TZ)
                out.append({"kind": "machine", "key": f"mdown:{r['id']}:{h['down_since']}", "icon": "alert", "title": f"{name} isn't answering",
                            "detail": f"off since {since:%H:%M}" if since.date() == day else f"off since {since:%-d %b}", "chip": "Off", "level": "soon"})
            s = self.state.get(r["id"])
            stats = s and s.get("stats")
            if not stats or not r["ssh_user"]:
                continue
            for d in stats.get("disks") or []:
                if d.get("total") and (d["free"] < DISK_LOW_BYTES or d["free"] / d["total"] < DISK_LOW_FRACTION):
                    out.append({"kind": "machine", "key": f"mdisk:{r['id']}:{d['mount']}:{day}", "icon": "alert",
                                "title": f"{name}: {d['mount']} is nearly full",
                                "detail": f"{d['free'] / 1024**3:,.0f} GB free of {d['total'] / 1024**3:,.0f} GB", "chip": "Disk", "level": "soon"})
            raid = stats.get("raid")
            if raid and raid.get("health") not in (None, "Healthy"):
                out.append({"kind": "machine", "key": f"mraid:{r['id']}:{day}", "icon": "alert", "title": f"{name}: the RAID isn't healthy",
                            "detail": f"{raid.get('health')} · {raid.get('status') or ''}".strip(" ·"), "chip": "RAID", "level": "now"})
            if s.get("hot_since") and t - s["hot_since"] >= HOT_FOR:
                cpu, gpu = (stats.get("cpu") or {}).get("temp"), (stats.get("gpu") or {}).get("temp")
                parts = [f"CPU {cpu:.0f}°C" if cpu is not None else "", f"GPU {gpu:.0f}°C" if gpu is not None else ""]
                out.append({"kind": "machine", "key": f"mhot:{r['id']}:{day}", "icon": "alert", "title": f"{name} is running hot",
                            "detail": " · ".join(p for p in parts if p), "chip": "Hot", "level": "now"})
        return out
