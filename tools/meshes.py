"""Mesh recipes and records: what each XGC mesh was made from, by which
TOMMS build, and which simulations used it.

An XGC mesh (``.node``, ``.ele``, ``.flx.aif``) is produced by ``tomms_meshgen``
from a small recipe: the ``xgc_mesh_in`` parameter file plus the files it
names, a radial surface list (``surf.txt``), a poloidal spacing table
(``dpol.txt``), an optional private-region surface list, a limiter curve, and
the equilibrium (geqdsk or ``.eqd``). The outputs are large and regenerable;
the recipe is a few kilobytes of text and is what a year later nobody can
reconstruct from a mesh file name. This module keeps the recipe.

Two homes, as for the shot index (``tools/shots.py``):

* **records and recipes** in the catalog repository,
  ``kb/meshes/<mesh_id>.yaml`` beside ``kb/meshes/<mesh_id>/`` holding copies
  of ``xgc_mesh_in`` and its small text inputs, plus failed variants under
  ``attempts/`` (``paths.MESHES_DIR``);
* **equilibria, pulled outputs and previews** in the data directory,
  ``<data_dir>/meshes/`` (``paths.DATA_ROOT``), never in git. Outputs are
  recorded by sha256 and only pulled on demand (preview, deploy).

``used_by`` is derived, never typed: every submission records the sha256 of
the mesh files its input references (``referenced_inputs.files``) and
:func:`link` joins the two by hash, so a renamed mesh file keeps its history.

Three entry points:

* :func:`import_from_remote` walks a mesh work tree on a machine (one
  directory per equilibrium, ``xgc_mesh_in`` edited in place and dated
  variants kept beside it), attributes each output triple to the variant
  that produced it by deterministic rules (``nodeFile`` match, variant name
  token in the output name, ``nphiN`` token, single candidate), and records
  the rest as ``ambiguous`` for the human to settle. Nothing is guessed about
  the device beyond what the directory and file names say, and the basis is
  recorded.
* :func:`plan_generation` / :func:`start_generation` / :func:`check_generation`
  run ``tomms_meshgen`` on the configured machine (``machines.yaml``
  ``<machine>.mesh``), detached on the login node, from a recipe derived from
  an existing record with overrides. The plan is shown and confirmed before
  anything is written remotely (absolute rule 1).
* :func:`deploy` copies a mesh's outputs to a simulation machine and verifies
  the hashes there, so the next ``stage_simulation_case`` can reference them
  and the hash join above closes the loop.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import yaml

from tools import machines as m
from tools import paths
from tools import simulation_catalog as catalog

SCHEMA_VERSION = 1
RECIPE_NAME = "xgc_mesh_in"
STATUSES = ("ok", "failed", "running", "unknown")

# xgc_mesh_in keys naming a small text input, and the role each plays.
FILE_KEYS = {
    "inter_curve_spacing_file": "surf",
    "intra_curve_spacing_file": "dpol",
    "inter_curve_spacing_private_file": "private_surf",
    "limiterFile": "limiter",
    "eq_tempi_file": "ti_profile",
}
EQUILIBRIUM_KEY = "eqdskFile"
OUTPUT_KEYS = {"nodeFile": ".node", "elmFile": ".ele", "addinfoflxFile": ".flx.aif"}
OUTPUT_SUFFIXES = (".node", ".ele", ".flx.aif")
# Parameters worth showing in a listing (the full set is kept in `parameters`).
HEADLINE_KEYS = (
    "num_phi_planes",
    "intra_curve_spacing_option",
    "radial_uniform_meshing_unit",
    "reverse_psi",
    "grid_bdry_psi2",
    "grid_bdry_psi2hfs",
    "inter_curve_spacing_sol",
    "fluxC_in_privateR_on",
    "finite_domain_on",
)
MAX_RECIPE_FILE_BYTES = 1 << 20  # a surf/dpol/limiter file is a few KB; refuse to copy more
MAX_PREVIEW_BYTES = 200 << 20  # node+ele pulled for a preview; ITER meshes exceed this
TOMMS_LOG = "tomms.out"
TOMMS_RC = "tomms.rc"
_FILE_MARK = "@@@XGC-AUTOMATION-FILE@@@"


class MeshIndexError(ValueError):
    pass


# --------------------------------------------------------------------------
# xgc_mesh_in


def parse_mesh_in(text: str) -> dict[str, str]:
    """Key/value pairs of an ``xgc_mesh_in``; ``!`` starts a comment, the last
    assignment of a key wins, ``key=value`` and ``key value`` both occur."""
    params: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("!", 1)[0].strip()
        if not line:
            continue
        if "=" in line and not line.split("=", 1)[0].strip().count(" "):
            key, value = line.split("=", 1)
        else:
            parts = line.split(None, 1)
            key, value = parts[0], parts[1] if len(parts) > 1 else ""
        params[key.strip()] = value.strip()
    return params


def render_mesh_in(text: str, overrides: dict[str, str | None]) -> str:
    """``text`` with each key in ``overrides`` set in place (comments kept);
    ``None`` comments the line out; keys not present are appended."""
    pending = dict(overrides)
    out: list[str] = []
    for raw in text.splitlines():
        code = raw.split("!", 1)[0].strip()
        key = None
        if code:
            key = code.split("=", 1)[0].strip() if "=" in code and " " not in code.split("=", 1)[0].strip() else code.split(None, 1)[0]
        if key is not None and key in pending:
            value = pending.pop(key)
            out.append(f"!{raw}" if value is None else f"{key} {value}")
        else:
            out.append(raw)
    if pending:
        out.append("")
        out.append("! set by xgc-automation")
        for key, value in pending.items():
            if value is not None:
                out.append(f"{key} {value}")
    return "\n".join(out) + "\n"


def recipe_files(params: dict[str, str]) -> dict[str, str]:
    """Role -> file name for every small input the recipe names."""
    return {role: params[key] for key, role in FILE_KEYS.items() if params.get(key)}


def output_prefix(params: dict[str, str]) -> str | None:
    """The output prefix the recipe sets (``nodeFile`` without ``.node``), if any."""
    node = params.get("nodeFile", "")
    return node[: -len(".node")] if node.endswith(".node") else (node or None)


def geometry(params: dict[str, str]) -> dict[str, Any]:
    planes = _int(params.get("num_phi_planes"))
    option = _int(params.get("intra_curve_spacing_option"))
    return {
        "planes": planes,
        "axisymmetric": None if planes is None else planes == 1,
        "field_aligned": None if option is None else option == -1,
        "radial_mode": _int(params.get("radial_uniform_meshing_unit")),
        "poloidal_option": option,
    }


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except ValueError:
        return None


def headline(params: dict[str, str]) -> dict[str, str]:
    return {key: params[key] for key in HEADLINE_KEYS if key in params}


def diff_parameters(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str | None]]:
    """``{key: [before, after]}`` for every key that differs."""
    changed: dict[str, list[str | None]] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed[key] = [before.get(key), after.get(key)]
    return changed


# --------------------------------------------------------------------------
# device inference (names only; the basis is recorded, never hidden)

_DEVICE_PATTERNS: list[tuple[str, str]] = [
    (r"tcv(?:[_-]?(\d{5}))?", "tcv"),
    (r"kstar(?:[_-]?0*(\d{5,6}))?", "kstar"),
    (r"nstx(?:[_-]?0*(\d{6}))?", "nstx"),
    (r"st[-_]?40(?:[_-]?(\d{8}))?", "st40"),
    (r"jet(?:[_-]?(\d{5}))?", "jet"),
    (r"asdex|aug[_-]?(\d{5})|au(\d{5})", "asdexu"),
    (r"c[-_]?mod", "cmod"),
    (r"iter", "iter"),
    (r"cyclone|cbc", "cyclone"),
    (r"(?:^|[_/])[dg](1\d{5})[._]", "d3d"),
    (r"d3d|diii", "d3d"),
]


def infer_device(*names: str) -> dict[str, Any]:
    """Device and shot read from directory and file names; ``basis`` says which."""
    for name in names:
        if not name:
            continue
        low = name.lower()
        for pattern, device in _DEVICE_PATTERNS:
            match = re.search(pattern, low)
            if match:
                shot = next((g for g in match.groups() if g), None)
                if device == "asdexu" and shot is None:
                    inner = re.search(r"(\d{5})", low)
                    shot = inner.group(1) if inner else None
                return {"device": device, "shot": int(shot) if shot else None, "device_basis": f"name {name!r}"}
    return {"device": "unknown", "shot": None, "device_basis": "no device token in the names"}


# --------------------------------------------------------------------------
# records


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text).strip()).strip("._-")
    if not slug:
        raise MeshIndexError(f"cannot make a name from {text!r}")
    return slug


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_path(mesh_id: str, *, meshes_dir: Path | None = None) -> Path:
    return (meshes_dir or paths.MESHES_DIR) / f"{mesh_id}.yaml"


def recipe_dir(mesh_id: str, *, meshes_dir: Path | None = None) -> Path:
    return (meshes_dir or paths.MESHES_DIR) / mesh_id


def load(mesh_id: str, *, meshes_dir: Path | None = None) -> dict[str, Any]:
    path = record_path(mesh_id, meshes_dir=meshes_dir)
    if not path.is_file():
        raise MeshIndexError(f"no mesh record {mesh_id!r} ({path})")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise MeshIndexError(f"{path} is not a mapping")
    return data


def iter_meshes(*, meshes_dir: Path | None = None) -> Iterator[dict[str, Any]]:
    directory = meshes_dir or paths.MESHES_DIR
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and data.get("mesh_id"):
            yield data


def _write(record: dict[str, Any], *, meshes_dir: Path | None = None) -> Path:
    path = record_path(record["mesh_id"], meshes_dir=meshes_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = _now()
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)
    return path


def _new_record(mesh_id: str, *, origin: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mesh_id": mesh_id,
        "origin": origin,
        "status": "unknown",
        "device": "unknown",
        "shot": None,
        "device_basis": "",
        "description": "",
        "geometry": {},
        "headline": {},
        "parameters": {},
        "recipe": {},
        "inputs": {},
        "outputs": {},
        "source": {},
        "tomms": {},
        "attempts": [],
        "deployments": [],
        "used_by": [],
        "note": "",
        "ai_note": "",
        "created_at": _now(),
    }


def summarize(record: dict[str, Any]) -> dict[str, Any]:
    outputs = record.get("outputs") or {}
    return {
        "mesh_id": record.get("mesh_id"),
        "status": record.get("status"),
        "device": record.get("device"),
        "shot": record.get("shot"),
        "planes": (record.get("geometry") or {}).get("planes"),
        "nodes": outputs.get("nodes"),
        "triangles": outputs.get("triangles"),
        "generated_at": (outputs.get("node") or {}).get("mtime") or (record.get("tomms") or {}).get("finished_at"),
        "attribution": (record.get("recipe") or {}).get("attribution"),
        "confidence": (record.get("recipe") or {}).get("confidence"),
        "source": (record.get("source") or {}).get("directory"),
        "used_by": list(record.get("used_by") or []),
        "description": record.get("description", ""),
    }


def search(query: str = "", *, device: str | None = None, status: str | None = None,
           meshes_dir: Path | None = None) -> list[dict[str, Any]]:
    needle = (query or "").lower()
    hits = []
    for record in iter_meshes(meshes_dir=meshes_dir):
        if device and str(record.get("device")) != device:
            continue
        if status and str(record.get("status")) != status:
            continue
        if needle:
            haystack = " ".join(
                str(x) for x in (
                    record.get("mesh_id"), record.get("device"), record.get("shot"),
                    record.get("description"), record.get("note"), record.get("ai_note"),
                    (record.get("source") or {}).get("directory"),
                    ((record.get("inputs") or {}).get("equilibrium") or {}).get("name"),
                )
            ).lower()
            if needle not in haystack:
                continue
        hits.append(summarize(record))
    return hits


def lookup(sha256: str, *, meshes_dir: Path | None = None) -> list[dict[str, Any]]:
    """Which mesh records have an output or input with this hash."""
    hits = []
    for record in iter_meshes(meshes_dir=meshes_dir):
        for section in ("outputs", "inputs"):
            for role, entry in (record.get(section) or {}).items():
                if isinstance(entry, dict) and entry.get("sha256") == sha256:
                    hits.append({"mesh_id": record["mesh_id"], "section": section, "role": role, **entry})
    return hits


def annotate(
    mesh_id: str,
    *,
    note: str | None = None,
    ai_note: str | None = None,
    description: str | None = None,
    device: str | None = None,
    shot: int | str | None = None,
    status: str | None = None,
    meshes_dir: Path | None = None,
) -> dict[str, Any]:
    """Set the human-owned fields of a record (device/shot corrections included)."""
    record = load(mesh_id, meshes_dir=meshes_dir)
    if note is not None:
        record["note"] = note
    if ai_note is not None:
        record["ai_note"] = ai_note
    if description is not None:
        record["description"] = description
    if device is not None:
        record["device"] = _slug(device).lower()
        record["device_basis"] = "set by the user"
    if shot is not None:
        record["shot"] = int(shot) if str(shot).isdigit() else str(shot)
        record["device_basis"] = "set by the user"
    if status is not None:
        if status not in STATUSES:
            raise MeshIndexError(f"status must be one of {STATUSES}, not {status!r}")
        record["status"] = status
    _write(record, meshes_dir=meshes_dir)
    return record


def _output_hashes(record: dict[str, Any]) -> set[str]:
    return {
        str(entry["sha256"])
        for role, entry in (record.get("outputs") or {}).items()
        if isinstance(entry, dict) and entry.get("sha256")
    }


def _catalog_hashes(record: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    sections = [record.get("referenced_inputs") or {}]
    sections += [run.get("referenced_inputs") or {} for run in record.get("runs") or [] if isinstance(run, dict)]
    for section in sections:
        for item in (section.get("files") or {}).values():
            if isinstance(item, dict) and item.get("sha256"):
                hashes.add(str(item["sha256"]))
    return hashes


def link(
    *,
    meshes_dir: Path | None = None,
    catalog_dir: Path | None = None,
    records: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recompute ``used_by`` on every mesh record from the catalog's input hashes."""
    by_hash: dict[str, set[str]] = {}
    source = records if records is not None else catalog.iter_records(catalog_dir=catalog_dir)
    for record in source:
        rid = str(record.get("record_id") or record.get("simulation_id") or "")
        for digest in _catalog_hashes(record):
            by_hash.setdefault(digest, set()).add(rid)
    linked: dict[str, list[str]] = {}
    changed = 0
    for mesh in list(iter_meshes(meshes_dir=meshes_dir)):
        users = sorted({rid for digest in _output_hashes(mesh) for rid in by_hash.get(digest, set())})
        if users != list(mesh.get("used_by") or []):
            mesh["used_by"] = users
            _write(mesh, meshes_dir=meshes_dir)
            changed += 1
        if users:
            linked[mesh["mesh_id"]] = users
    return {"meshes_updated": changed, "linked": linked}


