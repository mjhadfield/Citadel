"""PADD: cross-device Dropzone, persistent Markdown Notes & Checklists, and Home Assistant Todo sync.

Files uploaded to Dropzone stream directly to `data/padd/` on disk (never stored as SQLite BLOBs) so nightly
database backups stay small and fast. Caps are 500 MB per file and 5 GB total across Dropzone, kept until
manually cleared (Mike, 2026-11-28).

Downloads and QR shares use short-lived HMAC-signed URLs bound to a per-drop revocable `share_nonce`, which
lets CitadelAndroid hand off downloads to the system browser without an APK rebuild and lets unenrolled LAN
machines (such as a work laptop) grab a single dropped item safely without exposing an owner cookie.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from . import __version__, config, db, homeassistant, notify

log = logging.getLogger("hub.padd")

MAX_FILE_BYTES = 500_000_000                  # 500 MB per file (Mike, 2026-11-28; decimal to match util.js bytes())
MAX_TOTAL_BYTES = 5_000_000_000               # 5 GB total Dropzone storage cap
MAX_TEXT_BYTES = 256 * 1024                   # 256 KB max for text/code drops and notes
MAX_NOTE_TITLE = 120
MAX_DROP_TITLE = 160
MAX_TAGS = 8
MAX_TAG_LEN = 24
ZEBRALAB_MAX_BYTES = 40 * 1024 * 1024         # ZebraLab enforces a 40 MB upload cap
ZEBRALAB_DOC_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ZEBRALAB_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

DROP_KINDS = ("text", "link", "code", "image", "file")
NOTE_KINDS = ("note", "checklist")
INLINE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"})
SAFE_EXT_RE = re.compile(r"^\.[a-z0-9]{1,12}$")
STORED_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}(\.[a-z0-9]{1,12})?$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
TODO_ENTITY_RE = re.compile(r"^todo\.[a-z0-9_]{1,80}$")
CHECKBOX_LINE_RE = re.compile(r"^(\s*[-*]\s+)\[([ xX])\](\s.*)$")

# Tailscale / CGNAT range (RFC 6598) blocked alongside standard private/loopback/link-local ranges for SSRF safety.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

# Swappable transports for unit tests (never hit the real internet or live ZebraLab in pytest).
PREVIEW_TRANSPORT: httpx.AsyncBaseTransport | None = None
ZEBRALAB_TRANSPORT: httpx.AsyncBaseTransport | None = None

_PREVIEW_MAX_BYTES = 256 * 1024
_PREVIEW_TIMEOUT = 5.0
_PREVIEW_CACHE_TTL = 3600
_preview_cache: dict[str, tuple[float, dict[str, str]]] = {}

_CODE_LINE_RE = re.compile(
    r"^(?:```|#!/|\$\s+\S|sudo\s+|systemctl\s+|journalctl\s+|docker\s+|git\s+|ssh\s+|scp\s+|rsync\s+|curl\s+|wget\s+|"
    r"SELECT\s+.+\s+FROM\b|INSERT\s+INTO\b|UPDATE\s+\S+\s+SET\b|CREATE\s+TABLE\b|"
    r"from\s+\S+\s+import\s+|import\s+[A-Za-z_][\w.]*|def\s+\w+\s*\(|class\s+\w+[\s(:]|"
    r"(?:const|let|var|function|export)\s+\w+|Traceback \(most recent call last\):)",
    re.MULTILINE,
)


class PaddError(Exception):
    """Plain-English error for Mike to read, paired with an HTTP status code."""
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class PaddListsState:
    """Known Home Assistant todo.* entities discovered on this server instance (kept on app.state)."""
    def __init__(self) -> None:
        self.known: set[str] = set()


# ---- storage helpers ----------------------------------------------------------------------------

def padd_dir() -> Path:
    d = config.DATA_DIR / "padd"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def safe_file_path(stored_name: str | None) -> Path | None:
    """Resolve a stored drop filename safely inside data/padd/, returning None if invalid or missing."""
    if not stored_name or not STORED_NAME_RE.fullmatch(stored_name):
        return None
    root = padd_dir().resolve()
    path = (root / stored_name).resolve()
    if not path.is_relative_to(root):
        return None
    return path


def sanitize_filename(raw: str | None, fallback: str = "file") -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip().strip(".")
    if not name:
        return fallback
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 12:
            name = f"{stem[:180 - len(ext) - 1]}.{ext}"
        else:
            name = name[:180]
    return name


def safe_ext(filename: str, mime: str = "") -> str:
    _, dot, ext = filename.rpartition(".")
    cand = f".{ext.lower()}" if dot else ""
    if cand and SAFE_EXT_RE.fullmatch(cand):
        return cand
    guessed = mimetypes.guess_extension(mime or "") or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    return guessed.lower() if SAFE_EXT_RE.fullmatch(guessed.lower()) else ""


def sniff_mime(head: bytes, filename: str, header_mime: str = "") -> tuple[str, str]:
    """Return (kind, mime_type) using magic bytes first so screenshots/photos are accurately typed."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head.startswith(b"BM") and filename.lower().endswith(".bmp"):
        return "image", "image/bmp"
    if head.startswith(b"%PDF-"):
        return "file", "application/pdf"

    clean_header = (header_mime or "").split(";")[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename)
    mime = clean_header if clean_header and clean_header != "application/octet-stream" else (guessed or "application/octet-stream")
    if mime in INLINE_IMAGE_MIMES:
        # Don't trust a claimed raster image header if its magic bytes didn't match — serve as generic file.
        mime = "application/octet-stream"
    return "file", mime


