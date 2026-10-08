# The simulation catalog

One durable record per scientific simulation, holding what it was, what ran,
how it ended, and where the output went.

## Why it exists

The manual system worked like this: pick the next number, make a directory
named after it, type the provenance into a spreadsheet, and archive the output
to HPSS under the same number. Every step was sound. The problem was that the
number, the spreadsheet row, the run directory, and the scheduler job were
connected only inside the researcher's head, and only for as long as it was
remembered.

The catalog keeps the number and automates the bookkeeping. Reserving a number
opens a record; submitting a job links itself to it; checking on that job marks
it completed or failed. The parts a machine can determine, a machine
determines. What is left for a human is judgement: the note, and approving
anything that writes. The agent keeps its own running summary in a separate
field, `ai_note`, so its observations never overwrite the human's.

Nothing here needs an LLM. The `xgc-sim` command is the whole system with a
keyboard; the MCP tools call the same functions.

## Where it lives

```text
kb/simulations/<record_id>.yaml     one file per catalog item
```

One file per catalog item, rather than one index, so that git history shows what
changed about a given item instead of a single perpetually-conflicting
file. The directory follows the state root chosen by `XGC_CATALOG_DIR` or
`~/.config/xgc-automation/config.yaml` (`tools/paths.py`, `xgc-sim paths`);
`XGC_SIMULATION_CATALOG` still overrides this one directory.

The user-visible identity is the pair `simulation_tag + sub_tag`. Both values
must match for two rows to be the same item. `simulation_tag` is the familiar
shot/campaign label such as `n645pe`; `sub_tag` distinguishes directories or
variants that reuse that label. When the caller does not supply a `sub_tag`, it
defaults to the final component of `directory_name`.

`record_id` remains the unique storage key and CLI handle. For the first item it
normally stays short (`n645pe`). If another item has the same simulation tag but
a different sub-tag, its storage key is qualified, for example
`n645pe__nt1_psi_1_0`. Existing record IDs therefore remain valid.

For simulations this toolchain runs, the base record ID is the
`simulation_id` from `kb/simulation_registry.yaml` (`n645pe`, see
`docs/simulation-ids.md`). For imported historical runs it is the notebook's
shot name (`ti204`, `j86`). For adopted pre-catalog runs it is the working
directory name (`tcv_xgca_reduce_adia_k1`).

> The key is the ID string, never the bare number. Historical numbers are not
> unique: `ti204` and `su204` are different simulations, and roughly a hundred
> numbers repeat across the pre-2023 prefixes. Only the modern
> `n<NUMBER><MACHINE>` scheme guarantees a global number.

## What a record holds

```yaml
schema_version: 2
record_id: n645pe
simulation_id: n645pe        # null for historical shots
simulation_tag: n645pe       # grouping label; may be shared by several items
sub_tag: n645pe_tcv_delta_f_linear  # defaults to the directory basename
number: 645
kind: production             # or dev: a d-numbered test/debug run
short_name: tcv_delta_f_linear
machines: [perlmutter]
status: completed
origin: automation           # notebook (imported) or adopted (pre-catalog runs)
case: ecbc                   # the kb baseline it started from
directory_name: n645pe_tcv_delta_f_linear
tags: [tcv, delta-f]
remote_dirs:
  perlmutter: /pscratch/sd/s/sku/xgc-runs/n645pe_tcv_delta_f_linear
input_sha256: 6f1c...        # of the namelist actually submitted
input:
  parse_status: ok
  parameters:
    sml_node_file: tcv.node
    sml_electron_on: true
    eq_filename: tcv.eqd
    ptl_marker_type: [ReducedDeltaF, ReducedDeltaF]
  derived:
    field_model: electromagnetic  # explicit switch or current xgc --dryrun
    field_model_source: sml_electromagnetic
    marker_model: reduced-delta-f
    species: [e, i]
runs:
  - run_id: run-20260830T101010Z
    machine: perlmutter
    job_id: "12345"
    submitted_at: 2026-08-30T10:10:10+00:00
    provenance: runs/master/run-20260830T101010Z/provenance.json
    remote_run_dir: /pscratch/.../n645pe_tcv_delta_f_linear
    state: completed
    exit_code: 0
    build:                     # from the XGC startup banner in this run's stdout
      commit: b84b1bdd0fa24d2012c650df7635c98d09da92c5
      commit_subject: Merge remote-tracking branch '...'
      build_date: Sun Aug 30 12:42:31 PM PDT 2026
      build_type: RelWithDebInfo
      cxx_compiler: GNU 14.3.0
      built_by: sku
      working_tree: clean      # or dirty / no-git
      origin_url: git@github.com:seunghoeku/XGC-Devel.git
      source_file: slurm-57784606.out
archive:
  system: hpss
  path: /home/s/sku/n645pe.tar
  archived_at: 2026-09-02T18:00:00+00:00
ai_note: 1st job timed out at gstep 1200; restart submitted as 57785001
note: growth rate 1.2e5 /s, matches the GENE benchmark
created_at: ...
updated_at: ...
```

