# Roadmap and current status

Moved out of `AGENTS.md` so that the startup context holds rules, not history.
Item numbers are stable and are referenced from commit messages and from
retired handoff notes in git history; do not renumber them.

Governing rules live in `AGENTS.md`. This file records what has been built,
what was actually verified, and what is still open.

## Items

- [x] 1. `preflight` tool — check connectivity and authentication state.
- [x] 2. Finalize `machines.yaml` (stellar and Polaris done; Perlmutter
      access/account/scratch/Slurm settings are verified, but its general
      production `xgc.exe` remains deliberately unset; flux is unconfirmed).
- [x] 3. `hpc_runner` MCP — submit/status/cancel/output (slurm+pbs).
      push/pull exist in `tools/ssh.py` but are not yet exposed as MCP tools.
- [x] 4. Successful hello-world sbatch round trip — stellar, jobs
      2888408/2888413, exit 0:0. Polaris PBS also passed live. Perlmutter CPU
      XGC build, dryrun, Slurm jobs, restarts, and reduced output analysis were
      exercised in the reduced-adiabatic campaign; a canonical automation
      hello-world/provenance round trip is still useful after selecting a
      production executable.
- [x] 5. `input_builder` + `validator` — `validator` via `xgc --dryrun` on the
      login node (`tools/dryrun.py`), wired in as a pre-submit gate.
      `input_builder` builds from validated baselines in `kb/cases/`, reusing
      XGC's own `utils/input_gui/` parser and metadata. See `docs/workflow.md`.
- [x] 6. `physics_advisor` (KB + deterministic rules). Conservative MVP:
      selects only validated KB cases, carries through explicitly stated
      step/restart values, and rejects unsupported physics rather than
      inventing parameters. ECBC was the first baseline; KB now holds five
      (see item 10).
- [ ] 7. `data_analyzer` — bounded inventory plus an XGC-Analysis-backed
      `oned_profiles` reducer are implemented. The latter selects only the
      first/latest logical steps remotely and saves bounded JSON plus a local
      SVG without transferring `.bp`. Fixture tests pass; a real HPC run still
      needs to verify the remote analysis environment before this item closes.
      See `docs/data-analysis.md`.
- [x] 8. Polaris adapter (pbs/qsub) — PBS template, local tests, and a live
      1-node qsub/qstat/output round trip passed with exit 0 (job 7301673).
      Live qdel was not exercised; the XGC executable is compiled but not
      runtime-tested. See `docs/build-polaris.md`.
- [x] 9. Strengthen provenance — the simulation catalog
      (`tools/simulation_catalog.py`) gives every catalog item one durable
      record under `kb/simulations/`, created at reservation and updated
      automatically by staging, submission, and status checks, so a number is
      always linked to its runs, provenance files, input hash, and archive
      location. Submission also stores the submitted namelist and its SHA-256
      in the run record, parses `sml_node_file`, `sml_electron_on`,
      `eq_filename`, and `ptl_marker_type` with XGC's namelist parser, and
      keeps effective field model, marker model, and species summaries unknown
      when historical evidence is insufficient. Catalog identity is the pair
      `simulation_tag + sub_tag`; the latter defaults to the directory
      basename, so reused tags such as `n645pe` remain distinct when their
      directories differ. `tools/legacy_import.py` imports the historical
      notebook (741 of 742 rows) as `origin: notebook` evidence records,
      `tools/run_adoption.py` folds the 35 pre-catalog provenance runs under
      `runs/` into 25 `origin: adopted` simulations grouped by working
      directory (no retroactive numbering, no invented outcomes), and
      `tools/simcat_cli.py` (`xgc-sim`) searches all of it with no LLM
      involved. `tools/sheet_sync.py` mirrors the catalog to Google Sheets,
      pushing machine-known fields and pulling back only the human-owned
      `note` column (optional extra: `uv sync --extra sheets`).
      See `docs/simulation-catalog.md`.
      Open: a live Google Sheets round trip still needs a real service
      account, and HPSS archiving is recorded but not driven. Team-sharing UI
      remains later work.
