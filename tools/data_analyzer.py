"""Bounded remote reduction and local summary storage for XGC runs.

Raw XGC ADIOS2 output can be terabytes. This module therefore never copies a
``.bp`` product. It provides a top-level run inventory and a bounded 1-D
profile reducer: small JSON documents generated on the remote host and
returned over the existing SSH ControlMaster session.

The reducer boundary is intentionally independent of the GUI. XGC's Monitor
can browse directories and catalogs, while scientific reducers call the same
``xgc_analysis`` catalog layer used by its plugins.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from tools import machines as m
from tools import provenance as prov
from tools import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
MAX_INVENTORY_BYTES = 2_000_000
MAX_SCIENTIFIC_SUMMARY_BYTES = 4_000_000
DEFAULT_MAX_ENTRIES = 1_000
DEFAULT_MAX_PROFILE_POINTS = 512
MIN_PROFILE_POINTS = 16
MAX_PROFILE_POINTS = 2_048
LOGIN_NODE_MAX_RUNTIME_SECONDS = 6 * 60 * 60
FILE_ACCESS_DOMINANT = "file_access_dominant"
COMPUTE_DOMINANT = "compute_dominant"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DataAnalysisError(RuntimeError):
    """A run cannot be safely inspected or summarized."""


def analysis_execution_policy(
    workload_dominance: str,
    runtime_limit_seconds: float,
) -> dict[str, Any]:
    """Choose login-node or scheduler execution from workload character.

    Runtime is a secondary bound: file-access-dominant work may run on a login
    node for at most six hours, while compute-dominant work always belongs on a
    compute node even when its expected duration is short.
    """
    if workload_dominance not in {FILE_ACCESS_DOMINANT, COMPUTE_DOMINANT}:
        raise DataAnalysisError(
            "workload_dominance must be file_access_dominant or compute_dominant"
        )
    if runtime_limit_seconds <= 0:
        raise DataAnalysisError("runtime_limit_seconds must be positive")

    within_login_limit = runtime_limit_seconds <= LOGIN_NODE_MAX_RUNTIME_SECONDS
    login_node_allowed = (
        workload_dominance == FILE_ACCESS_DOMINANT and within_login_limit
    )
    if workload_dominance == COMPUTE_DOMINANT:
        reason = "compute-dominant analysis requires scheduler execution"
    elif not within_login_limit:
        reason = "analysis exceeds the six-hour login-node limit"
    else:
        reason = (
            "bounded file-access-dominant analysis is permitted on the login node"
        )
    return {
        "workload_dominance": workload_dominance,
        "runtime_limit_seconds": runtime_limit_seconds,
        "login_node_max_runtime_seconds": LOGIN_NODE_MAX_RUNTIME_SECONDS,
        "login_node_allowed": login_node_allowed,
        "execution_mode": "login" if login_node_allowed else "scheduler",
        "reason": reason,
    }


def _require_login_execution(
    workload_dominance: str,
    runtime_limit_seconds: float,
) -> dict[str, Any]:
    policy = analysis_execution_policy(workload_dominance, runtime_limit_seconds)
    if not policy["login_node_allowed"]:
        raise DataAnalysisError(policy["reason"])
    return policy


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


# This reducer is sent to the remote host and executed in the machine's
# configured analysis environment. It uses the XGC-Analysis fast catalog and
# reads only selected 1-D variables at the first and latest logical steps.
# No BP product is copied to the local machine.
_REMOTE_ONED_PROFILES = r"""
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xgc_analysis
from xgc_analysis.catalog import open_catalog

PRODUCT = "xgc.oneddiag.bp"
MAX_PROFILE_VARIABLES = 30
MAX_ESTIMATED_VALUES = 2_000_000

root = Path(sys.argv[1]).expanduser()
max_points = int(sys.argv[2])
if max_points < 16 or max_points > 2048:
    raise SystemExit("max_points must be between 16 and 2048")
if not root.exists() or not root.is_dir():
    raise SystemExit("run directory not found: " + str(root))

catalog = open_catalog(
    root,
    metadata_product_keys={PRODUCT},
    track_state=False,
)
if PRODUCT not in catalog.products:
    raise SystemExit("required product not found: " + PRODUCT)
product = catalog.get_product(PRODUCT)
available = set(product.variables)
if not available:
    raise SystemExit(PRODUCT + " contains no readable variables")

step_basis = next(
    (name for name in ("time", "gstep", "step", "psi") if name in available),
    None,
)
if step_basis is None:
    raise SystemExit(PRODUCT + " has no coordinate variable for step selection")
