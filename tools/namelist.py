"""Fortran namelist access and XGC parameter metadata.

Two responsibilities:

1. Read/write the `input` namelist file on a remote machine.
2. Provide the parameter dictionary XGC generates from its own C++ source --
   every parameter's type, default, description, and valid options.

Both reuse XGC's own code (`utils/input_gui/namelist_io.py` and
`utils/input_gui/metadata.py`) rather than reimplementing a namelist parser.
That code is what the XGC input GUI uses, so a file written here opens cleanly
in the GUI and vice versa. It also means the metadata tracks the C++ source
instead of drifting from it.

The catch is that the metadata is derived from the XGC source tree, which lives
on the remote machine, while this tool runs locally. So we export it once as
JSON over ssh and cache it under .cache/. The cache is keyed by the XGC commit,
so it refreshes automatically when the source changes.

The remote interpreter must be new enough to import XGC's utils (login-node
default python3 is often too old: stellar ships 3.6, where
`from __future__ import annotations` fails). Each machine therefore declares
`xgc.remote_python` in machines.yaml / machines.local.yaml.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import machines as m
from tools import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache"


def _remote_python_for(machine: str, cfg: dict[str, Any]) -> str:
    """The configured remote interpreter for XGC's utils on this machine."""
    candidate = cfg.get("xgc", {}).get("remote_python")
    if not candidate or m.is_placeholder(candidate):
        raise NamelistError(
            f"xgc.remote_python is not configured for {machine!r}. Set it in "
            f"machines.yaml (shared install) or machines.local.yaml (personal "
            f"venv) to a python >= 3.7 that can import XGC's utils/input_gui. "
            f"The login-node default python3 is often too old."
        )
    return candidate


class NamelistError(RuntimeError):
    """Namelist parsing, metadata extraction, or validation failed."""


@dataclass
class ParamInfo:
    """What XGC's own source says about one input parameter."""

    name: str
    namelist: str
    param_type: str
    default_value: str
    description: str
    options: list[str]          # valid values for enum-like parameters
    array_kind: str             # e.g. "per_species", or "" if scalar

    @property
    def is_array(self) -> bool:
        return bool(self.array_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namelist": self.namelist,
            "type": self.param_type,
            "default": self.default_value,
            "description": self.description,
            "options": self.options,
            "array_kind": self.array_kind,
        }


# Script run on the remote to dump metadata as JSON. Kept as a literal rather
# than a file to avoid needing to push anything to the remote first.
_METADATA_EXPORT = r"""
import json, sys
sys.path.insert(0, SRC)
from pathlib import Path
from utils.input_gui import metadata
md = metadata.load_metadata(Path(SRC))
out = {}
for nl_name, nl in md.items():
    params = {}
    for p_name, p in nl.parameters.items():
        params[p_name] = {
            "type": p.param_type,
            "default": p.default_value,
            "description": p.description,
            "options": [o.get("value", "") for o in (p.mult_options or [])],
            "array_kind": p.array_kind,
        }
    out[nl_name] = {"description": nl.description, "parameters": params}
print(json.dumps(out))
"""

_NAMELIST_READ = r"""
import json, sys
sys.path.insert(0, SRC)
from pathlib import Path
from utils.input_gui.namelist_io import parse_namelist_file
print(json.dumps(parse_namelist_file(Path(TARGET))))
"""


def _remote_python(machine: str, code: str, **subs: str) -> str:
    """Run a Python snippet on the remote with XGC's utils importable.

    Substitutions are injected as quoted literals rather than formatted into
    the source, so paths cannot break out of the string.
    """
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    src = cfg.get("xgc", {}).get("src")
    if not src or m.is_placeholder(src):
        raise NamelistError(
            f"xgc.src is not configured for {machine!r}; needed to import XGC's "
            f"namelist utilities."
        )

    preamble = f"SRC = {src!r}\n"
    for key, value in subs.items():
        preamble += f"{key} = {value!r}\n"

    result = ssh.run(
        host,
        [_remote_python_for(machine, cfg), "-c", preamble + code],
        timeout=180.0,
    )
    if not result.ok:
        raise NamelistError(
            f"remote python failed (rc={result.returncode}): {result.stderr.strip()[-500:]}"
        )
    # The login shell prints module banners to stderr; stdout carries the JSON.
    return result.stdout


def _xgc_commit(machine: str) -> str:
    """Short commit of the remote XGC source, used as the cache key."""
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    src = cfg.get("xgc", {}).get("src")
    result = ssh.run(host, ["git", "-C", src, "rev-parse", "--short", "HEAD"])
    return result.stdout.strip() if result.ok else "unknown"