# --------------------------------------------------------------------------
# remote access (thin, injectable for tests)


class Remote:
    """The few remote operations the mesh tools need, over ``tools.ssh``."""

    def __init__(self, machine: str):
        from tools import ssh

        self.machine = machine
        self.cfg = m.get_machine(machine)
        self.target = ssh.host_for(self.cfg)
        self._ssh = ssh

    def run(self, argv: list[str], *, timeout: float = 120.0, check: bool = True) -> str:
        result = self._ssh.run(self.target, argv, timeout=timeout, check=check)
        return result.stdout if check else result.stdout + result.stderr

    def shell(self, script: str, *, timeout: float = 120.0) -> str:
        return self.run(["bash", "-c", script], timeout=timeout)

    def write_text(self, path: str, content: str) -> None:
        self._ssh.write_file(self.target, path, content)

    def pull(self, remote_path: str, local_path: Path, *, timeout: float = 600.0) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._ssh.pull(self.target, remote_path, str(local_path), timeout=timeout)

    def push(self, local_path: Path, remote_path: str, *, timeout: float = 600.0) -> None:
        self._ssh.push(self.target, str(local_path), remote_path, timeout=timeout)


def mesh_settings(machine: str) -> dict[str, Any]:
    cfg = m.get_machine(machine)
    section = cfg.get("mesh") or {}
    root = section.get("root")
    if not root or m.is_placeholder(root):
        raise MeshIndexError(
            f"machine {machine!r} has no mesh.root; set it in machines.local.yaml (docs/mesh-generation.md)"
        )
    tomms = dict(section.get("tomms") or {})
    return {"root": root, "tomms": tomms}


