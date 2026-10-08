# Setting up xgc-automation as a new user

This repository was built around one researcher's accounts and data. The
committed configuration holds only machine-universal facts; everything tied
to a person lives in gitignored overlay files that you create once. This
page is the checklist for the full XGC workflow.

For queue monitoring alone, use the separate
[HPC Queue Monitor](https://github.com/seunghoeku/hpc-queue-monitor) package.
Its `hpc-monitor init` command creates `servers.yaml` with public hostnames,
scheduler types, and authentication modes for Perlmutter, Polaris, Aurora,
and Stellar. Enable your servers, fill in your usernames and SSH aliases,
then run `hpc-monitor check` and `hpc-monitor install`. Flux is listed but its
scheduler must be confirmed before enabling it. This path requires SwiftBar,
uv/Python, and SSH access, but no allocation for submission, XGC executable,
ohmic key, or `machines.local.yaml`. See [queue-watch.md](queue-watch.md) for
the two installation modes and their separate cache locations.

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
key and certificate and disables user-session multiplexing:

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
and use BatchMode public-key connections through a tool-owned multiplexing
socket under `~/.ssh/xgc-cm/`, separate from your interactive sessions. The
preflight authentication probe disables multiplexing to test the certificate
itself. Neither mode prompts.

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

To use the remote XGC_GUI Monitor, also set `gui.source` to an XGC-Devel
checkout containing `utils/input_gui/app.py` and `gui.python` to an interpreter
that imports Flask, NumPy, Matplotlib, and ADIOS2. `gui.env_script` is optional.
Run `uv run xgc-remote-gui plan ...` before opening a session; see
`docs/remote-gui.md`.

### Where your state lives (`XGC_CATALOG_DIR`)

The repository is two things: shared code and documentation, and one
researcher's state that the tools write (the catalog `kb/simulations/`, the
registry, `runs/`, `input_backup/`, f_init and flow_match case logs, caches).
By default the state lives inside the checkout. To keep it elsewhere, for
example in a catalog directory shared between a laptop checkout and a cluster
checkout, set one thing:

```bash
export XGC_CATALOG_DIR=~/xgc-catalog            # or, in
# ~/.config/xgc-automation/config.yaml
catalog_dir: ~/xgc-catalog
data_dir: ~/xgc-data        # PNG/NPZ/logs, same layout, outside git (default <catalog>/data)
catalog_sync: auto          # git pull/commit/push the catalog around every write (auto|true|false)
```

Every state path follows; `machines.local.yaml` is read from the checkout if
it is there, else from the state directory. `xgc-sim paths` prints the
resolved layout and marks what does not exist yet. `machines.yaml`,
`templates/` and the baseline cases in `kb/cases/` always come from the
checkout (`tools/paths.py`).

The recommended layout is a small git repository of your own for that
directory (a "catalog repository"): `git init ~/xgc-catalog`, copy
`templates/USER.md.example` there as `USER.md`, fill it in, and
`ln -s ~/xgc-catalog/USER.md ~/.config/xgc-automation/USER.md` so the agent
instructions pick it up. The state paths are gitignored in this checkout, so
with the default layout nothing of yours can land in the shared repository
either. See `docs/simulation-catalog.md`, "Catalog repository".

If the checkout runs on the cluster itself (a clone on a login node), bind
that machine with `auth.method: local` instead of an ssh alias; see
`docs/ssh-setup.md`, "Running the automation on the machine itself".

## 4. Data you will NOT have (and what still works)

Two directories are the original researcher's personal science record and
are not part of the toolchain:

- `Other_data/simulation_note_XGC1.xlsx` -- 18 years of per-run intent and
  outcome notes. Used by the *case promotion* tools
  (`rank_promotion_candidates`, `plan_case_promotion`) and by
  `xgc-sim import-notebook`; without it they explain themselves and stop.
- `input_backup/` -- ~700 historical XGC inputs. Same: promotion evidence
  only.

Everything else works without them: `kb/cases/` baselines, the physics
advisor, input building, dryrun validation, submission, and analysis. If
you have your own run history, the promotion pipeline works on any files
following the `n<number><machine-code>_<description>` naming plus a
notebook with `shot #` / `note` columns. The same applies to
`xgc-sim import-notebook`; point it at your own file with `--notebook` or
`XGC_SIMULATION_NOTEBOOK`.

