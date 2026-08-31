# xgc-automation

Automation workflow for XGC (gyrokinetic code, xgc.pppl.gov) simulations and
analysis.
**Local brain + remote hands**: the LLM (ohmic.pppl.gov) runs locally
(macOS/Linux), while jobs are submitted, monitored, and analyzed on
Perlmutter/Polaris over SSH.

> `AGENTS.md` holds the governing rules and architecture, and is loaded
> automatically into agent context at startup. Detail lives in `docs/` —
> `docs/roadmap.md` for status, `docs/tool-layers.md` for the module map,
> `docs/workflow.md` for a submission end to end.

**New user?** Start with `docs/setup-new-user.md`: create your
`machines.local.yaml` from the example, set up SSH authentication aliases,
and run `preflight`. MFA hosts use ControlMaster, while Perlmutter uses its
NERSC sshproxy key/certificate directly. Committed config holds only
machine-universal facts; everything personal (usernames, accounts, paths,
builds) lives in gitignored overlays.

## Directory layout

| Path | Purpose |
|---|---|
| `tools/` | MCP tool layer (`preflight`, `hpc_runner`, ...) |
| `machines.yaml` | HPC machine facts (committed, machine-universal) |
| `machines.local.yaml` | Your per-user bindings (gitignored; copy the `.example`) |
| `templates/` | Jinja2 templates for job scripts and XGC input |
| `docs/` | Setup and per-machine build notes |
| `kb/` | Knowledge base for physics_advisor |
| `input_backup/` | Read-only historical input archive; never treated as validated |
| `runs/` | Run records and pulled summary products |

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

## Docs

- `docs/ssh-setup.md` — ssh multiplexing setup for MFA-protected machines
- `docs/build-stellar.md` — building XGC on stellar (note: `-O2` crashes the compiler)
- `docs/build-polaris.md` — verified Polaris environment and CMake configure
  plus the locally tested PBS resource profile
- `docs/data-analysis.md` — remote reduction boundary and summary products
- `docs/remote-gui.md` — launch the remote XGC_GUI Monitor through an SSH tunnel
- `docs/simulation-ids.md` — sequential IDs, machine codes, and directories
- `docs/input-archive.md` — recent-first archive inspection and migration policy