def default_machine() -> str:
    """The first machine with a usable ``mesh.root`` (flux for the author)."""
    for name in m.machine_names():
        root = ((m.get_machine(name).get("mesh") or {}).get("root"))
        if root and not m.is_placeholder(root):
            return name
    raise MeshIndexError("no machine defines mesh.root (machines.local.yaml, docs/mesh-generation.md)")


# --------------------------------------------------------------------------
# import from a remote mesh work tree


@dataclass
class RemoteFile:
    path: str
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def directory(self) -> str:
        return self.path.rsplit("/", 1)[0]

    @property
    def mtime_iso(self) -> str:
        return datetime.fromtimestamp(self.mtime, timezone.utc).isoformat(timespec="seconds")


@dataclass
class DirectoryScan:
    path: str
    files: dict[str, RemoteFile] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def recipes(self) -> list[RemoteFile]:
        return sorted((f for n, f in self.files.items() if n.startswith(RECIPE_NAME)), key=lambda f: f.name)

    def outputs(self) -> dict[str, dict[str, RemoteFile]]:
        """prefix -> {node, ele, flx} for every complete output triple."""
        triples: dict[str, dict[str, RemoteFile]] = {}
        for name, item in self.files.items():
            if name.endswith(".node"):
                prefix = name[: -len(".node")]
                ele = self.files.get(prefix + ".ele")
                flx = self.files.get(prefix + ".flx.aif")
                if ele and flx:
                    triples[prefix] = {"node": item, "ele": ele, "flx": flx}
        return triples

    def newest(self, since: float) -> bool:
        return any(
            f.mtime >= since
            for n, f in self.files.items()
            if n.startswith(RECIPE_NAME) or n.endswith(".node")
        )


def parse_listing(text: str) -> dict[str, DirectoryScan]:
    """``find -printf '%p\\t%s\\t%T@\\n'`` output -> directory scans."""
    scans: dict[str, DirectoryScan] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        item = RemoteFile(parts[0], int(parts[1]), float(parts[2]))
        scans.setdefault(item.directory, DirectoryScan(item.directory)).files[item.name] = item
    return scans


def parse_since(value: str | None, *, now: datetime | None = None) -> float | None:
    """``2y``, ``18m``, ``90d`` or an ISO date -> epoch seconds; None means everything."""
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    match = re.fullmatch(r"(\d+)([ymwd])", value.strip().lower())
    if match:
        count, unit = int(match.group(1)), match.group(2)
        days = {"y": 365, "m": 30, "w": 7, "d": 1}[unit] * count
        return (now - timedelta(days=days)).timestamp()
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise MeshIndexError(f"cannot read --since {value!r}: use 2y, 90d or YYYY-MM-DD") from exc


def _is_failed_variant(name: str) -> bool:
    low = name.lower()
    return any(token in low for token in ("fail", "crash", "bad"))


MTIME_WINDOW_SECONDS = 36 * 3600
CONFIDENCE = {
    "nodeFile": "high", "variant name": "high", "nphi token": "high", "generated": "high",
    "xgca token": "medium", "xgc1 token": "medium", "mtime": "medium", "only candidate": "medium",
    "ambiguous": "none", "no outputs": "none", "none": "none",
}


def _name_token(name: str) -> str:
    """``xgc_mesh_in_may_26`` -> ``may26``; separators dropped on both sides of the match."""
    if not name.startswith(RECIPE_NAME):
        return ""
    return re.sub(r"[^a-z0-9]", "", name[len(RECIPE_NAME):].lower())


