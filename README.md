# xgc-automation

Automation workflow for XGC (gyrokinetic code, xgc.pppl.gov) simulations and
analysis.
**Local brain + remote hands**: the LLM (ohmic.pppl.gov) runs locally
(macOS/Linux), while jobs are submitted, monitored, and analyzed on
Perlmutter/Polaris/Aurora over SSH.

> `AGENTS.md` holds the governing rules and architecture, and is loaded
> automatically into agent context at startup. Detail lives in `docs/` —
> `docs/roadmap.md` for status, `docs/tool-layers.md` for the module map,
> `docs/workflow.md` for a submission end to end.

**Only need job monitoring and a menu bar display?** Install the standalone
[HPC Queue Monitor](https://github.com/seunghoeku/hpc-queue-monitor). It combines
Slurm/PBS polling with SwiftBar and takes a `servers.yaml` file containing
server addresses, scheduler types, usernames, and SSH aliases. Public site
presets are included; no XGC checkout, executable, or LLM account is required.

**Setting up the full XGC workflow?** Start with `docs/setup-new-user.md`: create your
`machines.local.yaml` from the example, set up SSH authentication aliases,
and run `preflight`. MFA hosts use ControlMaster, while Perlmutter uses its
NERSC sshproxy key/certificate directly. Committed config holds only
machine-universal facts; everything personal (usernames, accounts, paths,
builds) lives in gitignored overlays.

## Directory layout

| Path | Purpose |
|---|---|
| `tools/` | MCP tool layer (`preflight`, `hpc_runner`, ...) |
| `packaging/hpc-monitor/` | Original standalone monitor packaging snapshot; ongoing package development is in [hpc-queue-monitor](https://github.com/seunghoeku/hpc-queue-monitor) |
| `machines.yaml` | HPC machine facts (committed, machine-universal) |
| `machines.local.yaml` | Your per-user bindings (gitignored; copy the `.example`) |
| `templates/` | Jinja2 templates for job scripts and XGC input |
| `docs/` | Setup and per-machine build notes |
| `kb/` | Knowledge base for physics_advisor |
| `input_backup/` | Read-only historical input archive; never treated as validated |
| `runs/` | Run records and pulled summary products |
| `f_init/` | Optional non-Maxwellian `f_init` procedure (`xgc-finit`, skill `f-init`); not loaded in the everyday flow |

## Implemented workflow

`physics_advisor` matches a natural-language physics request to facts and
exclusions recorded in `kb/cases/`. It never invents numerical values. The
knowledge base currently holds five validated baselines (`ecbc` core ES
delta-f, `st40-heatload` edge heat-load, `asdex-imode` edge I-mode,
`kstar-rmp` RMP turbulence, `tcv-xgca-sheath` XGCa sheath); requests outside
their recorded physics (e.g. electromagnetic turbulence, ELM crashes, other
devices) are reported as unsupported rather than forced onto the closest
case.

The returned case and explicit overrides flow through `plan_input` for XGC
metadata validation and a human-readable diff before anything can be written.

New simulations receive a global sequential `simulation_id` such as `n645au`
or `n647pe_po`. The ID prefixes the remote directory, while full machine names
remain metadata rather than being repeated in the directory suffix. ID
reservation and remote case staging both require explicit human confirmation.

`data_analyzer` keeps raw ADIOS2 output on the HPC machine. Its inventory
product scans only top-level names, while `oned_profiles` uses XGC-Analysis
logical-step selection remotely and saves bounded JSON plus a local SVG for
the first/latest 1-D profiles.

`xgc-remote-gui` opens XGC-Devel's existing Monitor against a remote simulation
directory. It resolves recorded runs, validates the remote GUI environment,
starts a loopback-only login-node server and SSH tunnel, and opens the local
browser without submitting a scheduler job or transferring BP output.

`xgc-queue-watch` polls user queues, including jobs submitted outside the
catalog, and records recent observed departures. Its SwiftBar plugin reads the
local cache without SSH. This checkout-based workflow remains available;
the standalone `hpc-monitor` package has its own configuration and installer.

## Docs

- `docs/ssh-setup.md` — ssh multiplexing setup for MFA-protected machines
- `docs/build-stellar.md` — building XGC on stellar (note: `-O2` crashes the compiler)
- `docs/build-polaris.md` — verified Polaris environment and CMake configure
  plus the locally tested PBS resource profile
- `docs/data-analysis.md` — remote reduction boundary and summary products
- `docs/remote-gui.md` — launch the remote XGC_GUI Monitor through an SSH tunnel
- [Queue monitoring](docs/queue-watch.md) — checkout and standalone package setup,
  SwiftBar display, recent history, and connection handling
- `docs/simulation-ids.md` — sequential IDs, machine codes, and directories
- `docs/input-archive.md` — recent-first archive inspection and migration policy
