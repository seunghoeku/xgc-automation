"""Create a user's catalog in the one layout every catalog shares.

xgc-automation defines the catalog layout, not the user: one directory per
user named after the GitHub id, ``users/<id>/``, with the same
subdirectories, the same seeded registry and the same ``.gitignore``
whether the repository belongs to one person (option A in
docs/setup-new-user.md 4a) or to a group with several user directories
(option B). Because the layout is identical, two catalogs merge by copying
``users/<id>/`` directories side by side: no renumbering, no path rewrite,
no conflict, since each user directory carries its own registry.

``xgc-sim init-catalog <github-id>`` is the only supported way to start a
catalog; :func:`layout_check` is what ``xgc-sim paths`` uses to say whether
the configured state root follows the layout.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tools import paths

# Machine codes are part of the layout: two users' ids must read the same
# way, so every new registry starts from this table (tools/input_archive.py
# carries the same fallback for reading archives).
CANONICAL_MACHINE_CODES = {
    "perlmutter": "pe",
    "polaris": "po",
    "aurora": "au",
    "doudna": "do",
    "frontier": "fr",
    "summit": "su",
    "stellar": "st",
    "stellarai": "sa",
    "flux": "fl",
}

# Subdirectories of users/<id>/ (tools/paths.py state paths, relative).
LAYOUT_DIRS = (
    "kb/simulations",
    "kb/shots",
    "kb/meshes",
    "kb/cases/drafts",
    "runs",
    "input_backup",
    "f_init/cases",
    "flow_match/cases",
)

GITIGNORE = """# xgc-catalog: written by xgc-sim init-catalog; the same in every catalog.
# derived view of kb/simulations/*.yaml; rebuilt by the tools (any user directory)
simulation_notebook.csv
simulation_notebook.csv.tmp
# local caches of the tools
.cache/
# analysis products and job logs live in each user's data directory
# (same relative layout), never in git
*.png
*.npz
*.out
*.svg
*.pdf
*.h5
*.nc
# raw XGC output never comes here
*.bp
*.bp.dir/
*.bak*
.DS_Store
__pycache__/
"""

README = """# xgc-catalog

