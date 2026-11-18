"""Install or update Citadel's stats collector on NZXT (collector/citadel_collector.py), from Gaming.

    .venv/bin/python tools/collector_setup.py [ssh target]        # default: jellyfin (NZXT)

Run it again after changing the collector or the stats script: it copies them over and restarts the unit. It:
  1. copies citadel_collector.py and tools/machines/citadel-stats to ~/.local/lib/citadel-collector, and the user unit
     to ~/.config/systemd/user/citadel-collector.service;
  2. makes the collector's own SSH key on NZXT the first time (~/.config/citadel-collector/machines_ed25519) and keeps
     a copy of its public half here (data/collector_ed25519.pub) for tools/machines_setup.py to install on each machine;
  3. makes the shared token the first time (data/collector_token here, config.json there; both 0600);
  4. gives the collector the host keys Gaming already trusts for every monitored machine (`seed_known_hosts`), so it
     checks them strictly and never trusts one on sight;
  5. enables and (re)starts the unit, then checks it answers from here.

Needs Mike's own SSH access to NZXT; nothing here needs sudo. Opening the port in NZXT's firewall and lingering do, and
are printed when they're missing. The plan is docs/tasks/stats-collector.md.
"""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, procs   # noqa: E402

COLLECTOR = ROOT / "collector" / "citadel_collector.py"
UNIT = ROOT / "collector" / "citadel-collector.service"
STATS = ROOT / "tools" / "machines" / "citadel-stats"
PUB = config.DATA_DIR / "collector_ed25519.pub"
TOKEN = config.COLLECTOR_TOKEN_FILE
PORT = 8680
DEFAULT_TARGET = "jellyfin"

# Run on NZXT as `python3 -I -`, after FILES / TOKEN / KNOWN assignments (base64 strings), so no quoting goes through
# its login shell.
REMOTE = r'''
import base64, json, os, subprocess
home = os.path.expanduser("~")
lib = os.path.join(home, ".local", "lib", "citadel-collector")
cfgdir = os.path.join(home, ".config", "citadel-collector")
unitdir = os.path.join(home, ".config", "systemd", "user")
for d in (lib, cfgdir, unitdir):
    os.makedirs(d, exist_ok=True)
os.chmod(cfgdir, 0o700)
for name, b64 in FILES.items():
    dest = os.path.join(unitdir if name.endswith(".service") else lib, name)
    with open(dest, "wb") as f:
        f.write(base64.b64decode(b64))
    os.chmod(dest, 0o644 if name.endswith(".service") else 0o755)
key = os.path.join(cfgdir, "machines_ed25519")
if not os.path.exists(key):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "citadel-collector", "-f", key], check=True)
cfg = os.path.join(cfgdir, "config.json")
old = json.load(open(cfg)) if os.path.exists(cfg) else {}
old.update({"token": base64.b64decode(TOKEN).decode(), "port": PORT, "bind": old.get("bind", "0.0.0.0")})
fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(old, f)
kh = os.path.join(cfgdir, "known_hosts")
have = open(kh).read().splitlines() if os.path.exists(kh) else []
lines = have + [l for l in base64.b64decode(KNOWN).decode().splitlines() if l and l not in have]
with open(kh, "w") as f:
    f.write("\n".join(lines) + ("\n" if lines else ""))
sc = ["systemctl", "--user"]
subprocess.run(sc + ["daemon-reload"], check=True)
subprocess.run(sc + ["enable", "citadel-collector.service"], check=True, capture_output=True)
subprocess.run(sc + ["restart", "citadel-collector.service"], check=True)
linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"], capture_output=True, text=True).stdout.strip()
print(json.dumps({"pub": open(key + ".pub").read().strip(), "linger": linger}))
'''

# Adds host-key lines to the collector's known_hosts on NZXT (machines_setup.py uses this for a new machine).
REMOTE_KNOWN = r'''
import base64, os
kh = os.path.join(os.path.expanduser("~"), ".config", "citadel-collector", "known_hosts")
have = open(kh).read().splitlines() if os.path.exists(kh) else []
new = [l for l in base64.b64decode(KNOWN).decode().splitlines() if l and l not in have]
with open(kh, "w") as f:
    f.write("\n".join(have + new) + ("\n" if have + new else ""))
print(len(new))
'''


def b64(data: bytes | str) -> str:
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


def monitored_hosts() -> list[str]:
    """Every Remote Access device with a monitoring user, read straight from Citadel's database, read-only."""
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT address FROM remote_hosts WHERE ssh_user != ''").fetchall()
    finally:
        con.close()
    return [procs.vnc_port(r[0])[0] for r in rows]


def known_lines(hosts: list[str]) -> str:
    """The lines Gaming's own known_hosts has for these hosts (hashed or not), as `ssh-keygen -F` finds them. The
    collector checks a host's key under its name (HostKeyAlias), so these match whatever address it connects to."""
    out: list[str] = []
    for h in hosts:
        r = subprocess.run(["ssh-keygen", "-F", h], capture_output=True, text=True)
        out += [l for l in r.stdout.splitlines() if l and not l.startswith("#")]
    return "\n".join(out)


def seed_known_hosts(hosts: list[str], target: str = DEFAULT_TARGET) -> int:
    lines = known_lines(hosts)
    if not lines:
        return 0
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, "python3 -I -"], input=f"KNOWN = {b64(lines)!r}\n" + REMOTE_KNOWN,
                       capture_output=True, text=True, check=True)
    return int(r.stdout.strip() or 0)


def ensure_token() -> str:
    if TOKEN.exists():
        return TOKEN.read_text().strip()
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    TOKEN.touch(mode=0o600)
    TOKEN.write_text(tok + "\n")
    print(f"made {TOKEN}")
    return tok


def main(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(__doc__)
        return 0
    target = argv[1] if len(argv) > 1 else DEFAULT_TARGET
    token = ensure_token()
    files = {"citadel_collector.py": b64(COLLECTOR.read_bytes()), "citadel-stats": b64(STATS.read_bytes()),
             "citadel-collector.service": b64(UNIT.read_bytes())}
    program = (f"FILES = {files!r}\nTOKEN = {b64(token)!r}\nKNOWN = {b64(known_lines(monitored_hosts()))!r}\nPORT = {PORT}\n" + REMOTE)
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, "python3 -I -"], input=program, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip() or r.stdout.strip())
        return 1
    info = json.loads(r.stdout.strip().splitlines()[-1])
    PUB.write_text(info["pub"] + "\n")
    print(f"{target}: collector installed and restarted; its public key is in {PUB}")
    if info["linger"] != "yes":
        print("  Lingering is off, so it stops when Mike logs out of NZXT. On NZXT:  sudo loginctl enable-linger mike")
    host = subprocess.run(["ssh", "-G", target], capture_output=True, text=True).stdout
    hostname = next((l.split(" ", 1)[1] for l in host.splitlines() if l.startswith("hostname ")), target)
    url = f"http://{hostname}:{PORT}/api/health"
    try:
        import time
        time.sleep(2)
        with urllib.request.urlopen(url, timeout=5) as resp:
            print(f"  {url}: {resp.read().decode()}")
    except OSError as exc:
        print(f"  {url} didn't answer from here ({exc}). If the firewall is the cause, on NZXT:\n"
              f"    sudo ufw allow from 192.168.0.0/24 to any port {PORT} proto tcp")
    print("Next: .venv/bin/python tools/machines_setup.py <machine> for each machine, so it accepts the collector's key.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
