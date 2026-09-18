# SSH authentication and multiplexing setup

The XGC automation tools **never perform interactive authentication on your
behalf** (AGENTS.md, absolute rule 3). MFA-protected hosts reuse a session that
the human opens through a ControlMaster socket. Perlmutter is different: after
the human refreshes the NERSC sshproxy certificate, tools use public-key
connections through their own multiplexing socket under `~/.ssh/xgc-cm/`.
They bypass the user's ControlMaster; only the direct authentication probe
disables multiplexing altogether.

The standalone [HPC Queue Monitor](https://github.com/seunghoeku/hpc-queue-monitor)
uses the same authentication modes. Its `servers.yaml` calls the alias field
`ssh_host`; this repository uses `auth.ssh_config_host` in
`machines.local.yaml`. `hpc-monitor ssh-config` prints suggested SSH entries
for review. If an alias is supplied, its SSH configuration takes precedence
over the server's `host` field, including port forwards and proxy settings.
For generic public-key servers the package also accepts `auth: key`: it
uses non-interactive public-key authentication without a ControlMaster or a
mandatory NERSC certificate. The Perlmutter preset remains `auth: sshproxy`.

## 1. Add to `~/.ssh/config`

Alias names are your choice -- the contract is that each name matches
`auth.ssh_config_host` in your `machines.local.yaml` (see
`docs/setup-new-user.md`). Tip for Polaris: aliases pinned to a specific
login node (e.g. `polaris1` -> `polaris-login-01.alcf.anl.gov`) let you move
to another node when one misbehaves, by editing one line in
`machines.local.yaml`.

```sshconfig
# --- shared multiplexing ---
Host stellar stellar.princeton.edu flux polaris1 aurora frontier
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
    ServerAliveInterval 60
    ServerAliveCountMax 3

Host stellar stellar.princeton.edu
    HostName stellar.princeton.edu
    User <princeton_netid>

Host flux
    HostName flux.pppl.gov
    User <pppl_username>

Host polaris1
    HostName polaris-login-01.alcf.anl.gov
    User <alcf_username>

Host aurora
    HostName aurora.alcf.anl.gov
    User <alcf_username>

Host perlmutter
    HostName perlmutter.nersc.gov
    User <nersc_username>
    IdentityFile ~/.ssh/nersc
    CertificateFile ~/.ssh/nersc-cert.pub
    IdentitiesOnly yes
    PreferredAuthentications publickey
    ControlMaster no
    ControlPath none

Host frontier
    HostName frontier.olcf.ornl.gov
    User <olcf_username>
```

The `ControlPath` directory must already exist:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
```

Note: on macOS an overly long `ControlPath` hits the unix socket path length
limit (~104 characters). Keeping it directly under `~/.ssh/` as shown is safe.

## 2. Establish authentication yourself

```bash
ssh stellar        # Duo/password prompt. The master persists 8h even after you exit.
# Type the alias, or list the full hostname in the multiplexing Host line as above.
# `ssh user@stellar.princeton.edu` matches a Host block only if that name is listed;
# otherwise ControlMaster is off for that command and Duo is asked every time.
ssh frontier       # Frontier PASSCODE is entered by the human here
```

For Perlmutter, refresh the key/certificate with your normal NERSC sshproxy
workflow, then check the independent key connection:

```bash
ssh -o ControlMaster=no -o ControlPath=none -o BatchMode=yes perlmutter true
```

Keep `ControlMaster no` for Perlmutter in your own `~/.ssh/config`, and do not
open a Perlmutter master yourself. That keeps your interactive sessions --
tmux, VS Code Remote -- from sharing one socket's channel and lifetime state.

The toolchain multiplexes these connections separately, over a socket it owns
under `~/.ssh/xgc-cm/`, so it gets connection reuse without joining your
session. A direct-key handshake to NERSC costs several seconds and bulk
operations make many calls in a row, so this matters:

```bash
ssh -O exit -o ControlPath=~/.ssh/xgc-cm/s-<hash> perlmutter   # close it early
XGC_SSH_NO_MULTIPLEX=1 xgc-sim refresh                          # or disable it
```

Idle sockets close on their own after 10 minutes.

Every tool-side connection uses a 60 s connect timeout,
`SSH_CONNECT_TIMEOUT_SECONDS` in `tools/ssh.py`. It is deliberately
longer than a generic default: sshproxy handshakes and busy login nodes
routinely take tens of seconds. Do not shorten it.

To start only a background master:

```bash
ssh -MNf stellar
```

## 3. Check the state

```bash
ssh -O check stellar     # "Master running (pid=NNNN)" means the local master exists
ssh -o BatchMode=yes stellar true  # confirms a tool command channel also works
```

For MFA hosts, the `preflight` tool runs both ControlMaster checks. For
Perlmutter, it reads the certificate selected by `ssh -G`, checks its validity
window locally with `ssh-keygen`, and runs a bounded direct-key `true` probe
with multiplexing disabled. It never prompts or authenticates interactively.
How to read its output:

| `control_master` value | Meaning | Action |
|---|---|---|
| `alive: true`, `command_channel_usable: true` | Socket and tool channel work | none |
| `alive: true`, `command_channel_usable: false` | Local master exists but remote channels hang/reject | close the occupied interactive shell, or `ssh -O exit HOST` and reconnect |
| `multiplexing_configured: false` | No ControlMaster entry in `~/.ssh/config` | do step 1 |
| `alive: false` (but configured) | Master is absent or expired | do step 2 |

For Perlmutter, `control_master` is `null` by design and
`direct_key_connection.usable` is the relevant result.

For the standalone package, `hpc-monitor check` validates configuration only;
`hpc-monitor poll --no-notify` tests access and queries queues. Its sshproxy
probe checks the certificate locally, and the queue request tests the remote
connection. An existing-master command-channel probe is bounded at 30 seconds.

For Perlmutter, `ssh_certificate.valid: false` means the NERSC certificate must
be refreshed through the user's `sshproxy` workflow before submission.

Frontier has been observed to allow the original interactive shell while
rejecting an additional multiplexed command channel. In that state, exit the
remote interactive shell once while leaving the master alive, then rerun
preflight. A live `ssh -O check` result alone is not sufficient.

## 4. Close the session

```bash
ssh -O exit stellar
```

## Network prerequisites

- `flux.pppl.gov` is on the internal network (172.16.99.x) — requires PPPL VPN or
  on-site access.
- `stellar.princeton.edu` is reachable externally.
- `polaris` was reached manually in 2026-07; the automation still requires
  the user-created MFA session and reuses its ControlMaster.
- `aurora` (aurora.alcf.anl.gov, round-robin over the login nodes) uses the
  same ALCF MobilePASS+ MFA and the same ControlMaster contract as Polaris.
  Its filesystems are separate from Polaris (`/lus/flare/projects` vs
  `/grand`); nothing is shared between the two ALCF machines.
- Perlmutter access was verified in 2026-08 with the `perlmutter` alias and
  the NERSC key/certificate (original project's account and scratch, now
  bound via `machines.local.yaml`). CPU builds, login-node dryruns, Slurm
  submission, restart, and bounded result analysis all worked. A general
  production XGC executable is intentionally not selected; the verified
  reduced-adiabatic build is campaign-specific.
- Frontier access was verified in 2026-08 with the `frontier` alias, using a
  project-shared conda Python environment (bind yours in
  `machines.local.yaml` if you use frontier for analysis). Frontier remains
  an analysis/access note, not an enabled XGC submission target, until an
  XGC executable and resource profile are reviewed.