step_info = catalog.available_steps(PRODUCT, step_basis)
if not step_info:
    raise SystemExit(PRODUCT + " has no readable logical steps")
logical_steps = [int(step.logical_step) for step in step_info]
selected_steps = [logical_steps[0]]
if logical_steps[-1] != logical_steps[0]:
    selected_steps.append(logical_steps[-1])

coordinate_name = next(
    (name for name in ("psi", "psi00", "psi_mks") if name in available),
    None,
)
if coordinate_name is None:
    raise SystemExit(PRODUCT + " has no supported radial coordinate")
coordinate_steps = catalog.available_steps(PRODUCT, coordinate_name)
if not coordinate_steps:
    raise SystemExit(PRODUCT + " radial coordinate has no readable steps")
coordinate_step = int(coordinate_steps[0].logical_step)

profile_pattern = re.compile(
    r"^(e|i[2-9]?)_(gc_density_df_1d|perp_temperature_df_1d|parallel_flow_df_1d)$"
)
profile_names = sorted(name for name in available if profile_pattern.fullmatch(name))
if not profile_names:
    raise SystemExit(PRODUCT + " has no supported density/temperature/flow profiles")
if len(profile_names) > MAX_PROFILE_VARIABLES:
    raise SystemExit(
        "profile variable count exceeds safety limit: "
        + str(len(profile_names))
        + " > "
        + str(MAX_PROFILE_VARIABLES)
    )

scalar_names = [name for name in ("time", "gstep", "step") if name in available]
variables = [coordinate_name] + scalar_names + profile_names
read_steps = list(dict.fromkeys(selected_steps + [coordinate_step]))

estimated_values = 0
for name in variables:
    info = product.variables[name]
    shape = getattr(info, "shape_dims", None) or []
    count = 1
    for dim in shape:
        count *= max(1, int(dim))
    estimated_values += count * len(read_steps)
if estimated_values > MAX_ESTIMATED_VALUES:
    raise SystemExit(
        "selected read exceeds safety estimate: "
        + str(estimated_values)
        + " values > "
        + str(MAX_ESTIMATED_VALUES)
    )

read = catalog.read_arrays(
    PRODUCT,
    variables=variables,
    steps=read_steps,
    missing="skip",
)
arrays = read.arrays

coordinate_values = arrays.get(coordinate_name, {}).get(coordinate_step)
if coordinate_values is None:
    for step in read_steps:
        coordinate_values = arrays.get(coordinate_name, {}).get(step)
        if coordinate_values is not None:
            coordinate_step = int(step)
            break
if coordinate_values is None:
    raise SystemExit("failed to read radial coordinate " + coordinate_name)
coordinate_values = np.asarray(coordinate_values).squeeze()
if coordinate_values.ndim != 1 or coordinate_values.size < 2:
    raise SystemExit("radial coordinate is not a one-dimensional array")

if coordinate_values.size <= max_points:
    sample_indices = np.arange(coordinate_values.size, dtype=int)
else:
    sample_indices = np.unique(
        np.linspace(0, coordinate_values.size - 1, max_points, dtype=int)
    )

def finite_list(values):
    result = []
    for value in np.asarray(values).reshape(-1):
        number = float(value)
        result.append(number if math.isfinite(number) else None)
    return result

profiles = []
warnings = []
for name in profile_names:
    match = profile_pattern.fullmatch(name)
    species, quantity = match.groups()
    values_by_step = {}
    for logical_step in selected_steps:
        value = arrays.get(name, {}).get(logical_step)
        if value is None:
            warnings.append(name + " missing at logical step " + str(logical_step))
            continue
        value = np.asarray(value).squeeze()
        if value.ndim != 1 or value.size != coordinate_values.size:
            warnings.append(
                name
                + " shape "
                + str(tuple(value.shape))
                + " does not match coordinate length "
                + str(coordinate_values.size)
            )
            continue
        values_by_step[str(logical_step)] = finite_list(value[sample_indices])
    if not values_by_step:
        continue
    info = product.variables[name]
    metadata = getattr(info, "metadata", None)
    unit = getattr(metadata, "units", None) if metadata is not None else None
    profiles.append(
        {
            "variable": name,
            "species": species,
            "quantity": quantity,
            "unit": unit,
            "values_by_logical_step": values_by_step,
        }
    )

