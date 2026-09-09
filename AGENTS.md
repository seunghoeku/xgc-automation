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
- Remote analysis placement is decided by **workload character, not duration
  alone**: bounded file-access-dominant analysis may run on a login node for up
  to six hours; compute-dominant analysis needs a scheduler allocation even
  when short; anything above six hours needs the scheduler regardless.
  (`docs/data-analysis.md`)

## Data Principles
- XGC output is ADIOS2 (`.bp`) and can reach several TB. **Never download it
  all.** Reduce remotely to summary products, then pull only small amounts.
- Every new scientific simulation gets one **globally sequential
  `simulation_id`** from `kb/simulation_registry.yaml`. Numbers are never
  reused across machines. Scheduler attempts keep separate timestamp-based
  `run_id` values. (`docs/simulation-ids.md`)
- Each simulation owns one catalog record, `kb/simulations/<id>.yaml`,
  maintained automatically by reservation, staging, submission, and status
  checks, and searched with `xgc-sim`. A record's `origin` bounds what it may
  claim — `automation` (verified provenance) / `adopted` (real pre-catalog
  runs, outcome never recorded) / `notebook` (historical evidence only) — and
  only `automation` records own a reserved number. Adoption never mints one
  retroactively. (`docs/simulation-catalog.md`)
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

## Where things are documented
| Question | File |
|---|---|
| What is built, verified, still open, roadmap item N | `docs/roadmap.md` |
| Which tool/module owns which job | `docs/tool-layers.md` |
| One submission end to end | `docs/workflow.md` |
| Simulation IDs, directory naming, reservation flow | `docs/simulation-ids.md` |
| Catalog records, origins, status machine, `xgc-sim`, Sheets | `docs/simulation-catalog.md` |
| Remote reduction, analysis placement rules | `docs/data-analysis.md` |
| SSH aliases, ControlMaster, sshproxy, multiplexing | `docs/ssh-setup.md` |
| First-time setup, overlay files, absent personal data | `docs/setup-new-user.md` |
| Building XGC per machine | `docs/build-stellar.md`, `docs/build-polaris.md`, `docs/build-aurora.md` |
| Historical input archive and case promotion | `docs/input-archive.md` |
| Remote XGC_GUI Monitor | `docs/remote-gui.md` |
| Unattended queue polling from a local timer | `docs/queue-watch.md` |
| Non-Maxwellian `f_init` procedure (optional; load only when asked) | `f_init/README.md`, skill `f-init` |
| Traps already paid for (per case) | `kb/cases/<name>.yaml` notes, `docs/input-archive.md` |
| Past session history and rationale | `git log` |
