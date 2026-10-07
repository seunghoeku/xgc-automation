# Repository roles: XGC-Devel vs xgc-automation

Two repositories take part in every XGC study. They have different owners,
different audiences, and different rules, so a change must land in the one
whose job it is. This file is the rule of thumb; the rest of `docs/` covers
xgc-automation's side in detail.

| | XGC-Devel | xgc-automation |
|---|---|---|
| What it is | The XGC source code (Fortran/C++/Kokkos), shared with the whole XGC team | Simulation operations for this user: inputs, submissions, records, analysis |
| Local path | `~/Documents/git/XGC-Devel` (worktree `XGC-Devel-dadt` on branch `dadt_prediction`) | `~/Documents/Work/AI_project/xgc-automation` |
| Rules loaded from | its own `AGENTS.md` (coding rules) and `CLAUDE.local.md` (local build notes, not committed) | `AGENTS.md` here plus `docs/` |
| Commit audience | upstream reviewers; every commit may be merged into the shared code | this user only |

## What goes where

**XGC-Devel: the code itself.**
- Physics, numerics, and infrastructure changes to `xgc` / `xgca`
  (algorithms, Kokkos kernels, I/O, diagnostics, namelist parsing).
- New or changed input parameters, including their defaults, the
  documentation strings and `docs/` inside XGC-Devel.
- Bug fixes discovered while running a simulation. The run that exposed the
  bug is recorded in xgc-automation; the fix lives in XGC-Devel.
- Build system (`CMakeLists.txt`, platform cmake files) and unit tests.
- Commits follow XGC-Devel's `AGENTS.md`. Never commit personal workflow
  notes, machine paths, or session logs there; `CLAUDE.local.md` is the
  place for local-only notes and is excluded from git.

**xgc-automation: everything about running the code and remembering what ran.**
Since 2026-10-07 this side is itself two repositories: xgc-automation (shared
code, documentation, machine facts, promoted cases, templates) and the user's
**catalog repository** (`xgc-catalog`), which holds the state the tools write
and is found through `XGC_CATALOG_DIR` / `~/.config/xgc-automation/config.yaml`
(`docs/simulation-catalog.md`, "Catalog repository"). Record and provenance
commits go to the catalog repository.
- Simulation inputs: cases (`kb/cases/`), templates (`templates/`), input
  files written for a run; `input_backup/` and `f_init/cases/` in the catalog
  repository.
- Simulation identity and records (catalog repository): `simulation_id`
  reservation (`kb/simulation_registry.yaml`), catalog records
  (`kb/simulations/`), the derived CSV notebook, provenance (`runs/`).
- Job submission, status, cancellation, remote directories, transfers.
- Which binary runs where: `machines.yaml` and the local overlay
  `machines.local.yaml` (`<machine>.xgc.exe`, `.commit`, `.build`).
- Output handling: remote reduction, analysis scripts and notebooks under
  `runs/<id>/analysis/`, summaries, plots, and the notes written back into
  the catalog record.
- Per-machine build procedures and traps (`docs/build-<machine>.md`), even
  though the build system itself belongs to XGC-Devel.
- Documentation of *how a study was run*, in `docs/` and `kb/`.

## Handoffs between the two

1. **Code change → run.** After a change in XGC-Devel is built on a machine,
   update `machines.local.yaml` (`xgc.exe`, `commit`, `build`) in
   xgc-automation before the next submission so the catalog records the
   right binary. A dev-series id (`d000Npe`, `kind="dev"`) is for verifying
   the change; a production id is for the physics result.
2. **Run → code change.** When a run fails or misbehaves because of the code,
   record the run (status, reason, job id) in its catalog note first, then
   switch to XGC-Devel for the fix. Reference the simulation id in the
   XGC-Devel commit message so the two histories can be joined later.
3. **New parameter.** Add and document it in XGC-Devel; then teach
   xgc-automation about it (case files, templates, `describe_parameter`
   sources) as a separate change in this repository.
4. **Never mirror.** Do not copy XGC source into xgc-automation, and do not
   store run records, inputs for real runs, or analysis outputs inside
   XGC-Devel.

## Working in one session

- Open the session in xgc-automation. XGC-Devel is attached as an
  additional working directory through `.claude/settings.local.json`
  (`permissions.additionalDirectories`), which is gitignored because the
  path is machine-specific.
- Before editing XGC source, read `XGC-Devel/AGENTS.md`; it is not loaded
  automatically from the additional directory. Read `CLAUDE.local.md` there
  as well when building locally.
- Commit from inside the repository being changed (`git -C <repo> ...`).
  Never commit both repositories in one step, and check `git status` in the
  intended repository before committing.
- The `xgc-tools` MCP server and the `tools/` package resolve their root
  from their own file location, so they always act on xgc-automation
  regardless of the session's working directory.