`runs` is the link that used to be missing. A simulation has one number and one
directory but many submission attempts, each with its own timestamp `run_id`
and its own provenance record; the catalog holds them together.

### Origins

`origin` says how much a record can be trusted to claim, and the three kinds
are deliberately not equal:

| `origin` | Source | Has verified provenance? |
|---|---|---|
| `automation` | reserved and run through these tools | yes: runs, input hash, dryrun |
| `adopted` | provenance under `runs/` predating the catalog | runs and job IDs, but no recorded outcome |
| `notebook` | the researcher's xlsx | no: evidence only |

Only `automation` records carry a reserved sequence number. The other two have
`number: null` unless their original name happened to contain one.

### Kinds

`kind` says what the simulation is for, independently of how trustworthy the
record is:

| `kind` | Id series | Meaning |
|---|---|---|
| `production` | `n645pe` | a physics simulation; its results are studied |
| `dev` | `d0001pe` | a short test, debug, or algorithm-verification run |

The two series number independently (`docs/simulation-ids.md`), so listings
sort production first and dev after, never interleaved by number. A record
without the field is production; every record predating the split was one.
The exception is n662pe through n670pe, g2 verification runs that consumed
production numbers before the dev series existed and are marked `kind: dev`
by hand. `xgc-sim list --kind dev` and the `kind` argument of
`search_simulations` filter on it.

### Status

```text
reserved -> staged -> submitted -> running -> completed -> analyzed -> archived
                                       ^   \-> interrupted             abandoned
                                       |         |
                                       +-- restart
                                           \-> failed
```

Most transitions are automatic. `analyzed`, `abandoned`, and any correction are
human calls, made with `xgc-sim status`.

**A simulation's status is derived from all of its runs, not from whichever one
was polled last.** One scientific effort routinely spans several scheduler
jobs: run out of wall clock, restart from the checkpoint, repeat. The status
comes from the newest run that reported an outcome, so a failure early in a
chain that was later restarted and finished is history rather than the current
state. A newer run that is still queued (resubmission after a failure, restart
after a timeout) puts the simulation back to `submitted`: an effort with a job
in the queue is in flight, not failed.

`interrupted` means the last job hit the wall clock. That is not a failure:
XGC runs are routinely sized to be stopped by the scheduler and resumed from a
checkpoint, so `interrupted` reads as "restart to continue". Only a genuine
error -- nonzero exit, node failure, out of memory -- is `failed`.

Three rules keep automation from overwriting judgement:

* A finished job never drags an `analyzed`, `archived`, or `abandoned`
  simulation backwards, and neither does submitting a restart.
* A cancelled job updates the run but not the simulation. Cancelling one
  attempt says nothing about whether the science was abandoned.
* A job the scheduler has forgotten leaves the run untouched. Recording
  `unknown` would replace real information with an absence.

## What updates it, and when