def attribute(
    prefix: str,
    variants: dict[str, dict[str, str]],
    *,
    mtimes: dict[str, float] | None = None,
    output_mtime: float | None = None,
    sibling_outputs: int = 1,
) -> dict[str, Any]:
    """Which ``xgc_mesh_in`` variant produced the output ``prefix``.

    Deterministic rules in order: the variant's ``nodeFile`` names the prefix;
    the variant's name suffix (``xgc_mesh_in_may_26`` -> ``may26``) occurs in
    the prefix; an ``nphiN`` token in the prefix matches exactly one variant's
    ``num_phi_planes``; an ``xgca``/``xgc1`` token matches exactly one
    variant's geometry; exactly one variant was written within 36 hours of
    the output; a single non-failed variant. Otherwise ``ambiguous`` with
    every candidate listed, defaulting to the plain ``xgc_mesh_in``.
    ``confidence`` grades the rule; ``only candidate`` drops to ``low`` when
    the directory holds several outputs, because the one recipe left was
    edited in place between them.
    """
    good = {n: p for n, p in variants.items() if not _is_failed_variant(n)}
    low = prefix.lower()
    squashed = re.sub(r"[^a-z0-9]", "", low)

    def hit(name: str, rule: str, candidates: list[str] | None = None) -> dict[str, Any]:
        confidence = CONFIDENCE[rule]
        if rule == "only candidate" and sibling_outputs > 1:
            confidence = "low"
        return {"name": name, "attribution": rule, "confidence": confidence, "candidates": candidates or [name]}

    exact = [n for n, p in good.items() if output_prefix(p) == prefix]
    if len(exact) == 1:
        return hit(exact[0], "nodeFile")

    by_name = [n for n in good if _name_token(n) and _name_token(n) in squashed]
    if len(by_name) == 1:
        return hit(by_name[0], "variant name")

    nphi = re.search(r"nphi(\d+)", low)
    if nphi:
        hits = [n for n, p in good.items() if _int(p.get("num_phi_planes")) == int(nphi.group(1))]
        if len(hits) == 1:
            return hit(hits[0], "nphi token")

    if "xgca" in low or "axisym" in low:
        hits = [n for n, p in good.items() if _int(p.get("num_phi_planes")) == 1]
        if len(hits) == 1:
            return hit(hits[0], "xgca token")
    if "xgc1" in low or "turb" in low:
        hits = [n for n, p in good.items() if (_int(p.get("num_phi_planes")) or 0) > 1]
        if len(hits) == 1:
            return hit(hits[0], "xgc1 token")

    if mtimes and output_mtime is not None:
        hits = [n for n in good if n in mtimes and abs(mtimes[n] - output_mtime) <= MTIME_WINDOW_SECONDS]
        if len(hits) == 1:
            return hit(hits[0], "mtime")

    if len(good) == 1:
        return hit(next(iter(good)), "only candidate")
    if not good:
        return {"name": None, "attribution": "none", "confidence": "none", "candidates": []}
    default = RECIPE_NAME if RECIPE_NAME in good else sorted(good)[0]
    return hit(default, "ambiguous", sorted(good))


def _read_remote_texts(remote: Remote, paths_: list[str]) -> dict[str, str]:
    """Several small remote text files in one round trip."""
    texts: dict[str, str] = {}
    for chunk_start in range(0, len(paths_), 200):
        chunk = paths_[chunk_start:chunk_start + 200]
        script = "; ".join(
            f"printf '%s %s\\n' {shlex.quote(_FILE_MARK)} {shlex.quote(p)}; cat {shlex.quote(p)} 2>/dev/null"
            for p in chunk
        )
        output = remote.shell(script, timeout=300)
        current: str | None = None
        buffer: list[str] = []
        for line in output.splitlines(keepends=True):
            if line.startswith(_FILE_MARK + " "):
                if current is not None:
                    texts[current] = "".join(buffer)
                current = line[len(_FILE_MARK) + 1:].rstrip("\n")
                buffer = []
            else:
                buffer.append(line)
        if current is not None:
            texts[current] = "".join(buffer)
    return texts


