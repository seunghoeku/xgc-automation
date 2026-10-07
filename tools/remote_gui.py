"""Launch XGC-Devel's GUI against a remote simulation directory.

``xgc-remote-gui`` is an orchestration layer, not a second GUI. It resolves a
target from explicit input or local provenance, validates the remote runtime,
starts XGC-Devel's Flask app on the remote login node, creates a loopback-only
SSH tunnel, and opens the local Monitor page.

No scheduler command is used. The SSH process owns both the tunnel and the
remote Flask process, so stopping the local session closes both.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from tools import machines as m
from tools import provenance as prov
from tools import ssh


from tools import paths

PROJECT_ROOT = paths.PROJECT_ROOT
RUNS_DIR = paths.RUNS_DIR  # follows XGC_CATALOG_DIR (tools/paths.py)
SESSION_DIR = paths.CACHE_DIR / "remote-gui"
SESSION_ID_RE = re.compile(r"^gui-[A-Za-z0-9_.-]+$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RemoteGUIError(RuntimeError):
    """A remote GUI plan or session could not be completed safely."""


@dataclass(frozen=True)
class RemoteGUIPlan:
    machine: str
    target_source: str
    remote_directory: str
    monitor_source: str | None
    preset: str | None
    gui_source: str
    gui_python: str
    gui_env_script: str | None
    remote_port: int
    local_port: int
    local_url: str
    authentication: str | None
    control_master: bool
    scheduler_used: bool
    discovered_sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_remote_path(value: str, label: str) -> str:
    value = value.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise RemoteGUIError(f"{label} must be a non-empty remote path")
    normalized = posixpath.normpath(value)
    if not normalized.startswith("/"):
        raise RemoteGUIError(f"{label} must be an absolute remote path: {value!r}")
    if normalized == "/":
        raise RemoteGUIError(f"{label} may not be the remote filesystem root")
    return normalized


def _load_provenance(run_id: str) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise RemoteGUIError(f"invalid run_id: {run_id!r}")
    try:
        found = prov.find_record(RUNS_DIR, run_id)
    except prov.ProvenanceError as exc:
        raise RemoteGUIError(str(exc)) from exc
    if found is None:
        raise RemoteGUIError(f"local provenance not found for run_id {run_id!r}")
    _, path = found
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteGUIError(f"cannot read provenance for {run_id!r}: {exc}") from exc
    if not isinstance(record, dict):
        raise RemoteGUIError(f"provenance for {run_id!r} is not a JSON object")
    return record


def search_provenance(query: str, *, machine: str | None = None) -> list[dict[str, Any]]:
    """Search local run provenance without scanning a remote filesystem."""
    needle = query.strip().lower()
    if not needle:
        raise RemoteGUIError("search query must not be empty")
    matches: list[dict[str, Any]] = []
    if not RUNS_DIR.exists():
        return matches
    for provenance_class, path in prov.iter_records(RUNS_DIR):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if machine and record.get("machine") != machine:
            continue
        fields = [
            path.parent.name,
            provenance_class,
            record.get("run_id"),
            record.get("simulation_id"),
            record.get("job_id"),
            record.get("job_name"),
            record.get("remote_run_dir"),
            record.get("case_dir"),
        ]
        haystack = "\n".join(str(value) for value in fields if value is not None).lower()
        if needle in haystack:
            try:
                provenance_path = str(path.relative_to(PROJECT_ROOT))
            except ValueError:
                provenance_path = str(path)
            matches.append(
                {
                    "run_id": record.get("run_id") or path.parent.name,
                    "provenance_class": record.get("provenance_class")
                    or provenance_class,
                    "machine": record.get("machine"),
                    "simulation_id": record.get("simulation_id"),
                    "job_id": record.get("job_id"),
                    "job_name": record.get("job_name"),
                    "remote_run_dir": record.get("remote_run_dir")
                    or record.get("case_dir"),
                    "provenance": provenance_path,
                }
            )
    return matches


def _resolve_target(
    machine: str,
    *,
    remote_directory: str | None,
    run_id: str | None,
    simulation_id: str | None,
    query: str | None,
) -> tuple[str, str]:
    selectors = [
        remote_directory is not None,
        run_id is not None,
        simulation_id is not None,
        query is not None,
    ]
    if sum(selectors) != 1:
        raise RemoteGUIError(
            "select exactly one target: remote_directory, run_id, simulation_id, or query"
        )
    if remote_directory is not None:
        return _validate_remote_path(remote_directory, "remote_directory"), "explicit"

    if run_id is not None:
        record = _load_provenance(run_id)
        if record.get("machine") and record["machine"] != machine:
            raise RemoteGUIError(
                f"run {run_id!r} belongs to {record['machine']!r}, not {machine!r}"
            )
        path = record.get("remote_run_dir") or record.get("case_dir")
        if not isinstance(path, str) or not path.strip():
            raise RemoteGUIError(f"run {run_id!r} has no recorded remote directory")
        return _validate_remote_path(path, "provenance remote directory"), f"run:{run_id}"

    needle = simulation_id if simulation_id is not None else query
    matches = search_provenance(str(needle), machine=machine)
    if not matches:
        kind = "simulation_id" if simulation_id is not None else "query"
        raise RemoteGUIError(f"no local provenance matches {kind} {needle!r}")
    if simulation_id is not None:
        matches = [item for item in matches if item.get("simulation_id") == simulation_id]
    if len(matches) != 1:
        candidates = ", ".join(str(item["run_id"]) for item in matches[:10])
        raise RemoteGUIError(
            f"target is ambiguous ({len(matches)} matches): {candidates}; use --run-id or --dir"
        )
    path = matches[0].get("remote_run_dir")
    if not isinstance(path, str) or not path.strip():
        raise RemoteGUIError(f"matched run {matches[0]['run_id']!r} has no remote directory")
    return _validate_remote_path(path, "provenance remote directory"), (
        f"simulation:{simulation_id}" if simulation_id is not None else f"query:{query}"
    )


def _available_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _random_remote_port() -> int:
    # High ephemeral range; launch failure remains explicit if a rare race occurs.
    return 30000 + int.from_bytes(os.urandom(2), "big") % 20000


def _gui_config(machine: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = m.get_machine(machine)
    gui = cfg.get("gui") or {}
    missing = [name for name in ("source", "python") if not gui.get(name)]
    if missing:
        raise RemoteGUIError(
            f"remote GUI is not configured for {machine!r}; set "
            + ", ".join(f"gui.{name}" for name in missing)
            + " in machines.local.yaml"
        )
    source = _validate_remote_path(str(gui["source"]), "gui.source")
    python = str(gui["python"]).strip()
    if not python or any(char in python for char in "\x00\n\r"):
        raise RemoteGUIError("gui.python must be a remote executable path or command")
    env_script = gui.get("env_script")
    if env_script:
        env_script = _validate_remote_path(str(env_script), "gui.env_script")
    return cfg, {"source": source, "python": python, "env_script": env_script}


_REMOTE_INSPECTION_CODE = r"""
import importlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
app = pathlib.Path(sys.argv[2])
source = sys.argv[3]
preset = sys.argv[4]