def storage_stats() -> dict[str, int]:
    with db.db() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS used, COUNT(*) AS total, "
            "SUM(CASE WHEN stored_name IS NOT NULL THEN 1 ELSE 0 END) AS files, "
            "SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) AS pinned "
            "FROM padd_drops"
        ).fetchone()
    return {
        "used_bytes": int(row["used"] or 0),
        "cap_bytes": MAX_TOTAL_BYTES,
        "max_file_bytes": MAX_FILE_BYTES,
        "file_count": int(row["files"] or 0),
        "total_count": int(row["total"] or 0),
        "pinned_count": int(row["pinned"] or 0),
    }


def prune_orphan_temps(older_than_seconds: int = 3600) -> int:
    """Remove abandoned .tmp upload fragments from data/padd/ without touching real drops."""
    root = config.DATA_DIR / "padd"
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for entry in root.glob("*.tmp"):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed


# ---- Dropzone CRUD ------------------------------------------------------------------------------

def _is_single_url(text: str) -> tuple[bool, str, str]:
    s = text.strip()
    if not s or any(ch.isspace() for ch in s):
        return False, "", ""
    parts = urlsplit(s)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, "", ""
    try:
        httpx.URL(s)
    except httpx.InvalidURL:
        return False, "", ""
    host = (parts.hostname or parts.netloc).lower()
    return True, s, host


def classify_text(text: str, explicit_kind: str | None = None) -> tuple[str, str | None, str]:
    """Classify text into (kind, url, url_host)."""
    stripped = text.strip()
    is_url, clean_url, host = _is_single_url(stripped)
    if explicit_kind == "link":
        if not is_url:
            raise PaddError("Link drops must be a full http:// or https:// address.")
        return "link", clean_url, host
    if explicit_kind in ("text", "code"):
        return explicit_kind, None, ""
    if is_url:
        return "link", clean_url, host
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            json.loads(stripped)
            return "code", None, ""
        except ValueError:
            pass
    lines = stripped.splitlines()
    if _CODE_LINE_RE.search(stripped):
        return "code", None, ""
    if len(lines) >= 3:
        indented = sum(1 for ln in lines if ln.startswith(("  ", "\t")) or ln.rstrip().endswith((";", "{", "}")))
        if indented >= 2:
            return "code", None, ""
    return "text", None, ""


def _drop_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["pinned"] = bool(d["pinned"])
    ext = Path(d["file_name"] or "").suffix.lower() if d.get("file_name") else ""
    has_file = bool(d.get("stored_name"))
    d["can_inline"] = has_file and d.get("mime_type") in INLINE_IMAGE_MIMES
    d["can_zebralab_doc"] = has_file and ext in ZEBRALAB_DOC_EXTS and (d.get("size_bytes") or 0) <= ZEBRALAB_MAX_BYTES
    d["can_zebralab_asset"] = has_file and ext in ZEBRALAB_IMG_EXTS and (d.get("size_bytes") or 0) <= ZEBRALAB_MAX_BYTES
    d.pop("share_nonce", None)
    return d


def list_drops(kind: str | None = None, q: str = "", pinned_only: bool = False, limit: int = 120) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if kind and kind != "all":
        if kind == "text":
            clauses.append("kind IN ('text', 'code')")
        elif kind in DROP_KINDS:
            clauses.append("kind = ?")
            params.append(kind)
    if pinned_only:
        clauses.append("pinned = 1")
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        clauses.append("(title LIKE ? OR body LIKE ? OR url LIKE ? OR url_description LIKE ? OR file_name LIKE ? OR device_name LIKE ?)")
        params.extend([like] * 6)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    lim = max(1, min(int(limit or 120), 500))
    params.append(lim)
    with db.db() as con:
        rows = con.execute(
            f"SELECT * FROM padd_drops {where} ORDER BY pinned DESC, created_at DESC, rowid DESC LIMIT ?",
            params,
        ).fetchall()
    return [_drop_row(r) for r in rows]


