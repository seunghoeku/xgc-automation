# HANDOFF

## 2026-08-05 TCV XGCa example

`kb/cases/tcv-xgca-sheath.yaml` records the Perlmutter
`n641pe_TCV_xgca_gyraze/sheath_m1_3` case as a historical, runtime-tested XGCa
TCV baseline. Job 55052276 completed steps 1-100 and printed `XGC finished!`;
restart job 55053763 reached step 180 before the 30-minute scheduler limit.
The exact runtime input is the regular sheath mode-1/factor-3 comparison, not
the Gyraze surrogate: it omits `sml_sheath_use_gyraze_surrogate`. Do not confuse
it with sibling `gyraze_m1_3` or with the non-identical archive input. Reuse
still requires a current-XGCa dryrun and an explicitly selected executable.
The reusable `case_dir` is the small curated input-only directory
`/pscratch/sd/s/sku/xgc-automation-baselines/tcv-xgca-sheath`; do not point
`stage_case` at the 3.3 GB completed run or it will copy BP output.

## 2026-08-05 access hardening

Recent reduced-adiabatic work established real Perlmutter access and exposed a
ControlMaster edge case also seen on Frontier and Polaris. The automation now
checks both `ssh -O check` and a bounded BatchMode `true` command channel; a
local master process that cannot open commands is a preflight failure rather
than a false success. NERSC `sshproxy` certificate validity is also read from
the effective SSH alias and checked locally.

`machines.yaml` now records the verified Perlmutter identity (`sku`), host,
account `m499`, absolute scratch root, CPU debug defaults, and the shared Slurm
template. The previous config referenced a nonexistent
`templates/perlmutter.sbatch.j2`; that defect is fixed. The campaign-specific
reduced-adiabatic CPU build at commit `8a27511f` is recorded under
`observed_builds`, but `xgc.exe` remains unset so it cannot silently become the
general production binary. Perlmutter Slurm scripts now default to email
notifications `BEGIN,END,FAIL` for `sku@pppl.gov`.

Frontier access (`shku@frontier.olcf.ornl.gov`) and Python 3.14.6 environment
`/lustre/orion/proj-shared/fus183/shku/conda_env/myenv` are documented in
`docs/ssh-setup.md`. Frontier is deliberately not added as a submission target:
no reviewed XGC executable or scheduler resource profile was established in
that session. Preserve the untracked `runs/radia-*` provenance directories.

## Current handoff snapshot — 2026-07-31 (evening, case-promotion session)

Read `AGENTS.md` first. This session built the case-promotion pipeline
(roadmap item 10, now checked), promoted three new validated baselines, and
compiled (but did not test) the polaris `xgca` binary. Everything below is
committed on `main`; the working tree should be clean except optional
`.cache/` files.

### What exists now

| Thing | State |
|---|---|
| `kb/cases/` | **4 validated baselines**: `ecbc` (stellar, core ES delta-f), `st40-heatload` (polaris, edge ES total-f), `asdex-imode` (polaris, edge ES I-mode), `kstar-rmp` (polaris, edge ES RMP). Each passed `xgc --dryrun`; none of the three polaris cases is runtime-tested (stepping) yet. |
| `kb/cases/drafts/` | 16 drafts. The NT/PT torque family (n598/599/606/607/608/609/626/629, all "Used for Paper") is the main promotion backlog; it needs the xgca binary. `draft-n594fr` is inconclusive ("not that good") — do not promote. |
| `tools/case_promotion.py` | Notebook join, outcome classification (negation-aware), binary-era detection (xgc-eem/xgc-es/xgca), name-note mismatch detection, variant tagging, two-phase drafting. MCP tools: `rank_promotion_candidates`, `plan_case_promotion`, `write_draft_case`. |
| `Other_data/simulation_note_XGC1.xlsx` | The researcher's per-run notebook, 2008–2026, joined by simulation number. **Read-only evidence — never edit it.** 139/140 numbered archive files have entries. n645/n646 have no rows yet (user may add them). |
| polaris `xgca` | Built 2026-07-31 (`cmake -DXGCA=ON`, `ninja -j 8 xgca`, commit f805e6223): `/grand/TokamakProfile/shku/XGC-Devel/build/bin/xgca`, 671 MB, recorded as `xgc.xgca_exe` in machines.yaml. **Compiled only — zero testing.** |
| polaris login nodes | Cannot run the CUDA xgc build at all (NVML broken, Kokkos aborts with cudaErrorNoDevice). All dryruns go through 1-node/10-min `debug`-queue jobs (~0.003 node-hour each; pattern in `/grand/TokamakProfile/shku/n620fr_dryrun/dryrun.pbs`). ControlMaster alias is now `polaris4` (login-01 session died). |
| tests | 78 passing (`uv run python -m unittest discover tests`). |

