"""Bounded remote reduction and local summary storage for XGC runs.

Raw XGC ADIOS2 output can be terabytes. This module therefore never copies a
``.bp`` product. The first reducer is a top-level run inventory: a small JSON
document generated on the remote host and returned over the existing SSH
ControlMaster session.

The reducer boundary is intentionally independent of the GUI. XGC's Monitor
can browse directories and catalogs, while future scientific reducers can call
the same ``xgc_analysis`` library used by its plugins.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import machines as m
from tools import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
MAX_INVENTORY_BYTES = 2_000_000
DEFAULT_MAX_ENTRIES = 1_000
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DataAnalysisError(RuntimeError):
    """A run cannot be safely inspected or summarized."""


# This code runs remotely and prints one bounded JSON payload. It performs a
# top-level directory scan only; in particular, it never descends into .bp
# directories or reads ADIOS2 variable arrays.
_REMOTE_INVENTORY = r"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1]).expanduser()
max_entries = int(sys.argv[2])
if max_entries < 1 or max_entries > 10000:
    raise SystemExit("max_entries must be between 1 and 10000")
if not root.exists():
    raise SystemExit("run directory not found: " + str(root))
if not root.is_dir():
    raise SystemExit("run path is not a directory: " + str(root))

entries = []
counts = {
    "adios2_products": 0,
    "regular_files": 0,
    "directories": 0,
    "symlinks": 0,
    "other": 0,
}
top_level_regular_file_bytes = 0
children = sorted(root.iterdir(), key=lambda path: path.name)
for path in children[:max_entries]:
    stat = path.lstat()
    is_link = path.is_symlink()
    is_bp = path.name.endswith(".bp")
    if is_link:
        kind = "symlink"
        counts["symlinks"] += 1
    elif is_bp:
        kind = "adios2_product"
        counts["adios2_products"] += 1
    elif path.is_file():
        kind = "file"
        counts["regular_files"] += 1
        top_level_regular_file_bytes += int(stat.st_size)
    elif path.is_dir():
        kind = "directory"
        counts["directories"] += 1
    else:
        kind = "other"
        counts["other"] += 1
    entries.append({
        "name": path.name,
        "kind": kind,
        "size_bytes": int(stat.st_size) if path.is_file() and not is_link else None,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    })

payload = {
    "schema": "xgc-run-inventory/v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "remote_run_dir": str(root.resolve()),
    "scan": {
        "scope": "top-level-only",
        "entry_limit": max_entries,
        "entry_count_total": len(children),
        "entry_count_returned": len(entries),
        "truncated": len(children) > max_entries,
    },
    "counts": counts,
    "top_level_regular_file_bytes": top_level_regular_file_bytes,
    "entries": entries,
    "warnings": [
        "ADIOS2 .bp contents were not read or transferred.",
        "Directory sizes are intentionally not calculated recursively.",
    ],
}
print(json.dumps(payload, separators=(",", ":")))
"""


def _run_record(run_id: str) -> tuple[Path, dict[str, Any]]:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise DataAnalysisError(f"invalid run_id: {run_id!r}")
    run_dir = RUNS_DIR / run_id
    record_path = run_dir / "provenance.json"
    if not record_path.is_file():
        raise DataAnalysisError(f"provenance not found for run {run_id!r}")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataAnalysisError(f"invalid provenance for run {run_id!r}: {exc}") from exc
    if not isinstance(record, dict):
        raise DataAnalysisError(f"invalid provenance for run {run_id!r}: expected object")
    return run_dir, record


def _remote_run_dir(machine: str, run_id: str) -> tuple[Path, str]:
    run_dir, record = _run_record(run_id)
    recorded_machine = record.get("machine")
    if recorded_machine != machine:
        raise DataAnalysisError(
            f"run {run_id!r} belongs to {recorded_machine!r}, not {machine!r}"
        )
    remote = record.get("remote_run_dir")
    if not isinstance(remote, str) or not remote.startswith("/"):
        raise DataAnalysisError(
            f"run {run_id!r} has no absolute remote_run_dir in provenance"
        )
    return run_dir, remote


def inspect_run(
    machine: str,
    run_id: str,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict[str, Any]:
    """Generate a bounded inventory on the remote host without writing files."""
    if max_entries < 1 or max_entries > 10_000:
        raise DataAnalysisError("max_entries must be between 1 and 10000")

    _, remote = _remote_run_dir(machine, run_id)
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    result = ssh.run(
        host,
        ["python3", "-c", _REMOTE_INVENTORY, remote, str(max_entries)],
        timeout=180.0,
    )
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()[-1_000:]
        raise DataAnalysisError(
            f"remote inventory failed for {run_id!r} (rc={result.returncode}): {detail}"
        )
    encoded = result.stdout.encode("utf-8")
    if len(encoded) > MAX_INVENTORY_BYTES:
        raise DataAnalysisError(
            f"remote inventory exceeded {MAX_INVENTORY_BYTES} bytes; reduce max_entries"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DataAnalysisError(f"remote inventory returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "xgc-run-inventory/v1":
        raise DataAnalysisError("remote inventory returned an unsupported schema")

    payload["run_id"] = run_id
    payload["machine"] = machine
    return payload


def analyze_run(
    machine: str,
    run_id: str,
    *,
    product: str = "inventory",
    max_entries: int = DEFAULT_MAX_ENTRIES,
    save: bool = True,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Create one reduced product and optionally save it under runs/<run_id>/.

    Creating a new derived summary is safe and local. Replacing an existing
    summary requires ``confirm_overwrite=True`` so callers cannot silently
    destroy an earlier result.
    """
    if product != "inventory":
        raise DataAnalysisError(
            f"unsupported product {product!r}; available products: ['inventory']"
        )

    run_dir, _ = _run_record(run_id)
    payload = inspect_run(machine, run_id, max_entries=max_entries)
    result: dict[str, Any] = {
        "ok": True,
        "product": product,
        "summary": payload,
        "saved": False,
    }
    if not save:
        return result

    summary_dir = run_dir / "summaries"
    output_path = summary_dir / f"{product}.json"
    if output_path.exists() and not confirm_overwrite:
        result.update(
            {
                "ok": False,
                "reason": (
                    "summary already exists; review it and set "
                    "confirm_overwrite=True to replace it"
                ),
                "output_path": str(output_path),
            }
        )
        return result

    summary_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "product": product,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "machine": machine,
            "run_id": run_id,
            "remote_run_dir": payload["remote_run_dir"],
        },
        "data": payload,
    }
    output_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result.update({"saved": True, "output_path": str(output_path)})
    return result
