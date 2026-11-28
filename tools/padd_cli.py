#!/usr/bin/env python3
"""Citadel PADD terminal helper (Python 3 stdlib only — works on Gaming, Runabout, NZXT, Phoenix).

Usage:
  padd login <enrol-url>              Enrol this terminal as a Citadel owner device
  padd "https://example.com"          Drop a link or text snippet
  padd --code --title "Logs" "text"   Drop as code with an optional label
  echo "hello" | padd                 Pipe stdin straight into Dropzone
  padd photo.png report.pdf           Upload one or more files (up to 500 MB each)
  padd ls [-n 15]                     List recent Dropzone items
  padd get [index|id] [-o DIR]        Print latest text/link to stdout, or download a file
  padd note "Title" ["Body"]          Create a PADD note (reads stdin if Body omitted)
  padd shop "Milk" ["Eggs"]           Add item(s) to Home Assistant Shopping List
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import mimetypes
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "citadel-padd"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _die(msg: str, code: int = 1) -> None:
    print(f"padd: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_config() -> dict[str, str]:
    if not CONFIG_FILE.is_file():
        _die(f"not logged in yet. Run: padd login <enrol-link-from-Settings-Screens>")
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _die(f"couldn't read {CONFIG_FILE}: {exc}")
    if not cfg.get("url") or not cfg.get("cookie"):
        _die(f"incomplete config in {CONFIG_FILE}; re-run padd login <enrol-link>")
    return cfg


def save_config(url: str, cookie: str, device_name: str = "") -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    CONFIG_FILE.write_text(
        json.dumps({"url": url.rstrip("/"), "cookie": cookie, "device": device_name}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(CONFIG_FILE, 0o600)


def api_request(
    cfg: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    raw_headers: dict[str, str] | None = None,
    raw_data: bytes | Any = None,
) -> Any:
    url = f"{cfg['url'].rstrip('/')}{path}"
    headers = {
        "Cookie": f"hub_device={cfg['cookie']}",
        "X-Hub": "1",
        "User-Agent": f"citadel-padd-cli/1.0 ({socket.gethostname()})",
    }
    data = raw_data
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if raw_headers:
        headers.update(raw_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if "json" in ctype else raw
    except urllib.error.HTTPError as exc:
        detail = exc.reason
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("detail") or detail
        except Exception:
            pass
        _die(f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        _die(f"couldn't reach {cfg['url']}: {exc.reason}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cmd_login(enrol_url: str) -> None:
    parts = urllib.parse.urlsplit(enrol_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc or "/enrol/" not in parts.path:
        _die("expected a Citadel enrol URL like http://192.168.0.66:8080/enrol/<token>")
    base = f"{parts.scheme}://{parts.netloc}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)
    req = urllib.request.Request(
        enrol_url.strip(),
        data=b"",
        headers={"User-Agent": f"citadel-padd-cli/1.0 ({socket.gethostname()})"},
        method="POST",
    )
    try:
        opener.open(req, timeout=20)
    except urllib.error.HTTPError as exc:
        if exc.code not in (302, 303):
            _die(f"enrolment failed (HTTP {exc.code}) — link may be expired or already used")
    except urllib.error.URLError as exc:
        _die(f"couldn't reach {base}: {exc.reason}")

    cookie_val = None
    for c in jar:
        if c.name == "hub_device":
            cookie_val = c.value
            break
    if not cookie_val:
        _die("server didn't return an owner cookie — check that the enrol link is still valid")
    save_config(base, cookie_val, f"{socket.gethostname()} CLI")
    print(f"Enrolled against {base} (saved to {CONFIG_FILE})")


class _FileStream:
    """Iterable wrapper that prints progress while streaming a file upload."""
    def __init__(self, path: Path, show_progress: bool = True):
        self.path = path
        self.total = path.stat().st_size
        self.sent = 0
        self.show = show_progress and sys.stderr.isatty() and self.total > 256 * 1024

    def __len__(self) -> int:
        return self.total

    def __iter__(self):
        with open(self.path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.sent += len(chunk)
                if self.show:
                    pct = int(self.sent * 100 / self.total) if self.total else 100
                    print(f"\rUploading {self.path.name}: {pct}%", end="", file=sys.stderr, flush=True)
                yield chunk
        if self.show:
            print("", file=sys.stderr)


def upload_file(cfg: dict[str, str], path: Path, *, caption: str = "", pinned: bool = False) -> dict[str, Any]:
    if not path.is_file():
        _die(f"file not found: {path}")
    mime, _ = mimetypes.guess_type(path.name)
    headers = {
        "Content-Type": mime or "application/octet-stream",
        "Content-Length": str(path.stat().st_size),
        "X-Filename": urllib.parse.quote(path.name),
        "X-Device-Name": urllib.parse.quote(cfg.get("device") or f"{socket.gethostname()} CLI"),
    }
    if caption:
        headers["X-Caption"] = urllib.parse.quote(caption)
    if pinned:
        headers["X-Pinned"] = "1"
    return api_request(cfg, "POST", "/api/owner/padd/drops/file", raw_headers=headers, raw_data=_FileStream(path))


def _fmt_bytes(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1000 or unit == "GB":
            return f"{int(round(val))} {unit}" if unit == "B" or val >= 100 else f"{val:.1f} {unit}"
        val /= 1000
    return f"{n} B"


def cmd_ls(cfg: dict[str, str], limit: int) -> None:
    res = api_request(cfg, "GET", f"/api/owner/padd/drops?limit={limit}")
    drops = res.get("drops") or []
    if not drops:
        print("Dropzone is empty.")
        return
    for idx, d in enumerate(drops, 1):
        pin = "*" if d.get("pinned") else " "
        kind = d.get("kind", "text")[:5].ljust(5)
        dev = (d.get("device_name") or "")[:14].ljust(14)
        if d.get("file_name"):
            summary = f"{d['file_name']} ({_fmt_bytes(int(d.get('size_bytes') or 0))})"
        elif d.get("url"):
            summary = f"{d.get('title') or d.get('url_host') or ''} -> {d['url']}".strip(" ->")
        else:
            first = (d.get("body") or "").splitlines()[0] if d.get("body") else ""
            summary = first[:72] + ("…" if len(first) > 72 else "")
        print(f"{idx:>2}{pin} [{kind}] {dev}  {summary}  ({d['id'][:8]})")


def cmd_get(cfg: dict[str, str], ref: str, out_dir: str | None) -> None:
    res = api_request(cfg, "GET", "/api/owner/padd/drops?limit=100")
    drops = res.get("drops") or []
    if not drops:
        _die("Dropzone is empty.")
    target = None
    if not ref:
        target = drops[0]
    elif ref.isdigit() and 1 <= int(ref) <= len(drops):
        target = drops[int(ref) - 1]
    else:
        for d in drops:
            if d["id"].startswith(ref):
                target = d
                break
    if not target:
        _die(f"no drop matching '{ref}'")

    if target.get("stored_name"):
        data = api_request(cfg, "GET", f"/api/owner/padd/files/{target['id']}")
        dest_dir = Path(out_dir or ".")
        dest_dir.mkdir(parents=True, exist_ok=True)
        fname = Path(target.get("file_name") or f"drop-{target['id']}").name
        dest = dest_dir / fname
        dest.write_bytes(data)
        print(f"Saved {dest} ({_fmt_bytes(len(data))})", file=sys.stderr)
    elif target.get("kind") == "link" and target.get("url"):
        sys.stdout.write(target["url"])
        if sys.stdout.isatty():
            sys.stdout.write("\n")
    else:
        sys.stdout.write(target.get("body") or "")
        if sys.stdout.isatty() and not (target.get("body") or "").endswith("\n"):
            sys.stdout.write("\n")


def cmd_note(cfg: dict[str, str], title: str, body: str | None, tags: list[str], checklist: bool) -> None:
    if body is None:
        if not sys.stdin.isatty():
            body = sys.stdin.read()
        else:
            body = ""
    res = api_request(cfg, "POST", "/api/owner/padd/notes", {
        "title": title,
        "body": body,
        "kind": "checklist" if checklist else "note",
        "tags": tags,
    })
    note = res.get("note") or {}
    print(f"Saved note '{note.get('title', title)}' ({note.get('id', '')[:8]})")


def cmd_shop(cfg: dict[str, str], items: list[str], list_id: str | None) -> None:
    data = api_request(cfg, "GET", "/api/owner/padd/lists")
    if not data.get("available"):
        _die(data.get("error") or "Home Assistant lists unavailable")
    lists = data.get("lists") or []
    if not lists:
        _die("no Home Assistant todo.* lists found")
    eid = list_id or lists[0]["entity_id"]
    for item in items:
        api_request(cfg, "POST", f"/api/owner/padd/lists/{urllib.parse.quote(eid)}/items", {"summary": item})
        print(f"Added '{item}' to {eid}")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("login", "ls", "get", "note", "shop"):
        sub = argv[0]
        if sub == "login":
            p = argparse.ArgumentParser(prog="padd login")
            p.add_argument("url", help="One-time Citadel enrol URL from Settings → Screens")
            a = p.parse_args(argv[1:])
            cmd_login(a.url)
            return
        cfg = load_config()
        if sub == "ls":
            p = argparse.ArgumentParser(prog="padd ls")
            p.add_argument("-n", "--limit", type=int, default=15)
            a = p.parse_args(argv[1:])
            cmd_ls(cfg, a.limit)
            return
        if sub == "get":
            p = argparse.ArgumentParser(prog="padd get")
            p.add_argument("ref", nargs="?", default="", help="Index from 'padd ls' (default 1) or drop id prefix")
            p.add_argument("-o", "--out", default=None, help="Output directory for file drops")
            a = p.parse_args(argv[1:])
            cmd_get(cfg, a.ref, a.out)
            return
        if sub == "note":
            p = argparse.ArgumentParser(prog="padd note")
            p.add_argument("title")
            p.add_argument("body", nargs="?", default=None)
            p.add_argument("-t", "--tag", action="append", default=[])
            p.add_argument("--checklist", action="store_true")
            a = p.parse_args(argv[1:])
            cmd_note(cfg, a.title, a.body, a.tag, a.checklist)
            return
        if sub == "shop":
            p = argparse.ArgumentParser(prog="padd shop")
            p.add_argument("items", nargs="+")
            p.add_argument("--list", dest="list_id", default=None, help="HA todo.* entity id")
            a = p.parse_args(argv[1:])
            cmd_shop(cfg, a.items, a.list_id)
            return

    # Default mode: drop text, piped stdin, or file(s)
    p = argparse.ArgumentParser(prog="padd", description="Drop links, text, or files into Citadel PADD.")
    p.add_argument("items", nargs="*", help="Text, URL, or file path(s) to drop")
    p.add_argument("-f", "--file", action="append", default=[], help="Explicit file path to upload")
    p.add_argument("-t", "--title", default="", help="Optional title or file caption")
    p.add_argument("--code", action="store_true", help="Mark drop as code snippet")
    p.add_argument("--pin", action="store_true", help="Pin the dropped item")
    a = p.parse_args(argv)
    cfg = load_config()

    explicit_files = [Path(x) for x in a.file]
    if not explicit_files and a.items and all(Path(x).is_file() for x in a.items):
        explicit_files = [Path(x) for x in a.items]

    if explicit_files:
        for path in explicit_files:
            res = upload_file(cfg, path, caption=a.title, pinned=a.pin)
            d = res.get("drop") or {}
            print(f"Uploaded {d.get('file_name', path.name)} ({_fmt_bytes(int(d.get('size_bytes') or 0))}) [{d.get('id', '')[:8]}]")
        return

    text = " ".join(a.items)
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text.strip():
        p.print_help()
        raise SystemExit(2)

    res = api_request(cfg, "POST", "/api/owner/padd/drops", {
        "text": text,
        "title": a.title,
        "kind": "code" if a.code else "auto",
        "pinned": a.pin,
        "fetch_preview": True,
        "device_name": cfg.get("device") or f"{socket.gethostname()} CLI",
    })
    d = res.get("drop") or {}
    print(f"Dropped {d.get('kind', 'text')} [{d.get('id', '')[:8]}]")


if __name__ == "__main__":
    main()
