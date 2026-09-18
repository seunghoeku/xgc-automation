"""User-facing CLI; monitoring remains in the shared deterministic queue tools."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from . import __version__, config, install
from .core import machines, queue_menu, queue_watch, ssh


@contextmanager
def poll_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "poll.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another poll is already running for this config") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def render(cfg: config.Config) -> str:
    snapshots = [
        queue_watch.load_state(path) | {"machine": name}
        for name, path in cfg.state_files().items()
    ]
    return queue_menu.render(snapshots, history_limit=cfg.history_limit,
                             stale_after_seconds=2 * cfg.poll_interval)


def poll(cfg: config.Config, *, no_notify: bool = False) -> dict:
    if not shutil.which("ssh"):
        raise ValueError("OpenSSH is required; ssh was not found on PATH")
    with poll_lock(cfg.cache_dir), machines.using_machines(cfg.machines):
        result = queue_watch.watch_many(
            list(cfg.machines), state_files=cfg.state_files(),
            send_notification=False, history_limit=cfg.history_limit,
        )
    if cfg.notify and not no_notify and result["changes"]:
        queue_watch.notify("HPC Queue Monitor", "; ".join(result["changes"])[:500])
    return result


def ssh_config(cfg: config.Config) -> str:
    lines = ["# Review and add these entries to ~/.ssh/config; this command writes nothing."]
    for name, server in cfg.machines.items():
        alias = server["auth"].get("ssh_config_host") or server["host"]
        lines.extend(["", f"Host {alias}", f"    HostName {server['host'] or alias}",
                      f"    User {server['user']}"])
        method = server["auth"]["method"]
        if method == "controlmaster":
            lines.extend(["    ControlMaster auto", "    ControlPath ~/.ssh/cm-%C",
                          "    ControlPersist 8h"])
        elif method == "sshproxy":
            lines.extend(["    ControlMaster no", "    ControlPath none",
                          "    IdentitiesOnly yes", "    PreferredAuthentications publickey",
                          "    # Set paths to your current NERSC sshproxy key/certificate:",
                          "    # IdentityFile ~/.ssh/nersc",
                          "    # CertificateFile ~/.ssh/nersc-cert.pub"])
        else:
            lines.append("    # Set IdentityFile to your SSH private key if needed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hpc-monitor")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=config.default_config(),
                        help="server settings YAML (place before the command)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="write the example server list without overwriting")
    commands.add_parser("check", help="validate configuration locally without SSH")
    commands.add_parser("ssh-config", help="print suggested OpenSSH entries for review")
    commands.add_parser("menu", help="render cached SwiftBar output; never poll or write")
    polling = commands.add_parser("poll", help="poll all enabled servers once")
    polling.add_argument("--no-notify", action="store_true")
    polling.add_argument("--json", action="store_true")
    polling.add_argument("--rotate-log", type=Path)
    setup = commands.add_parser("install", help="install the timer and SwiftBar plugin on macOS")
    setup.add_argument("--plugin-dir", type=Path)
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--no-start", action="store_true")
    commands.add_parser("start", help="start the installed timer (macOS)")
    commands.add_parser("stop", help="stop the timer until next login/start (macOS)")
    commands.add_parser("status", help="show timer status (macOS)")
    remove = commands.add_parser("uninstall", help="remove timer/plugin, keep settings/history")
    remove.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in {"install", "uninstall", "start", "stop", "status"}:
            if sys.platform != "darwin":
                raise ValueError("Timer installation requires macOS; use 'poll' from cron on Linux.")
        if args.command == "init":
            path = args.config.expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(files("hpc_monitor").joinpath("servers.example.yaml").read_text())
            path.chmod(0o600)
            print(f"Created {path}\nEnable your servers and replace <username> placeholders.")
            return 0
        if args.command == "uninstall":
            install.uninstall(yes=args.yes)
            return 0
        if args.command in {"start", "stop"}:
            getattr(install, args.command)()
            print(f"Timer {'started' if args.command == 'start' else 'stopped'}.")
            return 0
        if args.command == "status":
            print("Timer running" if install.service_loaded() else "Timer not loaded")
            print(f"Timer: {install.service_file()}\nLog: {install.log_file()}")
            return 0
        cfg = config.load(args.config)
        if args.command == "menu":
            print(render(cfg))
        elif args.command == "check":
            print(f"Config valid: {cfg.path}")
            for name, server in cfg.machines.items():
                print(f"  {name}: {ssh.host_for(server).host}, "
                      f"{server['scheduler']['type']}, user {server['user']}, "
                      f"auth {server['auth']['method']}")
            print(f"Polling: {cfg.poll_interval}s; history: {cfg.history_limit}; "
                  f"cache: {cfg.cache_dir}")
            print("Local validation only. Run 'hpc-monitor poll --no-notify' to test access.")
        elif args.command == "ssh-config":
            print(ssh_config(cfg))
        elif args.command == "install":
            install.install(cfg, args.plugin_dir, dry_run=args.dry_run, no_start=args.no_start)
        elif args.command == "poll":
            result = poll(cfg, no_notify=args.no_notify)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                queue_watch._print_report(result, always_report=True)
            sys.stdout.flush()
            if args.rotate_log:
                queue_watch.rotate_log(args.rotate_log.expanduser(),
                                       marker=cfg.cache_dir / "log.rotated-at")
            return queue_watch.exit_code_for(result, list(cfg.machines))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if args.command == "menu":
            # A bad config must show a readable item instead of disappearing.
            print("HPC ⚠\n---\n" + queue_menu.clean(exc))
            print("Edit servers.yaml, then refresh. | refresh=true")
            return 0
        print(f"hpc-monitor: {exc}", file=sys.stderr)
        return 1