def get_drop(drop_id: str, *, include_nonce: bool = False) -> dict[str, Any] | None:
    with db.db() as con:
        row = con.execute("SELECT * FROM padd_drops WHERE id = ?", (drop_id,)).fetchone()
    if not row:
        return None
    if include_nonce:
        d = dict(row)
        d["pinned"] = bool(d["pinned"])
        return d
    return _drop_row(row)


def create_text_drop(
    text: str,
    *,
    title: str = "",
    kind: str | None = None,
    pinned: bool = False,
    device_id: str | None = None,
    device_name: str = "",
) -> dict[str, Any]:
    raw = text if kind == "code" else text.strip()
    if not raw or not raw.strip():
        raise PaddError("Nothing to drop — paste a link, text, or snippet first.")
    if len(raw.encode("utf-8")) > MAX_TEXT_BYTES:
        raise PaddError(f"Text drops are limited to {MAX_TEXT_BYTES // 1024} KB — upload a file instead.", 413)
    resolved_kind, url, url_host = classify_text(raw, kind)
    clean_title = (title or "").strip()[:MAX_DROP_TITLE]
    drop_id = secrets.token_hex(8)
    now = int(time.time())
    nonce = secrets.token_hex(8)
    with db.db() as con:
        con.execute(
            """INSERT INTO padd_drops
               (id, kind, title, body, url, url_host, url_description, file_name, stored_name,
                mime_type, size_bytes, sha256, device_id, device_name, pinned, share_nonce, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, '', NULL, NULL, '', 0, '', ?, ?, ?, ?, ?, ?)""",
            (drop_id, resolved_kind, clean_title, raw, url, url_host, device_id, (device_name or "")[:60],
             1 if pinned else 0, nonce, now, now),
        )
        row = con.execute("SELECT * FROM padd_drops WHERE id = ?", (drop_id,)).fetchone()
    return _drop_row(row)