| Moment | Tool | Effect on the record |
|---|---|---|
| Number reserved | `reserve_simulation_id` | creates it, `reserved` |
| Baseline staged | `stage_simulation_case` | records the directory and case, `staged` |
| Job submitted | `submit_job` | appends the run, stores the input hash, `submitted` |
| Job checked | `job_status` | records that job's state and re-derives the status |
| Catching up | `xgc-sim refresh` | polls every in-flight run at once |
| Output archived | `xgc-sim archive` | records the location, `archived` |
| Anytime | `xgc-sim note` | the human note |
| Anytime | `set_ai_note` / `xgc-sim ai-note` | the agent's note (no approval needed) |

Submission also copies the exact input namelist into the provenance record and
stores its SHA-256. XGC's namelist parser also extracts `sml_node_file`,
`sml_electron_on`, `eq_filename`, and `ptl_marker_type`. Provenance derives the
effective field model, marker model (`reduced-delta-f`, `total-f`, or `full-f`),
and species (`e, i` or ion-only) only when supported by explicit input or the
current run's dryrun. Older or ambiguous inputs remain `unknown`; the tool does
not retrofit modern defaults onto historical executables. Without the exact
snapshot a record can say which job ran but not what it ran, since the remote
case directory is routinely edited for the next attempt.

Both the submission link and the status update are best-effort: a catalog
problem is reported in the result, never raised. A queued job must not look
like a failed submission because a YAML file could not be written.

## Keeping the catalog current

`job_status` only updates the job you asked about, so a simulation submitted
and then left alone stays `submitted` indefinitely. `refresh` inverts that: it
finds every simulation still in flight, asks each machine about the runs
involved, and writes the answers back.

```bash
xgc-sim refresh               # the whole catalog
xgc-sim refresh --dry-run     # what would be polled; contacts nothing
xgc-sim refresh --machine perlmutter
xgc-sim refresh --record-id n645pe
```

It takes no confirmation, because it only writes down what the scheduler
already decided; there is nothing for a human to approve. It polls and never
submits, cancels, or touches remote files, so it is safe in a shell alias or a
cron entry.

Runs that already reported a terminal state are not polled again, which keeps
the cost proportional to work in flight rather than to catalog size. An
unreachable machine is reported per-run and does not stop the rest, and the
command exits non-zero so a script notices rather than assuming it is current.

A practical habit:

```bash
xgc-sim refresh && xgc-sim list --status running
```

## Finding past work

```bash
xgc-sim search "asdex i-mode"          # any text field, including the note
xgc-sim list --machine perlmutter --status failed --after 2025-01-01
xgc-sim list --tag electromagnetic --origin notebook
xgc-sim list --simulation-tag n645pe --sub-tag n645pe_tcv_delta_f_linear
xgc-sim list --origin adopted          # pre-catalog runs, newest work first
xgc-sim show n645pe                    # full record, runs, provenance paths
xgc-sim stats                          # counts by status, machine, origin
```

Add `--json` to any command for scripting. The MCP equivalents are
`search_simulations`, `show_simulation`, and `simulation_stats`.

A run's `build` block is captured automatically from the XGC startup banner
in its stdout on the first status check or refresh after the job starts
(`docs/workflow.md`, "Build banner capture"), so `xgc-sim search <commit>`
answers "which simulations ran this exact build". The full banner, including
XGC_FLAGS and a dirty build's changed-file list, is in the run's
`provenance.json` under `xgc.build`.

All query terms must match, but separators do not have to. The three sources
write names differently -- `ASDEX_I_mode_EM`, `asdex-imode`, `ASDEX I mode` --
so query and record are both reduced to words before matching. Any of
`asdex i-mode`, `ASDEX I mode`, or `asdex_i_mode` finds the same simulations.

Search covers imported history and adopted runs too, which is the point: "what
did we already try for ASDEX I-mode?" is one command, not an afternoon in a
spreadsheet.

## Adopting runs that predate the catalog

Runs submitted before the catalog existed have provenance under `runs/` but no
catalog record, so they are invisible to search. That is the work most worth
finding: it is recent, and the directories are usually still on disk.

```bash
xgc-sim adopt-runs            # plan only; writes nothing
xgc-sim adopt-runs --confirm
```

Runs are grouped into simulations **by working directory**, because a restart
reuses the directory and takes a new `run_id`. Grouping any other way would
split a campaign across records or merge unrelated work. A trailing `run/`
subdirectory is ignored, so `.../k1` and `.../k1/run` are one simulation.