step_metadata = {}
for logical_step in selected_steps:
    values = {"logical_step": logical_step}
    for name in scalar_names:
        value = arrays.get(name, {}).get(logical_step)
        if value is None:
            continue
        flattened = np.asarray(value).reshape(-1)
        if flattened.size == 1:
            scalar = flattened[0].item()
            if isinstance(scalar, np.integer):
                scalar = int(scalar)
            elif isinstance(scalar, np.floating):
                scalar = float(scalar)
            values[name] = scalar
    step_metadata[str(logical_step)] = values

if not profiles:
    raise SystemExit("supported profiles were present but none could be reduced")

module_path = Path(xgc_analysis.__file__).resolve()
reader_commit = None
checkout_root = module_path.parent.parent
if (checkout_root / ".git").exists():
    commit_result = subprocess.run(
        ["git", "-C", str(checkout_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if commit_result.returncode == 0:
        reader_commit = commit_result.stdout.strip() or None

payload = {
    "schema": "xgc-oned-profiles/v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "remote_run_dir": str(root.resolve()),
    "source_product": PRODUCT,
    "reader": {
        "package": "xgc-analysis",
        "version": getattr(xgc_analysis, "__version__", "unknown"),
        "module_path": str(module_path),
        "git_commit": reader_commit,
        "catalog_mode": "selected-product-metadata",
        "track_state": False,
    },
    "selection": {
        "policy": "first-and-latest-logical-step",
        "available_step_count": len(logical_steps),
        "selected_logical_steps": selected_steps,
        "max_profile_points": max_points,
        "source_radial_point_count": int(coordinate_values.size),
        "returned_radial_point_count": int(sample_indices.size),
        "estimated_values_upper_bound": int(estimated_values),
    },
    "coordinate": {
        "variable": coordinate_name,
        "logical_step": coordinate_step,
        "values": finite_list(coordinate_values[sample_indices]),
    },
    "step_metadata": step_metadata,
    "profiles": profiles,
    "warnings": warnings,
}
print(json.dumps(payload, separators=(",", ":")))
"""


def _run_record(run_id: str) -> tuple[Path, dict[str, Any]]:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise DataAnalysisError(f"invalid run_id: {run_id!r}")
    try:
        found = prov.find_record(RUNS_DIR, run_id)
    except prov.ProvenanceError as exc:
        raise DataAnalysisError(str(exc)) from exc
    if found is None:
        raise DataAnalysisError(f"provenance not found for run {run_id!r}")
    run_dir, record_path = found
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

    timeout = 180.0
    execution = _require_login_execution(FILE_ACCESS_DOMINANT, timeout)
    _, remote = _remote_run_dir(machine, run_id)
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    # The inventory script needs only the stdlib, so the system python3 is an
    # acceptable fallback; prefer the machine's configured analysis python.
    python = (cfg.get("analysis") or {}).get("python", "python3")
    result = ssh.run(
        host,
        [python, "-c", _REMOTE_INVENTORY, remote, str(max_entries)],
        timeout=timeout,
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
    payload["execution"] = execution
    return payload


def _analysis_runtime(
    cfg: dict[str, Any],
    script: str,
    *args: str,
) -> tuple[list[str], str | None]:
    """Build the configured remote Python invocation for scientific reducers."""
    analysis = cfg.get("analysis", {})
    python = analysis.get("python", "python3")
    if not isinstance(python, str) or not python or m.is_placeholder(python):
        raise DataAnalysisError("analysis.python must name a remote Python executable")

    pythonpath = analysis.get("pythonpath")
    if isinstance(pythonpath, list):
        pythonpath = ":".join(str(path) for path in pythonpath)
    if pythonpath is not None and (
        not isinstance(pythonpath, str)
        or not pythonpath
        or m.is_placeholder(pythonpath)
    ):
        raise DataAnalysisError(
            "analysis.pythonpath must be a remote path or list of paths"
        )

    argv = [python, "-c", script, *args]
    if pythonpath:
        argv = ["env", f"PYTHONPATH={pythonpath}", *argv]

    env_script = analysis.get("env_script")
    if env_script is None:
        env_script = cfg.get("xgc", {}).get("env_script")
    if env_script is not None and (
        not isinstance(env_script, str) or m.is_placeholder(env_script)
    ):
        raise DataAnalysisError("analysis.env_script must be a remote path")
    return argv, env_script


def inspect_oned_profiles(
    machine: str,
    run_id: str,
    *,
    max_points: int = DEFAULT_MAX_PROFILE_POINTS,
) -> dict[str, Any]:
    """Reduce first/latest 1-D profiles remotely through XGC-Analysis."""
    if max_points < MIN_PROFILE_POINTS or max_points > MAX_PROFILE_POINTS:
        raise DataAnalysisError(
            f"max_points must be between {MIN_PROFILE_POINTS} and "
            f"{MAX_PROFILE_POINTS}"
        )

    timeout = 300.0
    execution = _require_login_execution(FILE_ACCESS_DOMINANT, timeout)
    _, remote = _remote_run_dir(machine, run_id)
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    argv, env_script = _analysis_runtime(
        cfg,
        _REMOTE_ONED_PROFILES,
        remote,
        str(max_points),
    )
    result = ssh.run(
        host,
        argv,
        timeout=timeout,
        env_script=env_script,
    )
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()[-2_000:]
        if "No module named 'xgc_analysis'" in detail:
            detail += (
                "\nInstall the updated XGC-Analysis branch in the remote "
                "analysis environment or set analysis.pythonpath in machines.yaml."
            )
        raise DataAnalysisError(
            f"remote oned_profiles reduction failed for {run_id!r} "
            f"(rc={result.returncode}): {detail}"
        )
    encoded = result.stdout.encode("utf-8")
    if len(encoded) > MAX_SCIENTIFIC_SUMMARY_BYTES:
        raise DataAnalysisError(
            "remote oned_profiles summary exceeded "
            f"{MAX_SCIENTIFIC_SUMMARY_BYTES} bytes; reduce max_points"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DataAnalysisError(
            f"remote oned_profiles reducer returned invalid JSON: {exc}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "xgc-oned-profiles/v1"
        or not isinstance(payload.get("profiles"), list)
        or not isinstance(payload.get("coordinate", {}).get("values"), list)
    ):
        raise DataAnalysisError(
            "remote oned_profiles reducer returned an unsupported schema"
        )

    payload["run_id"] = run_id
    payload["machine"] = machine
    payload["execution"] = execution
    return payload


_QUANTITY_LABELS = {
    "gc_density_df_1d": "Guiding-center density",
    "perp_temperature_df_1d": "Perpendicular temperature",
    "parallel_flow_df_1d": "Parallel flow",
}
_PLOT_COLORS = (
    "#0072B2",
    "#D55E00",
    "#009E73",
    "#CC79A7",
    "#E69F00",
    "#56B4E9",
    "#000000",
)


def _oned_profiles_svg(payload: dict[str, Any]) -> str:
    """Render a dependency-free local SVG from a bounded profile summary."""
    coordinate = payload["coordinate"]
    x_values = coordinate["values"]
    grouped: dict[str, list[tuple[str, list[Any]]]] = {}
    for profile in payload["profiles"]:
        quantity = str(profile.get("quantity", "unknown"))
        for step, values in profile.get("values_by_logical_step", {}).items():
            label = f"{profile.get('species', '?')} step {step}"
            grouped.setdefault(quantity, []).append((label, values))
    grouped = {name: series for name, series in grouped.items() if series}
    if not grouped:
        raise DataAnalysisError("oned_profiles summary contains no plottable series")

    width = 1_080
    margin_left = 90
    margin_right = 220
    panel_height = 230
    margin_top = 50
    margin_bottom = 55
    panel_gap = 35
    plot_width = width - margin_left - margin_right
    height = (
        margin_top
        + len(grouped) * panel_height
        + (len(grouped) - 1) * panel_gap
        + margin_bottom
    )

    finite_x = [float(value) for value in x_values if value is not None]
    if len(finite_x) < 2:
        raise DataAnalysisError("oned_profiles coordinate has fewer than two points")
    x_min, x_max = min(finite_x), max(finite_x)
    if x_min == x_max:
        raise DataAnalysisError("oned_profiles coordinate range is zero")

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        '<rect width="100%" height="100%" fill="white"/>',
        (
            f'<text x="{margin_left}" y="28" font-family="sans-serif" '
            f'font-size="18" font-weight="bold">'
            f'{escape(str(payload.get("run_id", "XGC run")))}: 1-D profiles</text>'
        ),
    ]

    for panel_index, (quantity, series) in enumerate(sorted(grouped.items())):
        panel_top = margin_top + panel_index * (panel_height + panel_gap)
        panel_bottom = panel_top + panel_height
        finite_y = [
            float(value)
            for _, values in series
            for value in values
            if value is not None and math.isfinite(float(value))
        ]
        if not finite_y:
            continue
        y_min, y_max = min(finite_y), max(finite_y)
        if y_min == y_max:
            pad = abs(y_min) * 0.05 or 1.0
            y_min -= pad
            y_max += pad

        parts.extend(
            [
                (
                    f'<rect x="{margin_left}" y="{panel_top}" width="{plot_width}" '
                    f'height="{panel_height}" fill="#fafafa" stroke="#777"/>'
                ),
                (
                    f'<text x="{margin_left}" y="{panel_top - 10}" '
                    f'font-family="sans-serif" font-size="14" font-weight="bold">'
                    f'{escape(_QUANTITY_LABELS.get(quantity, quantity))}</text>'
                ),
                (
                    f'<text x="{margin_left - 8}" y="{panel_top + 12}" '
                    f'text-anchor="end" font-family="monospace" font-size="11">'
                    f'{y_max:.4g}</text>'
                ),
                (
                    f'<text x="{margin_left - 8}" y="{panel_bottom}" '
                    f'text-anchor="end" font-family="monospace" font-size="11">'
                    f'{y_min:.4g}</text>'
                ),
            ]
        )

        for series_index, (label, values) in enumerate(series):
            points = []
            for x_value, y_value in zip(x_values, values):
                if x_value is None or y_value is None:
                    continue
                x_number, y_number = float(x_value), float(y_value)
                if not (math.isfinite(x_number) and math.isfinite(y_number)):
                    continue
                px = margin_left + (x_number - x_min) / (x_max - x_min) * plot_width
                py = panel_bottom - (y_number - y_min) / (y_max - y_min) * panel_height
                points.append(f"{px:.2f},{py:.2f}")
            if len(points) < 2:
                continue
            color = _PLOT_COLORS[series_index % len(_PLOT_COLORS)]
            dash = ' stroke-dasharray="7 4"' if series_index % 2 else ""
            parts.append(
                f'<polyline points="{" ".join(points)}" fill="none" '
                f'stroke="{color}" stroke-width="1.7"{dash}/>'
            )
            legend_y = panel_top + 18 + series_index * 16
            parts.extend(
                [
                    (
                        f'<line x1="{margin_left + plot_width + 15}" y1="{legend_y}" '
                        f'x2="{margin_left + plot_width + 43}" y2="{legend_y}" '
                        f'stroke="{color}" stroke-width="2"{dash}/>'
                    ),
                    (
                        f'<text x="{margin_left + plot_width + 50}" '
                        f'y="{legend_y + 4}" font-family="sans-serif" '
                        f'font-size="11">{escape(label)}</text>'
                    ),
                ]
            )

    parts.extend(
        [
            (
                f'<text x="{margin_left}" y="{height - 15}" '
                f'font-family="sans-serif" font-size="12">'
                f'{escape(str(coordinate.get("variable", "radial coordinate")))} '
                f'[{x_min:.4g}, {x_max:.4g}]</text>'
            ),
            "</svg>",
        ]
    )
    return "\n".join(parts) + "\n"


def analyze_run(
    machine: str,
    run_id: str,
    *,
    product: str = "inventory",
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_profile_points: int = DEFAULT_MAX_PROFILE_POINTS,
    save: bool = True,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Create one reduced product and save it beside the run provenance.

    Creating a new derived summary is safe and local. Replacing an existing
    summary requires ``confirm_overwrite=True`` so callers cannot silently
    destroy an earlier result.
    """
    if product not in {"inventory", "oned_profiles"}:
        raise DataAnalysisError(
            f"unsupported product {product!r}; available products: "
            "['inventory', 'oned_profiles']"
        )

    run_dir, _ = _run_record(run_id)
    if product == "inventory":
        payload = inspect_run(machine, run_id, max_entries=max_entries)
    else:
        payload = inspect_oned_profiles(
            machine,
            run_id,
            max_points=max_profile_points,
        )
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
    plot_path = (
        summary_dir / f"{product}.svg" if product == "oned_profiles" else None
    )
    existing_paths = [
        path
        for path in (output_path, plot_path)
        if path is not None and path.exists()
    ]
    if existing_paths and not confirm_overwrite:
        result.update(
            {
                "ok": False,
                "reason": (
                    "summary artifact already exists; review it and set "
                    "confirm_overwrite=True to replace it"
                ),
                "output_path": str(output_path),
                "existing_paths": [str(path) for path in existing_paths],
            }
        )
        return result

    plot_document = _oned_profiles_svg(payload) if plot_path is not None else None
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
    if plot_path is not None and plot_document is not None:
        plot_path.write_text(plot_document, encoding="utf-8")
        result["plot_path"] = str(plot_path)
    return result