- [x] 10. Case promotion pipeline (`tools/case_promotion.py`) — joins
      `input_backup/` with the researcher's simulation notebook
      (`Other_data/simulation_note_XGC1.xlsx`), classifies outcomes from the
      notebook's own wording (negation-aware), detects binary eras
      (xgc-eem/xgc-es/xgca) and name-note mismatches, and drafts kb cases
      under `kb/cases/drafts/`. Three baselines were promoted and
      dryrun-validated on polaris via debug-queue jobs: st40-heatload
      (n620fr), asdex-imode (n566pe), kstar-rmp (n613fr). Together with ECBC
      and the historical TCV XGCa baseline below, KB now has 5 cases.
      Open: the torque family needs the freshly built (untested) xgca binary;
      kstar-rmp needs its missing RMP field file before a real run.
      A fifth historical baseline, tcv-xgca-sheath (n641pe/sheath_m1_3), is
      runtime-validated on Perlmutter with XGCa. It is the regular-sheath
      reference, not the sibling Gyraze-surrogate variant; a current-master
      dryrun remains required before reuse. See `docs/input-archive.md`.
- [x] 11. Remote XGC_GUI launcher (`tools/remote_gui.py`) — the
      `xgc-remote-gui` CLI and matching MCP tools resolve explicit paths or
      local provenance, validate the remote GUI/Python environment, load an
      optional `.xgcg` preset, start a loopback-only login-node Monitor and SSH
      tunnel, open the local browser, and provide status/stop lifecycle tools.
      Perlmutter uses sshproxy key connections with tool-owned multiplexing;
      no scheduler job
      or BP transfer is involved. A live Perlmutter plan/open/status/stop round
      trip passed against the TCV reduced-adiabatic run on 2026-08-07.
      See `docs/remote-gui.md`.
- [x] 12. Unattended queue monitoring and SwiftBar distribution —
      `xgc-queue-watch` queries Slurm/PBS user queues, preserves recent observed
      departures, displays scheduler start estimates, and renders a cache-only
      menu with prioritized job state and separate connection warnings.
      The standalone [HPC Queue Monitor](https://github.com/seunghoeku/hpc-queue-monitor)
      adds public server presets, `servers.yaml`, isolated history, and a
      per-user timer/plugin installer. Its initial source is exported from
      this repository; ongoing package development is in the independent
      public repository. Local isolated installation, 31 standalone tests,
      and wheel/source builds passed. GitHub Actions passed on macOS/Linux
      with Python 3.10/3.12, including the distribution build. Open: verify
      real SSH and SwiftBar on a second Mac;
      no PyPI release has been published. See `docs/queue-watch.md`.

- [ ] 13. Multi-machine checkouts and the multi-user split — the repository
      mixes shared code (`tools/`, `docs/`, `machines.yaml`, build docs,
      `kb/cases/`) with one person's mutable state (`kb/simulations/`, the
      registry, `runs/`, `input_backup/`, flow_match/f_init case logs), and
      that state is now written from two checkouts (the Mac and stellar-ai,
      `~/git/xgc-automation`, bound with `auth.method: local` since
      2026-10-07; flux is secondary). Plan, in order:
      1. ~~Make `kb/simulation_notebook.csv` a derived, untracked file and
         protect notes against stale copies~~ (done 2026-10-07).
      2. One setting (`XGC_CATALOG_DIR` or `~/.config/xgc-automation/`) for
         every state path that `PROJECT_ROOT` resolves today (15 modules);
         default stays the current layout.
      3. Move the state into a per-user catalog repository
         (`xgc-catalog-<user>`), history kept with `git filter-repo`; the
         user's `USER.md` (accounts, allocations, preferences) lives there
         and `AGENTS.md` imports it; shared repo ships `templates/USER.md.example`.
      4. Catalog tools pull before writing, commit and push after; the only
         contested writes are the registry counter (retry once on a rejected
         push) since the CSV is gone.
      5. Shot/experimental data and summary PNG/npz leave git: a data
         directory plus index YAML per shot (file hash, diagnostic, columns
         and units, which simulations used it) in the catalog repository.
      Keep YAML records as the source of truth; a SQLite index is a derived
      view for cross-catalog queries, never the master (binary diff, no merge).
      Personal paths still inside shared files (`pscratch/sd/s/sku` in
      docs/campaign.md, build-stellarai docs, f_init cases) become templated.

## Open questions

1. **ohmic.pppl.gov API spec** — OpenAI-compatible? Authentication
   (key/SSO/IP)? Is a VPN required locally?
2. XGC version (XGC1 vs XGCa), primary physics interest (core turbulence /
   edge-SOL / ELM).
3. ~~Do we have past successful inputs/geqdsk files for the physics_advisor
   KB?~~ Answered: yes — `input_backup/` plus the simulation notebook feed the
   case promotion pipeline (item 10); five baselines promoted.
4. Should we also use the Superfacility API for job submission?
5. Output data volume and scratch cleanup policy.
