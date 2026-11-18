r"""Set a machine up for Citadel's Monitoring, or undo it.

    .venv/bin/python tools/machines_setup.py <ssh target>             # e.g. jellyfin, runabout, mike@192.168.0.82
    .venv/bin/python tools/machines_setup.py <ssh target> --remove
    .venv/bin/python tools/machines_setup.py <ssh target> --remove --key citadel    # drop Citadel's old key only

The machines are asked for their stats by the collector on NZXT (collector/citadel_collector.py, installed by
tools/collector_setup.py), which has its own key; its public half is kept here as data/collector_ed25519.pub. Before
2026-11-18 Citadel asked them itself with data/machines_ed25519 (`--key citadel`, tag citadel-monitoring). Each key is
one line in authorized_keys, ending in its tag, so the two can sit side by side during the switch.

Run from Gaming with Mike's own SSH access. It:
  1. copies tools/machines/citadel-stats to ~/.local/bin/citadel-stats on the machine;
  2. adds the key to ~/.ssh/authorized_keys with a forced command and `restrict`, so that key can only ever run the
     stats script (no shell, no forwarding);
  3. gives the collector this machine's host key, as Gaming knows it;
  4. logs in with that key the way the collector will (from NZXT), and prints what came back.

`--remove` takes that key's line out again, and deletes the script once no Citadel key is left that runs it.

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
import collector_setup             # noqa: E402  (tools/, beside this file)

SCRIPT = ROOT / "tools" / "machines" / "citadel-stats"
PS_SCRIPT = ROOT / "tools" / "machines" / "citadel-stats.ps1"
WIN_DIR = r"C:\ProgramData\Citadel"
TAGS = {"collector": "citadel-collector", "citadel": "citadel-monitoring"}

# Sent on stdin after four assignments (script, keyline, remove, tag): nothing goes through the remote shell, so quotes
# in the key line survive whatever the login shell is.
REMOTE = r'''
import os
home = os.path.expanduser("~")
dest = os.path.join(home, ".local", "bin", "citadel-stats")
ssh_dir = os.path.join(home, ".ssh")
auth = os.path.join(ssh_dir, "authorized_keys")
lines = open(auth).read().splitlines() if os.path.exists(auth) else []
lines = [l for l in lines if not l.rstrip().endswith(" " + tag)]
if remove:
    if os.path.exists(dest) and not any(dest in l for l in lines):     # another Citadel key still runs it
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
$tag = ' @TAG@'
$auth = 'C:\ProgramData\ssh\administrators_authorized_keys'
$lines = @()
if (Test-Path $auth) { Copy-Item $auth "$auth.bak-citadel" -Force; $lines = @(Get-Content $auth | Where-Object { -not $_.TrimEnd().EndsWith($tag) }) }
if ($remove -and -not ($lines | Where-Object { $_ -like '*citadel-stats.ps1*' })) { Remove-Item -Force "$dir\citadel-stats.ps1" -ErrorAction SilentlyContinue }
else {
    New-Item -ItemType Directory -Force $dir | Out-Null
    [IO.File]::WriteAllText("$dir\citadel-stats.ps1", $script)
    $lines += $keyline
}
Set-Content -Path $auth -Value $lines -Encoding ascii
if ($remove) { 'removed' } else { "$dir\citadel-stats.ps1" }
'''


def public_key(which: str) -> str:
    """The base64 part of the key's public half. The collector's comes from its install; Citadel's old one is only
    needed to remove it."""
    pub = collector_setup.PUB if which == "collector" else config.MACHINES_KEY.with_suffix(".pub")
    if not pub.exists():
        raise SystemExit(f"{pub} is missing: run tools/collector_setup.py first" if which == "collector" else f"{pub} is missing")
    return pub.read_text().split()[1]


# Run on NZXT as `python3 -I -` after ARGV = [...]: logs in with the collector's own key exactly as it will.
CHECK = r'''
import json, os, subprocess
home = os.path.expanduser("~")
argv = [a.replace("@HOME@", home) for a in ARGV]
p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
print(json.dumps({"code": p.returncode, "out": p.stdout[-4000:], "err": p.stderr[-1000:]}))
'''


def check_from_collector(user: str, host: str, addr: str) -> tuple[dict | None, str | None]:
    collector = load_collector()
    collector.KEY = "@HOME@/.config/citadel-collector/machines_ed25519"
    collector.KNOWN_HOSTS = "@HOME@/.config/citadel-collector/known_hosts"
    argv = collector.ssh_argv({"host": host, "addr": addr, "user": user})
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", collector_setup.DEFAULT_TARGET, "python3 -I -"],
                       input=f"ARGV = {argv!r}\n" + CHECK, capture_output=True, text=True)
    if r.returncode != 0:
        return None, f"couldn't reach the collector's machine: {r.stderr.strip()}"
    res = json.loads(r.stdout.strip().splitlines()[-1])
    if res["code"] != 0:
        return None, collector.explain(res["err"], res["code"])
    try:
        return json.loads(res["out"].strip().splitlines()[-1]), None
    except (ValueError, IndexError):
        return None, "Stats script printed something unexpected"


def load_collector():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("citadel_collector", str(collector_setup.COLLECTOR))
    spec = importlib.util.spec_from_loader("citadel_collector", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


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
    which = argv[argv.index("--key") + 1] if "--key" in argv else "collector"
    if which not in TAGS:
        print(f"--key is one of: {', '.join(TAGS)}")
        return 2
    tag = TAGS[which]
    if which == "citadel" and not remove:
        print("Citadel's own key is only kept to be removed now: the collector asks the machines (--key collector)")
        return 2
    pub = public_key(which)
    if windows:
        import base64
        import textwrap
        # wrapped at 76: Windows' stdin stalls on one very long line (FromBase64String skips the line breaks)
        b64 = lambda s: "\n".join(textwrap.wrap(base64.b64encode(s.encode()).decode(), 76))
        keyline = f'command="powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {WIN_DIR}\\citadel-stats.ps1",restrict ssh-ed25519 {pub} {tag}'
        program = (WIN_REMOTE.replace("@SCRIPT@", b64(PS_SCRIPT.read_text())).replace("@KEYLINE@", b64(keyline))
                   .replace("@REMOVE@", "$true" if remove else "$false").replace("@DIR@", WIN_DIR).replace("@TAG@", tag))
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, 'powershell -NoProfile -NonInteractive -Command "iex ([Console]::In.ReadToEnd())"'],
                           input=program, capture_output=True, text=True)
    else:
        keyline = f'command="python3 -I @DEST@",restrict ssh-ed25519 {pub} {tag}'
        program = f"script = {SCRIPT.read_text()!r}\nkeyline = {keyline!r}\nremove = {remove!r}\ntag = {tag!r}\n" + REMOTE
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, "python3 -I -"], input=program, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip() or r.stdout.strip())
        return 1
    print(f"{target}: {r.stdout.strip()}")
    if remove:
        return 0
    user, host = resolve(target)
    added = collector_setup.seed_known_hosts([host])
    if added:
        print(f"gave the collector {host}'s host key ({added} line{'s' if added != 1 else ''})")
    stats, error = check_from_collector(user, host, machines.ipv4(host) or "")
    if error:
        print(f"The collector's key didn't work: {error}")
        return 1
    print(f"The collector sees {user}@{host}: {json.dumps({k: stats[k] for k in ('host', 'os', 'cpu')})}")
    print(f"In Settings → Remote access, set this device's Monitoring SSH user to: {user}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
