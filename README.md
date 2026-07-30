# xgc-automation

Automation workflow for XGC (gyrokinetic code, xgc.pppl.gov) simulations and
analysis.
**Local brain + remote hands**: the LLM (ohmic.pppl.gov) runs locally
(macOS/Linux), while jobs are submitted, monitored, and analyzed on
Perlmutter/Polaris over SSH.

> See `AGENTS.md` for the detailed design and rules. opencode loads that file
> automatically at startup.

## Directory layout

| Path | Purpose |
|---|---|
| `tools/` | MCP tool layer (`preflight`, `hpc_runner`, ...) |
| `machines.yaml` | HPC machine definitions (single source of truth) |
| `templates/` | Jinja2 templates for job scripts and XGC input |
| `docs/` | Setup and per-machine build notes |
| `kb/` | Knowledge base for physics_advisor |
| `input_backup/` | Read-only historical input archive; never treated as validated |
| `runs/` | Run records and pulled summary products |

## Implemented workflow

`physics_advisor` matches a natural-language physics request to facts and
exclusions recorded in `kb/cases/`. It never invents numerical values. With
the current single-case knowledge base, it recommends ECBC only for a
compatible electrostatic core request and reports electromagnetic, edge-SOL,
ELM, or collisional requests as unsupported.

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

## Docs

- `docs/ssh-setup.md` — ssh multiplexing setup for MFA-protected machines
- `docs/build-stellar.md` — building XGC on stellar (note: `-O2` crashes the compiler)
- `docs/build-polaris.md` — verified Polaris environment and CMake configure
- `docs/data-analysis.md` — remote reduction boundary and summary products
- `docs/simulation-ids.md` — sequential IDs, machine codes, and directories
- `docs/input-archive.md` — recent-first archive inspection and migration policy
