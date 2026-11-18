#!/usr/bin/python3 -I
"""Citadel's stats collector: keeps the history of every machine's CPU, memory, temperatures and disks.

Runs on NZXT (always on), not on Gaming, so the history doesn't stop whenever Gaming sleeps. Citadel stays the place to
look at it: it tells the collector which machines to watch (its Remote Access devices with a monitoring SSH user) and
reads everything back through this small JSON API. See docs/tasks/stats-collector.md in the Citadel repo.

Every 30 s each machine is asked for the stats JSON that tools/machines/citadel-stats (or citadel-stats.ps1 on Windows)
prints, over SSH with the collector's own key, which each machine only accepts with a forced command: it can do nothing
but run that script. NZXT itself runs the script locally. Gaming has no sshd, so Citadel runs the script there and
pushes the result (`push` machines are never polled).

Nothing is buffered anywhere: while this is down, or a machine is off, there are simply no readings, and the charts show
a gap. Storage is three tiers, each updated as a sample arrives (no rollup job to fall behind):
every reading for 7 days, 5-minute sums/counts/maxima for 90 days, hourly ones for good. Disks are kept hourly, and the
top processes whenever CPU stays high (two readings in a row ≥ 60 %), for 30 days.

Python standard library only (NZXT has python3 and nothing else is assumed). One folder: this file, citadel-stats beside
it, and a user unit. Config in ~/.config/citadel-collector, data in ~/.local/share/citadel-collector (both overridable
with COLLECTOR_CONFIG_DIR / COLLECTOR_DATA_DIR, which the tests use).
"""
from __future__ import annotations

import hmac
import json
import math
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

VERSION = 1
HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("COLLECTOR_CONFIG_DIR") or Path.home() / ".config" / "citadel-collector")
DATA_DIR = Path(os.environ.get("COLLECTOR_DATA_DIR") or Path.home() / ".local" / "share" / "citadel-collector")
KEY = CONFIG_DIR / "machines_ed25519"
KNOWN_HOSTS = CONFIG_DIR / "known_hosts"
STATS_SCRIPT = HERE / "citadel-stats"

INTERVAL = 30
TIMEOUT = 15.0             # Windows PowerShell takes a few seconds to start, then samples for 1 s
PROBE_TIMEOUT = 2.0
KEEP_RAW = 7 * 86400
KEEP_5M = 90 * 86400
KEEP_SPIKES = 30 * 86400
SPIKE_CPU = 60.0
MAX_POINTS = 600           # per series, whatever the range: a year at the hourly tier is grouped into ~15 h buckets
MAX_BODY = 1 << 20

# The figures kept over time. Each is a percentage or °C, so averages and maxima both mean something.
METRICS = ("cpu", "mem", "swap", "cpu_temp", "gpu", "gpu_temp")
TIERS = (("agg5m", 300), ("agg1h", 3600))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def figures(stats: dict[str, Any]) -> dict[str, float | None]:
    """The METRICS out of one stats JSON."""
    def num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    cpu, mem, gpu = stats.get("cpu") or {}, stats.get("mem") or {}, stats.get("gpu") or {}
    total, swap_total = num(mem.get("total")), num(mem.get("swap_total"))
    used, swap_used = num(mem.get("used")), num(mem.get("swap_used"))
    return {
        "cpu": num(cpu.get("percent")),
        "mem": round(100 * used / total, 1) if total and used is not None else None,
        "swap": round(100 * swap_used / swap_total, 1) if swap_total and swap_used is not None else None,
        "cpu_temp": num(cpu.get("temp")),
        "gpu": num(gpu.get("percent")),
        "gpu_temp": num(gpu.get("temp")),
    }


# ---- storage ------------------------------------------------------------------------------------------------------

