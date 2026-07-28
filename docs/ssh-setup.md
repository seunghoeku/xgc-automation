# SSH multiplexing setup (for MFA-protected machines)

The XGC automation tools **never authenticate on your behalf** (AGENTS.md,
absolute rule 3). To avoid re-authenticating on every command against an
MFA-protected machine, a human opens one session and the tools reuse it through a
ControlMaster socket.

## 1. Add to `~/.ssh/config`

```sshconfig
# --- shared multiplexing ---
Host stellar flux perlmutter polaris
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
```

To start only a background master:

```bash
ssh -MNf stellar
```

## 3. Check the state

```bash
ssh -O check stellar     # "Master running (pid=NNNN)" means healthy
```

The `preflight` tool runs exactly this command. How to read its output:

| `control_master` value | Meaning | Action |
|---|---|---|
| `alive: true` | Socket is live, no re-auth needed | none |
| `multiplexing_configured: false` | No ControlMaster entry in `~/.ssh/config` | do step 1 |
| `alive: false` (but configured) | Master is absent or expired | do step 2 |

## 4. Close the session

```bash
ssh -O exit stellar
```

## Network prerequisites

- `flux.pppl.gov` is on the internal network (172.16.99.x) — requires PPPL VPN or
  on-site access.
- `stellar.princeton.edu` is reachable externally.
- `perlmutter` and `polaris` are currently inaccessible (as of 2026-07).
