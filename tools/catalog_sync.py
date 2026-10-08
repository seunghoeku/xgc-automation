"""Keep the catalog repository in step across checkouts: pull before a write,
commit and push after it.

The catalog (``paths.STATE_ROOT``) is written from more than one checkout, a
laptop and a cluster login node at least. Each write entry point (MCP tool,
``xgc-sim`` command) therefore calls :func:`pull` first and
:func:`commit_and_push` afterwards, so a record written on one machine is on
the other before its next write. Everything here is best effort and never
raises into the caller: a sync problem is reported in the result under
``catalog_sync`` and the catalog write itself has already happened.

Per-simulation records are separate files, so two checkouts only collide on
the registry counter. A rejected push is rebased once; if the rebase
conflicts (both sides reserved the same number) the rebase is aborted, our
unpushed commit is dropped with :func:`drop_unpushed_commit`, and the caller
tells the human that the number moved, rather than silently taking another
one that was never shown to them (absolute rule 1).

Enabled when the state root is a git work tree with an ``origin`` remote and
is not the code checkout itself; ``catalog_sync: true|false`` in
``~/.config/xgc-automation/config.yaml`` forces it either way. ``xgc-sim
--no-sync`` turns it off for one command.
"""

from __future__ import annotations

import functools
import inspect
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tools import paths

GIT_TIMEOUT_SECONDS = 90.0
_disabled_for_process = False


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def detail(self) -> str:
        return (self.stderr or self.stdout or "").strip().splitlines()[-1:][0] if (self.stderr or self.stdout).strip() else ""


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> GitResult:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return GitResult(127, "", "git executable not found")
    except subprocess.TimeoutExpired:
        return GitResult(124, "", f"git {' '.join(args[:2])} timed out after {timeout}s")
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def disable_for_process() -> None:
    """``xgc-sim --no-sync``: no git for the rest of this process."""
    global _disabled_for_process
    _disabled_for_process = True


def repo_root(root: Path | None = None) -> Path | None:
    """The git work tree holding the state root, or None if there is none.

    The state root may be the repository itself or a directory inside it
    (``users/<name>/`` in a group catalog, docs/setup-new-user.md 4a), so
    walk up to the nearest ``.git``.
    """
    candidate = (paths.STATE_ROOT if root is None else Path(root)).resolve()
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory
    return None


def enabled(root: Path | None = None) -> bool:
    if _disabled_for_process:
        return False
    setting = paths.config_setting("catalog_sync", "auto") if root is None else "auto"
    if setting is False or (isinstance(setting, str) and setting.lower() in {"false", "off", "no"}):
        return False
    repo = repo_root(root)
    if repo is None:
        return False
    if root is None and repo == paths.PROJECT_ROOT.resolve() and setting is not True:
        # State still inside the code checkout: never auto-commit there.
        return False
    return _git(repo, "remote", "get-url", "origin", timeout=10).ok


def pull(root: Path | None = None) -> dict[str, Any]:
    """Bring the catalog up to date before a write. Never raises."""
    if not enabled(root):
        return {"enabled": False}
    repo = repo_root(root)
    assert repo is not None
    result = _git(repo, "pull", "--rebase", "--autostash", "-q")
    if result.ok:
        return {"enabled": True, "pulled": True}
    # A conflicting rebase would leave the tree mid-rebase; back out of it.
    _git(repo, "rebase", "--abort", timeout=30)
    return {"enabled": True, "pulled": False, "detail": result.detail}


