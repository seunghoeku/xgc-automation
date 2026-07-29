# HANDOFF

Session date: 2026-07-28. Written for the next coding agent picking this up.

Read `AGENTS.md` first — it holds the project rules, and they are not optional.
This document covers what happened in this session and what to do next.

---

## 1. Session goal and decisions

**Goal.** Take the project from an empty scaffold to a working pipeline:
describe a change → propose input values → human reviews a diff → submit to
HPC → monitor. Roadmap items 1–5 in `AGENTS.md`.

### Decisions made, with reasoning

**Target machine switched to stellar.** Perlmutter and Polaris are both
unreachable (TCP:22 times out). `flux.pppl.gov` resolves and port 22 is open,
but its scheduler type was never confirmed, so it is deliberately left as a
`<slurm|pbs>` placeholder in `machines.yaml` and the tooling refuses to submit
there. Do not guess it.

**Reuse XGC's own code rather than reimplementing it.** XGC ships
`utils/input_gui/` (a Flask GUI) containing a Fortran namelist parser and a
metadata extractor that scrapes every parameter's type, default, description,
and valid options from the C++ source. We import those instead of writing our
own parser. Two consequences worth preserving: files we write open cleanly in
the XGC GUI and vice versa, and the metadata tracks the C++ source instead of
drifting from it.

The alternative considered and rejected: driving the GUI's HTTP API. Its Flask
app keeps all state in a module-level `STATE` dict written for a single browser
session, so concurrent access from an agent would mutate what the human is
looking at — a direct conflict with absolute rule 1.

**Inputs are built by modifying a validated baseline, never generated from
scratch.** XGC has 708 input parameters and most are coupled. A diff against a
case known to pass `xgc --dryrun` is both safer and reviewable.

**`xgc --dryrun` is the validator.** Rather than reimplementing XGC's
validation rules, run the real binary. It parses the namelist, loads the mesh,
predicts memory, and checks particle resolution, then returns before
PETSc/ADIOS2 init. ~3 s on a login node, one rank, no allocation consumed.

**All repo content is in English.** Korean is for conversation only. This was
an explicit instruction mid-session; a prior scaffold generator
(`create_project.sh`) was deleted rather than translated because it embedded
stale, broken copies of config files.

---

## 2. Completed

Roadmap items 1–5 in `AGENTS.md` are done. 13 MCP tools:

| Tool | Purpose |
|---|---|
| `list_machines` | machines from `machines.yaml` |
| `preflight` | connectivity, auth, config readiness |
| `list_cases` | validated baseline cases |
| `describe_parameter` | a parameter's type/default/options from XGC source |
| `plan_input` | apply overrides to a baseline, return a diff |
| `stage_case` | copy a baseline's files to a new dir, remotely |
| `write_input` | write a modified input (needs confirm) |
| `validate_input` | `xgc --dryrun` on the login node |
| `plan_job` | render job script, estimate node-hours |
| `submit_job` | submit (needs confirm + passing dryrun) |
| `job_status` | normalized state across slurm/pbs |
| `cancel_job` | cancel (needs confirm) |
| `job_output` | bounded tail of stdout/stderr |

Beyond the tooling:

- **XGC builds on stellar.** The `-O2` compiler crash was diagnosed and worked
  around. See `docs/build-stellar.md`.
- **The ecbc case runs.** Migrated to current XGC, passes the dryrun cleanly,
  and reached XGC's main loop on a real job.
- **A real XGC run happened** (job 2888698): 11.4M ions and electrons pushed,
  ADIOS2 output written. Cancelled early — see §6.
- **Upstream comment fixes** pushed to the XGC fork (branch `fix_comments`).

---

## 3. In progress / not finished

**No XGC run has completed.** Job 2888698 reached step 3 of 50 before being
cancelled near its walltime. The toolchain is proven end to end, but no
scientific result has been produced.

**Roadmap 6–9 not started**: `physics_advisor`, `data_analyzer`, Polaris
adapter, provenance hardening.

**PBS support is written but untested.** `tools/scheduler.py` has a full PBS
adapter. No PBS machine was reachable, so none of it has ever executed.

**`push`/`pull` exist in `tools/ssh.py` but are not exposed as MCP tools.**
Deliberate: the data principles in `AGENTS.md` forbid bulk downloads, and no
reduction step exists yet to pull *from*.

**`flux` is unusable** until its scheduler type is confirmed.

**The `fix_comments` branch is on the fork only.** No PR to
`PrincetonUniversity/XGC-Devel` was opened. That is a judgement call left to
the human.

---

## 4. Files changed and why

### New — `tools/`

