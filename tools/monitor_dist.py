"""Export a self-contained monitor source distribution using a strict allowlist.

Run `python -m tools.monitor_dist --output .cache/hpc-monitor-source`, then
`uv build .cache/hpc-monitor-source --out-dir .cache/hpc-monitor-dist`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ("machines.py", "scheduler.py", "ssh.py", "queue_watch.py", "queue_menu.py")


def export(destination: Path) -> None:
    # Never export the whole checkout: it contains personal overlays and runs.
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    package = ROOT / "packaging" / "hpc-monitor"
    for name in ("pyproject.toml", "README.md"):
        shutil.copyfile(package / name, destination / name)
    for directory in ("src", "tests"):
        for source in sorted((package / directory).rglob("*")):
            if source.suffix not in {".py", ".yaml"} or "__pycache__" in source.parts:
                continue
            target = destination / source.relative_to(package)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    core = destination / "src" / "hpc_monitor" / "core"
    core.mkdir()
    (core / "__init__.py").write_text('"""Verified queue engine, exported from XGC Automation."""\n')
    hashes = {}
    for name in CORE:
        source = ROOT / "tools" / name
        shutil.copyfile(source, core / name)
        hashes[f"tools/{name}"] = hashlib.sha256(source.read_bytes()).hexdigest()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    (destination / "SOURCE.json").write_text(json.dumps({
        "repository_revision": revision,
        "note": "Hashes identify exported working-tree code, including uncommitted edits.",
        "sha256": hashes,
    }, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.output)
    print(f"Exported monitor source to {args.output.resolve()}")


if __name__ == "__main__":
    main()
