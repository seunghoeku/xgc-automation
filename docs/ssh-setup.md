# SSH multiplexing setup (for MFA-protected machines)

The XGC automation tools **never authenticate on your behalf** (AGENTS.md,
absolute rule 3). To avoid re-authenticating on every command against an
MFA-protected machine, a human opens one session and the tools reuse it through a
ControlMaster socket.

## 1. Add to `~/.ssh/config`

Alias names are your choice -- the contract is that each name matches
`auth.ssh_config_host` in your `machines.local.yaml` (see
`docs/setup-new-user.md`). Tip for Polaris: aliases pinned to a specific
login node (e.g. `polaris1` -> `polaris-login-01.alcf.anl.gov`) let you move
to another node when one misbehaves, by editing one line in
`machines.local.yaml`.

```sshconfig
# --- shared multiplexing ---
Host stellar flux perlmutter polaris1 frontier
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
    ServerAliveInterval 60
    ServerAliveCountMax 3

Host stellar
    HostName stellar.princeton.edu
    User <princeton_netid>

Host flux
    HostName flux.pppl.gov
    User <pppl_username>

Host polaris1
    HostName polaris-login-01.alcf.anl.gov
    User <alcf_username>

Host perlmutter
    HostName perlmutter.nersc.gov
    User <nersc_username>
    IdentityFile ~/.ssh/nersc
    CertificateFile ~/.ssh/nersc-cert.pub
    IdentitiesOnly yes

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

## 2. Open the session yourself (MFA happens once, here)

```bash
ssh stellar        # Duo/password prompt. The master persists 8h even after you exit.
ssh perlmutter     # refresh the NERSC sshproxy certificate first when needed
ssh frontier       # Frontier PASSCODE is entered by the human here
```

To start only a background master:

```bash
ssh -MNf stellar
```

## 3. Check the state

```bash
ssh -O check stellar     # "Master running (pid=NNNN)" means the local master exists
ssh -o BatchMode=yes stellar true  # confirms a tool command channel also works
```

The `preflight` tool runs both checks. For `sshproxy` hosts it also reads the
certificate selected by `ssh -G` and checks its validity window locally with
`ssh-keygen`; it never prompts or authenticates. How to read its output:

| `control_master` value | Meaning | Action |
|---|---|---|
| `alive: true`, `command_channel_usable: true` | Socket and tool channel work | none |
| `alive: true`, `command_channel_usable: false` | Local master exists but remote channels hang/reject | close the occupied interactive shell, or `ssh -O exit HOST` and reconnect |
| `multiplexing_configured: false` | No ControlMaster entry in `~/.ssh/config` | do step 1 |
| `alive: false` (but configured) | Master is absent or expired | do step 2 |

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
- Perlmutter access was verified in 2026-08 with the `perlmutter` alias,
  NERSC key/certificate, account `m499`, and `/pscratch/sd/s/sku`. CPU builds,
  login-node dryruns, Slurm submission, restart, and bounded result analysis
  all worked. A general production XGC executable is intentionally not selected
  in `machines.yaml`; the verified reduced-adiabatic build is campaign-specific.
- Frontier access was verified in 2026-08 with the `frontier` alias. The shared
  Python 3.14.6 environment is
  `/lustre/orion/proj-shared/fus183/shku/conda_env/myenv`. Frontier remains an
  analysis/access note, not an enabled XGC submission target, until an XGC
  executable and resource profile are reviewed.
