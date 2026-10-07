"""Where the repository's code ends and one user's state begins.

The repository holds two kinds of files. **Shared** files are the same for
every user: the tools, the documentation, ``machines.yaml``, the job
templates and the validated baseline cases in ``kb/cases/``. **State** files
belong to one researcher and are written by the automation: the simulation
catalog and registry, the run provenance under ``runs/``, ``input_backup/``,
the f_init and flow_match case logs, the historical notebook and the caches.

By default the state lives inside the checkout, exactly where it always has.
A user who keeps it elsewhere -- another directory, or a catalog repository of
their own shared between a laptop and a cluster checkout -- points the tools
at it once:

    export XGC_CATALOG_DIR=~/xgc-catalog          # environment, or
    ~/.config/xgc-automation/config.yaml:  catalog_dir: ~/xgc-catalog

and every state path below follows. ``xgc-sim paths`` prints the resolved
layout. Module-level constants elsewhere (``hpc_runner.RUNS_DIR``,
``simulation_catalog.CATALOG_DIR``, ...) are initialised from here so tests
can keep patching them one module at a time.

Two older variables keep working as overrides of a single path:
``XGC_SIMULATION_CATALOG`` (the records directory) and
``XGC_SIMULATION_NOTEBOOK`` (the historical xlsx notebook).

**Data directory.** Analysis products (PNG, NPZ, SVG, pulled job logs) are
regenerable, undiffable and large; they are what made the catalog repository
grow (130 MB of history in its first day, 95% images and arrays). They live
outside git in the data directory, ``XGC_DATA_DIR`` or ``data_dir`` in the
config file, default ``<state root>/data``, with the **same relative layout**
as the state: ``product_target(STATE_ROOT / "runs/master/run-x/summaries/a.png")``
is ``DATA_ROOT / "runs/master/run-x/summaries/a.png"``. Writers call
``product_target`` on every output path and small text products (JSON,
scripts, provenance) stay where they are.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STATE_ENV_VAR = "XGC_CATALOG_DIR"
DATA_ENV_VAR = "XGC_DATA_DIR"
CONFIG_ENV_VAR = "XGC_AUTOMATION_CONFIG"
DEFAULT_CONFIG_FILE = Path("~/.config/xgc-automation/config.yaml")
CONFIG_KEY = "catalog_dir"
DATA_CONFIG_KEY = "data_dir"

# File types that never enter the catalog repository (see module docstring).
PRODUCT_SUFFIXES = frozenset({".png", ".npz", ".out", ".svg", ".pdf", ".h5", ".nc"})


def config_file(env: Mapping[str, str] | None = None) -> Path:
    """The per-user config file (``XGC_AUTOMATION_CONFIG`` overrides the default)."""
    env = os.environ if env is None else env
    return Path(env.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_FILE).expanduser()


def _config_value(path: Path, key: str) -> Path | None:
    """``key`` from the YAML config file as a path (relative to the file), or None."""
    if not path.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    value = data.get(key) if isinstance(data, dict) else None
    if not value:
        return None
    root = Path(str(value)).expanduser()
    if not root.is_absolute():
        root = path.parent / root
    return root.resolve()


def resolve_state_root(
    env: Mapping[str, str] | None = None, *, config: Path | None = None
) -> tuple[Path, str]:
    """Return ``(state_root, source)``; ``source`` says which setting decided.

    Precedence: ``XGC_CATALOG_DIR`` > ``catalog_dir`` in the config file > the
    repository itself. A relative value is taken from the current directory
    for the environment variable and from the config file's directory for the
    file, so a config can say ``catalog_dir: ../xgc-catalog``.
    """
    env = os.environ if env is None else env
    value = env.get(STATE_ENV_VAR)
    if value:
        return Path(value).expanduser().resolve(), f"${STATE_ENV_VAR}"

    path = config_file(env) if config is None else config
    root = _config_value(path, CONFIG_KEY)
    if root is not None:
        return root, str(path)

    return PROJECT_ROOT, "repository (default)"


def resolve_data_root(
    env: Mapping[str, str] | None = None,
    *,
    config: Path | None = None,
    state_root: Path | None = None,
) -> tuple[Path, str]:
    """Return ``(data_root, source)``: ``XGC_DATA_DIR`` > ``data_dir`` > ``<state>/data``."""
    env = os.environ if env is None else env
    value = env.get(DATA_ENV_VAR)
    if value:
        return Path(value).expanduser().resolve(), f"${DATA_ENV_VAR}"
    path = config_file(env) if config is None else config
    root = _config_value(path, DATA_CONFIG_KEY)
    if root is not None:
        return root, str(path)
    base = STATE_ROOT if state_root is None else state_root
    return base / "data", "<state root>/data (default)"


STATE_ROOT, STATE_ROOT_SOURCE = resolve_state_root()
DATA_ROOT, DATA_ROOT_SOURCE = resolve_data_root()


def is_product(path: Path | str) -> bool:
    """Whether a file belongs in the data directory rather than in git."""
    return Path(path).suffix.lower() in PRODUCT_SUFFIXES


def product_target(
    path: Path | str,
    *,
    state_root: Path | None = None,
    data_root: Path | None = None,
) -> Path:
    """Where to write ``path``: the data directory (same relative layout) for a
    product under the state root, the path itself for everything else.

    Callers write JSON, scripts and provenance where they always did and pass
    every output path through here; only images, arrays and logs move.
    """
    target = Path(path)
    state = STATE_ROOT if state_root is None else state_root
    data = DATA_ROOT if data_root is None else data_root
    if not is_product(target):
        return target
    try:
        relative = target.resolve().relative_to(state.resolve())
    except ValueError:
        return target  # not under the state root: leave it alone
    return data / relative

# --- shared: always inside the repository -----------------------------------
MACHINES_FILE = PROJECT_ROOT / "machines.yaml"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
CASES_DIR = PROJECT_ROOT / "kb" / "cases"

# --- state: follow STATE_ROOT ------------------------------------------------
KB_DIR = STATE_ROOT / "kb"
CATALOG_DIR = Path(os.environ.get("XGC_SIMULATION_CATALOG") or KB_DIR / "simulations")
REGISTRY_PATH = KB_DIR / "simulation_registry.yaml"
NOTEBOOK_PATH = KB_DIR / "simulation_notebook.csv"
BAD_NODES_FILE = KB_DIR / "bad_nodes.yaml"
DRAFTS_DIR = KB_DIR / "cases" / "drafts"
RUNS_DIR = STATE_ROOT / "runs"
ADOPTED_DIR = RUNS_DIR / "adopted"
INPUT_BACKUP_DIR = STATE_ROOT / "input_backup"
CACHE_DIR = STATE_ROOT / ".cache"
HISTORICAL_NOTEBOOK = Path(
    os.environ.get("XGC_SIMULATION_NOTEBOOK")
    or STATE_ROOT / "Other_data" / "simulation_note_XGC1.xlsx"
)
F_INIT_CASES_DIR = STATE_ROOT / "f_init" / "cases"
FLOW_MATCH_CASES_DIR = STATE_ROOT / "flow_match" / "cases"


def _local_machines_file() -> Path:
    """``machines.local.yaml``: the checkout's copy wins, else the state root's.

    The overlay is personal, so it may live with the rest of the state; the
    checkout-local file stays first so existing setups keep working.
    """
    in_repo = PROJECT_ROOT / "machines.local.yaml"
    if in_repo.exists() or STATE_ROOT == PROJECT_ROOT:
        return in_repo
    return STATE_ROOT / "machines.local.yaml"


LOCAL_MACHINES_FILE = _local_machines_file()


def layout() -> list[dict[str, Any]]:
    """The resolved layout, for ``xgc-sim paths`` and diagnostics."""
    rows = [
        ("project_root", PROJECT_ROOT, "shared"),
        ("machines_file", MACHINES_FILE, "shared"),
        ("local_machines_file", LOCAL_MACHINES_FILE, "state"),
        ("templates", TEMPLATE_DIR, "shared"),
        ("cases", CASES_DIR, "shared"),
        ("state_root", STATE_ROOT, "state"),
        ("data_root", DATA_ROOT, "data"),
        ("catalog", CATALOG_DIR, "state"),
        ("registry", REGISTRY_PATH, "state"),
        ("notebook_csv", NOTEBOOK_PATH, "state"),
        ("bad_nodes", BAD_NODES_FILE, "state"),
        ("case_drafts", DRAFTS_DIR, "state"),
        ("runs", RUNS_DIR, "state"),
        ("adopted_runs", ADOPTED_DIR, "state"),
        ("input_backup", INPUT_BACKUP_DIR, "state"),
        ("cache", CACHE_DIR, "state"),
        ("historical_notebook", HISTORICAL_NOTEBOOK, "state"),
        ("f_init_cases", F_INIT_CASES_DIR, "state"),
        ("flow_match_cases", FLOW_MATCH_CASES_DIR, "state"),
    ]
    return [
        {"name": name, "path": str(path), "kind": kind, "exists": path.exists()}
        for name, path, kind in rows
    ]