The key is the directory name. Bare variant labels are qualified with their
campaign directory, since `k1` alone identifies nothing:

```text
/pscratch/.../codex_work/tcv_xgca_reduce_adia_k1   ->  tcv_xgca_reduce_adia_k1
/grand/.../dev_reduce_adia/k1                      ->  dev_reduce_adia_k1
```

What adoption will not do:

* **Assign a sequence number.** Numbering retroactively is irreversible, and
  the registry sequence is for simulations a human chose to number. Adopted
  records have `number: null` and `adoption.numbered: false`. If a run deserves
  a number, reserve one deliberately.
* **Invent an outcome.** Provenance records a submission, not a result. These
  runs predate the status write-back, so unless a state was recorded they are
  left at `submitted`. Correct them with `xgc-sim status`.
* **Overwrite a registered simulation.** A key already owned by an
  `automation` record is skipped and reported.

Scheduler smoke tests are excluded unless you pass `--include-smoke-tests`.
Rerunning is safe; `--overwrite` picks up runs added since.

## Adopting a run submitted by hand

A run submitted with a hand-written `qsub` in some other chat left no
provenance under `runs/` at all, so `adopt-runs` cannot see it. What remains is
the remote directory and the scheduler's memory of its jobs, and that is enough:

```bash
xgc-sim adopt-remote polaris /grand/TokamakProfile/shku/n648po_dAdt_ecbc            # plan only
xgc-sim adopt-remote polaris /grand/TokamakProfile/shku/n648po_dAdt_ecbc --confirm \
    --tag dadt --case dadt-ecbc-em --note "..."
```

(MCP: `adopt_remote_run`.) In one pass it reads the `input`, finds every job
from the stdout files left in the directory (`xgc.out.<id>`, `<name>.o<id>`,
`slurm-<id>.out`, one level of subdirectories included), asks `qstat -x -f` or
`sacct` about each, parses the XGC startup banner from each job's stdout, keeps
the job script, and hashes the referenced input files and build environment
exactly as a tool-driven submission would. It writes:

* `kb/simulations/<directory_name lowercased>.yaml`, `origin: adopted`,
  `number: null`, status derived from the newest run's scheduler state;
* `input_backup/<directory_name>` (or a dated variant if that name already
  holds a different input);
* `runs/adopted/<record_id>/` with the job script and `banner_<jobid>.json`.

Job states follow the scheduler: exit 0 is `completed`, a PBS walltime kill
(-29) is `timeout`, a SIGTERM exit (143) is `cancelled`, anything else nonzero
is `failed`. Whether the science is finished remains the human's call
(`xgc-sim status`). Scheduler timestamps are recorded as the server reports
them, without a time zone.

Rerunning on the same directory needs `--overwrite` and keeps the existing
note; a directory whose name is already an `automation` record is refused.

## Importing the historical notebook

`Other_data/simulation_note_XGC1.xlsx` is the researcher's own document, one
sheet per year, one row per shot. Importing it makes that corpus searchable
alongside current work.

```bash
xgc-sim import-notebook            # plan only; writes nothing
xgc-sim import-notebook --confirm
```

The notebook is never written to. Imported records carry `origin: notebook` and
keep the researcher's wording verbatim under `legacy`, including the phrases
that produced the outcome classification. The import claims nothing it cannot
support:

* No input hash, commit, or run is invented. A notebook row is evidence, not
  provenance.
* A machine is recorded only when the shot name says so, with `machine_basis`
  naming the inference. The ambiguous pre-2013 prefixes (`pf`, `j`, `f`, `hp`,
  `ew`, ...) get no machine at all: no machine is better than the wrong one.
* `data: hpss` marks the record archived but leaves `archive.path` null. The
  notebook says it went to tape, not where.
* A notebook row never overwrites an `origin: automation` record.

The import is idempotent; rerun with `--overwrite` to refresh.

Of 742 rows, 741 import. The one skip is an annotation row. Three rows that used
to be dropped because their cleaned shot tag repeated (`th337_2`, `j46s`, and
`f136-f139`) now survive under distinct sub-tags.