errors = []
if not root.is_dir():
    errors.append(f"remote simulation directory not found: {root}")
if not app.is_file():
    errors.append(f"XGC GUI app not found: {app}")
if source and not (root / source).exists():
    errors.append(f"monitor source not found: {root / source}")
if preset and preset != "auto" and not pathlib.Path(preset).is_file():
    errors.append(f"monitor preset not found: {preset}")

missing = []
for name in ("flask", "numpy", "matplotlib", "adios2"):
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}: {exc}")
if missing:
    errors.append("missing GUI Python dependencies: " + "; ".join(missing))

presets = []
sources = []
if root.is_dir():
    presets = sorted(str(path) for path in root.glob("*.xgcg") if path.is_file())
    sources = sorted(path.name for path in root.iterdir() if path.name.endswith(".bp"))

payload = {"errors": errors, "presets": presets, "sources": sources[:20]}
print("XGC_REMOTE_GUI_JSON=" + json.dumps(payload, separators=(",", ":")))
raise SystemExit(1 if errors else 0)
""".strip()


def _inspect_remote(
    target: ssh.SSHTarget,
    *,
    remote_directory: str,
    app_path: str,
    gui_python: str,
    env_script: str | None,
    monitor_source: str | None,
    preset: str | None,
) -> dict[str, Any]:
    """Validate all remote GUI inputs through one SSH connection."""
    result = ssh.run(
        target,
        [
            gui_python,
            "-c",
            _REMOTE_INSPECTION_CODE,
            remote_directory,
            app_path,
            monitor_source or "",
            preset or "",
        ],
        env_script=env_script,
        timeout=60.0,
    )
    marker = "XGC_REMOTE_GUI_JSON="
    payload_line = next(
        (line[len(marker) :] for line in result.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    if payload_line is None:
        detail = (result.stderr or result.stdout).strip()
        raise RemoteGUIError(
            "remote GUI inspection could not run"
            + (f": {detail}" if detail else "")
        )
    try:
        payload = json.loads(payload_line)
    except json.JSONDecodeError as exc:
        raise RemoteGUIError(f"remote GUI inspection returned invalid JSON: {exc}") from exc
    errors = payload.get("errors") or []
    if result.returncode != 0 or errors:
        raise RemoteGUIError("; ".join(str(item) for item in errors) or "remote GUI inspection failed")
    return payload


def plan_remote_gui(
    machine: str,
    *,
    remote_directory: str | None = None,
    run_id: str | None = None,
    simulation_id: str | None = None,
    query: str | None = None,
    source: str | None = None,
    preset: str | None = None,
    local_port: int | None = None,
    remote_port: int | None = None,
    inspect_remote: bool = True,
) -> RemoteGUIPlan:
    """Resolve and validate a remote Monitor launch without starting it."""
    directory, target_source = _resolve_target(
        machine,
        remote_directory=remote_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        query=query,
    )
    cfg, gui = _gui_config(machine)
    target = ssh.host_for(cfg)
    app_path = posixpath.join(gui["source"], "utils", "input_gui", "app.py")

    monitor_source = source.strip() if source else None
    if monitor_source and (
        "/" in monitor_source or "\x00" in monitor_source or "\n" in monitor_source
    ):
        raise RemoteGUIError("source must be a top-level BP source name, not a path")

    chosen_preset: str | None = None
    if preset and preset != "auto":
        chosen_preset = _validate_remote_path(preset, "preset")

    discovered_sources: tuple[str, ...] = ()
    if inspect_remote:
        inspection = _inspect_remote(
            target,
            remote_directory=directory,
            app_path=app_path,
            gui_python=gui["python"],
            env_script=gui["env_script"],
            monitor_source=monitor_source,
            preset=preset,
        )
        if preset == "auto":
            presets = inspection.get("presets") or []
            if len(presets) == 1:
                chosen_preset = presets[0]
            elif len(presets) > 1:
                raise RemoteGUIError(
                    "multiple monitor presets found; choose one explicitly: "
                    + ", ".join(presets)
                )
        discovered_sources = tuple(inspection.get("sources") or ())

    selected_local_port = local_port or _available_local_port()
    selected_remote_port = remote_port or int(cfg.get("gui", {}).get("remote_port") or 0)
    selected_remote_port = selected_remote_port or _random_remote_port()
    for label, value in (("local_port", selected_local_port), ("remote_port", selected_remote_port)):
        if not 1024 <= value <= 65535:
            raise RemoteGUIError(f"{label} must be between 1024 and 65535")

    return RemoteGUIPlan(
        machine=machine,
        target_source=target_source,
        remote_directory=directory,
        monitor_source=monitor_source,
        preset=chosen_preset,
        gui_source=gui["source"],
        gui_python=gui["python"],
        gui_env_script=gui["env_script"],
        remote_port=selected_remote_port,
        local_port=selected_local_port,
        local_url=f"http://127.0.0.1:{selected_local_port}/monitor",
        authentication=cfg.get("auth", {}).get("method"),
        control_master=cfg.get("auth", {}).get("method") != "sshproxy",
        scheduler_used=False,
        discovered_sources=discovered_sources,
    )


def _remote_command(plan: RemoteGUIPlan) -> str:
    app_path = posixpath.join(plan.gui_source, "utils", "input_gui", "app.py")
    commands = ["set -e"]
    if plan.gui_env_script:
        commands.append(f"source {shlex.quote(plan.gui_env_script)} >/dev/null 2>&1")
    commands.extend(
        [
            f"cd {shlex.quote(plan.remote_directory)}",
            "export XGC_OPEN_BROWSER=0",
            "export XGC_INPUT_GUI_STARTUP_MODE=monitor",
            f"export XGC_INPUT_GUI_STARTUP_TARGET={shlex.quote(plan.remote_directory)}",
            f"export XGC_INPUT_GUI_STARTUP_SOURCE={shlex.quote(plan.monitor_source or '')}",
            f"export XGC_INPUT_GUI_STARTUP_CONFIG={shlex.quote(plan.preset or '')}",
            "export XGC_INPUT_GUI_HOST=127.0.0.1",
            f"export XGC_INPUT_GUI_PORT={plan.remote_port}",
            f"exec {shlex.quote(plan.gui_python)} {shlex.quote(app_path)}",
        ]
    )
    return "; ".join(commands)


def _ensure_session_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        SESSION_DIR.chmod(0o700)
    except OSError:
        pass


def _session_path(session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise RemoteGUIError(f"invalid remote GUI session id: {session_id!r}")
    return SESSION_DIR / f"{session_id}.json"


def _write_state(state: dict[str, Any]) -> None:
    _ensure_session_dir()
    path = _session_path(str(state["session_id"]))
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_state(session_id: str) -> dict[str, Any]:
    path = _session_path(session_id)
    if not path.exists():
        raise RemoteGUIError(f"remote GUI session not found: {session_id}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteGUIError(f"cannot read session {session_id!r}: {exc}") from exc
    if not isinstance(state, dict):
        raise RemoteGUIError(f"session {session_id!r} is malformed")
    return state


def _process_command(pid: int) -> str | None:
    try:
        proc = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _owned_process_alive(state: dict[str, Any]) -> bool:
    try:
        pid = int(state["pid"])
        local_port = int(state["plan"]["local_port"])
    except (KeyError, TypeError, ValueError):
        return False
    command = _process_command(pid)
    if command is None:
        url = str(state.get("plan", {}).get("local_url") or "")
        # Restricted launchers such as Codex may deny both `ps` and signal-0
        # checks for a process that they started outside the sandbox. The
        # session-specific loopback Monitor URL is then the usable ownership
        # signal: no remote or non-loopback endpoint is consulted.
        return bool(url) and _url_ready(url)
    return bool(
        command
        and "ssh" in command
        and "ExitOnForwardFailure=yes" in command
        and f"127.0.0.1:{local_port}:127.0.0.1:" in command
    )


def _url_ready(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError):
        return False


def _log_tail(path: Path, max_lines: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def launch_remote_gui(
    plan: RemoteGUIPlan,
    *,
    open_browser: bool = True,
    ready_timeout: float = 45.0,
) -> dict[str, Any]:
    """Start a detached local SSH supervisor and wait for the Monitor page."""
    cfg = m.get_machine(plan.machine)
    target = ssh.host_for(cfg)
    host, auth_options = ssh.connection_options(target)
    session_id = f"gui-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(2).hex()}"
    _ensure_session_dir()
    log_path = SESSION_DIR / f"{session_id}.log"
    forward = f"127.0.0.1:{plan.local_port}:127.0.0.1:{plan.remote_port}"
    argv = [
        "ssh",
        *auth_options,
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        forward,
        host,
        _remote_command(plan),
    ]

    with log_path.open("ab") as log:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    state: dict[str, Any] = {
        "session_id": session_id,
        "status": "starting",
        "created_at": _now_iso(),
        "pid": process.pid,
        "log_path": str(log_path),
        "plan": plan.to_dict(),
    }
    _write_state(state)

    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            state.update(status="failed", returncode=returncode, finished_at=_now_iso())
            _write_state(state)
            detail = _log_tail(log_path)
            raise RemoteGUIError(
                f"remote GUI exited before becoming ready (rc={returncode})"
                + (f"\n{detail}" if detail else "")
            )
        if _url_ready(plan.local_url):
            state.update(status="running", ready_at=_now_iso())
            _write_state(state)
            if open_browser:
                webbrowser.open(plan.local_url)
            return state
        time.sleep(0.25)

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    state.update(status="failed", finished_at=_now_iso())
    _write_state(state)
    detail = _log_tail(log_path)
    raise RemoteGUIError(
        f"remote GUI did not become ready within {ready_timeout:.0f}s"
        + (f"\n{detail}" if detail else "")
    )


def remote_gui_status(session_id: str | None = None) -> dict[str, Any]:
    """Return one session or all recorded sessions with live-state checks."""
    _ensure_session_dir()
    if session_id:
        states = [_read_state(session_id)]
    else:
        states = []
        for path in sorted(SESSION_DIR.glob("gui-*.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(state, dict):
                states.append(state)

    output: list[dict[str, Any]] = []
    for state in states:
        item = dict(state)
        alive = _owned_process_alive(item)
        url = item.get("plan", {}).get("local_url", "")
        ready = bool(url and alive and _url_ready(str(url)))
        item["process_alive"] = alive
        item["url_ready"] = ready
        if item.get("status") in {"starting", "running"} and not alive:
            item["status"] = "exited"
        elif alive and ready:
            item["status"] = "running"
        elif alive:
            item["status"] = "unhealthy"
        output.append(item)
    return {"sessions": output}


def stop_remote_gui(session_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Stop a tunnel process created by this launcher and mark its state."""
    state = _read_state(session_id)
    alive = _owned_process_alive(state)
    if alive:
        pid = int(state["pid"])
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _owned_process_alive(state):
            time.sleep(0.1)
        if _owned_process_alive(state):
            os.killpg(pid, signal.SIGKILL)
    state.update(status="stopped", stopped_at=_now_iso())
    _write_state(state)
    return {
        "session_id": session_id,
        "stopped": alive,
        "status": "stopped",
        "local_url": state.get("plan", {}).get("local_url"),
    }


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", dest="remote_directory")
    group.add_argument("--run-id")
    group.add_argument("--simulation-id")
    group.add_argument("--query")
    parser.add_argument("--machine", required=True)
    parser.add_argument("--source", help="top-level BP source name")
    parser.add_argument("--preset", help="absolute remote .xgcg path or 'auto'")
    parser.add_argument("--local-port", type=int)
    parser.add_argument("--remote-port", type=int)