def commit_and_push(message: str, root: Path | None = None) -> dict[str, Any]:
    """Commit everything the catalog's .gitignore lets through and push it.

    A rejected push (the other checkout pushed first) is rebased once and
    pushed again. A rebase that conflicts is aborted and reported as
    ``rejected`` with the commit still local; see :func:`drop_unpushed_commit`.
    """
    if not enabled(root):
        return {"enabled": False}
    repo = repo_root(root)
    assert repo is not None
    add = _git(repo, "add", "-A")
    if not add.ok:
        return {"enabled": True, "committed": False, "detail": add.detail}
    if _git(repo, "diff", "--cached", "--quiet").ok:
        # Nothing new here, but a commit made by hand (a README edit, a note
        # typed in the record) may still be waiting; push it so the other
        # checkout sees it.
        ahead = _git(repo, "rev-list", "--count", "@{u}..HEAD", timeout=30)
        if ahead.ok and ahead.stdout.strip() not in ("", "0"):
            push = _git(repo, "push", "-q")
            return {
                "enabled": True,
                "committed": False,
                "pushed": push.ok,
                "detail": f"pushed {ahead.stdout.strip()} waiting commit(s)" if push.ok else push.detail,
            }
        return {"enabled": True, "committed": False, "pushed": False, "detail": "nothing to commit"}
    commit = _git(repo, "commit", "-q", "-m", message)
    if not commit.ok:
        return {"enabled": True, "committed": False, "detail": commit.detail}
    push = _git(repo, "push", "-q")
    if push.ok:
        return {"enabled": True, "committed": True, "pushed": True, "message": message}
    rebase = _git(repo, "pull", "--rebase", "-q")
    if not rebase.ok:
        _git(repo, "rebase", "--abort", timeout=30)
        return {
            "enabled": True,
            "committed": True,
            "pushed": False,
            "rejected": True,
            "message": message,
            "detail": rebase.detail or push.detail,
        }
    push = _git(repo, "push", "-q")
    return {
        "enabled": True,
        "committed": True,
        "pushed": push.ok,
        "rejected": not push.ok,
        "message": message,
        **({} if push.ok else {"detail": push.detail}),
    }


def drop_unpushed_commit(message: str, root: Path | None = None) -> dict[str, Any]:
    """Undo our own unpushed commit (and only it) after a rejected push.

    Refuses unless exactly one commit is ahead of upstream and its subject is
    ``message``, so nothing but the write we just made can be discarded.
    """
    repo = repo_root(root)
    if repo is None:
        return {"dropped": False, "detail": "no catalog repository"}
    ahead = _git(repo, "log", "@{u}..HEAD", "--format=%s", timeout=30)
    subjects = [line for line in ahead.stdout.splitlines() if line.strip()] if ahead.ok else None
    if subjects != [message]:
        return {"dropped": False, "detail": f"unexpected local commits: {subjects}"}
    reset = _git(repo, "reset", "--hard", "-q", "@{u}", timeout=30)
    if not reset.ok:
        return {"dropped": False, "detail": reset.detail}
    pulled = pull(root)
    return {"dropped": True, **{f"then_{k}": v for k, v in pulled.items()}}


def synced(message: Callable[..., str] | str):
    """Decorate a write entry point: pull first, commit and push after.

    ``message`` is the commit subject or a function of the wrapped call's
    ``(result, *args, **kwargs)`` returning one. The sync outcome is attached
    to a dict result under ``catalog_sync``. The wrapped function's signature
    is preserved so FastMCP still derives the tool schema from it.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            pulled = pull()
            result = fn(*args, **kwargs)
            # Callers (FastMCP, the CLI) may pass arguments positionally; the
            # message function always sees them by name.
            try:
                named = dict(signature.bind_partial(*args, **kwargs).arguments)
            except TypeError:
                named = dict(kwargs)
            subject = message(result, **named) if callable(message) else message
            if subject and _wrote(result):
                pushed = commit_and_push(subject)
            else:
                pushed = {"enabled": pulled.get("enabled", False), "committed": False}
            if isinstance(result, dict):
                result["catalog_sync"] = {"pull": pulled, "push": pushed}
            elif pulled.get("enabled"):
                # CLI commands return an exit code; say what happened on stderr.
                import sys

                pulled_text = "pulled" if pulled.get("pulled") else f"pull failed: {pulled.get('detail')}"
                if pushed.get("pushed"):
                    pushed_text = f"pushed '{pushed.get('message')}'"
                elif pushed.get("rejected"):
                    pushed_text = f"push rejected: {pushed.get('detail')}"
                else:
                    pushed_text = pushed.get("detail") or "nothing to push"
                print(f"catalog sync: {pulled_text}; {pushed_text}", file=sys.stderr)
            return result

        return wrapper

    return decorate


def _wrote(result: Any) -> bool:
    """Skip the commit for plan-only or refused calls; commit for everything else."""
    if not isinstance(result, dict):
        return True
    for key in ("reserved", "written", "adopted", "staged", "submitted", "synced", "cancelled"):
        if key in result:
            return bool(result[key])
    if result.get("writes") is False:
        return False
    return True
