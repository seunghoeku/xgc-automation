# XGC Automation Project — Agent Instructions (AGENTS.md)

> This file is loaded automatically into context at startup (opencode via
> `opencode.json`, Claude Code directly). It therefore holds **only what must
> govern every action**: goal, architecture, invariants, and where to look for
> everything else. Background, status, and per-machine detail live in `docs/`
> — see the map at the end. Keep this file short; when something here grows
> into a procedure or a history, move it to `docs/` and leave a pointer.

## Project Goal
Describe a physics problem in natural language → a local AI proposes XGC
(gyrokinetic code, xgc.pppl.gov) input values → a human reviews and edits via
diff → submit the job to Perlmutter/Polaris → summarize, analyze, and
visualize results locally.
**Single-user**, prototype-first. Team scaling comes later.

## Core Architecture: Local Brain + Remote Hands
- **The LLM runs locally only** (macOS first, Linux compatible). Endpoint: the
  `ohmic.pppl.gov` API.
- **No external LLM calls from inside NERSC** → treat HPC purely as an SSH target.
- The LLM only proposes, explains, and coordinates. Risky or deterministic
  actions (job submission, file deletion, transfers) are performed by
  **verified code (tools)**.

## Absolute Rules (must be followed)
These numbers are cited from source comments (`tools/*.py`) and from `docs/`.
Do not renumber them.

0. **Project rules take precedence**: for operations performed through this
   repository, this file and the deterministic `tools/` defaults override
   generic HPC guidance such as `perlmutter-ops` when they conflict. Use
   generic guidance only to fill gaps not covered here.
1. **Human approval before submission**: always show a diff/summary and get
   user approval before `sbatch`/`qsub`, or before deleting/overwriting files
   (human-in-the-loop).
2. **Deterministic actions go through tools**: for SSH, schedulers, and file
   transfers, the LLM must not invent shell commands — use the defined tools
   (`hpc_runner`, etc.). See `docs/tool-layers.md` for what exists.
3. **Humans handle authentication**: never enter MFA codes or passwords on the
   user's behalf. Reuse human-established ControlMaster sessions for MFA hosts.
   Perlmutter is the exception: use the current NERSC sshproxy
   key/certificate directly, over a multiplexing socket this toolchain owns
   under `~/.ssh/xgc-cm/` — never the user's own `ControlPath`. Details and the
   required `~/.ssh/config` contract: `docs/ssh-setup.md`.
4. **Protect the allocation**: present node-count and wall-time estimates
   before submitting a job.
5. **Record provenance**: track which input produced which output.

## Operational Limits (do not let generic guidance override these)
- SSH connect timeout is **60 s**, `SSH_CONNECT_TIMEOUT_SECONDS` in
  `tools/ssh.py`. Do not replace it with a shorter generic Perlmutter default.