| File | Why |
|---|---|
| `ssh.py` | Transport. Reuses the human's ControlMaster socket and **never authenticates** (absolute rule 3). `BatchMode=yes` everywhere so a missing session fails fast instead of hanging on a prompt. Commands are argv lists quoted with `shlex`, never shell-interpolated. |
| `scheduler.py` | slurm/pbs adapters. All scheduler-specific parsing is confined here so callers see only normalized states. `get_scheduler()` refuses a `<...>` placeholder type — guessing would submit with the wrong command. |
| `hpc_runner.py` | Submission, status, cancel, output. Two-phase with a confirmation gate. Writes provenance per run. |
| `dryrun.py` | Runs `xgc --dryrun` remotely and parses the output. Parsing is split from execution so it is unit-testable on captured text without a cluster. |
| `namelist.py` | Bridge to XGC's `utils/input_gui/` parser and metadata over ssh, plus Fortran value rendering and validation. Caches metadata under `.cache/` keyed by XGC commit. |
| `input_builder.py` | `plan_input` / `write_input` / `stage_case` / `describe_parameter`. Baseline-plus-overrides with a diff. |

### Modified

| File | Why |
|---|---|
| `opencode.json` | **Was broken.** It redefined the `ohmic` provider and deep-merged over the working global config with four wrong values (missing `api.` subdomain, nonexistent model IDs, a `model` pointing at one of them, and `{env:...}` for a key that opencode does not load from `.env`). Provider config is machine-specific and does not belong in a repo. Now only `instructions`, `mcp`, and `permission`. |
| `machines.yaml` | Added stellar (real values, all measured) and flux (placeholders). |
| `templates/slurm.sbatch.j2` | Job script. Two bugs fixed — see §6. |
| `AGENTS.md`, `README.md`, `pyproject.toml`, `.gitignore` | Translated to English; roadmap updated. |
| `kb/cases/ecbc.yaml` | First knowledge-base entry. Physics setup, input files, migration history, and **measured** resource numbers. |
| `docs/` | `build-stellar.md`, `ssh-setup.md`, `workflow.md`. |

### Deleted

`create_project.sh` — a one-shot scaffold generator whose job was done. It
embedded stale heredoc copies of `AGENTS.md`, `machines.yaml`, and
`opencode.json`, including the provider bug above. Running it would have
regenerated a broken config.

### Outside this repo

**`~/Documents/git/XGC-Devel`, branch `fix_comments`, commit `39c5c89f`** —
comment-only fixes to XGC input units, pushed to the fork
(`git@github.com:seunghoeku/XGC-Devel.git`). Six files, 16 insertions.

**`/home/seungku/tmp/test_ecbc` on stellar** — the ecbc input was migrated in
place. **The original is backed up at `test_ecbc.bak.20260728`.**

---

## 5. Tests run and results

Everything below was actually executed, not assumed.

### Jobs on stellar

| Job | What | Result |
|---|---|---|
| 2888408 | hello-world, 4 ranks | completed, exit 0:0 |
| 2888413 | hello-world, 2 ranks | completed, exit 0:0, clean stderr |
| 2888415/2888416 | cancel path | refused without confirm; cancelled with it |
| 2888425 | hello-world after dryrun gate | completed, exit 0:0 |
| **2888698** | **real XGC, ecbc, 8 ranks × 12 threads** | **cancelled at ~11 min; reached main loop, stepped 3/50** |

### Gates verified

All of these were exercised and behaved correctly:

- `submit` without `confirm=True` → no-op
- `cancel` without `confirm=True` → no-op
- `submit(confirm=True)` with a **bad input** → **blocked by the dryrun**
- `flux` → blocked on placeholder scheduler
- `perlmutter` → blocked on placeholder executable
- unknown parameter name → rejected with `did_you_mean`
- invalid enum (`ptl_marker_type='DeltaF'`) → rejected, valid options listed
- type mismatch (`sml_mstep='abc'`) → rejected
- writing over a baseline case dir → refused

### Build

`ninja -j 16 xgc` at `-O3`: 248/248 targets, 0 crashes. Later incremental
rebuild on `fix_comments` at 120 s, 0 errors.

### Input validation

- `test_ecbc` original → REJECTED, 5 removed parameters
- `test_ecbc` migrated → ACCEPTED, 0 removed / 0 deprecated / 0 warnings
- dryrun at 8/16/32/64 ranks → all ACCEPTED

### Round trip

`stage_case` → `plan_input` → `write_input` → dryrun ACCEPTED, with the
baseline confirmed unmodified afterward.

---

## 6. Known problems and risks

### XGC warns this configuration is unvalidated

At runtime XGC prints:

> `WARNING: Using split weight scheme (required for electrostatic simulation
> using ReducedDeltaF electrons). This operation mode is not yet validated.`

**This is XGC's own warning about the physics configuration, not ours.**
`ReducedDeltaF` was set on the human's instruction and is consistent with the
case's intent, but the XGC team has flagged this combination as unvalidated.
Confirm with them before trusting production results.

