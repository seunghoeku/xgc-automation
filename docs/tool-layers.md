# Tool layers

The map from a job in the workflow to the module that owns it. `AGENTS.md`
absolute rule 2 says deterministic actions go through these tools rather than
improvised shell commands; this file is the list of what exists, so that rule
is actionable.

All MCP tools are exposed from `tools/server.py`. Two CLIs are installed as
entry points: `xgc-sim` (`tools/simcat_cli.py`), `xgc-remote-gui`
(`tools/remote_gui.py`), and `xgc-queue-watch` (`tools/queue_watch.py`).

## Layers

| Layer | Module | What it does |
|---|---|---|
| `preflight` | `tools/server.py`, `tools/machines.py` | Is ohmic reachable? Is the Perlmutter key valid for 24h? Is the Polaris tunnel alive? Reports unfilled `<placeholder>` fields per machine. |
| `physics_advisor` | `tools/physics_advisor.py` | Problem description → parameter proposals, from validated `kb/cases/` facts and deterministic rules. Never invents numbers; reports unsupported physics as unsupported. |
| `input_builder` | `tools/input_builder.py`, `tools/namelist.py` | Builds the XGC namelist (`input`), geqdsk references, and profiles via Jinja2 from a KB baseline, reusing XGC's own `utils/input_gui/` parser and metadata. |
| `validator` | `tools/dryrun.py` | `xgc --dryrun` on the login node as a pre-submit gate (dt vs gyrofrequency, grid vs rho_i, particles/cell, wall-clock estimate). |
| `hpc_runner` | `tools/hpc_runner.py`, `tools/scheduler.py` | `submit/status/cancel/output`, slurm vs pbs behind one interface. `push/pull` live in `tools/ssh.py` and are not yet exposed as MCP tools. |
| `data_analyzer` | `tools/data_analyzer.py` | Bounded remote reduction (`inventory`, `oned_profiles`) → small JSON/SVG pulled locally. Never transfers `.bp`. |
| `provenance` | `tools/provenance.py` | input→output lineage under `runs/` (commit, hash, timestamp). |
| `remote_gui` | `tools/remote_gui.py` | Resolves a remote simulation, starts XGC-Devel's existing Monitor on the login node, tunnels it to localhost, and manages its lifecycle. |
| `simulation_catalog` | `tools/simulation_catalog.py`, `tools/simcat_cli.py`, `tools/refresh.py` | One durable record per simulation (`kb/simulations/<id>.yaml`) linking number, runs, status, archive location, and human note. Searchable without an LLM via `xgc-sim`. |
| `queue_watch` | `tools/queue_watch.py` | Asks a machine what the user currently has queued, diffs against the last snapshot, and reports only changes. Read-only, built for a local timer rather than a chat session. See `docs/queue-watch.md`. |

## Supporting modules

| Module | Role |
|---|---|
| `tools/ssh.py` | Every remote call. argv-list subprocess, ControlMaster for MFA hosts, sshproxy key + tool-owned `~/.ssh/xgc-cm/` socket for Perlmutter. Holds `SSH_CONNECT_TIMEOUT_SECONDS`. |
| `tools/machines.py` | Loads `machines.yaml` deep-merged under `machines.local.yaml`. `is_placeholder` is the single test for unfilled fields. |
| `tools/simulation_registry.py` | Allocates the global sequential `simulation_id` from `kb/simulation_registry.yaml`. |
| `tools/legacy_import.py` | Imports the historical xlsx notebook as `origin: notebook` evidence. |
| `tools/run_adoption.py` | Folds pre-catalog `runs/` provenance into `origin: adopted` records. |
| `tools/case_promotion.py`, `tools/input_archive.py` | Turn `input_backup/` plus the notebook into drafted kb cases. |
| `tools/sheet_sync.py` | Mirrors the catalog to Google Sheets; pulls back only the human-owned `note`. |

## Related documents

`docs/workflow.md` walks one submission end to end.
`docs/roadmap.md` records which of these are verified and which are not.