async def save_streamed_file(
    chunks: AsyncIterator[bytes],
    *,
    filename: str,
    content_type: str = "",
    caption: str = "",
    pinned: bool = False,
    device_id: str | None = None,
    device_name: str = "",
) -> dict[str, Any]:
    """Stream an uploaded file to data/padd/ in chunks while hashing and enforcing per-file & total caps."""
    clean_name = sanitize_filename(filename, fallback="upload.bin")
    used = storage_stats()["used_bytes"]
    remaining = MAX_TOTAL_BYTES - used
    if remaining <= 0:
        raise PaddError("Dropzone storage is full (5 GB cap). Clear some files first.", 507)

    drop_id = secrets.token_hex(8)
    root = padd_dir()
    tmp_path = root / f"{drop_id}.tmp"
    hasher = hashlib.sha256()
    head = bytearray()
    size = 0
    try:
        with open(tmp_path, "wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise PaddError(f"Files up to {MAX_FILE_BYTES // 1_000_000} MB, please.", 413)
                if size > remaining:
                    raise PaddError("This file would pass the 5 GB Dropzone cap. Clear older files first.", 507)
                if len(head) < 64:
                    head.extend(chunk[:64 - len(head)])
                hasher.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise PaddError("That file is empty.", 422)
        kind, mime = sniff_mime(bytes(head), clean_name, content_type)
        ext = safe_ext(clean_name, mime)
        stored_name = f"{drop_id}{ext}"
        final_path = root / stored_name
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    now = int(time.time())
    nonce = secrets.token_hex(8)
    clean_caption = (caption or "").strip()[:2000]
    try:
        with db.db() as con:
            con.execute(
                """INSERT INTO padd_drops
                   (id, kind, title, body, url, url_host, url_description, file_name, stored_name,
                    mime_type, size_bytes, sha256, device_id, device_name, pinned, share_nonce, created_at, updated_at)
                   VALUES (?, ?, ?, ?, NULL, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (drop_id, kind, clean_name[:MAX_DROP_TITLE], clean_caption, clean_name, stored_name,
                 mime, size, hasher.hexdigest(), device_id, (device_name or "")[:60],
                 1 if pinned else 0, nonce, now, now),
            )
            row = con.execute("SELECT * FROM padd_drops WHERE id = ?", (drop_id,)).fetchone()
    except Exception:
        (root / stored_name).unlink(missing_ok=True)
        raise
    return _drop_row(row)


def update_drop(
    drop_id: str,
    *,
    pinned: bool | None = None,
    title: str | None = None,
    body: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    existing = get_drop(drop_id)
    if not existing:
        raise PaddError("Drop not found.", 404)
    new_pinned = existing["pinned"] if pinned is None else bool(pinned)
    new_title = existing["title"] if title is None else title.strip()[:MAX_DROP_TITLE]
    new_body = existing["body"] if body is None else body
    new_kind = existing["kind"]
    new_url = existing["url"]
    new_host = existing["url_host"]

    if existing["kind"] in ("text", "link", "code"):
        if len(new_body.encode("utf-8")) > MAX_TEXT_BYTES:
            raise PaddError(f"Text drops are limited to {MAX_TEXT_BYTES // 1024} KB.", 413)
        if kind in ("text", "code", "link"):
            new_kind, new_url, new_host = classify_text(new_body, kind)
        elif body is not None:
            new_kind, new_url, new_host = classify_text(new_body, existing["kind"] if existing["kind"] == "code" else None)

    now = int(time.time())
    with db.db() as con:
        con.execute(
            "UPDATE padd_drops SET pinned = ?, title = ?, body = ?, kind = ?, url = ?, url_host = ?, updated_at = ? WHERE id = ?",
            (1 if new_pinned else 0, new_title, new_body, new_kind, new_url, new_host, now, drop_id),
        )
        row = con.execute("SELECT * FROM padd_drops WHERE id = ?", (drop_id,)).fetchone()
    return _drop_row(row)


def delete_drop(drop_id: str) -> bool:
    with db.db() as con:
        row = con.execute("SELECT stored_name FROM padd_drops WHERE id = ?", (drop_id,)).fetchone()
        if not row:
            return False
        con.execute("DELETE FROM padd_drops WHERE id = ?", (drop_id,))
    path = safe_file_path(row["stored_name"])
    if path and path.is_file():
        path.unlink(missing_ok=True)
    return True


def clear_drops(include_pinned: bool = False) -> dict[str, int]:
    where = "" if include_pinned else "WHERE pinned = 0"
    with db.db() as con:
        rows = con.execute(f"SELECT id, stored_name, size_bytes FROM padd_drops {where}").fetchall()
        con.execute(f"DELETE FROM padd_drops {where}")
    freed = 0
    for r in rows:
        freed += int(r["size_bytes"] or 0)
        path = safe_file_path(r["stored_name"])
        if path and path.is_file():
            path.unlink(missing_ok=True)
    return {"removed": len(rows), "freed_bytes": freed}


def promote_drop_to_note(drop_id: str, device_name: str = "") -> dict[str, Any]:
    d = get_drop(drop_id)
    if not d:
        raise PaddError("Drop not found.", 404)
    title = d["title"] or d["url_host"] or (d["body"].splitlines()[0][:60] if d["body"] else "Saved drop")
    if d["kind"] == "link" and d["url"]:
        parts = [f"[{title}]({d['url']})"]
        if d.get("url_description"):
            parts.append(d["url_description"])
        body = "\n\n".join(parts)
    elif d["kind"] == "code":
        body = f"```\n{d['body']}\n```"
    elif d["kind"] in ("image", "file"):
        body = f"Dropped file: `{d['file_name']}`" + (f"\n\n{d['body']}" if d["body"] else "")
    else:
        body = d["body"]
    return create_note(title=title[:MAX_NOTE_TITLE], body=body, kind="note", tags=["drop"], device_name=device_name or d["device_name"])


# ---- Signed download & QR share tickets ---------------------------------------------------------

def _ticket_sig(drop_id: str, exp: int, share_nonce: str) -> str:
    msg = f"padd:{drop_id}:{exp}:{share_nonce}".encode("utf-8")
    return hmac.new(config.secret_key(), msg, hashlib.sha256).hexdigest()[:32]


def make_ticket(drop_id: str, *, ttl: int = 120) -> dict[str, Any]:
    d = get_drop(drop_id, include_nonce=True)
    if not d:
        raise PaddError("Drop not found.", 404)
    ttl = max(30, min(int(ttl), 86400))
    exp = int(time.time()) + ttl
    sig = _ticket_sig(drop_id, exp, d["share_nonce"])
    return {
        "drop_id": drop_id,
        "exp": exp,
        "ttl": ttl,
        "sig": sig,
        "download_path": f"/padd/dl/{drop_id}/{exp}/{sig}" if d.get("stored_name") else None,
        "share_path": f"/padd/share/{drop_id}/{exp}/{sig}",
    }


def verify_ticket(drop_id: str, exp: int, sig: str) -> dict[str, Any] | None:
    if exp < int(time.time()):
        return None
    d = get_drop(drop_id, include_nonce=True)
    if not d:
        return None
    expected = _ticket_sig(drop_id, exp, d["share_nonce"])
    if not hmac.compare_digest(sig or "", expected):
        return None
    return d


def revoke_share(drop_id: str) -> bool:
    nonce = secrets.token_hex(8)
    with db.db() as con:
        return con.execute(
            "UPDATE padd_drops SET share_nonce = ?, updated_at = ? WHERE id = ?",
            (nonce, int(time.time()), drop_id),
        ).rowcount > 0


def qr_svg(url: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


# ---- Polite, SSRF-safe URL metadata previews ----------------------------------------------------

def _is_forbidden_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return True
    if ip.version == 4 and ip in _CGNAT_NET:
        return True
    if ip.version == 6 and ip.ipv4_mapped:
        return _is_forbidden_ip(str(ip.ipv4_mapped))
    return False


async def _check_url_safe(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.strip().lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".lan", ".internal", ".ts.net")):
        return False
    # If hostname is an IP literal, check directly without DNS.
    try:
        ipaddress.ip_address(host)
        return not _is_forbidden_ip(host)
    except ValueError:
        pass
    # Under a test fake transport, avoid real DNS lookups for synthetic hostnames.
    if PREVIEW_TRANSPORT is not None:
        return True
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    return all(not _is_forbidden_ip(info[4][0]) for info in infos)


def _extract_meta(raw_html: bytes) -> dict[str, str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    title = ""
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = str(og_title["content"]).strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    desc = ""
    for attrs in ({"property": "og:description"}, {"name": "description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            desc = str(tag["content"]).strip()
            break

    title = re.sub(r"\s+", " ", title)[:MAX_DROP_TITLE]
    desc = re.sub(r"\s+", " ", desc)[:260]
    return {"title": title, "description": desc}


async def fetch_url_preview(drop_id: str, url: str) -> dict[str, str] | None:
    """Politely fetch a link's <title> and description, updating the drop and notifying SSE clients."""
    if config.NO_FETCH and PREVIEW_TRANSPORT is None:
        return None
    cached = _preview_cache.get(url)
    if cached and (time.time() - cached[0]) < _PREVIEW_CACHE_TTL:
        meta = cached[1]
    else:
        meta = await _download_url_meta(url)
        if meta is None:
            return None
        _preview_cache[url] = (time.time(), meta)

    if not meta.get("title") and not meta.get("description"):
        return meta

    with db.db() as con:
        row = con.execute("SELECT title FROM padd_drops WHERE id = ?", (drop_id,)).fetchone()
        if not row:
            return meta
        # Keep an explicit user-supplied title if they already set one.
        new_title = row["title"] or meta.get("title", "")
        con.execute(
            "UPDATE padd_drops SET title = ?, url_description = ?, updated_at = ? WHERE id = ?",
            (new_title[:MAX_DROP_TITLE], meta.get("description", ""), int(time.time()), drop_id),
        )
    notify.padd()
    return meta


async def _download_url_meta(url: str) -> dict[str, str] | None:
    headers = {
        "User-Agent": f"Citadel/{__version__} ({config.CONTACT})",
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    }
    current = url
    try:
        async with httpx.AsyncClient(timeout=_PREVIEW_TIMEOUT, follow_redirects=False, transport=PREVIEW_TRANSPORT) as client:
            for _ in range(4):
                if not await _check_url_safe(current):
                    return None
                async with client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            return None
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code != 200:
                        return None
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "html" not in ctype and "xml" not in ctype:
                        return None
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) >= _PREVIEW_MAX_BYTES:
                            break
                    return _extract_meta(bytes(buf))
    except (httpx.HTTPError, OSError):
        return None
    return None


# ---- ZebraLab bridge ----------------------------------------------------------------------------

async def send_to_zebralab(drop_id: str, target: str = "document") -> dict[str, Any]:
    if target not in ("document", "asset"):
        raise PaddError("Target must be 'document' or 'asset'.")
    d = get_drop(drop_id)
    if not d or not d.get("stored_name"):
        raise PaddError("That drop has no file to send.", 404)
    path = safe_file_path(d["stored_name"])
    if not path or not path.is_file():
        raise PaddError("The file is missing from disk.", 404)
    ext = Path(d["file_name"] or "").suffix.lower()
    allowed = ZEBRALAB_IMG_EXTS if target == "asset" else ZEBRALAB_DOC_EXTS
    if ext not in allowed:
        raise PaddError("ZebraLab accepts PDF, PNG, JPEG, WebP, or BMP files." if target == "document" else "ZebraLab assets must be PNG, JPEG, WebP, or BMP images.")
    if (d["size_bytes"] or 0) > ZEBRALAB_MAX_BYTES:
        raise PaddError("ZebraLab accepts files up to 40 MB.")
    if not config.ZEBRALAB_URL and ZEBRALAB_TRANSPORT is None:
        raise PaddError("ZebraLab isn't configured in this environment.", 503)

    base = config.ZEBRALAB_URL or "http://127.0.0.1:8710"
    endpoint = "/api/assets" if target == "asset" else "/api/documents"
    raw = await asyncio.to_thread(path.read_bytes)
    files = {"file": (d["file_name"] or path.name, raw, d["mime_type"] or "application/octet-stream")}
    try:
        async with httpx.AsyncClient(timeout=20, transport=ZEBRALAB_TRANSPORT) as client:
            resp = await client.post(f"{base}{endpoint}", files=files, headers={"X-ZebraLab": "1"})
    except httpx.HTTPError as exc:
        raise PaddError("ZebraLab isn't answering — start it from Citadel's Projects card first.", 503) from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str(resp.json().get("detail") or "")
        except ValueError:
            pass
        raise PaddError(detail or f"ZebraLab rejected the file ({resp.status_code}).", 502)
    return {"ok": True, "target": target, "zebralab": resp.json()}


# ---- Notes & Checklists -------------------------------------------------------------------------

def _clean_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        t = re.sub(r"[^a-z0-9_-]+", "-", str(raw).strip().lower()).strip("-")[:MAX_TAG_LEN]
        if t and TAG_RE.fullmatch(t) and t not in seen:
            seen.add(t)
            out.append(t)
            if len(out) >= MAX_TAGS:
                break
    return out


def _checklist_counts(body: str) -> tuple[int, int]:
    total = done = 0
    for ln in (body or "").splitlines():
        m = CHECKBOX_LINE_RE.match(ln)
        if m:
            total += 1
            if m.group(2).lower() == "x":
                done += 1
    return done, total


def _note_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    try:
        tags = json.loads(d["tags"] or "[]")
    except ValueError:
        tags = []
    done, total = _checklist_counts(d["body"])
    d["tags"] = tags if isinstance(tags, list) else []
    d["pinned"] = bool(d["pinned"])
    d["archived"] = bool(d["archived"])
    d["check_done"] = done
    d["check_total"] = total
    return d


def list_notes(*, q: str = "", tag: str = "", archived: bool = False) -> dict[str, Any]:
    clauses = ["archived = ?"]
    params: list[Any] = [1 if archived else 0]
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        clauses.append("(title LIKE ? OR body LIKE ? OR tags LIKE ?)")
        params.extend([like, like, like])
    with db.db() as con:
        all_rows = con.execute(
            "SELECT * FROM padd_notes WHERE archived = ? ORDER BY pinned DESC, updated_at DESC, rowid DESC",
            (1 if archived else 0,),
        ).fetchall()
        rows = con.execute(
            f"SELECT * FROM padd_notes WHERE {' AND '.join(clauses)} ORDER BY pinned DESC, updated_at DESC, rowid DESC",
            params,
        ).fetchall()
    tag_counts: dict[str, int] = {}
    for r in all_rows:
        for t in _note_row(r)["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    notes = [_note_row(r) for r in rows]
    clean_tag = (tag or "").strip().lower()
    if clean_tag:
        notes = [n for n in notes if clean_tag in n["tags"]]
    tags = [{"name": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"notes": notes, "tags": tags}


def get_note(note_id: str) -> dict[str, Any] | None:
    with db.db() as con:
        row = con.execute("SELECT * FROM padd_notes WHERE id = ?", (note_id,)).fetchone()
    return _note_row(row) if row else None


def create_note(
    *,
    title: str = "",
    body: str = "",
    kind: str = "note",
    tags: list[str] | None = None,
    pinned: bool = False,
    device_name: str = "",
) -> dict[str, Any]:
    if kind not in NOTE_KINDS:
        raise PaddError("Kind must be 'note' or 'checklist'.")
    if len((body or "").encode("utf-8")) > MAX_TEXT_BYTES:
        raise PaddError(f"Notes are limited to {MAX_TEXT_BYTES // 1024} KB.", 413)
    note_id = secrets.token_hex(8)
    now = int(time.time())
    clean_title = (title or "").strip()[:MAX_NOTE_TITLE] or ("New checklist" if kind == "checklist" else "Untitled note")
    clean_tags = _clean_tags(tags)
    with db.db() as con:
        con.execute(
            """INSERT INTO padd_notes (id, title, body, kind, tags, pinned, archived, device_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (note_id, clean_title, body or "", kind, json.dumps(clean_tags), 1 if pinned else 0,
             (device_name or "")[:60], now, now),
        )
        row = con.execute("SELECT * FROM padd_notes WHERE id = ?", (note_id,)).fetchone()
    return _note_row(row)


def update_note(
    note_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
    device_name: str = "",
) -> dict[str, Any]:
    existing = get_note(note_id)
    if not existing:
        raise PaddError("Note not found.", 404)
    new_title = existing["title"] if title is None else (title.strip()[:MAX_NOTE_TITLE] or "Untitled note")
    new_body = existing["body"] if body is None else body
    if len(new_body.encode("utf-8")) > MAX_TEXT_BYTES:
        raise PaddError(f"Notes are limited to {MAX_TEXT_BYTES // 1024} KB.", 413)
    new_kind = existing["kind"] if kind is None else kind
    if new_kind not in NOTE_KINDS:
        raise PaddError("Kind must be 'note' or 'checklist'.")
    new_tags = existing["tags"] if tags is None else _clean_tags(tags)
    new_pinned = existing["pinned"] if pinned is None else bool(pinned)
    new_archived = existing["archived"] if archived is None else bool(archived)
    new_dev = (device_name or existing["device_name"])[:60]
    now = int(time.time())
    with db.db() as con:
        con.execute(
            """UPDATE padd_notes SET title = ?, body = ?, kind = ?, tags = ?, pinned = ?, archived = ?,
               device_name = ?, updated_at = ? WHERE id = ?""",
            (new_title, new_body, new_kind, json.dumps(new_tags), 1 if new_pinned else 0,
             1 if new_archived else 0, new_dev, now, note_id),
        )
        row = con.execute("SELECT * FROM padd_notes WHERE id = ?", (note_id,)).fetchone()
    return _note_row(row)


def toggle_checklist_line(note_id: str, line_index: int, device_name: str = "") -> dict[str, Any]:
    """Atomically toggle a single `- [ ]` / `- [x]` checkbox line on the server."""
    with db.db() as con:
        row = con.execute("SELECT * FROM padd_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise PaddError("Note not found.", 404)
        lines = (row["body"] or "").splitlines()
        if line_index < 0 or line_index >= len(lines):
            raise PaddError("Checkbox line out of range.", 422)
        m = CHECKBOX_LINE_RE.match(lines[line_index])
        if not m:
            raise PaddError("That line isn't a checklist item.", 422)
        mark = " " if m.group(2).lower() == "x" else "x"
        lines[line_index] = f"{m.group(1)}[{mark}]{m.group(3)}"
        new_body = "\n".join(lines)
        now = int(time.time())
        con.execute(
            "UPDATE padd_notes SET body = ?, device_name = ?, updated_at = ? WHERE id = ?",
            (new_body, (device_name or row["device_name"])[:60], now, note_id),
        )
        updated = con.execute("SELECT * FROM padd_notes WHERE id = ?", (note_id,)).fetchone()
    return _note_row(updated)


def checklist_action(note_id: str, action: str, device_name: str = "") -> dict[str, Any]:
    if action not in ("reset", "clear_done"):
        raise PaddError("Action must be 'reset' or 'clear_done'.")
    with db.db() as con:
        row = con.execute("SELECT * FROM padd_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise PaddError("Note not found.", 404)
        out_lines: list[str] = []
        for ln in (row["body"] or "").splitlines():
            m = CHECKBOX_LINE_RE.match(ln)
            if not m:
                out_lines.append(ln)
            elif action == "reset":
                out_lines.append(f"{m.group(1)}[ ]{m.group(3)}")
            elif m.group(2).lower() != "x":
                out_lines.append(ln)
        now = int(time.time())
        con.execute(
            "UPDATE padd_notes SET body = ?, device_name = ?, updated_at = ? WHERE id = ?",
            ("\n".join(out_lines), (device_name or row["device_name"])[:60], now, note_id),
        )
        updated = con.execute("SELECT * FROM padd_notes WHERE id = ?", (note_id,)).fetchone()
    return _note_row(updated)


def delete_note(note_id: str) -> bool:
    with db.db() as con:
        return con.execute("DELETE FROM padd_notes WHERE id = ?", (note_id,)).rowcount > 0


# ---- Home Assistant Shopping / Todo lists -------------------------------------------------------

def _check_todo_entity(entity_id: str, lists_state: PaddListsState) -> str:
    eid = (entity_id or "").strip()
    if not TODO_ENTITY_RE.fullmatch(eid):
        raise PaddError("Invalid Home Assistant list id.", 422)
    if lists_state.known and eid not in lists_state.known:
        raise PaddError("Unknown Home Assistant list.", 404)
    return eid


def _extract_todo_items(payload: Any, entity_id: str) -> list[dict[str, Any]]:
    container: Any = payload
    if isinstance(payload, dict) and isinstance(payload.get("service_response"), dict):
        container = payload["service_response"]
    raw_items: list[Any] = []
    if isinstance(container, dict):
        entry = container.get(entity_id)
        if isinstance(entry, dict) and isinstance(entry.get("items"), list):
            raw_items = entry["items"]
        elif isinstance(container.get("items"), list):
            raw_items = container["items"]
    out: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        status = "completed" if item.get("status") == "completed" else "needs_action"
        out.append({
            "uid": str(item.get("uid") or summary),
            "summary": summary,
            "status": status,
            "due": item.get("due"),
            "description": str(item.get("description") or "").strip(),
        })
    return out


async def fetch_ha_lists(lists_state: PaddListsState) -> dict[str, Any]:
    """Discover all todo.* lists in Home Assistant and load their items."""
    try:
        states = await homeassistant.request("GET", "/api/states")
    except homeassistant.Unavailable as exc:
        return {"available": False, "error": str(exc), "lists": []}

    todo_entities: list[dict[str, Any]] = []
    for st in states or []:
        eid = str(st.get("entity_id") or "")
        if not eid.startswith("todo.") or not TODO_ENTITY_RE.fullmatch(eid):
            continue
        attrs = st.get("attributes") or {}
        name = str(attrs.get("friendly_name") or eid.removeprefix("todo.").replace("_", " ").title()).strip()
        todo_entities.append({
            "entity_id": eid,
            "name": name,
            "supported_features": int(attrs.get("supported_features") or 0),
        })

    # Prefer shopping list first so mobile opens straight into it.
    todo_entities.sort(key=lambda x: (0 if "shop" in (x["entity_id"] + " " + x["name"]).lower() else 1, x["name"].lower()))
    lists_state.known = {t["entity_id"] for t in todo_entities}

    if not todo_entities:
        return {"available": True, "error": None, "lists": []}

    results: list[dict[str, Any]] = []
    for ent in todo_entities:
        eid = ent["entity_id"]
        try:
            resp = await homeassistant.request("POST", "/api/services/todo/get_items?return_response=true", {"entity_id": eid})
            items = _extract_todo_items(resp, eid)
        except homeassistant.Unavailable as exc:
            return {"available": False, "error": str(exc), "lists": []}
        open_count = sum(1 for it in items if it["status"] != "completed")
        results.append({**ent, "items": items, "open_count": open_count})

    return {"available": True, "error": None, "lists": results}


async def add_ha_item(entity_id: str, summary: str, lists_state: PaddListsState) -> dict[str, Any]:
    eid = _check_todo_entity(entity_id, lists_state)
    clean = (summary or "").strip()[:200]
    if not clean:
        raise PaddError("Enter an item to add.")
    try:
        await homeassistant.service("todo", "add_item", {"entity_id": eid, "item": clean})
    except homeassistant.Unavailable as exc:
        raise PaddError(str(exc), 503) from exc
    return await fetch_ha_lists(lists_state)


async def update_ha_item(
    entity_id: str,
    item: str,
    *,
    status: str | None = None,
    rename: str | None = None,
    lists_state: PaddListsState,
) -> dict[str, Any]:
    eid = _check_todo_entity(entity_id, lists_state)
    key = (item or "").strip()
    if not key:
        raise PaddError("Missing list item identifier.")
    payload: dict[str, Any] = {"entity_id": eid, "item": key}
    if status in ("needs_action", "completed"):
        payload["status"] = status
    if rename is not None:
        clean = rename.strip()[:200]
        if not clean:
            raise PaddError("Item text can't be blank.")
        payload["rename"] = clean
    try:
        await homeassistant.service("todo", "update_item", payload)
    except homeassistant.Unavailable as exc:
        raise PaddError(str(exc), 503) from exc
    return await fetch_ha_lists(lists_state)


async def remove_ha_item(entity_id: str, item: str, lists_state: PaddListsState) -> dict[str, Any]:
    eid = _check_todo_entity(entity_id, lists_state)
    key = (item or "").strip()
    if not key:
        raise PaddError("Missing list item identifier.")
    try:
        await homeassistant.service("todo", "remove_item", {"entity_id": eid, "item": key})
    except homeassistant.Unavailable as exc:
        raise PaddError(str(exc), 503) from exc
    return await fetch_ha_lists(lists_state)


async def clear_ha_completed(entity_id: str, lists_state: PaddListsState) -> dict[str, Any]:
    eid = _check_todo_entity(entity_id, lists_state)
    try:
        await homeassistant.service("todo", "remove_completed_items", {"entity_id": eid})
    except homeassistant.Unavailable as exc:
        raise PaddError(str(exc), 503) from exc
    return await fetch_ha_lists(lists_state)