## Catalog repository

Since 2026-10-07 the state is not in this repository. `kb/simulations/`,
`kb/simulation_registry.yaml`, `kb/bad_nodes.yaml`, `kb/cases/drafts/`,
`runs/`, `input_backup/`, `f_init/cases/`, `flow_match/cases/` and
`Other_data/` were extracted with `git filter-repo` (history kept) into a
per-user **catalog repository** (`xgc-catalog` for the original author), and
the same paths are gitignored here so a default-layout checkout never commits
them. The tools find the catalog through `XGC_CATALOG_DIR` or
`catalog_dir` in `~/.config/xgc-automation/config.yaml` (`tools/paths.py`,
`xgc-sim paths`); `machines.local.yaml` stays in each checkout because it
differs per machine.

Each user has their own catalog repository, and with it their own
registry and numbering; two catalogs may both hold an `n001pe` and nothing
merges them. A group that wants one repository gives each user a
subdirectory (`catalog_dir: .../users/<name>`), which has the same effect.
Another user's catalog is read with `xgc-sim --catalog-dir <clone>/kb/simulations`
and never pointed to as one's own state. Setup for a new user:
`docs/setup-new-user.md`, section 4a.

The catalog repository also holds `USER.md`, the person's accounts,
allocations, checkouts and preferences, symlinked to
`~/.config/xgc-automation/USER.md` and imported by `AGENTS.md`;
`templates/USER.md.example` is the blank. Commits that change records,
provenance or input backups are made in the catalog repository; this one
only carries code, documentation, machine facts and promoted cases.

The tools keep the checkouts in step themselves (`tools/catalog_sync.py`):
every write entry point, the MCP tools that change records and the `xgc-sim`
commands that do, runs `git pull --rebase` on the catalog first and commits
and pushes afterwards with a subject such as `reserve n695pe`, `submit n695pe
on perlmutter`, `status perlmutter 59018634` or `ai_note n674po`. The outcome
is attached to the tool result under `catalog_sync` and printed by the CLI.
A sync failure never undoes or blocks the write; it is reported. It is on
when the state root is a git work tree with an `origin` remote and is not the
code checkout; `catalog_sync: true|false` in the config file forces it, and
`xgc-sim --no-sync` skips it once.

Records are one file per simulation, so two checkouts collide only on the
registry counter. A rejected push is rebased once; when the rebase conflicts
(both sides reserved the same number) `reserve_simulation_id` drops its own
unpushed commit, pulls, and answers `reserved: false` with the number the
series has moved to, instead of handing out a number the human never saw.

### Data directory

Analysis products never enter the catalog repository: PNG, NPZ, SVG, PDF,
HDF5/NetCDF and pulled job `.out` logs (`tools/paths.PRODUCT_SUFFIXES`).
They are regenerable from the remote output with the scripts that *are*
kept, they do not diff, and they were the whole growth of the repository
(130 MB of history on its first day, 95% images and arrays, at roughly
100 MB a month). They live in the **data directory**, `data_dir` in
`~/.config/xgc-automation/config.yaml` or `XGC_DATA_DIR` (default
`<state root>/data`), with the **same relative layout** as the catalog:
`<data_dir>/runs/master/run-<stamp>/summaries/a.png` sits beside the
catalog's `runs/master/run-<stamp>/provenance.json`. Every writer passes its
output path through `paths.product_target`, which redirects exactly those
suffixes and leaves JSON, scripts and provenance where they are; the
catalog's `.gitignore` is the backstop. Agents writing ad-hoc analysis
products follow the same rule: products to the data directory, the script
and a small JSON of the numbers to the catalog.

The data directory is per machine (the Mac holds
`~/Documents/Work/xgc-data/xgc-data-AI`; a cluster checkout keeps its own
under scratch) and is synced on demand with `rsync`; the `.aca` campaign
archive built on completion (`docs/campaign.md`) is the long-term home of
reduced products on the remote side.

## Shot index