def _hash_remote(remote: Remote, paths_: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for chunk_start in range(0, len(paths_), 100):
        chunk = paths_[chunk_start:chunk_start + 100]
        output = remote.shell("sha256sum -- " + " ".join(shlex.quote(p) for p in chunk) + " 2>/dev/null", timeout=1800)
        for line in output.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                hashes[parts[1].strip().lstrip("*")] = parts[0]
    return hashes


def _head_counts(remote: Remote, paths_: list[str]) -> dict[str, int | None]:
    """First integer of the first line of each file (vertex/triangle counts)."""
    counts: dict[str, int | None] = {}
    if not paths_:
        return counts
    script = "; ".join(
        f"printf '%s ' {shlex.quote(p)}; head -n 1 {shlex.quote(p)} 2>/dev/null | awk '{{print $1}}'"
        for p in paths_
    )
    for line in remote.shell(script, timeout=300).splitlines():
        parts = line.split(None, 1)
        if parts:
            counts[parts[0]] = _int(parts[1]) if len(parts) == 2 else None
    return counts


def _remote_entry(item: RemoteFile, machine: str, sha256: str | None) -> dict[str, Any]:
    return {
        "name": item.name,
        "sha256": sha256,
        "size_bytes": item.size,
        "mtime": item.mtime_iso,
        "remote": f"{machine}:{item.path}",
    }


def _unique_mesh_id(prefix: str, directory: str, node_hash: str | None, *, meshes_dir: Path | None) -> tuple[str, dict[str, Any] | None]:
    """``prefix`` unless another directory already took it with different content."""
    mesh_id = _slug(prefix)
    try:
        existing = load(mesh_id, meshes_dir=meshes_dir)
    except MeshIndexError:
        return mesh_id, None
    same_dir = (existing.get("source") or {}).get("directory") == directory
    same_hash = node_hash and ((existing.get("outputs") or {}).get("node") or {}).get("sha256") == node_hash
    if same_dir or same_hash:
        return mesh_id, existing
    return f"{mesh_id}__{_slug(directory.rsplit('/', 1)[-1])}", None


def import_from_remote(
    machine: str | None = None,
    *,
    root: str | None = None,
    since: str | None = "2y",
    copy_equilibrium: bool = True,
    dry_run: bool = False,
    meshes_dir: Path | None = None,
    data_root: Path | None = None,
    catalog_dir: Path | None = None,
    remote: Remote | None = None,
    listing: str | None = None,
) -> dict[str, Any]:
    """Catalogue every mesh in the machine's mesh work tree modified since ``since``.

    One record per complete output triple; a directory with a recipe and no
    outputs becomes a ``failed`` record named after the directory. Idempotent:
    re-running refreshes records in place and keeps the human fields.
    ``dry_run`` scans and reports without writing anything locally.
    """
    machine = machine or default_machine()
    settings = mesh_settings(machine)
    root = root or settings["root"]
    remote = remote or Remote(machine)
    meshes_dir = meshes_dir or paths.MESHES_DIR
    data_root = data_root or paths.DATA_ROOT
    cutoff = parse_since(since)

    if listing is None:
        listing = remote.run(
            ["find", root, "-mindepth", "2", "-maxdepth", "2", "-type", "f", "-printf", "%p\\t%s\\t%T@\\n"],
            timeout=300,
        )
    scans = parse_listing(listing)
    selected = [
        scan for scan in sorted(scans.values(), key=lambda s: s.path)
        if scan.recipes() and (cutoff is None or scan.newest(cutoff))
    ]
    skipped = [scan.name for scan in scans.values() if scan not in selected]

    recipe_paths = [f.path for scan in selected for f in scan.recipes()]
    texts = _read_remote_texts(remote, recipe_paths) if recipe_paths else {}

    # Everything that needs a hash or a copy, collected before the round trips.
    plans: list[dict[str, Any]] = []
    hash_paths: list[str] = []
    small_paths: list[str] = []
    for scan in selected:
        variants = {f.name: parse_mesh_in(texts.get(f.path, "")) for f in scan.recipes()}
        triples = scan.outputs()
        mtimes = {f.name: f.mtime for f in scan.recipes()}
        if not triples:
            name = RECIPE_NAME if RECIPE_NAME in variants else sorted(variants)[0]
            plans.append({"scan": scan, "prefix": None, "recipe": {"name": name, "attribution": "no outputs", "confidence": "none", "candidates": sorted(variants)}, "variants": variants})
        for prefix, triple in triples.items():
            recipe = attribute(prefix, variants, mtimes=mtimes, output_mtime=triple["node"].mtime, sibling_outputs=len(triples))
            plans.append({"scan": scan, "prefix": prefix, "triple": triple, "recipe": recipe, "variants": variants})
            hash_paths += [f.path for f in triple.values()]
    # Oldest output first: a mesh copied into later directories keeps the
    # original directory (and its recipe) as its source.
    plans.sort(key=lambda p: (p["triple"]["node"].mtime if p.get("triple") else float("inf"), p["scan"].path))
    for plan in plans:
        params = plan["variants"].get(plan["recipe"]["name"]) or {}
        plan["params"] = params
        scan = plan["scan"]
        for role, name in recipe_files(params).items():
            item = scan.files.get(name)
            if item and item.size <= MAX_RECIPE_FILE_BYTES:
                small_paths.append(item.path)
        eq = scan.files.get(params.get(EQUILIBRIUM_KEY, ""))
        if eq:
            hash_paths.append(eq.path)
    hash_paths = sorted(set(hash_paths))
    small_paths = sorted(set(small_paths))

    result: dict[str, Any] = {
        "machine": machine, "root": root, "since": since, "dry_run": dry_run,
        "directories_scanned": len(scans), "directories_selected": [s.name for s in selected],
        "directories_skipped": skipped, "records": [], "ambiguous": [], "missing_inputs": [],
    }
    if dry_run:
        result["records"] = [
            {"mesh_id": p["prefix"] or p["scan"].name, "directory": p["scan"].name,
             "recipe": p["recipe"]["name"], "attribution": p["recipe"]["attribution"],
             "confidence": p["recipe"]["confidence"],
             "candidates": p["recipe"]["candidates"], "headline": headline(p["params"])}
            for p in plans
        ]
        result["ambiguous"] = [r["mesh_id"] for r in result["records"] if r["attribution"] == "ambiguous"]
        result["writes"] = False
        return result

    hashes = _hash_remote(remote, hash_paths) if hash_paths else {}
    small_texts = _read_remote_texts(remote, small_paths) if small_paths else {}
    counts = _head_counts(remote, [p for p in hash_paths if p.endswith((".node", ".ele"))])

    written: list[str] = []
    seen_hashes: dict[str, str] = {}
    for plan in plans:
        scan: DirectoryScan = plan["scan"]
        params: dict[str, str] = plan["params"]
        triple = plan.get("triple")
        node_hash = hashes.get(triple["node"].path) if triple else None
        if node_hash and node_hash in seen_hashes:
            # The same mesh copied into another directory: one record, every home listed.
            primary = load(seen_hashes[node_hash], meshes_dir=meshes_dir)
            also = primary.setdefault("source", {}).setdefault("also_in", [])
            copy_path = f"{scan.path}/{plan['prefix']}"
            if copy_path != f"{primary['source'].get('directory')}/{primary['mesh_id']}" and copy_path not in also:
                also.append(copy_path)
                _write(primary, meshes_dir=meshes_dir)
            result.setdefault("duplicates", []).append({"mesh_id": primary["mesh_id"], "also_in": copy_path})
            continue
        if plan["prefix"]:
            mesh_id, existing = _unique_mesh_id(plan["prefix"], scan.path, node_hash, meshes_dir=meshes_dir)
        else:
            mesh_id = _slug(scan.name) + "__no_output"
            try:
                existing = load(mesh_id, meshes_dir=meshes_dir)
            except MeshIndexError:
                existing = None
        record = existing or _new_record(mesh_id, origin="imported")
        if record.get("origin") == "automation":
            # Our own generated meshes keep their verified provenance.
            continue

        device = infer_device(scan.name, params.get(EQUILIBRIUM_KEY, ""))
        if record.get("device_basis") != "set by the user":
            record.update(device)
        record["geometry"] = geometry(params)
        record["headline"] = headline(params)
        record["parameters"] = dict(params)
        record["source"] = {"machine": machine, "directory": scan.path, "scanned_at": _now()}
        record["status"] = "ok" if triple else "failed"
        record["tomms"] = {"exe": None, "version": None, "basis": "imported: build not recorded"}

        # Recipe copies.
        rdir = recipe_dir(mesh_id, meshes_dir=meshes_dir)
        rdir.mkdir(parents=True, exist_ok=True)
        recipe_name = plan["recipe"]["name"]
        recipe_text = texts.get(scan.files[recipe_name].path, "") if recipe_name in scan.files else ""
        (rdir / RECIPE_NAME).write_text(recipe_text, encoding="utf-8")
        record["recipe"] = {
            "file": f"{mesh_id}/{RECIPE_NAME}",
            "source_name": recipe_name,
            "attribution": plan["recipe"]["attribution"],
            "confidence": plan["recipe"]["confidence"],
            "candidates": plan["recipe"]["candidates"],
            "sha256": hashlib.sha256(recipe_text.encode("utf-8")).hexdigest(),
        }
        if node_hash:
            seen_hashes[node_hash] = mesh_id
        if plan["recipe"]["attribution"] == "ambiguous":
            result["ambiguous"].append(mesh_id)

        inputs: dict[str, Any] = {}
        eq_name = params.get(EQUILIBRIUM_KEY, "")
        eq_item = scan.files.get(eq_name)
        if eq_name:
            if eq_item:
                entry = _remote_entry(eq_item, machine, hashes.get(eq_item.path))
                entry["local"] = None
                if copy_equilibrium and entry["sha256"]:
                    local = data_root / "meshes" / "equilibria" / entry["sha256"][:12] / eq_item.name
                    if not local.is_file():
                        remote.pull(eq_item.path, local)
                    entry["local"] = str(local.relative_to(data_root))
                inputs["equilibrium"] = entry
            else:
                inputs["equilibrium"] = {"name": eq_name, "missing": True}
                result["missing_inputs"].append(f"{mesh_id}: equilibrium {eq_name}")
        for role, name in recipe_files(params).items():
            item = scan.files.get(name)
            if item is None:
                inputs[role] = {"name": name, "missing": True}
                result["missing_inputs"].append(f"{mesh_id}: {role} {name}")
                continue
            text = small_texts.get(item.path)
            entry = _remote_entry(item, machine, None)
            if text is not None:
                (rdir / name).write_text(text, encoding="utf-8")
                entry["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                entry["local"] = f"{mesh_id}/{name}"
            inputs[role] = entry
        record["inputs"] = inputs

        if triple:
            outputs = {role: _remote_entry(item, machine, hashes.get(item.path)) for role, item in triple.items()}
            outputs["nodes"] = counts.get(triple["node"].path)
            outputs["triangles"] = counts.get(triple["ele"].path)
            record["outputs"] = outputs

        attempts = []
        adir = rdir / "attempts"
        for variant in scan.recipes():
            if not _is_failed_variant(variant.name):
                continue
            adir.mkdir(exist_ok=True)
            text = texts.get(variant.path, "")
            (adir / variant.name).write_text(text, encoding="utf-8")
            attempts.append({
                "name": variant.name,
                "saved": f"{mesh_id}/attempts/{variant.name}",
                "mtime": variant.mtime_iso,
                "parameters_diff": diff_parameters(params, parse_mesh_in(text)),
            })
        record["attempts"] = attempts
        _write(record, meshes_dir=meshes_dir)
        written.append(mesh_id)
        result["records"].append({
            "mesh_id": mesh_id, "directory": scan.name, "status": record["status"],
            "recipe": recipe_name, "attribution": plan["recipe"]["attribution"],
            "confidence": plan["recipe"]["confidence"],
            "device": record["device"], "shot": record["shot"], "headline": record["headline"],
            "nodes": (record.get("outputs") or {}).get("nodes"),
        })
    result["written"] = written
    result["linked"] = link(meshes_dir=meshes_dir, catalog_dir=catalog_dir)["linked"]
    return result


# --------------------------------------------------------------------------
# generation on the mesh machine


def _tomms_env_script(settings: dict[str, Any]) -> str:
    lines = [str(line) for line in (settings["tomms"].get("env") or [])]
    return "; ".join(lines) if lines else ":"


def plan_generation(
    mesh_id: str,
    *,
    base: str,
    overrides: dict[str, str | None] | None = None,
    machine: str | None = None,
    description: str = "",
    meshes_dir: Path | None = None,
) -> dict[str, Any]:
    """Everything a run of ``tomms_meshgen`` for ``mesh_id`` would do; writes nothing.

    ``base`` is an existing mesh record whose recipe is the starting point.
    ``overrides`` set ``xgc_mesh_in`` keys. Output names are forced to
    ``<mesh_id>.node/.ele/.flx.aif`` so no renaming is needed afterwards.
    """
    machine = machine or default_machine()
    settings = mesh_settings(machine)
    mesh_id = _slug(mesh_id)
    if record_path(mesh_id, meshes_dir=meshes_dir).exists():
        raise MeshIndexError(f"mesh record {mesh_id!r} already exists; pick another name")
    base_record = load(base, meshes_dir=meshes_dir)
    base_dir = recipe_dir(base, meshes_dir=meshes_dir)
    base_text = (base_dir / RECIPE_NAME).read_text(encoding="utf-8")
    base_params = parse_mesh_in(base_text)

    forced = {"nodeFile": f"{mesh_id}.node", "elmFile": f"{mesh_id}.ele", "addinfoflxFile": f"{mesh_id}.flx.aif"}
    if settings["tomms"].get("license"):
        forced["SimLic"] = str(settings["tomms"]["license"])
    merged: dict[str, str | None] = {**forced, **dict(overrides or {})}
    text = render_mesh_in(base_text, merged)
    params = parse_mesh_in(text)

    work_dir = f"{settings['root'].rstrip('/')}/{mesh_id}"
    # Inputs: small files from the base recipe copy; the equilibrium and
    # anything not copied locally from the base's remote directory.
    base_source = (base_record.get("source") or {}).get("directory")
    base_machine = (base_record.get("source") or {}).get("machine")
    stage: list[dict[str, Any]] = []
    base_inputs = base_record.get("inputs") or {}
    for role, name in recipe_files(params).items():
        local = base_dir / name
        remote_src = f"{base_source}/{name}" if base_source else None
        if (base_inputs.get(role) or {}).get("missing") and (base_inputs[role].get("name") == name):
            # The base recipe named it and did not have it either; TOMMS ran without it.
            stage.append({"role": role, "name": name, "from": "absent in base (not staged)"})
        elif local.is_file():
            stage.append({"role": role, "name": name, "from": "catalog copy", "local": str(local)})
        elif remote_src and base_machine == machine:
            stage.append({"role": role, "name": name, "from": "remote copy", "remote": remote_src})
        else:
            stage.append({"role": role, "name": name, "from": "missing"})
    eq_name = params.get(EQUILIBRIUM_KEY, "")
    eq_entry = (base_record.get("inputs") or {}).get("equilibrium") or {}
    if eq_name:
        local_eq = paths.DATA_ROOT / eq_entry["local"] if eq_entry.get("local") else None
        if base_source and base_machine == machine and eq_entry.get("name") == eq_name:
            stage.append({"role": "equilibrium", "name": eq_name, "from": "remote copy", "remote": f"{base_source}/{eq_name}"})
        elif local_eq and local_eq.is_file():
            stage.append({"role": "equilibrium", "name": eq_name, "from": "data directory", "local": str(local_eq)})
        else:
            stage.append({"role": "equilibrium", "name": eq_name, "from": "missing"})

    exe = settings["tomms"].get("exe")
    command = f"cd {shlex.quote(work_dir)} && {_tomms_env_script(settings)} && {shlex.quote(str(exe))} > {TOMMS_LOG} 2>&1"
    return {
        "writes": False,
        "mesh_id": mesh_id,
        "machine": machine,
        "base": base,
        "work_dir": work_dir,
        "tomms": {"exe": exe, "env": settings["tomms"].get("env"), "version": _tomms_version(exe)},
        "xgc_mesh_in": text,
        "parameters_diff": diff_parameters(base_params, params),
        "headline": headline(params),
        "geometry": geometry(params),
        "stage": stage,
        "missing": [s["name"] for s in stage if s["from"] == "missing"],
        "command": command,
        "description": description,
        "note": "login-node run, detached; no scheduler, no node-hours. Confirm to stage and start.",
    }


def _tomms_version(exe: str | None) -> str | None:
    if not exe:
        return None
    match = re.search(r"/TOMMS/([^/]+)/", str(exe))
    return match.group(1) if match else None


def start_generation(
    plan: dict[str, Any],
    *,
    confirm: bool = False,
    replace: bool = False,
    meshes_dir: Path | None = None,
    remote: Remote | None = None,
) -> dict[str, Any]:
    """Stage the recipe on the machine and start ``tomms_meshgen`` detached.

    Refuses without ``confirm`` (the plan was shown first) and when the work
    directory already exists unless ``replace``.
    """
    if not confirm:
        return {"started": False, "error": "confirm=False: show the plan and ask first", "plan": plan}
    if plan["missing"]:
        return {"started": False, "error": f"inputs not available: {plan['missing']}", "plan": plan}
    machine = plan["machine"]
    remote = remote or Remote(machine)
    work_dir = plan["work_dir"]
    q = shlex.quote
    if not replace:
        exists = remote.shell(f"test -e {q(work_dir)} && echo yes || echo no").strip()
        if exists == "yes":
            return {"started": False, "error": f"{machine}:{work_dir} exists; pass replace=True to reuse it"}
    remote.shell(f"mkdir -p {q(work_dir)}")
    remote.write_text(f"{work_dir}/{RECIPE_NAME}", plan["xgc_mesh_in"])
    for item in plan["stage"]:
        dest = f"{work_dir}/{item['name']}"
        if item["from"] in ("catalog copy", "data directory"):
            remote.push(Path(item["local"]), dest)
        elif item["from"] == "remote copy":
            remote.shell(f"cp -f {q(item['remote'])} {q(dest)}")
    script = (
        f"cd {q(work_dir)} && rm -f {TOMMS_RC} && "
        f"nohup setsid bash -lc {q(plan['command'] + f'; echo $? > {TOMMS_RC}')} "
        f"> /dev/null 2>&1 < /dev/null & echo started"
    )
    remote.shell(script)

    record = _new_record(plan["mesh_id"], origin="automation")
    base = load(plan["base"], meshes_dir=meshes_dir)
    params = parse_mesh_in(plan["xgc_mesh_in"])
    record.update({k: base.get(k) for k in ("device", "shot", "device_basis")})
    record["description"] = plan.get("description") or ""
    record["status"] = "running"
    record["geometry"] = plan["geometry"]
    record["headline"] = plan["headline"]
    record["parameters"] = params
    record["source"] = {"machine": machine, "directory": work_dir, "base": plan["base"], "parameters_diff": plan["parameters_diff"]}
    record["tomms"] = {**plan["tomms"], "started_at": _now(), "log": f"{work_dir}/{TOMMS_LOG}"}
    rdir = recipe_dir(record["mesh_id"], meshes_dir=meshes_dir)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / RECIPE_NAME).write_text(plan["xgc_mesh_in"], encoding="utf-8")
    record["recipe"] = {
        "file": f"{record['mesh_id']}/{RECIPE_NAME}", "source_name": RECIPE_NAME,
        "attribution": "generated", "confidence": "high", "candidates": [RECIPE_NAME],
        "sha256": hashlib.sha256(plan["xgc_mesh_in"].encode("utf-8")).hexdigest(),
    }
    inputs: dict[str, Any] = {}
    for item in plan["stage"]:
        entry: dict[str, Any] = {"name": item["name"], "remote": f"{machine}:{work_dir}/{item['name']}"}
        if item.get("local"):
            local = Path(item["local"])
            entry["sha256"] = sha256_of(local)
            if item["from"] == "catalog copy":
                shutil.copy2(local, rdir / item["name"])
                entry["local"] = f"{record['mesh_id']}/{item['name']}"
            else:
                entry["local"] = str(local.relative_to(paths.DATA_ROOT)) if local.is_relative_to(paths.DATA_ROOT) else str(local)
        inputs[item["role"]] = entry
    record["inputs"] = inputs
    _write(record, meshes_dir=meshes_dir)
    return {"started": True, "mesh_id": record["mesh_id"], "work_dir": f"{machine}:{work_dir}", "record": record}


def check_generation(mesh_id: str, *, meshes_dir: Path | None = None, catalog_dir: Path | None = None,
                     remote: Remote | None = None, log_lines: int = 20) -> dict[str, Any]:
    """Poll a running generation; on completion hash the outputs and close the record."""
    record = load(mesh_id, meshes_dir=meshes_dir)
    source = record.get("source") or {}
    machine, work_dir = source.get("machine"), source.get("directory")
    if not machine or not work_dir:
        raise MeshIndexError(f"{mesh_id} has no remote work directory to check")
    remote = remote or Remote(machine)
    q = shlex.quote
    rc_text = remote.shell(f"cat {q(work_dir + '/' + TOMMS_RC)} 2>/dev/null || echo none").strip()
    tail = remote.shell(f"tail -n {int(log_lines)} {q(work_dir + '/' + TOMMS_LOG)} 2>/dev/null")
    if rc_text == "none":
        running = remote.shell(f"pgrep -u \"$USER\" -f {q('tomms_meshgen')} >/dev/null && echo yes || echo no").strip()
        return {"mesh_id": mesh_id, "status": "running" if running == "yes" else "unknown",
                "detail": "no exit code yet" + ("" if running == "yes" else " and no tomms_meshgen process"),
                "log_tail": tail, "writes": False}
    rc = _int(rc_text)
    names = {role: f"{mesh_id}{suffix}" for role, suffix in zip(("node", "ele", "flx"), OUTPUT_SUFFIXES)}
    listing = remote.run(
        ["find", work_dir, "-maxdepth", "1", "-type", "f", "-printf", "%p\\t%s\\t%T@\\n"], timeout=120
    )
    scan = parse_listing(listing).get(work_dir, DirectoryScan(work_dir))
    present = {role: scan.files.get(name) for role, name in names.items()}
    if rc == 0 and all(present.values()):
        hashes = _hash_remote(remote, [f.path for f in present.values()])
        counts = _head_counts(remote, [present["node"].path, present["ele"].path])
        outputs = {role: _remote_entry(item, machine, hashes.get(item.path)) for role, item in present.items()}
        outputs["nodes"] = counts.get(present["node"].path)
        outputs["triangles"] = counts.get(present["ele"].path)
        record["outputs"] = outputs
        record["status"] = "ok"
    else:
        record["status"] = "failed"
        record["ai_note"] = (f"tomms_meshgen exit code {rc}; outputs present: "
                             f"{[r for r, f in present.items() if f]}. Log tail:\n{tail.strip()}")[:4000]
    record["tomms"] = {**(record.get("tomms") or {}), "finished_at": _now(), "exit_code": rc}
    _write(record, meshes_dir=meshes_dir)
    link(meshes_dir=meshes_dir, catalog_dir=catalog_dir)
    return {"mesh_id": mesh_id, "status": record["status"], "exit_code": rc, "outputs": record.get("outputs"),
            "log_tail": tail, "written": True}


# --------------------------------------------------------------------------
# preview and deploy


def _ensure_outputs_local(record: dict[str, Any], *, data_root: Path, remote: Remote | None,
                          roles: tuple[str, ...] = ("node", "ele", "flx"), max_bytes: int | None = None) -> dict[str, Path]:
    outputs = record.get("outputs") or {}
    if not all(outputs.get(role) for role in roles):
        raise MeshIndexError(f"{record['mesh_id']} has no recorded outputs")
    total = sum(int(outputs[role].get("size_bytes") or 0) for role in roles)
    if max_bytes is not None and total > max_bytes:
        raise MeshIndexError(f"outputs are {total / 2**20:.0f} MB, above the {max_bytes / 2**20:.0f} MB limit; raise max_bytes to pull them anyway")
    local_dir = data_root / "meshes" / record["mesh_id"]
    machine = (record.get("source") or {}).get("machine")
    result: dict[str, Path] = {}
    for role in roles:
        entry = outputs[role]
        local = local_dir / entry["name"]
        if not (local.is_file() and sha256_of(local) == entry.get("sha256")):
            remote = remote or Remote(machine)
            remote_path = str(entry["remote"]).split(":", 1)[1]
            remote.pull(remote_path, local, timeout=1800)
            if entry.get("sha256") and sha256_of(local) != entry["sha256"]:
                raise MeshIndexError(f"{local} does not match the recorded hash; the remote file changed")
        result[role] = local
    return result


def read_node_ele(node: Path, ele: Path) -> tuple[list[tuple[float, float]], list[tuple[int, int, int]]]:
    """XGC/Triangle format: header ``N 2 0 M`` then ``i x y ...``; ``K 3 0`` then ``i a b c``."""
    points: list[tuple[float, float]] = []
    with node.open() as handle:
        count = int(handle.readline().split()[0])
        for _ in range(count):
            parts = handle.readline().split()
            points.append((float(parts[1]), float(parts[2])))
    tris: list[tuple[int, int, int]] = []
    with ele.open() as handle:
        count = int(handle.readline().split()[0])
        for _ in range(count):
            parts = handle.readline().split()
            tris.append((int(parts[1]) - 1, int(parts[2]) - 1, int(parts[3]) - 1))
    return points, tris


def preview(mesh_id: str, *, meshes_dir: Path | None = None, data_root: Path | None = None,
            remote: Remote | None = None, max_bytes: int = MAX_PREVIEW_BYTES) -> dict[str, Any]:
    """Pull node+ele (under ``max_bytes``) and draw the mesh: whole domain plus
    the outboard edge and the lower divertor region, where meshes go wrong."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.tri as mtri
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MeshIndexError("matplotlib is not installed: `uv sync --extra mesh`") from exc
    data_root = data_root or paths.DATA_ROOT
    record = load(mesh_id, meshes_dir=meshes_dir)
    files = _ensure_outputs_local(record, data_root=data_root, remote=remote, roles=("node", "ele"), max_bytes=max_bytes)
    points, tris = read_node_ele(files["node"], files["ele"])
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    tri = mtri.Triangulation(xs, ys, tris)
    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    for ax in axes:
        ax.triplot(tri, lw=0.15, color="k")
        ax.set_aspect("equal")
        ax.set_xlabel("R [m]")
    axes[0].set_title(f"{mesh_id}: {len(points)} nodes, {len(tris)} triangles")
    axes[0].set_ylabel("Z [m]")
    rmax, zmin = max(xs), min(ys)
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    w = 0.15 * span
    axes[1].set_xlim(rmax - 1.5 * w, rmax + 0.2 * w)
    mid = sum(ys) / len(ys)
    axes[1].set_ylim(mid - w, mid + w)
    axes[1].set_title("outboard edge")
    r_at_zmin = xs[ys.index(zmin)]
    axes[2].set_xlim(r_at_zmin - 1.2 * w, r_at_zmin + 1.2 * w)
    axes[2].set_ylim(zmin - 0.2 * w, zmin + 1.6 * w)
    axes[2].set_title("lower region (around min Z)")
    out = data_root / "meshes" / mesh_id / f"{mesh_id}_preview.png"
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return {"mesh_id": mesh_id, "preview": str(out), "nodes": len(points), "triangles": len(tris), "writes": False}


def deploy(
    mesh_id: str,
    machine: str,
    directory: str,
    *,
    confirm: bool = False,
    meshes_dir: Path | None = None,
    data_root: Path | None = None,
    source_remote: Remote | None = None,
    target_remote: Remote | None = None,
) -> dict[str, Any]:
    """Copy a mesh's three output files to ``machine:directory`` and verify them there.

    Same machine: a remote ``cp``. Otherwise the files are pulled into the
    data directory (verified against the recorded hashes) and pushed. The
    deployment is appended to the record so the next staged input can
    reference the files by path and the hash join finds the run.
    """
    record = load(mesh_id, meshes_dir=meshes_dir)
    outputs = record.get("outputs") or {}
    roles = ("node", "ele", "flx")
    if not all(outputs.get(r) for r in roles):
        raise MeshIndexError(f"{mesh_id} has no recorded outputs to deploy")
    total = sum(int(outputs[r].get("size_bytes") or 0) for r in roles)
    plan = {
        "mesh_id": mesh_id, "machine": machine, "directory": directory,
        "files": {r: outputs[r]["name"] for r in roles}, "total_bytes": total,
        "via": "remote cp" if (record.get("source") or {}).get("machine") == machine else "pull to data directory, then push",
    }
    if not confirm:
        return {"deployed": False, "plan": plan, "writes": False}
    target = target_remote or Remote(machine)
    q = shlex.quote
    target.shell(f"mkdir -p {q(directory)}")
    if plan["via"] == "remote cp":
        for r in roles:
            src = str(outputs[r]["remote"]).split(":", 1)[1]
            target.shell(f"cp -f {q(src)} {q(directory + '/' + outputs[r]['name'])}", timeout=1800)
    else:
        local = _ensure_outputs_local(record, data_root=data_root or paths.DATA_ROOT, remote=source_remote)
        for r in roles:
            target.push(local[r], f"{directory}/{outputs[r]['name']}", timeout=3600)
    remote_paths = [f"{directory}/{outputs[r]['name']}" for r in roles]
    verified = _hash_remote(target, remote_paths)
    mismatch = [p for p, r in zip(remote_paths, roles) if verified.get(p) != outputs[r].get("sha256")]
    entry = {"machine": machine, "directory": directory, "at": _now(), "verified": not mismatch,
             "files": {r: f"{directory}/{outputs[r]['name']}" for r in roles}}
    record.setdefault("deployments", []).append(entry)
    _write(record, meshes_dir=meshes_dir)
    return {"deployed": not mismatch, "deployment": entry, "mismatch": mismatch, "plan": plan, "written": True}