### The case was silently running the wrong marker type

`sml_deltaf` and `sml_deltaf_elec` are **inert** in current XGC — they appear
only in the deprecation table and nothing reads them. So despite the input file
saying delta-f, the case defaulted to `TotalF`. The dryrun's low-resolution
warning was the visible symptom (0.0517 ptl/vertex is on target for
`ReducedDeltaF`, ~100× short for `TotalF`).

Generalize this: **a deprecation warning can mean the run is quietly doing
something other than what the input file appears to say.**

### Compiler crash on stellar

icpx 2024.2 segfaults (exit 139) in its loop vectorizer at `-O2`. `-O3`
(i.e. `CMAKE_BUILD_TYPE=Release`) is fine; `RelWithDebInfo` is unusable. Only
oneAPI 2024.2 is installed, so upgrading is not an option. Full flag matrix in
`docs/build-stellar.md`. For a debug build use
`-DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS=-g`.

### Two template bugs, both fixed — do not reintroduce

1. `trim_blocks` combined with `{%-` collapsed all `#SBATCH` lines onto one
   line, so Slurm silently ignored everything after `--time`.
2. No `--cpus-per-task` or `OMP_NUM_THREADS`, so OpenMP grabbed all 96 cores
   *per rank* and oversubscribed the node 8×.

### Estimates that were wrong

- The dryrun predicted **0.70 GB/rank**; observed **1.2 GB/rank** (~40% low).
  Matters when sizing near a memory limit.
- 50 steps at 8 ranks needs **~3 hours** (208 s/step + 166 s init). The
  10-minute walltime was never going to work. Scaling to more ranks is
  **unverified** — 64 ranks *should* be ~26 s/step but this has not been tested.

### Units are a trap

`sml_dt` is in main-ion toroidal transit times, **not seconds**, despite the
old source comment saying `(s)`. All `psi` inputs are normalized poloidal flux.
Confirmed empirically: `units.txt` shows `sml_dt(tau)=2.0e-4`,
`sml_dt(s)=1.797e-8`, `sml_tran(s)=8.986e-5`.

The comment fix is in `39c5c89f`, so metadata read from the stellar clone is
now correct. **But if you point the tooling at a different XGC checkout that
lacks that commit, `describe_parameter` will report seconds and mislead by ~10⁵.**

### Operational

- **ssh ControlMaster expires after 8 h.** It expired during this session's
  cleanup. Only the human can renew it (`ssh stellar`, MFA). `preflight` detects
  this and says so.
- **`scancel` returns 0 for a nonexistent job.** So `cancelled: true` means
  "request accepted", not "job existed". Use `job_status` to confirm.
- **A provenance record was lost** during cleanup (deleted before being
  committed) and had to be reconstructed from `sacct`. The reconstruction is
  marked as such in the file. Commit provenance before cleaning up.
- The stellar `build/` directory uses **Unix Makefiles**, not Ninja. Passing
  `-G Ninja` to it fails on a generator mismatch.

---

## 7. Next steps

Suggested order.

**1. Resolve the split-weight warning.** Ask the XGC team whether electrostatic
+ `ReducedDeltaF` electrons is trustworthy. Everything downstream depends on the
answer, so do this before investing in longer runs. Record it in
`kb/cases/ecbc.yaml`.

**2. Complete one XGC run.** 50 steps at 64 ranks, ~30 min walltime:

```python
plan_job("stellar", nodes=1, ntasks_per_node=64, cpus_per_task=1, walltime="00:40:00")
# show the plan to the human, get approval, then:
submit_job("stellar", confirm=True, case_dir="/scratch/gpfs/seungku/xgc-runs/ecbc-short",
           nodes=1, ntasks_per_node=64, cpus_per_task=1, walltime="00:40:00")
```

Ranks must be divisible by `sml_nphi_total=8`. Note 64 ranks × 1 thread on a
96-core node is not obviously optimal — measure before assuming.

**3. Then `data_analyzer` (roadmap 7).** A completed run gives real `.bp` files
to reduce. **Check XGC-GUI's Monitor page first** — it already does `.bp`
browsing, mesh plots, and dashboards. Decide whether to reuse or complement it
rather than duplicating it.

**4. `physics_advisor` (roadmap 6)** needs more than one case in `kb/cases/`.
Consider deferring until there are several.

**Do not** work on the Polaris adapter or flux until those machines are
reachable and their configuration is confirmed.

---

## 8. Commands, environment, external state

### Local

```bash
cd /Users/sku/Documents/Work/AI_project/xgc-automation
uv run python -m tools.server          # MCP server (opencode launches this)
uv run python -c "from tools import ..." # ad-hoc testing
```

