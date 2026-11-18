r"""Set a machine up for Citadel's Monitoring (app/machines.py), or undo it.

    .venv/bin/python tools/machines_setup.py <ssh target>             # e.g. jellyfin, runabout, mike@192.168.0.82
    .venv/bin/python tools/machines_setup.py <ssh target> --remove

Run from Gaming with Mike's own SSH access. It:
  1. makes Citadel's monitoring key (data/machines_ed25519) the first time;
  2. copies tools/machines/citadel-stats to ~/.local/bin/citadel-stats on the machine;
  3. adds Citadel's key to ~/.ssh/authorized_keys with a forced command and `restrict`, so that key can only ever run
     the stats script (no shell, no forwarding);
  4. logs in with that key the way Citadel will, and prints what came back.

The install runs as a small Python program on the machine (python3 is on every Linux machine here), so it doesn't
depend on the login shell: Runabout's is fish. Nothing needs sudo.

`--windows` (Nasferatu, an admin account) does the same with tools/machines/citadel-stats.ps1: it goes to
C:\ProgramData\Citadel\ and the key to C:\ProgramData\ssh\administrators_authorized_keys, the file Windows' sshd reads
for admin accounts (written in place, so its locked-down permissions stay as they are).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, machines   # noqa: E402

SCRIPT = ROOT / "tools" / "machines" / "citadel-stats"
PS_SCRIPT = ROOT / "tools" / "machines" / "citadel-stats.ps1"
WIN_DIR = r"C:\ProgramData\Citadel"
TAG = "citadel-monitoring"

# Sent on stdin after three assignments (script, keyline, remove): nothing goes through the remote shell, so quotes in
# the key line survive whatever the login shell is.
REMOTE = r'''
import os
home = os.path.expanduser("~")
dest = os.path.join(home, ".local", "bin", "citadel-stats")
ssh_dir = os.path.join(home, ".ssh")
auth = os.path.join(ssh_dir, "authorized_keys")
lines = open(auth).read().splitlines() if os.path.exists(auth) else []
lines = [l for l in lines if not l.rstrip().endswith(" ''' + TAG + r'''")]
if remove:
    if os.path.exists(dest):
        os.remove(dest)
else:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        f.write(script)
    os.chmod(dest, 0o755)
    lines.append(keyline.replace("@DEST@", dest))
os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
if os.path.exists(auth):
    import shutil
    shutil.copy2(auth, auth + ".bak-citadel")      # the previous file, in case anything looks wrong
with open(auth, "w") as f:
    f.write("\n".join(lines) + ("\n" if lines else ""))
os.chmod(auth, 0o600)
print("removed" if remove else dest)
'''


# PowerShell for --windows, run with `iex` on the whole of stdin (`-Command -` would run it line by line). The script and
# the key line arrive base64-encoded, so no quoting survives or breaks anything on the way.
WIN_REMOTE = r'''
$script = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(@'
@SCRIPT@
'@))
$keyline = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(@'
@KEYLINE@
'@))
$remove = @REMOVE@
$dir = '@DIR@'
$auth = 'C:\ProgramData\ssh\administrators_authorized_keys'
$lines = @()
if (Test-Path $auth) { Copy-Item $auth "$auth.bak-citadel" -Force; $lines = @(Get-Content $auth | Where-Object { -not $_.TrimEnd().EndsWith(' citadel-monitoring') }) }
if ($remove) { Remove-Item -Recurse -Force "$dir\citadel-stats.ps1" -ErrorAction SilentlyContinue }
else {
    New-Item -ItemType Directory -Force $dir | Out-Null
    [IO.File]::WriteAllText("$dir\citadel-stats.ps1", $script)
    $lines += $keyline
}
Set-Content -Path $auth -Value $lines -Encoding ascii
if ($remove) { 'removed' } else { "$dir\citadel-stats.ps1" }
'''


def ensure_key() -> str:
    key = config.MACHINES_KEY
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", TAG, "-f", str(key)], check=True)
        print(f"made {key}")
    return key.with_suffix(".pub").read_text().split()[1]


def resolve(target: str) -> tuple[str, str]:
    """(user, host) the way ssh itself reads the target, aliases included (Citadel's own ssh ignores ssh_config)."""
    out = subprocess.run(["ssh", "-G", target], capture_output=True, text=True, check=True).stdout
    opts = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    return opts["user"], opts["hostname"]


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1].startswith("-"):
        print(__doc__)
        return 2
    target, remove, windows = argv[1], "--remove" in argv, "--windows" in argv
    pub = ensure_key()
    if windows:
        import base64
        import textwrap
        # wrapped at 76: Windows' stdin stalls on one very long line (FromBase64String skips the line breaks)
        b64 = lambda s: "\n".join(textwrap.wrap(base64.b64encode(s.encode()).decode(), 76))
        keyline = f'command="powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {WIN_DIR}\\citadel-stats.ps1",restrict ssh-ed25519 {pub} {TAG}'
        program = (WIN_REMOTE.replace("@SCRIPT@", b64(PS_SCRIPT.read_text())).replace("@KEYLINE@", b64(keyline))
                   .replace("@REMOVE@", "$true" if remove else "$false").replace("@DIR@", WIN_DIR))
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, 'powershell -NoProfile -NonInteractive -Command "iex ([Console]::In.ReadToEnd())"'],
                           input=program, capture_output=True, text=True)
    else:
        keyline = f'command="python3 -I @DEST@",restrict ssh-ed25519 {pub} {TAG}'
        program = f"script = {SCRIPT.read_text()!r}\nkeyline = {keyline!r}\nremove = {remove!r}\n" + REMOTE
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, "python3 -I -"], input=program, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip() or r.stdout.strip())
        return 1
    print(f"{target}: {r.stdout.strip()}")
    if remove:
        return 0
    import asyncio
    user, host = resolve(target)
    stats, error = asyncio.run(machines.Machines.fetch(user, host))
    if error:
        print(f"Citadel's key didn't work: {error}")
        return 1
    print(f"Citadel sees {user}@{host}: {json.dumps({k: stats[k] for k in ('host', 'os', 'cpu')})}")
    print(f"In Settings → Remote access, set this device's Monitoring SSH user to: {user}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
