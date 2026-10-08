# Tool layers

The map from a job in the workflow to the module that owns it. `AGENTS.md`
absolute rule 2 says deterministic actions go through these tools rather than
improvised shell commands; this file is the list of what exists, so that rule
is actionable.

All MCP tools are exposed from `tools/server.py`. The repository installs
`xgc-sim` (`tools/simcat_cli.py`), `xgc-remote-gui` (`tools/remote_gui.py`),
`xgc-queue-watch` (`tools/queue_watch.py`), and the optional-workflow CLI
`xgc-finit` (`f_init/cli.py`). The independently distributed
[HPC Queue Monitor](https://github.com/seunghoeku/hpc-queue-monitor) installs
`hpc-monitor`; it is not an entry point of the root `xgc-automation` package.

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
| `campaign` | `tools/campaign.py` | When a simulation turns `completed` (refresh/status), starts XGC-Analysis's HPC-Campaign packager detached on the login node (opt-in `campaign.auto`) and records the result in the record's `campaign` field; never replaces an existing archive without confirmation. `xgc-sim campaign`, MCP `build_campaign`/`campaign_status`. See `docs/campaign.md`. |
| `remote_gui` | `tools/remote_gui.py` | Resolves a remote simulation, starts XGC-Devel's existing Monitor on the login node, tunnels it to localhost, and manages its lifecycle. |
| `simulation_catalog` | `tools/simulation_catalog.py`, `tools/simcat_cli.py`, `tools/refresh.py` | One durable record per simulation (`kb/simulations/<id>.yaml`) linking number, runs, status, archive location, and human note. Searchable without an LLM via `xgc-sim`. |
| `f_init` (optional) | `f_init/` | The non-Maxwellian `f_init` procedure: f0 snapshot to `f_init` files, dt=0 check runs, density adjustment. Own CLI `xgc-finit` and skill `f-init`; deliberately outside `tools/` and outside the everyday context. See `f_init/README.md`. |
| `queue_watch` | `tools/queue_watch.py` | Asks every reachable machine what the user currently has queued, lists it, and diffs against the last snapshot. Machines that cannot be reached without authenticating are skipped with a reason. Read-only, built for a local timer rather than a chat session. See `docs/queue-watch.md`. |
| Standalone queue monitor | `hpc-queue-monitor` repository, `src/hpc_monitor/` | Public server presets, validated `servers.yaml`, isolated per-config history, polling lock, CLI, and macOS timer/SwiftBar installer. Initially extracted from this repository; developed and distributed separately. |

## Supporting modules

| Module | Role |
|---|---|
| `tools/ssh.py` | Every remote call. argv-list subprocess, ControlMaster for MFA hosts, sshproxy key + tool-owned `~/.ssh/xgc-cm/` socket for Perlmutter, and explicit `key` mode for generic public-key hosts. Connect timeout 60 s; existing-master channel probe 30 s. |
| `tools/catalog_init.py` | Defines the catalog layout (`users/<github-id>/`, seeded registry, `.gitignore`) and creates it: `xgc-sim init-catalog`; `layout_check` behind `xgc-sim paths`. Identical for every user so catalogs merge by copying user directories. |
| `tools/paths.py` | Resolves where shared files (checkout) and the user's state (`XGC_CATALOG_DIR`, `~/.config/xgc-automation/config.yaml`, default the checkout) live; every state constant in the other modules is initialised from it. `xgc-sim paths` prints the layout. |
| `tools/machines.py` | Loads `machines.yaml` deep-merged under `machines.local.yaml`. `using_machines` supplies scoped explicit definitions for standalone consumers without reading repository configuration. `is_placeholder` tests unfilled fields. |
| `tools/queue_menu.py` | Cache-only SwiftBar rendering: jobs, recent departures, start estimates, prioritized server state icons, and separate connection warnings. |
| `tools/monitor_dist.py` | Exports the original package snapshot with a five-module allowlist and source hashes. Public package development/builds now use the independent `hpc-queue-monitor` repository. |
| `tools/simulation_registry.py` | Allocates the global sequential `simulation_id` from `kb/simulation_registry.yaml`. |
| `tools/legacy_import.py` | Imports the historical xlsx notebook as `origin: notebook` evidence. |
| `tools/run_adoption.py` | Folds pre-catalog `runs/` provenance into `origin: adopted` records. |
| `tools/case_promotion.py`, `tools/input_archive.py` | Turn `input_backup/` plus the notebook into drafted kb cases. |
| `tools/sheet_sync.py` | Mirrors the catalog to Google Sheets; pulls back only the human-owned `note`. |

## Related documents

`docs/workflow.md` walks one submission end to end.
`docs/roadmap.md` records which of these are verified and which are not.