Python 3.14 via `uv`. Deps: `mcp`, `pyyaml`, `asyncssh`, `jinja2`.
`asyncssh` is declared but unused — `tools/ssh.py` shells out to `ssh` to reuse
the ControlMaster socket. Consider removing the dependency or migrating.

### ssh

```bash
ssh -O check stellar    # "Master running (pid=...)" means usable
ssh stellar             # human does this; MFA prompt; persists 8h
```

Config lives in `~/.ssh/config` (see `docs/ssh-setup.md`). **Never authenticate
on the human's behalf.**

### stellar

```bash
source /projects/XGC/STELLAR/Software/bin/set_up_xgc.stellar   # required first
cd ~/git/XGC-Devel/build && cmake --build . -j 16 --target xgc
```

The login node's default `python3` is **3.6**, too old for XGC's utils. Use
`/projects/XGC/STELLAR/Software/install/python-venv/bin/python3.12`
(hardcoded as `REMOTE_PYTHON` in `tools/namelist.py`).

| Item | Value |
|---|---|
| XGC source | `/home/seungku/git/XGC-Devel`, branch `fix_comments` @ `39c5c89f` |
| Executable | `build/bin/xgc` (Release) |
| Baseline case | `/home/seungku/tmp/test_ecbc` (backup: `test_ecbc.bak.20260728`) |
| Working case | `/scratch/gpfs/seungku/xgc-runs/ecbc-short` (55 MB, has real output) |
| Account / partition | `pppl` / `all` (96 cores/node, 15-day limit) |
| Toolchain | Intel oneAPI 2024.2, OpenMPI 4.1.6, CMake 3.24.2 |

### State at handoff

- No jobs running. Scratch cleaned of hello-world test dirs; `ecbc-short` kept.
- **ControlMaster has expired.** The human must reopen a session before any
  remote tool works.
- `.cache/metadata-stellar-39c5c89f.json` is gitignored and regenerates on
  demand.

### Secrets

The ohmic API key lives in the human's global opencode config and in a
gitignored `.env`. Neither is in this repo. Do not copy them anywhere.

---

## 9. git status and diff summary

### This repo — clean, nothing uncommitted

```
2b764a9 feat: first real XGC run on stellar; add OpenMP thread control
8c2388b feat: input_builder — build XGC inputs from validated baselines
8f0e137 chore: rebuild stellar XGC on fix_comments, update recorded commit
ae335db fix(ecbc): migrate reference input to current XGC, declare ReducedDeltaF markers
4e9c76c feat: validate XGC input via `xgc --dryrun` before submitting
8a30e41 docs: mark roadmap items 1-4 complete
8a818b2 feat: hpc_runner — submit/status/cancel with human-in-the-loop gate
068752e docs: translate all repo content to English
84e6e51 docs: stellar 빌드 절차 기록  (pre-English-switch)
93c0dbd fix: opencode.json provider 중복 정의 제거   (pre-English-switch)
```

Net: **+3044 / −334** across 27 files. ~2000 lines of that is `tools/`.

The two oldest commit messages are in Korean, from before the English-only
instruction. The human explicitly chose not to rewrite that history.

### XGC clone — `~/Documents/git/XGC-Devel`

Branch `fix_comments`, pushed to the fork, nothing unpushed. `39c5c89f` is the
comment fix; `b23012ac` on top is the human's own commit.

**The stellar clone is checked out on `fix_comments`, not `master`.** Anyone
expecting master will be confused.

---

## Things discussed but not written down elsewhere

- **Local vs remote GUI launch.** Remote (over an ssh tunnel) is correct:
  inputs, mesh, `.bp` output, and the XGC binary all live on stellar. Running
  the GUI locally would lose the Monitor and dryrun entirely, and `.bp` files
  are far too large to copy. Reuse the existing ControlMaster:
  `ssh -O forward -L 5080:127.0.0.1:5080 stellar`.
- **Where the AI actually helps.** The GUI reports *what* is wrong (it flags
  removed parameters). It does not explain *why* or what to replace it with —
  it did not know `ptb_3db_on` also moves to a different namelist, and it could
  not have caught that `sml_deltaf` was inert. That gap is where the agent adds
  value; `vi` remains fine for final review.
- **Attaching an AI directly to XGC-GUI was considered and deferred.** Its
  global `STATE` dict assumes one browser session, so agent writes would mutate
  the human's view mid-review. Revisit only if upstream makes it stateless.
- **`sml_restart=.true.` in the baseline** expects a restart file. It must be
  `.false.` for a fresh run. Not changed in the baseline itself, since that is
  a physics-intent decision for the human.
- **`sml_dt`'s unit bug was a documentation bug, not a code bug.** Worth
  reporting upstream. icpx also leaves reproducer files for its crash under
  `/tmp/icpx-*` if anyone wants to file that with Intel.
