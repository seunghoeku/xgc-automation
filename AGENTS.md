# XGC Automation Project — Agent Instructions (AGENTS.md)

> opencode loads this file automatically as context at startup.
> Follow the background, rules, and current phase described here.

## Project Goal
Describe a physics problem in natural language → a local AI proposes XGC
(gyrokinetic code, xgc.pppl.gov) input values → a human reviews and edits via diff
→ submit the job to Perlmutter/Polaris → summarize, analyze, and visualize results
locally.
**Single-user**, prototype-first. Team scaling comes later.

## Core Architecture: Local Brain + Remote Hands
- **The LLM runs locally only** (macOS first, Linux compatible). Endpoint: the
  `ohmic.pppl.gov` API.
- **No external LLM calls from inside NERSC** → treat HPC purely as an SSH target.
- The LLM only proposes, explains, and coordinates. Risky or deterministic actions
  (job submission, file deletion, transfers) are performed by **verified code (tools)**.

## Absolute Rules (must be followed)
0. **Project rules take precedence**: for operations performed through this
   repository, this file and the deterministic `tools/` defaults override
   generic HPC guidance such as `perlmutter-ops` when they conflict. Use
   generic guidance only to fill gaps not covered here.
1. **Human approval before submission**: always show a diff/summary and get user
   approval before `sbatch`/`qsub`, or before deleting/overwriting files
   (human-in-the-loop).
2. **Deterministic actions go through tools**: for SSH, schedulers, and file
   transfers, the LLM must not invent shell commands — use the defined tools
   (`hpc_runner`, etc.).
3. **Humans handle authentication**: never enter MFA codes or passwords on the
   user's behalf. Reuse human-established ControlMaster sessions for MFA hosts.
   Perlmutter is the exception: use the current NERSC sshproxy key/certificate
   directly, over a multiplexing socket this toolchain owns under
   `~/.ssh/xgc-cm/` — never the user's own `ControlPath`, and the user's
   `~/.ssh/config` must not default those hosts to ControlMaster.
4. **Protect the allocation**: present node-count and wall-time estimates before
   submitting a job.
5. **Record provenance**: track which input produced which output.

The repository-wide SSH connection timeout is 60 seconds, defined by
`SSH_CONNECT_TIMEOUT_SECONDS` in `tools/ssh.py`. Do not replace it with a
shorter generic Perlmutter default.

Remote analysis placement is determined primarily by workload character, not
duration alone. Bounded file-access-dominant analysis may run on a login node
for up to six hours. Compute-dominant analysis must use a scheduler allocation
even when short, and any analysis exceeding six hours must use the scheduler.

## HPC Targets
| Machine | Center | Scheduler | Auth | Scratch |
|---|---|---|---|---|
| Perlmutter | NERSC | Slurm (`sbatch/squeue/scancel`) | sshproxy → 24h SSH key | `$PSCRATCH` |
| Polaris | ALCF | PBS (`qsub/qstat/qdel`) | ssh tunnel/port forwarding + MFA | `/grand/<project>` |

`machines.yaml` (committed, machine-universal facts) plus the gitignored
`machines.local.yaml` overlay (per-user bindings: username, account, paths,
personal builds — see `docs/setup-new-user.md`) and the `hpc_runner` tool
absorb the differences between machines. Higher-level logic must remain
unaware of the machine type.

## MCP Tool Layers (to implement)
1. `preflight` — Is ohmic reachable? Is the Perlmutter key valid for 24h? Is the
   Polaris tunnel alive?
2. `physics_advisor` — problem description → parameter proposals (hybrid of KB +
   rules/scaling; no raw LLM-invented numbers)
3. `input_builder` — build the XGC namelist (`input`), geqdsk references, and
   profiles via Jinja2
4. `validator` — dt vs gyrofrequency, grid vs rho_i, particles/cell, wall-clock
   estimate
5. `hpc_runner` — `submit/status/cancel/push/pull` (slurm vs pbs branching)
6. `data_analyzer` — generate reduced products remotely → pull locally → plot
   (heat flux, profiles, spectra)
7. `provenance` — input→output lineage (commit, hash, timestamp)
8. `remote_gui` — resolve a remote simulation, start XGC-Devel's existing
   Monitor on the login node, tunnel it to localhost, and manage its lifecycle
9. `simulation_catalog` — one durable record per simulation
   (`kb/simulations/<id>.yaml`) linking its number, runs, status, archive
   location, and human note; searchable via the `xgc-sim` CLI without an LLM,
   optionally mirrored to Google Sheets (`docs/simulation-catalog.md`)