### Known traps recorded this session (do not rediscover them)

1. **Binary eras**: EM runs used the `xgc-eem` binary; their inputs carry no
   field-model switch and the unified binary defaults to electrostatic. Any
   EM-era promotion MUST add `sml_electromagnetic=.true.` or it will silently
   run the wrong physics. All 16 EM-era n500+ inputs lack the switch.
   `classify_code_column`/`plan_case_promotion` warn automatically.
2. **kstar-rmp blocking gap**: the input references `rmp_k030306_n1_q4.bp`
   but only `rmp_k030306_n1.bp` exists in polaris `XGC_inputs/`. The dryrun
   does not read ptb_3db files, so it cannot catch this. Resolve with the
   researcher before any real run.
3. **Marker-type trap** (ecbc pattern, recurs): notebook says delta-f
   ("ES - df") but archived inputs declare no `ptl_marker_type`, so current
   XGC runs TotalF. Recorded in `asdex-imode` notes; decide before production.
4. **Dryrun int32 overflow**: 1-rank dryrun of large cases reports negative
   particle counts and a bogus 0.000 GB total (kstar-rmp did). Issue text
   ready in the conversation log/`kb/cases/kstar-rmp.yaml` note; re-run with
   production `-n_ranks` for real numbers.
5. **n592/n593 double numbering**: both numbers have a d3d AND an ASDEX
   file+note pair (detected by `name_note_mismatch`). Needs researcher
   correction in the notebook or filenames.
6. **Migration table is stable**: the same ~11 removed/deprecated parameters
   (neu_* grid family, sml_f0_grid*, f0_update_analytic_alpha rename,
   sml_special, smooth_rad_efield, sml_poisson_solver_type) cleared all three
   polaris promotions on the first try. Reuse it from any promoted case YAML.

### Recommended continuation order (next session)

1. **xgca smoke test**: debug-queue `xgca --dryrun` on a torque-family input
   (e.g. `input_backup/n606pe_NT_torque_xgca_adjust_omg2_from_604`;
   geometry `d193793_torque_nt_addx.*` — verify presence in XGC_inputs
   first). If it passes, promote the torque family (8 drafts, all
   "Used for Paper").
2. **Runtime test one polaris baseline**: short stepping run (st40-heatload,
   ~16 ranks over 4 nodes, sml_mstep small, sml_restart=.false.) to close
   `runtime_tested: false`. Requires human approval + resource plan.
3. **kstar-rmp RMP file**: ask the researcher about `rmp_k030306_n1_q4.bp`.
4. **data_analyzer PR #51 migration** (below, unchanged from the previous
   snapshot) is still open.

## Previous snapshot — 2026-07-31 (morning)

Read `AGENTS.md` before doing anything. The immediate objective is to finish
the bounded `data_analyzer` integration against XGC-Analysis PR #51 and then
validate the new GCC 14 Polaris build without disturbing the known GCC 13
fallback. No scheduler submission or destructive/overwriting remote action is
authorized by this handoff; show the resource plan and obtain explicit human
approval before `qsub`.

### Repository state

| Repository | Branch / revision | State |
|---|---|---|
| `xgc-automation` | `main`, HEAD `7234691`, five commits ahead of `origin/main` | Five tracked files are modified and not committed: `HANDOFF.md`, `docs/build-polaris.md`, `machines.yaml`, `templates/polaris.pbs.j2`, and `tests/test_hpc_runner.py`. Preserve the two untracked `runs/polaris-compute-dryrun-*` directories. |
| `XGC_reader` | `XGC_analysis`, commit `65e31ae` (`Align analysis catalogs with PR 51`) | Branch matches `origin/XGC_analysis`. Only unrelated user-owned files are untracked; do not add or remove them. |
| `XGC-Devel` | local branch `dadt_prediction`; `upstream/master` is `f805e622` | The checkout contains unrelated untracked files and build trees. Do not switch or clean it. Use a separate Polaris worktree for GCC 14. |