def _plan_from_args(args: argparse.Namespace) -> RemoteGUIPlan:
    return plan_remote_gui(
        args.machine,
        remote_directory=args.remote_directory,
        run_id=args.run_id,
        simulation_id=args.simulation_id,
        query=args.query,
        source=args.source,
        preset=args.preset,
        local_port=args.local_port,
        remote_port=args.remote_port,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xgc-remote-gui",
        description="Launch XGC-Devel's GUI for a remote simulation directory.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan_parser = commands.add_parser("plan", help="resolve and validate a launch")
    _add_target_arguments(plan_parser)

    open_parser = commands.add_parser("open", help="launch the tunnel and Monitor")
    _add_target_arguments(open_parser)
    open_parser.add_argument("--no-browser", action="store_true")
    open_parser.add_argument("--ready-timeout", type=float, default=45.0)

    search_parser = commands.add_parser("search", help="search local run provenance")
    search_parser.add_argument("query")
    search_parser.add_argument("--machine")

    status_parser = commands.add_parser("status", help="show GUI session status")
    status_parser.add_argument("session_id", nargs="?")

    stop_parser = commands.add_parser("stop", help="stop one GUI session")
    stop_parser.add_argument("session_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "search":
            result: Any = {"matches": search_provenance(args.query, machine=args.machine)}
        elif args.command == "status":
            result = remote_gui_status(args.session_id)
        elif args.command == "stop":
            result = stop_remote_gui(args.session_id)
        else:
            plan = _plan_from_args(args)
            if args.command == "plan":
                result = plan.to_dict()
            else:
                result = launch_remote_gui(
                    plan,
                    open_browser=not args.no_browser,
                    ready_timeout=args.ready_timeout,
                )
        print(json.dumps(result, indent=2))
        return 0
    except (RemoteGUIError, ssh.SSHError, KeyError) as exc:
        print(f"xgc-remote-gui: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