## Data Principles
- XGC output is ADIOS2 (`.bp`) and can reach several TB. **Never download it all.**
- Reduce remotely (summary products), then pull only small amounts locally. For the
  prototype, start with post-hoc summaries.
- Every new scientific simulation receives one globally sequential
  `simulation_id` from `kb/simulation_registry.yaml`. The number is never
  reused across machines. Scheduler submission attempts retain separate
  timestamp-based `run_id` values.
- Each simulation also owns one catalog record, `kb/simulations/<id>.yaml`,
  which links that number to its runs, status, input hash, archive location,
  and the human's note. It is maintained automatically by reservation,
  staging, submission, and status checks, and searched with `xgc-sim`.
  A record's `origin` bounds what it may claim: `automation` (verified
  provenance), `adopted` (real pre-catalog runs, outcome never recorded), or
  `notebook` (historical evidence only). Only `automation` records own a
  reserved sequence number; adoption never mints one retroactively.
- One simulation spans many scheduler jobs, because long runs are sized to hit
  the wall clock and restart from a checkpoint. A simulation's status is
  therefore derived from its whole restart chain, and a timed-out job leaves it
  `interrupted` ("restart to continue"), never `failed`. `xgc-sim refresh`
  polls every in-flight run in one command.

## Portability (macOS first, Linux compatible)
- SSH work goes through `tools/ssh.py`: argv-list subprocess calls against the
  user's ControlMaster socket for MFA hosts, or the current NERSC sshproxy
  key/certificate for Perlmutter (no shell-string parsing or BSD/GNU CLI
  differences).
- No hardcoded paths (`~`, `$PSCRATCH`). Distribute with `uv`/`pipx`.
- MFA-host multiplexing: `ControlMaster auto` / `ControlPersist 8h` /
  `ControlPath ~/.ssh/cm-%r@%h:%p`. The user's `~/.ssh/config` must set
  `ControlMaster no` / `ControlPath none` for Perlmutter; the toolchain
  multiplexes it separately over its own socket in `~/.ssh/xgc-cm/`
  (`XGC_SSH_NO_MULTIPLEX=1` disables that).

## Build Automation
- Automation is still out of scope. However, executables are not always available,
  so record per-machine build procedures and known pitfalls in
  `docs/build-<machine>.md`.
- Specify the built executable path in `xgc.exe` in `machines.yaml`.
- **stellar**: see `docs/build-stellar.md`. Two key points —
  (1) you must source `set_up_xgc.stellar`, and (2) `CMAKE_BUILD_TYPE=Release` is
  required. `RelWithDebInfo` (`-O2`) crashes the icpx 2024.2 vectorizer with SIGSEGV.
- Later, extend via a `hpc_runner.build(machine)` hook (CMake + per-machine modules).

## Current Phase / Next Steps (Roadmap)
- [x] 1. `preflight` tool — check connectivity and authentication state
- [x] 2. Finalize `machines.yaml` (stellar and Polaris done; Perlmutter
      access/account/scratch/Slurm settings are verified, but its general
      production `xgc.exe` remains deliberately unset; flux is unconfirmed)
- [x] 3. `hpc_runner` MCP — submit/status/cancel/output (slurm+pbs).
      push/pull exist in `tools/ssh.py` but are not yet exposed as MCP tools.
- [x] 4. Successful hello-world sbatch round trip — stellar, jobs 2888408/2888413,
      exit 0:0. Polaris PBS also passed live. Perlmutter CPU XGC build, dryrun,
      Slurm jobs, restarts, and reduced output analysis were exercised in the
      reduced-adiabatic campaign; a canonical automation hello-world/provenance
      round trip is still useful after selecting a production executable.
- [x] 5. `input_builder` + `validator` — `validator` via `xgc --dryrun` on the
      login node (`tools/dryrun.py`), wired in as a pre-submit gate.
      `input_builder` builds from validated baselines in `kb/cases/`, reusing
      XGC's own `utils/input_gui/` parser and metadata. See `docs/workflow.md`.
- [x] 6. `physics_advisor` (KB + deterministic rules). Conservative MVP:
      selects only validated KB cases, carries through explicitly stated
      step/restart values, and rejects unsupported physics rather than
      inventing parameters. ECBC is currently the only baseline.
