"""SSH transport layer.

All remote execution and file transfer goes through here. Two invariants:

1. We never perform interactive authentication. MFA-protected hosts reuse a
   ControlMaster session opened by the user. NERSC ``sshproxy`` hosts use the
   user's existing short-lived key and certificate directly.

   Direct-key hosts multiplex over a socket this toolchain owns, under
   ``~/.ssh/xgc-cm/``, never the user's own ``ControlPath``. A direct-key
   handshake to NERSC costs several seconds, and bulk operations make dozens
   of calls in a row, so reuse is the difference between seconds and minutes.
   Keeping the socket separate means automation is not sharing a channel or a
   lifetime with the user's tmux client or VS Code Remote session, which is
   what disabling multiplexing altogether was originally protecting against.
   The user's ``~/.ssh/config`` must therefore not default these hosts to
   ControlMaster; the options here are explicit either way.
2. Commands are passed as argument lists and quoted with shlex, never
   interpolated into a shell string. This keeps the "deterministic actions go
   through tools" rule honest (absolute rule 2).

BatchMode=yes is set on every call so that a missing socket fails fast rather
than blocking on an interactive prompt. The one exception is a direct-key host
with ``auth.batch_mode: false`` (stellar-ai), whose server answers the key with
an empty keyboard-interactive round that BatchMode would refuse; that call is
still non-interactive because the subprocess has no tty.

``auth.method: local`` means the checkout runs on the machine itself (a clone
of this repository on a cluster login node). ``run`` then executes the same
shell string through ``bash -c`` without ssh, ``write_file`` writes the file,
and ``pull``/``push`` copy with ``cp -r``. Every caller keeps the same API, so
higher layers stay unaware of where they run (AGENTS.md, "Higher-level logic
must remain unaware of the machine type").
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


SSH_CONNECT_TIMEOUT_SECONDS = 60.0
# A live ControlMaster may need longer to open a channel on a busy login node.
COMMAND_CHANNEL_TIMEOUT_SECONDS = 30.0

# Where the tool-owned multiplexing sockets live. Separate from the user's own
# ControlMaster sockets on purpose (see control_socket_path).
CONTROL_DIR = Path.home() / ".ssh" / "xgc-cm"

# How long an idle shared connection is kept open. Long enough that a sequence
# of commands reuses it, short enough that a stale certificate is not held past
# its usefulness.
CONTROL_PERSIST = "10m"


class SSHError(RuntimeError):
    """Raised when the remote command could not be executed at all.

    This is distinct from a command that ran and returned nonzero; that is
    reported through CommandResult.returncode.
    """


@dataclass(frozen=True)
class SSHTarget:
    """Resolved SSH host plus the configured authentication method.

    ``batch_mode`` is False only for a direct-key host whose server runs an
    empty keyboard-interactive round after the public key (stellar-ai does:
    partial success, zero prompts). ``BatchMode=yes`` makes OpenSSH refuse
    that round ("Permission denied (keyboard-interactive)"), so the call runs
    without it. Nothing becomes interactive: a real prompt still fails,
    because the subprocess has no tty, and the command timeout still applies.
    """

    host: str
    auth_method: str | None = None
    batch_mode: bool = True

    @property
    def is_local(self) -> bool:
        """The checkout runs on this machine: no ssh, commands execute directly."""
        return self.auth_method == LOCAL_METHOD


LOCAL_METHOD = "local"
LOCAL_HOST = "localhost"


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
    ssh_host: str, timeout: float = COMMAND_CHANNEL_TIMEOUT_SECONDS
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


def direct_key_channel_usable(
    ssh_host: str, timeout: float = SSH_CONNECT_TIMEOUT_SECONDS
) -> tuple[bool, str]:
    """Probe a non-multiplexed public-key SSH connection.

    This is intended for NERSC ``sshproxy`` certificates. It cannot prompt and
    cannot reuse or create a ControlMaster socket.

    Multiplexing is forced off here even though normal commands use it: this
    probe exists to prove the certificate still authenticates, and reusing an
    already-open socket would succeed without testing that.
    """
    argv = [
        "ssh",
        *_direct_key_options(),
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        f"ConnectTimeout={int(timeout)}",
    ]
    argv.extend([ssh_host, "true"])
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
        )
    except FileNotFoundError:
        return False, "ssh executable not found"
    except subprocess.TimeoutExpired:
        return False, f"direct key connection timed out after {timeout}s"

    msg = (proc.stderr or proc.stdout or "").strip()
    usable = proc.returncode == 0
    return usable, msg or ("direct key connection usable" if usable else "direct key connection rejected")


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


def _normalize_target(target: str | SSHTarget) -> SSHTarget:
    return target if isinstance(target, SSHTarget) else SSHTarget(target)


def _direct_key_options() -> list[str]:
    """Key-only, non-interactive options for a direct sshproxy connection.

    Multiplexing is decided separately by ``_sharing_options``: these options
    only ensure the connection authenticates with the certificate and never
    falls back to a prompt.
    """
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PreferredAuthentications=publickey",
    ]


def control_socket_path(host: str) -> Path:
    """Path of the tool-owned multiplexing socket for a direct-key host.

    Deliberately not the user's ``~/.ssh/cm-*`` path. Sharing a socket with a
    long-lived tmux client or VS Code Remote session would couple this
    toolchain's commands to that session's lifetime; a separate socket gets
    connection reuse without that entanglement.

    macOS caps unix socket paths near 104 characters, and ssh fails obscurely
    past it, so the name is kept short and the host is hashed rather than
    interpolated.
    """
    digest = hashlib.sha256(host.encode("utf-8")).hexdigest()[:12]
    return CONTROL_DIR / f"s-{digest}"


def _sharing_options(host: str) -> list[str]:
    """Options that reuse, and if needed create, the tool-owned socket.

    ``ControlMaster=auto`` means the first command pays for the handshake and
    every later one reuses it. That matters because a direct-key handshake to
    NERSC costs several seconds, and bulk work such as ``xgc-sim refresh``
    makes dozens of calls in a row.
    """
    if not _sharing_enabled():
        return ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        # Without a place for the socket, fall back to independent connections
        # rather than failing the command outright.
        return ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_socket_path(host)}",
        "-o",
        f"ControlPersist={CONTROL_PERSIST}",
    ]


def _sharing_enabled() -> bool:
    """Whether to multiplex direct-key connections.

    Set ``XGC_SSH_NO_MULTIPLEX=1`` to force independent connections, which is
    the escape hatch if a site ever forbids sharing.
    """
    return os.environ.get("XGC_SSH_NO_MULTIPLEX", "").strip() not in {"1", "true", "yes"}


def _connect_timeout_options() -> list[str]:
    """Return the repository-wide SSH connection timeout option."""
    return ["-o", f"ConnectTimeout={int(SSH_CONNECT_TIMEOUT_SECONDS)}"]


def connection_options(target: str | SSHTarget) -> tuple[str, list[str]]:
    """Validate the configured authentication and return SSH CLI options."""
    resolved = _normalize_target(target)
    if resolved.is_local:
        # Nothing to connect to; callers that must ssh (remote GUI tunnels)
        # cannot serve a local target and say so themselves.
        return LOCAL_HOST, []
    if resolved.auth_method == "key":
        # Generic public-key hosts do not require an MFA master or certificate.
        if resolved.batch_mode:
            key_options = _direct_key_options()
        else:
            # Empty keyboard-interactive round after the key (see SSHTarget).
            key_options = [
                "-o", "IdentitiesOnly=yes",
                "-o", "PreferredAuthentications=publickey,keyboard-interactive",
                "-o", "PasswordAuthentication=no",
            ]
        return resolved.host, [
            *key_options,
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            *_connect_timeout_options(),
        ]
    if resolved.auth_method == "sshproxy":
        certificate = certificate_status(resolved.host)
        if certificate is None:
            raise SSHError(
                f"no SSH certificate is configured for {resolved.host!r}; "
                "set CertificateFile in the SSH alias and refresh it with the "
                "user's NERSC sshproxy workflow"
            )
        if not certificate.get("valid", False):
            raise SSHError(
                f"SSH certificate for {resolved.host!r} is missing or expired "
                f"({certificate.get('detail')}); refresh it with the user's "
                "NERSC sshproxy workflow"
            )
        return resolved.host, [
            *_direct_key_options(),
            *_sharing_options(resolved.host),
            *_connect_timeout_options(),
        ]

    require_session(resolved.host)
    return resolved.host, ["-o", "BatchMode=yes", *_connect_timeout_options()]


def require_session(ssh_host: str) -> None:
    """Fail unless a live ControlMaster session exists for an MFA host."""
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
    ssh_host: str | SSHTarget,
    argv: Sequence[str],
    *,
    timeout: float = 120.0,
    cwd: str | None = None,
    env_script: str | None = None,
    check: bool = False,
) -> CommandResult:
    """Run a command on the remote host over the existing ssh session.

    Args:
        ssh_host: resolved target. ``sshproxy`` targets use a direct key;
            other targets require a configured ControlMaster alias.
        argv: command as an argument list; quoted with shlex before sending.
        cwd: remote working directory, if any.
        env_script: script to source first (e.g. a machine's XGC env script).
        check: raise SSHError on nonzero exit.
    """
    host, options = connection_options(ssh_host)

    remote = shlex.join(argv)
    if cwd:
        remote = f"cd {shlex.quote(cwd)} && {remote}"
    if env_script:
        # `set -e` so a failing source aborts instead of running the command in
        # a half-configured environment.
        remote = f"set -e; source {shlex.quote(env_script)} >/dev/null 2>&1; {remote}"

    if _normalize_target(ssh_host).is_local:
        # Same shell string, same quoting, no transport.
        full = ["bash", "-c", remote]
    else:
        full = ["ssh", *options, host, remote]
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


def write_file(
    ssh_host: str | SSHTarget,
    remote_path: str,
    content: str,
    *,
    timeout: float = 60.0,
) -> None:
    """Write text to a remote path by streaming it over stdin.

    Avoids a temp file on the local side and avoids embedding the content in the
    command line, which would break on quoting and hit ARG_MAX.
    """
    host, options = connection_options(ssh_host)

    if _normalize_target(ssh_host).is_local:
        try:
            Path(remote_path).write_text(content, encoding="utf-8")
        except OSError as exc:
            raise SSHError(f"failed to write {remote_path}: {exc}") from exc
        return

    remote = f"cat > {shlex.quote(remote_path)}"
    full = ["ssh", *options, host, remote]
    try:
        proc = subprocess.run(
            full, input=content, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise SSHError(f"write to {remote_path} timed out after {timeout}s")

    if proc.returncode != 0:
        raise SSHError(f"failed to write {remote_path}: {proc.stderr.strip()}")


def read_file(
    ssh_host: str | SSHTarget, remote_path: str, *, timeout: float = 60.0
) -> str:
    """Read a remote text file."""
    return run(ssh_host, ["cat", remote_path], timeout=timeout, check=True).stdout


def mkdir_p(
    ssh_host: str | SSHTarget, remote_path: str, *, timeout: float = 30.0
) -> None:
    """Create a remote directory, including parents."""
    run(ssh_host, ["mkdir", "-p", remote_path], timeout=timeout, check=True)


def exists(
    ssh_host: str | SSHTarget, remote_path: str, *, timeout: float = 30.0
) -> bool:
    """Whether a remote path exists."""
    return run(ssh_host, ["test", "-e", remote_path], timeout=timeout).ok


def pull(
    ssh_host: str | SSHTarget,
    remote_path: str,
    local_path: str,
    *,
    timeout: float = 600.0,
) -> CommandResult:
    """Copy a remote file or directory to the local filesystem via scp.

    SCP follows the target's authentication policy. NERSC ``sshproxy`` targets
    explicitly bypass ControlMaster; MFA targets reuse the existing socket.

    Note: XGC output can reach terabytes. Only pull reduced products
    (AGENTS.md, data principles). Nothing here enforces that -- callers must.
    """
    host, options = connection_options(ssh_host)

    if _normalize_target(ssh_host).is_local:
        argv = ["cp", "-r", remote_path, local_path]
    else:
        argv = ["scp", *options, "-r", f"{host}:{remote_path}", local_path]
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
    ssh_host: str | SSHTarget,
    local_path: str,
    remote_path: str,
    *,
    timeout: float = 600.0,
) -> CommandResult:
    """Copy a local file or directory to the remote host via scp."""
    host, options = connection_options(ssh_host)

    if _normalize_target(ssh_host).is_local:
        argv = ["cp", "-r", local_path, remote_path]
    else:
        argv = ["scp", *options, "-r", local_path, f"{host}:{remote_path}"]
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


def host_for(machine_cfg: dict[str, Any]) -> SSHTarget:
    """Resolve the ssh target for a machine definition.

    Prefers the ~/.ssh/config alias, which supplies either the ControlMaster
    settings or the NERSC IdentityFile/CertificateFile pair.
    """
    from . import machines as m

    auth = machine_cfg.get("auth", {})
    if auth.get("method") == LOCAL_METHOD:
        # The checkout runs on this machine; no alias or host is consulted.
        return SSHTarget(LOCAL_HOST, LOCAL_METHOD, True)
    batch_mode = bool(auth.get("batch_mode", True))
    alias = auth.get("ssh_config_host")
    if alias and not m.is_placeholder(alias):
        return SSHTarget(alias, auth.get("method"), batch_mode)

    user, host = machine_cfg.get("user"), machine_cfg.get("host")
    if not host:
        raise SSHError("machine definition has neither ssh_config_host nor host")
    if m.is_placeholder(user):
        user = None  # fall back to the local username rather than "<...>@host"
    target = f"{user}@{host}" if user else host
    return SSHTarget(target, auth.get("method"), batch_mode)