Experimental inputs (profile files, equilibria, meshes, diagnostic exports)
have the same two homes. The **files** go to the data directory,
`<data_dir>/shots/<device>/<shot>/`, never into git. The **index** is one
YAML per shot in the catalog repository, `kb/shots/<device>_<shot>.yaml`
(`tools/shots.py`): for each file its path relative to the data directory,
sha256 and size, `kind` (profile, equilibrium, mesh, diagnostic, other),
`diagnostic` (the instrument or source, "CXRS system 3", "Thomson"),
`quantity`, `columns` (the meaning and unit of every column), `origin`
(the e-mail, paper or MDSplus tree it came from) and `used_by`.

`used_by` is derived, never typed: every submission records the sha256 of
each file the input references (`referenced_inputs.files`), and
`xgc-sim shot link` (run automatically after `shot add`) joins the two by
hash, so renaming a file never breaks the link between an experiment and
the runs that used it.

```bash
xgc-sim shot add tcv 69271 ~/Downloads/ti_tcv69271_t1.1_NT.prf --kind profile \
    --diagnostic "CXRS system 3" --quantity T_i \
    --column psi_n="normalised poloidal flux" --column ti="ion temperature [eV]" \
    --origin "TCV team e-mail, 2026-05" --description "NT discharge, PT pair 69508" --time-ms 1100
xgc-sim shot list
xgc-sim shot show tcv 69271
xgc-sim shot link          # after adopting or importing runs
```

The MCP tools are `shot_index` (read) and `register_shot_file` (write,
synced like every catalog write). The agent asks for column meanings and
the diagnostic when they are not stated and never guesses them.

## CSV notebook

`kb/simulation_notebook.csv` is the spreadsheet view of the catalog kept next
to it in the working tree: one row per simulation, the same columns as the
Google Sheet (`tools/sheet_sync.COLUMNS`, including `kind` to separate
production runs from dev tests), the agent's `ai_note` and the human `note`
columns last. It replaces the hand-kept `Other_data/simulation_note_XGC1.xlsx`
going forward; that workbook stays as the historical notebook (rows through
n656po) and is never written by automation, because openpyxl rewrites drop the
cached values of its old formulas and a binary file shows no diff in git.

The CSV is a **derived file and is gitignored** (since 2026-10-07). The
records in `kb/simulations/` are the source of truth and travel through git;
the CSV is rebuilt from them. A fresh clone, or a checkout that has just
pulled, materialises it with

```bash
xgc-sim sync-notebook --confirm
```

and every submission and `set_ai_note` rebuilds it anyway. It used to be
committed; with the catalog written from more than one machine (the Mac and a
remote checkout) that made every submission on one side a merge conflict on
the other, for a whole-file rewrite nobody edits by hand except one column.

The direction is the same as the sheet: machine facts are pushed from the
catalog and overwrite the file; only `note` is pulled back. `ai_note` is
push-only like every machine column: an edit to it in the CSV or the sheet is
overwritten, because it belongs to the agent. A pulled note is believed only
when the CSV file is newer than the record file. A CSV left behind from before
a `git pull` (git gives the pulled record a fresh mtime) would otherwise
overwrite a note written since with `xgc-sim note` on another machine; such
rows are reported as `stale_notes` and the catalog value is pushed over them.

### Human note and AI note

| Field | Author | Written with | Round trip |
|---|---|---|---|
| `note` | the researcher | `xgc-sim note`, `set_simulation_note` (approved wording), or the CSV/Sheet cell | pulled back from CSV and Sheet |
| `ai_note` | the agent | `set_ai_note` MCP tool, `xgc-sim ai-note` | push-only |

The agent writes `ai_note` without asking: what the run is for, what happened
(failures, restarts, key results), and what is next. It replaces the text
instead of appending a log, keeps to a few sentences, and never writes
AI-authored text into `note`.

```bash
xgc-sim sync-notebook            # diff only
xgc-sim sync-notebook --confirm  # pull edited notes, rewrite the CSV
```

Every accepted submission (`hpc_runner.submit`) runs the same sync
automatically, so the CSV is current without anyone remembering to run it.
Editing a `note` cell in a spreadsheet program and saving as CSV is still the
way to annotate a row; the next submission or sync pulls it in. A sync failure
is reported in the submission result and never blocks a job. A note also
survives in git history, which the xlsx never offered.