The current `xgc-automation` tracked modifications implement the recorded GCC
14 target and the corrected Polaris rank/GPU mapping. They have not yet been
committed. Review their diff before committing; do not include the untracked
run directories.

### XGC-Analysis PR #51 benchmark and conclusion

PR #51 was tested independently from upstream master, explicitly excluding
the older `codex/catalog-fast-open` branch:

- upstream base: `258a8669b3dace4e0c30e65dd3bb70dde676ee60`
- PR head: `d422276725951396bd05738386ce574ee217fc8d`
- commits: `dfc77b1` (fast directory catalog options) and `d422276`
  (bounded manifest input scans)
- test directory:
  `/grand/TokamakProfile/shku/n645pe_TCV_NT_PT_totalf_ES/NT`
- directory size: 15,984 files, 456 top-level BP entries, 23 logical products

Measured timings on Polaris:

| Operation | Result |
|---|---:|
| master manifest construction | 23.0668 s |
| PR #51 manifest construction | 0.3836 s (about 60x faster) |
| master `open_catalog(collect_metadata=False)` conservative lower bound | 62.8544 s |
| PR #51 default `open_catalog(collect_metadata=False)` | 8.2167 s |
| PR #51 fast `oned`-only open, first / five-run median | 0.4502 s / 0.2098 s |
| metadata-free product-name discovery | 0.018 s first run |
| XGC_reader-equivalent discovery / required-product open | 0.0634 s / 1.0902 s |
| actual two-step `e_gc_density_1d` read, first / median | 0.2382 s / 0.1782 s |

The former delay was overwhelmingly catalog construction, not reading the
requested arrays. PR #51 solves it when callers use `exclude_products` and
`build_manifest=False`; merely updating the package while retaining the
default open path still leaves significant metadata/state work. The one-off
state-scan timings were cache/order sensitive and should not be used as a
standalone performance claim. Sixteen PR catalog/manifest tests passed.

Temporary benchmark material remains at `/tmp/sku-xgc-pr51-019fab9d` on
Polaris and in local worktrees `/private/tmp/xgc-analysis-pr51-base` and
`/private/tmp/xgc-analysis-pr51-head`. Do not delete the remote directory
without approval.

### XGC_reader status

Commit `65e31ae` aligns XGC_reader with PR #51. It performs metadata-free
product discovery, excludes everything outside the Simulation static/required
products and selected diagnostics, and opens with
`open_catalog(exclude_products=..., build_manifest=False)`. It does not depend
on the old `metadata_product_keys` or `track_state` arguments. Thirty tracked
tests pass. A local integration test opened the expected `mesh`, `oned`, and
`volumes` products in 0.317 s; the Polaris-equivalent required-product open
took 1.0902 s.

### Remaining incompatibility in xgc-automation

`tools/data_analyzer.py` still calls the old `codex/catalog-fast-open` API with
`metadata_product_keys` and `track_state`. Those arguments are incompatible
with PR #51. The next code change should:

1. adopt PR #51's metadata-free discovery and
   `exclude_products`/`build_manifest=False` pattern;
2. update its fixture/unit tests;
3. replace stale `codex/catalog-fast-open` references in
   `docs/data-analysis.md` and this handoff's older historical notes;
4. run the full local suite, then verify `oned_profiles` against the real
   Polaris NT directory without transferring raw `.bp` data.

### Polaris GCC 14 transition

Current XGC-Devel upstream master is `f805e622`; the GCC 14 setup was added in
`7c3d278f`. The target environment is:

- `PrgEnv-gnu`, GCC 14, `cudatoolkit-standalone/12.8.1`
- ADIOS2 `/grand/TokamakProfile/hager/Software/install/adios/v2.12.2-gcc14`
- Kokkos `/grand/TokamakProfile/hager/Software/install/kokkos/v4.7.04-gcc14-cuda12.8.1`
- Ginkgo `/grand/TokamakProfile/hager/Software/install/ginkgo/ginkgo-1.11.0-gcc14-cuda12.8.1`
- PETSc `/grand/TokamakProfile/hager/Software/install/petsc/3.24.4-gcc14`
- Python environment
  `/grand/TokamakProfile/hager/Software/install/cray-python-venv-3.11.7`
- CMake uses Kokkos `nvcc_wrapper`; `USE_CABANA=Off`

