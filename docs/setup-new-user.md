# Setting up xgc-automation as a new user

This repository was built around one researcher's accounts and data. The
committed configuration holds only machine-universal facts; everything tied
to a person lives in gitignored overlay files that you create once. This
page is the complete checklist.

## 1. What you need before starting

| Requirement | Why |
|---|---|
| Accounts on the HPC machines you will use (NERSC / ALCF / Princeton...) | The LLM never authenticates for you (AGENTS.md rule 3) |
| An allocation/account name per machine (e.g. an ALCF project) | Goes into job scripts; submission is blocked without it |
| `uv` and Python 3.12 locally | `uv sync` installs the toolchain |
| An ohmic API key (`OHMIC_API_KEY` in your environment) | Local LLM endpoint; `preflight` checks reachability |
| An XGC build on each machine, or willingness to build one | See `docs/build-<machine>.md`; `xgc --dryrun` is the input validator |

## 2. SSH aliases and authentication (the connection contract)

For MFA-protected machines, remote work reuses an SSH session that **you**
open. Add a `Host` alias to `~/.ssh/config` with ControlMaster multiplexing
(see `docs/ssh-setup.md` for full examples):

```
Host mypolaris
  HostName polaris.alcf.anl.gov
  User <your_alcf_username>
  ControlMaster auto
  ControlPersist 8h
  ControlPath ~/.ssh/cm-%r@%h:%p
```

The alias name is yours to choose -- the only rule is that
`auth.ssh_config_host` in your `machines.local.yaml` must match it exactly.
Authenticate once yourself (`ssh mypolaris`); the tools then reuse the
socket and fail fast with instructions when it has expired.

Perlmutter uses a different contract. Its alias selects the NERSC sshproxy
key and certificate and disables multiplexing:

```
Host perlmutter
  HostName perlmutter.nersc.gov
  User <your_nersc_username>
  IdentityFile ~/.ssh/nersc
  CertificateFile ~/.ssh/nersc-cert.pub
  IdentitiesOnly yes
  PreferredAuthentications publickey
  ControlMaster no
  ControlPath none
```

Refresh the sshproxy certificate yourself when needed. The tools validate it
and use independent BatchMode public-key connections; they never prompt.

## 3. machines.local.yaml (your bindings)

```bash
cp machines.local.yaml.example machines.local.yaml   # gitignored
```

Fill in, per machine you use: `user`, `auth.ssh_config_host`,
`paths.scratch`/`run_root`, `defaults.account` (and `mail_user` on
perlmutter), and your `xgc.exe`/`src`/`env_script` once you have a build.
Anything you leave out stays a `<placeholder>` from the committed
`machines.yaml`, and:

```
preflight            # MCP tool; reports per-machine what is missing
```

tells you exactly what still blocks submission. Machine-universal values
(schedulers, queues, shared site installs like stellar's `env_script` and
`remote_python`) are already set and normally need no override.

## 4. Data you will NOT have (and what still works)

Two directories are the original researcher's personal science record and
are not part of the toolchain:

- `Other_data/simulation_note_XGC1.xlsx` -- 18 years of per-run intent and
  outcome notes. Only used by the *case promotion* tools
  (`rank_promotion_candidates`, `plan_case_promotion`); without it they
  explain themselves and stop.
- `input_backup/` -- ~700 historical XGC inputs. Same: promotion evidence
  only.

Everything else works without them: `kb/cases/` baselines, the physics
advisor, input building, dryrun validation, submission, and analysis. If
you have your own run history, the promotion pipeline works on any files
following the `n<number><machine-code>_<description>` naming plus a
notebook with `shot #` / `note` columns.

## 5. Using the validated baselines (kb/cases/)

Each case YAML records where its author validated it (`case_dir`,
`input_file_dir`). Those are the author's directories; your account may not
read them. When a tool hits that, it prints your options, which are:

1. Ask the owner for read access, or for a copy in shared project space.
2. Reconstruct the case yourself: the exact archived input is referenced
   under `provenance.archive_file`, and the case's `migration:` section
   lists every change that made it pass the current `xgc --dryrun`. Stage
   it in your own space and run `validate_input`.
3. Point the case at your restaged copy with a gitignored override:

   ```yaml
   # kb/cases/st40-heatload.local.yaml
   case_dir: /grand/YourProject/you/st40-restage
   input_file_dir: /grand/YourProject/you/XGC_inputs
   ```

   Only site bindings (`case_dir`, `input_file_dir`, `machine`,
   `backup_dir`) can be overridden this way; physics changes require a new
   validated case.

Mesh/equilibrium/profile files referenced by the cases (`input_files:` in
each YAML) must exist on the target machine. They are typically multi-MB
and live in a shared `XGC_inputs/` directory; get them from the original
machine's project space or from the case author. **Never route them through
your laptop** -- copy remotely (`stage_case` does this).

## 6. First-run smoke sequence

```
preflight                          # everything green for your machine?
list_cases                         # baselines visible
validate_input <machine> <dir>     # after staging a case: dryrun gate
plan_job ... hello_world=true      # render a script + cost, submit nothing
```

Then follow `docs/workflow.md` for the full plan -> approve -> submit loop.
Polaris note: the CUDA build cannot dryrun on login nodes (GPU/NVML
limitation) -- use a 1-node debug-queue job (`docs/build-polaris.md`).

## 7. Known per-user gotchas inherited from the original setup

- `kb/simulation_registry.yaml` is a single global sequence shared by
  design. If you fork this repo for a separate research effort, your
  numbering starts fresh; do not reuse numbers if you share data with the
  original line of work.
- Some case warnings reference the original author's pending decisions
  (e.g. marker-type choice in `asdex-imode`); read the `notes:` section of
  a case before production use.
- `runs/` and `kb/cases/drafts/` contain the original project's provenance
  history; they are safe to keep and useful as examples.