Simulation state for xgc-automation (https://github.com/seunghoeku/xgc-automation):
catalog records, the simulation_id registry, submission provenance, input
backups and case logs. Created with `xgc-sim init-catalog`.

Layout, identical in every catalog so that user directories can be copied
between catalogs without renumbering or path changes:

    users/<github-id>/          one user's state root (catalog_dir points here)
      USER.md                   accounts, checkouts, preferences (imported by the agent)
      kb/simulations/<id>.yaml  one record per simulation
      kb/simulation_registry.yaml  this user's simulation_id counters
      kb/shots/, kb/meshes/, kb/cases/drafts/, runs/, input_backup/, f_init/cases/, flow_match/cases/

Products (PNG, NPZ, logs) and the notebook CSV stay out of git; see
xgc-automation docs/simulation-catalog.md ("Catalog repository", "Data directory").
"""

_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


class CatalogInitError(ValueError):
    pass


def validate_github_id(github_id: str) -> str:
    value = str(github_id).strip()
    if not _ID_RE.match(value):
        raise CatalogInitError(
            f"{github_id!r} is not a GitHub id (letters, digits, single hyphens, up to 39 chars)"
        )
    return value


def user_dir(repo: Path, github_id: str) -> Path:
    return Path(repo) / "users" / validate_github_id(github_id)


def seed_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "next_number": 1,
        "next_dev_number": 1,
        "machine_codes": dict(CANONICAL_MACHINE_CODES),
        "reservations": [],
    }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def layout_check(state_root: Path | None = None) -> dict[str, Any]:
    """Does the configured state root follow ``<repo>/users/<id>/``?"""
    root = Path(paths.STATE_ROOT if state_root is None else state_root).resolve()
    if root.parent.name == "users" and _ID_RE.match(root.name):
        return {"canonical": True, "user": root.name, "repo": str(root.parent.parent)}
    if root == paths.PROJECT_ROOT.resolve():
        return {
            "canonical": False,
            "user": None,
            "detail": "state inside the code checkout (default); run `xgc-sim init-catalog <github-id>`",
        }
    return {
        "canonical": False,
        "user": None,
        "detail": "state root is not a users/<github-id> directory; see docs/setup-new-user.md 4a",
    }


def init_catalog(
    github_id: str,
    *,
    repo: Path,
    data_dir: Path | None = None,
    remote: str | None = None,
    config_path: Path | None = None,
    write_config: bool = True,
    force_config: bool = False,
    use_git: bool = True,
) -> dict[str, Any]:
    """Create ``<repo>/users/<id>/`` in the canonical layout and point the tools at it.

    Idempotent: existing files are kept, missing ones are added. Returns what
    was created and what the user still has to do.
    """
    github_id = validate_github_id(github_id)
    repo = Path(repo).expanduser().resolve()
    user = user_dir(repo, github_id)
    created: list[str] = []
    kept: list[str] = []

    repo.mkdir(parents=True, exist_ok=True)
    if use_git and not (repo / ".git").exists():
        result = _git(repo, "init", "-q", "-b", "main")
        if result.returncode != 0:
            result = _git(repo, "init", "-q")
        if result.returncode != 0:
            raise CatalogInitError(f"git init failed: {result.stderr.strip()}")
        created.append(".git")

    for name, content in ((".gitignore", GITIGNORE), ("README.md", README)):
        target = repo / name
        if target.exists():
            kept.append(name)
        else:
            target.write_text(content, encoding="utf-8")
            created.append(name)

    for rel in LAYOUT_DIRS:
        directory = user / rel
        directory.mkdir(parents=True, exist_ok=True)
        keep = directory / ".gitkeep"
        if not any(directory.iterdir()):
            keep.write_text("", encoding="utf-8")
            created.append(f"users/{github_id}/{rel}/")

    registry = user / "kb" / "simulation_registry.yaml"
    if registry.exists():
        kept.append(f"users/{github_id}/kb/simulation_registry.yaml")
    else:
        registry.write_text(yaml.safe_dump(seed_registry(), sort_keys=False), encoding="utf-8")
        created.append(f"users/{github_id}/kb/simulation_registry.yaml")

    user_md = user / "USER.md"
    if user_md.exists():
        kept.append(f"users/{github_id}/USER.md")
    else:
        template = paths.TEMPLATE_DIR / "USER.md.example"
        user_md.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(f"users/{github_id}/USER.md (fill it in)")

    committed = False
    if use_git:
        _git(repo, "add", "-A")
        if _git(repo, "diff", "--cached", "--quiet").returncode != 0:
            env_ok = _git(repo, "config", "user.email").stdout.strip() and _git(repo, "config", "user.name").stdout.strip()
            commit = _git(repo, "commit", "-q", "-m", f"catalog: users/{github_id} created by xgc-sim init-catalog") if env_ok else None
            committed = bool(commit and commit.returncode == 0)
        if remote and _git(repo, "remote", "get-url", "origin").returncode != 0:
            _git(repo, "remote", "add", "origin", remote)
            created.append(f"remote origin -> {remote}")

    config_written = None
    data_root = Path(data_dir).expanduser() if data_dir else Path("~/xgc-data").expanduser()
    if write_config:
        config = Path(config_path) if config_path else paths.config_file()
        if config.exists() and not force_config:
            kept.append(str(config))
        else:
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                "# xgc-automation per-user settings (tools/paths.py); written by xgc-sim init-catalog\n"
                f"catalog_dir: {user}\n"
                f"data_dir: {data_root}\n"
                "catalog_sync: auto\n",
                encoding="utf-8",
            )
            config_written = str(config)
        link = config.parent / "USER.md"
        if link.is_symlink() or link.exists():
            if not force_config:
                kept.append(str(link))
            else:
                link.unlink()
                os.symlink(user_md, link)
        else:
            os.symlink(user_md, link)
            created.append(f"{link} -> {user_md}")

    next_steps = [f"edit {user_md}"]
    if use_git and not remote and _git(repo, "remote", "get-url", "origin").returncode != 0:
        next_steps.append(f"git -C {repo} remote add origin <private remote> && git -C {repo} push -u origin main")
    elif use_git and remote:
        next_steps.append(f"git -C {repo} push -u origin main")
    if use_git and not committed:
        next_steps.append(f"git -C {repo} commit -am 'catalog: start' (set git user.name/user.email first if needed)")
    next_steps.append("xgc-sim paths   # state_root must be the users/<id> directory; layout: canonical")

    return {
        "github_id": github_id,
        "repo": str(repo),
        "user_dir": str(user),
        "data_dir": str(data_root),
        "created": created,
        "kept": kept,
        "committed": committed,
        "config_written": config_written,
        "next_steps": next_steps,
    }
