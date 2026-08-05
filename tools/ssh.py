"""SSH transport layer.

All remote execution and file transfer goes through here. Two invariants:

1. We never authenticate. Commands run over an existing ControlMaster socket
   that the user opened (AGENTS.md, absolute rule 3). If the socket is not
   alive, we fail with an actionable message instead of prompting.
2. Commands are passed as argument lists and quoted with shlex, never
   interpolated into a shell string. This keeps the "deterministic actions go
   through tools" rule honest (absolute rule 2).

BatchMode=yes is set on every call so that a missing socket fails fast rather
than blocking on an interactive prompt.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


class SSHError(RuntimeError):
    """Raised when the remote command could not be executed at all.

    This is distinct from a command that ran and returned nonzero; that is
    reported through CommandResult.returncode.
    """


@dataclass
class CommandResult:
    """Outcome of a remote command."""

    returncode: int
    stdout: str
    stderr: str
    command: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> "CommandResult":
        """Return self if successful, otherwise raise SSHError."""
        if not self.ok:
            raise SSHError(
                f"remote command failed (rc={self.returncode}): {self.command}\n"
                f"stderr: {self.stderr.strip()}"
            )
        return self


def control_socket_alive(ssh_host: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Check the ControlMaster socket via `ssh -O check`.

    Returns:
        (alive, detail). Never raises.
    """
    try:
        proc = subprocess.run(
            ["ssh", "-O", "check", ssh_host],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "ssh executable not found"
    except subprocess.TimeoutExpired:
        return False, "ssh -O check timed out"

    msg = (proc.stderr or proc.stdout or "").strip()
    alive = proc.returncode == 0
    return alive, msg or ("master running" if alive else "no master")


def command_channel_usable(
    ssh_host: str, timeout: float = 10.0
) -> tuple[bool, str]:
    """Check that a live master can actually open a non-interactive channel.

    ``ssh -O check`` only checks the local master process. A stale Polaris
    connection, or a Frontier connection whose sole allowed channel is still
    occupied by an interactive shell, can pass that check while every tool
    command hangs. This bounded ``true`` probe distinguishes those states.

    Returns:
        ``(usable, detail)``. Never raises and never authenticates because
        BatchMode is mandatory.
    """
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", ssh_host, "true"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "ssh executable not found"
    except subprocess.TimeoutExpired:
        return False, f"non-interactive command channel timed out after {timeout}s"

    msg = (proc.stderr or proc.stdout or "").strip()
    usable = proc.returncode == 0
    return usable, msg or ("command channel usable" if usable else "command channel rejected")


def certificate_status(ssh_host: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """Inspect the OpenSSH certificate selected by an SSH config alias.

    Returns ``None`` when the alias has no certificate configured. The check is
    local and read-only; it does not contact the remote host or authenticate.
    """
    try:
        config = subprocess.run(
            ["ssh", "-G", ssh_host],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"present": False, "valid": False, "detail": "ssh executable not found"}
    except subprocess.TimeoutExpired:
        return {"present": False, "valid": False, "detail": "ssh -G timed out"}

    certificate = None
    for line in config.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() == "certificatefile" and value and value.lower() != "none":
            certificate = value.strip()
            break
    if certificate is None:
        return None

    path = Path(certificate).expanduser()
    if not path.exists():
        return {
            "path": str(path),
            "present": False,
            "valid": False,
            "detail": "configured certificate file is missing",
        }

    try:
        decoded = subprocess.run(
            ["ssh-keygen", "-L", "-f", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "path": str(path),
            "present": True,
            "valid": False,
            "detail": f"could not inspect certificate: {exc}",
        }
    if decoded.returncode != 0:
        return {
            "path": str(path),
            "present": True,
            "valid": False,
            "detail": (decoded.stderr or "ssh-keygen rejected certificate").strip(),
        }

    match = re.search(
        r"Valid:\s+from\s+(\S+)\s+to\s+(\S+)", decoded.stdout
    )
    if match is None:
        return {
            "path": str(path),
            "present": True,
            "valid": False,
            "detail": "certificate validity window was not found",
        }

    try:
        valid_from = datetime.fromisoformat(match.group(1))
        valid_to = datetime.fromisoformat(match.group(2))
        now = datetime.now()
        valid = valid_from <= now <= valid_to
    except ValueError:
        return {
            "path": str(path),
            "present": True,
            "valid": False,
            "detail": "certificate validity window could not be parsed",
        }

    return {
        "path": str(path),
        "present": True,
        "valid": valid,
        "valid_from": valid_from.isoformat(),
        "valid_to": valid_to.isoformat(),
        "detail": "certificate is current" if valid else "certificate is not current",
    }


def require_session(ssh_host: str) -> None:
    """Fail unless a live ControlMaster session exists.

    We deliberately refuse to open one ourselves: doing so would trigger an MFA
    prompt that the user, not the tool, must answer.
    """
    alive, detail = control_socket_alive(ssh_host)
    if alive:
        return
    if "no controlpath" in detail.lower():
        raise SSHError(
            f"no ControlMaster configuration for host {ssh_host!r} in ~/.ssh/config. "
            f"See docs/ssh-setup.md step 1."
        )
    raise SSHError(
        f"no live ssh session for {ssh_host!r} ({detail}). "
        f"Run `ssh {ssh_host}` yourself to authenticate, then retry. "
        f"See docs/ssh-setup.md step 2."
    )


def run(
    ssh_host: str,
    argv: Sequence[str],
    *,
    timeout: float = 120.0,
    cwd: str | None = None,
    env_script: str | None = None,
    check: bool = False,
) -> CommandResult:
    """Run a command on the remote host over the existing ssh session.

    Args:
        ssh_host: ~/.ssh/config alias with ControlMaster configured.
        argv: command as an argument list; quoted with shlex before sending.
        cwd: remote working directory, if any.
        env_script: script to source first (e.g. a machine's XGC env script).
        check: raise SSHError on nonzero exit.
    """
    require_session(ssh_host)

    remote = shlex.join(argv)
    if cwd:
        remote = f"cd {shlex.quote(cwd)} && {remote}"
    if env_script:
        # `set -e` so a failing source aborts instead of running the command in
        # a half-configured environment.
        remote = f"set -e; source {shlex.quote(env_script)} >/dev/null 2>&1; {remote}"

    full = ["ssh", "-o", "BatchMode=yes", ssh_host, remote]
    try:
        proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SSHError(f"remote command timed out after {timeout}s: {remote}")

    result = CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        command=remote,
    )
    return result.check() if check else result


def write_file(ssh_host: str, remote_path: str, content: str, *, timeout: float = 60.0) -> None:
    """Write text to a remote path by streaming it over stdin.

    Avoids a temp file on the local side and avoids embedding the content in the
    command line, which would break on quoting and hit ARG_MAX.
    """
    require_session(ssh_host)

    remote = f"cat > {shlex.quote(remote_path)}"
    full = ["ssh", "-o", "BatchMode=yes", ssh_host, remote]
    try:
        proc = subprocess.run(
            full, input=content, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise SSHError(f"write to {remote_path} timed out after {timeout}s")

    if proc.returncode != 0:
        raise SSHError(f"failed to write {remote_path}: {proc.stderr.strip()}")


def read_file(ssh_host: str, remote_path: str, *, timeout: float = 60.0) -> str:
    """Read a remote text file."""
    return run(ssh_host, ["cat", remote_path], timeout=timeout, check=True).stdout


def mkdir_p(ssh_host: str, remote_path: str, *, timeout: float = 30.0) -> None:
    """Create a remote directory, including parents."""
    run(ssh_host, ["mkdir", "-p", remote_path], timeout=timeout, check=True)


def exists(ssh_host: str, remote_path: str, *, timeout: float = 30.0) -> bool:
    """Whether a remote path exists."""
    return run(ssh_host, ["test", "-e", remote_path], timeout=timeout).ok


def pull(
    ssh_host: str,
    remote_path: str,
    local_path: str,
    *,
    timeout: float = 600.0,
) -> CommandResult:
    """Copy a remote file or directory to the local filesystem via scp.

    scp reuses the same ControlMaster socket as ssh.

    Note: XGC output can reach terabytes. Only pull reduced products
    (AGENTS.md, data principles). Nothing here enforces that -- callers must.
    """
    require_session(ssh_host)

    argv = ["scp", "-o", "BatchMode=yes", "-r", f"{ssh_host}:{remote_path}", local_path]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SSHError(f"pull timed out after {timeout}s: {remote_path}")

    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        command=" ".join(argv),
    ).check()


def push(
    ssh_host: str,
    local_path: str,
    remote_path: str,
    *,
    timeout: float = 600.0,
) -> CommandResult:
    """Copy a local file or directory to the remote host via scp."""
    require_session(ssh_host)

    argv = ["scp", "-o", "BatchMode=yes", "-r", local_path, f"{ssh_host}:{remote_path}"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SSHError(f"push timed out after {timeout}s: {local_path}")

    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        command=" ".join(argv),
    ).check()


def host_for(machine_cfg: dict[str, Any]) -> str:
    """Resolve the ssh target for a machine definition.

    Prefers the ~/.ssh/config alias, since that is where ControlMaster lives.
    """
    from tools import machines as m

    auth = machine_cfg.get("auth", {})
    alias = auth.get("ssh_config_host")
    if alias and not m.is_placeholder(alias):
        return alias

    user, host = machine_cfg.get("user"), machine_cfg.get("host")
    if not host:
        raise SSHError("machine definition has neither ssh_config_host nor host")
    if m.is_placeholder(user):
        user = None  # fall back to the local username rather than "<...>@host"
    return f"{user}@{host}" if user else host