def load_metadata(machine: str, *, refresh: bool = False) -> dict[str, Any]:
    """Load XGC's parameter metadata, caching it locally by commit.

    Parsing takes ~0.4 s remotely, but an ssh round trip per lookup would make
    interactive use sluggish, so the result is cached. A new commit produces a
    new cache file, so stale metadata is not silently reused.
    """
    commit = _xgc_commit(machine)
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"metadata-{machine}-{commit}.json"

    if cache_file.exists() and not refresh:
        return json.loads(cache_file.read_text(encoding="utf-8"))

    raw = _remote_python(machine, _METADATA_EXPORT)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NamelistError(f"could not parse metadata JSON: {exc}\n{raw[:400]}")

    cache_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def read_namelist(machine: str, remote_path: str) -> dict[str, dict[str, str]]:
    """Parse a remote namelist file into {namelist: {param: raw_value}}."""
    raw = _remote_python(machine, _NAMELIST_READ, TARGET=remote_path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NamelistError(f"could not parse namelist JSON: {exc}\n{raw[:400]}")


def find_parameter(
    metadata: dict[str, Any], name: str, *, namelist: str | None = None
) -> ParamInfo | None:
    """Look up a parameter by name, searching all namelists unless one is given.

    Parameter names are unique across namelists in practice, so callers do not
    have to know which namelist a parameter belongs to.
    """
    candidates = [namelist] if namelist else list(metadata.keys())
    for nl_name in candidates:
        nl = metadata.get(nl_name)
        if not nl:
            continue
        entry = nl["parameters"].get(name)
        if entry:
            return ParamInfo(
                name=name,
                namelist=nl_name,
                param_type=entry["type"],
                default_value=entry["default"],
                description=entry["description"],
                options=entry.get("options") or [],
                array_kind=entry.get("array_kind") or "",
            )
    return None


def suggest_names(metadata: dict[str, Any], name: str, limit: int = 5) -> list[str]:
    """Close matches for an unknown parameter name, for error messages."""
    import difflib

    all_names = [p for nl in metadata.values() for p in nl["parameters"]]
    return difflib.get_close_matches(name, all_names, n=limit, cutoff=0.6)


def _fortran_bool(value: Any) -> str | None:
    """Render a Python bool as Fortran, or None if the value is not a bool."""
    if isinstance(value, bool):
        return ".true." if value else ".false."
    if isinstance(value, str) and value.strip().lower() in {
        ".true.", ".false.", "t", "f", "true", "false",
    }:
        text = value.strip().lower()
        return ".true." if text in {".true.", "t", "true"} else ".false."
    return None


def format_value(value: Any, info: ParamInfo | None) -> str:
    """Render a Python value as a Fortran namelist literal.

    Types come from XGC's metadata, so a bool is written `.true.` rather than
    `True` and a string is quoted -- the two mistakes that produce a file XGC
    silently misreads.
    """
    ptype = (info.param_type if info else "").lower()

    if "bool" in ptype:
        rendered = _fortran_bool(value)
        if rendered is None:
            raise NamelistError(
                f"{info.name if info else 'value'} is a boolean; got {value!r}"
            )
        return rendered

    if isinstance(value, (list, tuple)):
        return " ".join(format_value(v, info) for v in value)

    if "string" in ptype or "char" in ptype:
        text = str(value)
        if text.startswith("'") and text.endswith("'"):
            return text
        return f"'{text}'"

    if isinstance(value, bool):
        # A bool for a non-bool parameter is a caller mistake worth surfacing.
        raise NamelistError(
            f"{info.name if info else 'value'} has type {ptype!r}; got a boolean"
        )

    return str(value)


def validate_value(value: Any, info: ParamInfo) -> list[str]:
    """Check a value against the metadata. Returns a list of problems."""
    problems: list[str] = []
    ptype = info.param_type.lower()

    if info.options:
        text = str(value).strip().strip("'\"")
        # Array parameters carry one value per species.
        parts = text.split() if info.is_array else [text]
        for part in parts:
            cleaned = part.strip().strip("'\"")
            if cleaned and cleaned not in info.options:
                problems.append(
                    f"{info.name}: {cleaned!r} is not a valid option; "
                    f"expected one of {info.options}"
                )

    if "bool" in ptype and _fortran_bool(value) is None:
        problems.append(f"{info.name}: expected a boolean, got {value!r}")

    if ("int" in ptype) and not isinstance(value, bool):
        try:
            int(str(value).strip())
        except ValueError:
            if not info.is_array:
                problems.append(f"{info.name}: expected an integer, got {value!r}")

    if ("double" in ptype or "float" in ptype) and not isinstance(value, bool):
        text = str(value).strip()
        # Fortran doubles use D exponents (2.0D-4), which Python cannot parse.
        try:
            float(text.replace("D", "E").replace("d", "e"))
        except ValueError:
            if not info.is_array:
                problems.append(f"{info.name}: expected a number, got {value!r}")

    return problems


def render_namelist_line(param: str, value: str) -> str:
    return f"{param}={value}"


def apply_overrides(
    text: str, overrides: dict[str, str], metadata: dict[str, Any]
) -> tuple[str, list[str]]:
    """Apply parameter overrides to raw namelist text.

    Edits the text in place rather than reserializing, so comments, ordering,
    and formatting survive -- important because these files are hand-maintained
    and reviewed as diffs. A parameter not already present is inserted into its
    namelist block.

    Returns (new_text, notes).
    """
    import re

    lines = text.splitlines(keepends=True)
    notes: list[str] = []
    remaining = dict(overrides)

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("!") or stripped.startswith("&"):
            continue
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^!]*)(!.*)?$", line)
        if not match:
            continue
        indent, name, old_value, comment = match.groups()
        if name not in remaining:
            continue
        new_value = remaining.pop(name)
        comment = comment or ""
        sep = "  " if comment else ""
        lines[idx] = f"{indent}{name}={new_value}{sep}{comment}\n"
        notes.append(f"{name}: {old_value.strip()} -> {new_value}")

    # Anything left was not in the file; insert it into the right namelist.
    for name, new_value in remaining.items():
        info = find_parameter(metadata, name)
        if info is None:
            raise NamelistError(f"unknown parameter {name!r}; cannot place it")
        target = f"&{info.namelist}"
        placed = False
        for idx, line in enumerate(lines):
            if line.strip().startswith(target):
                lines.insert(idx + 1, f"{name}={new_value}\n")
                notes.append(f"{name}: (absent) -> {new_value}  [added to {target}]")
                placed = True
                break
        if not placed:
            raise NamelistError(
                f"namelist &{info.namelist} not found in the file; "
                f"cannot add {name}"
            )

    return "".join(lines), notes
