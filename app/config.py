"""Process-level settings. Everything a *user* configures (projects, links, widget settings) lives in
the database; this module only holds what the server needs to boot, read from the environment / .env."""
from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


HOST = _env("HUB_HOST", "0.0.0.0")
PORT = int(_env("HUB_PORT", "8080"))
DATA_DIR = Path(_env("HUB_DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "hub.sqlite"
WEB_DIR = ROOT / "web"
# Where the projects the dashboard launches live. Seed data is relative to this, so moving the
# dashboard (or the projects) to another machine only needs this one value changed.
PROJECTS_DIR = Path(_env("HUB_PROJECTS_DIR", str(ROOT.parent)))
TZ = ZoneInfo(_env("HUB_TZ", "Europe/London"))
# MusicBrainz asks API clients to identify themselves. A public URL is enough — no personal address is sent.
CONTACT = _env("HUB_CONTACT", "+https://www.mikehadfield.co.uk")
MUSIC_DB = Path(_env("HUB_MUSIC_DB", str(PROJECTS_DIR / "MusicDatabase" / "data" / "music.sqlite")))
HOUSE_DB = Path(_env("HUB_HOUSE_DB", str(PROJECTS_DIR / "HomeImprovements" / "data" / "house.sqlite")))
POKER_DB = Path(_env("HUB_POKER_DB", str(PROJECTS_DIR / "PokerCompanion" / "data" / "poker.sqlite")))
# Test/dev switch: widgets are registered but never fetch (no network, no external services).
NO_FETCH = _env("HUB_NO_FETCH") == "1"
DISK_SERVER_URL = _env("HUB_DISK_SERVER_URL", "http://127.0.0.1:8765/disks.json")
# Monitoring of the other machines (app/machines.py): Citadel's own restricted SSH key, made by tools/machines_setup.py.
# Kept in this checkout's data/ whatever HUB_DATA_DIR says, so a scratch copy watches the same machines with the same key.
MACHINES_KEY = Path(_env("HUB_MACHINES_KEY", str(ROOT / "data" / "machines_ed25519")))
# Test/demo switch: no SSH to the machines at all.
NO_MACHINES = _env("HUB_NO_MACHINES") == "1"
# The GoXLR daemon's local HTTP API (goxlr-utility), behind the header's Line out / Vinyl volume buttons (app/volume.py).
# None under HUB_NO_FETCH (tests, scratch copies): only the live hub may touch the real speakers.
GOXLR_URL = _env("HUB_GOXLR_URL", None if NO_FETCH else "http://localhost:14564")
# The stats collector on NZXT (collector/citadel_collector.py, docs/tasks/stats-collector.md) asks the machines for their
# stats and keeps the history; Citadel reads it back. The token is in the data dir, unlike the SSH key above, so a
# scratch copy (its own HUB_DATA_DIR) has none and never pushes its machine list to the live collector.
COLLECTOR_URL = (_env("HUB_COLLECTOR_URL", "http://192.168.0.70:8680") or "").rstrip("/")
COLLECTOR_TOKEN_FILE = DATA_DIR / "collector_token"
# The Gaming → Phoenix backup (~/Projects/HomeLab/backup) runs from its own systemd user timer and writes this status
# file; the "Phoenix backup" maintenance job reads it, and its Run now starts the unit ("none" = never start it).
BACKUP_STATUS = Path(_env("HUB_BACKUP_STATUS", str(Path.home() / ".local" / "state" / "citadel-backup" / "last.json")))
BACKUP_UNIT = _env("HUB_BACKUP_UNIT", "citadel-backup.service")
# Home Assistant on Probe-1 (~/Projects/HomeLab/home-assistant): spoken reminders on the Echos (app/homeassistant.py).
# The token belongs to HA's non-admin "Citadel" user, saved by HomeLab's set-token.sh; no file = the job is "not set up".
HA_URL = (_env("HUB_HA_URL", "http://192.168.0.79") or "").rstrip("/")
HA_TOKEN_FILE = Path(_env("HUB_HA_TOKEN_FILE", str(Path.home() / ".config" / "home-assistant" / "token")))
# ZebraLab on this PC (PADD's "Send to ZebraLab" bridge for dropped PDFs and images). None under HUB_NO_FETCH.
ZEBRALAB_URL = (_env("HUB_ZEBRALAB_URL", None if NO_FETCH else "http://127.0.0.1:8710") or "").rstrip("/")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # the DB holds secrets (e.g. calendar URLs)


def secret_key() -> bytes:
    """Cookie-signing key: HUB_SECRET if set, otherwise generated once and kept in the data dir."""
    if env := _env("HUB_SECRET"):
        return env.encode()
    ensure_dirs()
    path = DATA_DIR / "secret.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return path.read_text().strip().encode()


def lan_ip() -> str:
    """The address other machines on the LAN would use to reach this host."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this just selects the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def public_url() -> str:
    return _env("HUB_PUBLIC_URL") or f"http://{lan_ip()}:{PORT}"