- **Dryrun rule by machine**: where a login node can run `xgc --dryrun`
  (Perlmutter, stellar) a rejected input blocks submission. Where it cannot
  (Polaris, Aurora; `dryrun.login_node: false` in `machines.yaml`) the job is
  submitted first and, if it has not started after a short grace period, a
  1-node job runs the dryrun in parallel in the first queue that can take it
  now (`dryrun.queues`; a debug queue the user already occupies is skipped for
  a regular one); its verdict is attached on the next status check and a
  rejection is a warning, never an automatic cancel.
  Do not pass `skip_dryrun` there. (`docs/workflow.md`, "Dryrun on machines
  without a login-node dryrun")
  **Dev (`d`-series) simulations skip the dryrun by default** on every
  machine (`skip_dryrun` unset); production runs keep the gate. Pass
  `skip_dryrun=False` to validate a dev run anyway.
- Remote analysis placement is decided by **workload character, not duration
  alone**: bounded file-access-dominant analysis may run on a login node for up
  to six hours; compute-dominant analysis needs a scheduler allocation even
  when short; anything above six hours needs the scheduler regardless.
  (`docs/data-analysis.md`)

## Data Principles
- XGC output is ADIOS2 (`.bp`) and can reach several TB. **Never download it
  all.** Reduce remotely to summary products, then pull only small amounts.
- Pulled products (PNG, NPZ, SVG, job logs) go to the **data directory**
  (`paths.product_target`, `data_dir` in the user config, `xgc-sim paths`),
  never into the catalog repository; the script and a small JSON of the
  numbers go to the catalog. (`docs/simulation-catalog.md`, "Data directory")
- Every new simulation gets one **globally sequential `simulation_id`** from
  `kb/simulation_registry.yaml`, in one of two series: **production** physics
  runs are `n671pe`; **dev** test, debug, and algorithm-verification runs are
  `d0001pe`, on their own counter, so code tests stop consuming physics
  numbers. Pass `kind="dev"` to `plan_simulation_id`/`reserve_simulation_id`
  for a test run and ask when the purpose is unclear. Numbers are never reused
  across machines. Scheduler attempts keep separate timestamp-based `run_id`
  values. (`docs/simulation-ids.md`)
- Each simulation owns one catalog record, `kb/simulations/<id>.yaml`,
  maintained automatically by reservation, staging, submission, and status
  checks, and searched with `xgc-sim`. A record's `origin` bounds what it may
  claim — `automation` (verified provenance) / `adopted` (real pre-catalog
  runs, outcome never recorded) / `notebook` (historical evidence only) — and
  only `automation` records own a reserved number. Adoption never mints one
  retroactively. The record has two notes: `note` is the human's (write it
  only with approved wording) and `ai_note` is the agent's own, kept current
  with `set_ai_note` after submissions, failures and findings, without asking.
  (`docs/simulation-catalog.md`)
- A submission also records what a rerun needs (hashes of the referenced mesh,
  equilibrium and profile files; build settings), appends the input to
  `input_backup/<directory_name>`, and rewrites `kb/simulation_notebook.csv`,
  the spreadsheet view. The CSV is derived and gitignored: the records are
  the source of truth, and `xgc-sim sync-notebook --confirm` rebuilds it on a
  fresh checkout (`docs/simulation-catalog.md`, "Reproduction evidence" and
  "CSV notebook"). `Other_data/simulation_note_XGC1.xlsx` is historical and
  never written by automation.
- One simulation spans many scheduler jobs, because long runs are sized to hit
  the wall clock and restart from a checkpoint. Status is derived from the
  whole restart chain, and a timed-out job leaves it **`interrupted`**
  ("restart to continue"), never `failed`.

## Machines and Configuration
| Machine | Center | Scheduler | Auth | Scratch |
|---|---|---|---|---|
| Perlmutter | NERSC | Slurm (`sbatch/squeue/scancel`) | sshproxy → 24h SSH key | `$PSCRATCH` |
| Polaris | ALCF | PBS (`qsub/qstat/qdel`) | ssh tunnel/port forwarding + MFA | `/grand/<project>` |
| Aurora | ALCF | PBS (`qsub/qstat/qdel`) | ControlMaster + MFA | `/lus/flare/projects/<project>` (Lustre; jobs must run here) |

`machines.yaml` (committed, machine-universal facts) plus the gitignored
`machines.local.yaml` overlay (per-user bindings) and the `hpc_runner` tool
absorb the differences. **Higher-level logic must remain unaware of the machine
type.** Never write personal values into committed files.
(`docs/setup-new-user.md`)

Shared files (code, docs, `machines.yaml`, `templates/`, `kb/cases/`) come
from the checkout; the user's state (catalog, registry, `runs/`,
`input_backup/`, case logs, caches) lives in the user's **catalog
repository**, found through `XGC_CATALOG_DIR` or
`~/.config/xgc-automation/config.yaml` (default: the checkout, gitignored
there). State paths are taken from `tools/paths.py`, never rebuilt from
`PROJECT_ROOT`; `xgc-sim paths` shows the resolved layout. Catalog writes are
committed and pushed in the catalog repository, never here; the write tools
do that themselves (`tools/catalog_sync.py`: pull before, commit and push
after, reported under `catalog_sync`). A checkout on the
machine itself binds it with `auth.method: local` (`docs/ssh-setup.md`).
(`docs/simulation-catalog.md`, "Catalog repository")