def _agg_cols() -> str:
    return ", ".join(f"{m}_s REAL NOT NULL DEFAULT 0, {m}_n INTEGER NOT NULL DEFAULT 0, {m}_x REAL" for m in METRICS)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS machines (id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
    addr TEXT NOT NULL DEFAULT '', user TEXT NOT NULL DEFAULT '', push INTEGER NOT NULL DEFAULT 0, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS latest (machine TEXT PRIMARY KEY, at INTEGER, checked INTEGER, stats TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS samples (machine TEXT NOT NULL, ts INTEGER NOT NULL, {", ".join(f"{m} REAL" for m in METRICS)},
    PRIMARY KEY (machine, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg5m (machine TEXT NOT NULL, ts INTEGER NOT NULL, n INTEGER NOT NULL, {_agg_cols()},
    PRIMARY KEY (machine, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg1h (machine TEXT NOT NULL, ts INTEGER NOT NULL, n INTEGER NOT NULL, {_agg_cols()},
    PRIMARY KEY (machine, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS disks (machine TEXT NOT NULL, ts INTEGER NOT NULL, mount TEXT NOT NULL, free INTEGER, total INTEGER,
    PRIMARY KEY (machine, ts, mount)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS spikes (machine TEXT NOT NULL, ts INTEGER NOT NULL, cpu REAL, top TEXT, PRIMARY KEY (machine, ts)) WITHOUT ROWID;
"""


def _agg_upsert(table: str) -> str:
    cols = ", ".join(f"{m}_s, {m}_n, {m}_x" for m in METRICS)
    marks = ", ".join("?" for _ in range(3 * len(METRICS) + 3))
    sets = ", ".join(
        f"{m}_s = {m}_s + excluded.{m}_s, {m}_n = {m}_n + excluded.{m}_n, "
        f"{m}_x = CASE WHEN excluded.{m}_x IS NULL THEN {m}_x WHEN {m}_x IS NULL OR excluded.{m}_x > {m}_x THEN excluded.{m}_x ELSE {m}_x END"
        for m in METRICS)
    return f"INSERT INTO {table} (machine, ts, n, {cols}) VALUES ({marks}) ON CONFLICT (machine, ts) DO UPDATE SET n = n + 1, {sets}"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.con() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)

    def con(self) -> sqlite3.Connection:
        """One short-lived connection per unit of work (the sweep and the HTTP threads each open their own)."""
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _run(self, fn):
        con = self.con()
        try:
            with con:
                return fn(con)
        finally:
            con.close()

    # machines ---------------------------------------------------------------------------------------------------------
    def set_machines(self, items: list[dict[str, Any]]) -> None:
        def go(con: sqlite3.Connection) -> None:
            con.execute("DELETE FROM machines")
            con.executemany("INSERT INTO machines (id, name, host, addr, user, push, sort) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [(m["id"], m["name"], m.get("host", ""), m.get("addr", ""), m.get("user", ""), int(bool(m.get("push"))), i)
                             for i, m in enumerate(items)])
        self._run(go)

    def machines(self) -> list[dict[str, Any]]:
        return self._run(lambda con: [dict(r) for r in con.execute("SELECT * FROM machines ORDER BY sort, id")])

    def machine(self, mid: str) -> dict[str, Any] | None:
        return self._run(lambda con: (lambda r: dict(r) if r else None)(con.execute("SELECT * FROM machines WHERE id = ?", (mid,)).fetchone()))

    # readings ---------------------------------------------------------------------------------------------------------
    def record(self, mid: str, stats: dict[str, Any], at: int, spike: bool = False) -> None:
        f = figures(stats)
        vals = [f[m] for m in METRICS]

        def go(con: sqlite3.Connection) -> None:
            con.execute(f"INSERT OR REPLACE INTO samples (machine, ts, {', '.join(METRICS)}) VALUES (?, ?, {', '.join('?' for _ in METRICS)})",
                        (mid, at, *vals))
            for table, res in TIERS:
                agg = [x for v in vals for x in ((v or 0.0), (1 if v is not None else 0), v)]
                con.execute(_agg_upsert(table), (mid, at - at % res, 1, *agg))
            hour = at - at % 3600
            for d in stats.get("disks") or []:
                if isinstance(d, dict) and d.get("mount"):
                    con.execute("INSERT OR REPLACE INTO disks (machine, ts, mount, free, total) VALUES (?, ?, ?, ?, ?)",
                                (mid, hour, str(d["mount"]), d.get("free"), d.get("total")))
            if spike and stats.get("top"):
                con.execute("INSERT OR REPLACE INTO spikes (machine, ts, cpu, top) VALUES (?, ?, ?, ?)",
                            (mid, at, f["cpu"], json.dumps(stats["top"], separators=(",", ":"))))
            con.execute("INSERT INTO latest (machine, at, checked, stats, error) VALUES (?, ?, ?, ?, NULL) "
                        "ON CONFLICT (machine) DO UPDATE SET at = excluded.at, checked = excluded.checked, stats = excluded.stats, error = NULL",
                        (mid, at, at, json.dumps(stats, separators=(",", ":"))))
        self._run(go)

    def failed(self, mid: str, error: str, at: int) -> None:
        self._run(lambda con: con.execute(
            "INSERT INTO latest (machine, checked, error) VALUES (?, ?, ?) ON CONFLICT (machine) DO UPDATE SET checked = excluded.checked, error = excluded.error",
            (mid, at, error)))

    def latest(self) -> list[dict[str, Any]]:
        def go(con: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = con.execute("SELECT m.id, m.name, m.push, l.at, l.checked, l.stats, l.error FROM machines m "
                               "LEFT JOIN latest l ON l.machine = m.id ORDER BY m.sort, m.id").fetchall()
            return [{"id": r["id"], "name": r["name"], "push": bool(r["push"]), "at": r["at"], "checked": r["checked"],
                     "stats": json.loads(r["stats"]) if r["stats"] else None, "error": r["error"]} for r in rows]
        return self._run(go)

    def prune(self, now: int) -> None:
        def go(con: sqlite3.Connection) -> None:
            con.execute("DELETE FROM samples WHERE ts < ?", (now - KEEP_RAW,))
            con.execute("DELETE FROM agg5m WHERE ts < ?", (now - KEEP_5M,))
            con.execute("DELETE FROM spikes WHERE ts < ?", (now - KEEP_SPIKES,))
        self._run(go)
        self._run(lambda con: con.execute("PRAGMA optimize"))

    # history ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def pick(frm: int, to: int, now: int) -> tuple[str, int, int]:
        """(table, its resolution, bucket) for a range: the finest tier that still holds all of it, grouped so no series
        has more than MAX_POINTS points."""
        span = max(1, to - frm)
        if span <= 2 * 86400 and frm >= now - KEEP_RAW:
            table, res = "samples", INTERVAL
        elif span <= 45 * 86400 and frm >= now - KEEP_5M:
            table, res = "agg5m", 300
        else:
            table, res = "agg1h", 3600
        return table, res, res * max(1, math.ceil(span / MAX_POINTS / res))

    def series(self, mid: str, frm: int, to: int, now: int) -> dict[str, Any]:
        table, res, b = self.pick(frm, to, now)
        if table == "samples":
            sel = ", ".join(f"AVG({m}), MAX({m})" for m in METRICS) + ", COUNT(*)"
        else:
            sel = ", ".join(f"SUM({m}_s) / NULLIF(SUM({m}_n), 0), MAX({m}_x)" for m in METRICS) + ", SUM(n)"
        rows = self._run(lambda con: con.execute(
            f"SELECT (ts / ?) * ? AS t, {sel} FROM {table} WHERE machine = ? AND ts >= ? AND ts < ? GROUP BY t ORDER BY t",
            (b, b, mid, frm, to)).fetchall())
        r1 = lambda v: None if v is None else round(v, 1)
        out: dict[str, Any] = {"machine": mid, "from": frm, "to": to, "res": res, "bucket": b,
                               "t": [r[0] for r in rows], "n": [r[-1] for r in rows], "metrics": {}}
        for i, m in enumerate(METRICS):
            out["metrics"][m] = {"avg": [r1(r[1 + 2 * i]) for r in rows], "max": [r1(r[2 + 2 * i]) for r in rows]}
        # a figure this machine never reports (no GPU, no sensor) is left out rather than sent as a row of nulls
        out["metrics"] = {m: v for m, v in out["metrics"].items() if any(x is not None for x in v["avg"])}
        return out

    def disks(self, mid: str, frm: int, to: int) -> dict[str, Any]:
        b = 3600 * max(1, math.ceil((to - frm) / MAX_POINTS / 3600))
        rows = self._run(lambda con: con.execute(
            "SELECT mount, (ts / ?) * ? AS t, MIN(free), MAX(total) FROM disks WHERE machine = ? AND ts >= ? AND ts < ? "
            "GROUP BY mount, t ORDER BY mount, t", (b, b, mid, frm, to)).fetchall())
        mounts: dict[str, dict[str, list]] = {}
        for mount, t, free, total in rows:
            m = mounts.setdefault(mount, {"t": [], "free": [], "total": []})
            m["t"].append(t), m["free"].append(free), m["total"].append(total)
        return {"machine": mid, "from": frm, "to": to, "bucket": b, "mounts": mounts}

    def spikes(self, mid: str, frm: int, to: int) -> list[dict[str, Any]]:
        """High-CPU moments, newest first: readings ≤ 2 intervals apart are one moment, its processes averaged over it."""
        rows = self._run(lambda con: con.execute("SELECT ts, cpu, top FROM spikes WHERE machine = ? AND ts >= ? AND ts < ? ORDER BY ts",
                                                 (mid, frm, to)).fetchall())
        out: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for ts, cpu, top in rows:
            if cur is None or ts - cur["end"] > 2 * INTERVAL + 5:
                cur = {"start": ts, "end": ts, "peak": cpu, "readings": 0, "_p": {}}
                out.append(cur)
            cur["end"], cur["readings"] = ts, cur["readings"] + 1
            cur["peak"] = max(cur["peak"] or 0, cpu or 0)
            for p in json.loads(top or "[]"):
                cur["_p"][p["name"]] = cur["_p"].get(p["name"], 0) + float(p.get("percent") or 0)
        for e in out:
            p = e.pop("_p")
            e["top"] = [{"name": k, "percent": round(v / e["readings"], 1)} for k, v in sorted(p.items(), key=lambda kv: -kv[1])[:5]]
        return out[::-1][:200]


# ---- asking the machines --------------------------------------------------------------------------------------------

def explain(stderr: str, code: int | None) -> str:
    """What went wrong, written for Mike to read on Citadel's Machines panel."""
    s = stderr.lower()
    if "remote host identification has changed" in s:
        return "Host key changed: if it was reinstalled, run tools/collector_setup.py again"
    if "host key verification failed" in s or "no matching host key" in s:
        return "The collector doesn't know its host key yet: run tools/collector_setup.py again"
    if "permission denied" in s:
        return "Not set up for monitoring (run tools/machines_setup.py)"
    if "no such identity" in s or "identity file" in s and "not accessible" in s:
        return "The collector's key is missing (run tools/collector_setup.py)"
    if "could not resolve" in s or "name or service not known" in s:
        return "Can't find it on the network"
    if code is None or "timed out" in s or "no route to host" in s or "connection refused" in s:
        return "No answer"
    return "Stats script failed" + (f": {stderr.strip().splitlines()[-1][:120]}" if stderr.strip() else "")


def is_local(addr: str) -> bool:
    """True for one of this machine's own addresses: only those can be bound to."""
    if not addr:
        return False
    try:
        with socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((addr, 0))
        return True
    except OSError:
        return False


def ssh_argv(m: dict[str, Any]) -> list[str]:
    """`-F /dev/null`: no ssh_config applies. HostKeyAlias: Citadel may send an address for a name (NZXT can't resolve
    `.local` to IPv4), and the host key is checked under the name, which is how collector_setup.py stored it."""
    host = m["host"]
    return ["ssh", "-F", "/dev/null", "-i", str(KEY), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", f"HostKeyAlias={host}",
            "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR", "-T", f"{m['user']}@{m.get('addr') or host}"]


def run(argv: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, explain("", None)
    except OSError as exc:
        return None, f"Couldn't run {argv[0]}: {exc}"
    if p.returncode != 0:
        return None, explain(p.stderr.decode(errors="replace"), p.returncode)
    try:
        stats = json.loads(p.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, "Stats script printed something unexpected"
    if not isinstance(stats, dict) or stats.get("v") != 1:
        return None, "Stats script is a different version: run tools/machines_setup.py again"
    return stats, None


def fetch(m: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(stats, None) or (None, what went wrong) for one machine."""
    addr = m.get("addr") or m["host"]
    if is_local(addr):
        return run([sys.executable, "-I", str(STATS_SCRIPT)])
    try:                                    # off or asleep: don't spend an SSH timeout finding out
        socket.create_connection((addr, 22), timeout=PROBE_TIMEOUT).close()
    except socket.gaierror:
        return None, "Can't find it on the network"
    except OSError:
        return None, "Off or asleep"
    return run(ssh_argv(m))


class Collector:
    def __init__(self, store: Store, fetcher=fetch) -> None:
        self.store = store
        self.fetcher = fetcher
        self.started = int(time.time())
        self.last_sweep: int | None = None
        self._high: dict[str, bool] = {}          # was the last reading ≥ SPIKE_CPU (two in a row make a spike)
        self._errors: dict[str, str | None] = {}
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fetch")
        self._stop = threading.Event()
        self._pruned = 0

    def accept(self, mid: str, stats: dict[str, Any], at: int) -> None:
        cpu = figures(stats)["cpu"]
        high = cpu is not None and cpu >= SPIKE_CPU
        spike = high and self._high.get(mid, False)
        self._high[mid] = high
        self.store.record(mid, stats, at, spike=spike)

    def sweep(self) -> None:
        due = [m for m in self.store.machines() if not m["push"] and m["user"]]
        results = list(self._pool.map(self.fetcher, due))
        now = int(time.time())
        for m, (stats, error) in zip(due, results):
            if stats is not None:
                self.accept(m["id"], stats, now)
            else:
                self._high[m["id"]] = False
                self.store.failed(m["id"], error or "No answer", now)
            if self._errors.get(m["id"]) != error:
                log(f"{m['name']}: {error or 'ok'}")
                self._errors[m["id"]] = error
        self.last_sweep = now
        if now - self._pruned >= 3600:
            self.store.prune(now)
            self._pruned = now

    def loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.sweep()
            except Exception as exc:          # keep sweeping; one bad reading mustn't stop the history
                log(f"sweep failed: {exc!r}")
            self._stop.wait(max(1.0, INTERVAL - (time.monotonic() - t0)))

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---- the API ------------------------------------------------------------------------------------------------------

def make_handler(collector: Collector, token: str):
    store = collector.store

    class Handler(BaseHTTPRequestHandler):
        server_version = f"citadel-collector/{VERSION}"

        def log_message(self, *args: Any) -> None:      # Citadel polls every 30 s: no access log in the journal
            pass

        def send(self, code: int, body: Any) -> None:
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            if token and hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self.send(401, {"error": "unauthorised"})
            return False

        def body(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("too big")
            return json.loads(self.rfile.read(n) or b"null")

        def range_args(self, q: dict[str, list[str]]) -> tuple[str, int, int, int]:
            now = int(time.time())
            mid = (q.get("machine") or [""])[0]
            to = int((q.get("to") or [now])[0])
            frm = int((q.get("from") or [to - 86400])[0])
            if not mid or frm >= to:
                raise ValueError("machine, from < to")
            return mid, frm, to, now

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            if url.path == "/api/health":
                return self.send(200, {"ok": True, "version": VERSION, "started": collector.started, "last_sweep": collector.last_sweep,
                                       "interval": INTERVAL})
            if not self.authed():
                return
            try:
                if url.path == "/api/latest":
                    return self.send(200, {"version": VERSION, "now": int(time.time()), "last_sweep": collector.last_sweep,
                                           "interval": INTERVAL, "machines": store.latest()})
                if url.path in ("/api/series", "/api/disks", "/api/spikes"):
                    mid, frm, to, now = self.range_args(q)
                    if url.path == "/api/series":
                        return self.send(200, store.series(mid, frm, to, now))
                    if url.path == "/api/disks":
                        return self.send(200, store.disks(mid, frm, to))
                    return self.send(200, {"machine": mid, "spikes": store.spikes(mid, frm, to)})
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            self.send(404, {"error": "not found"})

        def do_PUT(self) -> None:
            if not self.authed():
                return
            if urlsplit(self.path).path != "/api/machines":
                return self.send(404, {"error": "not found"})
            try:
                items = self.body()
                if not isinstance(items, list):
                    raise ValueError("a list")
                clean = []
                for m in items:
                    if not isinstance(m, dict) or not str(m.get("id") or "").strip() or not str(m.get("name") or "").strip():
                        raise ValueError("each machine needs an id and a name")
                    clean.append({"id": str(m["id"])[:40], "name": str(m["name"])[:60], "host": str(m.get("host") or "")[:255],
                                  "addr": str(m.get("addr") or "")[:64], "user": str(m.get("user") or "")[:64], "push": bool(m.get("push"))})
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            store.set_machines(clean)
            self.send(200, {"ok": True, "machines": len(clean)})

        def do_POST(self) -> None:
            if not self.authed():
                return
            path = urlsplit(self.path).path
            if not path.startswith("/api/ingest/"):
                return self.send(404, {"error": "not found"})
            mid = path.rsplit("/", 1)[1]
            m = store.machine(mid)
            if not m or not m["push"]:
                return self.send(404, {"error": "no such pushed machine"})
            try:
                stats = self.body()
                if not isinstance(stats, dict) or stats.get("v") != 1:
                    raise ValueError("stats JSON, v 1")
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            collector.accept(mid, stats, int(time.time()))
            self.send(200, {"ok": True})

    return Handler


def load_config() -> dict[str, Any]:
    cfg = json.loads((CONFIG_DIR / "config.json").read_text())
    if not cfg.get("token"):
        raise SystemExit("config.json has no token: run tools/collector_setup.py from Gaming")
    return cfg


def main() -> int:
    cfg = load_config()
    store = Store(DATA_DIR / "metrics.sqlite")
    collector = Collector(store)
    server = ThreadingHTTPServer((cfg.get("bind", "0.0.0.0"), int(cfg.get("port", 8680))), make_handler(collector, cfg["token"]))
    server.daemon_threads = True
    sweeper = threading.Thread(target=collector.loop, name="sweep", daemon=True)
    sweeper.start()

    def bye(*_: Any) -> None:
        collector.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    log(f"citadel-collector {VERSION} on {cfg.get('bind', '0.0.0.0')}:{cfg.get('port', 8680)}, data in {DATA_DIR}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