The historical workbook's year tabs are not reproduced: `submitted_at` and
`created_at` columns make a per-year filter a one-click sort.

## Reproduction evidence

The input namelist alone does not pin a simulation. Right after a submission is
accepted, `hpc_runner.submit` gathers, in one remote command, what a rerun
would need and stores it in the run's `provenance.json` and on the catalog
record:

| block | what it holds | why |
|---|---|---|
| `referenced_inputs` | SHA-256, size and mtime of every mesh, equilibrium and profile file the input names (`sml_node_file`, `eq_filename`, `eq_dens_file`, ...), resolved against `sml_input_file_dir` | these files are edited in place far more often than renamed |
| `build_environment` | executable, commit, env-script hash, and the `CMAKE_BUILD_TYPE` / compiler / `USE_*` / dependency `*_DIR` entries of the build's `CMakeCache.txt` | the startup banner names the commit but not how it was configured |
| `input_backup` | where the input was appended in `input_backup/` | see below |
| run `build` | the XGC startup banner (commit, dirty tree, build type) parsed from stdout on the first status check | what actually ran, not what was planned |

The submission also appends the input to `input_backup/<directory_name>`, the
historical archive's own layout, so `input_backup/` keeps growing with every
numbered simulation instead of stopping where the hand-copied collection did.
The first submission claims the name; a later submission with a different
input is kept as a dated variant (`<directory_name>.Sep10`), which
`tools/input_archive` already treats as a variant rather than a new simulation.
Identical inputs write nothing. The per-run copy under
`runs/master/<run_id>/input` remains the exact text each job ran.

The job script itself is `runs/master/<run_id>/submit.sh`. Hand-submitted runs
adopted later (`origin: adopted`) get the same blocks filled in by hand; their
scripts and banners live under `runs/adopted/<record_id>/` (see its README).

## Google Sheets

The spreadsheet stays, as a view.

The parsed input fields and their derived field/marker/species summaries are
machine-owned columns in this view. As with status and job IDs, sheet edits to
those columns are overwritten by catalog provenance; only `note` is pulled back.

```text
catalog (kb/simulations/*.yaml)  --push-->  Sheet    everything machine-known
catalog                          <--pull--  Sheet    the note column only
```

Every field the automation can determine is pushed and overwrites the sheet. A
status typed into the browser is not evidence of anything; the catalog's status
came from a scheduler. The one exception is `note`, which belongs to the human
and is pulled back before each push, so commenting in the browser works exactly
as it always did.

The sheet is a replica. Losing it costs nothing, and a revoked credential or a
dead network can never block a simulation.

### Setup

Install the optional dependencies:

```bash
uv sync --extra sheets
```

Then, once:

1. Create a Google Cloud service account and download its JSON key.
2. Share the spreadsheet with the service account's email address as **Editor**.
3. Point the toolchain at both, under `common` in `machines.local.yaml`
   (gitignored):

```yaml
common:
  sheet_sync:
    credentials: ~/.config/xgc/sheet-service-account.json
    spreadsheet_id: 1AbC...
    worksheet: simulations
```

`XGC_SHEET_CREDENTIALS`, `XGC_SHEET_ID`, and `XGC_SHEET_WORKSHEET` override
these. Keep the key file outside the repository.

### Use

```bash
xgc-sim sync-sheet --check      # report configuration; connects to nothing
xgc-sim sync-sheet              # show the diff; writes nothing
xgc-sim sync-sheet --confirm    # pull notes, then push
xgc-sim sync-sheet --confirm --no-pull
```

Rows present in the sheet but absent from the catalog are reported rather than
deleted, in case a human added something the catalog has not heard about.

## Archiving

The catalog records where output went; it does not move it. Run `htar`/`hsi`
yourself, then:

```bash
xgc-sim archive n645pe --system hpss --path /home/s/sku/n645pe.tar
```

The value is that the number-to-tape mapping stops living in someone's memory.
Driving the transfer is deliberately out of scope for now.