The modified PBS template follows upstream's Polaris NUMA mapping: local ranks
0, 1, 2, 3 bind to GPUs 3, 2, 1, 0. A matching unit test was added. The full
local xgc-automation suite passes (55 tests), and `git diff --check` passes.

The configured executable in `machines.yaml` deliberately remains the known
GCC 13 build at commit `60bef3bc`:

- source: `/grand/TokamakProfile/shku/XGC-Devel`
- build: `/grand/TokamakProfile/shku/XGC-Devel/build`
- executable: `/grand/TokamakProfile/shku/XGC-Devel/build/bin/xgc`
- compiled/configured: true; compute runtime verified: false
- GPU-aware MPI: false, because this binary is not linked with Cray GTL

The unverified GCC 14 target is recorded separately as:

- source: `/grand/TokamakProfile/shku/XGC-Devel-gcc14`
- build: `/grand/TokamakProfile/shku/XGC-Devel-gcc14/build`
- revision: `f805e622`

The `polaris1` ControlMaster was reported alive, but new command channels timed
out at both 20 and 90 seconds. Consequently, the new `hager` dependency paths,
remote checkout, configure, and build have not been verified, and no remote
files were changed in this phase. The human may need to reconnect/restart the
Polaris authenticated session.

### Recommended continuation order

1. Review and commit the five current tracked `xgc-automation` changes only.
2. Convert `tools/data_analyzer.py`, its tests, and analysis documentation to
   the PR #51 API; run the 55-test suite plus new tests.
3. Re-establish `polaris1`, then read-only verify all GCC 14 dependency paths.
4. Create the separate `XGC-Devel-gcc14` worktree at `f805e622`; configure and
   build following `docs/build-polaris.md`. Do not replace the GCC 13 tree.
5. Check CMake cache/compiler versions, dynamic linkage, absence of `/eagle`
   and `/grad` paths, and run a login-node dryrun.
6. Prepare a minimal compute-node smoke-test plan with node count and walltime;
   obtain explicit approval before `qsub`.
7. Only after a successful smoke test, update `xgc.src`, `xgc.build`,
   `xgc.exe`, and `xgc.env_script` together and mark the target verified.
8. Run the real bounded `oned_profiles` reducer on the NT directory and save
   only bounded JSON/SVG locally.

Run the local suite with:

```sh
env UV_CACHE_DIR=/private/tmp/uv-cache-xgc-auto \
  MPLCONFIGDIR=/private/tmp/mpl-xgc-auto \
  PYTHONPYCACHEPREFIX=/private/tmp/pycache-xgc-auto \
  uv run python -m unittest discover -s tests -p 'test_*.py'
```

Do not reintroduce `/eagle/TokamakITER` or the typo `/grad/TokamakProfile`;
Polaris paths belong under `/grand/TokamakProfile`. Do not enable
`MPICH_GPU_SUPPORT_ENABLED` until the selected executable is demonstrably GTL
linked. Do not clean or commit unrelated user-owned untracked files.

## 2026-07-31 update

XGC-Devel upstream `master` at `f805e622` now contains the GCC 14 Polaris
setup introduced by `7c3d278f`. It replaces the former GCC 13/CUDA 12.6.3
dependency stack with CUDA 12.8.1 and libraries under
`/grand/TokamakProfile/hager/Software/install`. The automation PBS template
now also follows master's Polaris NUMA/GPU mapping: local ranks 0,1,2,3 bind
to GPUs 3,2,1,0.

The configured executable remains the previously compiled GCC 13 binary at
commit `60bef3bc`. The GCC 14 target is recorded in `machines.yaml` but is
deliberately marked unverified. Build it in the separate
`/grand/TokamakProfile/shku/XGC-Devel-gcc14` worktree, run a compute-node smoke
test, and only then move `xgc.src`, `xgc.build`, `xgc.exe`, and `xgc.env_script`
together. Do not treat merely sourcing the new master setup as validation of
the old executable.

## 2026-07-29 update