The simulation catalog (`kb/simulations/`) starts empty and fills itself as
you reserve numbers and submit jobs, so it needs none of the above. See
`docs/simulation-catalog.md`, including the optional Google Sheets mirror.

### 4a. Your own catalog repository (required)

Every user needs a **catalog repository of their own**: the directory the
tools write to (records, registry, provenance, input backups, case logs,
`USER.md`), kept in git so a laptop and a cluster checkout stay in step
(`docs/simulation-catalog.md`, "Catalog repository"). The original author's
catalog, [xgc-catalog](https://github.com/seunghoeku/xgc-catalog), is **not**
shared state you join: it holds one person's simulation numbers and notes.
Two ways to set yours up; the first is recommended.

Both options use the **same layout**, one directory per user named after
the GitHub id, `users/<id>/`, with `catalog_dir` pointing at that directory
rather than at the repository root. A catalog started as option A can then
join a group repository later by copying its `users/<id>/` directory, with
no renumbering and nothing to move.

The layout is **defined by xgc-automation** (`tools/catalog_init.py`), not
assembled by hand: `xgc-sim init-catalog` creates it, and `xgc-sim paths`
reports `layout: canonical (users/<id>)` or says what is off. Use the
command rather than copying directories so that your catalog and everyone
else's stay identical down to the `.gitignore` and the seeded registry
(same machine codes, counters at 1).

**Option A, a separate repository (recommended).**

```bash
uv run xgc-sim init-catalog <your github id> \
    --repo ~/git/xgc-catalog --data-dir ~/xgc-data \
    --remote git@github.com:<you>/xgc-catalog.git      # a private repository you created
```

This creates `~/git/xgc-catalog` as a git repository with `.gitignore`,
`README.md` and `users/<id>/` (all state subdirectories, a registry starting
at `n001` / `d0001`, `USER.md` from the template), commits it, adds the
remote, writes `~/.config/xgc-automation/config.yaml` (`catalog_dir`,
`data_dir`, `catalog_sync`) and links `~/.config/xgc-automation/USER.md`.
It is idempotent and never overwrites a file that exists. Then:

```bash
$EDITOR ~/git/xgc-catalog/users/<id>/USER.md
git -C ~/git/xgc-catalog push -u origin main
uv run xgc-sim paths                 # layout: canonical (users/<id>)
```

Your numbering starts at `n001<code>` / `d0001<code>` in your own registry;
numbers are sequential **within one catalog** and two users' catalogs may
both contain an `n001pe`. That is intended: catalogs are personal, and
nothing merges them. The tools pull before every write and push after it,
so set the remote before the first reservation.

**Option B, a directory per user inside one shared catalog repository.**
If a group prefers one repository, each user takes `users/<id>/` in it and
points `catalog_dir` there:

```yaml
catalog_dir: ~/git/xgc-catalog/users/<id>     # own registry, own numbering
```

Every state path (registry, records, `runs/`, `input_backup/`) lives under
that directory, so each user has an independent sequence and only ever
writes their own files; `git pull --rebase` (which the tools run around
every write) merges the rest. The whole repository is cloned by everyone,
so products must stay out of it (the `.gitignore` above) or it grows with
the group. The original author's repository already has this layout
(`users/seunghoeku/`); to work inside it, contact Seung-Hoe Ku
(sku@pppl.gov) for write access and a directory.

**Using the original catalog as reference data.** Clone it read-only and
search it without making it your state:

```bash
git clone https://github.com/seunghoeku/xgc-catalog.git ~/git/xgc-catalog-seunghoeku
xgc-sim --catalog-dir ~/git/xgc-catalog-seunghoeku/users/seunghoeku/kb/simulations search "TCV negative triangularity"
xgc-sim --catalog-dir ~/git/xgc-catalog-seunghoeku/users/seunghoeku/kb/simulations show n675pe
```

Its records, notes, `input_backup/` and `runs/*/provenance.json` tell you
which inputs and builds produced which results; its `kb/shots/` index says
what experimental files were used. The validated baselines promoted from it
are already in this repository's `kb/cases/` and need no catalog at all. Do
not point `catalog_dir` at the clone: your reservations would then continue
someone else's numbering.

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
- The Google Sheets mirror needs a service account key that is personal to
  you. Keep it outside the repository and reference it from
  `common.sheet_sync` in `machines.local.yaml`; never commit it.