## Per-user context
The person's accounts, allocations, checkouts and working preferences are in
their catalog repository's `USER.md`, imported here through a symlink (absent
for a user who has not set one up; `templates/USER.md.example`):

@~/.config/xgc-automation/USER.md

## Portability (macOS first, Linux compatible)
- All remote work goes through `tools/ssh.py` (argv-list subprocess calls, no
  shell-string parsing, no BSD/GNU CLI differences).
- No hardcoded paths (`~`, `$PSCRATCH`). Distribute with `uv`/`pipx`.

## Build
Build automation is out of scope. Executables are not always available, so
record per-machine procedures and known pitfalls in `docs/build-<machine>.md`
(stellar and Polaris exist; read them before building — both contain traps that
cost a session to rediscover). Point `xgc.exe` in `machines.yaml` at the built
executable. A `hpc_runner.build(machine)` hook is possible later.

## Companion repository: XGC-Devel
The XGC source lives in a separate, team-shared repository,
`~/Documents/git/XGC-Devel`, attached to sessions here as an additional
working directory (`.claude/settings.local.json`, gitignored). Division of
labor: **changes to the code itself go to XGC-Devel; everything about
simulation input and output — cases, inputs, `simulation_id`s, submissions,
catalog records, analysis — stays here.** Before editing XGC source, read
`XGC-Devel/AGENTS.md` (coding rules) and `XGC-Devel/CLAUDE.local.md` (local
build notes); neither is loaded automatically from an additional directory.
Commit from inside the repository being changed, never both at once. After
building a new binary, update `machines.local.yaml` here before the next
submission. Full rules and handoffs: `docs/repo-roles.md`.

## Where things are documented
| Question | File |
|---|---|
| What is built, verified, still open, roadmap item N | `docs/roadmap.md` |
| Which repository a change belongs to (XGC-Devel code vs. simulation I/O here), handoffs | `docs/repo-roles.md` |
| Which tool/module owns which job | `docs/tool-layers.md` |
| One submission end to end | `docs/workflow.md` |
| Simulation IDs, directory naming, reservation flow | `docs/simulation-ids.md` |
| Catalog records, origins, status machine, `xgc-sim`, Sheets | `docs/simulation-catalog.md` |
| Remote reduction, analysis placement rules | `docs/data-analysis.md` |
| SSH aliases, ControlMaster, sshproxy, multiplexing | `docs/ssh-setup.md` |
| First-time setup, overlay files, absent personal data | `docs/setup-new-user.md` |
| Building XGC per machine | `docs/build-stellar.md`, `docs/build-stellarai.md`, `docs/build-stellarai-gpu.md`, `docs/build-polaris.md`, `docs/build-aurora.md` |
| Aurora day to day: submit, DAOS container, reading results | `docs/aurora-operations.md` |
| Historical input archive and case promotion | `docs/input-archive.md` |
| Remote XGC_GUI Monitor | `docs/remote-gui.md` |
| HPC-Campaign `.aca` archive built when a simulation completes | `docs/campaign.md` |
| Unattended queue polling from a local timer | `docs/queue-watch.md` |
| Standalone SwiftBar monitor distribution | [hpc-queue-monitor](https://github.com/seunghoeku/hpc-queue-monitor); checkout integration in `docs/queue-watch.md` |
| Non-Maxwellian `f_init` procedure (optional; load only when asked) | `f_init/README.md`, skill `f-init` |
| Rotation-input matching loop for XGCa (optional; load only when asked) | `flow_match/README.md`, CLI `xgc-flowmatch` |
| Traps already paid for (per case) | `kb/cases/<name>.yaml` notes, `docs/input-archive.md` |
| Past session history and rationale | `git log` |
