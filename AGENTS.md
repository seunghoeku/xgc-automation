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
1. **Human approval before submission**: always show a diff/summary and get user
   approval before `sbatch`/`qsub`, or before deleting/overwriting files
   (human-in-the-loop).
2. **Deterministic actions go through tools**: for SSH, schedulers, and file
   transfers, the LLM must not invent shell commands — use the defined tools
   (`hpc_runner`, etc.).
3. **Humans handle authentication**: never enter MFA codes or passwords on the
   user's behalf. The user establishes the session; reuse it via ssh ControlMaster.
4. **Protect the allocation**: present node-count and wall-time estimates before
   submitting a job.
5. **Record provenance**: track which input produced which output.

## HPC Targets
| Machine | Center | Scheduler | Auth | Scratch |
|---|---|---|---|---|
| Perlmutter | NERSC | Slurm (`sbatch/squeue/scancel`) | sshproxy → 24h SSH key | `$PSCRATCH` |
| Polaris | ALCF | PBS (`qsub/qstat/qdel`) | ssh tunnel/port forwarding + MFA | `/grand/TokamakProfile` |

`machines.yaml` plus the `hpc_runner` tool absorb the differences between machines.
Higher-level logic must remain unaware of the machine type.

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

## Data Principles
- XGC output is ADIOS2 (`.bp`) and can reach several TB. **Never download it all.**
- Reduce remotely (summary products), then pull only small amounts locally. For the
  prototype, start with post-hoc summaries.
- Every new scientific simulation receives one globally sequential
  `simulation_id` from `kb/simulation_registry.yaml`. The number is never
  reused across machines. Scheduler submission attempts retain separate
  timestamp-based `run_id` values.

## Portability (macOS first, Linux compatible)
- Implement real SSH work in Python (paramiko/asyncssh) rather than parsing CLI
  output → avoids BSD/GNU CLI differences.
- No hardcoded paths (`~`, `$PSCRATCH`). Distribute with `uv`/`pipx`.
- ssh multiplexing: `ControlMaster auto` / `ControlPersist 8h` /
  `ControlPath ~/.ssh/cm-%r@%h:%p`.

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
- [x] 2. Finalize `machines.yaml` (stellar done; Polaris paths and PBS
      resources plus its compiled executable are recorded;
      flux scheduler still unconfirmed, Perlmutter inaccessible)
- [x] 3. `hpc_runner` MCP — submit/status/cancel/output (slurm+pbs).
      push/pull exist in `tools/ssh.py` but are not yet exposed as MCP tools.
- [x] 4. Successful hello-world sbatch round trip — stellar, jobs 2888408/2888413,
      exit 0:0. PBS path is implemented but untested (no reachable PBS machine).
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
- [ ] 8. Polaris adapter (pbs/qsub) — PBS template and local parser/rendering
      tests are implemented. Live qsub/qstat/qdel verification still requires
      explicit approval; the executable is compiled but not runtime-tested.
- [ ] 9. Strengthen provenance / (later) team-sharing UI

## Open Questions to Resolve First
1. **ohmic.pppl.gov API spec** — OpenAI-compatible? Authentication (key/SSO/IP)?
   Is a VPN required locally?
2. XGC version (XGC1 vs XGCa), primary physics interest (core turbulence /
   edge-SOL / ELM).
3. Do we have past successful inputs/geqdsk files for the physics_advisor KB?
4. Should we also use the Superfacility API for job submission?
5. Output data volume and scratch cleanup policy.
