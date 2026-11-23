#!/usr/bin/env python3
"""Copy Citadel's shared theme kit (web/kit/) into each framed app's vendor folder.

Usage:
  sync_kit.py                          # sync all known apps under ~/Projects
  sync_kit.py holodeck zebralab        # sync specific targets
  sync_kit.py --check                  # exit 1 if any target is missing or stale (no writes)
  sync_kit.py --target-dir holodeck=../Holodeck-themes/web/vendor/citadel-kit holodeck

Never touches anything outside each target's vendor/citadel-kit folder.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

KIT_SRC = Path(__file__).resolve().parent.parent / "web" / "kit"
DEFAULT_PROJECTS = Path.home() / "Projects"
SKIP_NAMES = {"demo.html", "demo.css", "demo.js"}

TARGETS: dict[str, str] = {
    "holodeck": "Holodeck/web/vendor/citadel-kit",
    "zebralab": "ZebraLab/web/vendor/citadel-kit",
    "homeimprovements": "HomeImprovements/web/vendor/citadel-kit",
    "music-site": "MusicDatabase/site/vendor/citadel-kit",
    "music-explore": "MusicDatabase/etl/explore/vendor/citadel-kit",
    "music-maintenance": "MusicDatabase/etl/maintenance/vendor/citadel-kit",
}


def _file_map(root: Path, *, exclude_top: set[str] = frozenset()) -> dict[str, bytes]:
    if not root.exists():
        return {}
    out: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel in exclude_top:
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).digest()
    return out


def is_in_sync(src: Path, dest: Path) -> bool:
    return _file_map(src, exclude_top=SKIP_NAMES) == _file_map(dest)


def sync_target(src: Path, dest: Path) -> bool:
    """Copy src -> dest (strictly inside dest). Returns True if changes were written."""
    if is_in_sync(src, dest):
        return False
    src_files = _file_map(src, exclude_top=SKIP_NAMES)
    dest.mkdir(parents=True, exist_ok=True)
    # Remove stale files strictly inside dest
    for p in sorted(dest.rglob("*"), reverse=True):
        if p.is_file():
            rel = p.relative_to(dest).as_posix()
            if rel not in src_files:
                p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    # Copy fresh/updated files
    for rel in src_files:
        s = src / rel
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync Citadel's shared theme kit into framed apps.")
    ap.add_argument("targets", nargs="*", help=f"Subset of targets ({', '.join(TARGETS)}); default: all")
    ap.add_argument("--check", action="store_true", help="Check sync status without modifying files")
    ap.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS, help="Workspace root (default ~/Projects)")
    ap.add_argument("--target-dir", action="append", default=[], help="Override a target path: name=/abs/or/rel/path")
    args = ap.parse_args(argv)

    overrides: dict[str, Path] = {}
    for spec in args.target_dir:
        if "=" not in spec:
            raise SystemExit(f"invalid --target-dir {spec!r}; expected name=path")
        k, v = spec.split("=", 1)
        overrides[k] = Path(v).expanduser().resolve()

    selected = args.targets or list(TARGETS)
    unknown = [t for t in selected if t not in TARGETS and t not in overrides]
    if unknown:
        raise SystemExit(f"unknown target(s): {', '.join(unknown)} (valid: {', '.join(TARGETS)})")

    stale: list[str] = []
    for name in selected:
        dest = overrides[name] if name in overrides else (args.projects_root / TARGETS[name])
        if args.check:
            ok = is_in_sync(KIT_SRC, dest)
            print(f"{'OK   ' if ok else 'STALE'} {name:18s} {dest}")
            if not ok:
                stale.append(name)
        else:
            changed = sync_target(KIT_SRC, dest)
            print(f"{'SYNC ' if changed else 'OK   '} {name:18s} {dest}")

    if args.check and stale:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