- [ ] 7. `data_analyzer` — bounded inventory plus an XGC-Analysis-backed
      `oned_profiles` reducer are implemented. The latter selects only the
      first/latest logical steps remotely and saves bounded JSON plus a local
      SVG without transferring `.bp`. Fixture tests pass; a real HPC run still
      needs to verify the remote analysis environment before this item closes.
- [x] 8. Polaris adapter (pbs/qsub) — PBS template, local tests, and a live
      1-node qsub/qstat/output round trip passed with exit 0 (job 7301673).
      Live qdel was not exercised; the XGC executable is compiled but not
      runtime-tested.
- [x] 9. Strengthen provenance — the simulation catalog
      (`tools/simulation_catalog.py`) gives every catalog item one durable
      record under `kb/simulations/`, created at reservation and updated
      automatically by staging, submission, and status checks, so a number is
      always linked to its runs, provenance files, input hash, and archive
      location. Submission now also stores the submitted namelist and its
      SHA-256 in the run record, parses `sml_node_file`, `sml_electron_on`,
      `eq_filename`, and `ptl_marker_type` with XGC's namelist parser, and keeps
      effective field model, marker model, and species summaries unknown when
      historical evidence is insufficient. Catalog identity is the pair
      `simulation_tag + sub_tag`; the latter defaults to the directory basename,
      so reused tags such as `n645pe` remain distinct when their directories
      differ. `tools/legacy_import.py` imports the historical notebook (741 of
      742 rows) as `origin: notebook` evidence
      records, `tools/run_adoption.py` folds the 35 pre-catalog provenance
      runs under `runs/` into 25 `origin: adopted` simulations grouped by
      working directory (no retroactive numbering, no invented outcomes), and
      `tools/simcat_cli.py` (`xgc-sim`) searches all of it with no LLM
      involved. `tools/sheet_sync.py` mirrors the catalog to Google
      Sheets, pushing machine-known fields and pulling back only the
      human-owned `note` column (optional extra: `uv sync --extra sheets`).
      See `docs/simulation-catalog.md`. Open: a live Google Sheets round trip
      still needs a real service account, and HPSS archiving is recorded but
      not driven. Team-sharing UI remains later work.
- [x] 10. Case promotion pipeline (`tools/case_promotion.py`) — joins
      `input_backup/` with the researcher's simulation notebook
      (`Other_data/simulation_note_XGC1.xlsx`), classifies outcomes from the
      notebook's own wording (negation-aware), detects binary eras
      (xgc-eem/xgc-es/xgca) and name-note mismatches, and drafts kb cases
      under `kb/cases/drafts/`. Three baselines were promoted and
      dryrun-validated on polaris via debug-queue jobs: st40-heatload
      (n620fr), asdex-imode (n566pe), kstar-rmp (n613fr). Together with ECBC
      and the historical TCV XGCa baseline below, KB now has 5 cases.
      Open: torque family needs the freshly built (untested) xgca binary;
      kstar-rmp needs its missing RMP field file before a real run.
      A fifth historical baseline, tcv-xgca-sheath (n641pe/sheath_m1_3), is
      runtime-validated on Perlmutter with XGCa. It is the regular-sheath
      reference, not the sibling Gyraze-surrogate variant; current-master
      dryrun remains required before reuse.
- [x] 11. Remote XGC_GUI launcher (`tools/remote_gui.py`) — the
      `xgc-remote-gui` CLI and matching MCP tools resolve explicit paths or
      local provenance, validate the remote GUI/Python environment, load an
      optional `.xgcg` preset, start a loopback-only login-node Monitor and SSH
      tunnel, open the local browser, and provide status/stop lifecycle tools.
      Perlmutter uses independent sshproxy key connections; no scheduler job
      or BP transfer is involved. A live Perlmutter plan/open/status/stop round
      trip passed against the TCV reduced-adiabatic run on 2026-08-07.

## Open Questions to Resolve First
1. **ohmic.pppl.gov API spec** — OpenAI-compatible? Authentication (key/SSO/IP)?
   Is a VPN required locally?
2. XGC version (XGC1 vs XGCa), primary physics interest (core turbulence /
   edge-SOL / ELM).
3. ~~Do we have past successful inputs/geqdsk files for the physics_advisor
   KB?~~ Answered: yes — `input_backup/` plus the simulation notebook feed
   the case promotion pipeline (roadmap item 10); five baselines promoted.
4. Should we also use the Superfacility API for job submission?
5. Output data volume and scratch cleanup policy.
