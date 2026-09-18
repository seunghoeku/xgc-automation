# HPC Queue Monitor

**The distributable package now has its own repository:
[seunghoeku/hpc-queue-monitor](https://github.com/seunghoeku/hpc-queue-monitor).**
Use that repository for current installation instructions, development, and
releases. This directory retains the original packaging/export snapshot in
XGC Automation; running its exporter creates a snapshot of this checkout,
not a synchronization with the independent repository.

A small Slurm/PBS queue monitor for macOS, with SwiftBar as its menu UI.
The installer connects a background polling timer to a cache-only menu plugin.
It monitors all of your jobs, including jobs submitted outside XGC Automation.
No XGC installation, repository checkout, simulation catalog, or LLM is required.

## Install on a Mac

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
   [SwiftBar](https://github.com/swiftbar/SwiftBar/releases/tag/v2.1.2-beta-3).
   Use SwiftBar 2.1.2 beta 3 (or a later version containing the submenu refresh
   fix). SwiftBar 2.1.0/2.1.1 may disable changing server submenus.
2. Install the supplied wheel. This package is not yet published to PyPI:

   ```sh
   uv tool install --python 3.12 ./hpc_queue_monitor-0.1.0-py3-none-any.whl
   uv tool update-shell
   ```

   If `hpc-monitor` is not found, reopen Terminal after updating the shell.
   `uv` supplies an isolated Python runtime and the package's PyYAML dependency.
3. Create your configuration:

   ```sh
   hpc-monitor init
   open -e "$HOME/Library/Application Support/HPC Queue Monitor/servers.yaml"
   ```

   Public hostnames, schedulers, and authentication modes are prefilled for
   Perlmutter, Polaris, Aurora, and Stellar. Flux is also listed, but its
   scheduler must be confirmed before enabling it. Set `enabled: true` for
   the servers you use and replace the username placeholders. Set `ssh_host`
   to an existing alias in your `~/.ssh/config`.
4. Verify and install:

   ```sh
   hpc-monitor check
   hpc-monitor poll --no-notify
   hpc-monitor install
   ```

   An inaccessible server is reported separately; other servers continue to
   work. If desired, preview installation with `hpc-monitor install --dry-run`.
5. Open SwiftBar and select the Plugin Folder printed by the installer, if
   it is not already selected. Enable SwiftBar's **Launch at Login** setting.

The installer uses SwiftBar's existing Plugin Folder when configured; otherwise
it creates a `plugins` folder in the monitor's application-support directory.
It writes only `hpc-monitor.30s.sh` there. The timer polls immediately, every
configured interval, and at login. No administrator password is needed.

## Server configuration

An enabled server can be as small as:

```yaml
version: 1
poll_interval: 600
history_limit: 5
notify: true
servers:
  my-cluster:
    host: login.example.edu
    ssh_host: my-cluster
    user: alice
    scheduler: slurm
    auth: controlmaster
```

- `host`: server address. The shipped example includes the known server addresses.
- `ssh_host`: optional OpenSSH alias; when supplied, it takes precedence over
  `host`, and SSH reads the address, port, proxy, and keys from `~/.ssh/config`.
  This supports local port forwards and site-specific login paths. Without an
  alias the target is `user@host`.
- `user`: scheduler account to monitor; it may differ from the SSH login user
  if the alias configures a different `User`.
- `scheduler`: `slurm` or `pbs`. PBS requires PBS Pro/OpenPBS JSON `qstat`
  support; other PBS/Torque variants have not been validated.
- `auth`: `controlmaster`, `key`, or `sshproxy` (see below).
- `enabled`: optional, defaults to `true`. Shipped presets start disabled.
- `poll_interval`: seconds, 60 to 86400, default 600. Rerun `install` after
  changing it so launchd uses the new interval. Staleness is twice this interval.
- `history_limit`: 1 to 100 observed departed jobs per server, default 5.
- `notify`: show a desktop notification when jobs change, default `true`.

New servers can be added to this file without editing Python or shell scripts.
Unknown fields, duplicate names, and unfilled enabled-server placeholders are
reported as configuration errors. Changes are read at the next poll/menu refresh.
Changing a server's account or connection definition starts a separate history.

Use another file with `hpc-monitor --config /path/servers.yaml COMMAND`.
One timer/plugin installation is supported per macOS user; that installation
can monitor many servers. The configuration path is recorded at installation.

## SSH authentication

The monitor uses the system OpenSSH client. Passwords and MFA are handled by
the user in Terminal; the monitor never prompts for or stores them. Scheduler
commands must be available in the remote non-interactive SSH environment.

`hpc-monitor ssh-config` prints suggested entries for enabled servers. Review
and add them to `~/.ssh/config`; it does not change that file for you.

- **controlmaster:** for MFA hosts. Configure an SSH alias with `ControlMaster
  auto`, `ControlPath ~/.ssh/cm-%C`, and `ControlPersist 8h`. Open a session
  yourself (for example `ssh -MNf my-cluster`) and complete authentication.
  The monitor checks the master and uses non-interactive channels. Expired or
  stale sessions appear as connection warnings. Some sites need their own
  tunnel/proxy procedure; use that procedure to establish the session first.
- **key:** ordinary public-key login, with the key set in SSH config and any
  passphrase already unlocked through your SSH agent. No ControlMaster is
  required. Password and interactive authentication are disabled.
- **sshproxy:** NERSC certificates. Generate/refresh credentials with your
  normal site workflow; set `IdentityFile` and `CertificateFile` in SSH config.
  The transport validates the certificate and uses its own multiplexing socket
  under `~/.ssh/xgc-cm`, separate from your interactive session's socket.

Connect timeout is 60 seconds; an existing master has a 30-second channel probe.
Remote commands time out after 120 seconds. A slow server may delay later
servers in a poll; menu opening always reads local cached data immediately.

## What the menu shows

Every 30 seconds SwiftBar reads the local cache. It shows job name, ID, nodes,
requested time, and scheduler-provided estimated start times for pending jobs.
The server state icon follows Running, Pending, Completed, Failed priority;
connection/staleness warnings are independent. Failed connections retain the
last successful queue rather than appearing as an empty queue.

Recent history includes only jobs observed by this installation after monitoring
started. Jobs that finished before monitoring, or entirely between polls, are
not discovered. Completion means scheduler completion, not completion of a
scientific workflow. Accounting delays remain unconfirmed until resolved.
Refresh cached view rereads the cache; use `hpc-monitor poll` for a remote check.

## Manage or remove

```sh
hpc-monitor status
hpc-monitor stop      # stop polling until start or next login
hpc-monitor start
hpc-monitor install  # update paths/timer after upgrading or changing interval
hpc-monitor uninstall        # preview the files to remove
hpc-monitor uninstall --yes  # stop timer, remove owned plugin and plist
uv tool uninstall hpc-queue-monitor
```

Configuration, cache, and logs are retained on uninstall. The installer refuses
to overwrite an unrelated file at its plugin/plist paths. To change the config
file or plugin directory, uninstall first, then install with the new options.
Use `install --plugin-dir /path/to/plugins` to select a specific existing folder.

Configuration/cache: `~/Library/Application Support/HPC Queue Monitor/`.
Timer: `~/Library/LaunchAgents/org.hpc-queue-monitor.poll.plist`.
Log: `~/Library/Logs/hpc-queue-monitor.log`, rotated weekly with one archive.

The CLI also supports Linux: run `poll` from your own cron/systemd timer.
SwiftBar and the bundled timer installer are macOS-only.

## Build and test this original snapshot (maintainers)

For this checkout's export, the scheduler, SSH, history, and menu modules come
from XGC Automation's `tools/`. The exporter copies an explicit five-module
allowlist into this package. The independent repository builds directly from
its own `src/hpc_monitor/core/`; changes do not sync automatically. This exporter never exports
machine overlays, accounts, catalog records, credentials, caches, or runs.
The generated `SOURCE.json` records module hashes and the repository revision.

From the XGC Automation repository:

```sh
python -m tools.monitor_dist --output .cache/hpc-monitor-source
uv build .cache/hpc-monitor-source --out-dir .cache/hpc-monitor-dist
```

Choose a new export directory for each build; the exporter refuses to overwrite
an existing one. Build from the exported tree, not from `packaging/hpc-monitor`
directly. The resulting `.whl` and `.tar.gz` are standalone distributions.
The source archive can rebuild the wheel without the original repository.

Install the wheel into an isolated environment, then run the shipped tests:

```sh
uv venv /tmp/hpc-monitor-test
uv pip install --python /tmp/hpc-monitor-test/bin/python .cache/hpc-monitor-dist/hpc_queue_monitor-0.1.0-py3-none-any.whl
/tmp/hpc-monitor-test/bin/python -m unittest discover -s .cache/hpc-monitor-source/tests
```

Tests use fixture queues and temporary paths. They do not contact HPC systems
or install a live launchd service. A release smoke test on a second Mac should
also cover real SSH authentication and the SwiftBar menu. SwiftBar is installed
separately from its official release; it is not bundled in this distribution.