The Polaris adapter now has a real PBS template and local tests. Its configured
paths use `/grand/TokamakProfile`, not the retired `/eagle/TokamakITER` tree.
The four-node prototype profile uses `debug-scaling`, four MPI ranks and four
GPUs per node, 16 OpenMP threads per rank, and `filesystems=home:grand`.
Planning rejects missing PBS account/queue/filesystem/system resources, and
terminal `qstat` output uses `Exit_status` to distinguish success from failure.
The Polaris `xgc` target compiled successfully on 2026-07-30 from commit
`60bef3bc`; `build/bin/xgc` is executable and all dynamic libraries resolve.
`machines.yaml` now records that path. PBS job 7301673 then completed a live
1-node `qsub`/`qstat`/output round trip with `Exit_status=0` and four MPI ranks
on `x3005c0s7b0n0`. Live `qdel` and XGC compute-node runtime remain unverified.
The Polaris ControlMaster was alive during compilation and testing. Do not
submit further jobs without the normal human approval gate.

A first compute-node dryrun attempt (job 7302090) exposed a template bug:
`MPICH_GPU_SUPPORT_ENABLED=1` was forced even though the executable is not
linked with Cray GTL. It failed before XGC input validation with exit 255.
The template now enables GPU-aware MPI only when `xgc.gpu_aware_mpi` is true;
Polaris is explicitly false, matching XGC's own environment script. A retry
still requires the normal reviewed resource plan and approval.

`data_analyzer` now includes the first scientific product, `oned_profiles`.
It targets XGC-Analysis `codex/catalog-fast-open` (`252c3ea`) and the combined
reader direction validated by XGC_reader `XGC_analysis` (`43b4122`). The
remote reducer restricts catalog metadata to `xgc.oneddiag.bp`, disables state
tracking, reads the first/latest logical steps of selected density,
perpendicular-temperature, and parallel-flow channels, and downsamples the
radial grid. Only bounded JSON returns; an SVG is rendered locally. Configure
`analysis.pythonpath` in `machines.yaml` when XGC-Analysis is not installed in
the machine's default XGC environment. Fixture tests cover the boundary, but
stellar's expired ControlMaster prevented a real-run check.

Roadmap item 6, `physics_advisor`, now has a conservative MVP in
`tools/physics_advisor.py` and is exposed by `tools/server.py`. It matches a
natural-language request against evidence and exclusions stored in
`kb/cases/*.yaml`, proposes only explicitly stated values covered by
deterministic rules, and never invents numerical values. Because ECBC remains
the only validated case, electromagnetic, edge-SOL, ELM, and collisional
requests return `unsupported`. English and Korean matching are covered by
tests in `tests/test_physics_advisor.py`.

Global simulation IDs are implemented in `tools/simulation_registry.py`,
backed by `kb/simulation_registry.yaml`. Historical numbering is recorded
through 644, so the next reservation is 645. IDs are global rather than
per-machine; historical duplicates are treated as mistakes. The reservation
gate requires confirmation plus the exact planned number, and
`stage_simulation_case` derives the remote directory from the reserved ID.
`simulation_id` is permanent while timestamp `run_id` remains one submission
attempt. See `docs/simulation-ids.md`.

The user copied 427 historical input files into `input_backup/`. Treat this
directory as read-only. `tools/input_archive.py` and the MCP
`inspect_input_archive` prioritize `n600+` files, hash them, extract a bounded
parameter summary, and compare parameter names with the newest local metadata
cache. The initial recent set contains 17 files across 15 numbers. The ST40
family n618/n620/n621/n624/n625 is the best first migration target; no archived
input is a KB baseline until its companion files are found and current XGC
dryrun succeeds. See `docs/input-archive.md`.

The next roadmap implementation item is `data_analyzer`. Before designing it,
inspect the current XGC GUI Monitor implementation and use a completed run (or
a deliberately small captured fixture) to define the first remote summary
product. Never pull raw `.bp` output wholesale.

`data_analyzer` now has its first bounded product in
`tools/data_analyzer.py`: a remote top-level run inventory saved locally as
JSON. It records `.bp` product names but never enters or transfers them.
`docs/data-analysis.md` records how this complements the XGC GUI Monitor and
why future scientific reducers should reuse `xgc_analysis`. Stellar's
ControlMaster had expired during implementation, so the reducer is covered by
fixtures but has not yet been exercised against the real ECBC run directory.

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

**Roadmap 6 and the foundations of 7–8 are implemented.** Scientific analysis
still needs a live HPC verification, the Polaris adapter still needs its first
approved PBS round trip, and provenance hardening remains.

**PBS support is locally tested, not live-tested.** Script rendering and
qstat parsing have fixtures, but no scheduler command has been issued through
the automation.

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
